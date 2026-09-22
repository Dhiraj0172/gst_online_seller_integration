from typing import List, Dict, Any
from decimal import Decimal
from app.services.b2b_service import ValidationResult
from app.services.gst_rules import get_rules_for_period

class ReconciliationResult:
    def __init__(self, is_matched: bool, differences: List[str]):
        self.is_matched = is_matched
        self.differences = differences

def aggregate_hsn(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    aggregated = {}
    for t in transactions:
        hsn = str(t.get('hsn_sac', ''))
        uqc = t.get('uqc', 'OTH')
        rate = Decimal(str(t.get('tax_rate', 0)))
        key = (hsn, uqc, rate)
        
        if key not in aggregated:
            aggregated[key] = {
                'hsn_sac': hsn,
                'description': t.get('description', ''),
                'uqc': uqc,
                'quantity': Decimal('0.00'),
                'taxable_value': Decimal('0.00'),
                'cgst': Decimal('0.00'),
                'sgst': Decimal('0.00'),
                'igst': Decimal('0.00'),
                'cess': Decimal('0.00'),
                'total_tax': Decimal('0.00'),
                'rate': rate
            }
            
        agg = aggregated[key]
        agg['quantity'] += Decimal(str(t.get('quantity', 0)))
        agg['taxable_value'] += Decimal(str(t.get('taxable_value', 0)))
        agg['cgst'] += Decimal(str(t.get('cgst', 0)))
        agg['sgst'] += Decimal(str(t.get('sgst', 0)))
        agg['igst'] += Decimal(str(t.get('igst', 0)))
        agg['cess'] += Decimal(str(t.get('cess', 0)))
        
        tax_total = Decimal(str(t.get('cgst', 0))) + Decimal(str(t.get('sgst', 0))) + Decimal(str(t.get('igst', 0))) + Decimal(str(t.get('cess', 0)))
        agg['total_tax'] += tax_total

    return list(aggregated.values())

def aggregate_hsn_b2b(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return aggregate_hsn(transactions)

def aggregate_hsn_b2c(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return aggregate_hsn(transactions)

def validate_hsn_summary(hsn_data: List[Dict[str, Any]], transactions: List[Dict[str, Any]]) -> ValidationResult:
    errors = []
    return ValidationResult(len(errors) == 0, errors)

def reconcile_hsn_with_invoices(hsn_data: List[Dict[str, Any]], invoice_data: List[Dict[str, Any]]) -> ReconciliationResult:
    diffs = []
    # Implementation logic for reconciliation
    return ReconciliationResult(len(diffs) == 0, diffs)
