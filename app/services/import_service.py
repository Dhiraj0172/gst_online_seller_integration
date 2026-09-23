import openpyxl
import json
import os
from typing import Dict, Any, Iterator, List, Optional
from decimal import Decimal
from datetime import datetime
from app.adapters.registry import get_adapter, detect_platform
from app.adapters.base import ImportResult, ImportRow, ImportRowStatus
from app.models import ImportHistory, RawImport, Transaction
from app.extensions import db
from app.services.classification_service import classify_transaction, determine_supply_type
from app.services.duplicate_service import build_fingerprint, find_existing_transactions
from app.services.gst_rules import get_rules_for_period
from app.utils.csv_utils import read_csv_rows


class ImportProcessingResult:
    def __init__(self):
        self.total_rows = 0
        self.success_rows = 0
        self.warning_rows = 0
        self.error_rows = 0
        self.skipped_rows = 0
        self.errors = []
        self.warnings = []
        self.stats = {}
        self.import_history_id = None


def validate_file(file_path: str) -> bool:
    if not file_path.endswith(('.xlsx', '.csv', '.xls')):
        raise ValueError("Invalid file format")
    return True


def read_chunks(file_path: str, chunk_size: int = 1000) -> Iterator[List[Dict[str, Any]]]:
    """Read an Excel or CSV file in chunks, yielding lists of row dicts.

    Both formats expose the same row dicts (header keys + ``_row_number`` /
    ``_sheet_name``) so the adapter normalization path is identical.
    """
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
    """Chunked reader for CSV sources (UTF-8/BOM aware, delimiter detected)."""
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
    financial_year: str
) -> ImportProcessingResult:
    """
    Complete import pipeline:
    1. Detect platform (if not specified)
    2. Parse file using adapter
    3. Validate and normalize each row
    4. Create RawImport records
    5. Classify transactions
    6. Create Transaction records
    7. Update ImportHistory with stats
    """
    result = ImportProcessingResult()
    start_time = datetime.utcnow()
    
    # Create ImportHistory record
    import_history = ImportHistory(
        user_id=user_id,
        profile_id=profile_id,
        file_name=os.path.basename(file_path),
        original_file_name=os.path.basename(file_path),
        platform_name=platform_name,
        return_period=return_period,
        financial_year=financial_year,
        processing_status='PARSING',
        processing_started_at=start_time,
        raw_file_path=file_path
    )
    db.session.add(import_history)
    db.session.flush()
    result.import_history_id = import_history.id
    
    try:
        # Get or detect adapter
        adapter = get_adapter(platform_name)
        if not adapter:
            # Try auto-detection
            wb = openpyxl.load_workbook(file_path, read_only=True)
            adapter = detect_platform(wb, os.path.basename(file_path))
            wb.close()
            
        if not adapter:
            raise ValueError(f"No adapter found for platform: {platform_name}")
            
        # Validate file structure (Excel as a workbook, CSV as a path)
        is_csv = file_path.lower().endswith('.csv')
        source = file_path
        if not is_csv:
            source = openpyxl.load_workbook(file_path, read_only=True)
        is_valid, validation_errors = adapter.validate(source)
        if not is_csv:
            source.close()
        
        if not is_valid:
            raise ValueError(f"File validation failed: {', '.join(validation_errors)}")
            
        import_history.processing_status = 'NORMALIZING'
        db.session.flush()
        
        # Get profile for seller GSTIN
        from app.models import GSTProfile
        profile = db.session.get(GSTProfile, profile_id)
        seller_gstin = profile.gstin if profile else ''
        seller_state = profile.state_code if profile else ''

        # Duplicate bookkeeping: rows already stored for this profile, plus rows
        # seen earlier in this very file. Duplicates are marked, never dropped.
        seen_in_file: Dict[str, int] = {}
        duplicate_rows = 0
        duplicate_entries: List[Dict[str, Any]] = []
        
        # Process in chunks
        for chunk in read_chunks(file_path):
            for raw_row in chunk:
                row_number = raw_row.pop('_row_number', 0)
                sheet_name = raw_row.pop('_sheet_name', '')
                
                result.total_rows += 1
                
                # Create RawImport record
                raw_import = RawImport(
                    import_history_id=import_history.id,
                    sheet_name=sheet_name,
                    row_number=row_number,
                    raw_data=json.dumps(raw_row, default=str),
                    status='PENDING'
                )
                db.session.add(raw_import)
                db.session.flush()
                
                try:
                    # Normalize using adapter
                    normalized = adapter.normalize(raw_row)
                    row_errors = list(normalized.get('_errors') or [])

                    # Duplicate detection against this file and stored data
                    fingerprint = build_fingerprint(normalized, profile_id, platform_name)
                    duplicate_reason = None
                    if fingerprint in seen_in_file:
                        duplicate_reason = f'Duplicate of row {seen_in_file[fingerprint]} in this file'
                    else:
                        prior_tx = find_existing_transactions(profile_id, [fingerprint]).get(fingerprint)
                        if prior_tx is not None:
                            duplicate_reason = (
                                f'Already imported: transaction #{prior_tx.id} '
                                f'(import #{prior_tx.import_history_id})'
                            )
                    if duplicate_reason:
                        seen_in_file.setdefault(fingerprint, row_number)
                        duplicate_rows += 1
                        duplicate_entries.append({'row': row_number, 'message': duplicate_reason})
                        raw_import.status = 'SKIPPED'
                        raw_import.errors = json.dumps([duplicate_reason])
                        result.skipped_rows += 1
                        continue
                    seen_in_file.setdefault(fingerprint, row_number)

                    if row_errors:
                        raw_import.status = 'ERROR'
                        raw_import.errors = json.dumps(row_errors)
                        result.error_rows += 1
                        result.errors.append(f"Row {row_number}: {'; '.join(row_errors)}")
                        continue
                    
                    # Determine supply type (INTRA/INTER)
                    supply_type = determine_supply_type(
                        seller_gstin=seller_gstin,
                        customer_gstin=normalized.get('customer_gstin'),
                        place_of_supply=normalized.get('place_of_supply', ''),
                        seller_state=seller_state
                    )
                    normalized['supply_type'] = supply_type
                    normalized['seller_gstin'] = seller_gstin
                    normalized['seller_state'] = seller_state
                    
                    # Classify transaction
                    classification = classify_transaction(normalized, profile, return_period)
                    normalized['classification'] = classification
                    
                    # Create Transaction record
                    transaction = _create_transaction_from_normalized(
                        normalized=normalized,
                        import_history_id=import_history.id,
                        profile_id=profile_id,
                        raw_import_id=raw_import.id,
                        source_platform=platform_name,
                        classification=classification,
                        return_period=return_period
                    )
                    transaction.row_fingerprint = fingerprint
                    
                    db.session.add(transaction)
                    
                    # Update RawImport status
                    raw_import.status = 'SUCCESS'
                    raw_import.transaction_id = transaction.id
                    
                    result.success_rows += 1
                    
                except Exception as e:
                    raw_import.status = 'ERROR'
                    raw_import.errors = json.dumps([str(e)])
                    result.error_rows += 1
                    result.errors.append(f"Row {row_number}: {str(e)}")
                    
        # Update import history with final stats
        import_history.total_rows = result.total_rows
        import_history.success_rows = result.success_rows
        import_history.error_rows = result.error_rows
        import_history.warning_rows = result.warning_rows
        import_history.skipped_rows = result.skipped_rows
        import_history.processing_status = 'COMPLETED' if result.error_rows == 0 else 'PARTIAL'
        import_history.processing_completed_at = datetime.utcnow()
        import_history.processing_duration_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        
        # Calculate totals
        _update_import_totals(import_history)
        
        db.session.commit()
        result.stats['status'] = import_history.processing_status
        
    except Exception as e:
        db.session.rollback()
        import_history.processing_status = 'FAILED'
        import_history.processing_completed_at = datetime.utcnow()
        import_history.error_summary = json.dumps([str(e)])
        db.session.commit()
        result.errors.append(str(e))
        result.stats['status'] = 'FAILED'
        
    return result


def _create_transaction_from_normalized(
    normalized: Dict[str, Any],
    import_history_id: int,
    profile_id: int,
    raw_import_id: int,
    source_platform: str,
    classification: str,
    return_period: str
) -> Transaction:
    """Create Transaction model from normalized data."""
    
    # Parse date
    invoice_date = None
    date_str = normalized.get('invoice_date', '')
    if date_str:
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
            try:
                invoice_date = datetime.strptime(str(date_str), fmt).date()
                break
            except:
                continue
    
    # Parse note date
    note_date = None
    note_date_str = normalized.get('note_date', '')
    if note_date_str:
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
            try:
                note_date = datetime.strptime(str(note_date_str), fmt).date()
                break
            except:
                continue
                
    # Parse original invoice date
    original_invoice_date = None
    orig_date_str = normalized.get('original_invoice_date', '')
    if orig_date_str:
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d'):
            try:
                original_invoice_date = datetime.strptime(str(orig_date_str), fmt).date()
                break
            except:
                continue
    
    # Determine GSTR-1 table mapping
    gstr1_table_map = {
        'B2B': 'b2b',
        'B2CS': 'b2cs',
        'B2CL': 'b2cl',
        'CDNR': 'cdnr',
        'CDNUR': 'cdnur',
        'NIL': 'nil',
        'EXEMPT': 'nil',
        'NONGST': 'nil',
        'EXPORT': 'exp',
        'SEZ': 'exp',
        'UNKNOWN': 'unknown'
    }
    
    return Transaction(
        import_history_id=import_history_id,
        profile_id=profile_id,
        raw_import_id=raw_import_id,
        source_platform=source_platform,
        source_row_id=normalized.get('order_id', ''),
        order_id=normalized.get('order_id', ''),
        invoice_number=normalized.get('invoice_number', ''),
        invoice_date=invoice_date,
        invoice_type=normalized.get('invoice_type', 'regular'),
        customer_name=normalized.get('customer_name', ''),
        customer_gstin=normalized.get('customer_gstin', ''),
        place_of_supply=normalized.get('place_of_supply', ''),
        seller_gstin=normalized.get('seller_gstin', ''),
        item_code=normalized.get('item_code', ''),
        hsn_sac=normalized.get('hsn_sac', ''),
        description=normalized.get('description', ''),
        quantity=Decimal(str(normalized.get('quantity', 0))) if normalized.get('quantity') else None,
        uqc=normalized.get('uqc', ''),
        taxable_value=Decimal(str(normalized.get('taxable_value', 0))) if normalized.get('taxable_value') else None,
        discount=Decimal(str(normalized.get('discount', 0))) if normalized.get('discount') else None,
        cgst_rate=Decimal(str(normalized.get('cgst_rate', 0))) if normalized.get('cgst_rate') else None,
        cgst_amount=Decimal(str(normalized.get('cgst_amount', 0))) if normalized.get('cgst_amount') else None,
        sgst_rate=Decimal(str(normalized.get('sgst_rate', 0))) if normalized.get('sgst_rate') else None,
        sgst_amount=Decimal(str(normalized.get('sgst_amount', 0))) if normalized.get('sgst_amount') else None,
        igst_rate=Decimal(str(normalized.get('igst_rate', 0))) if normalized.get('igst_rate') else None,
        igst_amount=Decimal(str(normalized.get('igst_amount', 0))) if normalized.get('igst_amount') else None,
        cess_rate=Decimal(str(normalized.get('cess_rate', 0))) if normalized.get('cess_rate') else None,
        cess_amount=Decimal(str(normalized.get('cess_amount', 0))) if normalized.get('cess_amount') else None,
        total_tax=Decimal(str(normalized.get('cgst_amount', 0))) + Decimal(str(normalized.get('sgst_amount', 0))) + Decimal(str(normalized.get('igst_amount', 0))) + Decimal(str(normalized.get('cess_amount', 0))),
        invoice_value=Decimal(str(normalized.get('invoice_value', 0))) if normalized.get('invoice_value') else None,
        tax_rate=Decimal(str(normalized.get('tax_rate', 0))) if normalized.get('tax_rate') else None,
        supply_type=normalized.get('supply_type', 'INTRA'),
        reverse_charge='Y' if normalized.get('reverse_charge') else 'N',
        ecommerce_gstin=normalized.get('ecommerce_gstin', ''),
        marketplace_name=source_platform,
        note_type=normalized.get('note_type', ''),
        note_number=normalized.get('note_number', ''),
        note_date=note_date,
        original_invoice_number=normalized.get('original_invoice_number', ''),
        original_invoice_date=original_invoice_date,
        nil_rated_flag=classification == 'NIL',
        exempt_flag=classification == 'EXEMPT',
        non_gst_flag=classification == 'NONGST',
        return_flag=normalized.get('return_flag', False),
        cancellation_flag=normalized.get('cancellation_flag', False),
        amendment_flag=normalized.get('amendment_flag', False),
        validation_status='VALID',
        classification_status=classification,
        gstr1_table=gstr1_table_map.get(classification, 'unknown'),
        source_metadata=json.dumps(normalized.get('source_metadata', {}), default=str)
    )


def _update_import_totals(import_history: ImportHistory) -> None:
    """Update import history with calculated totals from transactions."""
    from sqlalchemy import func
    
    totals = db.session.query(
        func.sum(Transaction.taxable_value),
        func.sum(Transaction.cgst_amount),
        func.sum(Transaction.sgst_amount),
        func.sum(Transaction.igst_amount),
        func.sum(Transaction.cess_amount),
        func.sum(Transaction.invoice_value),
        func.count(Transaction.id).filter(Transaction.classification_status == 'B2B'),
        func.count(Transaction.id).filter(Transaction.classification_status == 'B2CS'),
        func.count(Transaction.id).filter(Transaction.classification_status == 'B2CL'),
        func.count(Transaction.id).filter(Transaction.classification_status.in_(['CDNR', 'CDNUR'])),
        func.count(Transaction.id).filter(Transaction.classification_status.in_(['NIL', 'EXEMPT', 'NONGST'])),
    ).filter(Transaction.import_history_id == import_history.id).first()
    
    if totals:
        import_history.total_taxable_value = totals[0] or 0
        import_history.total_cgst = totals[1] or 0
        import_history.total_sgst = totals[2] or 0
        import_history.total_igst = totals[3] or 0
        import_history.total_cess = totals[4] or 0
        import_history.total_invoice_value = totals[5] or 0


# Import os at module level
import os