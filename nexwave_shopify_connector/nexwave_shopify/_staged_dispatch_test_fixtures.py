# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Synthetic orders and physical picks for the optional NZ integration tests."""

import frappe
from frappe.utils import nowdate


class StagedDispatchFixture:
	"""Create real staged stock without enabling any Shopify network connection."""

	def __init__(self):
		from erpnext_new_zealand_customizations.nexwave_bin_management.tests.fixtures import (
			TEST_BIN_WAREHOUSE_ID,
		)

		self.warehouse = TEST_BIN_WAREHOUSE_ID
		frappe.db.set_value(
			"Warehouse", self.warehouse, {"enable_wms": 1, "outbound_bin_mode": "Stage Before Dispatch"}
		)
		frappe.clear_document_cache("Warehouse", self.warehouse)
		self.rack = self.make_bin("Storage")
		self.other_rack = self.make_bin("Storage")
		self.stage = self.make_bin("Outbound Staging")
		self.store = self.make_store()
		self.item = self.make_item()

	def make_bin(self, bin_type):
		"""Create isolated bins with an explicit current operational type."""
		return (
			frappe.get_doc(
				{
					"doctype": "Warehouse Bin",
					"warehouse": self.warehouse,
					"bin_code": "SHOP-" + frappe.generate_hash(length=10),
					"bin_type": bin_type,
				}
			)
			.insert()
			.name
		)

	def make_store(self):
		"""Create a disabled store with an explicit test company and warehouse."""
		return frappe.get_doc(
			{
				"doctype": "Shopify Store",
				"shop_domain": "staged-" + frappe.generate_hash(length=10) + ".myshopify.com",
				"enabled": 0,
				"auth_method": "Legacy (Access Token)",
				"company": "_Test Company",
				"cost_center": "Main - _TC",
				"warehouse": self.warehouse,
				"auto_fulfill_remaining_qty": 1,
			}
		).insert()

	def make_item(self, **values):
		"""Create a unique item, optionally using batches or another sales UOM."""
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": "_Test Shopify Staged " + frappe.generate_hash(length=10),
				"item_group": "Products",
				"stock_uom": "_Test UOM",
				"is_stock_item": 1,
				"use_bins": 1,
				**values,
			}
		).insert()

	def order(self, rows=None, store=None, order_id=None):
		"""Submit an order with stable synthetic Shopify line identities."""
		store = store or self.store
		rows = rows if rows is not None else [{"qty": 10, "rate": 10}]
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": store.company,
				"customer": "_Test Customer",
				"currency": frappe.get_cached_value("Company", store.company, "default_currency"),
				"transaction_date": nowdate(),
				"delivery_date": nowdate(),
				"shopify_order_id": order_id or frappe.generate_hash(length=12),
				"shopify_store": store.name,
				"ignore_pricing_rule": 1,
				"items": [
					{
						"item_code": self.item.name,
						"warehouse": self.warehouse,
						"shopify_line_item_id": str(101 + index),
						**row,
					}
					for index, row in enumerate(rows)
				],
			}
		).insert()
		doc.submit()
		return doc

	def pick(self, order, lines, stage=True):
		"""Receive and pick (SO row index, stock quantity, source bin, batch)."""
		from erpnext_new_zealand_customizations.nexwave_bin_management.services import outbound_staging
		from erpnext_new_zealand_customizations.nexwave_bin_management.tests.fixtures import (
			make_stock_entry_with_bin,
		)

		locations = []
		for index, quantity, source_bin, batch in lines:
			row = order.items[index]
			make_stock_entry_with_bin(
				item_code=row.item_code,
				t_warehouse=self.warehouse,
				to_warehouse_bin=source_bin,
				qty=quantity,
				batch_no=batch,
			)
			locations.append(
				{
					"item_code": row.item_code,
					"warehouse": self.warehouse,
					"warehouse_bin": source_bin,
					"qty": quantity / row.conversion_factor,
					"stock_qty": quantity,
					"picked_qty": quantity,
					"uom": row.uom,
					"stock_uom": row.stock_uom,
					"conversion_factor": row.conversion_factor,
					"sales_order": order.name,
					"sales_order_item": row.name,
					"batch_no": batch,
					"use_serial_batch_fields": int(bool(batch)),
				}
			)
		pick = frappe.get_doc(
			{
				"doctype": "Pick List",
				"company": order.company,
				"customer": order.customer,
				"purpose": "Delivery",
				"pick_manually": 1,
				"parent_warehouse": self.warehouse,
				"locations": locations,
			}
		).insert()
		pick.submit()
		if stage:
			preview = outbound_staging.get_staging_preview(pick.name, self.stage)
			outbound_staging.stage_pick_list(
				pick_list=pick.name,
				staging_bin=self.stage,
				request_key=frappe.generate_hash(length=24),
				preview_token=preview["preview_token"],
			)
		return pick.reload()

	def payload(self, order, *lines, fulfillment_id=None):
		"""Build one or more successful fulfillment events for the test order."""
		return {
			"id": order.shopify_order_id,
			"fulfillment_status": "fulfilled",
			"fulfillments": [
				{
					"id": fulfillment_id or frappe.generate_hash(length=12),
					"status": "success",
					"line_items": values,
				}
				for values in lines
			],
		}

	def physical(self, item, warehouse_bin):
		"""Read actual posted stock in one physical bin."""
		return sum(
			frappe.get_all(
				"Stock Ledger Entry",
				filters={
					"item_code": item,
					"warehouse": self.warehouse,
					"warehouse_bin": warehouse_bin,
					"is_cancelled": 0,
				},
				pluck="actual_qty",
			)
		)
