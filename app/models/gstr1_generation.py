from datetime import datetime
from decimal import Decimal
from app.extensions import db

class GSTR1Generation(db.Model):
    """
    Tracks generated GSTR1 outputs.
    """
    __tablename__ = 'gstr1_generations'

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey('gst_profiles.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    return_period = db.Column(db.String(6), index=True, nullable=False) # MMYYYY
    
    generation_status = db.Column(db.String(20), nullable=False)
    generation_started_at = db.Column(db.DateTime)
    generation_completed_at = db.Column(db.DateTime)
    
    excel_file_path = db.Column(db.String(512))
    json_file_path = db.Column(db.String(512))
    
    total_b2b = db.Column(db.Integer, default=0)
    total_b2cs = db.Column(db.Integer, default=0)
    total_b2cl = db.Column(db.Integer, default=0)
    total_cdnr = db.Column(db.Integer, default=0)
    total_cdnur = db.Column(db.Integer, default=0)
    total_nil = db.Column(db.Integer, default=0)
    total_hsn_b2b = db.Column(db.Integer, default=0)
    total_hsn_b2c = db.Column(db.Integer, default=0)
    total_ecom = db.Column(db.Integer, default=0)
    
    total_taxable_value = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    total_cgst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    total_sgst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    total_igst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    total_cess = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    
    validation_passed = db.Column(db.Boolean, default=False)
    validation_errors = db.Column(db.Text) # JSON string
    reconciliation_report = db.Column(db.Text) # JSON string
    
    schema_version = db.Column(db.String(20))
    rule_version = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    profile = db.relationship('GSTProfile', back_populates='generations')

    def __repr__(self):
        return f"<GSTR1Generation {self.return_period} for Profile {self.profile_id}>"
