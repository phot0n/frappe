# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import contextlib

# imports - standard imports
import gzip
import json
import os
import subprocess
import sys
from calendar import timegm
from collections.abc import Callable
from datetime import datetime
from glob import glob
from shutil import which

# imports - third party imports
import click
from cryptography.fernet import Fernet

# imports - module imports
import frappe
import frappe.utils
from frappe import _, conf
from frappe.utils import cint, get_file_size, get_url, now, now_datetime

# backup variable for backwards compatibility
verbose = False
compress = False
_verbose = verbose
base_tables = ["__Auth", "__global_search", "__UserSettings"]

BACKUP_ENCRYPTION_CONFIG_KEY = "backup_encryption_key"

# S3 Configuration Keys
S3_BACKUP_CONFIG_KEY = "s3_backup_config"


class BackupGenerator:
	"""
	This class contains methods to perform On Demand Backup

	To initialize, specify (db_name, user, password, db_file_name=None, db_host="127.0.0.1")
	If specifying db_file_name, also append ".sql.gz"
	"""

	def __init__(
		self,
		db_name,
		user,
		password,
		backup_path=None,
		backup_path_db=None,
		backup_path_files=None,
		backup_path_private_files=None,
		db_socket=None,
		db_host=None,
		db_port=None,
		db_type=None,
		backup_path_conf=None,
		ignore_conf=False,
		compress_files=False,
		include_doctypes="",
		exclude_doctypes="",
		verbose=False,
		old_backup_metadata=False,
		rollback_callback=None,
		stream_to_s3=False,
		s3_config=None,
	):
		global _verbose
		self.compress_files = compress_files or compress
		self.db_socket = db_socket
		self.db_host = db_host
		self.db_port = db_port
		self.db_name = db_name
		self.db_type = db_type
		self.user = user
		self.password = password
		self.backup_path = backup_path
		self.backup_path_conf = backup_path_conf
		self.backup_path_db = backup_path_db
		self.backup_path_files = backup_path_files
		self.backup_path_private_files = backup_path_private_files
		self.ignore_conf = ignore_conf
		self.include_doctypes = include_doctypes
		self.exclude_doctypes = exclude_doctypes
		self.partial = False
		self.old_backup_metadata = old_backup_metadata
		self.rollback_callback = rollback_callback

		# S3 Streaming Configuration
		self.stream_to_s3 = stream_to_s3
		self.s3_config = s3_config or self._get_s3_config_from_site()

		if self.stream_to_s3:
			self._validate_s3_config()
			self._validate_rclone()

		site = frappe.local.site or frappe.generate_hash(length=8)
		self.site_slug = site.replace(".", "_")
		self.verbose = verbose
		self.setup_backup_directory()
		self.setup_backup_tables()
		_verbose = verbose

	def _get_s3_config_from_site(self):
		"""Get S3 configuration from site config"""
		return frappe.conf.get(S3_BACKUP_CONFIG_KEY, {})

	def _validate_s3_config(self):
		"""Validate required S3 configuration parameters"""
		if not self.s3_config:
			frappe.throw(_("S3 configuration is required for streaming backups to S3"))

		required_fields = ["remote_name"]
		missing_fields = [field for field in required_fields if not self.s3_config.get(field)]

		if missing_fields:
			frappe.throw(_("Missing required S3 configuration fields: {0}").format(", ".join(missing_fields)))

	def _validate_rclone(self):
		"""Check if rclone is available"""
		if not which("rclone"):
			frappe.throw(
				_("rclone not found in PATH! This is required to stream backups to S3"),
				exc=frappe.ExecutableNotFound,
			)

	def _get_rclone_remote_path(self, filename):
		"""Generate rclone remote path"""
		remote_name = self.s3_config["remote_name"]
		prefix = self.s3_config.get("prefix", "backups/")

		# Construct path: remote:prefix/site_slug/filename
		path = f"{remote_name}:{prefix}{self.site_slug}/{filename}"
		return path

	def setup_backup_directory(self):
		specified = (
			self.backup_path
			or self.backup_path_db
			or self.backup_path_files
			or self.backup_path_private_files
			or self.backup_path_conf
		)

		if not specified:
			backups_folder = get_backup_path()
			if not os.path.exists(backups_folder):
				os.makedirs(backups_folder, exist_ok=True)
		else:
			if self.backup_path:
				os.makedirs(self.backup_path, exist_ok=True)

			for file_path in {
				self.backup_path_files,
				self.backup_path_db,
				self.backup_path_private_files,
				self.backup_path_conf,
			}:
				if file_path:
					dir = os.path.dirname(file_path)
					os.makedirs(dir, exist_ok=True)

	def _set_existing_tables(self):
		"""Ensure self._existing_tables is set."""
		if not hasattr(self, "_existing_tables"):
			self._existing_tables = frappe.db.get_tables()

	def setup_backup_tables(self):
		"""Set self.backup_includes, self.backup_excludes based on include_doctypes, exclude_doctypes"""
		self._set_existing_tables()

		self.backup_includes = _get_tables(self.include_doctypes.strip().split(","), self._existing_tables)
		self.backup_excludes = _get_tables(self.exclude_doctypes.strip().split(","), self._existing_tables)

		self.set_backup_tables_from_config()
		self.partial = (self.backup_includes or self.backup_excludes) and not self.ignore_conf

	def set_backup_tables_from_config(self):
		"""Set self.backup_includes, self.backup_excludes based on site config"""
		if self.ignore_conf:
			return

		backup_conf = frappe.conf.get("backup", {})
		self._set_existing_tables()
		if not self.backup_includes:
			if specified_tables := _get_tables(backup_conf.get("includes", []), self._existing_tables):
				self.backup_includes = specified_tables + base_tables
			else:
				self.backup_includes = []

		if not self.backup_excludes:
			self.backup_excludes = _get_tables(backup_conf.get("excludes", []), self._existing_tables)

	@property
	def site_config_backup_path(self):
		# For backwards compatibility
		click.secho(
			"BackupGenerator.site_config_backup_path has been deprecated in favour of"
			" BackupGenerator.backup_path_conf",
			fg="yellow",
		)
		return getattr(self, "backup_path_conf", None)

	def get_backup(self, older_than=24, ignore_files=False, force=False):
		"""
		Takes a new dump if existing file is old
		and sends the link to the file as email
		"""
		# Check if file exists and is less than a day old
		# If not Take Dump
		if not force:
			if self.stream_to_s3:
				# For S3, always force new backup (no local file checking)
				last_db, last_file, last_private_file, site_config_backup_path = (False, False, False, False)
			else:
				(
					last_db,
					last_file,
					last_private_file,
					site_config_backup_path,
				) = self.get_recent_backup(older_than)
		else:
			last_db, last_file, last_private_file, site_config_backup_path = (
				False,
				False,
				False,
				False,
			)

		if not (
			self.backup_path_conf
			and self.backup_path_db
			and self.backup_path_files
			and self.backup_path_private_files
		):
			self.set_backup_file_name()

		if not (last_db and last_file and last_private_file and site_config_backup_path):
			self.delete_if_step_fails(self.take_dump, self.backup_path_db)
			self.delete_if_step_fails(self.copy_site_config, self.backup_path_conf)
			if not ignore_files:
				self.delete_if_step_fails(
					self.backup_files, self.backup_path_files, self.backup_path_private_files
				)

			if frappe.get_system_settings("encrypt_backup"):
				if self.stream_to_s3:
					click.secho(
						"Encryption is not yet supported with S3 streaming. "
						"Backups will be uploaded without encryption.",
						fg="yellow",
					)
				else:
					self.backup_encryption()

		else:
			self.backup_path_files = last_file
			self.backup_path_db = last_db
			self.backup_path_private_files = last_private_file
			self.backup_path_conf = site_config_backup_path

	def set_backup_file_name(self):
		partial = "-partial" if self.partial else ""
		ext = "tgz" if self.compress_files else "tar"
		enc = "-enc" if frappe.get_system_settings("encrypt_backup") and not self.stream_to_s3 else ""
		self.todays_date = now_datetime().strftime("%Y%m%d_%H%M%S")

		for_conf = f"{self.todays_date}-{self.site_slug}-site_config_backup{enc}.json"
		for_db = f"{self.todays_date}-{self.site_slug}{partial}-database{enc}.sql.gz"
		for_public_files = f"{self.todays_date}-{self.site_slug}-files{enc}.{ext}"
		for_private_files = f"{self.todays_date}-{self.site_slug}-private-files{enc}.{ext}"

		if self.stream_to_s3:
			# For S3, use rclone remote paths
			self.backup_path_conf = self._get_rclone_remote_path(for_conf)
			self.backup_path_db = self._get_rclone_remote_path(for_db)
			self.backup_path_files = self._get_rclone_remote_path(for_public_files)
			self.backup_path_private_files = self._get_rclone_remote_path(for_private_files)
		else:
			backup_path = self.backup_path or get_backup_path()
			if not self.backup_path_conf:
				self.backup_path_conf = os.path.join(backup_path, for_conf)
			if not self.backup_path_db:
				self.backup_path_db = os.path.join(backup_path, for_db)
			if not self.backup_path_files:
				self.backup_path_files = os.path.join(backup_path, for_public_files)
			if not self.backup_path_private_files:
				self.backup_path_private_files = os.path.join(backup_path, for_private_files)

	def backup_encryption(self):
		"""
		Encrypt all the backups created using gpg.
		"""
		if which("gpg") is None:
			click.secho("Please install `gpg` and ensure its available in your PATH", fg="red")
			sys.exit(1)
		paths = (self.backup_path_db, self.backup_path_files, self.backup_path_private_files)
		for path in paths:
			if os.path.exists(path):
				cmd_string = "gpg --yes --passphrase {passphrase} --pinentry-mode loopback -c {filelocation}"
				try:
					command = cmd_string.format(
						passphrase=get_or_generate_backup_encryption_key(),
						filelocation=path,
					)

					frappe.utils.execute_in_shell(command)
					os.rename(path + ".gpg", path)

				except Exception as err:
					print(err)
					click.secho(
						"Error occurred during encryption. Files are stored without encryption.", fg="red"
					)

	def get_recent_backup(self, older_than, partial=False):
		backup_path = get_backup_path()
		separator = suffix = ""
		if partial:
			separator = "*"

		if frappe.get_system_settings("encrypt_backup"):
			suffix = "-enc"

		file_type_slugs = {
			"database": f"*-{{}}-{separator}database{suffix}.sql.gz",
			"public": f"*-{{}}-files{suffix}.tar",
			"private": f"*-{{}}-private-files{suffix}.tar",
			"config": f"*-{{}}-site_config_backup{suffix}.json",
		}

		def backup_time(file_path):
			file_name = file_path.split(os.sep)[-1]
			file_timestamp = file_name.split("-", 1)[0]
			return timegm(datetime.strptime(file_timestamp, "%Y%m%d_%H%M%S").utctimetuple())

		def get_latest(file_pattern):
			file_pattern = os.path.join(backup_path, file_pattern.format(self.site_slug))
			file_list = glob(file_pattern)
			if file_list:
				return max(file_list, key=backup_time)

		def old_enough(file_path):
			if file_path:
				if not os.path.isfile(file_path) or is_file_old(file_path, older_than):
					return None
				return file_path

		latest_backups = {file_type: get_latest(pattern) for file_type, pattern in file_type_slugs.items()}

		recent_backups = {file_type: old_enough(file_name) for file_type, file_name in latest_backups.items()}

		return (
			recent_backups.get("database"),
			recent_backups.get("public"),
			recent_backups.get("private"),
			recent_backups.get("config"),
		)

	def zip_files(self):
		# For backwards compatibility - pre v13
		click.secho(
			"BackupGenerator.zip_files has been deprecated in favour of BackupGenerator.backup_files",
			fg="yellow",
		)
		return self.backup_files()

	def get_summary(self):
		summary = {
			"config": {
				"path": self.backup_path_conf,
				"size": self._get_backup_size(self.backup_path_conf),
			},
			"database": {
				"path": self.backup_path_db,
				"size": self._get_backup_size(self.backup_path_db),
			},
		}

		if self.stream_to_s3:
			# For S3, include file backups
			summary.update(
				{
					"public": {
						"path": self.backup_path_files,
						"size": self._get_backup_size(self.backup_path_files),
					},
					"private": {
						"path": self.backup_path_private_files,
						"size": self._get_backup_size(self.backup_path_private_files),
					},
				}
			)
		elif os.path.exists(self.backup_path_files) and os.path.exists(self.backup_path_private_files):
			summary.update(
				{
					"public": {
						"path": self.backup_path_files,
						"size": get_file_size(self.backup_path_files, format=True),
					},
					"private": {
						"path": self.backup_path_private_files,
						"size": get_file_size(self.backup_path_private_files, format=True),
					},
				}
			)

		return summary

	def _get_backup_size(self, path):
		"""Get size of backup, handling both local files and rclone remotes"""
		if self.stream_to_s3:
			try:
				# Use rclone to get file size
				result = subprocess.run(
					["rclone", "size", path, "--json"],
					capture_output=True,
					text=True,
					check=True,
					verbose=self.verbose,
				)
				data = json.loads(result.stdout)
				size_bytes = data.get("bytes", 0)
				return get_file_size(size_bytes, format=True)
			except Exception as e:
				if self.verbose:
					print(f"Could not get size for {path}: {e}")
				return "-"

		return get_file_size(path, format=True)

	def print_summary(self):
		backup_summary = self.get_summary()
		print(f"Backup Summary for {frappe.local.site} at {now()}")

		title = max(len(x) for x in backup_summary)
		path = max(len(x["path"]) for x in backup_summary.values())

		for _type, info in backup_summary.items():
			template = f"{{0:{title}}}: {{1:{path}}} {{2}}"
			# TODO: abspath wont work for s3
			print(template.format(_type.title(), os.path.abspath(info["path"]), info["size"]))

	def backup_files(self):
		"""Backup public and private files, streaming to S3 if configured"""
		for folder in ("public", "private"):
			files_path = frappe.get_site_path(folder, "files")
			backup_path = self.backup_path_files if folder == "public" else self.backup_path_private_files

			if self.stream_to_s3:
				command = self._get_stream_files_to_s3_cmd(files_path, backup_path)
			else:
				command = self._get_backup_files_locally_cmd(files_path, backup_path)

			try:
				frappe.utils.execute_in_shell(
					command,
					verbose=self.verbose,
					low_priority=True,
					check_exit_code=True,
				)
			except frappe.CommandFailedError as e:
				if e.err and "file changed as we read it" in e.err:
					click.secho(
						"Ignoring `tar: file changed as we read it` to prevent backup failure",
						fg="red",
					)
				else:
					raise e

	def _backup_files_locally(self, files_path, backup_path):
		"""Backup files to local filesystem"""
		if self.compress_files:
			cmd_string = "set -o pipefail; tar cf - {1} | gzip > {0}"
		else:
			cmd_string = "tar -cf {0} {1}"

		return cmd_string.format(backup_path, files_path)

	def _stream_files_to_s3(self, files_path, remote_path):
		if self.compress_files:
			cmd_string = "set -o pipefail; tar cf - {0} | gzip | rclone rcat {1}"
		else:
			cmd_string = "set -o pipefail; tar cf - {0} | rclone rcat {1}"

		return cmd_string.format(files_path, remote_path)

	def copy_site_config(self):
		site_config_path = os.path.join(frappe.get_site_path(), "site_config.json")

		if self.stream_to_s3:
			# Upload site config to S3 using rclone
			command = f"rclone copyto {site_config_path} {self.backup_path_conf}"
			frappe.utils.execute_in_shell(
				command,
				verbose=self.verbose,
				check_exit_code=True,
			)
		else:
			# Save locally
			with open(self.backup_path_conf, "w") as n, open(site_config_path) as c:
				n.write(c.read())

	def take_dump(self):
		if self.db_type == "sqlite":
			from pathlib import Path

			import frappe

			db_path = Path(frappe.get_site_path()) / "db" / f"{self.db_name}.db"
			if self.stream_to_s3:
				command = f"gzip -k {db_path} -c | rclone rcat {self.backup_path_db}"
			else:
				command = f"gzip -k {db_path} -c > {self.backup_path_db}"
			frappe.utils.execute_in_shell(
				command, low_priority=True, check_exit_code=True, verbose=self.verbose
			)
			return

		import shlex

		import frappe.utils
		from frappe.utils.change_log import get_app_branch

		gzip_exc: str = which("gzip")
		if not gzip_exc:
			frappe.throw(
				_("gzip not found in PATH! This is required to take a backup."), exc=frappe.ExecutableNotFound
			)

		if self.old_backup_metadata:
			database_header_content = [
				f"Backup generated by Frappe {frappe.__version__} on branch {get_app_branch('frappe') or 'N/A'}",
				"",
			]
		else:
			database_header_content = [
				"begin frappe metadata",
				"[frappe]",
				f"version = {frappe.__version__}",
				f"branch = {get_app_branch('frappe') or 'N/A'}",
				"end frappe metadata",
				"",
			]

		if self.backup_includes:
			backup_info = ("Backing Up Tables: ", ", ".join(self.backup_includes))
		elif self.backup_excludes:
			backup_info = ("Skipping Tables: ", ", ".join(self.backup_excludes))

		if self.partial:
			if self.verbose:
				print("".join(backup_info), "\n")
			database_header_content.extend(
				[
					f"Partial Backup of Frappe Site {frappe.local.site}",
					("Backup contains: " if self.backup_includes else "Backup excludes: ") + backup_info[1],
					"",
				]
			)

		cmd = []
		extra = []
		if self.db_type == "mariadb":
			if self.backup_includes:
				extra.extend(self.backup_includes)
			elif self.backup_excludes:
				extra.extend([f"--ignore-table={self.db_name}.{table}" for table in self.backup_excludes])

		elif self.db_type == "postgres":
			if self.backup_includes:
				extra.extend([f'--table=public."{table}"' for table in self.backup_includes])
			elif self.backup_excludes:
				extra.extend([f'--exclude-table-data=public."{table}"' for table in self.backup_excludes])

		from frappe.database import get_command

		bin, args, bin_name = get_command(
			socket=self.db_socket,
			host=self.db_host,
			port=self.db_port,
			user=self.user,
			password=self.password,
			db_name=self.db_name,
			extra=extra,
			dump=True,
		)
		if not bin:
			frappe.throw(
				_("{} not found in PATH! This is required to take a backup.").format(bin_name),
				exc=frappe.ExecutableNotFound,
			)
		cmd.append(bin)
		cmd.append(shlex.join(args))

		cmd_parts = ["set -o pipefail;", *cmd, "|", gzip_exc]
		if self.stream_to_s3:
			# TODO: add generated header
			cmd_parts.extend(["|", "rclone rcat", self.backup_path_db])
		else:
			generated_header = "\n".join(f"-- {x}" for x in database_header_content) + "\n"
			with gzip.open(self.backup_path_db, "wt") as f:
				f.write(generated_header)

			cmd_parts.extend([">>", self.backup_path_db])
		command = " ".join(cmd_parts)

		if self.verbose:
			print(command.replace(shlex.quote(self.password), "*" * 10) + "\n")

		frappe.utils.execute_in_shell(command, low_priority=True, check_exit_code=True)

	def add_to_rollback(self, func: Callable) -> None:
		"""
		Adds the given callable to the rollback CallbackManager stack

		:param func: The callable to add to the rollback stack
		:return: Nothing
		"""
		if self.rollback_callback:
			self.rollback_callback.add(func)

	def delete_if_step_fails(self, step: Callable, *paths: str):
		"""
		Deletes the given path if the given step fails

		:param step: The step to execute
		:param paths: The paths to delete
		:return: Nothing
		"""
		try:
			step()
		except Exception as e:
			if self.stream_to_s3:
				# Delete from S3 using rclone
				for path in paths:
					try:
						subprocess.run(["rclone", "deletefile", path], check=False, capture_output=True)
						if self.verbose:
							print(f"Deleted failed backup from S3: {path}")
					except Exception as del_err:
						if self.verbose:
							print(f"Failed to delete S3 object {path}: {del_err}")
			else:
				# Delete local files
				for path in paths:
					if os.path.exists(path):
						os.remove(path)
			raise e

		if self.stream_to_s3:
			# Add S3 deletion to rollback
			for path in paths:
				self.add_to_rollback(
					lambda p=path: subprocess.run(
						["rclone", "deletefile", p], check=False, capture_output=True
					)
				)
		else:
			# Add local file deletion to rollback
			for path in paths:
				self.add_to_rollback(lambda p=path: os.path.exists(p) and os.remove(p))


def _get_tables(doctypes: list[str], existing_tables: list[str]) -> list[str]:
	"""Return a list of tables for the given doctypes that exist in the database."""
	tables = []
	for doctype in doctypes:
		if not doctype:
			continue
		table = frappe.utils.get_table_name(doctype)
		if table in existing_tables:
			tables.append(table)
	return tables


@frappe.whitelist()
def fetch_latest_backups(partial=False) -> dict:
	"""Fetch paths of the latest backup taken in the last 30 days.

	Note: Only for System Managers

	Return:
	        dict: relative Backup Paths
	"""
	frappe.only_for("System Manager")
	odb = BackupGenerator(
		frappe.conf.db_name,
		frappe.conf.db_user,
		frappe.conf.db_password,
		db_socket=frappe.conf.db_socket,
		db_host=frappe.conf.db_host,
		db_port=frappe.conf.db_port,
		db_type=frappe.conf.db_type,
	)
	database, public, private, config = odb.get_recent_backup(older_than=24 * 30, partial=partial)

	return {"database": database, "public": public, "private": private, "config": config}


def scheduled_backup(
	older_than=6,
	ignore_files=False,
	backup_path=None,
	backup_path_db=None,
	backup_path_files=None,
	backup_path_private_files=None,
	backup_path_conf=None,
	ignore_conf=False,
	include_doctypes="",
	exclude_doctypes="",
	compress=False,
	force=False,
	verbose=False,
	old_backup_metadata=False,
	rollback_callback=None,
	stream_to_s3=False,
	s3_config=None,
):
	"""this function is called from scheduler
	deletes backups older than 7 days
	takes backup"""
	return new_backup(
		older_than=older_than,
		ignore_files=ignore_files,
		backup_path=backup_path,
		backup_path_db=backup_path_db,
		backup_path_files=backup_path_files,
		backup_path_private_files=backup_path_private_files,
		backup_path_conf=backup_path_conf,
		ignore_conf=ignore_conf,
		include_doctypes=include_doctypes,
		exclude_doctypes=exclude_doctypes,
		compress=compress,
		force=force,
		verbose=verbose,
		old_backup_metadata=old_backup_metadata,
		rollback_callback=rollback_callback,
		stream_to_s3=stream_to_s3,
		s3_config=s3_config,
	)


def new_backup(
	older_than=6,
	ignore_files=False,
	backup_path=None,
	backup_path_db=None,
	backup_path_files=None,
	backup_path_private_files=None,
	backup_path_conf=None,
	ignore_conf=False,
	include_doctypes="",
	exclude_doctypes="",
	compress=False,
	force=False,
	verbose=False,
	old_backup_metadata=False,
	rollback_callback=None,
	stream_to_s3=False,
	s3_config=None,
):
	if not stream_to_s3:
		delete_temp_backups()

	odb = BackupGenerator(
		frappe.conf.db_name,
		frappe.conf.db_user,
		frappe.conf.db_password,
		db_socket=frappe.conf.db_socket,
		db_host=frappe.conf.db_host,
		db_port=frappe.conf.db_port,
		db_type=frappe.conf.db_type,
		backup_path=backup_path,
		backup_path_db=backup_path_db,
		backup_path_files=backup_path_files,
		backup_path_private_files=backup_path_private_files,
		backup_path_conf=backup_path_conf,
		ignore_conf=ignore_conf,
		include_doctypes=include_doctypes,
		exclude_doctypes=exclude_doctypes,
		verbose=verbose,
		compress_files=compress,
		old_backup_metadata=old_backup_metadata,
		rollback_callback=rollback_callback,
		stream_to_s3=stream_to_s3,
		s3_config=s3_config,
	)
	odb.get_backup(older_than, ignore_files, force=force)
	return odb


def delete_temp_backups(older_than=24):
	"""
	Cleans up the backup_link_path directory by deleting older files
	"""
	older_than = cint(frappe.conf.keep_backups_for_hours) or older_than
	backup_path = get_backup_path()
	if os.path.exists(backup_path):
		file_list = os.listdir(get_backup_path())
		for this_file in file_list:
			this_file_path = os.path.join(get_backup_path(), this_file)
			if is_file_old(this_file_path, older_than):
				os.remove(this_file_path)


def is_file_old(file_path, older_than=24) -> bool:
	"""Return True if file exists and is older than specified hours."""
	if os.path.isfile(file_path):
		from datetime import timedelta

		# Get timestamp of the file
		file_datetime = datetime.fromtimestamp(os.stat(file_path).st_ctime)
		if datetime.today() - file_datetime >= timedelta(hours=older_than):
			if _verbose:
				print(f"File {file_path} is older than {older_than} hours")
			return True
		else:
			if _verbose:
				print(f"File {file_path} is recent")
			return False
	else:
		if _verbose:
			print(f"File {file_path} does not exist")
		return True


def get_backup_path():
	return frappe.utils.get_site_path(conf.get("backup_path", "private/backups"))


@frappe.whitelist()
def get_backup_encryption_key():
	frappe.only_for("System Manager")
	return get_or_generate_backup_encryption_key()


def get_or_generate_backup_encryption_key():
	from frappe.installer import update_site_config

	key = frappe.conf.get(BACKUP_ENCRYPTION_CONFIG_KEY)
	if key:
		return key

	key = Fernet.generate_key().decode()
	update_site_config(BACKUP_ENCRYPTION_CONFIG_KEY, key)

	return key


@contextlib.contextmanager
def decrypt_backup(file_path: str, passphrase: str):
	if which("gpg") is None:
		click.secho("Please install `gpg` and ensure its available in your PATH", fg="red")
		sys.exit(1)
	if not os.path.exists(file_path):
		print("Invalid path: ", file_path)
		return
	else:
		file_path_with_ext = file_path + ".gpg"
		os.rename(file_path, file_path_with_ext)

		cmd_string = "gpg --yes --passphrase {passphrase} --pinentry-mode loopback -o {decrypted_file} -d {file_location}"
		command = cmd_string.format(
			passphrase=passphrase,
			file_location=file_path_with_ext,
			decrypted_file=file_path,
		)
	frappe.utils.execute_in_shell(command)
	try:
		yield
	finally:
		if os.path.exists(file_path_with_ext):
			if os.path.exists(file_path):
				os.remove(file_path)
			if os.path.exists(file_path.rstrip(".gz")):
				os.remove(file_path.rstrip(".gz"))
			os.rename(file_path_with_ext, file_path)


def backup(
	with_files=False,
	backup_path_db=None,
	backup_path_files=None,
	backup_path_private_files=None,
	backup_path_conf=None,
	stream_to_s3=False,
	s3_config=None,
):
	"Backup"
	odb = scheduled_backup(
		ignore_files=not with_files,
		backup_path_db=backup_path_db,
		backup_path_files=backup_path_files,
		backup_path_private_files=backup_path_private_files,
		backup_path_conf=backup_path_conf,
		force=True,
		stream_to_s3=stream_to_s3,
		s3_config=s3_config,
	)
	return {
		"backup_path_db": odb.backup_path_db,
		"backup_path_files": odb.backup_path_files,
		"backup_path_private_files": odb.backup_path_private_files,
	}
