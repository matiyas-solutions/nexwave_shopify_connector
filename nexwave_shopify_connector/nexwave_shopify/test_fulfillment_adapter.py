# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Real-document verification of the generic fulfillment provider contract."""

import frappe
from frappe.tests.utils import FrappeTestCase, patch_hooks

from nexwave_shopify_connector.nexwave_shopify._fulfillment_adapter_test_fixtures import (
	RecordingAdapter,
	make_identity_order,
	make_shipping_service,
)
from nexwave_shopify_connector.nexwave_shopify.fulfillment import create_delivery_notes_from_fulfillments
from nexwave_shopify_connector.nexwave_shopify.fulfillment_adapter import resolve_fulfillment_demands
from nexwave_shopify_connector.nexwave_shopify.test_fulfillment import (
	create_stock_entry,
	create_test_sales_order,
	get_or_create_test_shopify_store,
)

test_dependencies = ["Company", "Customer", "Warehouse", "Item"]
PROVIDERS = "nexwave_shopify_connector.nexwave_shopify._fulfillment_adapter_test_fixtures."


class TestFulfillmentAdapter(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint("fulfillment_adapter_test")
		RecordingAdapter.instances.clear()
		self.store = get_or_create_test_shopify_store(auto_fulfill_remaining_qty=1)
		create_stock_entry("_Test Item", 20, self.store.warehouse)
		self.order = create_test_sales_order(
			"_Test Customer", "_Test Item", shopify_order_id=frappe.generate_hash(length=12)
		)

	def tearDown(self):
		frappe.db.rollback(save_point="fulfillment_adapter_test")
		RecordingAdapter.instances.clear()
		super().tearDown()

	def payload(self, *quantities):
		return {
			"id": self.order.shopify_order_id,
			"fulfillments": [
				{
					"id": frappe.generate_hash(length=12),
					"status": "success",
					"line_items": [{"id": 101, "sku": "_Test Item", "quantity": qty}],
				}
				for qty in quantities
			],
		}

	def dispatch(self, provider, *quantities):
		hooks = [] if provider is None else [PROVIDERS + provider]
		with patch_hooks({"shopify_fulfillment_adapter": hooks}):
			return create_delivery_notes_from_fulfillments(self.payload(*quantities), self.store)

	def assert_no_delivery(self):
		self.assertFalse(frappe.db.exists("Delivery Note", {"shopify_order_id": self.order.shopify_order_id}))

	def test_no_provider_uses_standard_erpnext(self):
		result = self.dispatch(None, 2)
		self.assertEqual(len(result["created"]), 1)
		self.assertEqual(self.order.reload().items[0].delivered_qty, 2)
		self.assertFalse(RecordingAdapter.instances)

	def test_one_instance_prepares_before_lock_and_gets_fresh_order_for_each_fulfillment(self):
		result = self.dispatch("RecordingAdapter", 2, 3)
		self.assertEqual(len(result["created"]), 2)
		self.assertEqual(len(RecordingAdapter.instances), 1)
		self.assertEqual(
			RecordingAdapter.instances[0].calls,
			[
				("prepare", False, {self.store.warehouse}),
				("items", True, 0),
				("items", True, 2),
			],
		)

	def test_provider_supplies_exact_commercial_rows(self):
		frappe.db.set_value("Sales Order Item", self.order.items[0].name, "shopify_line_item_id", "101")
		result = self.dispatch("CustomItemsAdapter", 2)
		dn = frappe.get_doc("Delivery Note", result["created"][0])
		self.assertEqual(dn.items[0].description, "Selected by the fulfillment adapter")
		self.assertEqual(dn.items[0].so_detail, self.order.items[0].name)
		self.assertEqual(dn.items[0].qty, 2)

	def test_empty_provider_items_never_fall_back_to_product_mapping(self):
		result = self.dispatch("EmptyItemsAdapter", 2)
		self.assertEqual(result["created"], [])
		self.assertEqual(result["failed"], 1)
		self.assert_no_delivery()

	def test_invalid_provider_result_fails_closed(self):
		with self.assertRaisesRegex(frappe.ValidationError, "list of item dictionaries"):
			self.dispatch("InvalidItemsAdapter", 2)
		self.assert_no_delivery()

	def test_invalid_provider_item_fails_closed(self):
		with self.assertRaisesRegex(frappe.ValidationError, "list of item dictionaries"):
			self.dispatch("InvalidRowAdapter", 2)
		self.assert_no_delivery()

	def test_constructor_failure_rolls_back_provider_writes(self):
		with self.assertRaisesRegex(frappe.ValidationError, "constructor failure"):
			self.dispatch("ConstructorFailureAdapter", 2)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "Synthetic adapter constructor failure"}))
		self.assert_no_delivery()

	def test_empty_product_result_still_applies_configured_shipping(self):
		service = make_shipping_service()
		self.store.add_shipping_as_item = 1
		self.store.shipping_item = service.name
		self.store.save()
		self.order = make_identity_order(
			self.store,
			[
				{"qty": 10, "rate": 100},
				{"item_code": service.name, "qty": 1, "rate": 5},
			],
		)
		result = self.dispatch("EmptyItemsAdapter", 2)
		dn = frappe.get_doc("Delivery Note", result["created"][0])
		self.assertEqual([(row.item_code, row.qty) for row in dn.items], [(service.name, 1)])
		self.assertEqual(self.order.reload().items[0].delivered_qty, 0)

	def test_exact_identity_selects_the_right_repeated_item(self):
		order = make_identity_order(
			self.store,
			[
				{"qty": 2, "rate": 100, "shopify_line_item_id": "101"},
				{"qty": 3, "rate": 75, "shopify_line_item_id": "102"},
			],
		)
		demands = resolve_fulfillment_demands(
			order, [{"id": 102, "sku": "old-sku", "quantity": 1}], self.store
		)
		self.assertEqual(list(demands), [order.items[1].name])
		self.assertEqual(next(iter(demands.values()))[0].rate, 75)

	def test_repeated_requests_are_aggregated_before_remaining_quantity_cap(self):
		order = make_identity_order(self.store, [{"qty": 3, "rate": 100, "shopify_line_item_id": "101"}])
		demands = resolve_fulfillment_demands(order, [{"id": 101, "quantity": 2}] * 2, self.store)
		self.assertEqual(demands[order.items[0].name][1], 3)

	def test_ambiguous_legacy_items_require_exact_identity(self):
		order = make_identity_order(self.store, [{"qty": 2, "rate": 100}, {"qty": 3, "rate": 75}])
		with self.assertRaisesRegex(frappe.ValidationError, "identify one"):
			resolve_fulfillment_demands(order, [{"sku": "_Test Item", "quantity": 1}], self.store)

	def test_unknown_identity_cannot_rebind_an_identified_item(self):
		order = make_identity_order(self.store, [{"qty": 2, "rate": 100, "shopify_line_item_id": "101"}])
		with self.assertRaisesRegex(frappe.ValidationError, "identify one"):
			resolve_fulfillment_demands(order, [{"id": 999, "sku": "_Test Item", "quantity": 1}], self.store)

	def test_preparation_failure_rolls_back_provider_writes(self):
		with self.assertRaisesRegex(frappe.ValidationError, "preparation failure"):
			self.dispatch("PreparationFailureAdapter", 2)
		self.assertFalse(frappe.db.exists("ToDo", {"description": "Adapter preparation " + self.order.name}))
		self.assert_no_delivery()

	def test_later_provider_failure_rolls_back_earlier_deliveries(self):
		with self.assertRaisesRegex(frappe.ValidationError, "item failure"):
			self.dispatch("LaterFailureAdapter", 2, 3)
		self.assert_no_delivery()
		self.assertEqual(self.order.reload().items[0].delivered_qty, 0)

	def test_multiple_providers_fail_before_instantiation(self):
		with patch_hooks({"shopify_fulfillment_adapter": [PROVIDERS + "RecordingAdapter"] * 2}):
			with self.assertRaisesRegex(frappe.ValidationError, "Only one"):
				create_delivery_notes_from_fulfillments(self.payload(2), self.store)
		self.assertFalse(RecordingAdapter.instances)
		self.assert_no_delivery()

	def test_incomplete_provider_fails_before_preparation(self):
		with self.assertRaisesRegex(frappe.ValidationError, "must implement"):
			self.dispatch("IncompleteAdapter", 2)
		self.assert_no_delivery()
