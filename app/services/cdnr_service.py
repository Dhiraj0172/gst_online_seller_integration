from typing import List, Dict, Any
from decimal import Decimal
from app.services.b2b_service import ValidationResult

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
