from typing import List, Dict, Any
from decimal import Decimal

def get_nil_rated(profile_id: int, return_period: str) -> Dict[str, List[Dict[str, Any]]]:
    return {
        'NIL': [],
        'EXEMPT': [],
        'NONGST': []
    }

def aggregate_nil(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    aggregated = {}
    for t in transactions:
        cat = t.get('nil_category', 'NIL')
        stype = t.get('supply_type', 'INTRA')
        key = (cat, stype)
        
        if key not in aggregated:
            aggregated[key] = {
                'category': cat,
                'supply_type': stype,
                'registered_value': Decimal('0.00'),
                'unregistered_value': Decimal('0.00')
            }
            
        agg = aggregated[key]
        val = Decimal(str(t.get('invoice_value', 0)))
        if t.get('customer_gstin'):
            agg['registered_value'] += val
        else:
            agg['unregistered_value'] += val
            
    return list(aggregated.values())
