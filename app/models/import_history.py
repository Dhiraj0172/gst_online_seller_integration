from datetime import datetime
from app.extensions import db

class ImportHistory(db.Model):
    """
    Model tracking history of imported files.
    """
    __tablename__ = 'import_histories'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    profile_id = db.Column(db.Integer, db.ForeignKey('gst_profiles.id'), nullable=False)
    file_name = db.Column(db.String(255), nullable=False)
    original_file_name = db.Column(db.String(255), nullable=False)
    file_hash = db.Column(db.String(64), index=True)
    file_size = db.Column(db.Integer)
    mime_type = db.Column(db.String(100))
    platform_name = db.Column(db.String(50), nullable=False)
    return_period = db.Column(db.String(6), index=True, nullable=False) # MMYYYY format
    financial_year = db.Column(db.String(9), nullable=False)
    total_rows = db.Column(db.Integer, default=0)
    success_rows = db.Column(db.Integer, default=0)
    warning_rows = db.Column(db.Integer, default=0)
    error_rows = db.Column(db.Integer, default=0)
    skipped_rows = db.Column(db.Integer, default=0)
    # Formalized Lifecycle Statuses
    STATUS_PENDING = 'PENDING'
    STATUS_PROCESSING = 'PROCESSING'
    STATUS_STAGED = 'STAGED'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_PARTIAL = 'PARTIAL'
    STATUS_FAILED = 'FAILED'
    STATUS_SUPERSEDED = 'SUPERSEDED'
    STATUS_CANCELLED = 'CANCELLED'

    VALID_STATUSES = {
        STATUS_PENDING,
        STATUS_PROCESSING,
        STATUS_STAGED,
        STATUS_COMPLETED,
        STATUS_PARTIAL,
        STATUS_FAILED,
        STATUS_SUPERSEDED,
        STATUS_CANCELLED,
    }
    ACTIVE_STATUSES = {STATUS_COMPLETED, STATUS_PARTIAL}

    processing_status = db.Column(db.String(20), index=True, nullable=False) # PROCESSING, STAGED, COMPLETED, PARTIAL, FAILED, SUPERSEDED, CANCELLED
    processing_started_at = db.Column(db.DateTime)
    processing_completed_at = db.Column(db.DateTime)
    processing_duration_ms = db.Column(db.Integer)
    error_summary = db.Column(db.Text) # Stored as JSON text
    warning_summary = db.Column(db.Text) # Stored as JSON text
    raw_file_path = db.Column(db.String(512))
    task_id = db.Column(db.String(128), index=True, nullable=True)
    processed_rows = db.Column(db.Integer, default=0, nullable=True)
    error_message = db.Column(db.String(500), nullable=True)
    is_reprocessed = db.Column(db.Boolean, default=False)
    parent_import_id = db.Column(db.Integer, db.ForeignKey('import_histories.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    profile = db.relationship('GSTProfile', back_populates='import_histories')
    raw_imports = db.relationship('RawImport', back_populates='import_history', cascade='all, delete-orphan')
    transactions = db.relationship('Transaction', back_populates='import_history', cascade='all, delete-orphan')

    @property
    def progress_pct(self) -> float:
        """Calculate processing completion percentage (0.0 to 100.0)."""
        if self.processing_status in (self.STATUS_COMPLETED, self.STATUS_PARTIAL, self.STATUS_STAGED):
            return 100.0
        if self.total_rows and self.total_rows > 0:
            count = self.processed_rows if (self.processed_rows is not None and self.processed_rows > 0) else (
                (self.success_rows or 0) + (self.warning_rows or 0) + (self.error_rows or 0) + (self.skipped_rows or 0)
            )
            pct = (float(count) / float(self.total_rows)) * 100.0
            return min(100.0, max(0.0, round(pct, 1)))
        return 0.0

    @property
    def can_retry(self) -> bool:
        """Return True if import is eligible for retry."""
        return self.processing_status in (
            self.STATUS_FAILED,
            self.STATUS_CANCELLED,
            self.STATUS_PARTIAL,
            'REJECTED_UNREADABLE',
            'REJECTED_INVALID',
            'REJECTED_DUPLICATE_FILE',
            'REJECTED_FROZEN'
        )

    @property
    def can_cancel(self) -> bool:
        """Return True if import is in an active state that can be cancelled."""
        return self.processing_status in (
            self.STATUS_PENDING,
            self.STATUS_PROCESSING,
            self.STATUS_STAGED
        )

    def __repr__(self):
        return f"<ImportHistory {self.id} - {self.platform_name}>"
