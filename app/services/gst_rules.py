"""
GST Period-Aware Rules Engine.

Codifies known GST rule changes by return period, allowing the system to
apply the correct rules for any historical or current filing period.

Rule change log:
  - Pre Aug-2024: B2CL applicable for inter-state B2C > ₹2.5 lakhs (monthly filers)
  - Aug-2024 onwards: B2CL category eliminated; all B2C goes to B2CS
  - Pre May-2025: HSN reported as a single combined table
  - May-2025 onwards: HSN separately furnished for B2B and B2C
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
