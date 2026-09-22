from typing import List, Dict, Any
from decimal import Decimal
from app.services.b2b_service import ValidationResult

def get_ecom_supplies(profile_id: int, return_period: str) -> List[Dict[str, Any]]:
    return []

def aggregate_ecom(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    aggregated = {}
    for t in transactions:
        ecom_gstin = t.get('ecommerce_gstin')
        state = t.get('place_of_supply')
        rate = Decimal(str(t.get('tax_rate', 0)))
        key = (ecom_gstin, state, rate)
        
        if key not in aggregated:
            aggregated[key] = {
                'ecommerce_gstin': ecom_gstin,
                'state': state,
                'rate': rate,
                'taxable_value': Decimal('0.00'),
                'cgst': Decimal('0.00'),
                'sgst': Decimal('0.00'),
                'igst': Decimal('0.00'),
                'cess': Decimal('0.00')
            }
            
        agg = aggregated[key]
        agg['taxable_value'] += Decimal(str(t.get('taxable_value', 0)))
        agg['cgst'] += Decimal(str(t.get('cgst', 0)))
        agg['sgst'] += Decimal(str(t.get('sgst', 0)))
        agg['igst'] += Decimal(str(t.get('igst', 0)))
        agg['cess'] += Decimal(str(t.get('cess', 0)))
        
    return list(aggregated.values())

def validate_ecom(data: List[Dict[str, Any]]) -> ValidationResult:
    errors = []
    return ValidationResult(len(errors) == 0, errors)
