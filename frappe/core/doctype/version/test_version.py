# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt
import frappe
import unittest, copy
from frappe.test_runner import make_test_objects
from frappe.core.doctype.version.version import get_diff

class TestVersion(unittest.TestCase):
	def test_get_diff(self):
		frappe.set_user('Administrator')
		test_records = make_test_objects('Event', reset = True)
		old_doc = frappe.get_doc("Event", test_records[0])
		new_doc = copy.deepcopy(old_doc)

		old_doc.color = None
		new_doc.color = '#fafafa'

		diff = get_diff(old_doc, new_doc)['changed']

		self.assertEqual(get_fieldnames(diff)[0], 'color')
		self.assertTrue(get_old_values(diff)[0] is None)
		self.assertEqual(get_new_values(diff)[0], '#fafafa')

		new_doc.starts_on = "2017-07-20"

		diff = get_diff(old_doc, new_doc)['changed']

		self.assertEqual(get_fieldnames(diff)[1], 'starts_on')
		self.assertEqual(get_old_values(diff)[1], '01-01-2014 00:00:00')
		self.assertEqual(get_new_values(diff)[1], '07-20-2017 00:00:00')

def get_fieldnames(change_array):
	return [d[0] for d in change_array]

def get_old_values(change_array):
	return [d[1] for d in change_array]

def get_new_values(change_array):
	return [d[2] for d in change_array]
