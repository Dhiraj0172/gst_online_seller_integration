"""Unit Quantity Codes for GST."""

UQC_CODES = {
    'BAG': 'BAGS',
    'BAL': 'BALE',
    'BDL': 'BUNDLES',
    'BKL': 'BUCKLES',
    'BOU': 'BILLION OF UNITS',
    'BOX': 'BOX',
    'BTL': 'BOTTLES',
    'BUN': 'BUNCHES',
    'CAN': 'CANS',
    'CBM': 'CUBIC METERS',
    'CCM': 'CUBIC CENTIMETERS',
    'CMS': 'CENTIMETERS',
    'CTN': 'CARTONS',
    'DOZ': 'DOZENS',
    'DRM': 'DRUMS',
    'GGK': 'GREAT GROSS',
    'GMS': 'GRAMS',
    'GRS': 'GROSS',
    'GYD': 'GROSS YARDS',
    'KGS': 'KILOGRAMS',
    'KLR': 'KILOLITERS',
    'KME': 'KILOMETERS',
    'LTR': 'LITERS',
    'MLS': 'MILLILITERS',
    'MLT': 'MILLILITERS',
    'MTR': 'METERS',
    'MTS': 'METRIC TON',
    'NOS': 'NUMBERS',
    'OTH': 'OTHERS',
    'PAC': 'PACKS',
    'PCS': 'PIECES',
    'PRS': 'PAIRS',
    'QTL': 'QUINTAL',
    'ROL': 'ROLLS',
    'SET': 'SETS',
    'SQF': 'SQUARE FEET',
    'SQM': 'SQUARE METERS',
    'SQY': 'SQUARE YARDS',
    'TBS': 'TABLETS',
    'TGM': 'TEN GROSS',
    'THD': 'THOUSANDS',
    'TON': 'TONNES',
    'TUB': 'TUBES',
    'UGS': 'US GALLONS',
    'UNT': 'UNITS',
    'YDS': 'YARDS'
}

def validate_uqc(code: str) -> bool:
    """Check if UQC code is valid."""
    if not code:
        return False
    return str(code).strip().upper() in UQC_CODES

def normalize_uqc(code: str) -> str:
    """Normalize UQC code to uppercase 3-letter format."""
    if not code:
        return "OTH"
    code = str(code).strip().upper()
    if code in UQC_CODES:
        return code
    return "OTH"

def get_uqc_description(code: str) -> str:
    """Get full description of a UQC code."""
    if not code:
        return "OTHERS"
    code = str(code).strip().upper()
    return UQC_CODES.get(code, "OTHERS")
