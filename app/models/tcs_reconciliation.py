from datetime import datetime
from decimal import Decimal
from app.extensions import db

class TCSReconciliation(db.Model):
    """
    Model for tracking TCS Reconciliation data.
    """
    __tablename__ = 'tcs_reconciliations'

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey('gst_profiles.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    return_period = db.Column(db.String(6), index=True, nullable=False) # MMYYYY
    import_history_id = db.Column(db.Integer, db.ForeignKey('import_histories.id'))
    
    state_code = db.Column(db.String(2), nullable=False)
    state_name = db.Column(db.String(100))
    
    calculated_taxable_value = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    portal_taxable_value = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    calculated_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    portal_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    
    difference_taxable = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    difference_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    
    match_status = db.Column(db.String(50), nullable=False) # MATCHED/MISMATCH/MISSING_IN_SOURCE/MISSING_IN_PORTAL
    
    adjustment_notes = db.Column(db.Text)
    is_adjusted = db.Column(db.Boolean, default=False)
    adjusted_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    adjusted_at = db.Column(db.DateTime)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<TCSReconciliation {self.return_period} - {self.state_code}>"
