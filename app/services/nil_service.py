from typing import List, Dict, Any, Optional
from decimal import Decimal
from app.services.classification_service import GST_RULE_VERSION


class NilAggregateResult(list):
    """List of aggregated section rows that also allows dictionary-style summary metric access."""
    def __init__(self, items: list, total_nil: Decimal, total_exempt: Decimal, total_nongst: Decimal, rule_version: str):
        super().__init__(items)
        self.total_nil = total_nil
        self.total_exempt = total_exempt
        self.total_nongst = total_nongst
        self.rule_version = rule_version

    def __getitem__(self, item):
        if isinstance(item, str):
            if item == 'total_nil': return self.total_nil
            if item == 'total_exempt': return self.total_exempt
            if item == 'total_nongst': return self.total_nongst
            if item == 'rule_version': return self.rule_version
            raise KeyError(item)
        return super().__getitem__(item)

    def get(self, key: str, default: Any = None) -> Any:
        if key == 'total_nil': return self.total_nil
        if key == 'total_exempt': return self.total_exempt
        if key == 'total_nongst': return self.total_nongst
        if key == 'rule_version': return self.rule_version
        return default


def _val(t: Any, key: str, default: Any = None) -> Any:
    if isinstance(t, dict):
        return t.get(key, default)
    return getattr(t, key, default)


def get_nil_rated(profile_id: int, return_period: str) -> Dict[str, List[Dict[str, Any]]]:
    """Retrieve all NIL, EXEMPT, and NON-GST transactions for profile and return period,
    cleanly partitioned into distinct statutory categories.
    """
    from app.models import Transaction, ImportHistory
    from app.utils.query import tenant_query

    pid = int(profile_id)
    rp = str(return_period).strip()

    query = (
        tenant_query(Transaction, pid)
        .join(ImportHistory, Transaction.import_history_id == ImportHistory.id)
        .filter(
            ImportHistory.return_period == rp,
            Transaction.is_deleted == False,
            Transaction.is_staged == False,
            ImportHistory.processing_status.in_(['COMPLETED', 'PARTIAL']),
        )
    )

    transactions = query.all()
    buckets: Dict[str, List[Dict[str, Any]]] = {'NIL': [], 'EXEMPT': [], 'NONGST': []}

    for tx in transactions:
        cls = (tx.classification_status or tx.supply_type or '').upper()
        norm_dict = {
            'id': tx.id,
            'invoice_number': tx.invoice_number,
            'invoice_date': tx.invoice_date,
            'invoice_value': tx.invoice_value,
            'taxable_value': tx.taxable_value,
            'customer_gstin': tx.customer_gstin,
            'place_of_supply': tx.place_of_supply,
            'nil_rated_flag': getattr(tx, 'nil_rated_flag', False),
            'exempt_flag': getattr(tx, 'exempt_flag', False),
            'non_gst_flag': getattr(tx, 'non_gst_flag', False),
            'supply_type': tx.supply_type,
            'classification_status': tx.classification_status,
        }
        if 'EXEMPT' in cls or getattr(tx, 'exempt_flag', False):
            buckets['EXEMPT'].append(norm_dict)
        elif 'NONGST' in cls or 'NON_GST' in cls or getattr(tx, 'non_gst_flag', False):
            buckets['NONGST'].append(norm_dict)
        elif 'NIL' in cls or getattr(tx, 'nil_rated_flag', False) or (tx.tax_rate is not None and Decimal(str(tx.tax_rate or 0)) == 0):
            buckets['NIL'].append(norm_dict)

    return buckets


def aggregate_nil(transactions: List[Any], supplier_state: Optional[str] = None, **kwargs) -> NilAggregateResult:
    """Aggregate NIL, EXEMPT, and NONGST transactions without collapsing categories."""
    aggregated = {}
    tot_nil = Decimal('0.00')
    tot_exempt = Decimal('0.00')
    tot_nongst = Decimal('0.00')

    for t in transactions:
        cat = _val(t, 'nil_category')
        if not cat:
            cls = str(_val(t, 'classification_status') or _val(t, 'supply_type') or '').upper()
            if 'EXEMPT' in cls or _val(t, 'exempt_flag'):
                cat = 'EXEMPT'
            elif 'NONGST' in cls or 'NON_GST' in cls or _val(t, 'non_gst_flag'):
                cat = 'NONGST'
            else:
                cat = 'NIL'

        stype = _val(t, 'supply_type', 'INTRA') or 'INTRA'
        key = (cat, stype)
        if key not in aggregated:
            aggregated[key] = {
                'category': cat,
                'supply_type': stype,
                'registered_value': Decimal('0.00'),
                'unregistered_value': Decimal('0.00'),
                'total_value': Decimal('0.00'),
                'rule_version': GST_RULE_VERSION,
            }

        agg = aggregated[key]
        raw_val = _val(t, 'taxable_value') if _val(t, 'taxable_value') is not None else _val(t, 'invoice_value', 0)
        val = Decimal(str(raw_val or 0)).quantize(Decimal('0.01'))
        if _val(t, 'customer_gstin'):
            agg['registered_value'] = (agg['registered_value'] + val).quantize(Decimal('0.01'))
        else:
            agg['unregistered_value'] = (agg['unregistered_value'] + val).quantize(Decimal('0.01'))
        agg['total_value'] = (agg['registered_value'] + agg['unregistered_value']).quantize(Decimal('0.01'))

        if cat == 'NIL':
            tot_nil += val
        elif cat == 'EXEMPT':
            tot_exempt += val
        elif cat == 'NONGST':
            tot_nongst += val

    return NilAggregateResult(
        list(aggregated.values()),
        total_nil=tot_nil,
        total_exempt=tot_exempt,
        total_nongst=tot_nongst,
        rule_version=GST_RULE_VERSION
    )
