"""Indian State Codes for GST."""

STATE_CODES = {
    '01': 'Jammu & Kashmir',
    '02': 'Himachal Pradesh',
    '03': 'Punjab',
    '04': 'Chandigarh',
    '05': 'Uttarakhand',
    '06': 'Haryana',
    '07': 'Delhi',
    '08': 'Rajasthan',
    '09': 'Uttar Pradesh',
    '10': 'Bihar',
    '11': 'Sikkim',
    '12': 'Arunachal Pradesh',
    '13': 'Nagaland',
    '14': 'Manipur',
    '15': 'Mizoram',
    '16': 'Tripura',
    '17': 'Meghalaya',
    '18': 'Assam',
    '19': 'West Bengal',
    '20': 'Jharkhand',
    '21': 'Odisha',
    '22': 'Chhattisgarh',
    '23': 'Madhya Pradesh',
    '24': 'Gujarat',
    '25': 'Daman & Diu',
    '26': 'Dadra & Nagar Haveli',
    '27': 'Maharashtra',
    '28': 'Andhra Pradesh (old)',
    '29': 'Karnataka',
    '30': 'Goa',
    '31': 'Lakshadweep',
    '32': 'Kerala',
    '33': 'Tamil Nadu',
    '34': 'Puducherry',
    '35': 'Andaman & Nicobar',
    '36': 'Telangana',
    '37': 'Andhra Pradesh',
    '38': 'Ladakh',
    '96': 'Foreign Country',
    '97': 'Other Territory'
}

STATE_NAMES = {v.lower(): k for k, v in STATE_CODES.items()}

def get_state_code(name_or_code: str) -> str:
    """Get state code from state name or code."""
    if not name_or_code:
        return ""
    name_or_code = str(name_or_code).strip()
    if name_or_code in STATE_CODES:
        return name_or_code
    return STATE_NAMES.get(name_or_code.lower(), "")

def get_state_name(code: str) -> str:
    """Get state name from state code."""
    if not code:
        return ""
    return STATE_CODES.get(str(code).strip().zfill(2), "")

def is_valid_state(code_or_name: str) -> bool:
    """Check if the given state code or name is valid."""
    if not code_or_name:
        return False
    code_or_name = str(code_or_name).strip()
    return code_or_name in STATE_CODES or code_or_name.lower() in STATE_NAMES

def resolve_pos_code(pos_str: str, customer_gstin: str = None, fallback_code: str = "27") -> str:
    """Resolve a valid 2-digit GST state code from various POS representations or customer GSTIN."""
    if customer_gstin and len(customer_gstin) >= 2 and customer_gstin[:2].isdigit():
        return customer_gstin[:2]
    if not pos_str:
        return fallback_code
    pos_str = str(pos_str).strip()
    if len(pos_str) >= 2 and pos_str[:2].isdigit():
        return pos_str[:2]
    code = get_state_code(pos_str)
    if code:
        return code
    for part in pos_str.split('-'):
        part = part.strip()
        code = get_state_code(part)
        if code:
            return code
        if len(part) == 2 and part.isdigit():
            return part
    return fallback_code
