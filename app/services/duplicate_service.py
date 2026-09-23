"""Duplicate detection for marketplace imports.

Three duplicate situations are distinguished, because they need different
handling:

``DUPLICATE_FILE``
    The same source file (same sha256) was already imported for this GST
    profile. Rejected up front, before any row is touched.

``DUPLICATE_IN_FILE``
    The same transaction appears more than once inside the uploaded file.

``DUPLICATE_EXISTING``
    The transaction is already present for this profile from an earlier import.

Duplicate rows are **marked, never silently discarded**: the raw row is stored,
the row's status becomes ``SKIPPED``, the reason names the colliding row /
transaction / import, and the row is counted in ``ImportHistory.skipped_rows``
and itemised in the import report. No ``Transaction`` is created for a
duplicate, so a re-import can never double-count into GSTR-1 or TCS.

Business key
------------
The key is deliberately conservative and uses only fields this project actually
stores. Invoice number alone is *not* used: the key includes the source
platform and marketplace, the document kind and number, the marketplace line
identifier, the document date, the counter-party GSTIN, HSN, place of supply,
quantity, description and the money amounts.

Consequence, by design:

* the same invoice number from two different marketplaces stays distinct;
* two lines of one invoice stay distinct whenever the source provides a line or
  order identifier (Amazon Order ID, Flipkart Order Item ID, Meesho Sub Order
  ID);
* a file whose source gives no line identifier cannot distinguish two truly
  identical lines, so the second one is reported as a duplicate rather than
  silently imported twice.

Only *active* (not soft-deleted) transactions take part in ``DUPLICATE_EXISTING``
matching, so deleting a wrong transaction and re-importing the file is allowed.
"""
import hashlib
import json
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.models import ImportHistory, Transaction
from app.extensions import db

FINGERPRINT_VERSION = 'v1'

# Statuses that count as a previously completed import of the same file.
COMPLETED_STATUSES = ('COMPLETED', 'PARTIAL')

DUPLICATE_FILE = 'DUPLICATE_FILE'
DUPLICATE_IN_FILE = 'DUPLICATE_IN_FILE'
DUPLICATE_EXISTING = 'DUPLICATE_EXISTING'


def _money(value: Any) -> str:
    """Quantize a money amount to 2dp for a stable key component."""
    if value in (None, ''):
        return '0.00'
    try:
        return f'{Decimal(str(value)):.2f}'
    except Exception:
        return str(value).strip()


def _text(value: Any) -> str:
    return str(value if value is not None else '').strip().upper()


def document_identity(normalized: Dict[str, Any]) -> Tuple[str, str]:
    """Return (kind, number) for the row: a credit/debit note or an invoice."""
    note_number = _text(normalized.get('note_number'))
    if note_number:
        return 'NOTE', note_number
    return 'INVOICE', _text(normalized.get('invoice_number'))


def fingerprint_parts(normalized: Dict[str, Any], profile_id: int,
                      source_platform: str) -> List[str]:
    """The exact components of the business key (also used for reporting)."""
    kind, number = document_identity(normalized)
    line_id = (
        _text(normalized.get('order_item_id'))
        or _text(normalized.get('sub_order_id'))
        or _text(normalized.get('order_id'))
    )
    total_tax = (
        (normalized.get('cgst_amount') or Decimal('0'))
        + (normalized.get('sgst_amount') or Decimal('0'))
        + (normalized.get('igst_amount') or Decimal('0'))
        + (normalized.get('cess_amount') or Decimal('0'))
    )
    return [
        FINGERPRINT_VERSION,
        str(profile_id),
        _text(source_platform or normalized.get('source_platform')),
        _text(normalized.get('marketplace_name')),
        kind,
        number,
        _text(normalized.get('invoice_date') or normalized.get('note_date')),
        line_id,
        _text(normalized.get('customer_gstin')),
        _text(normalized.get('place_of_supply')),
        _text(normalized.get('hsn_sac')),
        _money(normalized.get('quantity')),
        _text(normalized.get('description')),
        _money(normalized.get('taxable_value')),
        _money(normalized.get('invoice_value')),
        # Rates are part of the key too: the same amounts at a different rate
        # are not the same source line for GSTR-1 purposes.
        _money(normalized.get('tax_rate')),
        _money(normalized.get('cgst_rate')),
        _money(normalized.get('sgst_rate')),
        _money(normalized.get('igst_rate')),
        _money(normalized.get('cess_rate')),
        _money(total_tax),
    ]


def build_fingerprint(normalized: Dict[str, Any], profile_id: int,
                      source_platform: str) -> str:
    """sha256 business-key fingerprint for one normalized transaction row."""
    key = '|'.join(fingerprint_parts(normalized, profile_id, source_platform))
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def find_prior_file_import(profile_id: int, file_hash: Optional[str],
                           platform_name: Optional[str] = None) -> Optional[ImportHistory]:
    """A previously *completed* import of the same file for this profile.

    The marketplace is part of the import's identity: the same bytes uploaded
    under a different source label produce different row fingerprints by design
    (see ``build_fingerprint``), so it is not the same import.
    """
    if not file_hash:
        return None
    query = ImportHistory.query.filter(
        ImportHistory.profile_id == profile_id,
        ImportHistory.file_hash == file_hash,
        ImportHistory.processing_status.in_(COMPLETED_STATUSES),
        ImportHistory.total_rows > 0,
    )
    if platform_name:
        query = query.filter(ImportHistory.platform_name == platform_name)
    return query.order_by(ImportHistory.id.asc()).first()


def find_existing_transactions(profile_id: int,
                               fingerprints: Iterable[str]) -> Dict[str, Transaction]:
    """Map fingerprint -> active transaction already stored for this profile."""
    wanted = {fingerprint for fingerprint in fingerprints if fingerprint}
    if not wanted:
        return {}
    existing: Dict[str, Transaction] = {}
    chunk = list(wanted)
    # SQLite parameter limits: look the keys up in bounded batches.
    batch_size = 400
    for start in range(0, len(chunk), batch_size):
        batch = chunk[start:start + batch_size]
        rows = (
            Transaction.query
            .filter(
                Transaction.profile_id == profile_id,
                Transaction.is_deleted.is_(False),
                Transaction.row_fingerprint.in_(batch),
            )
            .order_by(Transaction.id.asc())
            .all()
        )
        for transaction in rows:
            existing.setdefault(transaction.row_fingerprint, transaction)
    return existing


def duplicate_report_entry(row_number: int, code: str, message: str,
                           **extra: Any) -> Dict[str, Any]:
    entry = {'row': row_number, 'code': code, 'message': message}
    entry.update({key: value for key, value in extra.items() if value is not None})
    return entry


def file_duplicate_report(prior: ImportHistory) -> Dict[str, Any]:
    return {
        'code': DUPLICATE_FILE,
        'message': (
            f'This exact file was already imported successfully on '
            f'{prior.created_at.strftime("%Y-%m-%d %H:%M") if prior.created_at else "an earlier date"} '
            f'as import #{prior.id} ({prior.file_name}, {prior.total_rows} rows). Re-upload the '
            f'same file only if you really want to import its rows again (this will then be '
            f'reported row by row as duplicates).'
        ),
        'duplicate_of_import_id': prior.id,
        'duplicate_of_file': prior.file_name,
        'duplicate_of_imported_at': prior.created_at.isoformat() if prior.created_at else None,
        'duplicate_of_total_rows': prior.total_rows,
    }


def duplicate_envelope(rejections: List[Dict[str, Any]],
                       duplicates: List[Dict[str, Any]],
                       warnings: List[Dict[str, Any]],
                       counts: Dict[str, int]) -> Tuple[str, str]:
    """Serialize the import report into the ImportHistory summary columns."""
    error_summary = json.dumps({'counts': counts, 'errors': rejections})
    warning_summary = json.dumps({'counts': counts, 'duplicates': duplicates,
                                  'warnings': warnings})
    return error_summary, warning_summary
