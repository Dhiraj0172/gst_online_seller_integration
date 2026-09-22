# Platform Adapter Guide

## Overview

The GST Online Seller module uses a plugin architecture for importing data from different e-commerce platforms. Each platform has its own adapter that knows how to parse that platform's specific file format and map it to the canonical internal transaction model.

## Architecture

```
PlatformAdapter (Abstract Base Class)
├── detect()          - Can this adapter handle this file?
├── validate()        - Is the file structurally valid?
├── parse()           - Parse file into ImportResult
├── normalize()       - Convert raw row to canonical format
├── map_taxes()       - Extract tax components
├── map_customer()    - Extract customer info
├── map_invoice()     - Extract invoice details
├── map_items()       - Extract line items
├── map_returns()     - Handle return/cancellation rows
├── map_notes()       - Handle credit/debit notes
└── get_import_errors() - Get accumulated errors
```

## Creating a New Adapter

### Step 1: Create the adapter file

Create `app/adapters/your_platform.py`:

```python
from app.adapters.all_adapters import BaseGenericAdapter

class YourPlatformAdapter(BaseGenericAdapter):
    PLATFORM_NAME = 'YourPlatform'
    SUPPORTED_FILE_TYPES = ['xlsx']
    INSTRUCTIONS = 'Download the GST report from YourPlatform seller dashboard'
    
    # Map platform-specific column names to canonical names
    HEADER_MAP = {
        'Platform Order ID': 'order_id',
        'Platform Invoice No': 'invoice_number',
        'Platform Date': 'invoice_date',
        # ... map all columns
    }
    
    EXPECTED_HEADERS = list(HEADER_MAP.keys())
```

### Step 2: Register the adapter

Add to `app/adapters/__init__.py`:
```python
from .your_platform import YourPlatformAdapter
```

The adapter auto-registers through the registry system.

### Step 3: Add tests

Create test fixture data and adapter-specific tests.

## Canonical Transaction Fields

Every adapter must map to these fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| order_id | str | No | Platform order identifier |
| invoice_number | str | Yes | Invoice number |
| invoice_date | str | Yes | DD-MM-YYYY format |
| customer_name | str | No | Buyer name |
| customer_gstin | str | No | Buyer GSTIN (empty for B2C) |
| place_of_supply | str | Yes | State code or "code-name" |
| seller_gstin | str | Yes | Seller's GSTIN |
| hsn_sac | str | Yes | HSN/SAC code |
| description | str | No | Item description |
| quantity | Decimal | Yes | Quantity |
| taxable_value | Decimal | Yes | Taxable amount |
| cgst_rate | Decimal | No | CGST rate |
| cgst_amount | Decimal | No | CGST amount |
| sgst_rate | Decimal | No | SGST rate |
| sgst_amount | Decimal | No | SGST amount |
| igst_rate | Decimal | No | IGST rate |
| igst_amount | Decimal | No | IGST amount |
| cess_rate | Decimal | No | Cess rate |
| cess_amount | Decimal | No | Cess amount |
| tax_rate | Decimal | Yes | Total GST rate |
| invoice_value | Decimal | Yes | Total invoice value |
| reverse_charge | str | No | Y/N |
| ecommerce_gstin | str | No | E-commerce operator GSTIN |
| note_type | str | No | CREDIT/DEBIT |
| cancellation_flag | bool | No | Is cancelled |
| return_flag | bool | No | Is return |

## Error Handling Rules

1. **Never crash on bad input** - Catch exceptions per row, record error
2. **Never silently discard data** - Every row must have a status
3. **Record unexpected columns** - Log them as warnings
4. **Validate GSTIN format** - But don't reject on checksum
5. **Parse dates flexibly** - Support multiple date formats
6. **Handle missing optional columns** - Use defaults, add warning

## Current Adapters

| Platform | Key Column Names | Notes |
|----------|-----------------|-------|
| Amazon | Order ID, Invoice Number, Invoice Date, HSN/SAC, Taxable Value | MTR format |
| Flipkart | Order ID, Order Item ID, Invoice Number, Product Title | GST report format |
| Meesho | Order ID, Sub Order ID, HSN, Taxable Amount, Total | Orders report |
| Myntra | Similar to Flipkart | Same parent company |
| Custom Excel | User-mapped columns | Flexible header detection |
| GSTR-1 Govt | Official GSTR-1 sheet names | Multi-sheet workbook |
