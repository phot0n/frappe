from frappe import db



# NOTE: sequences are only available in mariadb >= 10.3

def create_sequence(
	doctype_name: str,
	*,
	slug: str="_id_seq",
	check_not_exists: bool=False,
	cycle: bool=False,
	cache: int=0,
	start_value: int=0,
	increment_by: int=0,
	min_value: int=0,
	max_value: int=0
) -> None:

	query = "create sequence"

	if check_not_exists:
		query += " if not exists"

	query += f" `{doctype_name}{slug}`"

	if cache:
		query += f" cache {cache}"
	elif cache == 0:
		query += " nocache"

	if start_value:
		# default is 1
		query += f" start with {start_value}"

	if increment_by:
		# default is 1
		query += f" increment by {increment_by}"

	if min_value:
		# default is 1
		query += f" min value {min_value}"

	if max_value:
		query += f" max value {max_value}"

	if not cycle:
		query += " nocycle"

	db.sql(query)


def get_next_val(doctype_name: str, slug: str="_id_seq") -> int:
	return db.sql(f"select nextval(`{doctype_name}{slug}`)")[0][0]


def set_next_val(
	doctype_name: str,
	next_val: int,
	*,
	slug: str="_id_seq",
	is_val_used :bool=False
) -> None:

	is_val_used = 0 if not is_val_used else 1
	db.sql(f"SELECT SETVAL(`{doctype_name}{slug}`, {next_val}, {is_val_used})")
