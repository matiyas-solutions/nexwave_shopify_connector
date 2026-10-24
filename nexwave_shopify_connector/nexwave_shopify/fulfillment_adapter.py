# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Optional fulfillment extension contract and commercial line resolution."""

from collections import defaultdict
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cstr, flt

FULFILLMENT_ADAPTER_API_VERSION = 1


def get_fulfillment_adapter():
	"""Load one optional provider without coupling fulfillment to another app."""
	providers = frappe.get_hooks("shopify_fulfillment_adapter") or []
	if len(providers) > 1:
		frappe.throw(_("Only one Shopify fulfillment adapter may be configured."))
	if not providers:
		return None
	adapter = frappe.get_attr(providers[0])()
	if not all(
		callable(getattr(adapter, method, None)) for method in ("prepare_sources", "get_delivery_items")
	):
		frappe.throw(
			_("The Shopify fulfillment adapter must implement prepare_sources and get_delivery_items.")
		)
	return adapter


def resolve_fulfillment_demands(sales_order, line_items, store):
	"""Resolve exact order rows and aggregate requested sales quantities.

	Legacy imports may fall back to one unambiguous item without a stored line ID.
	The order must be refreshed under the operation's lock before resolving demand.
	"""
	by_id = {}
	by_item = defaultdict(list)
	for row in sales_order.items:
		by_item[row.item_code].append(row)
		line_id = cstr(row.get("shopify_line_item_id")).strip()
		if line_id:
			if line_id in by_id:
				frappe.throw(_("Duplicate Shopify line item ID {0} on this Sales Order.").format(line_id))
			by_id[line_id] = row
	requested = defaultdict(Decimal)
	for line in line_items:
		qty = Decimal(str(flt(line.get("quantity", 1))))
		if qty <= 0:
			continue
		line_id = cstr(line.get("id")).strip()
		row = by_id.get(line_id)
		if row is None:
			sku = cstr(line.get("sku") or line.get("variant_id") or line.get("product_id")).strip()
			matches = [
				row for row in by_item.get(sku, []) if not cstr(row.get("shopify_line_item_id")).strip()
			]
			if len(matches) != 1:
				frappe.throw(
					_(
						"Shopify fulfillment line {0} does not identify one Sales Order row. Check its line ID and item."
					).format(line_id or sku)
				)
			row = matches[0]
		requested[row.name] += qty
	demands = {}
	for row in sales_order.items:
		qty = requested.get(row.name, Decimal(0))
		if store.auto_fulfill_remaining_qty:
			qty = min(qty, max(Decimal(str(row.qty)) - Decimal(str(row.delivered_qty)), Decimal(0)))
		if qty > 0:
			demands[row.name] = (row, qty)
	return demands
