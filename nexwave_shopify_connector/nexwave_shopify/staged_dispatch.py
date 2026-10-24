# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Optional NZ staged dispatch, using the warehouse app's source mapper."""

from collections import defaultdict
from decimal import Decimal

import frappe
from frappe import _
from frappe.model.mapper import map_child_doc
from frappe.utils import cstr, flt

NZ_APP = "erpnext_new_zealand_customizations"


def _has_staging():
	"""Older NZ schemas without outbound staging retain ordinary fulfillment."""
	return NZ_APP in frappe.get_installed_apps() and frappe.get_meta("Warehouse").has_field(
		"outbound_bin_mode"
	)


def _pick_names(so_name, warehouses, *, for_update=False):
	"""Read candidate source picks in deterministic order, using current locks."""
	if not warehouses:
		return []
	return [
		row.name
		for row in frappe.db.sql(
			"SELECT DISTINCT pl.name, pl.creation FROM `tabPick List` pl "
			"JOIN `tabPick List Item` row ON row.parent=pl.name "
			"WHERE pl.docstatus=1 AND pl.purpose='Delivery' "
			"AND row.sales_order=%s AND row.warehouse IN %s "
			"ORDER BY pl.creation, pl.name" + (" FOR UPDATE" if for_update else ""),
			(so_name, tuple(warehouses)),
			as_dict=True,
		)
	]


def lock_dispatch_sources(so, warehouses):
	"""Take NZ's union stock locks before the order lock, matching WMS order."""
	if not _has_staging():
		return
	from erpnext_new_zealand_customizations.nexwave_bin_management.services.outbound_staging import (
		lock_staging_pick_list,
	)
	from erpnext_new_zealand_customizations.nexwave_bin_management.utils import is_bin_tracking_applicable

	locations = [
		{"item_code": row.item_code, "warehouse": warehouse}
		for row in so.items
		for warehouse in warehouses
		if is_bin_tracking_applicable(row.item_code, warehouse)
	]
	if not locations:
		return
	picks = _pick_names(so.name, warehouses)
	if picks:
		# Include the full source scope before any individual mapper takes locks.
		locations.extend(
			frappe.get_all(
				"Pick List Item", filters={"parent": ["in", picks]}, fields=["item_code", "warehouse"]
			)
		)
	lock_staging_pick_list(frappe.get_doc({"doctype": "Pick List", "locations": locations}))
	# A new mixed-order pick may have committed while configuration locks waited.
	# Never extend configuration scope after taking Pick List or Sales Order locks.
	current_picks = _pick_names(so.name, warehouses, for_update=True)
	if current_picks:
		current_rows = frappe.db.sql(
			"SELECT item_code, warehouse FROM `tabPick List Item` WHERE parent IN %s FOR UPDATE",
			(tuple(current_picks),),
			as_dict=True,
		)
		locked_items = {row["item_code"] for row in locations}
		locked_warehouses = {row["warehouse"] for row in locations}
		if any(
			row.item_code not in locked_items or row.warehouse not in locked_warehouses
			for row in current_rows
		):
			frappe.throw(
				_("The staged Pick List sources changed while waiting. Retry this fulfillment event.")
			)


def _demands(so, lines, store):
	"""Resolve commercial row identity before applying remaining sales quantities."""
	by_id = {}
	by_item = defaultdict(list)
	for row in so.items:
		by_item[row.item_code].append(row)
		line_id = cstr(row.get("shopify_line_item_id")).strip()
		if line_id:
			if line_id in by_id:
				frappe.throw(_("Duplicate Shopify line item ID {0} on this Sales Order.").format(line_id))
			by_id[line_id] = row
	requested = defaultdict(Decimal)
	for line in lines:
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
	for row in so.items:
		qty = requested.get(row.name, Decimal(0))
		if store.auto_fulfill_remaining_qty:
			qty = min(qty, max(Decimal(str(row.qty)) - Decimal(str(row.delivered_qty)), Decimal(0)))
		if qty > 0:
			demands[row.name] = (row, qty)
	return demands


def _assert_unassigned(so, source_rows, pick_names):
	"""Do not consume stock owned by a draft delivery or an active scanner task."""
	drafts = frappe.db.sql(
		"SELECT DISTINCT dn.name FROM `tabDelivery Note` dn "
		"JOIN `tabDelivery Note Item` row ON row.parent=dn.name "
		"WHERE dn.docstatus=0 AND row.against_sales_order=%s AND row.so_detail IN %s FOR UPDATE",
		(so.name, tuple(source_rows)),
		as_dict=True,
	)
	if drafts:
		frappe.throw(
			_(
				"A draft Delivery Note already owns these staged order rows. Complete or remove that draft first."
			)
		)
	# A snapshot read can miss a task committed while these stock locks waited.
	if pick_names and frappe.db.sql(
		"SELECT name FROM `tabWarehouse Task` WHERE task_type='Dispatch' "
		"AND source_doctype='Pick List' AND source_name IN %s "
		"AND status IN ('Ready', 'In Progress', 'Blocked') FOR UPDATE",
		(tuple(pick_names),),
	):
		frappe.throw(
			_(
				"An active warehouse dispatch task owns this staged Pick List. Complete or withdraw that task first."
			)
		)


def get_staged_delivery_items(so, lines, warehouse, store):
	"""Return exact staged/native item rows, or None for the ordinary direct path.

	This only consumes already posted staging. It never creates a physical pick,
	staging transfer, or outgoing bundle outside normal Delivery Note validation.
	"""
	if not _has_staging():
		return None
	from erpnext_new_zealand_customizations.nexwave_bin_management.overrides.pick_list import (
		create_dn_for_pick_lists,
	)
	from erpnext_new_zealand_customizations.nexwave_bin_management.services.outbound_allocation import (
		get_remaining_pick_entries,
	)
	from erpnext_new_zealand_customizations.nexwave_bin_management.services.outbound_staging import (
		get_pick_list_staging,
	)
	from erpnext_new_zealand_customizations.nexwave_bin_management.utils import is_bin_tracking_applicable

	managed = {row.name for row in so.items if is_bin_tracking_applicable(row.item_code, warehouse)}
	if not managed:
		return None
	picks = _pick_names(so.name, [warehouse], for_update=True)
	states = {name: get_pick_list_staging(name, for_update=True, locks_held=True) for name in picks}
	if frappe.get_cached_value(
		"Warehouse", warehouse, "outbound_bin_mode"
	) != "Stage Before Dispatch" and not any(
		state.status in ("Staged", "Staging in progress") for state in states.values()
	):
		return None
	demands = _demands(so, lines, store)
	staged = {name: values for name, values in demands.items() if name in managed}
	if not staged:
		return [] if not demands else None
	if picks:
		relevant = {
			row.parent
			for row in frappe.db.sql(
				"SELECT DISTINCT parent FROM `tabPick List Item` WHERE parent IN %s "
				"AND sales_order=%s AND sales_order_item IN %s AND warehouse=%s FOR UPDATE",
				(tuple(picks), so.name, tuple(staged), warehouse),
				as_dict=True,
			)
		}
		picks = [name for name in picks if name in relevant]
	for row, _qty in staged.values():
		if frappe.get_cached_value("Item", row.item_code, "has_serial_no"):
			frappe.throw(_("Serial-tracked items are not supported by NZ staged dispatch in this release."))
	_assert_unassigned(so, staged, picks)
	remaining = {name: qty * Decimal(str(row.conversion_factor or 1)) for name, (row, qty) in staged.items()}
	items = []
	capacity = {}
	for name in picks:
		if states[name].status != "Staged":
			continue
		mapped = create_dn_for_pick_lists(name, kwargs={"sales_order": so.name})
		for source in mapped.items:
			# Native filtered_children/condition does not filter map_pl_locations.
			if source.against_sales_order != so.name or source.warehouse != warehouse:
				continue
			needed = remaining.get(source.so_detail, Decimal(0))
			factor = Decimal(str(source.conversion_factor or 1))
			if source.pick_list_item not in capacity:
				location = frappe.get_doc("Pick List Item", source.pick_list_item, for_update=True)
				capacity[source.pick_list_item] = {
					entry.batch_no or "": Decimal(str(entry.qty))
					for entry in get_remaining_pick_entries(location, for_update=True)
				}
			available = capacity[source.pick_list_item].get(source.batch_no or "", Decimal(0))
			quantity = min(needed, available)
			if quantity <= 0:
				continue
			sales_qty = flt(float(quantity / factor), source.precision("qty"))
			if Decimal(str(sales_qty)) * factor != quantity:
				frappe.throw(
					_(
						"A staged allocation cannot be represented exactly in the Sales Order UOM at Delivery Note quantity precision. Use pick quantities that fit the sales UOM before retrying."
					)
				)
			values = source.as_dict()
			values["qty"] = sales_qty
			values["stock_qty"] = float(quantity)
			items.append(_new_item_values(values))
			remaining[source.so_detail] -= quantity
			capacity[source.pick_list_item][source.batch_no or ""] -= quantity
		if all(qty <= 0 for qty in remaining.values()):
			break
	if any(qty > 0 for qty in remaining.values()):
		frappe.throw(
			_(
				"There is insufficient staged stock for this fulfillment. Stage the required Pick List quantities first."
			)
		)
	from erpnext.stock.doctype.pick_list.pick_list import update_delivery_note_item

	plain = frappe.new_doc("Delivery Note")
	plain.company = so.company
	plain.project = so.project
	for name, (row, qty) in demands.items():
		if name in staged:
			continue
		mapped = map_child_doc(
			row,
			plain,
			{
				"doctype": "Delivery Note Item",
				"field_map": {"name": "so_detail", "parent": "against_sales_order"},
			},
		)
		mapped.qty = float(qty)
		mapped.warehouse = warehouse
		update_delivery_note_item(row, mapped, plain)
		mapped.cost_center = store.cost_center or mapped.cost_center
		items.append(_new_item_values(mapped.as_dict()))
	return items


def _new_item_values(values):
	"""Preserve physical/commercial fields while assigning fresh DN row identity."""
	return {
		key: value
		for key, value in values.items()
		if key
		not in {"name", "parent", "parenttype", "parentfield", "idx", "docstatus", "__islocal", "__unsaved"}
	}
