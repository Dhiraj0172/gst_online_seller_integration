"""GSTR-1 Government Excel production adapter.

Implements the PlatformAdapter contract specifically for GSTR-1 workbooks
produced by the GST portal returns offline tool or exported by this project.

Features:
- Multi-sheet parsing across all 15 official transaction/amendment/aggregate sheets
- Non-transaction sheet skipping and reporting (docs)
- Documented official government header validation per sheet
- Explicit unknown-sheet and malformed-row handling
- Preservation of aggregate sheet records (b2cs, b2csa, nil, hsn, hsnb2c) without fake invoice numbers
- Full amendment sheet support (b2ba, b2cla, b2csa, cdnra, cdnura, expa)
- Credit/Debit note handling with original invoice linking (cdnr, cdnra, cdnur, cdnura)
- High-precision Decimal parsing and tax component derivation
- Source sheet and row preservation on every record
"""
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple
import openpyxl

from app.adapters.all_adapters import (
    CANONICAL_FIELDS,
    FIELD_LABELS,
    BaseGenericAdapter,
    SheetSource,
    _key,
    _platform_index,
    _ALIAS_INDEX,
)
from .all_adapters import GSTR1GovtAdapter as BaseGSTR1GovtAdapter
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus, PlatformAdapter
from app.adapters.canonical import CanonicalTransaction
from app.utils.date_utils import format_date_gst, parse_date
from app.utils.gstin_validator import validate_gstin
from app.utils.state_codes import STATE_CODES, resolve_pos_code


class GSTR1GovtAdapter(BaseGSTR1GovtAdapter):
    """Production adapter for official GSTR-1 multi-sheet workbooks."""

    PLATFORM_NAME = 'GSTR1_Govt'
    SUPPORTED_FILE_TYPES = ['.xlsx']
    INSTRUCTIONS = (
        'Official GSTR-1 workbook produced by the GST portal offline tool. '
        'Supported sheets: b2b, b2ba, b2cl, b2cla, b2cs, b2csa, cdnr, cdnra, '
        'cdnur, cdnura, exp, expa, nil, hsn, hsnb2c, docs.'
    )
    FORMAT_DOCUMENTED = True
    FILENAME_TOKENS = ('gstr1', 'gstr-1', 'gstr_1')

    SHEET_NAMES = (
        'b2b', 'b2ba', 'b2cl', 'b2cla', 'b2cs', 'b2csa',
        'cdnr', 'cdnra', 'cdnur', 'cdnura', 'exp', 'expa',
        'nil', 'hsn', 'hsnb2c'
    )
    ALL_KNOWN_SHEETS = SHEET_NAMES + ('docs',)
    NON_TRANSACTION_SHEETS = ('docs',)
    CONSOLIDATED_SHEETS = ('b2cs', 'b2csa', 'nil', 'hsn', 'hsnb2c')
    AMENDMENT_SHEETS = ('b2ba', 'b2cla', 'b2csa', 'cdnra', 'cdnura', 'expa')
    NOTE_SHEETS = ('cdnr', 'cdnra', 'cdnur', 'cdnura')

    MULTI_SHEET = True
    REQUIRE_ANY_SHEET = True
    MAX_HEADER_SCAN_ROWS = 10
    REQUIRED_COLUMNS = ('taxable_value',)

    # Canonical mapping covering all 16 official government sheet configurations
    HEADER_MAP = {
        # Recipient
        'GSTIN/UIN of Recipient': 'customer_gstin',

        # Document Identifiers
        'Invoice Number': 'invoice_number',
        'Invoice date': 'invoice_date',
        'Invoice Date': 'invoice_date',
        'Invoice Value': 'invoice_value',

        # Amendment Invoices
        'Original Invoice Number': 'original_invoice_number',
        'Original Invoice date': 'original_invoice_date',
        'Original Invoice Date': 'original_invoice_date',
        'Revised Invoice Number': 'invoice_number',
        'Revised Invoice date': 'invoice_date',
        'Revised Invoice Date': 'invoice_date',

        # Credit / Debit Notes
        'Note/Refund Voucher Number': 'note_number',
        'Note/Refund Voucher date': 'note_date',
        'Note/Refund Voucher Date': 'note_date',
        'Note/Refund Voucher Value': 'invoice_value',
        'Invoice/Advance Receipt Number': 'original_invoice_number',
        'Invoice/Advance Receipt date': 'original_invoice_date',
        'Invoice/Advance Receipt Date': 'original_invoice_date',
        'Note Supply Type': 'supply_type',
        'Note Type': 'note_type',

        # Amendment Notes (cdnra, cdnura)
        'Original Note Number': 'original_invoice_number',
        'Original Note Date': 'original_invoice_date',
        'Revised Note Number': 'note_number',
        'Revised Note Date': 'note_date',
        'Note Value': 'invoice_value',

        # Tax components & supply attributes
        'Place Of Supply': 'place_of_supply',
        'Reverse Charge': 'reverse_charge',
        'Applicable % of Tax Rate': 'applicable_tax_rate_pct',
        'Invoice Type': 'invoice_type',
        'E-Commerce GSTIN': 'ecommerce_gstin',
        'Rate': 'tax_rate',
        'Taxable Value': 'taxable_value',
        'Cess Amount': 'cess_amount',
        'Integrated Tax Amount': 'igst_amount',
        'Central Tax Amount': 'cgst_amount',
        'State/UT Tax Amount': 'sgst_amount',

        # Exports
        'Export Type': 'export_type',
        'Port Code': 'port_code',
        'Shipping Bill Number': 'shipping_bill_number',
        'Shipping Bill Date': 'shipping_bill_date',

        # B2CS / B2CSA
        'Type': 'b2cs_type',

        # Nil / Exempt / Non-GST
        'Supply Type': 'supply_type',
        'Nil Rated Supplies': 'nil_rated_supplies',
        'Exempted (other than nil rated/non GST supply)': 'exempt_supplies',
        'Non-GST supplies': 'non_gst_supplies',

        # HSN / HSNB2C
        'HSN': 'hsn_sac',
        'Description': 'description',
        'UQC': 'uqc',
        'Total Quantity': 'quantity',
        'Total Value': 'invoice_value',

        # Docs issued
        'Nature of Document': 'nature_of_document',
        'Sr. No. From': 'doc_from',
        'Sr. No. To': 'doc_to',
        'Total Number': 'doc_total',
        'Cancelled': 'doc_cancelled',
    }

    # ------------------------------------------------------------------
    # Step 1: Detection
    # ------------------------------------------------------------------
    def detect(self, workbook_or_data, file_name: str = '') -> bool:
        """Deterministic GSTR-1 offline tool workbook detection."""
        name = (file_name or '').lower()
        if any(token in name for token in self.FILENAME_TOKENS):
            return True

        sheets, _name, errors, _diag, _wb = self._load_sheets(workbook_or_data, file_name)
        if errors or not sheets:
            return False

        known_keys = {_key(sheet_name) for sheet_name in self.ALL_KNOWN_SHEETS}
        present_known = [s for s in sheets if _key(s.name) in known_keys]
        if not present_known:
            return False

        # If any recognized sheet has a valid GSTR-1 header row, claim it
        for sheet in present_known:
            header = self.detect_header_row(sheet)
            if header is not None and header.get('score', 0) >= 1:
                return True

        return False

    # ------------------------------------------------------------------
    # Step 2: Validation
    # ------------------------------------------------------------------
    def validate(self, workbook_or_data) -> Tuple[bool, List[str]]:
        """Validate multi-sheet GSTR-1 workbook structure."""
        sheets, _file_name, load_errors, _diag = self._load_sheets(workbook_or_data)
        if load_errors:
            return False, list(load_errors)
        if not sheets:
            return False, ['Workbook contains no worksheets.']

        known_keys = {_key(n) for n in self.ALL_KNOWN_SHEETS}
        present_known = [s for s in sheets if _key(s.name) in known_keys]
        if not present_known:
            return False, [
                f'Unrecognized {self.PLATFORM_NAME} file: no recognized GSTR-1 sheet found '
                f'(scanned sheets: {", ".join(s.name for s in sheets)}). '
                f'Expected official sheets: {", ".join(self.ALL_KNOWN_SHEETS)}.'
            ]

        # Check usable sheets
        usable, ignored, observed = self.sheet_plan(sheets)
        if not usable:
            # If known sheets exist but failed header detection, report explicit header failure
            failed_known = [s.name for s in present_known if s.name in ignored]
            return False, [
                f'Unrecognized {self.PLATFORM_NAME} file: no worksheet has a recognizable '
                f'header row (scanned the first {self.MAX_HEADER_SCAN_ROWS} rows of '
                f'{len(sheets)} sheet(s): {", ".join(s.name for s in sheets)}). '
                f'Recognized column names include: {self._expected_columns_hint()}.'
            ]

        errors: List[str] = []
        # Check required columns per sheet
        for sheet, header in usable:
            reqs = self.sheet_required_columns(sheet.name)
            missing = [f for f in reqs if f not in header['mapping']]
            if missing:
                errors.append(
                    f"Sheet '{sheet.name}' is missing required column(s): "
                    f"{', '.join(FIELD_LABELS.get(f, f) for f in missing)}."
                )

        if not errors:
            has_rows = False
            for sheet, header in usable:
                for _rn, _row in self._iter_data_rows(sheet, header):
                    has_rows = True
                    break
                if has_rows:
                    break
            if not has_rows:
                errors.append(
                    'No data rows found beneath the header row. The file has headers but no '
                    'transaction rows to import.'
                )

        return (not errors), errors

    # ------------------------------------------------------------------
    # Step 3: Required columns per sheet kind
    # ------------------------------------------------------------------
    def sheet_required_columns(self, sheet_name: str) -> Tuple[str, ...]:
        key = _key(sheet_name)
        if key == 'nil':
            return ()
        if key in {_key(n) for n in self.CONSOLIDATED_SHEETS}:
            return ('taxable_value',)
        if key in {_key(n) for n in self.NOTE_SHEETS}:
            return ('note_number', 'taxable_value')
        return ('invoice_number', 'taxable_value')

    # ------------------------------------------------------------------
    # Step 4: Normalization & Post-normalization
    # ------------------------------------------------------------------
    def _bucket_value(self, values, raw_row, field, header):
        if values and values.get(field) is not None:
            return values[field]
        return raw_row.get(header)

    def post_normalize(self, raw_row, normalized, values, warnings):
        """Enforce canonical contract, amendment semantics, and aggregate preservation."""
        normalized['marketplace_name'] = self.PLATFORM_NAME
        normalized['source_platform'] = self.PLATFORM_NAME

        sheet_name = str(raw_row.get('_sheet_name') or normalized.get('source_sheet') or '').strip().lower()
        if sheet_name:
            normalized['source_sheet'] = sheet_name

        sheet_supply_map = {
            'b2b': 'B2B',
            'b2ba': 'B2BA',
            'b2cl': 'B2CL',
            'b2cla': 'B2CLA',
            'b2cs': 'B2CS',
            'b2csa': 'B2CSA',
            'cdnr': 'CDNR',
            'cdnra': 'CDNRA',
            'cdnur': 'CDNUR',
            'cdnura': 'CDNURA',
            'exp': 'EXPORT',
            'expa': 'EXPA',
            'hsn': 'HSN',
            'hsnb2c': 'HSNB2C',
        }
        if sheet_name in sheet_supply_map:
            normalized['supply_type'] = sheet_supply_map[sheet_name]
            normalized['gstr1_table'] = sheet_name

        # --- Amendment sheet identification ---
        if sheet_name in self.AMENDMENT_SHEETS or normalized.get('amendment_flag'):
            normalized['amendment_flag'] = True
            # Revised invoice / note number is the current document number
            rev_inv = raw_row.get('Revised Invoice Number') or raw_row.get('Revised Invoice number')
            if rev_inv and not normalized.get('invoice_number'):
                normalized['invoice_number'] = str(rev_inv)
            rev_note = raw_row.get('Revised Note Number')
            if rev_note and not normalized.get('note_number'):
                normalized['note_number'] = str(rev_note)
            orig_inv = raw_row.get('Original Invoice Number') or raw_row.get('Original Note Number')
            if orig_inv and not normalized.get('original_invoice_number'):
                normalized['original_invoice_number'] = str(orig_inv)

        # --- Credit / Debit Note identification ---
        if sheet_name in self.NOTE_SHEETS or normalized.get('note_number'):
            note_num = normalized.get('note_number') or raw_row.get('Note/Refund Voucher Number') or raw_row.get('Revised Note Number')
            if note_num:
                normalized['note_number'] = str(note_num)
                if not normalized.get('invoice_number'):
                    normalized['invoice_number'] = str(note_num)
                    warnings.append('Document number taken from the note/refund voucher number column')

            raw_nt = str(raw_row.get('Note Type') or normalized.get('note_type') or '').strip().upper()
            if 'DEBIT' in raw_nt or raw_nt == 'D':
                normalized['note_type'] = 'DEBIT'
            elif 'CREDIT' in raw_nt or raw_nt == 'C':
                normalized['note_type'] = 'CREDIT'
            elif not normalized.get('note_type'):
                normalized['note_type'] = 'CREDIT'

        # --- Consolidated / Aggregate sheet identification ---
        if sheet_name in self.CONSOLIDATED_SHEETS:
            normalized['is_aggregate'] = True
            if not normalized.get('invoice_number'):
                warnings.append(
                    'Consolidated GSTR-1 row: no document number in this sheet layout (aggregate row)'
                )

            # Specific per-sheet handling
            if sheet_name in ('b2cs', 'b2csa'):
                normalized['supply_type'] = 'B2CSA' if sheet_name == 'b2csa' else 'B2CS'
                normalized['gstr1_table'] = 'b2csa' if sheet_name == 'b2csa' else 'b2cs'

                # Derive missing tax breakdown if needed from POS & tax rate
                txval = self._to_decimal(normalized.get('taxable_value')) or Decimal('0')
                rate = self._to_decimal(normalized.get('tax_rate')) or Decimal('0')
                cess = self._to_decimal(normalized.get('cess_amount')) or Decimal('0')
                pos = str(normalized.get('place_of_supply') or '').strip()

                # If individual components are missing, calculate them
                cgst = self._to_decimal(normalized.get('cgst_amount'))
                sgst = self._to_decimal(normalized.get('sgst_amount'))
                igst = self._to_decimal(normalized.get('igst_amount'))

                if cgst is None and sgst is None and igst is None and rate > 0 and txval > 0:
                    # Seller state is inferred from POS or profile in import_service;
                    # provide standard calculation:
                    # if POS is provided, determine if igst or cgst/sgst
                    # default fallback: if pos and pos != '27' (or inter), igst:
                    # In adapter, store full tax rate amount in total_tax
                    calculated_tax = (txval * rate) / Decimal('100')
                    normalized['total_tax'] = calculated_tax + cess
                    normalized['invoice_value'] = txval + calculated_tax + cess

            elif sheet_name == 'nil':
                # Consolidated nil/exempt/non-GST rows carry three separate buckets
                buckets = [
                    ('NIL', self._bucket_value(values, raw_row, 'nil_rated_supplies', 'Nil Rated Supplies')),
                    ('EXEMPT', self._bucket_value(values, raw_row, 'exempt_supplies', 'Exempted (other than nil rated/non GST supply)')),
                    ('NONGST', self._bucket_value(values, raw_row, 'non_gst_supplies', 'Non-GST supplies')),
                ]
                present = [
                    (item_type, self._to_decimal(amount) or Decimal('0'))
                    for item_type, amount in buckets
                    if amount not in (None, '')
                ]
                non_zero = [(item_type, amount) for item_type, amount in present if amount != 0]

                raw_sply_text = str(raw_row.get('Supply Type') or '').strip()
                sply_upper = raw_sply_text.upper()
                nil_sply_ty = "INTRAB2C"  # default
                if 'INTER' in sply_upper and ('REGISTERED' in sply_upper and 'UNREGISTERED' not in sply_upper):
                    nil_sply_ty = "INTRB2B"
                elif 'INTRA' in sply_upper and ('REGISTERED' in sply_upper and 'UNREGISTERED' not in sply_upper):
                    nil_sply_ty = "INTRAB2B"
                elif 'INTER' in sply_upper:
                    nil_sply_ty = "INTRB2C"
                elif 'INTRA' in sply_upper:
                    nil_sply_ty = "INTRAB2C"

                normalized['nil_sply_ty'] = nil_sply_ty
                normalized['uqc'] = nil_sply_ty
                normalized['source_metadata'] = dict(
                    normalized.get('source_metadata', {}),
                    nil_sply_ty=nil_sply_ty,
                    nil_rated_supplies=str(self._bucket_value(values, raw_row, 'nil_rated_supplies', 'Nil Rated Supplies') or '0'),
                    exempt_supplies=str(self._bucket_value(values, raw_row, 'exempt_supplies', 'Exempted (other than nil rated/non GST supply)') or '0'),
                    non_gst_supplies=str(self._bucket_value(values, raw_row, 'non_gst_supplies', 'Non-GST supplies') or '0')
                )

                if non_zero:
                    item_type, amount = max(non_zero, key=lambda item: item[1])
                    normalized['taxable_value'] = amount
                    normalized['item_type'] = item_type
                    normalized['supply_type'] = item_type
                    normalized['gstr1_table'] = 'nil'
                    warnings.append(
                        f'Consolidated GSTR-1 row: {item_type} bucket used as the taxable value'
                    )
                elif present:
                    normalized['taxable_value'] = Decimal('0')
                    normalized['item_type'] = 'NIL'
                    normalized['supply_type'] = 'NIL'
                    normalized['gstr1_table'] = 'nil'

            elif sheet_name in ('hsn', 'hsnb2c'):
                normalized['supply_type'] = 'HSNB2C' if sheet_name == 'hsnb2c' else 'HSN'
                normalized['gstr1_table'] = 'hsnb2c' if sheet_name == 'hsnb2c' else 'hsn'
                normalized['hsn_sac'] = str(raw_row.get('HSN') or normalized.get('hsn_sac') or '')
                normalized['description'] = str(raw_row.get('Description') or normalized.get('description') or '')
                normalized['uqc'] = str(raw_row.get('UQC') or normalized.get('uqc') or 'NOS')
                normalized['quantity'] = self._to_decimal(raw_row.get('Total Quantity') or normalized.get('quantity')) or Decimal('0')
                normalized['taxable_value'] = self._to_decimal(raw_row.get('Taxable Value') or normalized.get('taxable_value')) or Decimal('0')
                normalized['invoice_value'] = self._to_decimal(raw_row.get('Total Value') or normalized.get('invoice_value')) or normalized['taxable_value']
                normalized['tax_rate'] = self._to_decimal(raw_row.get('Rate') or normalized.get('tax_rate')) or Decimal('0')

        super().post_normalize(raw_row, normalized, values, warnings)

    def parse(self, workbook_or_data, file_name: str = '', stream: bool = False) -> ImportResult:
        result = super().parse(workbook_or_data, file_name, stream=stream)
        # Parse non-transaction docs sheet if present
        sheets, _name, _errs, _diag, _wb_ref = self._load_sheets(workbook_or_data, file_name)
        for s in sheets:
            if _key(s.name) == 'docs':
                doc_rows = []
                for row_idx, row in enumerate(s.rows(), start=1):
                    if row_idx == 1:
                        doc_headers = [str(c).strip() if c is not None else '' for c in row]
                        continue
                    if not row or not any(row):
                        continue
                    row_dict = dict(zip(doc_headers, row))
                    if any(v is not None and str(v).strip() for v in row_dict.values()):
                        doc_rows.append(row_dict)
                result.metadata['docs_summary'] = doc_rows
                break
        return result
