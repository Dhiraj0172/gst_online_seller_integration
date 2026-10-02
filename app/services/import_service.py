"""Common marketplace import service and orchestration pipeline.

This module provides the central, authoritative import pipeline used by all
marketplace adapters:

  raw file
  -> adapter detection / resolution
  -> file validation / structure recognition
  -> duplicate file check
  -> adapter parsing
  -> canonical transaction representation
  -> normalization & supply classification
  -> duplicate transaction detection (in-file & database)
  -> batched persistence (HIGH-02 relationship-based linkage)
  -> ImportHistory & result reporting

Guarantees:
- Relationship-based RawImport linkage (Transaction.raw_import = raw_import)
- Batched database flushes without per-row queries
- Cross-import and in-file duplicate detection (never double-counted)
- Complete rollback on failure (zero orphaned transactions/raw imports)
- Strict multi-tenant / profile scoping
- Safe error disclosure (MED-01)
"""
import hashlib
import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import openpyxl

from app.adapters.all_adapters import BaseGenericAdapter
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus
from app.adapters.canonical import CanonicalTransaction
from app.adapters.registry import detect_platform, get_adapter
from app.extensions import db
from app.models import GSTProfile, ImportHistory, RawImport, Transaction
from app.services.classification_service import classify_transaction
from app.services.duplicate_service import (
    DUPLICATE_EXISTING,
    DUPLICATE_IN_FILE,
    build_fingerprint,
    duplicate_envelope,
    duplicate_report_entry,
    file_duplicate_report,
    find_existing_transactions,
    find_prior_file_import,
)
from app.utils.csv_utils import read_csv_rows
from app.utils.date_utils import parse_date
from app.utils.state_codes import resolve_pos_code

# Re-export for compatibility with tcs_service and other modules
__all__ = [
    'ImportProcessingResult',
    'validate_file',
    'read_chunks',
    'process_import',
    'reprocess_import',
    '_create_transaction_from_normalized',
    'ImportResult',
    'ImportRow',
    'ImportRowStatus',
]


class ImportProcessingResult:
    """Detailed summary of the import pipeline execution."""

    def __init__(self):
        self.total_rows: int = 0
        self.success_rows: int = 0
        self.warning_rows: int = 0
        self.error_rows: int = 0
        self.skipped_rows: int = 0
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.validation_errors: List[str] = []
        self.stats: Dict[str, Any] = {}
        self.import_history_id: Optional[int] = None
        self.status: str = 'PENDING'
        self.error_summary: Optional[str] = None
        self.warning_summary: Optional[str] = None
        self.flash_messages: List[Tuple[str, str]] = []


def validate_file(file_path: str) -> bool:
    """Verify that the uploaded file has a supported extension."""
    if not file_path.lower().endswith(('.xlsx', '.csv', '.xls')):
        raise ValueError("Invalid file format. Please upload an Excel (.xlsx/.xls) or CSV file.")
    return True


def _first_parsed_sheet(parse_result: ImportResult) -> str:
    """Return the primary sheet title parsed from the file."""
    sheets = parse_result.metadata.get('sheets_parsed') or []
    return sheets[0] if sheets else 'Sheet1'


def _safe_decimal(val: Any, default: Decimal = Decimal('0')) -> Decimal:
    """Safely convert any value to Decimal, falling back to default."""
    if val is None or val == '':
        return default
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except Exception:
        return default


def read_chunks(file_path: str, chunk_size: int = 1000) -> Iterator[List[Dict[str, Any]]]:
    """Read an Excel or CSV file in chunks, yielding lists of row dicts."""
    if file_path.lower().endswith('.csv'):
        yield from _read_csv_chunks(file_path, chunk_size)
        return

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active

    headers = None
    chunk = []

    for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if not any(row):
            continue

        if headers is None:
            headers = [str(c).strip() if c else f"col_{i}" for i, c in enumerate(row)]
            continue

        raw_data = dict(zip(headers, row))
        if not any(v for v in raw_data.values() if v is not None and str(v).strip()):
            continue

        raw_data['_row_number'] = row_idx
        raw_data['_sheet_name'] = ws.title
        chunk.append(raw_data)

        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []

    if chunk:
        yield chunk

    wb.close()


def _read_csv_chunks(file_path: str, chunk_size: int = 1000) -> Iterator[List[Dict[str, Any]]]:
    """Chunked reader for CSV sources."""
    csv_result = read_csv_rows(file_path)
    if csv_result.errors:
        raise ValueError('; '.join(csv_result.errors))

    rows = csv_result.rows
    if not rows:
        return
    headers = [str(cell).strip() if str(cell).strip() else f'col_{i}'
               for i, cell in enumerate(rows[0])]
    sheet_name = os.path.basename(file_path)

    chunk: List[Dict[str, Any]] = []
    for row_idx, row in enumerate(rows[1:], start=2):
        raw_data: Dict[str, Any] = {}
        for position in range(max(len(headers), len(row))):
            key = headers[position] if position < len(headers) else f'col_{position}'
            raw_data[key] = row[position] if position < len(row) else None
        if not any(value is not None and str(value).strip() for value in raw_data.values()):
            continue
        raw_data['_row_number'] = row_idx
        raw_data['_sheet_name'] = sheet_name
        chunk.append(raw_data)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def process_import(
    file_path: str,
    profile_id: int,
    platform_name: str,
    user_id: int,
    return_period: str,
    financial_year: str,
    allow_duplicate_file: bool = False,
    classification_fn: Optional[Callable] = None,
    import_history_id: Optional[int] = None,
) -> ImportProcessingResult:
    """Execute the canonical import pipeline for any marketplace report.

    Phases:
    1. Adapter detection / resolution
    2. File format & structure validation
    3. Duplicate file detection
    4. Adapter parsing into canonical transactions
    5. Batch duplicate detection (in-file and cross-import)
    6. Batched persistence (HIGH-02 relationship-based linkage)
    7. ImportHistory recording and status reporting
    """
    result = ImportProcessingResult()
    start_time = datetime.utcnow()
    wb = None

    # Step 1: Ensure ImportHistory record exists
    if import_history_id is not None:
        import_history = db.session.get(ImportHistory, import_history_id)
        if not import_history:
            raise ValueError(f"ImportHistory record {import_history_id} not found.")
        file_hash = import_history.file_hash
        file_size = import_history.file_size
    else:
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(65536), b""):
                sha256_hash.update(byte_block)
        file_hash = sha256_hash.hexdigest()
        file_size = os.path.getsize(file_path)

        import_history = ImportHistory(
            user_id=user_id,
            profile_id=profile_id,
            file_name=os.path.basename(file_path),
            original_file_name=os.path.basename(file_path),
            file_hash=file_hash,
            file_size=file_size,
            platform_name=platform_name or 'Generic',
            return_period=return_period,
            financial_year=financial_year,
            processing_status='PROCESSING',
            processing_started_at=start_time,
            raw_file_path=file_path,
        )
        db.session.add(import_history)
        db.session.commit()

    result.import_history_id = import_history.id

    try:
        # Step 2: Adapter detection & file handle loading
        filename = os.path.basename(file_path)
        is_csv = filename.lower().endswith('.csv')
        adapter = get_adapter(platform_name) if platform_name and platform_name != 'Generic' else None
        source = file_path

        if not is_csv:
            try:
                wb = openpyxl.load_workbook(file_path, data_only=True)
                source = wb
            except Exception as exc:
                import_history.processing_status = 'FAILED'
                import_history.error_summary = json.dumps({
                    'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                               'error_rows': 0, 'skipped_rows': 0},
                    'errors': [{'code': 'UNREADABLE_SOURCE',
                                'message': f'Unsupported or unreadable file: {exc}'}],
                })
                import_history.processing_completed_at = datetime.utcnow()
                db.session.commit()
                result.status = 'REJECTED_UNREADABLE'
                result.stats['status'] = 'FAILED'
                result.errors.append('Unsupported or unreadable file. Upload an Excel (.xlsx/.xls) or CSV marketplace export.')
                return result

        if not adapter:
            adapter = detect_platform(source, filename)
        if not adapter:
            adapter = BaseGenericAdapter()

        # Step 3: CSV Readability Check
        if is_csv:
            csv_check = read_csv_rows(file_path)
            if csv_check.errors:
                import_history.processing_status = 'FAILED'
                import_history.error_summary = json.dumps({
                    'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                               'error_rows': 0, 'skipped_rows': 0},
                    'errors': [{'code': 'UNREADABLE_SOURCE', 'message': csv_check.errors[0]}],
                })
                import_history.processing_completed_at = datetime.utcnow()
                db.session.commit()
                result.status = 'REJECTED_UNREADABLE'
                result.stats['status'] = 'FAILED'
                result.errors.append(csv_check.errors[0])
                return result

        # Step 4: Validate File Structure / Required Headers
        is_valid, validation_errors = adapter.validate(source)
        if not is_valid:
            import_history.processing_status = 'FAILED'
            import_history.error_summary = json.dumps({
                'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                           'error_rows': 0, 'skipped_rows': 0},
                'errors': [{'code': 'INVALID_FILE', 'message': message}
                           for message in validation_errors],
            })
            import_history.processing_completed_at = datetime.utcnow()
            db.session.commit()
            result.status = 'REJECTED_INVALID'
            result.stats['status'] = 'FAILED'
            result.validation_errors = list(validation_errors)
            result.errors = list(validation_errors)
            return result

        # Step 5: Duplicate File Detection
        prior_import = find_prior_file_import(profile_id, file_hash, platform_name)
        if prior_import is not None and not allow_duplicate_file:
            report = file_duplicate_report(prior_import)
            import_history.processing_status = 'FAILED'
            import_history.error_summary = json.dumps({
                'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                           'error_rows': 0, 'skipped_rows': 0},
                'errors': [report],
            })
            import_history.processing_completed_at = datetime.utcnow()
            db.session.commit()
            result.status = 'REJECTED_DUPLICATE_FILE'
            result.stats['status'] = 'FAILED'
            result.errors.append(report['message'])
            return result

        # Step 6: Parse File using Adapter
        parse_result = adapter.parse(source, filename)
        if parse_result.errors:
            import_history.processing_status = 'FAILED'
            import_history.error_summary = json.dumps({
                'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                           'error_rows': 0, 'skipped_rows': 0},
                'errors': [{'code': 'UNREADABLE_SOURCE', 'message': message}
                           for message in parse_result.errors],
            })
            import_history.processing_completed_at = datetime.utcnow()
            db.session.commit()
            result.status = 'REJECTED_UNREADABLE'
            result.stats['status'] = 'FAILED'
            result.errors.extend(parse_result.errors)
            return result

        # Step 7: Batch duplicate check against database
        candidate_fingerprints = [
            build_fingerprint(r.normalized_data or {}, profile_id, platform_name)
            for r in parse_result.rows
        ]
        existing_by_fp = find_existing_transactions(profile_id, candidate_fingerprints) if candidate_fingerprints else {}

        profile = db.session.get(GSTProfile, profile_id)
        seller_gstin = profile.gstin if profile else ''
        seller_state = profile.state_code if profile else ''

        total_rows = 0
        success_rows = 0
        error_rows = 0
        warning_rows = 0
        skipped_rows = 0
        rejections = []
        duplicates = []
        warning_report = []
        seen_in_file: Dict[str, int] = {}

        # GSTR-1 Table mappings
        gstr1_table_map = {
            'B2B': 'b2b',
            'B2BA': 'b2ba',
            'B2CS': 'b2cs',
            'B2CSA': 'b2csa',
            'B2CL': 'b2cl',
            'B2CLA': 'b2cla',
            'CDNR': 'cdnr',
            'CDNRA': 'cdnra',
            'CDNUR': 'cdnur',
            'CDNURA': 'cdnura',
            'NIL': 'nil',
            'EXEMPT': 'nil',
            'NONGST': 'nil',
            'EXPORT': 'exp',
            'EXPA': 'expa',
            'SEZ': 'exp',
            'HSN': 'hsn',
            'HSNB2C': 'hsnb2c',
            'UNKNOWN': 'unknown'
        }

        # Step 8: Row-by-row normalization, duplicate marking, and batched persistence
        for row in parse_result.rows:
            total_rows += 1
            norm = row.normalized_data or {}
            fingerprint = build_fingerprint(norm, profile_id, platform_name)

            # In-file and existing duplicate detection
            duplicate_entry = None
            if fingerprint in seen_in_file:
                first_row = seen_in_file[fingerprint]
                duplicate_entry = duplicate_report_entry(
                    row.row_number, DUPLICATE_IN_FILE,
                    f'Duplicate of row {first_row} in this file',
                    duplicate_of_row=first_row,
                )
            elif fingerprint in existing_by_fp:
                prior_tx = existing_by_fp[fingerprint]
                duplicate_entry = duplicate_report_entry(
                    row.row_number, DUPLICATE_EXISTING,
                    f'Already imported: transaction #{prior_tx.id} '
                    f'(import #{prior_tx.import_history_id})',
                    duplicate_of_transaction_id=prior_tx.id,
                    duplicate_of_import_id=prior_tx.import_history_id,
                )

            if duplicate_entry is not None:
                skipped_rows += 1
                duplicates.append(duplicate_entry)
                raw_imp = RawImport(
                    import_history_id=import_history.id,
                    sheet_name=getattr(row, 'sheet_name', '') or _first_parsed_sheet(parse_result),
                    row_number=row.row_number,
                    raw_data=json.dumps({k: str(v) for k, v in row.raw_data.items() if v is not None}),
                    status='SKIPPED',
                    errors=json.dumps([duplicate_entry]),
                    warnings=json.dumps(row.warnings) if row.warnings else None,
                )
                db.session.add(raw_imp)
                if total_rows % 1000 == 0:
                    db.session.flush()
                continue

            seen_in_file.setdefault(fingerprint, row.row_number)

            if row.status is ImportRowStatus.SUCCESS or row.status.value == 'SUCCESS':
                success_rows += 1
            elif row.status is ImportRowStatus.WARNING or row.status.value == 'WARNING':
                warning_rows += 1
            else:
                error_rows += 1
                rejections.append(duplicate_report_entry(
                    row.row_number, 'ROW_REJECTED', '; '.join(row.errors),
                ))

            for warning in row.warnings:
                warning_report.append(duplicate_report_entry(
                    row.row_number, 'ROW_WARNING', warning,
                ))

            # RawImport record
            raw_imp = RawImport(
                import_history_id=import_history.id,
                sheet_name=getattr(row, 'sheet_name', '') or _first_parsed_sheet(parse_result),
                row_number=row.row_number,
                raw_data=json.dumps({k: str(v) for k, v in row.raw_data.items() if v is not None}),
                status=row.status.value,
                errors=json.dumps(row.errors) if row.errors else None,
                warnings=json.dumps(row.warnings) if row.warnings else None,
            )
            db.session.add(raw_imp)

            if row.status is ImportRowStatus.ERROR or row.status.value == 'ERROR':
                if total_rows % 1000 == 0:
                    db.session.flush()
                continue

            # Normalized Transaction persistence
            is_agg = bool(
                norm.get('is_aggregate')
                or getattr(row, 'is_aggregate', False)
                or (getattr(row, 'sheet_name', '').lower() in ('b2cs', 'b2csa', 'nil', 'hsn', 'hsnb2c'))
            )
            has_doc_number = bool(norm.get('invoice_number') or norm.get('note_number'))

            if norm and (has_doc_number or is_agg):
                inv_date = parse_date(norm.get('invoice_date'))
                note_date = parse_date(norm.get('note_date'))
                orig_inv_date = parse_date(norm.get('original_invoice_date'))

                # Supply classification
                raw_supply = str(norm.get('supply_type') or '').strip().upper()
                if raw_supply in (
                    'B2B', 'B2BA', 'B2CS', 'B2CSA', 'B2CL', 'B2CLA',
                    'CDNR', 'CDNRA', 'CDNUR', 'CDNURA', 'NIL', 'EXEMPT',
                    'NONGST', 'EXPORT', 'EXPA', 'SEZ', 'HSN', 'HSNB2C'
                ):
                    supply_type = raw_supply
                else:
                    classify = classification_fn or classify_transaction
                    supply_type = classify(norm, profile, return_period)

                pos_code = resolve_pos_code(norm.get('place_of_supply'), norm.get('customer_gstin'), seller_state)
                txval = _safe_decimal(norm.get('taxable_value'))
                rate = _safe_decimal(norm.get('tax_rate'))
                cgst = _safe_decimal(norm.get('cgst_amount'))
                sgst = _safe_decimal(norm.get('sgst_amount'))
                igst = _safe_decimal(norm.get('igst_amount'))
                cess = _safe_decimal(norm.get('cess_amount'))
                cgst_r = _safe_decimal(norm.get('cgst_rate'))
                sgst_r = _safe_decimal(norm.get('sgst_rate'))
                igst_r = _safe_decimal(norm.get('igst_rate'))

                if cgst == 0 and sgst == 0 and igst == 0 and rate > 0 and txval > 0:
                    if pos_code and seller_state and pos_code == seller_state:
                        half_rate = rate / Decimal('2')
                        cgst = ((txval * half_rate) / Decimal('100')).quantize(Decimal('0.01'))
                        sgst = ((txval * half_rate) / Decimal('100')).quantize(Decimal('0.01'))
                        cgst_r = half_rate
                        sgst_r = half_rate
                    else:
                        igst = ((txval * rate) / Decimal('100')).quantize(Decimal('0.01'))
                        igst_r = rate
                elif rate > 0 and cgst_r == 0 and sgst_r == 0 and igst_r == 0:
                    if pos_code and seller_state and pos_code == seller_state:
                        cgst_r = rate / Decimal('2')
                        sgst_r = rate / Decimal('2')
                    else:
                        igst_r = rate

                total_tax = cgst + sgst + igst + cess
                inv_val = _safe_decimal(norm.get('invoice_value'))
                if inv_val == 0 and txval > 0:
                    inv_val = txval + total_tax

                tx = Transaction(
                    import_history_id=import_history.id,
                    profile_id=profile_id,
                    raw_import=raw_imp,
                    source_platform=platform_name,
                    source_row_id=str(
                        norm.get('order_item_id')
                        or norm.get('sub_order_id')
                        or norm.get('order_id')
                        or f"{getattr(row, 'sheet_name', 'row')}_{row.row_number}"
                    ),
                    order_id=norm.get('order_id'),
                    invoice_number=(None if is_agg else norm.get('invoice_number')),
                    invoice_date=inv_date,
                    invoice_type=('aggregate' if is_agg else (norm.get('invoice_type') or 'regular')),
                    customer_name=norm.get('customer_name'),
                    customer_gstin=norm.get('customer_gstin'),
                    place_of_supply=pos_code,
                    seller_gstin=seller_gstin,
                    item_code=norm.get('item_code'),
                    hsn_sac=norm.get('hsn_sac'),
                    description=norm.get('description'),
                    quantity=_safe_decimal(norm.get('quantity'), Decimal('0') if is_agg else Decimal('1')),
                    uqc=norm.get('uqc', 'NOS'),
                    taxable_value=txval,
                    discount=_safe_decimal(norm.get('discount')) if norm.get('discount') else None,
                    cgst_rate=cgst_r,
                    cgst_amount=cgst,
                    sgst_rate=sgst_r,
                    sgst_amount=sgst,
                    igst_rate=igst_r,
                    igst_amount=igst,
                    cess_rate=_safe_decimal(norm.get('cess_rate')),
                    cess_amount=cess,
                    total_tax=total_tax,
                    invoice_value=inv_val,
                    tax_rate=rate,
                    supply_type=supply_type,
                    reverse_charge='Y' if str(norm.get('reverse_charge', 'N')).upper() in ('Y', 'YES', 'TRUE', '1') else 'N',
                    ecommerce_gstin=norm.get('ecommerce_gstin'),
                    marketplace_name=norm.get('marketplace_name', platform_name),
                    note_type=norm.get('note_type'),
                    note_number=norm.get('note_number'),
                    note_date=note_date,
                    original_invoice_number=norm.get('original_invoice_number'),
                    original_invoice_date=orig_inv_date,
                    nil_rated_flag=(supply_type == 'NIL'),
                    exempt_flag=(supply_type == 'EXEMPT'),
                    non_gst_flag=(supply_type == 'NONGST'),
                    return_flag=bool(norm.get('return_flag', False)),
                    cancellation_flag=bool(norm.get('cancellation_flag', False)),
                    amendment_flag=bool(norm.get('amendment_flag', False)),
                    validation_status='VALID',
                    classification_status=supply_type,
                    gstr1_table=gstr1_table_map.get(supply_type, 'unknown'),
                    source_metadata=json.dumps(
                        dict(norm.get('source_metadata', {}), is_aggregate=is_agg, source_sheet=getattr(row, 'sheet_name', '')),
                        default=str
                    ),
                    row_fingerprint=fingerprint,
                )
                db.session.add(tx)

            if total_rows % 1000 == 0:
                db.session.flush()

        # Step 9: Final flush and ImportHistory bookkeeping
        db.session.flush()

        counts = {
            'total_rows': total_rows,
            'success_rows': success_rows,
            'warning_rows': warning_rows,
            'error_rows': error_rows,
            'skipped_rows': skipped_rows,
            'duplicate_rows': skipped_rows,
        }
        import_history.total_rows = total_rows
        import_history.success_rows = success_rows
        import_history.error_rows = error_rows
        import_history.warning_rows = warning_rows
        import_history.skipped_rows = skipped_rows
        import_history.processing_status = 'COMPLETED' if not (
            error_rows or warning_rows or skipped_rows
        ) else 'PARTIAL'

        error_summary, warning_summary = duplicate_envelope(
            rejections, duplicates, warning_report, counts
        )
        import_history.error_summary = error_summary if rejections else None
        import_history.warning_summary = warning_summary if (duplicates or warning_report) else None
        import_history.processing_completed_at = datetime.utcnow()
        import_history.processing_duration_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)

        db.session.commit()

        result.total_rows = total_rows
        result.success_rows = success_rows
        result.error_rows = error_rows
        result.warning_rows = warning_rows
        result.skipped_rows = skipped_rows
        result.error_summary = import_history.error_summary
        result.warning_summary = import_history.warning_summary
        result.status = import_history.processing_status
        result.stats['status'] = import_history.processing_status

    except Exception as e:
        db.session.rollback()
        try:
            failed_ih = db.session.get(ImportHistory, result.import_history_id)
            if failed_ih:
                failed_ih.processing_status = 'FAILED'
                failed_ih.processing_completed_at = datetime.utcnow()
                failed_ih.error_summary = json.dumps({
                    'counts': {'total_rows': 0, 'success_rows': 0, 'warning_rows': 0,
                               'error_rows': 1, 'skipped_rows': 0},
                    'errors': [{'code': 'UNEXPECTED_ERROR', 'message': str(e)}],
                })
                db.session.commit()
        except Exception:
            db.session.rollback()
        result.status = 'FAILED'
        result.stats['status'] = 'FAILED'
        result.errors.append(str(e))
        raise

    finally:
        if wb:
            try:
                wb.close()
            except Exception:
                pass

    return result


def _create_transaction_from_normalized(
    normalized: Dict[str, Any],
    import_history_id: int,
    profile_id: int,
    raw_import_id: Optional[int] = None,
    source_platform: str = '',
    classification: str = '',
    return_period: str = '',
    raw_import: Optional[RawImport] = None,
) -> Transaction:
    """Create a Transaction entity from normalized data (for backward compatibility)."""
    inv_date = parse_date(normalized.get('invoice_date'))
    note_date = parse_date(normalized.get('note_date'))
    orig_inv_date = parse_date(normalized.get('original_invoice_date'))

    gstr1_table_map = {
        'B2B': 'b2b',
        'B2BA': 'b2ba',
        'B2CS': 'b2cs',
        'B2CSA': 'b2csa',
        'B2CL': 'b2cl',
        'B2CLA': 'b2cla',
        'CDNR': 'cdnr',
        'CDNRA': 'cdnra',
        'CDNUR': 'cdnur',
        'CDNURA': 'cdnura',
        'NIL': 'nil',
        'EXEMPT': 'nil',
        'NONGST': 'nil',
        'EXPORT': 'exp',
        'EXPA': 'expa',
        'SEZ': 'exp',
        'HSN': 'hsn',
        'HSNB2C': 'hsnb2c',
        'UNKNOWN': 'unknown'
    }

    txval = _safe_decimal(normalized.get('taxable_value'))
    rate = _safe_decimal(normalized.get('tax_rate'))
    cgst = _safe_decimal(normalized.get('cgst_amount'))
    sgst = _safe_decimal(normalized.get('sgst_amount'))
    igst = _safe_decimal(normalized.get('igst_amount'))
    cess = _safe_decimal(normalized.get('cess_amount'))
    cgst_r = _safe_decimal(normalized.get('cgst_rate'))
    sgst_r = _safe_decimal(normalized.get('sgst_rate'))
    igst_r = _safe_decimal(normalized.get('igst_rate'))
    pos = str(normalized.get('place_of_supply') or '').strip()
    seller_st = str(normalized.get('seller_gstin') or '')[:2]

    if cgst == 0 and sgst == 0 and igst == 0 and rate > 0 and txval > 0:
        if pos and seller_st and pos == seller_st:
            half_rate = rate / Decimal('2')
            cgst = ((txval * half_rate) / Decimal('100')).quantize(Decimal('0.01'))
            sgst = ((txval * half_rate) / Decimal('100')).quantize(Decimal('0.01'))
            cgst_r = half_rate
            sgst_r = half_rate
        else:
            igst = ((txval * rate) / Decimal('100')).quantize(Decimal('0.01'))
            igst_r = rate
    elif rate > 0 and cgst_r == 0 and sgst_r == 0 and igst_r == 0:
        if pos and seller_st and pos == seller_st:
            cgst_r = rate / Decimal('2')
            sgst_r = rate / Decimal('2')
        else:
            igst_r = rate

    total_tax = cgst + sgst + igst + cess
    inv_val = _safe_decimal(normalized.get('invoice_value'))
    if inv_val == 0 and txval > 0:
        inv_val = txval + total_tax

    is_agg = bool(
        normalized.get('is_aggregate')
        or normalized.get('invoice_type') == 'aggregate'
        or str(normalized.get('source_sheet', '')).lower() in ('b2cs', 'b2csa', 'nil', 'hsn', 'hsnb2c')
    )

    extra_kwargs = {'raw_import': raw_import} if raw_import is not None else {'raw_import_id': raw_import_id}
    return Transaction(
        import_history_id=import_history_id,
        profile_id=profile_id,
        source_platform=source_platform,
        source_row_id=str(
            normalized.get('order_item_id')
            or normalized.get('sub_order_id')
            or normalized.get('order_id')
            or ''
        ),
        order_id=normalized.get('order_id'),
        invoice_number=(None if is_agg else (normalized.get('invoice_number') or None)),
        **extra_kwargs,
        invoice_date=inv_date,
        invoice_type=('aggregate' if is_agg else (normalized.get('invoice_type') or 'regular')),
        customer_name=normalized.get('customer_name', ''),
        customer_gstin=normalized.get('customer_gstin', ''),
        place_of_supply=normalized.get('place_of_supply', ''),
        seller_gstin=normalized.get('seller_gstin', ''),
        item_code=normalized.get('item_code', ''),
        hsn_sac=normalized.get('hsn_sac', ''),
        description=normalized.get('description', ''),
        quantity=_safe_decimal(normalized.get('quantity'), Decimal('1')),
        uqc=normalized.get('uqc', 'NOS'),
        taxable_value=txval,
        discount=_safe_decimal(normalized.get('discount')) if normalized.get('discount') else None,
        cgst_rate=cgst_r,
        cgst_amount=cgst,
        sgst_rate=sgst_r,
        sgst_amount=sgst,
        igst_rate=igst_r,
        igst_amount=igst,
        cess_rate=_safe_decimal(normalized.get('cess_rate')),
        cess_amount=cess,
        total_tax=total_tax,
        invoice_value=inv_val,
        tax_rate=rate,
        supply_type=classification or normalized.get('supply_type', 'INTRA'),
        reverse_charge='Y' if str(normalized.get('reverse_charge', 'N')).upper() in ('Y', 'YES', 'TRUE', '1') else 'N',
        ecommerce_gstin=normalized.get('ecommerce_gstin', ''),
        marketplace_name=normalized.get('marketplace_name', source_platform),
        note_type=normalized.get('note_type', ''),
        note_number=normalized.get('note_number', ''),
        note_date=note_date,
        original_invoice_number=normalized.get('original_invoice_number', ''),
        original_invoice_date=orig_inv_date,
        nil_rated_flag=(classification == 'NIL'),
        exempt_flag=(classification == 'EXEMPT'),
        non_gst_flag=(classification == 'NONGST'),
        return_flag=bool(normalized.get('return_flag', False)),
        cancellation_flag=bool(normalized.get('cancellation_flag', False)),
        amendment_flag=bool(normalized.get('amendment_flag', False)),
        validation_status='VALID',
        classification_status=classification,
        gstr1_table=gstr1_table_map.get(classification, 'unknown'),
        source_metadata=json.dumps(normalized.get('source_metadata', {}), default=str),
    )
def reprocess_import(
    import_history_id: int,
    profile_id: int,
    user_id: int,
) -> ImportProcessingResult:
    """Reprocess an existing import safely."""
    from app.models.gstr1_generation import GSTR1Generation
    
    # 1. Resolve and check original import
    original_import = db.session.get(ImportHistory, import_history_id)
    if not original_import or original_import.profile_id != profile_id:
        result = ImportProcessingResult()
        result.status = 'FAILED'
        result.errors.append("Original import not found or unauthorized.")
        return result

    # 4. Check generation freeze BEFORE mutating
    freeze = db.session.query(GSTR1Generation).filter_by(
        profile_id=profile_id,
        return_period=original_import.return_period,
        generation_status='COMPLETED',
        validation_passed=True
    ).first()
    if freeze:
        result = ImportProcessingResult()
        result.status = 'FAILED'
        result.errors.append("Cannot reprocess: A finalized GSTR-1 generation exists for this return period.")
        return result

    # 8. Create NEW ImportHistory
    new_import = ImportHistory(
        user_id=user_id,
        profile_id=profile_id,
        file_name=original_import.file_name,
        original_file_name=original_import.original_file_name,
        file_hash=original_import.file_hash,
        file_size=original_import.file_size,
        mime_type=original_import.mime_type,
        platform_name=original_import.platform_name,
        return_period=original_import.return_period,
        financial_year=original_import.financial_year,
        raw_file_path=original_import.raw_file_path,
        processing_status='PROCESSING',
        processing_started_at=datetime.utcnow(),
        is_reprocessed=True,
        parent_import_id=original_import.id
    )
    db.session.add(new_import)
    db.session.flush()
    new_id = new_import.id

    # 6. Soft-delete ONLY active Transactions belonging to the source ImportHistory
    db.session.query(Transaction).filter_by(
        import_history_id=original_import.id,
        is_deleted=False
    ).update({
        'is_deleted': True,
        'deleted_at': datetime.utcnow(),
        'deleted_by': user_id
    }, synchronize_session=False)
    db.session.flush()

    # 11. Re-run import pipeline
    res = process_import(
        file_path=original_import.raw_file_path,
        profile_id=profile_id,
        platform_name=original_import.platform_name,
        user_id=user_id,
        return_period=original_import.return_period,
        financial_year=original_import.financial_year,
        allow_duplicate_file=True,
        import_history_id=new_id
    )

    return res
