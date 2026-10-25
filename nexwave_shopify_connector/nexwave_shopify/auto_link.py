# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Opt-in store mappings learned from successfully imported order lines."""

from functools import partial

import frappe
from frappe.utils import cint, cstr

from nexwave_shopify_connector.utils.logger import get_logger


def auto_link_order_items(order: dict, store, sales_order) -> list[str]:
	"""Link existing items without saving Item or publishing Shopify products.

	Call after the order's stock-affecting phases. Mapping writes remain in
	the caller's transaction; the inventory worker starts only after commit.
	"""
	if (
		not store.enabled
		or not cint(store.get("auto_link_items_by_sku"))
		or sales_order.docstatus == 2
		or sales_order.get("shopify_store") != store.name
	):
		return []

	# Serialize ownership checks for this store, including different Items
	# competing for the same variant. Re-read settings under the lock.
	store = frappe.get_doc("Shopify Store", store.name, for_update=True)
	if not store.enabled or not cint(store.get("auto_link_items_by_sku")):
		return []

	candidates = {}
	for line in order.get("line_items", []):
		sku = cstr(line.get("sku")).strip()
		product_id = _shopify_id(line.get("product_id"))
		variant_id = _shopify_id(line.get("variant_id"))
		if not sku or not product_id or not variant_id:
			continue

		matches = frappe.get_all(
			"Item",
			or_filters=[["name", "=", sku], ["item_code", "=", sku]],
			fields=["name", "disabled", "is_stock_item"],
		)
		if len(matches) != 1 or matches[0].disabled:
			continue
		item_code = matches[0].name
		line_id = cstr(line.get("id"))
		if not any(
			row.item_code == item_code and (not line_id or cstr(row.get("shopify_line_item_id")) == line_id)
			for row in sales_order.items
		):
			continue
		candidates[(item_code, variant_id)] = (product_id, sku)

	variant_owners = {}
	for item_code, variant_id in candidates:
		variant_owners.setdefault(variant_id, set()).add(item_code)

	linked = set()
	stock_items = set()
	for (item_code, variant_id), (product_id, sku) in sorted(candidates.items()):
		if len(variant_owners[variant_id]) > 1:
			_log_warning(
				store.name, item_code, variant_id, "Order lines match different items for the same variant."
			)
			continue
		item = frappe.get_doc("Item", item_code, for_update=True)
		if item.disabled:
			continue
		owners = frappe.db.get_values(
			"Item Shopify Store",
			filters={
				"parenttype": "Item",
				"parentfield": "shopify_stores",
				"shopify_store": store.name,
				"shopify_variant_id": variant_id,
			},
			fieldname=["parent"],
			as_dict=True,
			for_update=True,
		)
		if any(row.parent != item_code for row in owners):
			_log_warning(store.name, item_code, variant_id, "The variant is already linked to another item.")
			continue

		rows = [row for row in item.shopify_stores if row.shopify_store == store.name]
		exact = [row for row in rows if cstr(row.shopify_variant_id) == variant_id]
		stubs = [row for row in rows if not row.shopify_variant_id]
		if any(not row.enabled for row in exact + stubs):
			continue
		if len(exact) > 1 or (not exact and len(stubs) > 1):
			_log_warning(store.name, item_code, variant_id, "Multiple existing store links need review.")
			continue
		row = exact[0] if exact else (stubs[0] if stubs else None)
		if row and row.shopify_product_id and cstr(row.shopify_product_id) != product_id:
			_log_warning(store.name, item_code, variant_id, "The existing link has a different product ID.")
			continue
		if row and row.shopify_product_id and row.shopify_variant_id:
			continue

		values = {
			"shopify_product_id": product_id,
			"shopify_variant_id": variant_id,
			"shopify_sku": sku,
			"shopify_inventory_item_id": None,
			"inventory_sync_pending": cint(item.is_stock_item),
		}
		if row:
			frappe.db.set_value("Item Shopify Store", row.name, values)
		else:
			row = item.append("shopify_stores", {"shopify_store": store.name, "enabled": 1, **values})
			row.db_insert()
		linked.add(item_code)
		if item.is_stock_item:
			stock_items.add(item_code)

	queue_auto_link_inventory(store.name, sorted(stock_items))
	return sorted(linked)


def queue_auto_link_inventory(store_name: str, item_codes: list[str]) -> None:
	"""Schedule a targeted update after the mapping and order changes commit."""
	item_codes = sorted(set(item_codes))
	if not item_codes:
		return
	store = frappe.db.get_value(
		"Shopify Store", store_name, ["enabled", "enable_inventory_sync"], as_dict=True
	)
	if not store or not store.enabled or not store.enable_inventory_sync:
		return
	frappe.db.after_commit.add(partial(_enqueue_inventory_update, store_name, item_codes))


def _enqueue_inventory_update(store_name: str, item_codes: list[str]) -> None:
	try:
		store = frappe.db.get_value(
			"Shopify Store", store_name, ["enabled", "enable_inventory_sync"], as_dict=True
		)
		if not store or not store.enabled or not store.enable_inventory_sync:
			return
		frappe.enqueue(
			"nexwave_shopify_connector.nexwave_shopify.inventory.sync_items_inventory",
			queue="short",
			timeout=300,
			store_name=store_name,
			item_codes=item_codes,
			source="order_auto_link",
		)
	except Exception:
		# The link has already committed. Its pending flag drives scheduled
		# retry even when Redis is unavailable during this callback.
		get_logger().exception("Could not queue initial inventory update for Shopify Store %s", store_name)


def _shopify_id(value) -> str | None:
	value = cstr(value).strip()
	return value if value.isascii() and value.isdigit() and int(value) > 0 else None


def _log_warning(store_name: str, item_code: str, variant_id: str, reason: str) -> None:
	# Keep this log in the mapping transaction. create_shopify_log commits
	# internally and would release the store ownership lock mid-operation.
	frappe.get_doc(
		{
			"doctype": "NexWave Shopify Log",
			"status": "Warning",
			"method": "auto_link_order_items",
			"shopify_store": store_name,
			"reference_doctype": "Item",
			"reference_name": item_code,
			"message": f"Skipped automatic link for variant {variant_id}. {reason}",
		}
	).insert(ignore_permissions=True)
