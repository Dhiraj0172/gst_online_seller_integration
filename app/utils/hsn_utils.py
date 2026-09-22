import re

def validate_hsn(hsn: str) -> tuple[bool, str]:
    """Validate HSN code format (2, 4, 6, or 8 digits)."""
    if not hsn:
        return False, "HSN cannot be empty"
    hsn = str(hsn).strip()
    if not re.match(r'^\d+$', hsn):
        return False, "HSN must contain only digits"
    if len(hsn) not in (2, 4, 6, 8):
        return False, "HSN must be exactly 2, 4, 6, or 8 digits long"
    return True, "Valid HSN"

def validate_sac(sac: str) -> tuple[bool, str]:
    """Validate SAC code format (starts with 99, 6 digits long)."""
    if not sac:
        return False, "SAC cannot be empty"
    sac = str(sac).strip()
    if not re.match(r'^99\d{4}$', sac):
        return False, "SAC must be 6 digits and start with 99"
    return True, "Valid SAC"

def normalize_hsn(hsn: str) -> str:
    """Normalize HSN code (strip whitespace, ensure even length)."""
    if not hsn:
        return ""
    hsn = str(hsn).strip()
    if len(hsn) % 2 != 0:
        hsn = '0' + hsn
    return hsn

def is_hsn_or_sac(code: str) -> str:
    """Identify if a code is HSN or SAC."""
    code = str(code).strip()
    if validate_sac(code)[0]:
        return 'SAC'
    if validate_hsn(code)[0]:
        return 'HSN'
    return 'UNKNOWN'
