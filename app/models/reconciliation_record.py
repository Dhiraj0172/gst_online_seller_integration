from datetime import datetime
from decimal import Decimal
from app.extensions import db


class ReconciliationRecord(db.Model):
    """Tracks reconciliation exceptions and user resolution lifecycle.
    
    Supports structured reconciliation buckets:
    - MATCHED: Exact mathematical & entity match
    - MATCHED_WITH_VARIANCE: Match with acceptable or non-blocking variance
    - UNMATCHED_SOURCE: In source file / portal but not in normalized database
    - UNMATCHED_NORMALIZED: In normalized database but not in portal / settlement
    - AMBIGUOUS: Multiple candidates preventing deterministic 1:1 match
    - EXCLUDED_BY_RULE: Excluded by statutory rule (e.g. missing HSN, staged rows)
    """
    __tablename__ = 'reconciliation_records'

    id = db.Column(db.Integer, primary_key=True)
    profile_id = db.Column(db.Integer, db.ForeignKey('gst_profiles.id'), nullable=False, index=True)
    return_period = db.Column(db.String(6), index=True, nullable=False)
    import_history_id = db.Column(db.Integer, db.ForeignKey('import_histories.id'), nullable=True, index=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=True, index=True)
    raw_import_id = db.Column(db.Integer, db.ForeignKey('raw_imports.id'), nullable=True)

    # Classification & Buckets
    bucket = db.Column(db.String(50), nullable=False, index=True)
    severity = db.Column(db.String(20), default='INFO', index=True)  # BLOCKING, WARNING, INFO
    category = db.Column(db.String(50), index=True)  # Tax, GSTIN, HSN, Classification, Duplicate, Settlement
    code = db.Column(db.String(100), index=True)
    description = db.Column(db.Text)

    # Amounts & Variances
    source_taxable = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    normalized_taxable = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    variance_taxable = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    source_tax = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    normalized_tax = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))
    variance_tax = db.Column(db.Numeric(15, 2), default=Decimal('0.00'))

    # Resolution Lifecycle
    resolution_status = db.Column(db.String(30), default='OPEN', index=True)  # OPEN, RESOLVED, IGNORED, OVERRIDDEN
    resolution_note = db.Column(db.Text)
    resolved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    profile = db.relationship('GSTProfile', backref=db.backref('reconciliation_records', lazy='dynamic'))
    transaction = db.relationship('Transaction', backref=db.backref('reconciliation_record', uselist=False))

    @property
    def status(self):
        return self.resolution_status

    @status.setter
    def status(self, val):
        self.resolution_status = val

    @property
    def resolution_notes(self):
        return self.resolution_note

    @resolution_notes.setter
    def resolution_notes(self, val):
        self.resolution_note = val

    def to_dict(self):
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "return_period": self.return_period,
            "import_history_id": self.import_history_id,
            "transaction_id": self.transaction_id,
            "bucket": self.bucket,
            "severity": self.severity,
            "category": self.category,
            "code": self.code,
            "description": self.description,
            "source_taxable": float(self.source_taxable or 0),
            "normalized_taxable": float(self.normalized_taxable or 0),
            "variance_taxable": float(self.variance_taxable or 0),
            "source_tax": float(self.source_tax or 0),
            "normalized_tax": float(self.normalized_tax or 0),
            "variance_tax": float(self.variance_tax or 0),
            "resolution_status": self.resolution_status,
            "status": self.resolution_status,
            "resolution_note": self.resolution_note,
            "resolution_notes": self.resolution_note,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<ReconciliationRecord {self.id}: {self.bucket} [{self.resolution_status}]>"
