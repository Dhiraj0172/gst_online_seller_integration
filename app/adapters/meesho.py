"""Meesho marketplace production adapter.

Implements the PlatformAdapter contract specifically for Meesho orders reports:
- Deterministic Meesho detection via workbook structure, "Orders" sheet, and signature headers
- Explicit Meesho schema validation distinguishing invalid headers, wrong sheets, malformed files
- High-precision Decimal row parsing preserving Sub Order ID, product details, taxes, POS
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
from .all_adapters import MeeshoAdapter as BaseMeeshoAdapter
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus, PlatformAdapter
from app.adapters.canonical import CanonicalTransaction
from app.utils.date_utils import format_date_gst, parse_date
from app.utils.gstin_validator import validate_gstin
from app.utils.state_codes import STATE_CODES, resolve_pos_code


class MeeshoAdapter(BaseMeeshoAdapter):
    """Production adapter for Meesho orders reports."""

    PLATFORM_NAME = 'Meesho'
    SUPPORTED_FILE_TYPES = ['.xlsx', '.csv']
    INSTRUCTIONS = (
        'Meesho Supplier Panel > Orders > Download orders report. Sheet is '
        'named "Orders"; line identifier column is "Sub Order ID".'
    )
    FORMAT_DOCUMENTED = True
    FILENAME_TOKENS = ('meesho',)
    SHEET_NAMES = ('Orders',)

    MAX_HEADER_SCAN_ROWS = 10
    MIN_MAPPED_COLUMNS = 3
    REQUIRED_COLUMNS = ('invoice_number', 'taxable_value')

    # Signature Meesho headers that identify a Meesho orders report
    SIGNATURE_HEADERS = (
        'Sub Order ID',
        'Product Name',
        'Taxable Amount',
        'Total',
        'Selling Price',
        'Order ID',
        'Invoice Number',
        'HSN',
        'Supply Type',
        'Place of Supply',
        'Buyer State',
    )

    HEADER_MAP = {
        'Order ID': 'order_id',
        'Order Id': 'order_id',
        'Sub Order ID': 'sub_order_id',
        'Sub Order Id': 'sub_order_id',
        'Invoice Number': 'invoice_number',
        'Invoice Date': 'invoice_date',
        'Buyer GSTIN': 'customer_gstin',
        'Seller GSTIN': 'seller_gstin',
        'Ship From State': 'ship_from_state',
        'HSN': 'hsn_sac',
        'HSN Code': 'hsn_sac',
        'HSN/SAC': 'hsn_sac',
        'Product Name': 'description',
        'Product Description': 'description',
        'Item Description': 'description',
        'Selling Price': 'unit_price',
        'Quantity': 'quantity',
        'Taxable Amount': 'taxable_value',
        'Taxable Value': 'taxable_value',
        'Total': 'invoice_value',
        'Total Value': 'invoice_value',
        'Invoice Value': 'invoice_value',
        'CGST': 'cgst_amount',
        'SGST': 'sgst_amount',
        'IGST': 'igst_amount',
        'CGST Amount': 'cgst_amount',
        'SGST Amount': 'sgst_amount',
        'IGST Amount': 'igst_amount',
        'Cess Amount': 'cess_amount',
        'CGST Rate': 'cgst_rate',
        'SGST Rate': 'sgst_rate',
        'IGST Rate': 'igst_rate',
        'Cess Rate': 'cess_rate',
        'Tax Rate': 'tax_rate',
        'Buyer State': 'place_of_supply',
        'Place of Supply': 'place_of_supply',
        'Supply Type': 'supply_type',
        'Reverse Charge': 'reverse_charge',
        'E-Commerce GSTIN': 'ecommerce_gstin',
        'Ecommerce GSTIN': 'ecommerce_gstin',
        'Marketplace': 'marketplace_name',
        'Credit Note No': 'note_number',
        'Debit Note No': 'note_number',
        'Note Number': 'note_number',
        'Customer Name': 'customer_name',
        'Buyer Name': 'customer_name',
        'Original Invoice Number': 'original_invoice_number',
        'Original Invoice Date': 'original_invoice_date',
        'Document Type': 'document_type',
        'Order Status': 'order_status',
    }

    # ------------------------------------------------------------------
    # Step 2: Deterministic Meesho Detection
    # ------------------------------------------------------------------
    def detect(self, workbook_or_data, file_name: str = '') -> bool:
        """Deterministic Meesho orders report detection.

        Checks:
        1. If no workbook content is provided, filename tokens are accepted as
           a secondary hint (pre-read check).
        2. If workbook content is provided:
           - Requires sheet named 'Orders' (case-insensitive) containing Meesho signature headers.
           - Or single-sheet / CSV files with filename hint AND Meesho signature headers.
        Ambiguous, wrong-sheet, or other marketplace workbooks (e.g. Amazon 'MTR', Flipkart 'GST Report')
        are never classified as Meesho.
        """
        name = (file_name or '').lower()
        if workbook_or_data is None:
            return any(token in name for token in self.FILENAME_TOKENS)

        sheets, _name, errors, _diagnostics, _wb_ref = self._load_sheets(workbook_or_data, file_name)
        if errors or not sheets:
            return False

        # Look for sheet named 'Orders' (case-insensitive)
        target_sheet = next((s for s in sheets if _key(s.name) == 'orders'), None)

        if target_sheet is not None:
            header_info = self.detect_header_row(target_sheet)
            if header_info is None:
                return False
            mapping = header_info.get('mapping', {})
            raw_headers = set(str(h).strip().lower() for h in header_info.get('headers', []) if h)

            has_identity = 'sub_order_id' in mapping or 'invoice_number' in mapping or 'order_id' in mapping
            has_taxable = 'taxable_value' in mapping

            if not (has_identity and has_taxable):
                return False

            signature_candidates = [
                'sub order id',
                'product name',
                'taxable amount',
                'total',
                'selling price',
                'hsn',
                'supply type',
                'place of supply',
                'buyer state',
                'buyer gstin',
                'cgst',
                'sgst',
                'igst',
            ]
            matched_signatures = sum(1 for sig in signature_candidates if sig in raw_headers)
            has_meesho_specific = (
                'sub order id' in raw_headers
                or 'sub_order_id' in mapping
                or 'taxable amount' in raw_headers
                or 'product name' in raw_headers
            )

            return bool(matched_signatures >= 3 and has_meesho_specific)

        # If no sheet is named 'Orders':
        # Check if it's a CSV or single-sheet file with filename hint
        is_csv = name.endswith('.csv') or (
            len(sheets) == 1 and (sheets[0].name.endswith('.csv') or getattr(sheets[0], 'is_csv', False))
        )
        if is_csv and any(token in name for token in self.FILENAME_TOKENS):
            header_info = self.detect_header_row(sheets[0])
            if header_info is None:
                return False
            mapping = header_info.get('mapping', {})
            raw_headers = set(str(h).strip().lower() for h in header_info.get('headers', []) if h)

            has_identity = 'sub_order_id' in mapping or 'invoice_number' in mapping or 'order_id' in mapping
            has_taxable = 'taxable_value' in mapping

            signature_candidates = [
                'sub order id',
                'product name',
                'taxable amount',
                'total',
                'selling price',
                'hsn',
                'supply type',
                'place of supply',
                'buyer state',
                'buyer gstin',
                'cgst',
                'sgst',
                'igst',
            ]
            matched_signatures = sum(1 for sig in signature_candidates if sig in raw_headers)
            has_meesho_specific = (
                'sub order id' in raw_headers
                or 'sub_order_id' in mapping
                or 'taxable amount' in raw_headers
                or 'product name' in raw_headers
            )

            return bool(has_identity and has_taxable and matched_signatures >= 3 and has_meesho_specific)

        return False

    # ------------------------------------------------------------------
    # Step 3: Meesho Schema Validation
    # ------------------------------------------------------------------
    def validate(self, workbook_or_data) -> Tuple[bool, List[str]]:
        """Validate Meesho orders report structure and required headers.

        Distinguishes:
        - None / missing input
        - Malformed or unreadable file
        - Unrecognized file (no worksheet has recognizable header row)
        - Missing required headers (invoice_number, taxable_value)
        - Header without transaction rows
        - Valid Meesho orders report
        """
        if workbook_or_data is None:
            return False, ['No workbook or data provided.']

        sheets, _file_name, load_errors, _diagnostics, _wb_ref = self._load_sheets(workbook_or_data)
        if load_errors:
            return False, list(load_errors)
        if not sheets:
            return False, ['Workbook contains no worksheets.']

        usable, _ignored, _observed = self.sheet_plan(sheets)
        if not usable:
            return False, [
                f"Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable "
                f"header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of "
                f"{len(sheets)} sheet(s): {', '.join(s.name for s in sheets)}). "
                f"Recognized column names include: {self._expected_columns_hint()}."
            ]

        target_sheet, header_info = usable[0]

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
    # Step 4: Meesho Row Parsing & Canonicalization
    # ------------------------------------------------------------------
    def parse(self, workbook_or_data, file_name: str = '', stream: bool = False) -> ImportResult:
        """Parse Meesho orders report into ImportResult containing canonical rows."""
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

        usable, ignored, observed = self.sheet_plan(sheets)
        result.metadata['sheets_ignored'] = ignored
        if not usable:
            result.errors.append(
                f'Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable '
                f'header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of '
                f'{len(sheets)} sheet(s)). Recognized column names include: '
                f'{self._expected_columns_hint()}.'
            )
            return result

        target_sheet, header = usable[0]

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
        result.metadata['sheets_parsed'] = list(header_rows)
        result.metadata['columns_found'] = sheet_columns
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
    def _decimal_field(self, values: Optional[Dict[str, Any]], raw_row: Dict[str, Any],
                       field: str, warnings: Optional[List[str]] = None,
                       label: Optional[str] = None) -> Optional[Decimal]:
        raw_val = self._value(values, raw_row, field)
        if raw_val is None or (isinstance(raw_val, str) and not raw_val.strip()):
            return None
        dec = self._to_decimal(raw_val)
        if dec is None:
            if warnings is not None:
                lbl = label or FIELD_LABELS.get(field, field)
                warnings.append(f"{lbl} '{raw_val}' could not be parsed as a number")
            return None
        return dec

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
        sub_order_id = self.get_string(raw_row, ['Sub Order ID'], values=values)
        order_id = self.get_string(raw_row, ['Order ID', 'Order Id'], values=values)
        qty = self._decimal_field(values, raw_row, 'quantity', warnings, label='Quantity')
        unit_price = self._decimal_field(values, raw_row, 'unit_price', warnings, label='Unit price')
        return {
            'order_id': order_id,
            'sub_order_id': sub_order_id,
            'order_item_id': sub_order_id or order_id,
            'hsn_sac': str(self._value(values, raw_row, 'hsn_sac') or '').strip(),
            'description': self.get_string(raw_row, ['Product Name', 'Product Description', 'Description'], values=values),
            'quantity': qty,
            'unit_price': unit_price,
            'uqc': 'NOS',
        }

    def map_taxes(self, raw_row: Dict, values: Optional[Dict[str, Any]] = None,
                  warnings: Optional[List[str]] = None) -> Dict:
        return {
            'taxable_value': self._decimal_field(values, raw_row, 'taxable_value', warnings, label='Taxable value'),
            'cgst_rate': self._decimal_field(values, raw_row, 'cgst_rate', warnings, label='CGST rate'),
            'cgst_amount': self._decimal_field(values, raw_row, 'cgst_amount', warnings, label='CGST amount'),
            'sgst_rate': self._decimal_field(values, raw_row, 'sgst_rate', warnings, label='SGST rate'),
            'sgst_amount': self._decimal_field(values, raw_row, 'sgst_amount', warnings, label='SGST amount'),
            'igst_rate': self._decimal_field(values, raw_row, 'igst_rate', warnings, label='IGST rate'),
            'igst_amount': self._decimal_field(values, raw_row, 'igst_amount', warnings, label='IGST amount'),
            'cess_rate': self._decimal_field(values, raw_row, 'cess_rate', warnings, label='Cess rate'),
            'cess_amount': self._decimal_field(values, raw_row, 'cess_amount', warnings, label='Cess amount'),
            'tax_rate': self._decimal_field(values, raw_row, 'tax_rate', warnings, label='Tax rate'),
            'invoice_value': self._decimal_field(values, raw_row, 'invoice_value', warnings, label='Invoice value'),
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

        if 'credit' in lowered or lowered in ('cr', 'c') or inv_num.startswith(('CN-', 'CRN-')):
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

        # Validate required invoice_number
        raw_inv = normalized.get('invoice_number')
        if raw_inv is None or not str(raw_inv).strip():
            if 'Missing Invoice number' not in errors:
                errors.append('Missing Invoice number')

        # Validate required taxable_value
        raw_taxable = self._value(values, raw_row, 'taxable_value')
        if raw_taxable is None or (isinstance(raw_taxable, str) and not raw_taxable.strip()):
            if 'Missing Taxable value' not in errors:
                errors.append('Missing Taxable value')
        elif self._to_decimal(raw_taxable) is None:
            err_msg = f"Taxable value '{raw_taxable}' is not a valid numeric amount"
            if err_msg not in errors:
                errors.append(err_msg)

        # Check for malformed other non-empty numeric fields
        for field, label in (
            ('invoice_value', 'Invoice value'),
            ('quantity', 'Quantity'),
            ('cgst_amount', 'CGST amount'),
            ('sgst_amount', 'SGST amount'),
            ('igst_amount', 'IGST amount'),
            ('cess_amount', 'Cess amount'),
            ('tax_rate', 'Tax rate'),
            ('cgst_rate', 'CGST rate'),
            ('sgst_rate', 'SGST rate'),
            ('igst_rate', 'IGST rate'),
            ('unit_price', 'Unit price'),
        ):
            raw_val = self._value(values, raw_row, field)
            if raw_val is not None and (not isinstance(raw_val, str) or raw_val.strip()):
                if self._to_decimal(raw_val) is None:
                    err_msg = f"{label} '{raw_val}' is not a valid numeric amount"
                    if err_msg not in errors:
                        errors.append(err_msg)
