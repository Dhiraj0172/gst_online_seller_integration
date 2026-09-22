"""
GSTIN validation utilities.

Validates GSTIN format, state code, and structure.
The check digit algorithm for GSTIN is not reliably documented in official
public sources, so this module validates format and state code strictly
but treats checksum as a warning rather than a hard failure.
"""
import re
from app.utils.state_codes import is_valid_state


_GSTIN_PATTERN = re.compile(
    r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'
)


def validate_gstin_format(gstin: str) -> bool:
    """Validate GSTIN format using the official regex pattern.
    
    GSTIN format (15 chars):
      - Positions 1-2: State code (01-38, 96, 97)
      - Positions 3-12: PAN (10 chars)  
      - Position 13: Entity number (1-9, A-Z)
      - Position 14: 'Z' (default)
      - Position 15: Check digit (0-9, A-Z)
    """
    if not gstin or not isinstance(gstin, str):
        return False
    return bool(_GSTIN_PATTERN.match(gstin.strip().upper()))


def get_state_from_gstin(gstin: str) -> str:
    """Extract 2-digit state code from GSTIN."""
    if not gstin or len(gstin) < 2:
        return ""
    return gstin[:2]


def get_pan_from_gstin(gstin: str) -> str:
    """Extract 10-character PAN from GSTIN (characters 3-12)."""
    if not gstin or len(gstin) < 12:
        return ""
    return gstin[2:12]


def is_valid_state_code(code: str) -> bool:
    """Check if the state code is a valid Indian state/UT code."""
    return is_valid_state(code)


def validate_gstin(gstin: str) -> tuple:
    """Complete GSTIN validation.
    
    Validates:
      1. Non-empty string
      2. Exactly 15 characters  
      3. Matches the GSTIN regex pattern
      4. Valid state code
    
    Returns:
        tuple: (is_valid: bool, message: str)
    """
    if not gstin:
        return False, "GSTIN cannot be empty"
    
    if not isinstance(gstin, str):
        return False, "GSTIN must be a string"
    
    gstin = gstin.strip().upper()
    
    if len(gstin) != 15:
        return False, f"GSTIN must be exactly 15 characters, got {len(gstin)}"
    
    if not validate_gstin_format(gstin):
        return False, "Invalid GSTIN format. Expected: 2-digit state code + PAN + entity + Z + check digit"
    
    state_code = get_state_from_gstin(gstin)
    if not is_valid_state_code(state_code):
        return False, f"Invalid state code in GSTIN: {state_code}"
    
    return True, "Valid GSTIN"
