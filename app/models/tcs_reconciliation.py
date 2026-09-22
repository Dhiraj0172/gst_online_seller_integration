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

    # E-commerce GSTIN from marketplace data (nullable for GSTR-8 state-only reports)
    ecommerce_gstin = db.Column(db.String(15), index=True)

    state_code = db.Column(db.String(2), nullable=False)
    state_name = db.Column(db.String(100))

    # Internal calculated values (based on marketplace transactions)
    # Net taxable value = taxable supplies - supplier returns (Section 52)
    our_net_taxable_value = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    # TCS calculated on net taxable value using period-aware rates
    our_calculated_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    our_calculated_cgst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    our_calculated_sgst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    our_calculated_igst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))

    # Portal reported values (from GSTR-8 / marketplace TCS certificate)
    portal_taxable_value = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    portal_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    portal_cgst_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    portal_sgst_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    portal_igst_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))

    difference_taxable = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    difference_tcs = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    difference_cgst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    difference_sgst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    difference_igst = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))

    # Match status: MATCHED, MISMATCH, MISSING_IN_SOURCE, MISSING_IN_PORTAL, AMBIGUOUS
    # AMBIGUOUS = multiple ecommerce_gstin map to same state in internal data but portal only has state
    match_status = db.Column(db.String(50), nullable=False)
    # If AMBIGUOUS, list the ecommerce_gstins that could match
    ambiguity_details = db.Column(db.Text)

    adjustment_notes = db.Column(db.Text)
    is_adjusted = db.Column(db.Boolean, default=False)
    adjusted_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    adjusted_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<TCSReconciliation {self.return_period} - {self.state_code}>"
