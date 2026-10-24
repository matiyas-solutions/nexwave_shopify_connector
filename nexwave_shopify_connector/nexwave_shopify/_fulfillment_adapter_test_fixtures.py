# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Synthetic providers for the public fulfillment extension contract."""

from typing import ClassVar

import frappe
from frappe import _

from nexwave_shopify_connector.nexwave_shopify.fulfillment_adapter import resolve_fulfillment_demands


class RecordingAdapter:
	instances: ClassVar[list] = []

	def __init__(self):
		self.calls = []
		self.instances.append(self)

	def prepare_sources(self, sales_order, order, store, warehouses):
		self.calls.append(("prepare", bool(sales_order.flags.for_update), warehouses))

	def get_delivery_items(self, sales_order, line_items, warehouse, store):
		self.calls.append(("items", bool(sales_order.flags.for_update), sales_order.items[0].delivered_qty))
		return None


class CustomItemsAdapter(RecordingAdapter):
	def get_delivery_items(self, sales_order, line_items, warehouse, store):
		super().get_delivery_items(sales_order, line_items, warehouse, store)
		return [
			{
				"item_code": row.item_code,
				"description": "Selected by the fulfillment adapter",
				"qty": float(qty),
				"rate": row.rate,
				"uom": row.uom,
				"conversion_factor": row.conversion_factor,
				"warehouse": warehouse,
				"cost_center": store.cost_center,
				"against_sales_order": sales_order.name,
				"so_detail": row.name,
			}
			for row, qty in resolve_fulfillment_demands(sales_order, line_items, store).values()
		]


class EmptyItemsAdapter(RecordingAdapter):
	def get_delivery_items(self, sales_order, line_items, warehouse, store):
		return []


class ReusedItemsAdapter(CustomItemsAdapter):
	"""Return the same owned item list for successive fulfillment requests."""

	def __init__(self):
		super().__init__()
		self.cached_items = None

	def get_delivery_items(self, sales_order, line_items, warehouse, store):
		if self.cached_items is None:
			self.cached_items = super().get_delivery_items(sales_order, line_items, warehouse, store)
		return self.cached_items


class InvalidItemsAdapter(RecordingAdapter):
	def get_delivery_items(self, sales_order, line_items, warehouse, store):
		return {"qty": 1}


class InvalidRowAdapter(RecordingAdapter):
	def get_delivery_items(self, sales_order, line_items, warehouse, store):
		return [None]


class ConstructorFailureAdapter:
	def __init__(self):
		frappe.get_doc({"doctype": "ToDo", "description": "Synthetic adapter constructor failure"}).insert()
		frappe.throw(_("Synthetic adapter constructor failure"))


class PreparationFailureAdapter(RecordingAdapter):
	def prepare_sources(self, sales_order, order, store, warehouses):
		frappe.get_doc({"doctype": "ToDo", "description": "Adapter preparation " + sales_order.name}).insert()
		frappe.throw(_("Synthetic adapter preparation failure"))


class LaterFailureAdapter(RecordingAdapter):
	def get_delivery_items(self, sales_order, line_items, warehouse, store):
		super().get_delivery_items(sales_order, line_items, warehouse, store)
		if len(self.calls) > 2:
			frappe.throw(_("Synthetic adapter item failure"))
		return None


class IncompleteAdapter:
	pass


def make_identity_order(store, rows):
	"""Submit a real order whose repeated items have independent line identities."""
	from frappe.utils import nowdate

	order = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": store.company,
			"customer": "_Test Customer",
			"currency": frappe.get_cached_value("Company", store.company, "default_currency"),
			"transaction_date": nowdate(),
			"delivery_date": nowdate(),
			"shopify_store": store.name,
			"shopify_order_id": frappe.generate_hash(length=12),
			"ignore_pricing_rule": 1,
			"items": [{"item_code": "_Test Item", "warehouse": store.warehouse, **row} for row in rows],
		}
	).insert()
	order.submit()
	return order


def make_shipping_service():
	"""Create a unique non-stock shipping item for fulfillment augmentation."""
	return frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": "_Test Adapter Shipping " + frappe.generate_hash(length=10),
			"item_group": "Products",
			"stock_uom": "Nos",
			"is_stock_item": 0,
		}
	).insert()
