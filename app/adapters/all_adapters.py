from typing import Dict, List, Tuple, Any
from decimal import Decimal
import datetime
from .base import PlatformAdapter, ImportResult, ImportRow, ImportRowStatus

class BaseGenericAdapter(PlatformAdapter):
    """A generic base adapter with robust, default implementations to satisfy abstract methods."""
    
    PLATFORM_NAME = "Generic"
    
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return False
        
    def validate(self, workbook_or_data) -> Tuple[bool, List[str]]:
        return True, []
        
    def parse(self, workbook_or_data) -> ImportResult:
        result = ImportResult(platform=self.PLATFORM_NAME, file_name="")
        
        if not workbook_or_data:
            result.errors.append("No data provided.")
            return result
            
        try:
            import openpyxl
            if isinstance(workbook_or_data, openpyxl.Workbook):
                sheet = workbook_or_data.active
                headers = []
                for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                    if row_idx == 1:
                        headers = [str(c).strip() if c else f"col_{i}" for i, c in enumerate(row)]
                        continue
                        
                    raw_data = dict(zip(headers, row))
                    if not any(raw_data.values()):
                        continue
                        
                    normalized = self.normalize(raw_data)
                    status = ImportRowStatus.SUCCESS
                    errors = []
                    
                    if not normalized.get('invoice_number'):
                        status = ImportRowStatus.ERROR
                        errors.append("Missing invoice number")
                        
                    row_obj = ImportRow(
                        row_number=row_idx,
                        status=status,
                        raw_data=raw_data,
                        normalized_data=normalized,
                        errors=errors
                    )
                    
                    result.rows.append(row_obj)
                    if status == ImportRowStatus.SUCCESS:
                        result.success_rows += 1
                    else:
                        result.error_rows += 1
                        
                result.total_rows = len(result.rows)
            else:
                result.errors.append("Unsupported data type, expected openpyxl Workbook.")
        except Exception as e:
            result.errors.append(f"Parse error: {str(e)}")
            
        return result

    def get_decimal(self, raw_row: Dict, keys: List[str], default: Decimal = Decimal('0.0')) -> Decimal:
        for k in keys:
            if k in raw_row and raw_row[k] is not None:
                try:
                    return Decimal(str(raw_row[k]).replace(',', '').strip())
                except:
                    pass
        return default
        
    def get_string(self, raw_row: Dict, keys: List[str], default: str = "") -> str:
        for k in keys:
            if k in raw_row and raw_row[k] is not None:
                return str(raw_row[k]).strip()
        return default

    def map_taxes(self, raw_row: Dict) -> Dict:
        return {
            'taxable_value': self.get_decimal(raw_row, ['Taxable Value', 'Principal Amount', 'Taxable Amount', 'Selling Price']),
            'cgst_amount': self.get_decimal(raw_row, ['CGST Amount', 'CGST', 'Central Tax']),
            'sgst_amount': self.get_decimal(raw_row, ['SGST Amount', 'SGST', 'State/UT Tax', 'State Tax']),
            'igst_amount': self.get_decimal(raw_row, ['IGST Amount', 'IGST', 'Integrated Tax']),
            'cess_amount': self.get_decimal(raw_row, ['Cess Amount', 'CESS', 'Cess']),
            'cgst_rate': self.get_decimal(raw_row, ['CGST Rate']),
            'sgst_rate': self.get_decimal(raw_row, ['SGST Rate']),
            'igst_rate': self.get_decimal(raw_row, ['IGST Rate']),
            'cess_rate': self.get_decimal(raw_row, ['Cess Rate']),
            'tax_rate': self.get_decimal(raw_row, ['Tax Rate', 'GST Rate']),
            'invoice_value': self.get_decimal(raw_row, ['Invoice Amount', 'Invoice Value', 'Total Amount', 'Total'])
        }

    def map_customer(self, raw_row: Dict) -> Dict:
        return {
            'customer_name': self.get_string(raw_row, ['Customer Name', 'Buyer Name', 'Bill To Name']),
            'customer_gstin': self.get_string(raw_row, ['Buyer GSTIN', 'Customer GSTIN', 'GSTIN/UIN of Recipient']),
            'place_of_supply': self.get_string(raw_row, ['Ship To State', 'Place Of Supply', 'State', 'Buyer State'])
        }

    def map_invoice(self, raw_row: Dict) -> Dict:
        inv_date = self.get_string(raw_row, ['Invoice Date', 'Date', 'Order Date'])
        if isinstance(inv_date, datetime.datetime):
            inv_date = inv_date.strftime('%Y-%m-%d')
            
        return {
            'order_id': self.get_string(raw_row, ['Order ID', 'Order Id', 'Order Item ID']),
            'invoice_number': self.get_string(raw_row, ['Invoice Number', 'Invoice No', 'Invoice No.']),
            'invoice_date': inv_date,
            'seller_gstin': self.get_string(raw_row, ['Seller GSTIN']),
            'reverse_charge': self.get_string(raw_row, ['Reverse Charge', 'Reverse charge']),
            'ecommerce_gstin': self.get_string(raw_row, ['Ecommerce GSTIN']),
        }

    def map_items(self, raw_row: Dict) -> Dict:
        return {
            'hsn_sac': self.get_string(raw_row, ['HSN/SAC', 'HSN Code', 'HSN']),
            'description': self.get_string(raw_row, ['Product Description', 'Product Title', 'Product Name', 'Item Description']),
            'quantity': self.get_decimal(raw_row, ['Quantity', 'Qty']),
            'supply_type': self.get_string(raw_row, ['Supply Type'])
        }

    def map_returns(self, raw_row: Dict) -> Dict:
        return {
            'return_flag': self.get_string(raw_row, ['Return', 'Return Status', 'Status']) in ['Returned', 'Return', 'Cancelled', 'Cancellation'],
            'cancellation_flag': self.get_string(raw_row, ['Status']) in ['Cancelled']
        }

    def map_notes(self, raw_row: Dict) -> Dict:
        return {
            'note_type': self.get_string(raw_row, ['Note Type', 'Document Type']),
            'note_number': self.get_string(raw_row, ['Note Number', 'Credit Note No', 'Refund Id']),
            'original_invoice_number': self.get_string(raw_row, ['Original Invoice Number', 'Invoice Number']),
        }

    def normalize(self, raw_row: Dict) -> Dict:
        normalized = {}
        normalized.update(self.map_invoice(raw_row))
        normalized.update(self.map_customer(raw_row))
        normalized.update(self.map_items(raw_row))
        normalized.update(self.map_taxes(raw_row))
        normalized.update(self.map_returns(raw_row))
        normalized.update(self.map_notes(raw_row))
        normalized['source_metadata'] = str(raw_row)
        return normalized

class AmazonAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Amazon"
    SUPPORTED_FILE_TYPES = [".xlsx", ".csv"]
    
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "amazon" in file_name.lower() or "mtr" in file_name.lower()

class FlipkartAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Flipkart"
    SUPPORTED_FILE_TYPES = [".xlsx", ".csv"]
    
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "flipkart" in file_name.lower()

class MeeshoAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Meesho"
    
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "meesho" in file_name.lower()

class MyntraAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Myntra"
    
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "myntra" in file_name.lower()

class JioMartAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "JioMart"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "jiomart" in file_name.lower()

class SnapdealAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Snapdeal"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "snapdeal" in file_name.lower()

class TataCliqAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "TataCLiQ"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "tatacliq" in file_name.lower() or "tata_cliq" in file_name.lower()

class LimeroadAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Limeroad"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "limeroad" in file_name.lower()

class ShopdeckAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Shopdeck"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "shopdeck" in file_name.lower()

class GlowRoadAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "GlowRoad"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "glowroad" in file_name.lower()

class SnapmintAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Snapmint"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "snapmint" in file_name.lower()

class CitymallAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Citymall"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "citymall" in file_name.lower()

class RoposoAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Roposo"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "roposo" in file_name.lower() or "clout" in file_name.lower()

class GSTR1GovtAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "GSTR1_Govt"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        return "gstr1" in file_name.lower() or "gstr-1" in file_name.lower()

class CustomExcelAdapter(BaseGenericAdapter):
    PLATFORM_NAME = "Custom_Excel"
    def detect(self, workbook_or_data, file_name: str) -> bool:
        # Fallback adapter
        return True

