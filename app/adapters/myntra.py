"""Myntra marketplace production adapter.

Implements the PlatformAdapter contract specifically for Myntra partner portal exports:
- Deterministic Myntra detection requiring explicit "Myntra GST Report" sheet and signature headers
- Resistant to filename spoofing (never claims Flipkart "GST Report", Amazon "MTR", or Meesho "Orders")
- Shared "GST Report" format left to Flipkart adapter to prevent false-positive takeover
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.adapters.all_adapters import (
    CANONICAL_FIELDS,
    FIELD_LABELS,
    BaseGenericAdapter,
    _key,
    _platform_index,
    _ALIAS_INDEX,
)
from .all_adapters import MyntraAdapter as BaseMyntraAdapter
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus, PlatformAdapter


class MyntraAdapter(BaseMyntraAdapter):
    """Production adapter for Myntra partner portal reports."""

    PLATFORM_NAME = 'Myntra'
    SUPPORTED_FILE_TYPES = ['.xlsx', '.csv']
    INSTRUCTIONS = (
        'Myntra Partner Portal > Reports > GST report. Explicit sheet is '
        'named "Myntra GST Report"; line identifier column is "Order Item ID".'
    )
    FORMAT_DOCUMENTED = True
    FILENAME_TOKENS = ('myntra',)
    SHEET_NAMES = ('Myntra GST Report',)

    MAX_HEADER_SCAN_ROWS = 10
    MIN_MAPPED_COLUMNS = 3
    REQUIRED_COLUMNS = ('invoice_number', 'taxable_value')

    SIGNATURE_HEADERS = (
        'Order Item ID',
        'Product Title',
        'Selling Price',
        'Order ID',
        'Invoice Number',
        'Taxable Value',
        'Supply Type',
    )

    HEADER_MAP = dict(BaseMyntraAdapter.HEADER_MAP)

    def detect(self, workbook_or_data, file_name: str = '') -> bool:
        """Deterministic Myntra GST report detection.

        Strict Detection Policy:
        1. If no workbook content is provided, filename tokens are accepted as
           a secondary pre-read hint only.
        2. If workbook content is provided:
           - Requires an explicit Myntra-specific sheet: "Myntra GST Report" (case-insensitive).
           - Must contain strong signature headers (Order Item ID, Product Title, Selling Price, etc.).
           - Shared "GST Report" sheet (used by Flipkart) is NEVER claimed by Myntra, even if
             the filename contains "myntra", avoiding false-positive misclassification.
           - Amazon ("MTR"), Meesho ("Orders"), and generic/unrelated workbooks are rejected.
           - Empty workbooks or wrong sheets with Myntra-like headers are rejected.
        3. CSV files lack workbook sheet provenance; without explicit Myntra-specific
           sheet provenance, detection returns False (safe not-detected limitation).
        """
        name = (file_name or '').lower()
        if workbook_or_data is None:
            return any(token in name for token in self.FILENAME_TOKENS)

        sheets, _name, errors, _diagnostics = self._load_sheets(workbook_or_data, file_name)
        if errors or not sheets:
            return False

        # Look specifically for explicit Myntra sheet: "Myntra GST Report" (case-insensitive)
        target_sheet = next((s for s in sheets if _key(s.name) == 'myntra gst report'), None)
        if target_sheet is None:
            return False

        header_info = self.detect_header_row(target_sheet)
        if header_info is None:
            return False

        mapping = header_info.get('mapping', {})
        raw_headers = set(str(h).strip().lower() for h in header_info.get('headers', []) if h)

        # Require identity (Order Item ID / Invoice Number / Order ID) and taxable value
        has_identity = 'order_item_id' in mapping or 'invoice_number' in mapping or 'order_id' in mapping
        has_taxable = 'taxable_value' in mapping

        if not (has_identity and has_taxable):
            return False

        signature_candidates = [
            'order item id',
            'product title',
            'selling price',
            'order id',
            'invoice number',
            'taxable value',
            'supply type',
            'ship from state',
            'ship to state',
            'buyer gstin',
            'seller gstin',
            'hsn/sac',
            'invoice value',
            'place of supply',
        ]
        matched_signatures = sum(1 for sig in signature_candidates if sig in raw_headers)
        if matched_signatures < 4:
            return False

        return True
