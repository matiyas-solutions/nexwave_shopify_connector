# Developer Guide

Technical reference for developers working on or integrating with the NexWave Shopify Connector.

## DocTypes

| DocType | Description |
|---------|-------------|
| **Shopify Store** | Main configuration - auth, company, tax accounts, write-off account, sync settings |
| Shopify Store Warehouse Mapping | Maps Shopify locations to ERPNext warehouses |
| Shopify Store Item Field | Field mapping (standard fields and metafields) |
| Shopify Store Collection Mapping | ERPNext values to Shopify collections |
| Shopify Store Item Filter | Auto-eligibility rules |
| Shopify Store Tax Account | Tax mapping (Shopify tax to ERPNext account + item tax templates) |
| Shopify Store Payment Method Mapping | Maps Shopify payment gateways to ERPNext Mode of Payment |
| Shopify Store Webhook | Registered webhook tracking |
| Item Shopify Store | Per-item, per-store mapping (child of Item) |

## Custom Fields Added

| DocType | Fields |
|---------|--------|
| **Item** | `shopify_stores` (table) |
| **Customer** | `shopify_customer_id` |
| **Sales Order** | `shopify_store`, `shopify_order_id`, `shopify_order_number`, `shopify_financial_status`, `shopify_fulfillment_status` |
| **Sales Order Item** | `shopify_item_discount`, `shopify_line_item_id` |
| **Delivery Note** | `shopify_store`, `shopify_order_id`, `shopify_order_number` |
| **Sales Invoice** | `shopify_store`, `shopify_order_id`, `shopify_order_number` |

## Permissions

| Role | Access |
|------|--------|
| **Sales Manager** | Full access (create, edit, delete, import/export) |
| **Sales User** | Read-only + reports |
| **Accounts Manager** | Read-only + reports |
| **Accounts User** | Read-only + reports |
| **System Manager** | Full administrative access |

## Architecture

```
nexwave_shopify_connector/
├── nexwave_shopify/
│   ├── connection.py      # @shopify_session decorator, webhook endpoint
│   ├── oauth.py           # OAuth authorize & callback endpoints
│   ├── order.py           # Order sync logic (webhooks & manual sync)
│   ├── fulfillment.py     # Fulfillment webhook -> Delivery Note creation
│   ├── product.py         # Product/item sync to Shopify
│   ├── inventory.py       # Inventory sync to Shopify
│   ├── utils.py           # Logging, eligibility helpers
│   ├── tax/               # Tax calculation module
│   │   ├── __init__.py    # Public exports
│   │   ├── builder.py     # TaxBuilder: orchestrates tax row creation
│   │   ├── detector.py    # TaxDetector: identifies zero-rated items
│   │   ├── shipping.py    # ShippingTaxHandler: shipping charges & GST
│   │   └── rounding.py    # Rounding adjustment for total matching
│   └── doctype/
│       ├── shopify_store/
│       ├── shopify_store_warehouse_mapping/
│       ├── shopify_store_item_field/
│       ├── shopify_store_collection_mapping/
│       ├── shopify_store_item_filter/
│       ├── shopify_store_tax_account/
│       ├── shopify_store_webhook/
│       ├── shopify_store_payment_method_mapping/
│       └── item_shopify_store/
└── fixtures/
    └── custom_field.json  # Custom fields for Item, Customer, SO, DN, SI
```

## Fulfillment adapters

The connector works with standard ERPNext without any fulfillment provider. An app that manages its own delivery workflow can register one adapter class in its Frappe `hooks.py`:

```python
shopify_fulfillment_adapter = "my_app.fulfillment.DeliveryAdapter"
```

```python
class DeliveryAdapter:
    def prepare_sources(self, sales_order, order, store, warehouses):
        # Acquire all workflow source locks here, before the Sales Order lock.
        pass

    def get_delivery_items(self, sales_order, line_items, warehouse, store):
        # Return None to use the connector's ordinary ERPNext item mapping.
        # Otherwise return a list of Delivery Note Item dictionaries.
        return None
```

One instance serves the entire fulfillment operation. `prepare_sources` runs once before the connector locks and refreshes the submitted Sales Order. `get_delivery_items` receives that refreshed order for each successful fulfillment that has not already been processed. `order` and `line_items` contain the Shopify payload, `store` is its Shopify Store document, and `warehouses` is the set resolved from the payload's location mappings.

Only `None` requests default item mapping. An empty list supplies no product rows; the connector's configured shipping augmentation still applies. A provider error or invalid return value rolls back the operation, including deliveries created for earlier fulfillments in the same payload. Multiple configured providers fail before any provider is invoked.

The connector owns fulfillment deduplication, the Sales Order lock, shipping augmentation, Delivery Note submission and the transaction. Adapters must not commit, roll back, submit deliveries, or perform external side effects. They must acquire the complete source-lock scope during preparation and reject source changes that would require more locks after the Sales Order is locked. Workflow-specific rules and integration tests belong to the providing app.

Adapters can call `resolve_fulfillment_demands(sales_order, line_items, store)` from `nexwave_shopify_connector.nexwave_shopify.fulfillment_adapter` to resolve exact Shopify line IDs, aggregate repeated requests and apply the store's remaining-quantity setting. The result maps Sales Order Item names to `(source_row, sales_quantity)` pairs. Older imports can match one unambiguous item without a stored line ID; ambiguous or mismatched identities fail explicitly. The module exposes `FULFILLMENT_ADAPTER_API_VERSION = 1` for capability checks.

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/one-highflyer/nexwave_shopify_connector --branch version-15
bench --site [sitename] install-app nexwave_shopify_connector
bench --site [sitename] migrate
```

## Authentication

The connector supports two authentication methods:

### Legacy (Access Token)
Manual access token entry for existing custom apps created before January 2026.

> **Note:** Shopify deprecated legacy custom apps from 1 January 2026. New integrations should use OAuth.

1. Create a Custom App in Shopify Admin > Settings > Apps and sales channels > Develop apps
2. Configure required API scopes
3. Install the app and copy the Admin API access token
4. Enter the token in the Shopify Store's `Access Token` field

### OAuth 2.0 (Recommended)
OAuth flow for Shopify Dev Dashboard apps, required for new apps created after January 2026.

```
NexWave Store Form -> Shopify Auth Page -> NexWave Callback -> Token stored on Shopify Store doc
```

The OAuth flow is self-contained within the Shopify Store document.

#### OAuth Setup Steps

**Step 1: Create Shopify App (Dev Dashboard)**
1. Go to Shopify Admin > Settings > Develop Apps > "Develop apps in Dev Dashboard"
2. In [Shopify Dev Dashboard](https://dev.shopify.com/), click **Apps** > **Create app**
3. Create a new version and add your redirect URI:
   ```
   https://{your-site}/api/method/nexwave_shopify_connector.nexwave_shopify.oauth.callback
   ```
4. Go to **Client credentials** and note the Client ID and Client Secret

**Step 2: Configure Shopify Store in ERPNext**
1. Create/edit a Shopify Store document
2. Set `Auth Method` = "OAuth"
3. Enter the **Client ID** and **Client Secret** from Shopify
4. Copy the **Callback URL** shown on the form to your Shopify app's redirect URIs
5. Click **Actions > Connect to Shopify**
6. Authorise on Shopify when redirected
7. Verify status shows "Connected"

All required scopes are requested automatically during the OAuth flow:
- `read_orders`, `write_orders`
- `read_customers`, `write_customers`
- `read_products`, `write_products`
- `read_inventory`, `write_inventory`
- `read_locations`
- `read_fulfillments`, `write_fulfillments`

## Configuration

1. Navigate to **Shopify Store** DocType
2. Create a new store with:
   - Shop domain (e.g., `mystore.myshopify.com`)
   - Authentication method (Legacy or OAuth)
   - Company mapping
   - Warehouse and location mappings
3. Configure tax settings:
   - Default Sales Tax Account (for GST/VAT)
   - Default Shipping Charges Account
   - Write Off Account (for rounding adjustments, falls back to Company's if not set)
   - Tax account mappings for specific Shopify tax titles
4. Configure field mappings, collection mappings, and filters as needed
5. Enable the store to start syncing

## Contributing

This app uses `pre-commit` for code formatting and linting:

```bash
cd apps/nexwave_shopify_connector
pre-commit install
```

Tools used: ruff, eslint, prettier, pyupgrade
