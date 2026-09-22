from decimal import Decimal, ROUND_HALF_UP

VALID_GST_RATES = [Decimal('0'), Decimal('0.1'), Decimal('0.25'), Decimal('1'), 
                   Decimal('1.5'), Decimal('3'), Decimal('5'), Decimal('6'), 
                   Decimal('7.5'), Decimal('12'), Decimal('18'), Decimal('28')]

def gst_round(value, precision=2) -> Decimal:
    """Round using ROUND_HALF_UP with given precision."""
    if value is None:
        return Decimal('0.00')
    val = Decimal(str(value))
    quantizer = Decimal('0.' + '0' * precision)
    return val.quantize(quantizer, rounding=ROUND_HALF_UP)

def calculate_cgst(taxable_value, rate) -> Decimal:
    """Calculate CGST (rate / 2)."""
    tv = Decimal(str(taxable_value or 0))
    r = Decimal(str(rate or 0))
    return gst_round(tv * (r / Decimal('200')))

def calculate_sgst(taxable_value, rate) -> Decimal:
    """Calculate SGST (rate / 2)."""
    tv = Decimal(str(taxable_value or 0))
    r = Decimal(str(rate or 0))
    return gst_round(tv * (r / Decimal('200')))

def calculate_igst(taxable_value, rate) -> Decimal:
    """Calculate IGST (full rate)."""
    tv = Decimal(str(taxable_value or 0))
    r = Decimal(str(rate or 0))
    return gst_round(tv * (r / Decimal('100')))

def calculate_cess(taxable_value, rate) -> Decimal:
    """Calculate CESS."""
    tv = Decimal(str(taxable_value or 0))
    r = Decimal(str(rate or 0))
    return gst_round(tv * (r / Decimal('100')))

def calculate_total_tax(cgst, sgst, igst, cess) -> Decimal:
    """Sum all tax components."""
    c = Decimal(str(cgst or 0))
    s = Decimal(str(sgst or 0))
    i = Decimal(str(igst or 0))
    ce = Decimal(str(cess or 0))
    return gst_round(c + s + i + ce)

def calculate_invoice_value(taxable_value, total_tax) -> Decimal:
    """Calculate total invoice value."""
    tv = Decimal(str(taxable_value or 0))
    tt = Decimal(str(total_tax or 0))
    return gst_round(tv + tt)

def determine_tax_type(seller_state: str, buyer_state: str) -> str:
    """Determine INTRA (same state) or INTER (different state)."""
    if not seller_state or not buyer_state:
        return 'UNKNOWN'
    return 'INTRA' if str(seller_state).strip() == str(buyer_state).strip() else 'INTER'

def validate_tax_calculation(taxable, cgst, sgst, igst, cess, total, tolerance=Decimal('0.02')) -> tuple[bool, list]:
    """Validate provided tax calculations vs computed ones."""
    errors = []
    
    taxable_val = Decimal(str(taxable or 0))
    cgst_val = Decimal(str(cgst or 0))
    sgst_val = Decimal(str(sgst or 0))
    igst_val = Decimal(str(igst or 0))
    cess_val = Decimal(str(cess or 0))
    total_val = Decimal(str(total or 0))
    tol = Decimal(str(tolerance))
    
    calculated_total_tax = calculate_total_tax(cgst_val, sgst_val, igst_val, cess_val)
    calculated_total = calculate_invoice_value(taxable_val, calculated_total_tax)
    
    if abs(total_val - calculated_total) > tol:
        errors.append(f"Total invoice value mismatch. Calculated: {calculated_total}, Provided: {total_val}")
        
    is_valid = len(errors) == 0
    return is_valid, errors
