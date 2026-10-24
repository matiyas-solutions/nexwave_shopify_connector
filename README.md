# ERPNext Shopify Connector

Multi-store Shopify connector for ERPNext, designed to connect multiple Shopify stores to a single ERPNext instance with multi-company support. Built and maintained by [HighFlyer](https://highflyerglobal.com/).

> Works with any ERPNext v15+ instance. No additional dependencies beyond ERPNext.

![Multi-Store Architecture](docs/multi-store-architecture.svg)

## Key Features

### Multi-Store, Multi-Company
Connect multiple Shopify stores to one ERPNext instance, each mapped to a separate company for proper accounting separation.

### Real-Time Order Sync
Webhook-based order ingestion with HMAC validation. Orders are automatically routed to the correct company based on store configuration. Supports automatic submission, invoicing, and payment entry creation for paid orders.

### Fulfillment Sync
Listens to Shopify fulfillment webhooks and automatically creates Delivery Notes with tracking information. Handles partial fulfillments and prevents duplicates.

With NZ customisations installed, warehouses using **Stage Before Dispatch** consume already staged Pick Lists. Partial deliveries retain the exact order row, pick row, staging bin and batch allocation. Complete physical picking and staging before the Shopify fulfillment arrives; the connector does not perform those warehouse operations. A shortage, an existing draft delivery, or an active warehouse dispatch task blocks automatic delivery and leaves the event retryable. Serial-tracked items and Product Bundles remain subject to NZ's staging restrictions.

New order imports retain Shopify line IDs. Older orders can use a unique matching item; repeated items without line IDs require their source identities to be resolved before automatic staged delivery. Run the normal site migration when upgrading to add the line-ID field.

Each physical source allocation must fit the order's sales UOM at Delivery Note quantity precision. Allocations that cannot be represented exactly stop before stock moves. NZ integration tests were verified with customisations v1.59.7 (`6daba050c72a295c1d475ce4f08a2c4cef2f02c0`), Frappe v15.121.3, ERPNext v15.121.6, HRMS v15.64.6 and Payments `9885a6e500104f1f160add47d55b370773e67a81`.

The normal CI suite requires only ERPNext. To run the optional staged-dispatch integration tests, install NZ customisations and its dependencies on a blank test site, enable tests, then run `bench --site <test-site> run-tests --app nexwave_shopify_connector --module nexwave_shopify_connector.nexwave_shopify.test_staged_dispatch`. These cases skip when NZ is absent.

### Product and Inventory Push
Sync items from ERPNext to Shopify with configurable field mapping (standard fields and metafields). Push stock levels from ERPNext warehouses to Shopify locations with multi-location support.

### Configurable Tax Handling
- "On Net Total" approach for clean, efficient tax calculation
- Automatic zero-rated item detection with item tax template overrides
- Multi-region support (NZ 15% GST, AU 10% GST, and others)
- Shipping tax handling (as line item or separate tax row)
- Automatic rounding adjustments for Shopify/ERPNext total matching

### Collection Mapping
Map ERPNext field values (item group, brand, etc.) to Shopify Custom Collections for automatic product categorization during sync.

### Payment Method Mapping
Map Shopify payment gateways to ERPNext Mode of Payment for accurate payment tracking.

### OAuth 2.0 and Legacy Auth
Supports modern OAuth flow (recommended for new integrations) and legacy access tokens for existing custom apps.

### SKU-Based Migration Tool
Link existing Shopify products to ERPNext items by matching SKUs. Generates a report of matched, unmatched, and conflicting items.

### Item Eligibility Filters
Control which items sync to which stores using manual overrides or automatic filter rules (e.g., "if field X has value, sync to store Y").

## Data Flow

![Data Flow](docs/data-flow.svg)

## Documentation

- [Shopify Integration Overview](https://docs.nexwaveapp.com/s/docs/doc/shopify-integration-L1ofBu1m6e)
- [Shopify Connector Setup Guide](https://docs.nexwaveapp.com/s/docs/doc/shopify-connector-setup-guide-hpVraqmSVk)
- [Developer Guide](docs/developer-guide.md)

## License

GNU General Public License v3.0
