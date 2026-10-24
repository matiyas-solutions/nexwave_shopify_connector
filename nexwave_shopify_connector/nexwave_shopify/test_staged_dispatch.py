# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Optional integration coverage for real NZ picking and staged dispatch."""

from unittest import skipUnless

import frappe
from frappe.tests.utils import FrappeTestCase

from nexwave_shopify_connector.nexwave_shopify._staged_dispatch_test_fixtures import StagedDispatchFixture
from nexwave_shopify_connector.nexwave_shopify.fulfillment import create_delivery_notes_from_fulfillments

NZ_INSTALLED = "erpnext_new_zealand_customizations" in frappe.get_installed_apps()
test_dependencies = ["Company", "Customer", "Warehouse", "Item"]


@skipUnless(NZ_INSTALLED, "Requires the optional NZ customisations app")
class TestStagedDispatch(FrappeTestCase):
	"""Assert both physical stock and source identity through the connector."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if NZ_INSTALLED:
			from erpnext_new_zealand_customizations.nexwave_bin_management.tests.fixtures import (
				setup_bin_test_data,
			)

			setup_bin_test_data(commit=True)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.savepoint = "shopify_staged_test"
		frappe.db.savepoint(self.savepoint)
		self.f = StagedDispatchFixture()

	def tearDown(self):
		frappe.db.rollback(save_point=self.savepoint)
		frappe.clear_document_cache("Warehouse", self.f.warehouse)
		super().tearDown()

	def dispatch(self, order, *lines, payload=None, store=None):
		"""Run the production entry point and reload its submitted documents."""
		result = create_delivery_notes_from_fulfillments(
			payload or self.f.payload(order, *lines), store or self.f.store
		)
		return [frappe.get_doc("Delivery Note", name) for name in result["created"]]

	def line(self, quantity, index=0, **values):
		"""Identify an imported source line independently of its current SKU."""
		return {"id": 101 + index, "sku": self.f.item.name, "quantity": quantity, **values}

	def assert_no_delivery(self, order):
		"""Require failed processing to leave no draft or submitted delivery."""
		self.assertFalse(frappe.db.exists("Delivery Note", {"shopify_order_id": order.shopify_order_id}))

	def test_staged_partial_dispatch_preserves_source_and_physical_stock(self):
		"""Only requested goods leave the staging bin and retain the pick row."""
		order = self.f.order()
		pick = self.f.pick(order, [(0, 10, self.f.rack, None)])
		dn = self.dispatch(order, [self.line(7)])[0]
		self.assertEqual(dn.docstatus, 1)
		self.assertEqual(dn.items[0].so_detail, order.items[0].name)
		self.assertEqual(dn.items[0].against_pick_list, pick.name)
		self.assertEqual(dn.items[0].pick_list_item, pick.locations[0].name)
		self.assertEqual(dn.items[0].warehouse_bin, self.f.stage)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.rack), 0)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 3)

	def test_split_source_rows_retain_their_remainder_across_shipments(self):
		"""A split load consumes each source row once, including later webhooks."""
		order = self.f.order()
		pick = self.f.pick(order, [(0, 4, self.f.rack, None), (0, 6, self.f.other_rack, None)])
		first = self.dispatch(order, [self.line(7)])[0]
		self.assertEqual(
			[(row.pick_list_item, row.qty) for row in first.items],
			[(pick.locations[0].name, 4), (pick.locations[1].name, 3)],
		)
		second = self.dispatch(order, [self.line(3)])[0]
		self.assertEqual(
			[(row.pick_list_item, row.qty) for row in second.items], [(pick.locations[1].name, 3)]
		)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 0)

	def test_multiple_pick_lists_cover_one_fulfillment(self):
		"""One fulfillment may consume staged goods from several pick lists."""
		order = self.f.order()
		first = self.f.pick(order, [(0, 4, self.f.rack, None)])
		second = self.f.pick(order, [(0, 6, self.f.other_rack, None)])
		dn = self.dispatch(order, [self.line(7)])[0]
		self.assertEqual(
			[(row.against_pick_list, row.qty) for row in dn.items], [(first.name, 4), (second.name, 3)]
		)

	def test_exact_line_id_retains_price_for_repeated_sku(self):
		"""Out-of-order fulfillment must not borrow another order row's price."""
		order = self.f.order([{"qty": 2, "rate": 100}, {"qty": 3, "rate": 75}])
		self.f.pick(order, [(0, 2, self.f.rack, None), (1, 3, self.f.other_rack, None)])
		dn = self.dispatch(order, [self.line(1, index=1, sku="old-sku")])[0]
		self.assertEqual(dn.items[0].so_detail, order.items[1].name)
		self.assertEqual(dn.items[0].rate, 75)
		self.assertEqual(dn.total, 75)

	def test_insufficient_staged_stock_leaves_no_delivery(self):
		"""A partial staged plan must fail rather than silently ship less."""
		order = self.f.order()
		self.f.pick(order, [(0, 5, self.f.rack, None)])
		with self.assertRaisesRegex(frappe.ValidationError, "staged"):
			self.dispatch(order, [self.line(7)])
		self.assert_no_delivery(order)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 5)

	def test_batch_partial_dispatch_uses_fresh_outgoing_bundles(self):
		"""Outgoing batches retain source ownership and only requested quantities."""
		self.f.item = self.f.make_item(has_batch_no=1)
		batches = [
			frappe.get_doc(
				{
					"doctype": "Batch",
					"item": self.f.item.name,
					"batch_id": "SHOP-" + frappe.generate_hash(length=10),
				}
			)
			.insert()
			.name
			for _ in range(2)
		]
		order = self.f.order()
		pick = self.f.pick(order, [(0, 4, self.f.rack, batches[0]), (0, 6, self.f.other_rack, batches[1])])
		first = self.dispatch(order, [self.line(7)])[0]
		self.assertEqual([row.qty for row in first.items], [4, 3])
		outgoing = []
		for row in first.items:
			bundle = frappe.get_doc("Serial and Batch Bundle", row.serial_and_batch_bundle)
			self.assertNotIn(bundle.name, [source.serial_and_batch_bundle for source in pick.locations])
			outgoing.extend((entry.batch_no, abs(entry.qty)) for entry in bundle.entries)
		self.assertEqual(outgoing, [(batches[0], 4), (batches[1], 3)])
		second = self.dispatch(order, [self.line(3)])[0]
		bundle = frappe.get_doc("Serial and Batch Bundle", second.items[0].serial_and_batch_bundle)
		self.assertEqual([(entry.batch_no, abs(entry.qty)) for entry in bundle.entries], [(batches[1], 3)])

	def test_stock_uom_conversion_is_applied_once(self):
		"""Sales quantities and staged stock units use the source conversion factor."""
		unit = "_Test Shopify Box"
		if not frappe.db.exists("UOM", unit):
			frappe.get_doc({"doctype": "UOM", "uom_name": unit}).insert()
		self.f.item.append("uoms", {"uom": unit, "conversion_factor": 5})
		self.f.item.save()
		order = self.f.order([{"qty": 2, "rate": 50, "uom": unit, "conversion_factor": 5}])
		self.f.pick(order, [(0, 10, self.f.rack, None)])
		dn = self.dispatch(order, [self.line(1)])[0]
		self.assertEqual(dn.items[0].qty, 1)
		self.assertEqual(dn.items[0].stock_qty, 5)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 5)

	def test_mixed_service_and_staged_stock_keep_order_links(self):
		"""Service rows need no pick but retain source pricing in the same DN."""
		service = self.f.make_item(is_stock_item=0, use_bins=0)
		order = self.f.order([{"qty": 2, "rate": 10}, {"item_code": service.name, "qty": 1, "rate": 4}])
		self.f.pick(order, [(0, 2, self.f.rack, None)])
		dn = self.dispatch(order, [self.line(2), self.line(1, index=1, sku=service.name)])[0]
		self.assertEqual(dn.total, 24)
		self.assertEqual({row.so_detail for row in dn.items}, {row.name for row in order.items})
		self.assertFalse(next(row for row in dn.items if row.item_code == service.name).pick_list_item)

	def test_unrepresentable_source_quantity_fails_with_uom_guidance(self):
		"""Core DN precision must not silently lose fractional stock allocations."""
		unit = "_Test Shopify Triple"
		if not frappe.db.exists("UOM", unit):
			frappe.get_doc({"doctype": "UOM", "uom_name": unit}).insert()
		self.f.item.append("uoms", {"uom": unit, "conversion_factor": 3})
		self.f.item.save()
		order = self.f.order([{"qty": 1, "rate": 30, "uom": unit, "conversion_factor": 3}])
		self.f.pick(order, [(0, 1, self.f.rack, None)])
		self.f.pick(order, [(0, 1, self.f.other_rack, None)])
		self.f.pick(order, [(0, 1, self.f.rack, None)])
		with self.assertRaisesRegex(frappe.ValidationError, "UOM.*precision"):
			self.dispatch(order, [self.line(1)])
		self.assert_no_delivery(order)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 3)

	def test_representable_source_quantities_preserve_factor_three(self):
		"""Factor three works when each physical source fits the sales UOM."""
		unit = "_Test Shopify Triple"
		if not frappe.db.exists("UOM", unit):
			frappe.get_doc({"doctype": "UOM", "uom_name": unit}).insert()
		self.f.item.append("uoms", {"uom": unit, "conversion_factor": 3})
		self.f.item.save()
		order = self.f.order([{"qty": 3, "rate": 30, "uom": unit, "conversion_factor": 3}])
		self.f.pick(order, [(0, 3, self.f.rack, None), (0, 6, self.f.other_rack, None)])
		dn = self.dispatch(order, [self.line(3)])[0]
		self.assertEqual([row.stock_qty for row in dn.items], [3, 6])
		self.assertEqual(dn.total, 90)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 0)

	def test_repeated_payload_lines_and_multiple_fulfillments_share_capacity(self):
		"""Each repeated request consumes only its own staged remainder."""
		order = self.f.order()
		self.f.pick(order, [(0, 10, self.f.rack, None)])
		dns = self.dispatch(order, [self.line(2), self.line(3)], [self.line(5)])
		self.assertEqual([sum(row.qty for row in dn.items) for dn in dns], [5, 5])
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 0)

	def test_replaying_fulfillment_moves_stock_only_once(self):
		"""Sequential retries and duplicate events share one submitted DN."""
		order = self.f.order()
		self.f.pick(order, [(0, 10, self.f.rack, None)])
		payload = self.f.payload(order, [self.line(4)])
		payload["fulfillments"].append(payload["fulfillments"][0].copy())
		self.assertEqual(len(self.dispatch(order, payload=payload)), 1)
		self.assertEqual(self.dispatch(order, payload=payload), [])
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 6)

	def test_unstaged_picks_cannot_fall_back_to_direct_dispatch(self):
		"""A physical pick is insufficient until its staging transfer is posted."""
		order = self.f.order()
		self.f.pick(order, [(0, 10, self.f.rack, None)], stage=False)
		with self.assertRaisesRegex(frappe.ValidationError, "staged"):
			self.dispatch(order, [self.line(2)])
		self.assert_no_delivery(order)

	def test_another_orders_staged_stock_cannot_cover_a_shortage(self):
		"""Same item and physical bin do not confer another order's entitlement."""
		foreign = self.f.order()
		self.f.pick(foreign, [(0, 10, self.f.rack, None)])
		order = self.f.order()
		with self.assertRaisesRegex(frappe.ValidationError, "staged"):
			self.dispatch(order, [self.line(2)])
		self.assert_no_delivery(order)

	def test_failure_rolls_back_earlier_fulfillments_in_the_operation(self):
		"""A later shortage rolls back all deliveries produced by this call."""
		order = self.f.order()
		self.f.pick(order, [(0, 5, self.f.rack, None)])
		with self.assertRaisesRegex(frappe.ValidationError, "staged"):
			self.dispatch(order, [self.line(3)], [self.line(4)])
		self.assert_no_delivery(order)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 5)

	def test_existing_draft_pick_delivery_is_not_adopted(self):
		"""A draft owned by another workflow blocks automatic consumption."""
		from erpnext_new_zealand_customizations.nexwave_bin_management.overrides.pick_list import (
			create_dn_for_pick_lists,
		)

		order = self.f.order()
		pick = self.f.pick(order, [(0, 10, self.f.rack, None)])
		draft = create_dn_for_pick_lists(pick.name).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "draft|Draft"):
			self.dispatch(order, [self.line(2)])
		self.assertEqual(frappe.db.get_value("Delivery Note", draft.name, "docstatus"), 0)
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 10)

	def test_serial_staging_remains_unsupported(self):
		"""The connector must not bypass NZ's serial staging restriction."""
		self.f.item = self.f.make_item(has_serial_no=1)
		order = self.f.order([{"qty": 1, "rate": 10}])
		with self.assertRaisesRegex(frappe.ValidationError, "[Ss]erial|staged"):
			self.dispatch(order, [self.line(1)])
		self.assert_no_delivery(order)

	def test_ready_scanner_dispatch_task_blocks_automatic_delivery(self):
		"""Released warehouse work keeps ownership before scanning begins."""
		from erpnext_new_zealand_customizations.nexwave_bin_management.services import wms_dispatch

		order = self.f.order()
		pick = self.f.pick(order, [(0, 10, self.f.rack, None)])
		task = wms_dispatch.schedule_dispatch(
			"Pick List", pick.name, self.f.warehouse, frappe.generate_hash(length=24)
		)
		with self.assertRaisesRegex(frappe.ValidationError, "dispatch task"):
			self.dispatch(order, [self.line(2)])
		self.assert_no_delivery(order)
		self.assertEqual(frappe.db.get_value("Warehouse Task", task["task_id"], "status"), "Ready")
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 10)

	def test_store_scopes_order_lookup_and_fulfillment_deduplication(self):
		"""Two stores may use the same remote order and fulfillment IDs."""
		other_store = self.f.make_store()
		order = self.f.order()
		other = self.f.order(store=other_store, order_id=order.shopify_order_id)
		self.f.pick(order, [(0, 10, self.f.rack, None)])
		self.f.pick(other, [(0, 10, self.f.other_rack, None)])
		payload = self.f.payload(order, [self.line(2)])
		first = self.dispatch(order, payload=payload)[0]
		second = self.dispatch(other, payload=payload, store=other_store)[0]
		self.assertEqual(first.items[0].against_sales_order, order.name)
		self.assertEqual(second.items[0].against_sales_order, other.name)
		self.assertEqual(first.shopify_fulfillment_id, second.shopify_fulfillment_id)
		self.assertNotEqual(first.shopify_store, second.shopify_store)

	def test_unrelated_scanner_task_does_not_block_another_source_pick(self):
		"""A task keeps its whole pick, without taking other picks for the order."""
		from erpnext_new_zealand_customizations.nexwave_bin_management.services import wms_dispatch

		other_item = self.f.make_item()
		order = self.f.order([{"qty": 2, "rate": 10}, {"item_code": other_item.name, "qty": 3, "rate": 20}])
		first = self.f.pick(order, [(0, 2, self.f.rack, None)])
		second = self.f.pick(order, [(1, 3, self.f.other_rack, None)])
		task = wms_dispatch.schedule_dispatch(
			"Pick List", first.name, self.f.warehouse, frappe.generate_hash(length=24)
		)
		dn = self.dispatch(order, [self.line(1, index=1, sku=other_item.name)])[0]
		self.assertEqual(dn.items[0].against_pick_list, second.name)
		self.assertEqual(frappe.db.get_value("Warehouse Task", task["task_id"], "status"), "Ready")
		self.assertEqual(self.f.physical(self.f.item.name, self.f.stage), 2)

	def test_legacy_unique_item_can_use_sku_without_line_identity(self):
		"""Orders imported before the field was added still allow unique matches."""
		order = self.f.order([{"qty": 10, "rate": 10, "shopify_line_item_id": None}])
		self.f.pick(order, [(0, 10, self.f.rack, None)])
		dn = self.dispatch(order, [self.line(2)])[0]
		self.assertEqual(dn.items[0].so_detail, order.items[0].name)

	def test_legacy_duplicate_items_require_manual_line_identity(self):
		"""Ambiguous old imports must not choose another row's price or picks."""
		order = self.f.order(
			[
				{"qty": 2, "rate": 100, "shopify_line_item_id": None},
				{"qty": 3, "rate": 75, "shopify_line_item_id": None},
			]
		)
		self.f.pick(order, [(0, 2, self.f.rack, None), (1, 3, self.f.other_rack, None)])
		with self.assertRaisesRegex(frappe.ValidationError, "identify one"):
			self.dispatch(order, [self.line(1)])
		self.assert_no_delivery(order)

	def test_unknown_line_id_does_not_rebind_an_identified_order_row(self):
		"""A matching SKU cannot override a mismatched imported line ID."""
		order = self.f.order()
		self.f.pick(order, [(0, 10, self.f.rack, None)])
		with self.assertRaisesRegex(frappe.ValidationError, "identify one"):
			self.dispatch(order, [self.line(1, id=999)])
		self.assert_no_delivery(order)

	def test_direct_warehouse_keeps_ordinary_fulfillment(self):
		"""NZ installed with direct dispatch does not require a staged Pick List."""
		from erpnext_new_zealand_customizations.nexwave_bin_management.tests.fixtures import (
			make_stock_entry_with_bin,
		)

		frappe.db.set_value("Warehouse", self.f.warehouse, "outbound_bin_mode", "Direct Dispatch")
		frappe.clear_document_cache("Warehouse", self.f.warehouse)
		order = self.f.order()
		make_stock_entry_with_bin(
			item_code=self.f.item.name,
			t_warehouse=self.f.warehouse,
			to_warehouse_bin=self.f.rack,
			qty=10,
		)
		dn = self.dispatch(order, [self.line(2)])[0]
		self.assertEqual(dn.docstatus, 1)
		self.assertFalse(dn.items[0].against_pick_list)
		self.assertEqual(order.reload().items[0].delivered_qty, 2)

	def test_staged_ownership_survives_a_warehouse_mode_change(self):
		"""Changing mode does not permit a second physical source for staged goods."""
		order = self.f.order()
		pick = self.f.pick(order, [(0, 10, self.f.rack, None)])
		frappe.db.set_value("Warehouse", self.f.warehouse, "outbound_bin_mode", "Direct Dispatch")
		frappe.clear_document_cache("Warehouse", self.f.warehouse)
		dn = self.dispatch(order, [self.line(2)])[0]
		self.assertEqual(dn.items[0].against_pick_list, pick.name)
		self.assertEqual(dn.items[0].warehouse_bin, self.f.stage)

	def test_shipping_service_is_added_once_with_staged_stock(self):
		"""Shipping included in the payload is not appended a second time."""
		service = self.f.make_item(is_stock_item=0, use_bins=0)
		self.f.store.add_shipping_as_item = 1
		self.f.store.shipping_item = service.name
		self.f.store.save()
		order = self.f.order([{"qty": 2, "rate": 10}, {"item_code": service.name, "qty": 1, "rate": 4}])
		self.f.pick(order, [(0, 2, self.f.rack, None)])
		dn = self.dispatch(order, [self.line(2), self.line(1, index=1, sku=service.name)])[0]
		self.assertEqual(dn.total, 24)
		self.assertEqual(sum(row.qty for row in dn.items if row.item_code == service.name), 1)
