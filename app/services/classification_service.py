from decimal import Decimal
from typing import Dict, Any, Optional
from app.services.gst_rules import get_rules_for_period

def determine_supply_type(seller_gstin: str, customer_gstin: Optional[str], place_of_supply: str, seller_state: str) -> str:
    """Determine if supply is INTER or INTRA state."""
    # Place of supply usually indicated by state code prefix
    pos_code = place_of_supply[:2] if place_of_supply else None
    seller_code = seller_state[:2] if seller_state else (seller_gstin[:2] if seller_gstin else None)
    
    if pos_code and seller_code and pos_code != seller_code:
        return "INTER"
    return "INTRA"

def classify_b2b(transaction: Dict[str, Any]) -> bool:
    """Check if transaction is B2B."""
    gstin = transaction.get('customer_gstin', '')
    return bool(gstin and len(gstin) == 15 and not classify_cdnr(transaction) and not classify_cdnur(transaction))

def classify_b2c(transaction: Dict[str, Any], return_period: str) -> Optional[str]:
    """Classify as B2CS or B2CL based on rules."""
    if transaction.get('customer_gstin'):
        return None
    
    rules = get_rules_for_period(return_period)
    is_inter = determine_supply_type(
        transaction.get('seller_gstin', ''),
        None,
        transaction.get('place_of_supply', ''),
        transaction.get('seller_state', '')
    ) == "INTER"
    
    invoice_value = Decimal(str(transaction.get('invoice_value', 0)))
    
    if rules['b2cl_applicable'] and is_inter and invoice_value > rules['b2cl_threshold']:
        return 'B2CL'
    return 'B2CS'

def classify_cdnr(transaction: Dict[str, Any]) -> bool:
    """Check if transaction is Credit/Debit note to registered person."""
    doc_type = str(transaction.get('document_type') or transaction.get('note_type') or transaction.get('supply_type') or '').lower()
    gstin = str(transaction.get('customer_gstin') or '')
    is_note = bool(
        any(k in doc_type for k in ['credit', 'debit', 'cr', 'dr', 'cdnr']) or
        transaction.get('note_type') or transaction.get('note_number')
    )
    return bool(is_note and len(gstin) == 15)

def classify_cdnur(transaction: Dict[str, Any]) -> bool:
    """Check if transaction is Credit/Debit note to unregistered person."""
    doc_type = str(transaction.get('document_type') or transaction.get('note_type') or transaction.get('supply_type') or '').lower()
    gstin = str(transaction.get('customer_gstin') or '')
    is_note = bool(
        any(k in doc_type for k in ['credit', 'debit', 'cr', 'dr', 'cdnur']) or
        transaction.get('note_type') or transaction.get('note_number')
    )
    return bool(is_note and (not gstin or len(gstin) != 15))

def classify_nil_exempt(transaction: Dict[str, Any]) -> Optional[str]:
    """Classify as NIL, EXEMPT, or NONGST."""
    item_type = str(transaction.get('item_type', '') or '').lower()
    supply_type = str(transaction.get('supply_type', '') or '').lower()
    
    if 'exempt' in item_type or 'exempt' in supply_type or transaction.get('exempt_flag'):
        return 'EXEMPT'
    if 'non-gst' in item_type or 'nongst' in item_type or 'non-gst' in supply_type or transaction.get('non_gst_flag'):
        return 'NONGST'
    if 'nil' in item_type or 'nil' in supply_type or transaction.get('nil_rated_flag'):
        return 'NIL'
        
    tax_rate = transaction.get('tax_rate')
    if tax_rate is not None and Decimal(str(tax_rate)) == 0 and not transaction.get('customer_gstin'):
        return 'NIL'
        
    return None

def classify_export(transaction: Dict[str, Any]) -> bool:
    """Check if transaction is an export."""
    return transaction.get('export_type') in ['WPAY', 'WOPAY']

def classify_sez(transaction: Dict[str, Any]) -> bool:
    """Check if transaction is to SEZ."""
    return transaction.get('sez_type') in ['WPAY', 'WOPAY']

def classify_reverse_charge(transaction: Dict[str, Any]) -> bool:
    """Check if reverse charge applies."""
    return bool(transaction.get('reverse_charge', False))

def classify_ecommerce(transaction: Dict[str, Any]) -> bool:
    """Check if transaction goes through E-commerce operator."""
    return bool(transaction.get('ecommerce_gstin'))

def classify_transaction(transaction: Dict[str, Any], profile: Any, return_period: str) -> str:
    """Determine primary classification for a transaction."""
    if classify_export(transaction):
        return "EXPORT"
    if classify_sez(transaction):
        return "SEZ"
    if classify_cdnr(transaction):
        return "CDNR"
    if classify_cdnur(transaction):
        return "CDNUR"
        
    if classify_b2b(transaction):
        return "B2B"
        
    nil_exempt = classify_nil_exempt(transaction)
    if nil_exempt:
        return nil_exempt
        
    b2c_type = classify_b2c(transaction, return_period)
    if b2c_type:
        return b2c_type
        
    return "B2CS"
