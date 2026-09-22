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
    processing_status = db.Column(db.String(20), index=True, nullable=False) # PENDING, VALIDATING, PARSING, NORMALIZING, CLASSIFYING, COMPLETED, FAILED
    processing_started_at = db.Column(db.DateTime)
    processing_completed_at = db.Column(db.DateTime)
    processing_duration_ms = db.Column(db.Integer)
    error_summary = db.Column(db.Text) # Stored as JSON text
    warning_summary = db.Column(db.Text) # Stored as JSON text
    raw_file_path = db.Column(db.String(512))
    is_reprocessed = db.Column(db.Boolean, default=False)
    parent_import_id = db.Column(db.Integer, db.ForeignKey('import_histories.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    profile = db.relationship('GSTProfile', back_populates='import_histories')
    raw_imports = db.relationship('RawImport', back_populates='import_history', cascade='all, delete-orphan')
    transactions = db.relationship('Transaction', back_populates='import_history', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<ImportHistory {self.id} - {self.platform_name}>"
