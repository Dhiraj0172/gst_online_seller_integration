# GST Online Seller Module

## Overview

Production-ready GST Online Seller module for processing e-commerce marketplace data and generating GSTR-1 returns. Supports 16+ e-commerce platforms with a plugin adapter architecture.

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   Frontend (UI)                   │
│        Flask Templates + Bootstrap 5 + JS         │
├──────────────────────────────────────────────────┤
│                   Routes/API                      │
│    auth | profile | import | statement | generate │
├──────────────────────────────────────────────────┤
│                  Services Layer                   │
│  classification | b2b | b2c | cdnr | hsn | tcs   │
│      gstr1_generator | reconciliation            │
├──────────────────────────────────────────────────┤
│               Platform Adapters                   │
│  Amazon | Flipkart | Meesho | Myntra | +12 more  │
├──────────────────────────────────────────────────┤
│               Validators/Utils                    │
│  GSTIN | HSN | Tax Calc | Dates | State Codes    │
├──────────────────────────────────────────────────┤
│               Data Layer (SQLAlchemy)             │
│  User | GSTProfile | Transaction | ImportHistory  │
│  RawImport | AuditLog | GSTR1Generation | TCS    │
├──────────────────────────────────────────────────┤
│                SQLite Database                    │
└──────────────────────────────────────────────────┘
```

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.13, Flask 3.0 |
| Database | SQLite via SQLAlchemy 2.0 |
| Frontend | HTML5, Bootstrap 5.3, Vanilla JS |
| Excel | openpyxl 3.1 |
| Auth | Flask-Login |
| Security | Flask-WTF (CSRF), werkzeug (password hashing) |
| Testing | pytest 9.x |
| JSON Validation | jsonschema |

## Quick Start

```bash
cd gst_online_seller
pip install -r requirements.txt
python run.py
```

Open http://localhost:5000 in your browser.

## Project Structure

```
gst_online_seller/
├── app/
│   ├── __init__.py              # Flask app factory
│   ├── config.py                # Configuration
│   ├── extensions.py            # Flask extensions
│   ├── adapters/                # Platform import adapters
│   │   ├── base.py              # Abstract base adapter
│   │   ├── registry.py          # Adapter registry
│   │   ├── amazon.py            # Amazon MTR adapter
│   │   ├── flipkart.py          # Flipkart adapter
│   │   ├── meesho.py            # Meesho adapter
│   │   └── ... (16 adapters)
│   ├── models/                  # SQLAlchemy models
│   │   ├── user.py              # User model
│   │   ├── gst_profile.py       # GST Profile
│   │   ├── transaction.py       # Normalized transactions
│   │   ├── import_history.py    # Import tracking
│   │   ├── raw_import.py        # Raw data preservation
│   │   ├── audit_log.py         # Change audit trail
│   │   ├── gstr1_generation.py  # Generation tracking
│   │   └── tcs_reconciliation.py
│   ├── routes/                  # Flask route blueprints
│   │   ├── auth.py              # Authentication
│   │   ├── main.py              # Dashboard
│   │   ├── profile.py           # GST profiles
│   │   ├── import_routes.py     # File import
│   │   ├── statement.py         # Manage data
│   │   ├── tcs.py               # TCS reconciliation
│   │   ├── generate.py          # GSTR-1 generation
│   │   └── api.py               # REST API
│   ├── services/                # Business logic
│   │   ├── classification_service.py
│   │   ├── gst_rules.py         # Period-aware rules
│   │   ├── gstr1_generator.py   # Main generator
│   │   ├── gstr1_excel_writer.py
│   │   ├── gstr1_json_writer.py
│   │   ├── gstr1_json_validator.py
│   │   ├── b2b_service.py
│   │   ├── b2c_service.py
│   │   ├── cdnr_service.py
│   │   ├── hsn_service.py
│   │   ├── nil_service.py
│   │   ├── ecom_service.py
│   │   ├── tcs_service.py
│   │   ├── import_service.py
│   │   └── reconciliation_service.py
│   ├── utils/                   # Utilities
│   │   ├── gstin_validator.py
│   │   ├── tax_calculator.py
│   │   ├── date_utils.py
│   │   ├── state_codes.py
│   │   ├── hsn_utils.py
│   │   ├── uqc_codes.py
│   │   └── file_utils.py
│   ├── validators/              # Data validation
│   │   ├── transaction_validator.py
│   │   └── import_validator.py
│   ├── templates/               # Jinja2 HTML templates
│   └── static/                  # CSS, JS
├── tests/
│   ├── unit/                    # 95 unit tests
│   ├── integration/             # E2E pipeline tests
│   └── fixtures/                # 27 test Excel files
├── requirements.txt
└── run.py
```

## Supported Platforms (16)

| # | Platform | Adapter File | Status |
|---|----------|-------------|--------|
| 1 | Amazon | amazon.py | ✅ Ready |
| 2 | Flipkart | flipkart.py | ✅ Ready |
| 3 | Meesho | meesho.py | ✅ Ready |
| 4 | Myntra | myntra.py | ✅ Ready |
| 5 | JioMart | jiomart.py | ✅ Ready |
| 6 | Snapdeal | snapdeal.py | ✅ Ready |
| 7 | Tata CLiQ | tatacliq.py | ✅ Ready |
| 8 | Limeroad | limeroad.py | ✅ Ready |
| 9 | Shopdeck | shopdeck.py | ✅ Ready |
| 10 | GlowRoad | glowroad.py | ✅ Ready |
| 11 | Snapmint | snapmint.py | ✅ Ready |
| 12 | Citymall | citymall.py | ✅ Ready |
| 13 | Roposo/Clout | roposo.py | ✅ Ready |
| 14 | GSTR-1 Govt Excel | gstr1_govt.py | ✅ Ready |
| 15 | Custom Excel | custom_excel.py | ✅ Ready |
| 16 | Generic | all_adapters.py | ✅ Ready |

## GST Rules Engine

Period-aware rules for B2CL threshold and HSN reporting:

| Rule | Before | After | Change Date |
|------|--------|-------|-------------|
| B2CL | Inter-state B2C > ₹2.5L = B2CL | B2CL eliminated, all → B2CS | August 2024 |
| HSN | Combined single table | Separate B2B + B2C tables | May 2025 |

## GSTR-1 Output

### Excel Format
Government offline tool format with sheets: b2b, b2ba, b2cl, b2cs, cdnr, cdnur, exp, nil, hsn, hsnb2c, docs

### JSON Format
GSTN-compatible JSON with tables: b2b, b2cl, b2cs, cdnr, cdnur, exp, nil, hsn, doc_issue

### Validation Layers
1. **Syntax** - Valid JSON
2. **Schema** - Correct structure
3. **Field** - Valid field values (GSTIN, dates, rates)
4. **Business Rules** - Tax calculations, supply type consistency
5. **Cross-table** - HSN totals match invoices

## Rounding Rules

- All monetary calculations use `decimal.Decimal`
- Rounding: `ROUND_HALF_UP` to 2 decimal places
- Rounding applied at the reporting boundary, not intermediate steps
- Tax rates: 0%, 0.1%, 0.25%, 1%, 1.5%, 3%, 5%, 6%, 7.5%, 12%, 18%, 28%

## Security

- Password hashing (werkzeug.security)
- CSRF protection (Flask-WTF)
- File upload validation (size, extension, MIME)
- Filename sanitization (path traversal prevention)
- User-scoped data access
- Soft deletes with audit trail
- SQL injection prevention (SQLAlchemy ORM)

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run unit tests only
python -m pytest tests/unit/ -v

# Run integration tests
python -m pytest tests/integration/ -v

# Generate test fixtures
python tests/fixtures/generate_test_data.py
```

**Test Coverage:**
- 95 unit tests (GSTIN, tax calc, dates, state codes, HSN, classification, validators, Excel, JSON)
- 18 integration tests (end-to-end pipeline, Excel round-trip, JSON schema, rounding edge cases)
- 27 test fixture files

## Known Limitations

1. **GSTIN Checksum**: The GSTN check digit algorithm is not reliably documented publicly. Format + state code are validated strictly; checksum is not enforced.
2. **Platform Adapters**: Column formats vary across platforms and may change. Adapters use flexible header matching.
3. **Background Jobs**: Celery/Redis configured but processing currently runs synchronously for simplicity.
4. **GSTN Schema**: Based on publicly observable JSON structure. Should be updated when official schema documents are available.

## GST Rule Update Process

1. Add new rule entry to `GST_RULE_VERSIONS` in `app/services/gst_rules.py`
2. Update `get_rules_for_period()` with the new effective date
3. Update classification logic in `classification_service.py` if needed
4. Update Excel writer for new sheet structures
5. Update JSON writer for new JSON structures
6. Add tests for the new period
7. Run full regression suite
