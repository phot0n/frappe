import frappe
from frappe.model.document import Document


class RedisDocument(Document):
	_namespace_prefix = "redoc:"

	def __setup__(self):
		self._namespace = RedisDocument.get_namespace(self.doctype)

	@staticmethod
	def get_namespace(doctype):
		return f"{RedisDocument._namespace_prefix}{frappe.local.site}:{doctype}"

	@staticmethod
	def get_list(args) -> list[frappe._dict]:
		"""Similar to reportview.get_list"""
		return list(frappe.cache().hgetall(RedisDocument.get_namespace(args.doctype)).values())

	@staticmethod
	def get_count(args) -> int:
		"""Similar to reportview.get_count, return total count of documents on listview."""
		return frappe.cache().hlen(RedisDocument.get_namespace(args.doctype))

	@staticmethod
	def get_stats(args):
		"""Similar to reportview.get_stats, return sidebar stats."""
		...

	def db_insert(self, *args, **kwargs) -> None:
		"""Serialize the `Document` object and insert it in backend."""
		if not self.name:
			self.name = frappe.generate_hash(length=10)

		doc = self.as_dict()
		for attr in ("__islocal", "__unsaved"):
			doc.pop(attr, None)

		frappe.cache().hset(self._namespace, self.name, doc)

	def load_from_db(self) -> None:
		"""Using self.name initialize current document from backend data.

		This is responsible for updatinng __dict__ of class with all the fields on doctype."""

		# TODO: on reloading a new-doctype-1 doc, it given an invalid argument error

		doc_values = frappe.cache().hget(RedisDocument.get_namespace(self.doctype), self.name)
		super().__init__(doc_values)

	def db_update(self, *args, **kwargs) -> None:
		"""Serialize the `Document` object and update existing document in backend."""
		self.db_insert(*args, **kwargs)

	def delete(self, *args, **kwargs) -> None:
		"""Delete the current document from backend"""
		frappe.cache().hdel(self._namespace, self.name)
