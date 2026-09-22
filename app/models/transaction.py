from datetime import datetime
from decimal import Decimal
from app.extensions import db

class Transaction(db.Model):
    """
    Normalized GST transaction model. Core database entity for the platform.
    """
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    import_history_id = db.Column(db.Integer, db.ForeignKey('import_histories.id'), nullable=False, index=True)
    profile_id = db.Column(db.Integer, db.ForeignKey('gst_profiles.id'), nullable=False, index=True)
    raw_import_id = db.Column(db.Integer, db.ForeignKey('raw_imports.id'), nullable=False)
    
    source_platform = db.Column(db.String(50))
    source_row_id = db.Column(db.String(100))
    
    order_id = db.Column(db.String(100), index=True)
    invoice_number = db.Column(db.String(100), index=True)
    invoice_date = db.Column(db.Date)
    invoice_type = db.Column(db.String(50))
    
    customer_name = db.Column(db.String(255))
    customer_gstin = db.Column(db.String(15), index=True)
    place_of_supply = db.Column(db.String(2))
    
    seller_gstin = db.Column(db.String(15))
    
    item_code = db.Column(db.String(100))
    hsn_sac = db.Column(db.String(20), index=True)
    description = db.Column(db.Text)
    quantity = db.Column(db.Numeric(15, 2))
    uqc = db.Column(db.String(10))
    
    taxable_value = db.Column(db.Numeric(15, 2))
    discount = db.Column(db.Numeric(15, 2))
    
    cgst_rate = db.Column(db.Numeric(15, 2))
    cgst_amount = db.Column(db.Numeric(15, 2))
    sgst_rate = db.Column(db.Numeric(15, 2))
    sgst_amount = db.Column(db.Numeric(15, 2))
    igst_rate = db.Column(db.Numeric(15, 2))
    igst_amount = db.Column(db.Numeric(15, 2))
    cess_rate = db.Column(db.Numeric(15, 2))
    cess_amount = db.Column(db.Numeric(15, 2))
    
    total_tax = db.Column(db.Numeric(15, 2))
    invoice_value = db.Column(db.Numeric(15, 2))
    tax_rate = db.Column(db.Numeric(15, 2))
    
    # Supply Types: B2B/B2CS/B2CL/CDNR/CDNUR/NIL/EXEMPT/NONGST/EXPORT/SEZ
    supply_type = db.Column(db.String(20), index=True)
    reverse_charge = db.Column(db.String(1)) # Y/N
    ecommerce_gstin = db.Column(db.String(15))
    marketplace_name = db.Column(db.String(100))
    
    note_type = db.Column(db.String(10)) # CREDIT/DEBIT
    note_number = db.Column(db.String(100))
    note_date = db.Column(db.Date)
    original_invoice_number = db.Column(db.String(100))
    original_invoice_date = db.Column(db.Date)
    
    nil_rated_flag = db.Column(db.Boolean, default=False)
    exempt_flag = db.Column(db.Boolean, default=False)
    non_gst_flag = db.Column(db.Boolean, default=False)
    return_flag = db.Column(db.Boolean, default=False)
    cancellation_flag = db.Column(db.Boolean, default=False)
    amendment_flag = db.Column(db.Boolean, default=False)
    
    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime)
    deleted_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    
    validation_status = db.Column(db.String(20), index=True) # VALID/WARNING/ERROR
    validation_errors = db.Column(db.Text) # JSON string
    
    classification_status = db.Column(db.String(50))
    gstr1_table = db.Column(db.String(20), index=True)
    source_metadata = db.Column(db.Text) # JSON string
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    import_history = db.relationship('ImportHistory', back_populates='transactions')
    profile = db.relationship('GSTProfile', back_populates='transactions')
    raw_import = db.relationship('RawImport', back_populates='transactions')

    def __repr__(self):
        return f"<Transaction {self.invoice_number}>"
