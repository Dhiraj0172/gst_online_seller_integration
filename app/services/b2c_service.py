from typing import List, Dict, Any
from decimal import Decimal
from app.services.b2b_service import ValidationResult
from app.services.gst_rules import get_rules_for_period

def get_b2c_invoices(profile_id: int, return_period: str) -> Dict[str, List[Dict[str, Any]]]:
    # Mock implementation for DB query
    return {
        'b2cs': [],
        'b2cl': []
    }

def aggregate_b2cs(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    aggregated = {}
    for t in transactions:
        key = (t.get('place_of_supply'), t.get('tax_rate', 0))
        if key not in aggregated:
            aggregated[key] = {
                'place_of_supply': key[0],
                'tax_rate': Decimal(str(key[1])),
                'taxable_value': Decimal('0.00'),
                'cess_amount': Decimal('0.00'),
                'supply_type': t.get('supply_type', 'INTRA')
            }
        
        agg = aggregated[key]
        agg['taxable_value'] += Decimal(str(t.get('taxable_value', 0)))
        agg['cess_amount'] += Decimal(str(t.get('cess_amount', 0)))
        
    return list(aggregated.values())

def aggregate_b2cl(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # B2CL is invoice level, similar to B2B
    aggregated = {}
    for t in transactions:
        key = t.get('invoice_number')
        if key not in aggregated:
            aggregated[key] = {
                'invoice_number': key,
                'invoice_date': t.get('invoice_date'),
                'invoice_value': Decimal('0.00'),
                'place_of_supply': t.get('place_of_supply'),
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

def validate_b2c(data: Dict[str, List[Dict[str, Any]]]) -> ValidationResult:
    errors = []
    # Add validation logic
    return ValidationResult(len(errors) == 0, errors)
