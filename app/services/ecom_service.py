from typing import List, Dict, Any
from decimal import Decimal
from app.services.b2b_service import ValidationResult
from app.models import Transaction, GSTProfile
from app.extensions import db
from datetime import datetime


def get_ecom_supplies(profile_id: int, return_period: str, include_undated: bool = False) -> List[Dict[str, Any]]:
    """Get e-commerce supplies for a profile and return period.

    Returns transactions where ecommerce_gstin is not null,
    filtered by the return period (invoice_date month/year).
    If include_undated is True, also includes transactions linked
    to this return period where invoice_date is NULL.
    """
    from app.models import ImportHistory
    from sqlalchemy import or_, and_
    period_date = datetime.strptime(return_period, '%m%Y')
    start_date = period_date.replace(day=1)
    if period_date.month == 12:
        end_date = start_date.replace(year=period_date.year + 1, month=1)
    else:
        end_date = start_date.replace(month=period_date.month + 1)

    query = Transaction.query.filter(
        Transaction.profile_id == profile_id,
        Transaction.ecommerce_gstin.isnot(None),
        Transaction.is_deleted == False
    )

    if include_undated:
        query = query.join(
            ImportHistory, Transaction.import_history_id == ImportHistory.id
        ).filter(
            or_(
                and_(Transaction.invoice_date >= start_date, Transaction.invoice_date < end_date),
                and_(ImportHistory.return_period == return_period, Transaction.invoice_date.is_(None))
            )
        )
    else:
        query = query.filter(
            Transaction.invoice_date >= start_date,
            Transaction.invoice_date < end_date
        )

    transactions = query.all()

    result = []
    for t in transactions:
        result.append({
            'id': t.id,
            'invoice_number': t.invoice_number,
            'invoice_date': t.invoice_date,
            'taxable_value': float(t.taxable_value or 0),
            'cgst': float(t.cgst_amount or 0),
            'sgst': float(t.sgst_amount or 0),
            'igst': float(t.igst_amount or 0),
            'cess': float(t.cess_amount or 0),
            'tax_rate': float(t.tax_rate or 0),
            'place_of_supply': t.place_of_supply,
            'ecommerce_gstin': t.ecommerce_gstin,
            'marketplace_name': t.marketplace_name,
            'supply_type': t.supply_type,
            'return_flag': t.return_flag,
            'return_reason': t.return_reason,
            'cancellation_flag': t.cancellation_flag,
            'note_type': t.note_type,
            'note_number': t.note_number,
            'customer_gstin': t.customer_gstin,
        })
    return result


def aggregate_ecom(transactions: List[Dict[str, Any]], return_period: str, seller_state: str = '27') -> List[Dict[str, Any]]:
    """Aggregate e-commerce supplies per Section 52 for TCS calculation.

    Groups by (ecommerce_gstin, state, rate) and computes:
    - Gross taxable value (all supplies)
    - Supplier returns (return_flag=True AND return_reason='SUPPLIER_RETURN')
    - Net taxable value = Gross - Supplier Returns
    - TCS on net taxable value using period-aware rates

    Other events (cancellation, credit_note, refund, debit_note) are tracked
    separately but do NOT affect Section 52 net value.

    Args:
        transactions: List of transaction dicts from get_ecom_supplies
        return_period: Period in MMYYYY format
        seller_state: State code of the supplier (our user), e.g., '27' for Maharashtra

    Returns:
        List of aggregated dicts with our_net_taxable_value and our_tcs_*
    """
    from app.services.gst_rules import (
        calculate_tcs_split, get_tcs_rates_for_date, GST_BASELINE_TCS_TOTAL_RATE,
    )
    from app.services.classification_service import determine_supply_type
    from datetime import date as _date, datetime as _dt

    from app.utils.date_utils import parse_date

    def _as_date(value):
        if value is None:
            return None
        if isinstance(value, _dt):
            return value.date()
        if isinstance(value, _date):
            return value
        if isinstance(value, str):
            parsed = parse_date(value)
            if parsed is not None:
                return parsed
            try:
                return _dt.strptime(value.strip()[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                return None
        return None

    aggregated = {}
    for t in transactions:
        ecom_gstin = t.get('ecommerce_gstin')
        state = t.get('place_of_supply')
        rate = Decimal(str(t.get('tax_rate', 0)))

        if not ecom_gstin or not state:
            continue

        key = (ecom_gstin, state, rate)

        if key not in aggregated:
            # Determine supply type (INTRA/INTER) for TCS calculation
            # seller_state is the state code of our user (the supplier)
            supply_type = determine_supply_type(
                '', ecom_gstin, state, seller_state
            )

            aggregated[key] = {
                'ecommerce_gstin': ecom_gstin,
                'state': state,
                'rate': rate,
                'supply_type': supply_type,
                # Gross value of genuine taxable supply rows only. Returns and
                # the other event types are tracked separately and are NOT part
                # of the supplies figure (CGST Act s.52(1)).
                'gross_taxable_value': Decimal('0.00'),
                'all_rows_value': Decimal('0.00'),
                'supplier_returns': Decimal('0.00'),
                'cancellations': Decimal('0.00'),
                'credit_notes': Decimal('0.00'),
                'refunds': Decimal('0.00'),
                'debit_notes': Decimal('0.00'),
                'net_taxable_value': Decimal('0.00'),
                # Section 52 base split across the 10-07-2024 TCS rate change
                'gross_before': Decimal('0.00'),
                'gross_from': Decimal('0.00'),
                'returns_before': Decimal('0.00'),
                'returns_from': Decimal('0.00'),
                # Rows with no usable date: excluded from the rate-bucketed base
                # and surfaced instead of being assigned a guessed rate.
                'undated_supplies': Decimal('0.00'),
                'undated_supplier_returns': Decimal('0.00'),
                'has_undated_supplies': False,
                'splits_tcs_rate_periods': False,
                'cgst': Decimal('0.00'),
                'sgst': Decimal('0.00'),
                'igst': Decimal('0.00'),
                'cess': Decimal('0.00'),
            }

        agg = aggregated[key]
        taxable = Decimal(str(t.get('taxable_value', 0)))
        supply_dt = _as_date(t.get('invoice_date'))

        # Tax amounts and the informational all-rows total span every row
        agg['all_rows_value'] += taxable
        agg['cgst'] += Decimal(str(t.get('cgst', 0)))
        agg['sgst'] += Decimal(str(t.get('sgst', 0)))
        agg['igst'] += Decimal(str(t.get('igst', 0)))
        agg['cess'] += Decimal(str(t.get('cess', 0)))

        # Classify the row as an auditable event type. Only SUPPLIER_RETURN
        # moves the Section 52 net value; cancellations, credit notes, refunds
        # and debit notes are recorded for audit and deliberately do NOT
        # affect the statutory net value.
        return_flag = t.get('return_flag', False)
        return_reason = t.get('return_reason', '')
        note_type = t.get('note_type', '')

        if return_flag and return_reason == 'SUPPLIER_RETURN':
            event_type = 'SUPPLIER_RETURN'
        elif return_flag and return_reason == 'CANCELLATION':
            event_type = 'CANCELLATION'
        elif note_type == 'CREDIT' or return_reason == 'CREDIT_NOTE':
            event_type = 'CREDIT_NOTE'
        elif return_reason == 'REFUND':
            event_type = 'REFUND'
        elif note_type == 'DEBIT' or return_reason == 'DEBIT_NOTE':
            event_type = 'DEBIT_NOTE'
        else:
            event_type = None  # genuine taxable supply

        if event_type == 'SUPPLIER_RETURN':
            agg['supplier_returns'] += taxable
        elif event_type == 'CANCELLATION':
            agg['cancellations'] += taxable
        elif event_type == 'CREDIT_NOTE':
            agg['credit_notes'] += taxable
        elif event_type == 'REFUND':
            agg['refunds'] += taxable
        elif event_type == 'DEBIT_NOTE':
            agg['debit_notes'] += taxable
        else:
            agg['gross_taxable_value'] += taxable

        # Bucket the Section 52 base by the applicable TCS rate period.
        if supply_dt is None:
            # No usable date -> a defensible rate cannot be chosen. Record the
            # row and flag it rather than silently applying a guessed rate.
            agg['has_undated_supplies'] = True
            if event_type == 'SUPPLIER_RETURN':
                agg['undated_supplier_returns'] += taxable
            else:
                agg['undated_supplies'] += taxable
            continue

        rates = get_tcs_rates_for_date(supply_dt)
        at_or_after_change = rates['total'] != GST_BASELINE_TCS_TOTAL_RATE

        if event_type == 'SUPPLIER_RETURN':
            if at_or_after_change:
                agg['returns_from'] += taxable
            else:
                agg['returns_before'] += taxable
        elif event_type is None:
            # Genuine taxable supply -> forms the Section 52 supplies base.
            if at_or_after_change:
                agg['gross_from'] += taxable
            else:
                agg['gross_before'] += taxable
        # Cancellations, credit notes, refunds and debit notes deliberately do
        # NOT enter the Section 52 base: they are audited above and left out.

    # Compute net taxable value and TCS for each group
    result = []
    for key, agg in aggregated.items():
        # Section 52 net value: genuine taxable supplies less supplier returns.
        net_before = agg['gross_before'] - agg['returns_before']
        net_from = agg['gross_from'] - agg['returns_from']
        net_value = net_before + net_from
        agg['net_taxable_value'] = net_value
        agg['net_value_before_rate_change'] = net_before
        agg['net_value_from_rate_change'] = net_from
        agg['splits_tcs_rate_periods'] = bool(net_before != 0 and net_from != 0)

        # Calculate TCS on net taxable value at the rate applicable to each date
        supply_type = agg.get('supply_type', 'INTER')
        rate = agg.get('rate', Decimal('0'))
        if rate is None or Decimal(str(rate)) == 0:
            # No TCS on zero-rated / exempt / non-GST supplies
            tcs = {'cgst': Decimal('0.00'), 'sgst': Decimal('0.00'),
                   'igst': Decimal('0.00'), 'total': Decimal('0.00')}
        else:
            tcs = calculate_tcs_split(net_before, net_from, supply_type)
        agg['our_tcs_cgst'] = tcs['cgst']
        agg['our_tcs_sgst'] = tcs['sgst']
        agg['our_tcs_igst'] = tcs['igst']
        agg['our_tcs_total'] = tcs['total']

        # Add state code for matching
        agg['state_code'] = agg['state'][:2] if len(agg['state']) >= 2 else agg['state']

        result.append(agg)

    return result


def validate_ecom(data: List[Dict[str, Any]]) -> ValidationResult:
    errors = []
    return ValidationResult(len(errors) == 0, errors)
