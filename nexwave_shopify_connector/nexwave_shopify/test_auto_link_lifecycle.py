# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Committed order lifecycles and current-read ownership checks on real database rows."""

from unittest.mock import patch

import frappe
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from frappe.tests.utils import FrappeTestCase, change_settings
from frappe.utils import CallbackManager, flt, nowdate

from nexwave_shopify_connector.nexwave_shopify._auto_link_test_fixtures import (
	make_item,
	make_mapping,
	make_order,
	make_payload,
	make_store,
)
from nexwave_shopify_connector.nexwave_shopify._inventory_test_fixtures import _get_default_warehouse
from nexwave_shopify_connector.nexwave_shopify.auto_link import auto_link_order_items
from nexwave_shopify_connector.nexwave_shopify.order import _process_order, process_paid_order

test_dependencies = ["Company", "Customer", "Warehouse", "Item"]
INVENTORY_JOB = "nexwave_shopify_connector.nexwave_shopify.inventory.sync_items_inventory"


class TestAutoLinkLifecycle(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.original_after_commit = frappe.db.after_commit
		frappe.db.after_commit = CallbackManager()
		self.stock_entries = []
		self.items = []
		self.observed_inventory_jobs = []
		self.store = make_store(
			warehouse=_get_default_warehouse(),
			default_customer="_Test Customer",
			price_list="_Test Price List",
			cost_center=frappe.db.get_value(
				"Cost Center", {"company": "_Test Company", "is_group": 0}, "name"
			),
			auto_submit_sales_order=1,
			auto_create_invoice=0,
			auto_create_payment_entry=0,
			reserve_stock_for_draft_orders=0,
			enable_webhook_fulfillment=0,
		)
		self.addCleanup(self.cleanup_committed_documents)
		self.item = self.new_item()

	def new_item(self):
		item = make_item()
		self.items.append(item.name)
		return item

	def prepare_stock(self):
		entry = make_stock_entry(
			item_code=self.item.name,
			company=self.store.company,
			to_warehouse=self.store.warehouse,
			qty=5,
			basic_rate=10,
			cost_center=self.store.cost_center,
		)
		self.stock_entries.append(entry.name)

	def payload(self):
		order_id = "auto-link-lifecycle-" + frappe.generate_hash(length=12)
		return {
			"id": order_id,
			"name": "#" + order_id,
			"financial_status": "paid",
			"fulfillment_status": None,
			"currency": frappe.db.get_value("Company", self.store.company, "default_currency"),
			"created_at": nowdate(),
			"taxes_included": False,
			"total_price": "20.00",
			"line_items": [
				make_payload(
					self.item,
					title=self.item.item_code,
					name=self.item.item_code,
					quantity=2,
					price="10.00",
					discount_allocations=[],
					tax_lines=[],
				)
			],
			"shipping_lines": [],
		}

	def observe_inventory_queue(self, method, **kwargs):
		"""Read from a separate connection to prove data committed before enqueue."""
		if method != INVENTORY_JOB:
			return
		connection = frappe.db.get_connection()
		try:
			with connection.cursor() as cursor:
				cursor.execute(
					"SELECT name, docstatus FROM `tabSales Order` WHERE shopify_store = %s",
					(self.store.name,),
				)
				order = cursor.fetchone()
				cursor.execute(
					"""
					SELECT SUM(reserved_qty - delivered_qty)
					FROM `tabStock Reservation Entry`
					WHERE voucher_type = 'Sales Order' AND voucher_no = %s AND docstatus = 1
					""",
					(order[0] if order else None,),
				)
				reserved_qty = cursor.fetchone()[0]
				cursor.execute(
					"""
					SELECT reserved_qty FROM `tabBin` WHERE item_code = %s AND warehouse = %s
					""",
					(self.item.name, self.store.warehouse),
				)
				bin_qty = cursor.fetchone()
				cursor.execute(
					"""
					SELECT inventory_sync_pending FROM `tabItem Shopify Store`
					WHERE parent = %s AND shopify_store = %s AND shopify_variant_id = '601'
					""",
					(self.item.name, self.store.name),
				)
				mapping = cursor.fetchone()
			self.observed_inventory_jobs.append(
				{
					"order": order,
					"reserved_qty": flt(reserved_qty),
					"bin_reserved_qty": flt(bin_qty[0]) if bin_qty else None,
					"pending": mapping[0] if mapping else None,
					"kwargs": kwargs,
				}
			)
		finally:
			connection.close()

	def assert_committed_inventory_job(self, order_name):
		self.assertEqual(len(self.observed_inventory_jobs), 1)
		job = self.observed_inventory_jobs[0]
		self.assertEqual(job["order"], (order_name, 1))
		self.assertEqual(job["reserved_qty"], 2)
		self.assertEqual(job["bin_reserved_qty"], 2)
		self.assertEqual(job["pending"], 1)
		self.assertEqual(job["kwargs"]["store_name"], self.store.name)
		self.assertEqual(job["kwargs"]["item_codes"], [self.item.name])
		self.assertEqual(job["kwargs"]["source"], "order_auto_link")

	@change_settings(
		"Stock Settings", {"enable_stock_reservation": 1, "allow_partial_reservation": 1}, commit=True
	)
	def test_order_import_queues_only_after_submitted_order_and_reservations_commit(self):
		self.prepare_stock()
		with patch("frappe.enqueue", side_effect=self.observe_inventory_queue):
			order_name = _process_order(self.payload(), self.store)
			self.assertEqual(frappe.db.get_value("Sales Order", order_name, "docstatus"), 1)
			self.assertEqual(
				frappe.db.get_value(
					"Item Shopify Store",
					{"parent": self.item.name, "shopify_store": self.store.name},
					"inventory_sync_pending",
				),
				1,
			)
			self.assertEqual(self.observed_inventory_jobs, [])
			frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- exercise caller's final order transaction commit
		self.assert_committed_inventory_job(order_name)

	@change_settings(
		"Stock Settings", {"enable_stock_reservation": 1, "allow_partial_reservation": 1}, commit=True
	)
	def test_paid_event_links_existing_draft_after_setting_is_enabled(self):
		self.prepare_stock()
		frappe.db.set_value("Shopify Store", self.store.name, "auto_link_items_by_sku", 0)
		self.store.reload()
		payload = self.payload()
		payload["financial_status"] = "pending"
		with patch("frappe.enqueue", side_effect=self.observe_inventory_queue):
			order_name = _process_order(payload, self.store)
			frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- persist imported draft before its paid event
			self.assertEqual(frappe.db.get_value("Sales Order", order_name, "docstatus"), 0)
			self.assertFalse(
				frappe.db.exists(
					"Item Shopify Store", {"parent": self.item.name, "shopify_store": self.store.name}
				)
			)
			self.assertEqual(self.observed_inventory_jobs, [])
			frappe.db.set_value("Shopify Store", self.store.name, "auto_link_items_by_sku", 1)
			frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- enable opt-in before a later webhook
			payload["financial_status"] = "paid"
			process_paid_order(payload, shopify_store=self.store.name)
		self.assert_committed_inventory_job(order_name)

	def test_ownership_check_sees_variant_reassigned_after_transaction_snapshot(self):
		other_item = self.new_item()
		owner_row = make_mapping(other_item, self.store, shopify_variant_id="602")
		order = make_order(self.store, {"item_code": self.item.name, "shopify_line_item_id": "701"})
		original_isolation = frappe.db.sql("SELECT @@tx_isolation")[0][0]
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- commit synthetic fixtures and begin a repeatable-read test transaction
		try:
			self.assertEqual(
				frappe.db.sql(
					"SELECT shopify_variant_id FROM `tabItem Shopify Store` WHERE name = %s", owner_row.name
				)[0][0],
				"602",
			)
			connection = frappe.db.get_connection()
			try:
				with connection.cursor() as cursor:
					cursor.execute(
						"UPDATE `tabItem Shopify Store` SET shopify_variant_id = %s WHERE name = %s",
						("601", owner_row.name),
					)
				connection.commit()
			finally:
				connection.close()
			# Prove that an ordinary read still sees the old owner snapshot.
			self.assertEqual(
				frappe.db.sql(
					"SELECT shopify_variant_id FROM `tabItem Shopify Store` WHERE name = %s", owner_row.name
				)[0][0],
				"602",
			)
			with patch("frappe.enqueue") as enqueue:
				linked = auto_link_order_items({"line_items": [make_payload(self.item)]}, self.store, order)
				self.assertEqual(linked, [])
				frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- execute any wrongly registered callbacks in the regression
				enqueue.assert_not_called()
			self.assertFalse(
				frappe.db.exists(
					"Item Shopify Store", {"parent": self.item.name, "shopify_store": self.store.name}
				)
			)
			self.assertEqual(
				frappe.db.get_value("Item Shopify Store", owner_row.name, "shopify_variant_id"), "601"
			)
		finally:
			frappe.db.rollback()
			isolation_commands = {
				"READ-UNCOMMITTED": "SET SESSION TRANSACTION ISOLATION LEVEL READ UNCOMMITTED",
				"READ-COMMITTED": "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
				"REPEATABLE-READ": "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
				"SERIALIZABLE": "SET SESSION TRANSACTION ISOLATION LEVEL SERIALIZABLE",
			}
			frappe.db.sql(isolation_commands[original_isolation])
			frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- restore only this test connection's isolation level

	def cleanup_committed_documents(self):
		"""Cancel and remove only this test's uniquely named committed fixtures."""
		frappe.db.after_commit.reset()
		try:
			for order_name in frappe.get_all(
				"Sales Order", filters={"shopify_store": self.store.name}, pluck="name"
			):
				order = frappe.get_doc("Sales Order", order_name)
				reservations = frappe.get_all(
					"Stock Reservation Entry",
					filters={"voucher_type": "Sales Order", "voucher_no": order_name},
					pluck="name",
				)
				for reservation_name in reservations:
					reservation = frappe.get_doc("Stock Reservation Entry", reservation_name)
					if reservation.docstatus == 1:
						reservation.cancel()
					frappe.delete_doc("Stock Reservation Entry", reservation_name, force=True)
				if order.docstatus == 1:
					order.reload().cancel()
				frappe.delete_doc("Sales Order", order_name, force=True)
			for entry_name in self.stock_entries:
				if not frappe.db.exists("Stock Entry", entry_name):
					continue
				entry = frappe.get_doc("Stock Entry", entry_name)
				if entry.docstatus == 1:
					entry.cancel()
				frappe.delete_doc("Stock Entry", entry_name, force=True)
				frappe.db.delete("GL Entry", {"voucher_type": "Stock Entry", "voucher_no": entry_name})
			for item_code in self.items:
				frappe.db.delete("Stock Ledger Entry", {"item_code": item_code})
				frappe.db.delete("Bin", {"item_code": item_code})
				frappe.delete_doc("Item", item_code, force=True, ignore_missing=True)
			frappe.db.delete("NexWave Shopify Log", {"shopify_store": self.store.name})
			frappe.delete_doc("Shopify Store", self.store.name, force=True)
			frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- remove fixtures persisted by real order lifecycle commits
		finally:
			frappe.db.after_commit.reset()
			frappe.db.after_commit = self.original_after_commit
