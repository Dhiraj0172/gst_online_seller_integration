from typing import List, Dict, Any, Optional
from decimal import Decimal
from app.services.b2b_service import ValidationResult
from app.extensions import db
from app.models import Transaction, ImportHistory
from app.utils.query import tenant_query

def get_cdnr(profile_id: int, return_period: str) -> List[Dict[str, Any]]:
    return []

def get_cdnur(profile_id: int, return_period: str) -> List[Dict[str, Any]]:
    return []

def aggregate_cdnr(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    aggregated = {}
    for t in transactions:
        key = (t.get('customer_gstin'), t.get('note_number'))
        if key not in aggregated:
            aggregated[key] = {
                'customer_gstin': t.get('customer_gstin'),
                'note_number': t.get('note_number'),
                'note_date': t.get('note_date'),
                'note_type': t.get('note_type'),
                'original_invoice_number': t.get('original_invoice_number'),
                'original_invoice_date': t.get('original_invoice_date'),
                'note_value': Decimal('0.00'),
                'rate_wise_details': {}
            }
            
        agg = aggregated[key]
        agg['note_value'] += Decimal(str(t.get('note_value', 0)))
        
        rate = Decimal(str(t.get('tax_rate', 0)))
        if rate not in agg['rate_wise_details']:
            agg['rate_wise_details'][rate] = {
                'taxable_value': Decimal('0.00'),
                'cess_amount': Decimal('0.00')
            }
            
        agg['rate_wise_details'][rate]['taxable_value'] += Decimal(str(t.get('taxable_value', 0)))
        agg['rate_wise_details'][rate]['cess_amount'] += Decimal(str(t.get('cess_amount', 0)))
        
    return list(aggregated.values())

def validate_cdnr(notes: List[Dict[str, Any]]) -> ValidationResult:
    errors = []
    for note in notes:
        if not note.get('note_number'):
            errors.append("Missing note number")
    return ValidationResult(len(errors) == 0, errors)


def link_cross_period_credit_notes(profile_id: int, return_period: str) -> Dict[str, Any]:
    """Link credit/debit notes to original invoices across filing periods.

    Tenant-scoped: strictly restricts matching to the same tenant profile_id.
    Excludes staged and deleted rows.
    Identifies notes in return_period whose original invoice was issued in a prior period.
    """
    notes_query = tenant_query(Transaction, int(profile_id)).join(
        ImportHistory, Transaction.import_history_id == ImportHistory.id
    ).filter(
        Transaction.is_deleted == False,
        Transaction.is_staged == False,
        ImportHistory.processing_status.in_(['COMPLETED', 'PARTIAL']),
        ImportHistory.return_period == return_period
    ).filter(
        db.or_(
            Transaction.note_type.isnot(None),
            Transaction.supply_type.in_(['CDNR', 'CDNUR', 'CDNRA', 'CDNURA']),
            Transaction.note_number.isnot(None),
            Transaction.return_flag == True
        )
    )

    notes = notes_query.all()

    links = []
    cross_period_count = 0
    same_period_count = 0
    unmatched_count = 0

    for note in notes:
        orig_inv_num = (note.original_invoice_number or '').strip()
        note_num = note.note_number or note.invoice_number or f"NOTE-{note.id}"
        note_val = float(note.invoice_value or note.taxable_value or 0)
        taxval = float(note.taxable_value or 0)

        if not orig_inv_num:
            links.append({
                'note_id': note.id,
                'note_number': note_num,
                'note_date': str(note.note_date or note.invoice_date or ''),
                'note_type': note.note_type or 'CREDIT',
                'note_value': note_val,
                'taxable_value': taxval,
                'return_period': return_period,
                'original_invoice_number': None,
                'original_invoice_date': None,
                'original_return_period': None,
                'original_invoice_id': None,
                'match_status': 'NO_ORIGINAL_REF',
                'is_cross_period': False,
            })
            unmatched_count += 1
            continue

        # Search for original invoice within the same tenant profile
        orig_tx = tenant_query(Transaction, int(profile_id)).join(
            ImportHistory, Transaction.import_history_id == ImportHistory.id
        ).filter(
            Transaction.is_deleted == False,
            Transaction.is_staged == False,
            Transaction.invoice_number == orig_inv_num
        ).first()

        if orig_tx:
            orig_period = orig_tx.import_history.return_period if orig_tx.import_history else None
            is_cross = (orig_period is not None and orig_period != return_period)
            status = 'CROSS_PERIOD_LINKED' if is_cross else 'SAME_PERIOD_MATCHED'

            if is_cross:
                cross_period_count += 1
            else:
                same_period_count += 1

            links.append({
                'note_id': note.id,
                'note_number': note_num,
                'note_date': str(note.note_date or note.invoice_date or ''),
                'note_type': note.note_type or 'CREDIT',
                'note_value': note_val,
                'taxable_value': taxval,
                'return_period': return_period,
                'original_invoice_number': orig_inv_num,
                'original_invoice_date': str(orig_tx.invoice_date or ''),
                'original_return_period': orig_period,
                'original_invoice_id': orig_tx.id,
                'original_customer_gstin': orig_tx.customer_gstin,
                'match_status': status,
                'is_cross_period': is_cross,
            })
        else:
            unmatched_count += 1
            links.append({
                'note_id': note.id,
                'note_number': note_num,
                'note_date': str(note.note_date or note.invoice_date or ''),
                'note_type': note.note_type or 'CREDIT',
                'note_value': note_val,
                'taxable_value': taxval,
                'return_period': return_period,
                'original_invoice_number': orig_inv_num,
                'original_invoice_date': str(note.original_invoice_date or ''),
                'original_return_period': None,
                'original_invoice_id': None,
                'match_status': 'UNMATCHED_ORIGINAL',
                'is_cross_period': False,
            })

    return {
        'profile_id': profile_id,
        'return_period': return_period,
        'total_notes': len(notes),
        'cross_period_count': cross_period_count,
        'same_period_count': same_period_count,
        'unmatched_count': unmatched_count,
        'links': links,
    }
