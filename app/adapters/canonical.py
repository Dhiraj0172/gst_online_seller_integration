"""Canonical transaction representation for marketplace imports.

Defines the core CanonicalTransaction dataclass that sits at the normalization
boundary between raw marketplace export formats and the application's
accounting/tax calculation engine.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional, Tuple


@dataclass
class CanonicalTransaction:
    """Canonical representation of a single marketplace transaction.

    Separates source/raw marketplace data from normalized, GST-compliant data.
    Provides dict-like mapping access for backward compatibility with existing
    classification and fingerprinting services.
    """
    order_id: Optional[str] = None
    order_item_id: Optional[str] = None
    sub_order_id: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None  # Expected standard format (e.g. DD-MM-YYYY)
    invoice_type: str = 'regular'
    document_type: Optional[str] = None
    customer_name: Optional[str] = None
    customer_gstin: Optional[str] = None
    place_of_supply: Optional[str] = None  # 2-digit state code
    place_of_supply_raw: Optional[str] = None
    ship_to_state: Optional[str] = None
    ship_from_state: Optional[str] = None
    buyer_state: Optional[str] = None
    seller_gstin: Optional[str] = None
    seller_state: Optional[str] = None
    item_code: Optional[str] = None
    hsn_sac: Optional[str] = None
    description: Optional[str] = None
    quantity: Optional[Decimal] = None
    uqc: str = 'NOS'
    unit_price: Optional[Decimal] = None
    discount: Optional[Decimal] = None
    taxable_value: Decimal = Decimal('0')
    cgst_rate: Decimal = Decimal('0')
    cgst_amount: Decimal = Decimal('0')
    sgst_rate: Decimal = Decimal('0')
    sgst_amount: Decimal = Decimal('0')
    igst_rate: Decimal = Decimal('0')
    igst_amount: Decimal = Decimal('0')
    cess_rate: Decimal = Decimal('0')
    cess_amount: Decimal = Decimal('0')
    tax_rate: Decimal = Decimal('0')
    invoice_value: Decimal = Decimal('0')
    supply_type: Optional[str] = None
    reverse_charge: str = 'N'
    ecommerce_gstin: Optional[str] = None
    marketplace_name: Optional[str] = None
    source_platform: Optional[str] = None
    note_type: Optional[str] = None  # 'CREDIT', 'DEBIT', or ''
    note_type_raw: Optional[str] = None
    note_number: Optional[str] = None
    note_date: Optional[str] = None
    original_invoice_number: Optional[str] = None
    original_invoice_date: Optional[str] = None
    return_flag: bool = False
    cancellation_flag: bool = False
    amendment_flag: bool = False
    tax_rate_derived: bool = False
    source_metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def total_tax(self) -> Decimal:
        """Sum of all tax amount components."""
        return (
            (self.cgst_amount or Decimal('0'))
            + (self.sgst_amount or Decimal('0'))
            + (self.igst_amount or Decimal('0'))
            + (self.cess_amount or Decimal('0'))
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert canonical transaction to a dictionary."""
        return {
            'order_id': self.order_id,
            'order_item_id': self.order_item_id,
            'sub_order_id': self.sub_order_id,
            'invoice_number': self.invoice_number,
            'invoice_date': self.invoice_date,
            'invoice_type': self.invoice_type,
            'document_type': self.document_type,
            'customer_name': self.customer_name,
            'customer_gstin': self.customer_gstin,
            'place_of_supply': self.place_of_supply,
            'place_of_supply_raw': self.place_of_supply_raw,
            'ship_to_state': self.ship_to_state,
            'ship_from_state': self.ship_from_state,
            'buyer_state': self.buyer_state,
            'seller_gstin': self.seller_gstin,
            'seller_state': self.seller_state,
            'item_code': self.item_code,
            'hsn_sac': self.hsn_sac,
            'description': self.description,
            'quantity': self.quantity,
            'uqc': self.uqc,
            'unit_price': self.unit_price,
            'discount': self.discount,
            'taxable_value': self.taxable_value,
            'cgst_rate': self.cgst_rate,
            'cgst_amount': self.cgst_amount,
            'sgst_rate': self.sgst_rate,
            'sgst_amount': self.sgst_amount,
            'igst_rate': self.igst_rate,
            'igst_amount': self.igst_amount,
            'cess_rate': self.cess_rate,
            'cess_amount': self.cess_amount,
            'tax_rate': self.tax_rate,
            'invoice_value': self.invoice_value,
            'supply_type': self.supply_type,
            'reverse_charge': self.reverse_charge,
            'ecommerce_gstin': self.ecommerce_gstin,
            'marketplace_name': self.marketplace_name,
            'source_platform': self.source_platform,
            'note_type': self.note_type,
            'note_type_raw': self.note_type_raw,
            'note_number': self.note_number,
            'note_date': self.note_date,
            'original_invoice_number': self.original_invoice_number,
            'original_invoice_date': self.original_invoice_date,
            'return_flag': self.return_flag,
            'cancellation_flag': self.cancellation_flag,
            'amendment_flag': self.amendment_flag,
            'tax_rate_derived': self.tax_rate_derived,
            'source_metadata': dict(self.source_metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CanonicalTransaction':
        """Construct a CanonicalTransaction instance from a normalized dictionary."""
        if not data:
            return cls()

        def _to_dec(val, default=Decimal('0')) -> Decimal:
            if val is None or val == '':
                return default
            if isinstance(val, Decimal):
                return val
            try:
                return Decimal(str(val))
            except Exception:
                return default

        def _to_dec_opt(val) -> Optional[Decimal]:
            if val is None or val == '':
                return None
            if isinstance(val, Decimal):
                return val
            try:
                return Decimal(str(val))
            except Exception:
                return None

        # Build instance with known fields
        source_meta = data.get('source_metadata')
        if not isinstance(source_meta, dict):
            source_meta = {}

        return cls(
            order_id=str(data.get('order_id') or '') or None,
            order_item_id=str(data.get('order_item_id') or '') or None,
            sub_order_id=str(data.get('sub_order_id') or '') or None,
            invoice_number=str(data.get('invoice_number') or '') or None,
            invoice_date=str(data.get('invoice_date') or '') or None,
            invoice_type=str(data.get('invoice_type') or 'regular'),
            document_type=str(data.get('document_type') or '') or None,
            customer_name=str(data.get('customer_name') or '') or None,
            customer_gstin=str(data.get('customer_gstin') or '') or None,
            place_of_supply=str(data.get('place_of_supply') or '') or None,
            place_of_supply_raw=str(data.get('place_of_supply_raw') or '') or None,
            ship_to_state=str(data.get('ship_to_state') or '') or None,
            ship_from_state=str(data.get('ship_from_state') or '') or None,
            buyer_state=str(data.get('buyer_state') or '') or None,
            seller_gstin=str(data.get('seller_gstin') or '') or None,
            seller_state=str(data.get('seller_state') or '') or None,
            item_code=str(data.get('item_code') or '') or None,
            hsn_sac=str(data.get('hsn_sac') or '') or None,
            description=str(data.get('description') or '') or None,
            quantity=_to_dec_opt(data.get('quantity')),
            uqc=str(data.get('uqc') or 'NOS'),
            unit_price=_to_dec_opt(data.get('unit_price')),
            discount=_to_dec_opt(data.get('discount')),
            taxable_value=_to_dec(data.get('taxable_value')),
            cgst_rate=_to_dec(data.get('cgst_rate')),
            cgst_amount=_to_dec(data.get('cgst_amount')),
            sgst_rate=_to_dec(data.get('sgst_rate')),
            sgst_amount=_to_dec(data.get('sgst_amount')),
            igst_rate=_to_dec(data.get('igst_rate')),
            igst_amount=_to_dec(data.get('igst_amount')),
            cess_rate=_to_dec(data.get('cess_rate')),
            cess_amount=_to_dec(data.get('cess_amount')),
            tax_rate=_to_dec(data.get('tax_rate')),
            invoice_value=_to_dec(data.get('invoice_value')),
            supply_type=str(data.get('supply_type') or '') or None,
            reverse_charge='Y' if str(data.get('reverse_charge') or '').strip().upper() in ('Y', 'YES', 'TRUE', '1') else 'N',
            ecommerce_gstin=str(data.get('ecommerce_gstin') or '') or None,
            marketplace_name=str(data.get('marketplace_name') or '') or None,
            source_platform=str(data.get('source_platform') or '') or None,
            note_type=str(data.get('note_type') or '') or None,
            note_type_raw=str(data.get('note_type_raw') or '') or None,
            note_number=str(data.get('note_number') or '') or None,
            note_date=str(data.get('note_date') or '') or None,
            original_invoice_number=str(data.get('original_invoice_number') or '') or None,
            original_invoice_date=str(data.get('original_invoice_date') or '') or None,
            return_flag=bool(data.get('return_flag', False)),
            cancellation_flag=bool(data.get('cancellation_flag', False)),
            amendment_flag=bool(data.get('amendment_flag', False)),
            tax_rate_derived=bool(data.get('tax_rate_derived', False)),
            source_metadata=dict(source_meta),
        )

    # ----------------------------------------------------------------------
    # Mapping protocol methods for backward compatibility
    # ----------------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        if hasattr(self, key):
            setattr(self, key, value)
        else:
            self.source_metadata[key] = value

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> List[str]:
        return list(self.to_dict().keys())

    def values(self) -> List[Any]:
        return list(self.to_dict().values())

    def items(self) -> List[Tuple[str, Any]]:
        return list(self.to_dict().items())
