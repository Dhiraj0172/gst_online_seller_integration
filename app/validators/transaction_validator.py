from decimal import Decimal
from typing import Dict, Any, List
from app.utils.gstin_validator import validate_gstin
from app.utils.hsn_utils import validate_hsn, validate_sac, is_hsn_or_sac
from app.utils.tax_calculator import validate_tax_calculation, VALID_GST_RATES
from app.utils.date_utils import parse_date
from app.utils.state_codes import is_valid_state

class ValidationResult:
    def __init__(self):
        self.is_valid = True
        self.errors = []
        self.warnings = []
        self.error_details = []
        
    def add_error(self, field: str, value: Any, message: str, severity: str = "ERROR"):
        self.is_valid = False
        self.errors.append(message)
        self.error_details.append({
            "field": field,
            "value": value,
            "message": message,
            "severity": severity
        })
        
    def add_warning(self, field: str, value: Any, message: str):
        self.warnings.append(message)
        self.error_details.append({
            "field": field,
            "value": value,
            "message": message,
            "severity": "WARNING"
        })

def classify_errors_vs_warnings(results: ValidationResult) -> tuple[List[Dict], List[Dict]]:
    errors = [e for e in results.error_details if e["severity"] == "ERROR"]
    warnings = [e for e in results.error_details if e["severity"] == "WARNING"]
    return errors, warnings

def validate_transaction(data: Dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    
    # Invoice Number
    invoice_number = data.get('invoice_number')
    if not invoice_number:
        result.add_error('invoice_number', invoice_number, "Invoice number is required")
        
    # Invoice Date
    invoice_date = data.get('invoice_date')
    if not invoice_date:
        result.add_error('invoice_date', invoice_date, "Invoice date is required")
    elif not parse_date(invoice_date):
        result.add_error('invoice_date', invoice_date, "Invalid invoice date format")
        
    # GSTIN (check both 'gstin' and 'customer_gstin' fields)
    gstin = data.get('gstin') or data.get('customer_gstin')
    if gstin:
        is_valid_g, g_msg = validate_gstin(gstin)
        if not is_valid_g:
            result.add_error('gstin', gstin, f"Invalid GSTIN: {g_msg}")
            
    # HSN/SAC
    hsn_sac = data.get('hsn_sac')
    if hsn_sac:
        type_code = is_hsn_or_sac(hsn_sac)
        if type_code == 'UNKNOWN':
            result.add_error('hsn_sac', hsn_sac, "Invalid HSN or SAC format")
            
    # Supply Type and State - warn if missing rather than error
    supply_type = data.get('supply_type')
    if supply_type and supply_type not in ('INTRA', 'INTER', 'B2B', 'B2CS', 'B2CL', 'CDNR', 'CDNUR', 'EXPORT', 'SEZ', 'NIL', 'EXEMPT', 'NONGST'):
        result.add_warning('supply_type', supply_type, f"Unrecognized supply type: {supply_type}")
        
    seller_state = data.get('seller_state')
    if seller_state and not is_valid_state(seller_state):
        result.add_error('seller_state', seller_state, "Invalid seller state code")
        
    buyer_state = data.get('buyer_state')
    if buyer_state and not is_valid_state(buyer_state):
        result.add_error('buyer_state', buyer_state, "Invalid buyer state code")
        
    # Rates and Amounts
    try:
        taxable_value = Decimal(str(data.get('taxable_value', 0)))
        if taxable_value < 0:
            result.add_error('taxable_value', taxable_value, "Taxable value cannot be negative")
            
        rate = Decimal(str(data.get('rate', 0)))
        if rate not in VALID_GST_RATES:
            result.add_error('rate', rate, f"Invalid GST rate. Must be one of: {VALID_GST_RATES}")
            
        cgst = Decimal(str(data.get('cgst_amount', 0)))
        sgst = Decimal(str(data.get('sgst_amount', 0)))
        igst = Decimal(str(data.get('igst_amount', 0)))
        cess = Decimal(str(data.get('cess_amount', 0)))
        total = Decimal(str(data.get('invoice_value', 0)))
        
        if supply_type == 'INTRA' and igst > 0:
            result.add_error('igst_amount', igst, "IGST cannot be present for INTRA state supply")
        if supply_type == 'INTER' and (cgst > 0 or sgst > 0):
            result.add_error('cgst_sgst', f"CGST:{cgst}, SGST:{sgst}", "CGST/SGST cannot be present for INTER state supply")
            
        is_valid_tax, tax_errors = validate_tax_calculation(taxable_value, cgst, sgst, igst, cess, total)
        for err in tax_errors:
            result.add_error('tax_calculation', total, err)
            
    except Exception as e:
        result.add_error('amounts', None, f"Invalid amount format: {str(e)}")
        
    return result
