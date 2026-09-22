"""
GST Period-Aware Rules Engine.

Codifies known GST rule changes by return period, allowing the system to
apply the correct rules for any historical or current filing period.

Rule change log:
  - Pre Aug-2024: B2CL applicable for inter-state B2C > ₹2.5 lakhs (monthly filers)
  - Aug-2024 onwards: B2CL category eliminated; all B2C goes to B2CS
  - Pre May-2025: HSN reported as a single combined table
  - May-2025 onwards: HSN separately furnished for B2B and B2C
  - Pre 10-07-2024: TCS rate 1% (0.5% CGST + 0.5% SGST / 1% IGST)
  - 10-07-2024 onwards: TCS rate 0.5% (0.25% CGST + 0.25% SGST / 0.5% IGST)
"""
from decimal import Decimal
from datetime import datetime
from typing import Optional, Dict, Any


GST_RULE_VERSIONS = {
    'baseline': {
        'b2cl_applicable': True,
        'b2cl_threshold': Decimal('250000.00'),
        'hsn_reporting_mode': 'combined',
        'gstr1_schema_version': '1.0',
        'tcs_rate': Decimal('0.01'),  # 1% total
        'tcs_cgst_rate': Decimal('0.005'),  # 0.5%
        'tcs_sgst_rate': Decimal('0.005'),  # 0.5%
        'tcs_igst_rate': Decimal('0.01'),   # 1%
    },
    '2024-07-10': {
        # TCS rate change effective 10-07-2024 per Notification 15/2024-Central Tax
        'tcs_rate': Decimal('0.005'),      # 0.5% total
        'tcs_cgst_rate': Decimal('0.0025'), # 0.25%
        'tcs_sgst_rate': Decimal('0.0025'), # 0.25%
        'tcs_igst_rate': Decimal('0.005'),  # 0.5%
    },
    '2024-08': {
        'b2cl_applicable': False,
        'b2cl_threshold': None,
        'hsn_reporting_mode': 'combined',
        'gstr1_schema_version': '2.0',
    },
    '2025-05': {
        'b2cl_applicable': False,
        'b2cl_threshold': None,
        'hsn_reporting_mode': 'separate_b2b_b2c',
        'gstr1_schema_version': '3.0',
    },
}


TCS_RATE_CHANGE_DATE = datetime(2024, 7, 10)

#: TCS total rate in force before the 10-07-2024 rate change (1%).
GST_BASELINE_TCS_TOTAL_RATE = GST_RULE_VERSIONS['baseline']['tcs_rate']


def parse_period(return_period: str) -> datetime:
    """Parse return period string to datetime.

    Supports formats:
      - MMYYYY  (e.g. '012025' for January 2025)
      - YYYY-MM (e.g. '2025-01')
      - MM-YYYY (e.g. '01-2025')
    """
    rp = str(return_period).strip()

    # Try MMYYYY first (6 digits, no separator)
    if len(rp) == 6 and rp.isdigit():
        month = int(rp[:2])
        year = int(rp[2:])
        return datetime(year, month, 1)

    # Try YYYY-MM
    if len(rp) == 7 and '-' in rp:
        parts = rp.split('-')
        if len(parts[0]) == 4:
            return datetime(int(parts[0]), int(parts[1]), 1)
        elif len(parts[1]) == 4:
            # MM-YYYY
            return datetime(int(parts[1]), int(parts[0]), 1)

    raise ValueError(f"Cannot parse return period: '{return_period}'. Expected MMYYYY, YYYY-MM, or MM-YYYY.")


def get_rules_for_period(return_period: str) -> Dict[str, Any]:
    """Get the active GST rules for a given return period.

    Applies rule changes chronologically so that the latest applicable
    rule set wins.
    """
    period_date = parse_period(return_period)
    rules = GST_RULE_VERSIONS['baseline'].copy()

    # TCS rate change (10-07-2024)
    if period_date >= TCS_RATE_CHANGE_DATE:
        rules.update(GST_RULE_VERSIONS['2024-07-10'])

    aug_2024 = datetime(2024, 8, 1)
    may_2025 = datetime(2025, 5, 1)

    if period_date >= aug_2024:
        rules.update(GST_RULE_VERSIONS['2024-08'])
    if period_date >= may_2025:
        rules.update(GST_RULE_VERSIONS['2025-05'])

    return rules


def get_b2cl_threshold(return_period: str) -> Optional[Decimal]:
    """Get the B2CL invoice threshold for the period, or None if B2CL is eliminated."""
    return get_rules_for_period(return_period).get('b2cl_threshold')


def is_b2cl_applicable(return_period: str) -> bool:
    """Check if B2CL reporting is applicable for the return period."""
    return get_rules_for_period(return_period)['b2cl_applicable']


def get_hsn_reporting_mode(return_period: str) -> str:
    """Get HSN reporting mode: 'combined' or 'separate_b2b_b2c'."""
    return get_rules_for_period(return_period)['hsn_reporting_mode']


def get_gstr1_schema_version(return_period: str) -> str:
    """Get the GSTR-1 schema version string for the period."""
    return get_rules_for_period(return_period)['gstr1_schema_version']


def get_tcs_rates_for_period(return_period: str) -> Dict[str, Decimal]:
    """Get TCS rates for a given return period.

    Returns dict with keys: 'total', 'cgst', 'sgst', 'igst'

    Before 10-07-2024: 1% total (0.5% CGST + 0.5% SGST intra, 1% IGST inter)
    From 10-07-2024: 0.5% total (0.25% CGST + 0.25% SGST intra, 0.5% IGST inter)

    Source: Notification 15/2024-Central Tax dated 10-07-2024
    """
    rules = get_rules_for_period(return_period)
    return {
        'total': rules['tcs_rate'],
        'cgst': rules['tcs_cgst_rate'],
        'sgst': rules['tcs_sgst_rate'],
        'igst': rules['tcs_igst_rate'],
    }


def get_tcs_rates_for_date(supply_date) -> Dict[str, Decimal]:
    """Get the TCS rates applicable to a specific supply date.

    Notification 15/2024-Central Tax (dated 10-07-2024) changed the TCS rate
    mid-month, so a month-level rule cannot express July 2024 correctly:
    supplies up to 09-07-2024 use 1%, supplies from 10-07-2024 use 0.5%.

    Args:
        supply_date: a ``datetime``, ``date``, or ``'YYYY-MM-DD'`` string

    Returns:
        Dict with keys 'total', 'cgst', 'sgst', 'igst'
    """
    if supply_date is None:
        raise ValueError('supply_date is required to select a TCS rate')

    if isinstance(supply_date, str):
        parsed = datetime.strptime(supply_date.strip()[:10], '%Y-%m-%d')
    elif isinstance(supply_date, datetime):
        parsed = supply_date
    else:
        # datetime.date
        parsed = datetime(supply_date.year, supply_date.month, supply_date.day)

    rules = GST_RULE_VERSIONS['baseline'].copy()
    if parsed >= TCS_RATE_CHANGE_DATE:
        rules.update(GST_RULE_VERSIONS['2024-07-10'])

    return {
        'total': rules['tcs_rate'],
        'cgst': rules['tcs_cgst_rate'],
        'sgst': rules['tcs_sgst_rate'],
        'igst': rules['tcs_igst_rate'],
    }


def calculate_tcs_split(value_before: Decimal, value_from: Decimal,
                        supply_type: str) -> Dict[str, Decimal]:
    """Calculate TCS where a single period spans the 10-07-2024 rate change.

    Args:
        value_before: net taxable value of supplies dated before 10-07-2024 (1%)
        value_from: net taxable value of supplies dated on/after 10-07-2024 (0.5%)
        supply_type: 'INTRA' or 'INTER'

    Returns:
        Dict with 'cgst', 'sgst', 'igst', 'total' TCS amounts

    Source: CGST Act Section 52(1); Notification 15/2024-Central Tax (10-07-2024)
    """
    old_rates = GST_RULE_VERSIONS['baseline']
    new_rates = GST_RULE_VERSIONS['2024-07-10'].copy()
    # The TCS change version only overrides TCS keys; fall back for any others.
    for key in ('tcs_rate', 'tcs_cgst_rate', 'tcs_sgst_rate', 'tcs_igst_rate'):
        new_rates.setdefault(key, old_rates[key])

    value_before = Decimal(str(value_before or 0))
    value_from = Decimal(str(value_from or 0))

    if supply_type.upper() == 'INTRA':
        cgst = (value_before * old_rates['tcs_cgst_rate']).quantize(Decimal('0.01')) \
             + (value_from * new_rates['tcs_cgst_rate']).quantize(Decimal('0.01'))
        sgst = (value_before * old_rates['tcs_sgst_rate']).quantize(Decimal('0.01')) \
             + (value_from * new_rates['tcs_sgst_rate']).quantize(Decimal('0.01'))
        igst = Decimal('0.00')
    else:
        cgst = Decimal('0.00')
        sgst = Decimal('0.00')
        igst = (value_before * old_rates['tcs_igst_rate']).quantize(Decimal('0.01')) \
             + (value_from * new_rates['tcs_igst_rate']).quantize(Decimal('0.01'))

    return {'cgst': cgst, 'sgst': sgst, 'igst': igst, 'total': cgst + sgst + igst}


def calculate_tcs(net_taxable_value: Decimal, supply_type: str, return_period: str, tax_rate: Decimal = None) -> Dict[str, Decimal]:
    """Calculate TCS on net taxable value per Section 52.

    Args:
        net_taxable_value: Aggregate taxable supplies minus supplier returns (Section 52)
        supply_type: 'INTRA' or 'INTER' (intra-state or inter-state)
        return_period: Return period in MMYYYY format
        tax_rate: GST tax rate (if 0 or None, no TCS is applicable)

    Returns:
        Dict with 'cgst', 'sgst', 'igst', 'total' TCS amounts

    Source: CGST Act Section 52(1), Notification 15/2024-Central Tax
    """
    # No TCS on zero-rated / exempt / non-GST supplies (tax_rate = 0)
    if tax_rate is not None and Decimal(str(tax_rate)) == 0:
        return {
            'cgst': Decimal('0.00'),
            'sgst': Decimal('0.00'),
            'igst': Decimal('0.00'),
            'total': Decimal('0.00')
        }

    rates = get_tcs_rates_for_period(return_period)
    net_value = Decimal(str(net_taxable_value))

    if supply_type.upper() == 'INTRA':
        cgst = (net_value * rates['cgst']).quantize(Decimal('0.01'))
        sgst = (net_value * rates['sgst']).quantize(Decimal('0.01'))
        igst = Decimal('0.00')
    else:  # INTER
        cgst = Decimal('0.00')
        sgst = Decimal('0.00')
        igst = (net_value * rates['igst']).quantize(Decimal('0.01'))

    total = cgst + sgst + igst

    return {
        'cgst': cgst,
        'sgst': sgst,
        'igst': igst,
        'total': total
    }
