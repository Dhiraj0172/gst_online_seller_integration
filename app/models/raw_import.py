from datetime import datetime
from app.extensions import db

class RawImport(db.Model):
    """
    Model representing raw rows imported from files.
    """
    __tablename__ = 'raw_imports'

    id = db.Column(db.Integer, primary_key=True)
    import_history_id = db.Column(db.Integer, db.ForeignKey('import_histories.id'), nullable=False)
    sheet_name = db.Column(db.String(100))
    row_number = db.Column(db.Integer, nullable=False)
    raw_data = db.Column(db.Text) # JSON string representation of the row dict
    status = db.Column(db.String(20)) # SUCCESS/WARNING/ERROR/SKIPPED
    errors = db.Column(db.Text) # JSON string
    warnings = db.Column(db.Text) # JSON string
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    import_history = db.relationship('ImportHistory', back_populates='raw_imports')
    transactions = db.relationship('Transaction', back_populates='raw_import', cascade='all, delete-orphan')

    def __repr__(self):
        return f"<RawImport {self.id} - Row {self.row_number}>"
