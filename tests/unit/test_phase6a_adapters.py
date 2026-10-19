"""Phase 6A: Unit and Contract Tests for 15/15 Marketplace Production Adapters.

Tests all 9 newly promoted production adapters:
  - JioMartAdapter
  - SnapdealAdapter
  - TataCLiQAdapter (and TataCliqAdapter)
  - LimeroadAdapter
  - ShopdeckAdapter
  - GlowRoadAdapter
  - SnapmintAdapter
  - CitymallAdapter
  - RoposoAdapter

Verifies:
1. FORMAT_DOCUMENTED = True and status: 'PRODUCTION' in readiness matrix for all 15 adapters.
2. Complete seller-panel HEADER_MAP and SIGNATURE_HEADERS.
3. Canonical field normalization for all 15 key attributes:
   order_id, invoice_number, invoice_date, transaction_type, buyer_gstin,
   place_of_supply, hsn_code, quantity, taxable_value, gst_rate,
   igst_amount, cgst_amount, sgst_amount, tcs_amount, ecommerce_gstin.
4. Sheet / filename detection and validation failure modes.
"""
from decimal import Decimal
import pytest

from app.adapters.all_adapters import (
    CitymallAdapter,
    GlowRoadAdapter,
    JioMartAdapter,
    LimeroadAdapter,
    RoposoAdapter,
    ShopdeckAdapter,
    SnapdealAdapter,
    SnapmintAdapter,
    TataCLiQAdapter,
    TataCliqAdapter,
    get_adapter_readiness_matrix,
)
from app.adapters.canonical import CanonicalTransaction
from app.adapters.registry import detect_platform, get_adapter, list_platforms


PROMOTED_ADAPTER_CLASSES = [
    (JioMartAdapter, 'JioMart', 'jiomart'),
    (SnapdealAdapter, 'Snapdeal', 'snapdeal'),
    (TataCliqAdapter, 'TataCLiQ', 'tatacliq'),
    (LimeroadAdapter, 'Limeroad', 'limeroad'),
    (ShopdeckAdapter, 'Shopdeck', 'shopdeck'),
    (GlowRoadAdapter, 'GlowRoad', 'glowroad'),
    (SnapmintAdapter, 'Snapmint', 'snapmint'),
    (CitymallAdapter, 'Citymall', 'citymall'),
    (RoposoAdapter, 'Roposo', 'roposo'),
]

ALL_15_PLATFORMS = [
    'Amazon',
    'Citymall',
    'Custom_Excel',
    'Flipkart',
    'GSTR1_Govt',
    'GlowRoad',
    'JioMart',
    'Limeroad',
    'Meesho',
    'Myntra',
    'Roposo',
    'Shopdeck',
    'Snapdeal',
    'Snapmint',
    'TataCLiQ',
]


class TestPhase6AProductionAdapters:
    def test_all_15_platforms_report_production_readiness(self):
        matrix = get_adapter_readiness_matrix()
        assert len(matrix) == 15
        platform_names = [m['platform'] for m in matrix]
        assert sorted(platform_names) == sorted(ALL_15_PLATFORMS)

        for entry in matrix:
            assert entry['format_documented'] is True, f"{entry['platform']} not documented"
            assert entry['status'] == 'PRODUCTION', f"{entry['platform']} status is {entry['status']}"
            if entry['platform'] != 'Custom_Excel':
                assert entry['mapped_headers_count'] >= 25, (
                    f"{entry['platform']} has only {entry['mapped_headers_count']} headers"
                )

    @pytest.mark.parametrize("cls, name, token", PROMOTED_ADAPTER_CLASSES)
    def test_promoted_adapter_metadata_and_signatures(self, cls, name, token):
        adapter = cls()
        assert adapter.PLATFORM_NAME == name
        assert adapter.FORMAT_DOCUMENTED is True
        assert len(adapter.HEADER_MAP) >= 70, f"{name} HEADER_MAP too small: {len(adapter.HEADER_MAP)}"
        assert len(adapter.SIGNATURE_HEADERS) >= 10, f"{name} SIGNATURE_HEADERS too small"
        assert token in adapter.FILENAME_TOKENS
        assert len(adapter.SHEET_NAMES) >= 2

    @pytest.mark.parametrize("cls, name, token", PROMOTED_ADAPTER_CLASSES)
    def test_all_15_canonical_fields_normalization(self, cls, name, token):
        """Test complete extraction of all 15 required canonical fields."""
        adapter = cls()
        raw_row = {
            'Order ID': 'ORD-6A-999',
            'Invoice Number': 'INV-6A-0001',
            'Invoice Date': '15-08-2026',
            'Customer GSTIN': '27ABCDE1234F1Z5',
            'Place of Supply': '27',
            'HSN Code': '8471',
            'Product Name': 'Wireless Earbuds',
            'Quantity': '2',
            'Taxable Value': '2000.00',
            'Tax Rate': '18',
            'IGST Amount': '0.00',
            'CGST Amount': '180.00',
            'SGST Amount': '180.00',
            'Invoice Value': '2360.00',
            'TCS Amount': '20.00',
            'E-Commerce GSTIN': '27AABCT3518Q1ZV',
            'Transaction Type': 'Regular Sale',
        }

        normalized = adapter.normalize(raw_row)
        assert normalized['_errors'] == []
        assert normalized['order_id'] == 'ORD-6A-999'
        assert normalized['invoice_number'] == 'INV-6A-0001'
        assert normalized['invoice_date'] == '15-08-2026'
        assert normalized['customer_gstin'] == '27ABCDE1234F1Z5'
        assert normalized['buyer_gstin'] == '27ABCDE1234F1Z5'
        assert normalized['place_of_supply'] == '27'
        assert normalized['hsn_sac'] == '8471'
        assert normalized['hsn_code'] == '8471'
        assert normalized['quantity'] == Decimal('2')
        assert normalized['taxable_value'] == Decimal('2000.00')
        assert normalized['tax_rate'] == Decimal('18')
        assert normalized['gst_rate'] == Decimal('18')
        assert normalized['cgst_amount'] == Decimal('180.00')
        assert normalized['sgst_amount'] == Decimal('180.00')
        assert normalized['igst_amount'] == Decimal('0.00')
        assert normalized['tcs_amount'] == Decimal('20.00')
        assert normalized['ecommerce_gstin'] == '27AABCT3518Q1ZV'
        assert normalized['transaction_type'] == 'Regular Sale'

        # CanonicalTransaction contract
        canonical = CanonicalTransaction.from_dict(normalized)
        assert canonical.order_id == 'ORD-6A-999'
        assert canonical.invoice_number == 'INV-6A-0001'
        assert canonical.buyer_gstin == '27ABCDE1234F1Z5'
        assert canonical.customer_gstin == '27ABCDE1234F1Z5'
        assert canonical.place_of_supply == '27'
        assert canonical.hsn_code == '8471'
        assert canonical.hsn_sac == '8471'
        assert canonical.quantity == Decimal('2')
        assert canonical.taxable_value == Decimal('2000.00')
        assert canonical.gst_rate == Decimal('18')
        assert canonical.tax_rate == Decimal('18')
        assert canonical.cgst_amount == Decimal('180.00')
        assert canonical.sgst_amount == Decimal('180.00')
        assert canonical.tcs_amount == Decimal('20.00')
        assert canonical.ecommerce_gstin == '27AABCT3518Q1ZV'
        assert canonical.transaction_type == 'Regular Sale'

    @pytest.mark.parametrize("cls, name, token", PROMOTED_ADAPTER_CLASSES)
    def test_validation_rejects_missing_invoice_number(self, cls, name, token):
        adapter = cls()
        headers = ['Order ID', 'Item Code', 'Quantity', 'Taxable Value']
        row = ['ORD-001', 'SKU-1', 1, 100]
        valid, errors = adapter.validate([headers, row])
        assert valid is False
        assert any('invoice number' in err.lower() for err in errors)

    def test_tatacliq_alias_identity(self):
        assert TataCLiQAdapter is TataCliqAdapter
