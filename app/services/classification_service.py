from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional, Tuple, Union
from app.services.gst_rules import get_rules_for_period

GST_RULE_VERSION = "2026.1"

TAXABILITY_TAXABLE = "TAXABLE"
TAXABILITY_NIL_RATED = "NIL_RATED"
TAXABILITY_EXEMPT = "EXEMPT"
TAXABILITY_NON_GST = "NON_GST"

TAXABLE = TAXABILITY_TAXABLE
NIL_RATED = TAXABILITY_NIL_RATED
EXEMPT = TAXABILITY_EXEMPT
NON_GST = TAXABILITY_NON_GST


class TaxabilityResult(str):
    """String subclass that allows tuple unpacking for backward and dual compatibility."""
    def __new__(cls, taxability: str, reason: str = ""):
        obj = str.__new__(cls, taxability)
        obj.taxability = taxability
        obj.reason = reason
        return obj

    def __iter__(self):
        yield self.taxability
        yield self.reason


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    """Helper to safely retrieve an attribute from either a dict or an object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def determine_supply_type(seller_gstin: Optional[str], customer_gstin: Optional[str], place_of_supply: Optional[str], seller_state: Optional[str]) -> str:
    """Determine if supply is INTER or INTRA state."""
    pos_code = str(place_of_supply)[:2] if place_of_supply else None
    seller_code = str(seller_state)[:2] if seller_state else (str(seller_gstin)[:2] if seller_gstin else None)

    if pos_code and seller_code and pos_code != seller_code:
        return "INTER"
    return "INTRA"


def determine_taxability(transaction_or_rate: Any, is_nil_rated: bool = False, is_exempt: bool = False, is_non_gst: bool = False, **kwargs) -> TaxabilityResult:
    """Determine explicit statutory taxability category and provenance reason.
    
    Supports:
        taxability, reason = determine_taxability(tx)
        res = determine_taxability(18.0)
    """
    if isinstance(transaction_or_rate, (int, float, Decimal)):
        if is_non_gst or kwargs.get('non_gst_flag'):
            return TaxabilityResult(TAXABILITY_NON_GST, "STATUTORY_FLAG_NON_GST")
        if is_exempt or kwargs.get('exempt_flag'):
            return TaxabilityResult(TAXABILITY_EXEMPT, "STATUTORY_FLAG_EXEMPT")
        if is_nil_rated or kwargs.get('nil_rated_flag'):
            return TaxabilityResult(TAXABILITY_NIL_RATED, "STATUTORY_FLAG_NIL_RATED")
        try:
            rate_dec = Decimal(str(transaction_or_rate))
            if rate_dec == 0:
                return TaxabilityResult(TAXABILITY_NIL_RATED, "ZERO_RATE_SCHEDULE_DEFAULT")
            return TaxabilityResult(TAXABILITY_TAXABLE, f"TAXABLE_RATE_{rate_dec}")
        except Exception:
            return TaxabilityResult(TAXABILITY_TAXABLE, "DEFAULT_TAXABLE")

    transaction = transaction_or_rate
    if _get_val(transaction, 'non_gst_flag') or _get_val(transaction, 'is_non_gst'):
        return TaxabilityResult(TAXABILITY_NON_GST, "STATUTORY_FLAG_NON_GST")

    item_type = str(_get_val(transaction, 'item_type') or _get_val(transaction, 'product_category') or '').lower()
    supply_type = str(_get_val(transaction, 'supply_type') or '').lower()

    if any(k in item_type or k in supply_type for k in ('non-gst', 'nongst', 'non_gst', 'outside_gst')):
        return TaxabilityResult(TAXABILITY_NON_GST, "ITEM_TYPE_NON_GST")

    if _get_val(transaction, 'exempt_flag') or _get_val(transaction, 'is_exempt'):
        return TaxabilityResult(TAXABILITY_EXEMPT, "STATUTORY_FLAG_EXEMPT")
    if any(k in item_type or k in supply_type for k in ('exempt', 'exempted')):
        return TaxabilityResult(TAXABILITY_EXEMPT, "ITEM_TYPE_EXEMPT")

    if _get_val(transaction, 'nil_rated_flag') or _get_val(transaction, 'is_nil_rated'):
        return TaxabilityResult(TAXABILITY_NIL_RATED, "STATUTORY_FLAG_NIL_RATED")
    if any(k in item_type or k in supply_type for k in ('nil', 'nil_rated', 'nil rated')):
        return TaxabilityResult(TAXABILITY_NIL_RATED, "ITEM_TYPE_NIL_RATED")

    tax_rate = _get_val(transaction, 'tax_rate')
    if tax_rate is not None:
        try:
            rate_dec = Decimal(str(tax_rate))
            if rate_dec == 0:
                return TaxabilityResult(TAXABILITY_NIL_RATED, "ZERO_RATE_SCHEDULE_DEFAULT")
            return TaxabilityResult(TAXABILITY_TAXABLE, f"TAXABLE_RATE_{rate_dec}")
        except (InvalidOperation, TypeError, ValueError):
            pass

    cgst = Decimal(str(_get_val(transaction, 'cgst_amount') or 0))
    sgst = Decimal(str(_get_val(transaction, 'sgst_amount') or 0))
    igst = Decimal(str(_get_val(transaction, 'igst_amount') or 0))
    if cgst > 0 or sgst > 0 or igst > 0:
        return TaxabilityResult(TAXABILITY_TAXABLE, "POSITIVE_TAX_AMOUNTS")

    return TaxabilityResult(TAXABILITY_TAXABLE, "DEFAULT_TAXABLE")


def classify_b2b(transaction: Any) -> bool:
    """Check if transaction is B2B."""
    gstin = _get_val(transaction, 'customer_gstin', '')
    return bool(gstin and len(str(gstin)) == 15 and not classify_cdnr(transaction) and not classify_cdnur(transaction))


def classify_b2c(transaction: Any, return_period: str = "082024", seller_state: Optional[str] = None) -> Optional[str]:
    """Classify as B2CS or B2CL based on rules."""
    if _get_val(transaction, 'customer_gstin'):
        return None

    rules = get_rules_for_period(return_period)
    is_inter = determine_supply_type(
        _get_val(transaction, 'seller_gstin', ''),
        None,
        _get_val(transaction, 'place_of_supply', ''),
        seller_state or _get_val(transaction, 'seller_state', '')
    ) == "INTER"

    invoice_value = Decimal(str(_get_val(transaction, 'invoice_value', 0) or 0))

    if rules['b2cl_applicable'] and is_inter and invoice_value > rules['b2cl_threshold']:
        return 'B2CL'
    return 'B2CS'


def classify_cdnr(transaction: Any) -> bool:
    """Check if transaction is Credit/Debit note to registered person."""
    doc_type = str(_get_val(transaction, 'document_type') or _get_val(transaction, 'note_type') or _get_val(transaction, 'supply_type') or '').lower()
    gstin = str(_get_val(transaction, 'customer_gstin') or '')
    is_note = bool(
        any(k in doc_type for k in ['credit', 'debit', 'cr', 'dr', 'cdnr']) or
        _get_val(transaction, 'note_type') or _get_val(transaction, 'note_number')
    )
    return bool(is_note and len(gstin) == 15)


def classify_cdnur(transaction: Any) -> bool:
    """Check if transaction is Credit/Debit note to unregistered person."""
    doc_type = str(_get_val(transaction, 'document_type') or _get_val(transaction, 'note_type') or _get_val(transaction, 'supply_type') or '').lower()
    gstin = str(_get_val(transaction, 'customer_gstin') or '')
    is_note = bool(
        any(k in doc_type for k in ['credit', 'debit', 'cr', 'dr', 'cdnur']) or
        _get_val(transaction, 'note_type') or _get_val(transaction, 'note_number')
    )
    return bool(is_note and (not gstin or len(gstin) != 15))


def classify_sez(transaction: Any) -> str:
    """Check if transaction is to SEZ. Returns SEZ_REGISTERED or SEZ_UNREGISTERED."""
    sez_type = _get_val(transaction, 'sez_type')
    if sez_type in ['WPAY', 'WOPAY']:
        gstin = str(_get_val(transaction, 'customer_gstin') or '')
        if gstin and len(gstin) == 15:
            return "SEZ_REGISTERED"
        return "SEZ_UNREGISTERED"
    return ""


def classify_nil_exempt(transaction: Any) -> Optional[str]:
    """Classify as NIL, EXEMPT, or NONGST with recipient type."""
    taxability, _ = determine_taxability(transaction)
    gstin = str(_get_val(transaction, 'customer_gstin') or '')
    is_registered = bool(gstin and len(gstin) == 15)
    suffix = "_REGISTERED" if is_registered else "_UNREGISTERED"

    if taxability == TAXABILITY_EXEMPT:
        return 'EXEMPT' + suffix
    if taxability == TAXABILITY_NON_GST:
        return 'NONGST' + suffix
    if taxability == TAXABILITY_NIL_RATED:
        # If registered and no explicit nil/exempt/nongst flag or item_type, a 0% rate is a taxable B2B invoice
        if is_registered and not any(k in str(_get_val(transaction, 'item_type') or '').lower() for k in ('nil', 'exempt', 'non-gst', 'nongst')) and not _get_val(transaction, 'nil_rated_flag'):
            return None
        return 'NIL' + suffix

    return None


def classify_export(transaction: Any) -> bool:
    """Check if transaction is an export."""
    return _get_val(transaction, 'export_type') in ['WPAY', 'WOPAY']


def classify_reverse_charge(transaction: Any) -> bool:
    """Check if reverse charge applies."""
    return bool(_get_val(transaction, 'reverse_charge', False))


def classify_ecommerce(transaction: Any) -> bool:
    """Check if transaction goes through E-commerce operator."""
    return bool(_get_val(transaction, 'ecommerce_gstin'))


def classify_transaction(transaction: Any, profile: Any = None, return_period: str = "082024", supplier_state: Optional[str] = None) -> str:
    """Determine primary classification for a transaction."""
    if classify_export(transaction):
        return "EXPORT"
    sez_class = classify_sez(transaction)
    if sez_class:
        return sez_class
    if classify_cdnr(transaction):
        return "CDNR"
    if classify_cdnur(transaction):
        return "CDNUR"

    # Evaluate explicit NIL/EXEMPT/NONGST before generic B2B/B2CS
    nil_exempt = classify_nil_exempt(transaction)
    if nil_exempt:
        return nil_exempt

    if classify_b2b(transaction):
        return "B2B"

    seller_st = supplier_state or (getattr(profile, 'state_code', None) if profile else None)
    b2c_type = classify_b2c(transaction, return_period, seller_state=seller_st)
    if b2c_type:
        return b2c_type

    return "B2CS"


def classify_with_provenance(transaction: Any, profile: Any = None, return_period: str = "082024", supplier_state: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Classify transaction and attach full statutory provenance metadata."""
    classification = classify_transaction(transaction, profile, return_period, supplier_state=supplier_state)
    taxability, taxability_reason = determine_taxability(transaction)
    rules = get_rules_for_period(return_period) if return_period else {}

    # Normalize to base classification if prefixed (e.g. EXEMPT_UNREGISTERED -> EXEMPT / NIL)
    base_classification = classification
    if classification.startswith(('NIL', 'EXEMPT', 'NONGST')):
        base_classification = 'NIL'

    return {
        'classification': base_classification,
        'detailed_classification': classification,
        'taxability': str(taxability),
        'taxability_reason': taxability_reason,
        'classification_reason': f"Classified as {classification} under {taxability} ({taxability_reason})",
        'rule_version': GST_RULE_VERSION,
        'schema_version': rules.get('gstr1_schema_version', '1.0'),
        'return_period': return_period,
        'is_b2cl_applicable': rules.get('b2cl_applicable', False),
        'hsn_reporting_mode': rules.get('hsn_reporting_mode', 'combined'),
    }