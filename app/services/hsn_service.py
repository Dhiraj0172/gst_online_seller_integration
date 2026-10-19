from typing import List, Dict, Any
from decimal import Decimal
from app.services.b2b_service import ValidationResult
from app.services.gst_rules import get_rules_for_period

class ReconciliationResult:
    def __init__(self, is_matched: bool, differences: List[str]):
        self.is_matched = is_matched
        self.differences = differences


def _val(t: Any, key: str, default: Any = None) -> Any:
    """Extract value from either dict or object with key fallbacks."""
    if isinstance(t, dict):
        val = t.get(key)
        if val is None:
            if key == 'cgst': val = t.get('cgst_amount')
            elif key == 'sgst': val = t.get('sgst_amount')
            elif key == 'igst': val = t.get('igst_amount')
            elif key == 'cess': val = t.get('cess_amount')
            elif key == 'description': val = t.get('product_name')
        return val if val is not None else default

    val = getattr(t, key, None)
    if val is None:
        if key == 'cgst': val = getattr(t, 'cgst_amount', None)
        elif key == 'sgst': val = getattr(t, 'sgst_amount', None)
        elif key == 'igst': val = getattr(t, 'igst_amount', None)
        elif key == 'cess': val = getattr(t, 'cess_amount', None)
        elif key == 'description': val = getattr(t, 'product_name', None)
    return val if val is not None else default


def aggregate_hsn(transactions: List[Any]) -> List[Dict[str, Any]]:
    aggregated = {}
    for t in transactions:
        hsn = str(_val(t, 'hsn_sac', '') or '')
        uqc = _val(t, 'uqc', 'OTH') or 'OTH'
        rate = Decimal(str(_val(t, 'tax_rate', 0) or 0))
        key = (hsn, uqc, rate)

        if key not in aggregated:
            desc = _val(t, 'description', '') or ''
            aggregated[key] = {
                'hsn_sac': hsn,
                'hsn_sc': hsn,
                'description': desc,
                'desc': desc,
                'uqc': uqc,
                'quantity': Decimal('0.00'),
                'qty': 0.0,
                'taxable_value': Decimal('0.00'),
                'txval': 0.0,
                'cgst': Decimal('0.00'),
                'camt': 0.0,
                'sgst': Decimal('0.00'),
                'samt': 0.0,
                'igst': Decimal('0.00'),
                'iamt': 0.0,
                'cess': Decimal('0.00'),
                'csamt': 0.0,
                'total_tax': Decimal('0.00'),
                'val': 0.0,
                'rate': rate,
                'rt': float(rate)
            }

        agg = aggregated[key]
        agg['quantity'] += Decimal(str(_val(t, 'quantity', 0) or 0))
        agg['taxable_value'] += Decimal(str(_val(t, 'taxable_value', 0) or 0))
        agg['cgst'] += Decimal(str(_val(t, 'cgst', 0) or 0))
        agg['sgst'] += Decimal(str(_val(t, 'sgst', 0) or 0))
        agg['igst'] += Decimal(str(_val(t, 'igst', 0) or 0))
        agg['cess'] += Decimal(str(_val(t, 'cess', 0) or 0))

        tax_total = Decimal(str(_val(t, 'cgst', 0) or 0)) + Decimal(str(_val(t, 'sgst', 0) or 0)) + Decimal(str(_val(t, 'igst', 0) or 0)) + Decimal(str(_val(t, 'cess', 0) or 0))
        agg['total_tax'] += tax_total

        # Keep float copies updated for json/table rendering
        agg['qty'] = float(agg['quantity'])
        agg['txval'] = float(agg['taxable_value'])
        agg['camt'] = float(agg['cgst'])
        agg['samt'] = float(agg['sgst'])
        agg['iamt'] = float(agg['igst'])
        agg['csamt'] = float(agg['cess'])
        agg['val'] = float(agg['taxable_value'] + agg['total_tax'])

    return list(aggregated.values())


def aggregate_hsn_b2b(transactions: List[Any]) -> List[Dict[str, Any]]:
    """Aggregate HSN for B2B transactions only."""
    b2b_txs = [
        t for t in transactions
        if bool(_val(t, 'customer_gstin') and len(str(_val(t, 'customer_gstin')).strip()) == 15)
        or str(_val(t, 'classification_status') or _val(t, 'supply_type') or '').upper() in (
            'B2B', 'B2BA', 'CDNR', 'CDNRA', 'EXPORT', 'SEZ', 'SEZ_REGISTERED',
            'NIL_REGISTERED', 'EXEMPT_REGISTERED', 'NONGST_REGISTERED', 'HSN'
        )
    ]
    return aggregate_hsn(b2b_txs)


def aggregate_hsn_b2c(transactions: List[Any]) -> List[Dict[str, Any]]:
    """Aggregate HSN for B2C transactions only."""
    b2c_txs = [
        t for t in transactions
        if not (bool(_val(t, 'customer_gstin') and len(str(_val(t, 'customer_gstin')).strip()) == 15))
        and str(_val(t, 'classification_status') or _val(t, 'supply_type') or '').upper() not in (
            'B2B', 'B2BA', 'CDNR', 'CDNRA', 'EXPORT', 'SEZ', 'SEZ_REGISTERED',
            'NIL_REGISTERED', 'EXEMPT_REGISTERED', 'NONGST_REGISTERED'
        )
    ]
    return aggregate_hsn(b2c_txs)


def validate_hsn_summary(hsn_data: List[Dict[str, Any]], transactions: List[Dict[str, Any]]) -> ValidationResult:
    errors = []
    return ValidationResult(len(errors) == 0, errors)


def reconcile_hsn_with_invoices(hsn_data: List[Dict[str, Any]], invoice_data: List[Dict[str, Any]]) -> ReconciliationResult:
    diffs = []
    return ReconciliationResult(len(diffs) == 0, diffs)
