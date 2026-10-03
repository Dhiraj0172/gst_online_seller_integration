"""Flipkart marketplace production adapter.

Implements the PlatformAdapter contract specifically for Flipkart GST reports:
- Deterministic Flipkart GST report detection via workbook structure, "GST Report" sheet, and signature headers
- Explicit Flipkart schema validation distinguishing invalid headers, wrong sheets, malformed files
- High-precision Decimal row parsing preserving order item identity, taxes, POS, credit notes, returns
- Canonical transaction generation (CanonicalTransaction) and normalized data contract
- Zero persistence / database coupling (handled by import_service)
"""
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.adapters.all_adapters import (
    CANONICAL_FIELDS,
    FIELD_LABELS,
    BaseGenericAdapter,
    _key,
    _platform_index,
    _ALIAS_INDEX,
)
from .all_adapters import FlipkartAdapter as BaseFlipkartAdapter
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus, PlatformAdapter
from app.adapters.canonical import CanonicalTransaction
from app.utils.date_utils import format_date_gst, parse_date
from app.utils.gstin_validator import validate_gstin
from app.utils.state_codes import STATE_CODES, resolve_pos_code


class FlipkartAdapter(BaseFlipkartAdapter):
    """Production adapter for Flipkart GST reports."""

    PLATFORM_NAME = 'Flipkart'
    SUPPORTED_FILE_TYPES = ['.xlsx', '.csv']
    INSTRUCTIONS = (
        'Flipkart Seller Hub > Reports > GST report. Sheet is named "GST Report"; '
        'line identifier column is "Order Item ID".'
    )
    FORMAT_DOCUMENTED = True
    FILENAME_TOKENS = ('flipkart',)
    SHEET_NAMES = ('GST Report',)

    MAX_HEADER_SCAN_ROWS = 10
    MIN_MAPPED_COLUMNS = 3
    REQUIRED_COLUMNS = ('invoice_number', 'taxable_value')

    # Signature Flipkart headers that identify a Flipkart GST report
    SIGNATURE_HEADERS = (
        'Order Item ID',
        'Product Title',
        'Selling Price',
        'Order ID',
        'Invoice Number',
        'Taxable Value',
        'Supply Type',
    )

    HEADER_MAP = dict(BaseFlipkartAdapter.HEADER_MAP)

    # ------------------------------------------------------------------
    # Step 2: Deterministic Flipkart Detection
    # ------------------------------------------------------------------
    def detect(self, workbook_or_data, file_name: str = '') -> bool:
        """Deterministic Flipkart GST report detection.

        Checks:
        1. If no workbook content is provided, filename tokens are accepted as
           a secondary hint (pre-read check).
        2. If workbook content is provided:
           - Requires sheet named 'GST Report' (case-insensitive) containing Flipkart signature headers.
           - Or single-sheet / CSV files with filename hint AND Flipkart signature headers.
        Ambiguous, wrong-sheet, or other marketplace workbooks (e.g. Amazon 'MTR', Meesho 'Orders')
        are never classified as Flipkart.
        """
        name = (file_name or '').lower()
        if workbook_or_data is None:
            return any(token in name for token in self.FILENAME_TOKENS)

        sheets, _name, errors, _diagnostics, _wb_ref = self._load_sheets(workbook_or_data, file_name)
        if errors or not sheets:
            return False

        # Look for sheet named 'GST Report' (case-insensitive)
        target_sheet = next((s for s in sheets if _key(s.name) == 'gst report'), None)

        if target_sheet is not None:
            has_any_content = False
            for row in target_sheet.rows():
                if any(v is not None and str(v).strip() for v in row):
                    has_any_content = True
                    break

            if not has_any_content:
                # Empty worksheet titled 'GST Report': detected by explicit sheet name
                return True

            header_info = self.detect_header_row(target_sheet)
            if header_info is None:
                return False
            mapping = header_info['mapping']
            has_identity = 'order_item_id' in mapping or 'invoice_number' in mapping or 'order_id' in mapping
            has_taxable = 'taxable_value' in mapping
            return bool(has_identity and has_taxable)

        # If no sheet is named 'GST Report':
        # Check if it's a CSV or single-sheet file with filename hint
        is_csv = name.endswith('.csv') or (
            len(sheets) == 1 and (sheets[0].name.endswith('.csv') or getattr(sheets[0], 'is_csv', False))
        )
        if is_csv and any(token in name for token in self.FILENAME_TOKENS):
            header_info = self.detect_header_row(sheets[0])
            if header_info is None:
                return False
            mapping = header_info['mapping']
            has_identity = 'order_item_id' in mapping or 'invoice_number' in mapping or 'order_id' in mapping
            has_taxable = 'taxable_value' in mapping
            has_extra = any(
                f in mapping
                for f in ('order_item_id', 'description', 'unit_price', 'supply_type', 'ship_from_state', 'ship_to_state', 'customer_gstin')
            )
            return bool(has_identity and has_taxable and has_extra)

        return False

    # ------------------------------------------------------------------
    # Step 3: Flipkart Schema Validation
    # ------------------------------------------------------------------
    def validate(self, workbook_or_data) -> Tuple[bool, List[str]]:
        """Validate Flipkart GST report structure and required headers.

        Distinguishes:
        - None / missing input
        - Malformed or unreadable file
        - Wrong worksheet (not a Flipkart GST report)
        - Missing required headers
        - Header without transaction rows
        - Valid Flipkart GST report
        """
        if workbook_or_data is None:
            return False, ['No workbook or data provided.']

        sheets, _file_name, load_errors, _diagnostics, _wb_ref = self._load_sheets(workbook_or_data)
        if load_errors:
            return False, list(load_errors)
        if not sheets:
            return False, ['Workbook contains no worksheets.']

        target_sheet = next((s for s in sheets if _key(s.name) == 'gst report'), None)

        if target_sheet is None:
            is_csv = len(sheets) == 1 and (sheets[0].name.endswith('.csv') or getattr(sheets[0], 'is_csv', False))
            if is_csv:
                target_sheet = sheets[0]
            else:
                return False, [
                    f"Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable "
                    f"header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of "
                    f"{len(sheets)} sheet(s): {', '.join(s.name for s in sheets)}). "
                    f"Recognized column names include: {self._expected_columns_hint()}."
                ]

        header_info = self.detect_header_row(target_sheet)
        if header_info is None:
            return False, [
                f"Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable "
                f"header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of "
                f"{len(sheets)} sheet(s)). Recognized column names include: "
                f"{self._expected_columns_hint()}."
            ]

        errors: List[str] = []
        missing = [field for field in self.REQUIRED_COLUMNS if field not in header_info['mapping']]
        if missing:
            errors.append(
                f"Sheet '{target_sheet.name}' is missing required column(s): "
                f"{', '.join(FIELD_LABELS.get(f, f) for f in missing)}. "
                f"Columns found: {', '.join(h for h in header_info['headers'] if h) or '(none)'}."
            )

        if not errors:
            has_rows = False
            for _row_number, _row in self._iter_data_rows(target_sheet, header_info):
                has_rows = True
                break
            if not has_rows:
                errors.append(
                    'No data rows found beneath the header row. The file has headers but no '
                    'transaction rows to import.'
                )

        return (not errors), errors

    # ------------------------------------------------------------------
    # Step 4: Flipkart Row Parsing & Canonicalization
    # ------------------------------------------------------------------
    def parse(self, workbook_or_data, file_name: str = '', stream: bool = False) -> ImportResult:
        """Parse Flipkart GST report into ImportResult containing canonical rows."""
        sheets, resolved_name, load_errors, diagnostics, _wb_ref = self._load_sheets(workbook_or_data, file_name)
        result = ImportResult(platform=self.PLATFORM_NAME, file_name=resolved_name or '')
        result.sheet_count = len(sheets)
        result.metadata['platform'] = self.PLATFORM_NAME
        result.metadata['format_documented'] = self.FORMAT_DOCUMENTED
        result.metadata.update(diagnostics or {})
        result.metadata["_wb_ref"] = _wb_ref

        if load_errors:
            result.errors.extend(load_errors)
            return result

        if not sheets:
            result.errors.append('Workbook contains no worksheets.')
            return result

        target_sheet = next((s for s in sheets if _key(s.name) == 'gst report'), None)
        if target_sheet is None:
            is_csv = len(sheets) == 1 and (sheets[0].name.endswith('.csv') or getattr(sheets[0], 'is_csv', False))
            if is_csv:
                target_sheet = sheets[0]
            else:
                result.errors.append(
                    f'Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable '
                    f'header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of '
                    f'{len(sheets)} sheet(s)). Recognized column names include: '
                    f'{self._expected_columns_hint()}.'
                )
                return result

        header = self.detect_header_row(target_sheet)
        if header is None:
            result.errors.append(
                f'Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable '
                f'header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of '
                f'{len(sheets)} sheet(s)). Recognized column names include: '
                f'{self._expected_columns_hint()}.'
            )
            return result

        header_rows = {target_sheet.name: header['row_number']}
        sheet_columns = {target_sheet.name: sorted(header['mapping'])}
        headers = header['headers']

        def _row_generator():
            for row_number, row in self._iter_data_rows(target_sheet, header):
                raw_data: Dict[str, Any] = {}
                for position in range(max(len(headers), len(row))):
                    if position < len(headers):
                        key = headers[position] or f'col_{position}'
                    else:
                        key = f'col_{position}'
                    raw_data[key] = row[position] if position < len(row) else None
                if len(row) > len(headers):
                    raw_data['_extra_values'] = [row[i] for i in range(len(headers), len(row))]

                row_object = self._build_import_row(raw_data, row_number, target_sheet.name)
                yield row_object

        result.rows = _row_generator()
        
        if not stream:
            result.rows = list(result.rows)
            result.total_rows = len(result.rows)
            for row_object in result.rows:
                if row_object.status is ImportRowStatus.ERROR:
                    result.error_rows += 1
                elif row_object.status is ImportRowStatus.WARNING:
                    result.warning_rows += 1
                elif row_object.status is ImportRowStatus.SKIPPED:
                    result.skipped_rows += 1
                else:
                    result.success_rows += 1

        result.metadata['header_rows'] = header_rows
        result.metadata['sheet_columns'] = sheet_columns
        result.metadata['sheets_parsed'] = [target_sheet.name]
        result.metadata['unrecognized_headers'] = sorted(set(header['unknown_headers']))
        result.metadata['recognized_columns'] = sorted(set(header['mapping'].keys()))

        if header['unknown_headers']:
            result.warnings.append(
                f"{len(set(header['unknown_headers']))} unrecognised column(s) recorded and ignored: "
                f"{', '.join(sorted(set(header['unknown_headers'])))}"
            )

        self._finalize_result(result)
        return result

    def _build_import_row(self, raw_data: Dict[str, Any], row_number: int, sheet_name: str) -> ImportRow:
        """Construct ImportRow with normalized dictionary and CanonicalTransaction."""
        normalized = self.normalize(raw_data)
        warnings = list(normalized.pop('_warnings', []) or [])
        errors: List[str] = list(normalized.pop('_errors', []) or [])
        extra_values = raw_data.get('_extra_values')
        if extra_values:
            warnings.append(
                f'{len(extra_values)} value(s) beyond the header columns were kept as '
                f'col_N and are not mapped to any field'
            )
        status = ImportRowStatus.ERROR if errors else (
            ImportRowStatus.WARNING if warnings else ImportRowStatus.SUCCESS
        )
        canonical = CanonicalTransaction.from_dict(normalized)
        return ImportRow(
            row_number=row_number,
            status=status,
            raw_data=raw_data,
            normalized_data=normalized,
            errors=errors,
            warnings=warnings,
            sheet_name=sheet_name,
            canonical_data=canonical,
        )

    # ------------------------------------------------------------------
    # Field Mapping Implementation
    # ------------------------------------------------------------------
    def map_invoice(self, raw_row: Dict, values: Optional[Dict[str, Any]] = None,
                    warnings: Optional[List[str]] = None) -> Dict:
        reverse_charge_raw = self._value(values, raw_row, 'reverse_charge')
        reverse_charge = 'Y' if str(reverse_charge_raw or '').strip().upper() in (
            'Y', 'YES', 'TRUE', '1'
        ) else 'N'
        return {
            'invoice_number': self.get_string(raw_row, ['Invoice Number'], values=values),
            'invoice_date': self._value(values, raw_row, 'invoice_date'),
            'invoice_type': 'regular',
            'reverse_charge': reverse_charge,
            'supply_type': str(self._value(values, raw_row, 'supply_type') or '').strip(),
            'document_type': str(self._value(values, raw_row, 'document_type') or '').strip(),
        }

    def map_customer(self, raw_row: Dict, values: Optional[Dict[str, Any]] = None,
                     warnings: Optional[List[str]] = None) -> Dict:
        return {
            'customer_name': self.get_string(raw_row, ['Buyer Name', 'Customer Name'], values=values),
            'customer_gstin': str(self._value(values, raw_row, 'customer_gstin') or '').strip().upper(),
            'seller_gstin': str(self._value(values, raw_row, 'seller_gstin') or '').strip().upper(),
            'place_of_supply': self._value(values, raw_row, 'place_of_supply'),
            'ship_from_state': self.get_string(raw_row, ['Ship From State'], values=values),
            'ship_to_state': self.get_string(raw_row, ['Ship To State'], values=values),
            'buyer_state': self.get_string(raw_row, ['Buyer State'], values=values),
            'ecommerce_gstin': str(self._value(values, raw_row, 'ecommerce_gstin') or '').strip().upper(),
        }

    def map_items(self, raw_row: Dict, values: Optional[Dict[str, Any]] = None,
                  warnings: Optional[List[str]] = None) -> Dict:
        qty = self._decimal_field(values, raw_row, 'quantity', warnings, label='Quantity')
        unit_price = self._decimal_field(values, raw_row, 'unit_price', warnings, label='Unit price')
        return {
            'order_id': self.get_string(raw_row, ['Order ID', 'Order Id'], values=values),
            'order_item_id': self.get_string(raw_row, ['Order Item ID'], values=values),
            'hsn_sac': str(self._value(values, raw_row, 'hsn_sac') or '').strip(),
            'description': self.get_string(raw_row, ['Product Title', 'Product Description', 'Description'], values=values),
            'quantity': qty,
            'unit_price': unit_price,
            'uqc': 'NOS',
        }

    def map_taxes(self, raw_row: Dict, values: Optional[Dict[str, Any]] = None,
                  warnings: Optional[List[str]] = None) -> Dict:
        return {
            'taxable_value': self._decimal_field(values, raw_row, 'taxable_value', warnings),
            'cgst_rate': self._decimal_field(values, raw_row, 'cgst_rate', warnings),
            'cgst_amount': self._decimal_field(values, raw_row, 'cgst_amount', warnings),
            'sgst_rate': self._decimal_field(values, raw_row, 'sgst_rate', warnings),
            'sgst_amount': self._decimal_field(values, raw_row, 'sgst_amount', warnings),
            'igst_rate': self._decimal_field(values, raw_row, 'igst_rate', warnings),
            'igst_amount': self._decimal_field(values, raw_row, 'igst_amount', warnings),
            'cess_rate': self._decimal_field(values, raw_row, 'cess_rate', warnings),
            'cess_amount': self._decimal_field(values, raw_row, 'cess_amount', warnings),
            'tax_rate': self._decimal_field(values, raw_row, 'tax_rate', warnings),
            'invoice_value': self._decimal_field(values, raw_row, 'invoice_value', warnings),
        }

    def map_returns(self, raw_row: Dict, values: Optional[Dict[str, Any]] = None) -> Dict:
        status = str(self._value(values, raw_row, 'order_status') or '').strip().lower()
        supply_type = str(self._value(values, raw_row, 'supply_type') or '').strip().lower()
        document_type = str(self._value(values, raw_row, 'document_type') or '').strip().lower()
        text = ' '.join(part for part in (status, supply_type, document_type) if part)

        cancelled = 'cancel' in text
        returned = any(token in text for token in ('return', 'rto', 'refund', 'exchange'))
        return {
            'return_flag': bool(returned or cancelled),
            'cancellation_flag': bool(cancelled),
            'event_status_raw': ' '.join(part for part in (status, supply_type) if part),
        }

    def map_notes(self, raw_row: Dict, values: Optional[Dict[str, Any]] = None) -> Dict:
        note_type_raw = str(
            self._value(values, raw_row, 'note_type')
            or self._value(values, raw_row, 'document_type')
            or ''
        ).strip()
        supply_type_text = str(self._value(values, raw_row, 'supply_type') or '').strip()
        inv_num = str(self._value(values, raw_row, 'invoice_number') or '').strip().upper()
        lowered = ' '.join(part for part in (note_type_raw, supply_type_text) if part).lower()

        if 'credit' in lowered or lowered in ('cr', 'c') or inv_num.startswith('CN-'):
            note_type = 'CREDIT'
        elif 'debit' in lowered or lowered in ('dr', 'd') or inv_num.startswith('DN-'):
            note_type = 'DEBIT'
        else:
            note_type = ''

        if note_type and not note_type_raw:
            note_type_raw = supply_type_text or ('Credit Note' if note_type == 'CREDIT' else 'Debit Note')

        note_number = self.get_string(raw_row, ['Credit Note No', 'Debit Note No', 'Note Number'], values=values)
        if not note_number and note_type:
            note_number = self.get_string(raw_row, ['Invoice Number'], values=values)

        note_date = self._value(values, raw_row, 'note_date')
        if not note_date and note_type:
            note_date = self._value(values, raw_row, 'invoice_date')

        return {
            'note_type': note_type,
            'note_type_raw': note_type_raw,
            'note_number': note_number,
            'note_date': note_date,
            'original_invoice_number': self.get_string(raw_row, ['Original Invoice Number'], values=values),
            'original_invoice_date': self._value(values, raw_row, 'original_invoice_date'),
        }

    def normalize(self, raw_row: Dict) -> Dict:
        values = self._row_field_values(raw_row)
        warnings: List[str] = []

        normalized: Dict[str, Any] = {}
        normalized.update(self.map_invoice(raw_row, values, warnings))
        normalized.update(self.map_customer(raw_row, values, warnings))
        normalized.update(self.map_items(raw_row, values, warnings))
        normalized.update(self.map_taxes(raw_row, values, warnings))
        normalized.update(self.map_returns(raw_row, values))
        normalized.update(self.map_notes(raw_row, values))
        normalized['marketplace_name'] = (
            str(self._value(values, raw_row, 'marketplace_name') or '').strip()
            or self.PLATFORM_NAME
        )
        normalized['source_platform'] = self.PLATFORM_NAME
        normalized['_errors'] = []

        self.post_normalize(raw_row, normalized, values, warnings)

        normalized['source_metadata'] = {
            'platform': self.PLATFORM_NAME,
            'marketplace': normalized.get('marketplace_name', ''),
            'columns': [str(key) for key in raw_row.keys() if not str(key).startswith('_')],
        }
        normalized['_warnings'] = warnings

        # Ensure all canonical fields are present in the returned dictionary
        for field in CANONICAL_FIELDS:
            if field not in normalized:
                normalized[field] = None

        return normalized

    def post_normalize(self, raw_row: Dict, normalized: Dict[str, Any],
                       values: Dict[str, Any], warnings: List[str]) -> None:
        """Canonicalise dates, place of supply, GSTINs, and check for invalid/negative values."""
        super().post_normalize(raw_row, normalized, values, warnings)
        errors: List[str] = normalized.setdefault('_errors', [])

        # Validate required fields
        raw_inv = normalized.get('invoice_number')
        if raw_inv is None or not str(raw_inv).strip():
            errors.append('Missing Invoice number')

        raw_taxable = self._value(values, raw_row, 'taxable_value')
        if raw_taxable is None or (isinstance(raw_taxable, str) and not raw_taxable.strip()):
            errors.append('Missing Taxable value')
        elif self._to_decimal(raw_taxable) is None:
            err_msg = f"Taxable value '{raw_taxable}' is not a valid numeric amount"
            if err_msg not in errors:
                errors.append(err_msg)
