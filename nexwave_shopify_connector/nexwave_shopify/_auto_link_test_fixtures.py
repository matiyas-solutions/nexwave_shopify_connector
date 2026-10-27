# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Real-document fixtures for order-driven Shopify item linking."""

import frappe
from frappe.utils import nowdate

from nexwave_shopify_connector.nexwave_shopify._inventory_test_fixtures import (
	_get_default_warehouse,
	ensure_test_item,
	ensure_test_shopify_store,
)


def make_store(**overrides):
	values = {
		"shop_domain": "_test-auto-link-" + frappe.generate_hash(length=10) + ".myshopify.com",
		"auto_link_items_by_sku": 1,
		"warehouse": _get_default_warehouse(),
		**overrides,
	}
	return ensure_test_shopify_store(**values)


def make_item(**overrides):
	return ensure_test_item("_Test Auto Link " + frappe.generate_hash(length=10), **overrides)


def make_order(store, *items):
	"""Persist an imported draft order without triggering stock movements."""
	return frappe.get_doc(
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
			"items": [{"qty": 1, "rate": 10, "warehouse": store.warehouse, **item} for item in items],
		}
	).insert(ignore_permissions=True)


def make_mapping(item, store, **overrides):
	"""Persist child rows directly, as the linking path itself does."""
	item.reload()
	row = item.append(
		"shopify_stores",
		{
			"shopify_store": store.name,
			"enabled": 1,
			"shopify_product_id": "501",
			"shopify_variant_id": "601",
			"shopify_sku": item.item_code,
			**overrides,
		},
	)
	row.db_insert()
	return row


def make_payload(item, **overrides):
	return {
		"id": "701",
		"sku": item.item_code,
		"product_id": 501,
		"variant_id": 601,
		"quantity": 1,
		**overrides,
	}
