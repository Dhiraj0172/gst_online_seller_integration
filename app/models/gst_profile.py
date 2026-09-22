from datetime import datetime
import re
from app.extensions import db

class GSTProfile(db.Model):
    """
    GST Profile model representing a business entity.
    """
    __tablename__ = 'gst_profiles'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    gstin = db.Column(db.String(15), unique=True, index=True, nullable=False)
    legal_name = db.Column(db.String(255), nullable=False)
    trade_name = db.Column(db.String(255))
    state_code = db.Column(db.String(2), nullable=False)
    state_name = db.Column(db.String(100), nullable=False)
    financial_year = db.Column(db.String(9), nullable=False)
    filing_frequency = db.Column(db.String(20), nullable=False) # monthly/quarterly
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = db.relationship('User', back_populates='profiles')
    import_histories = db.relationship('ImportHistory', back_populates='profile', cascade='all, delete-orphan')
    transactions = db.relationship('Transaction', back_populates='profile', cascade='all, delete-orphan')
    generations = db.relationship('GSTR1Generation', back_populates='profile', cascade='all, delete-orphan')

    @staticmethod
    def validate_gstin(gstin):
        """Validate GSTIN format."""
        if not gstin:
            return False
        pattern = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
        return bool(re.match(pattern, gstin))

    def __repr__(self):
        return f"<GSTProfile {self.gstin}>"
