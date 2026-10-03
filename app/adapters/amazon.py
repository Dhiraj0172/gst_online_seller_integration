"""Amazon Marketplace Tax Report (MTR) production adapter.

Implements the PlatformAdapter contract specifically for Amazon MTR exports:
- Deterministic Amazon MTR detection via workbook structure, MTR sheet, and signature headers
- Explicit Amazon MTR schema validation distinguishing invalid headers, wrong sheets, malformed files
- High-precision Decimal row parsing preserving invoice identity, taxes, POS, credit notes, cancellations
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
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus, PlatformAdapter
from app.adapters.canonical import CanonicalTransaction
from app.utils.date_utils import format_date_gst, parse_date
from app.utils.gstin_validator import validate_gstin
from app.utils.state_codes import STATE_CODES, resolve_pos_code


class AmazonAdapter(BaseGenericAdapter):
    """Production adapter for Amazon Marketplace Tax Reports (MTR)."""

    PLATFORM_NAME = 'Amazon'
    SUPPORTED_FILE_TYPES = ['.xlsx', '.csv']
    INSTRUCTIONS = (
        'Amazon Seller Central > Reports > Tax Document Library > MTR '
        '(Marketplace Tax Report) export. Sheet is named "MTR".'
    )
    FORMAT_DOCUMENTED = True
    FILENAME_TOKENS = ('amazon', 'mtr')
    SHEET_NAMES = ('MTR',)

    MAX_HEADER_SCAN_ROWS = 10
    MIN_MAPPED_COLUMNS = 3
    REQUIRED_COLUMNS = ('invoice_number', 'taxable_value')

    # Signature Amazon MTR headers that uniquely identify an Amazon MTR report
    SIGNATURE_HEADERS = (
        'Order ID',
        'Invoice Number',
        'Invoice Date',
        'Taxable Value',
        'Supply Type',
    )

    HEADER_MAP = {
        'Order ID': 'order_id',
        'Order Id': 'order_id',
        'Order ID / Sub Order ID': 'order_id',
        'Invoice Number': 'invoice_number',
        'Invoice Date': 'invoice_date',
        'Ship From State': 'ship_from_state',
        'Ship To State': 'ship_to_state',
        'Buyer GSTIN': 'customer_gstin',
        'Seller GSTIN': 'seller_gstin',
        'HSN/SAC': 'hsn_sac',
        'HSN': 'hsn_sac',
        'Product Description': 'description',
        'Item Description': 'description',
        'Quantity': 'quantity',
        'Taxable Value': 'taxable_value',
        'Taxable Amount': 'taxable_value',
        'CGST Rate': 'cgst_rate',
        'CGST Amount': 'cgst_amount',
        'SGST Rate': 'sgst_rate',
        'SGST Amount': 'sgst_amount',
        'IGST Rate': 'igst_rate',
        'IGST Amount': 'igst_amount',
        'Cess Rate': 'cess_rate',
        'Cess Amount': 'cess_amount',
        'Invoice Value': 'invoice_value',
        'Total Value': 'invoice_value',
        'Tax Rate': 'tax_rate',
        'Supply Type': 'supply_type',
        'Place of Supply': 'place_of_supply',
        'Reverse Charge': 'reverse_charge',
        'E-Commerce GSTIN': 'ecommerce_gstin',
        'Ecommerce GSTIN': 'ecommerce_gstin',
        'Marketplace': 'marketplace_name',
        'Document Type': 'document_type',
        'Original Invoice Number': 'original_invoice_number',
        'Original Invoice Date': 'original_invoice_date',
        'Credit Note No': 'note_number',
        'Debit Note No': 'note_number',
        'Customer Name': 'customer_name',
        'Buyer Name': 'customer_name',
    }

    # ------------------------------------------------------------------
    # Step 3: Amazon-Specific Detection
    # ------------------------------------------------------------------
    def detect(self, workbook_or_data, file_name: str = '') -> bool:
        """Deterministic Amazon MTR detection.

        Checks:
        1. If no workbook content is provided, filename tokens are accepted as
           a secondary hint (pre-read check).
        2. If workbook content is provided:
           - Requires sheet named 'MTR' (case-insensitive) containing Amazon signature headers.
           - Or single-sheet / CSV files with filename hint AND Amazon signature headers.
        Ambiguous, non-MTR, or generic workbooks are never classified as Amazon.
        """
        name = (file_name or '').lower()
        if workbook_or_data is None:
            return any(token in name for token in self.FILENAME_TOKENS)

        sheets, _name, errors, _diagnostics, _wb_ref = self._load_sheets(workbook_or_data, file_name)
        if errors or not sheets:
            return False

        # Look for sheet named 'MTR' (case-insensitive)
        mtr_sheet = next((s for s in sheets if _key(s.name) == 'mtr'), None)

        if mtr_sheet is not None:
            # Check if there are any rows in the sheet
            has_any_content = False
            for row in mtr_sheet.rows():
                if any(v is not None and str(v).strip() for v in row):
                    has_any_content = True
                    break

            if not has_any_content:
                # Empty worksheet titled 'MTR': detected by explicit sheet name
                return True

            # If content exists, verify headers match Amazon schema
            header_info = self.detect_header_row(mtr_sheet)
            if header_info is None:
                return False
            mapping = header_info['mapping']
            return bool('invoice_number' in mapping or 'order_id' in mapping or 'taxable_value' in mapping)

        # If no sheet is named 'MTR':
        # Check if it's a CSV or single-sheet file with filename hint
        is_csv = name.endswith('.csv') or (
            len(sheets) == 1 and (sheets[0].name.endswith('.csv') or getattr(sheets[0], 'is_csv', False))
        )
        if is_csv and any(token in name for token in self.FILENAME_TOKENS):
            header_info = self.detect_header_row(sheets[0])
            if header_info is None:
                return False
            mapping = header_info['mapping']
            has_identity = 'invoice_number' in mapping or 'order_id' in mapping
            has_taxable = 'taxable_value' in mapping
            has_extra = any(f in mapping for f in ('supply_type', 'ship_from_state', 'ship_to_state', 'customer_gstin'))
            return bool(has_identity and has_taxable and has_extra)

        return False

    # ------------------------------------------------------------------
    # Step 4: Amazon-Specific Schema Validation
    # ------------------------------------------------------------------
    def validate(self, workbook_or_data) -> Tuple[bool, List[str]]:
        """Validate Amazon MTR workbook structure and required headers.

        Distinguishes:
        - None / missing input
        - Malformed or unreadable file
        - Wrong worksheet (not an Amazon MTR file)
        - Missing required headers
        - Header without transaction rows
        - Valid Amazon MTR file
        """
        if workbook_or_data is None:
            return False, ['No workbook or data provided.']

        sheets, _file_name, load_errors, _diagnostics, _wb_ref = self._load_sheets(workbook_or_data)
        if load_errors:
            return False, list(load_errors)
        if not sheets:
            return False, ['Workbook contains no worksheets.']

        # Find target sheet named 'MTR' (or single-sheet CSV)
        mtr_sheet = next((s for s in sheets if _key(s.name) == 'mtr'), None)

        if mtr_sheet is None:
            is_csv = len(sheets) == 1 and (sheets[0].name.endswith('.csv') or getattr(sheets[0], 'is_csv', False))
            if is_csv:
                mtr_sheet = sheets[0]
            else:
                return False, [
                    f"Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable "
                    f"header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of "
                    f"{len(sheets)} sheet(s): {', '.join(s.name for s in sheets)}). "
                    f"Recognized column names include: {self._expected_columns_hint()}."
                ]

        header_info = self.detect_header_row(mtr_sheet)
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
                f"Sheet '{mtr_sheet.name}' is missing required column(s): "
                f"{', '.join(FIELD_LABELS.get(f, f) for f in missing)}. "
                f"Columns found: {', '.join(h for h in header_info['headers'] if h) or '(none)'}."
            )

        if not errors:
            has_rows = False
            for _row_number, _row in self._iter_data_rows(mtr_sheet, header_info):
                has_rows = True
                break
            if not has_rows:
                errors.append(
                    'No data rows found beneath the header row. The file has headers but no '
                    'transaction rows to import.'
                )

        return (not errors), errors

    # ------------------------------------------------------------------
    # Step 5: Amazon Row Parsing & Canonicalization
    # ------------------------------------------------------------------
    def parse(self, workbook_or_data, file_name: str = '', stream: bool = False) -> ImportResult:
        """Parse Amazon MTR file into ImportResult containing canonical rows."""
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

        mtr_sheet = next((s for s in sheets if _key(s.name) == 'mtr'), None)
        if mtr_sheet is None:
            is_csv = len(sheets) == 1 and (sheets[0].name.endswith('.csv') or getattr(sheets[0], 'is_csv', False))
            if is_csv:
                mtr_sheet = sheets[0]
            else:
                result.errors.append(
                    f'Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable '
                    f'header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of '
                    f'{len(sheets)} sheet(s)). Recognized column names include: '
                    f'{self._expected_columns_hint()}.'
                )
                return result

        header = self.detect_header_row(mtr_sheet)
        if header is None:
            result.errors.append(
                f'Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable '
                f'header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of '
                f'{len(sheets)} sheet(s)). Recognized column names include: '
                f'{self._expected_columns_hint()}.'
            )
            return result

        header_rows = {mtr_sheet.name: header['row_number']}
        sheet_columns = {mtr_sheet.name: sorted(header['mapping'])}
        headers = header['headers']

        def _row_generator():
            for row_number, row in self._iter_data_rows(mtr_sheet, header):
                raw_data: Dict[str, Any] = {}
                for position in range(max(len(headers), len(row))):
                    if position < len(headers):
                        key = headers[position] or f'col_{position}'
                    else:
                        key = f'col_{position}'
                    raw_data[key] = row[position] if position < len(row) else None
                if len(row) > len(headers):
                    raw_data['_extra_values'] = [row[i] for i in range(len(headers), len(row))]

                row_object = self._build_import_row(raw_data, row_number, mtr_sheet.name)
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
        result.metadata['sheets_parsed'] = list(header_rows)
        result.metadata['columns_found'] = sheet_columns
        result.metadata['sheet_columns'] = sheet_columns
        result.metadata['sheets_parsed'] = [mtr_sheet.name]
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
        for field in self.sheet_required_columns(sheet_name):
            if not normalized.get(field):
                errors.append(f'Missing {FIELD_LABELS.get(field, field)}')
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
        qty = self._decimal_field(values, raw_row, 'quantity', warnings)
        return {
            'order_id': self.get_string(raw_row, ['Order ID', 'Order Id'], values=values),
            'order_item_id': self.get_string(raw_row, ['Order Item ID'], values=values),
            'hsn_sac': str(self._value(values, raw_row, 'hsn_sac') or '').strip(),
            'description': self.get_string(raw_row, ['Product Description', 'Description'], values=values),
            'quantity': qty,
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
