from typing import List, Dict, Any
from decimal import Decimal

class ValidationResult:
    def __init__(self, is_valid: bool, errors: List[str]):
        self.is_valid = is_valid
        self.errors = errors

def get_b2b_invoices(profile_id: int, return_period: str) -> List[Dict[str, Any]]:
    # In a real app, query DB: Transaction.query.filter_by(profile_id=profile_id, return_period=return_period, classification='B2B').all()
    return []

def aggregate_b2b(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    aggregated = {}
    for t in transactions:
        key = (t.get('customer_gstin'), t.get('invoice_number'))
        if key not in aggregated:
            aggregated[key] = {
                'customer_gstin': t.get('customer_gstin'),
                'invoice_number': t.get('invoice_number'),
                'invoice_date': t.get('invoice_date'),
                'invoice_value': Decimal('0.00'),
                'place_of_supply': t.get('place_of_supply'),
                'reverse_charge': t.get('reverse_charge', 'N'),
                'invoice_type': t.get('invoice_type', 'Regular'),
                'rate_wise_details': {}
            }
        
        agg = aggregated[key]
        agg['invoice_value'] += Decimal(str(t.get('invoice_value', 0)))
        
        rate = Decimal(str(t.get('tax_rate', 0)))
        if rate not in agg['rate_wise_details']:
            agg['rate_wise_details'][rate] = {
                'taxable_value': Decimal('0.00'),
                'cess_amount': Decimal('0.00')
            }
            
        agg['rate_wise_details'][rate]['taxable_value'] += Decimal(str(t.get('taxable_value', 0)))
        agg['rate_wise_details'][rate]['cess_amount'] += Decimal(str(t.get('cess_amount', 0)))
        
    return list(aggregated.values())

def validate_b2b(invoices: List[Dict[str, Any]]) -> ValidationResult:
    errors = []
    for inv in invoices:
        if not inv.get('customer_gstin') or len(inv.get('customer_gstin')) != 15:
            errors.append(f"Invalid GSTIN for invoice {inv.get('invoice_number')}")
    return ValidationResult(len(errors) == 0, errors)
