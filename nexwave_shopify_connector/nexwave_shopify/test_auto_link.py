# Copyright (c) 2026, HighFlyer and contributors
# For license information, please see license.txt

"""Order-driven linking with real Frappe documents and an isolated queue boundary."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import CallbackManager

from nexwave_shopify_connector.nexwave_shopify._auto_link_test_fixtures import (
	make_item,
	make_mapping,
	make_order,
	make_payload,
	make_store,
)
from nexwave_shopify_connector.nexwave_shopify.auto_link import (
	auto_link_order_items,
	queue_auto_link_inventory,
)

test_dependencies = ["Company", "Customer", "Warehouse", "Item"]


class TestAutoLinkOrderItems(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.savepoint("auto_link_test")
		self.original_after_commit = frappe.db.after_commit
		frappe.db.after_commit = CallbackManager()
		self.addCleanup(self.restore_callbacks)
		self.store = make_store()
		self.item = make_item()
		self.order = make_order(self.store, {"item_code": self.item.name, "shopify_line_item_id": "701"})

	def restore_callbacks(self):
		frappe.db.after_commit.reset()
		frappe.db.after_commit = self.original_after_commit

	def tearDown(self):
		frappe.db.rollback(save_point="auto_link_test")
		super().tearDown()

	def link(self, *lines):
		return auto_link_order_items({"line_items": list(lines)}, self.store, self.order)

	def mappings(self, item=None, store=None):
		return frappe.get_all(
			"Item Shopify Store",
			filters={
				"parent": (item or self.item).name,
				"parenttype": "Item",
				"parentfield": "shopify_stores",
				"shopify_store": (store or self.store).name,
			},
			fields=[
				"name",
				"enabled",
				"shopify_product_id",
				"shopify_variant_id",
				"shopify_sku",
				"inventory_sync_pending",
			],
			order_by="idx",
		)

	def test_setting_off_preserves_existing_manual_workflow(self):
		self.store.auto_link_items_by_sku = 0
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), [])

	def test_disabled_store_does_not_link(self):
		self.store.enabled = 0
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), [])

	def test_matching_order_line_creates_pending_store_mapping(self):
		self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
		rows = self.mappings()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].shopify_product_id, "501")
		self.assertEqual(rows[0].shopify_variant_id, "601")
		self.assertEqual(rows[0].shopify_sku, self.item.item_code)
		self.assertEqual(rows[0].enabled, 1)
		self.assertEqual(rows[0].inventory_sync_pending, 1)

	def test_repeat_import_is_idempotent_and_preserves_pending_state(self):
		self.link(make_payload(self.item))
		before = self.mappings()
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), before)

	def test_existing_completed_mapping_is_not_marked_pending_again(self):
		row = make_mapping(self.item, self.store, inventory_sync_pending=0)
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings()[0].name, row.name)
		self.assertEqual(self.mappings()[0].inventory_sync_pending, 0)

	def test_enabled_blank_stub_is_completed_in_place(self):
		row = make_mapping(self.item, self.store, shopify_product_id=None, shopify_variant_id=None)
		self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
		rows = self.mappings()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].name, row.name)
		self.assertEqual(rows[0].shopify_product_id, "501")
		self.assertEqual(rows[0].shopify_variant_id, "601")
		self.assertEqual(rows[0].inventory_sync_pending, 1)

	def test_completed_stub_discards_stale_inventory_cache(self):
		row = make_mapping(
			self.item,
			self.store,
			shopify_product_id=None,
			shopify_variant_id=None,
			shopify_inventory_item_id="stale-inventory-id",
		)
		self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
		self.assertFalse(frappe.db.get_value("Item Shopify Store", row.name, "shopify_inventory_item_id"))
		self.assertEqual(self.mappings()[0].inventory_sync_pending, 1)

	def test_disabled_exact_mapping_is_not_reenabled(self):
		make_mapping(self.item, self.store, enabled=0)
		before = self.mappings()
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), before)

	def test_disabled_different_variant_does_not_disable_entire_store(self):
		original = make_mapping(self.item, self.store, enabled=0, shopify_variant_id="602")
		self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
		rows = {row.name: row for row in self.mappings()}
		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[original.name].enabled, 0)
		self.assertEqual(rows[original.name].shopify_variant_id, "602")

	def test_disabled_blank_stub_is_not_completed_or_duplicated(self):
		make_mapping(self.item, self.store, enabled=0, shopify_product_id=None, shopify_variant_id=None)
		before = self.mappings()
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), before)

	def test_disabled_item_does_not_receive_link(self):
		frappe.db.set_value("Item", self.item.name, "disabled", 1)
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), [])

	def test_variant_owned_by_another_item_is_preserved_and_warned(self):
		owner = make_item()
		original = make_mapping(owner, self.store)
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), [])
		self.assertEqual(self.mappings(owner)[0].name, original.name)
		self.assertTrue(
			frappe.db.exists("NexWave Shopify Log", {"shopify_store": self.store.name, "status": "Warning"})
		)

	def test_disabled_mapping_on_another_item_still_owns_variant(self):
		owner = make_item()
		make_mapping(owner, self.store, enabled=0)
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), [])
		self.assertEqual(self.mappings(owner)[0].enabled, 0)

	def test_payload_conflicting_skus_for_one_variant_are_both_skipped(self):
		other_item = make_item()
		self.order = make_order(
			self.store,
			{"item_code": self.item.name, "shopify_line_item_id": "701"},
			{"item_code": other_item.name, "shopify_line_item_id": "702"},
		)
		self.assertEqual(self.link(make_payload(self.item), make_payload(other_item, id="702")), [])
		self.assertEqual(self.mappings(), [])
		self.assertEqual(self.mappings(other_item), [])
		self.assertTrue(
			frappe.db.exists("NexWave Shopify Log", {"shopify_store": self.store.name, "status": "Warning"})
		)

	def test_other_store_variant_mapping_does_not_block_linking(self):
		other_store = make_store()
		other_item = make_item()
		make_mapping(other_item, other_store, enabled=0)
		self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
		self.assertEqual(len(self.mappings()), 1)
		self.assertEqual(len(self.mappings(other_item, other_store)), 1)

	def test_another_variant_of_same_item_gets_its_own_row(self):
		make_mapping(self.item, self.store, shopify_variant_id="602")
		self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
		self.assertEqual({row.shopify_variant_id for row in self.mappings()}, {"601", "602"})

	def test_no_sku_or_shopify_ids_never_creates_mapping(self):
		for overrides in (
			{"sku": None},
			{"sku": ""},
			{"product_id": None},
			{"variant_id": None},
			{"product_id": 0},
			{"variant_id": 0},
			{"product_id": "invalid"},
			{"variant_id": "invalid"},
		):
			with self.subTest(overrides=overrides):
				self.assertEqual(self.link(make_payload(self.item, **overrides)), [])
		self.assertEqual(self.mappings(), [])

	def test_missing_item_is_not_created(self):
		missing_sku = "_Test Missing Auto Link " + frappe.generate_hash(length=10)
		self.assertEqual(self.link(make_payload(self.item, sku=missing_sku)), [])
		self.assertFalse(frappe.db.exists("Item", missing_sku))

	def test_existing_item_absent_from_imported_order_is_not_linked(self):
		other_item = make_item()
		self.assertEqual(self.link(make_payload(other_item)), [])
		self.assertEqual(self.mappings(other_item), [])

	def test_payload_identity_must_match_imported_line(self):
		self.assertEqual(self.link(make_payload(self.item, id="999")), [])
		self.assertEqual(self.mappings(), [])

	def test_identity_cannot_attach_payload_sku_to_different_imported_item(self):
		other_item = make_item()
		self.order = make_order(
			self.store,
			{"item_code": self.item.name, "shopify_line_item_id": "701"},
			{"item_code": other_item.name, "shopify_line_item_id": "702"},
		)
		self.assertEqual(self.link(make_payload(self.item, id="702")), [])
		self.assertEqual(self.mappings(), [])
		self.assertEqual(self.mappings(other_item), [])

	def test_missing_imported_identity_cannot_confirm_payload_line(self):
		frappe.db.set_value("Sales Order Item", self.order.items[0].name, "shopify_line_item_id", None)
		self.order.reload()
		self.assertEqual(self.link(make_payload(self.item)), [])
		self.assertEqual(self.mappings(), [])

	def test_payload_without_identity_can_match_imported_item_code(self):
		self.assertEqual(self.link(make_payload(self.item, id=None)), [self.item.name])

	def test_non_stock_item_links_without_requesting_inventory(self):
		frappe.db.set_value("Item", self.item.name, "is_stock_item", 0)
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
			frappe.db.after_commit.run()
			enqueue.assert_not_called()
		self.assertEqual(self.mappings()[0].inventory_sync_pending, 0)

	def test_inventory_disabled_still_links_and_retains_pending_state(self):
		self.store.enable_inventory_sync = 0
		frappe.db.set_value("Shopify Store", self.store.name, "enable_inventory_sync", 0)
		with patch("frappe.enqueue") as enqueue:
			self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
			frappe.db.after_commit.run()
			enqueue.assert_not_called()
		self.assertEqual(self.mappings()[0].inventory_sync_pending, 1)

	def test_new_link_defers_inventory_job_until_transaction_completes(self):
		item_modified = frappe.db.get_value("Item", self.item.name, "modified")
		with patch("frappe.enqueue") as enqueue:
			self.link(make_payload(self.item))
			enqueue.assert_not_called()
			frappe.db.after_commit.run()
			enqueue.assert_called_once()
			self.assertEqual(enqueue.call_args.kwargs["store_name"], self.store.name)
			self.assertEqual(enqueue.call_args.kwargs["item_codes"], [self.item.name])
			self.assertEqual(enqueue.call_args.kwargs["source"], "order_auto_link")
		self.assertEqual(self.mappings()[0].inventory_sync_pending, 1)
		self.assertEqual(frappe.db.get_value("Item", self.item.name, "modified"), item_modified)

	def test_enqueue_failure_does_not_lose_link_or_pending_retry(self):
		with patch("frappe.enqueue", side_effect=ConnectionError("Synthetic queue outage")):
			self.assertEqual(self.link(make_payload(self.item)), [self.item.name])
			frappe.db.after_commit.run()
		self.assertEqual(len(self.mappings()), 1)
		self.assertEqual(self.mappings()[0].inventory_sync_pending, 1)

	def test_queue_helper_ignores_disabled_inventory_and_empty_item_list(self):
		with patch("frappe.enqueue") as enqueue:
			queue_auto_link_inventory(self.store.name, [])
			frappe.db.set_value("Shopify Store", self.store.name, "enable_inventory_sync", 0)
			queue_auto_link_inventory(self.store.name, [self.item.name])
			frappe.db.after_commit.run()
			enqueue.assert_not_called()

	def test_queue_helper_rechecks_store_before_enqueue(self):
		with patch("frappe.enqueue") as enqueue:
			queue_auto_link_inventory(self.store.name, [self.item.name])
			frappe.db.set_value("Shopify Store", self.store.name, "enabled", 0)
			frappe.db.after_commit.run()
			enqueue.assert_not_called()
