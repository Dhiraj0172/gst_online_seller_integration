"""
GSTR-3B Auto-Draft Bridge, Section 52 TCS Summary & Rule 88A Tax Offset Priority.

Implements:
1. GSTR-3B Table 3.1 Outward & Inward Supplies Computation:
   - 3.1(a): Outward taxable supplies (other than zero rated, nil rated, and exempted)
   - 3.1(b): Outward taxable supplies (zero rated) - Export & SEZ
   - 3.1(c): Other outward supplies (Nil rated, exempted)
   - 3.1(d): Inward supplies liable to reverse charge
   - 3.1(e): Non-GST outward supplies
2. Section 52 TCS Credit Summary:
   - E-commerce gross supplies, returns, net taxable values, and credited TCS (IGST/CGST/SGST).
3. Statutory Rule 88A / Section 49 Tax Liability Offset Priority:
   - Strict statutory utilization order:
     * Step 1: Exhaust IGST ITC fully against IGST liability first, then CGST, then SGST.
     * Step 2: Utilize CGST ITC against CGST liability, then IGST liability. (NEVER SGST).
     * Step 3: Utilize SGST ITC against SGST liability, then IGST liability. (NEVER CGST).
     * Step 4: Utilize Cess ITC against Cess liability only.
     * Step 5: Remaining balances computed in cash payable and ITC carried forward.
   - Pure Decimal arithmetic with zero float precision errors.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional, Tuple
from app.extensions import db
from app.models import Transaction, GSTProfile, ImportHistory, TCSReconciliation
from app.utils.query import tenant_query
from app.services.classification_service import (
    determine_taxability,
    TAXABILITY_TAXABLE,
    TAXABILITY_NIL_RATED,
    TAXABILITY_EXEMPT,
    TAXABILITY_NON_GST,
)


def to_dec(val: Any) -> Decimal:
    """Safely convert any numeric/string value to 2-decimal place Decimal."""
    if val is None:
        return Decimal('0.00')
    if isinstance(val, Decimal):
        return val.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    try:
        s = str(val).replace(',', '').strip()
        if not s:
            return Decimal('0.00')
        return Decimal(s).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal('0.00')


def calculate_gst_liability_offset(liability: Dict[str, Any], itc: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate GST liability offset under statutory Rule 88A & Section 49.

    Statutory Offset Priority:
    1. IGST ITC must be completely exhausted before CGST ITC or SGST ITC can be utilized.
       - IGST ITC -> IGST liability first.
       - Remaining IGST ITC -> CGST liability.
       - Remaining IGST ITC -> SGST liability.
    2. CGST ITC is utilized:
       - CGST ITC -> CGST liability first.
       - Remaining CGST ITC -> IGST liability.
       - STRICT PROHIBITION: CGST ITC CAN NEVER BE USED TO OFFSET SGST LIABILITY.
    3. SGST ITC is utilized:
       - SGST ITC -> SGST liability first.
       - Remaining SGST ITC -> IGST liability.
       - STRICT PROHIBITION: SGST ITC CAN NEVER BE USED TO OFFSET CGST LIABILITY.
    4. Cess ITC is utilized:
       - Cess ITC -> Cess liability only.
    5. Pure Decimal computation throughout.

    Args:
        liability: dict with keys 'igst', 'cgst', 'sgst', 'cess'
        itc: dict with keys 'igst', 'cgst', 'sgst', 'cess'

    Returns:
        dict containing liability_before, itc_available, offset_applied,
        liability_payable_cash, and itc_balance_remaining.
    """
    # 1. Parse into Decimals
    l_igst = max(Decimal('0.00'), to_dec(liability.get('igst', 0)))
    l_cgst = max(Decimal('0.00'), to_dec(liability.get('cgst', 0)))
    l_sgst = max(Decimal('0.00'), to_dec(liability.get('sgst', 0)))
    l_cess = max(Decimal('0.00'), to_dec(liability.get('cess', 0)))

    c_igst = max(Decimal('0.00'), to_dec(itc.get('igst', 0)))
    c_cgst = max(Decimal('0.00'), to_dec(itc.get('cgst', 0)))
    c_sgst = max(Decimal('0.00'), to_dec(itc.get('sgst', 0)))
    c_cess = max(Decimal('0.00'), to_dec(itc.get('cess', 0)))

    # Snapshots before offset
    liability_before = {
        'igst': l_igst,
        'cgst': l_cgst,
        'sgst': l_sgst,
        'cess': l_cess,
        'total': (l_igst + l_cgst + l_sgst + l_cess).quantize(Decimal('0.01')),
    }
    itc_available = {
        'igst': c_igst,
        'cgst': c_cgst,
        'sgst': c_sgst,
        'cess': c_cess,
        'total': (c_igst + c_cgst + c_sgst + c_cess).quantize(Decimal('0.01')),
    }

    # Tracking remaining amounts
    rem_l_igst = l_igst
    rem_l_cgst = l_cgst
    rem_l_sgst = l_sgst
    rem_l_cess = l_cess

    rem_c_igst = c_igst
    rem_c_cgst = c_cgst
    rem_c_sgst = c_sgst
    rem_c_cess = c_cess

    # Step 1: Exhaust IGST ITC fully
    # 1a: IGST ITC -> IGST liability
    used_igst_for_igst = min(rem_c_igst, rem_l_igst)
    rem_c_igst -= used_igst_for_igst
    rem_l_igst -= used_igst_for_igst

    # 1b: Remaining IGST ITC -> CGST liability
    used_igst_for_cgst = min(rem_c_igst, rem_l_cgst)
    rem_c_igst -= used_igst_for_cgst
    rem_l_cgst -= used_igst_for_cgst

    # 1c: Remaining IGST ITC -> SGST liability
    used_igst_for_sgst = min(rem_c_igst, rem_l_sgst)
    rem_c_igst -= used_igst_for_sgst
    rem_l_sgst -= used_igst_for_sgst

    # Step 2: Utilize CGST ITC (only after IGST ITC is completely utilized)
    # 2a: CGST ITC -> CGST liability
    used_cgst_for_cgst = min(rem_c_cgst, rem_l_cgst)
    rem_c_cgst -= used_cgst_for_cgst
    rem_l_cgst -= used_cgst_for_cgst

    # 2b: Remaining CGST ITC -> IGST liability
    used_cgst_for_igst = min(rem_c_cgst, rem_l_igst)
    rem_c_cgst -= used_cgst_for_igst
    rem_l_igst -= used_cgst_for_igst

    # 2c: CGST ITC -> SGST liability is 0 (Statutory Prohibition)
    used_cgst_for_sgst = Decimal('0.00')

    # Step 3: Utilize SGST ITC
    # 3a: SGST ITC -> SGST liability
    used_sgst_for_sgst = min(rem_c_sgst, rem_l_sgst)
    rem_c_sgst -= used_sgst_for_sgst
    rem_l_sgst -= used_sgst_for_sgst

    # 3b: Remaining SGST ITC -> IGST liability
    used_sgst_for_igst = min(rem_c_sgst, rem_l_igst)
    rem_c_sgst -= used_sgst_for_igst
    rem_l_igst -= used_sgst_for_igst

    # 3c: SGST ITC -> CGST liability is 0 (Statutory Prohibition)
    used_sgst_for_cgst = Decimal('0.00')

    # Step 4: Utilize Cess ITC
    used_cess_for_cess = min(rem_c_cess, rem_l_cess)
    rem_c_cess -= used_cess_for_cess
    rem_l_cess -= used_cess_for_cess

    offset_applied = {
        'igst_from_igst': used_igst_for_igst.quantize(Decimal('0.01')),
        'cgst_from_igst': used_igst_for_cgst.quantize(Decimal('0.01')),
        'sgst_from_igst': used_igst_for_sgst.quantize(Decimal('0.01')),
        'cgst_from_cgst': used_cgst_for_cgst.quantize(Decimal('0.01')),
        'igst_from_cgst': used_cgst_for_igst.quantize(Decimal('0.01')),
        'cgst_from_sgst': used_cgst_for_sgst.quantize(Decimal('0.01')),
        'sgst_from_sgst': used_sgst_for_sgst.quantize(Decimal('0.01')),
        'igst_from_sgst': used_sgst_for_igst.quantize(Decimal('0.01')),
        'sgst_from_cgst': used_sgst_for_cgst.quantize(Decimal('0.01')),
        'cess_from_cess': used_cess_for_cess.quantize(Decimal('0.01')),
        'total_offset': (
            used_igst_for_igst + used_igst_for_cgst + used_igst_for_sgst +
            used_cgst_for_cgst + used_cgst_for_igst +
            used_sgst_for_sgst + used_sgst_for_igst +
            used_cess_for_cess
        ).quantize(Decimal('0.01')),
    }

    liability_payable_cash = {
        'igst': rem_l_igst.quantize(Decimal('0.01')),
        'cgst': rem_l_cgst.quantize(Decimal('0.01')),
        'sgst': rem_l_sgst.quantize(Decimal('0.01')),
        'cess': rem_l_cess.quantize(Decimal('0.01')),
        'total': (rem_l_igst + rem_l_cgst + rem_l_sgst + rem_l_cess).quantize(Decimal('0.01')),
    }

    itc_balance_remaining = {
        'igst': rem_c_igst.quantize(Decimal('0.01')),
        'cgst': rem_c_cgst.quantize(Decimal('0.01')),
        'sgst': rem_c_sgst.quantize(Decimal('0.01')),
        'cess': rem_c_cess.quantize(Decimal('0.01')),
        'total': (rem_c_igst + rem_c_cgst + rem_c_sgst + rem_c_cess).quantize(Decimal('0.01')),
    }

    return {
        'statutory_rule': 'Rule 88A & Section 49',
        'liability_before': liability_before,
        'itc_available': itc_available,
        'offset_applied': offset_applied,
        'liability_payable_cash': liability_payable_cash,
        'itc_balance_remaining': itc_balance_remaining,
    }


def get_gstr3b_table_3_1(profile_id: int, return_period: str) -> Dict[str, Any]:
    """Calculate GSTR-3B Table 3.1 details for outward & inward supplies.

    Excludes staged (is_staged=True) and soft-deleted (is_deleted=True) transactions.
    Nets credit notes (reduces liability/taxable value) and debit notes.
    """
    transactions = tenant_query(Transaction, int(profile_id)).join(
        ImportHistory, Transaction.import_history_id == ImportHistory.id
    ).filter(
        Transaction.is_deleted == False,
        Transaction.is_staged == False,
        ImportHistory.processing_status.in_(['COMPLETED', 'PARTIAL']),
        ImportHistory.return_period == return_period
    ).all()

    # Accumulators in Decimal
    row_a = {'taxable_value': Decimal('0.00'), 'igst': Decimal('0.00'), 'cgst': Decimal('0.00'), 'sgst': Decimal('0.00'), 'cess': Decimal('0.00')}
    row_b = {'taxable_value': Decimal('0.00'), 'igst': Decimal('0.00'), 'cgst': Decimal('0.00'), 'sgst': Decimal('0.00'), 'cess': Decimal('0.00')}
    row_c = {'taxable_value': Decimal('0.00'), 'igst': Decimal('0.00'), 'cgst': Decimal('0.00'), 'sgst': Decimal('0.00'), 'cess': Decimal('0.00')}
    row_d = {'taxable_value': Decimal('0.00'), 'igst': Decimal('0.00'), 'cgst': Decimal('0.00'), 'sgst': Decimal('0.00'), 'cess': Decimal('0.00')}
    row_e = {'taxable_value': Decimal('0.00'), 'igst': Decimal('0.00'), 'cgst': Decimal('0.00'), 'sgst': Decimal('0.00'), 'cess': Decimal('0.00')}

    for tx in transactions:
        txval = to_dec(tx.taxable_value)
        igst = to_dec(tx.igst_amount)
        cgst = to_dec(tx.cgst_amount)
        sgst = to_dec(tx.sgst_amount)
        cess = to_dec(tx.cess_amount)

        # Netting sign: Credit Note reduces outward supply; Invoices & Debit notes increase
        is_credit = False
        if tx.note_type and tx.note_type.upper().startswith('C'):
            is_credit = True
        elif tx.supply_type in ('CDNR', 'CDNUR', 'CDNRA', 'CDNURA') and (not tx.note_type or not tx.note_type.upper().startswith('D')):
            is_credit = True
        elif tx.return_flag or tx.return_reason in ('SUPPLIER_RETURN', 'CREDIT_NOTE'):
            is_credit = True

        sign = Decimal('-1.00') if is_credit else Decimal('1.00')

        st = (tx.supply_type or '').upper()
        cs = (getattr(tx, 'classification_status', None) or '').upper()
        taxability, _ = determine_taxability(tx)

        # 3.1(b): Zero-rated outward supplies (Export & SEZ)
        if st in ('EXPORT', 'EXPORTS', 'SEZ', 'SEZ_WP', 'SEZ_WOP', 'EXPA') or cs in ('EXPORT', 'EXPORTS', 'SEZ', 'SEZ_REGISTERED', 'SEZ_UNREGISTERED'):
            row_b['taxable_value'] += sign * txval
            row_b['igst'] += sign * igst
            row_b['cess'] += sign * cess

        # 3.1(e): Non-GST outward supplies
        elif tx.non_gst_flag or st in ('NONGST', 'NON_GST', 'NONGST_REGISTERED', 'NONGST_UNREGISTERED') or taxability == TAXABILITY_NON_GST:
            row_e['taxable_value'] += sign * txval

        # 3.1(c): Other outward supplies (Nil rated, exempted)
        elif tx.nil_rated_flag or tx.exempt_flag or st in ('NIL', 'EXEMPT', 'NIL_REGISTERED', 'NIL_UNREGISTERED', 'EXEMPT_REGISTERED', 'EXEMPT_UNREGISTERED') or taxability in (TAXABILITY_NIL_RATED, TAXABILITY_EXEMPT):
            row_c['taxable_value'] += sign * txval

        # 3.1(d): Inward supplies liable to reverse charge
        elif getattr(tx, 'is_inward_supply', False) and tx.reverse_charge == 'Y':
            row_d['taxable_value'] += sign * txval
            row_d['igst'] += sign * igst
            row_d['cgst'] += sign * cgst
            row_d['sgst'] += sign * sgst
            row_d['cess'] += sign * cess

        # 3.1(a): Standard outward taxable supplies
        else:
            row_a['taxable_value'] += sign * txval
            row_a['igst'] += sign * igst
            row_a['cgst'] += sign * cgst
            row_a['sgst'] += sign * sgst
            row_a['cess'] += sign * cess

    def _pack_row(desc: str, r: dict) -> dict:
        total_tax = (r['igst'] + r['cgst'] + r['sgst'] + r['cess']).quantize(Decimal('0.01'))
        return {
            'description': desc,
            'taxable_value': r['taxable_value'].quantize(Decimal('0.01')),
            'igst': r['igst'].quantize(Decimal('0.01')),
            'cgst': r['cgst'].quantize(Decimal('0.01')),
            'sgst': r['sgst'].quantize(Decimal('0.01')),
            'cess': r['cess'].quantize(Decimal('0.01')),
            'total_tax': total_tax,
        }

    table_3_1 = {
        '3.1(a)': _pack_row('Outward taxable supplies (other than zero rated, nil rated and exempted)', row_a),
        '3.1(b)': _pack_row('Outward taxable supplies (zero rated)', row_b),
        '3.1(c)': _pack_row('Other outward supplies (Nil rated, exempted)', row_c),
        '3.1(d)': _pack_row('Inward supplies liable to reverse charge', row_d),
        '3.1(e)': _pack_row('Non-GST outward supplies', row_e),
    }

    # Total outward supplies liability
    total_taxable = (row_a['taxable_value'] + row_b['taxable_value'] + row_c['taxable_value'] + row_d['taxable_value'] + row_e['taxable_value']).quantize(Decimal('0.01'))
    total_igst = (row_a['igst'] + row_b['igst'] + row_d['igst']).quantize(Decimal('0.01'))
    total_cgst = (row_a['cgst'] + row_d['cgst']).quantize(Decimal('0.01'))
    total_sgst = (row_a['sgst'] + row_d['sgst']).quantize(Decimal('0.01'))
    total_cess = (row_a['cess'] + row_b['cess'] + row_d['cess']).quantize(Decimal('0.01'))
    total_tax = (total_igst + total_cgst + total_sgst + total_cess).quantize(Decimal('0.01'))

    return {
        'profile_id': profile_id,
        'return_period': return_period,
        'table_3_1': table_3_1,
        'total_taxable_value': total_taxable,
        'total_liability': {
            'igst': total_igst,
            'cgst': total_cgst,
            'sgst': total_sgst,
            'cess': total_cess,
            'total': total_tax,
        },
    }


def get_section_52_tcs_summary(profile_id: int, return_period: str) -> Dict[str, Any]:
    """Calculate Section 52 TCS credit summary for supplies through e-commerce operators.

    Summarizes:
    - Gross e-commerce taxable sales
    - E-commerce returns and cancellations
    - Net taxable value under Section 52
    - TCS credit claimed/credited (IGST, CGST, SGST)
    - Operator-wise breakdown
    """
    transactions = tenant_query(Transaction, int(profile_id)).join(
        ImportHistory, Transaction.import_history_id == ImportHistory.id
    ).filter(
        Transaction.is_deleted == False,
        Transaction.is_staged == False,
        ImportHistory.processing_status.in_(['COMPLETED', 'PARTIAL']),
        ImportHistory.return_period == return_period,
        Transaction.ecommerce_gstin.isnot(None),
        Transaction.ecommerce_gstin != ''
    ).all()

    gross_taxable = Decimal('0.00')
    returns_taxable = Decimal('0.00')
    tcs_igst = Decimal('0.00')
    tcs_cgst = Decimal('0.00')
    tcs_sgst = Decimal('0.00')

    operators = {}

    for tx in transactions:
        txval = to_dec(tx.taxable_value)
        igst_amt = to_dec(tx.igst_amount)
        cgst_amt = to_dec(tx.cgst_amount)
        sgst_amt = to_dec(tx.sgst_amount)

        is_ret = (
            tx.return_flag or
            (tx.note_type and tx.note_type.upper().startswith('C')) or
            tx.supply_type in ('CDNR', 'CDNUR', 'CDNRA', 'CDNURA') or
            tx.return_reason in ('SUPPLIER_RETURN', 'CREDIT_NOTE')
        )

        eco_gstin = (tx.ecommerce_gstin or 'UNKNOWN').strip()
        if eco_gstin not in operators:
            operators[eco_gstin] = {
                'ecommerce_gstin': eco_gstin,
                'marketplace_name': tx.marketplace_name or tx.source_platform or 'E-Commerce',
                'gross_taxable_value': Decimal('0.00'),
                'returns_taxable_value': Decimal('0.00'),
                'net_taxable_value': Decimal('0.00'),
                'tcs_igst': Decimal('0.00'),
                'tcs_cgst': Decimal('0.00'),
                'tcs_sgst': Decimal('0.00'),
                'total_tcs': Decimal('0.00'),
            }

        op = operators[eco_gstin]
        if is_ret:
            returns_taxable += txval
            op['returns_taxable_value'] += txval
        else:
            gross_taxable += txval
            op['gross_taxable_value'] += txval

    # Also check TCSReconciliation table if available for actual portal certificate TCS credits
    portal_recs = tenant_query(TCSReconciliation, int(profile_id)).filter(
        TCSReconciliation.return_period == return_period
    ).all()

    if portal_recs:
        for r in portal_recs:
            r_igst = to_dec(r.igst_tcs or r.portal_igst_tcs or 0)
            r_cgst = to_dec(r.cgst_tcs or r.portal_cgst_tcs or 0)
            r_sgst = to_dec(r.sgst_tcs or r.portal_sgst_tcs or 0)
            tcs_igst += r_igst
            tcs_cgst += r_cgst
            tcs_sgst += r_sgst
            eco = (r.ecommerce_gstin or 'PORTAL').strip()
            if eco in operators:
                operators[eco]['tcs_igst'] += r_igst
                operators[eco]['tcs_cgst'] += r_cgst
                operators[eco]['tcs_sgst'] += r_sgst
                operators[eco]['total_tcs'] += (r_igst + r_cgst + r_sgst)
    else:
        # Default standard statutory TCS rate on net taxable supplies: 0.5% (or 1% pre 10-07-2024)
        for eco, op in operators.items():
            op['net_taxable_value'] = (op['gross_taxable_value'] - op['returns_taxable_value']).quantize(Decimal('0.01'))
            if op['net_taxable_value'] > Decimal('0.00'):
                # Default 0.25% CGST + 0.25% SGST or 0.5% IGST
                calc_cgst = (op['net_taxable_value'] * Decimal('0.0025')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                calc_sgst = (op['net_taxable_value'] * Decimal('0.0025')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                op['tcs_cgst'] = calc_cgst
                op['tcs_sgst'] = calc_sgst
                op['total_tcs'] = (calc_cgst + calc_sgst).quantize(Decimal('0.01'))
                tcs_cgst += calc_cgst
                tcs_sgst += calc_sgst

    net_taxable = (gross_taxable - returns_taxable).quantize(Decimal('0.01'))
    total_tcs = (tcs_igst + tcs_cgst + tcs_sgst).quantize(Decimal('0.01'))

    return {
        'profile_id': profile_id,
        'return_period': return_period,
        'gross_taxable_value': gross_taxable.quantize(Decimal('0.01')),
        'returns_taxable_value': returns_taxable.quantize(Decimal('0.01')),
        'net_taxable_value': net_taxable,
        'tcs_credit': {
            'igst': tcs_igst.quantize(Decimal('0.01')),
            'cgst': tcs_cgst.quantize(Decimal('0.01')),
            'sgst': tcs_sgst.quantize(Decimal('0.01')),
            'total': total_tcs,
        },
        'operator_breakdown': list(operators.values()),
    }


def generate_gstr3b_summary(profile_id: int, return_period: str, itc_available: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Generate complete GSTR-3B auto-draft package including Table 3.1, TCS summary, and Rule 88A offset."""
    profile = db.session.get(GSTProfile, int(profile_id))
    gstin = profile.gstin if profile else ""

    t31 = get_gstr3b_table_3_1(profile_id, return_period)
    tcs = get_section_52_tcs_summary(profile_id, return_period)

    # Use provided ITC or TCS credit as available credit for offset
    effective_itc = itc_available or {
        'igst': tcs['tcs_credit']['igst'],
        'cgst': tcs['tcs_credit']['cgst'],
        'sgst': tcs['tcs_credit']['sgst'],
        'cess': Decimal('0.00'),
    }

    offset_result = calculate_gst_liability_offset(t31['total_liability'], effective_itc)

    def _dec_to_float_or_str(val):
        """Helper to serialize Decimals for JSON response."""
        if isinstance(val, Decimal):
            return str(val)
        if isinstance(val, dict):
            return {k: _dec_to_float_or_str(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_dec_to_float_or_str(v) for v in val]
        return val

    raw_summary = {
        'gstin': gstin,
        'profile_id': profile_id,
        'return_period': return_period,
        'table_3_1': t31['table_3_1'],
        'total_taxable_value': t31['total_taxable_value'],
        'total_liability': t31['total_liability'],
        'section_52_tcs': tcs,
        'tax_offset_ledger': offset_result,
    }

    return _dec_to_float_or_str(raw_summary)
