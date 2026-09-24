"""Focused regression tests for HIGH-02 import performance hardening.

Verifies:
- Batched flush / relationship-based linkage without per-row flush
- Correct RawImport linkage (Transaction.raw_import_id == RawImport.id)
- Correct Transaction persistence and tax calculations
- In-file and cross-import duplicate detection preservation
- Repeated import behavior (idempotency, no duplicate Transactions)
- Injected failure rolls back all partial rows and leaves 0 partial writes
- ImportHistory failure state is correctly updated to FAILED
- Tenant / profile isolation is strictly preserved
"""
import io
import json
import pytest
from decimal import Decimal
from datetime import date
from unittest.mock import patch

from app.models import User, GSTProfile, ImportHistory, RawImport, Transaction
from app.extensions import db as _db
from app.services.import_service import process_import, _create_transaction_from_normalized


import uuid

@pytest.fixture
def perf_seller(app, db):
    """Create two users with distinct GST profiles for tenancy tests."""
    suffix = uuid.uuid4().hex[:8]
    u1_name = f'perf_user1_{suffix}'
    u2_name = f'perf_user2_{suffix}'
    u1_email = f'perf1_{suffix}@example.com'
    u2_email = f'perf2_{suffix}@example.com'
    gstin1 = f'27AAAA{suffix[:4].upper()}1Z5'
    gstin2 = f'29BBBB{suffix[:4].upper()}1Z6'

    with app.app_context():
        u1 = User(username=u1_name, email=u1_email)
        u1.set_password('Pass123!')
        _db.session.add(u1)

        u2 = User(username=u2_name, email=u2_email)
        u2.set_password('Pass123!')
        _db.session.add(u2)
        _db.session.commit()

        p1 = GSTProfile(
            user_id=u1.id,
            gstin=gstin1,
            legal_name='Perf Test Co 1',
            trade_name='Perf 1',
            state_code='27',
            state_name='Maharashtra',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(p1)

        p2 = GSTProfile(
            user_id=u2.id,
            gstin=gstin2,
            legal_name='Perf Test Co 2',
            trade_name='Perf 2',
            state_code='29',
            state_name='Karnataka',
            financial_year='2024-25',
            filing_frequency='Monthly'
        )
        _db.session.add(p2)
        _db.session.commit()

        p1_id = p1.id
        p2_id = p2.id
        u1_id = u1.id
        u2_id = u2.id

    return {
        'u1_id': u1_id,
        'u2_id': u2_id,
        'u1_name': u1_name,
        'u2_name': u2_name,
        'p1_id': p1_id,
        'p2_id': p2_id,
        'gstin1': gstin1,
        'gstin2': gstin2,
    }


def _make_csv(rows_data):
    """Generate CSV string from list of dicts with standard headers."""
    headers = [
        'Invoice Number', 'Invoice Date', 'Customer GSTIN', 'Place of Supply',
        'Taxable Value', 'Rate', 'CGST', 'SGST', 'IGST', 'Total Value'
    ]
    lines = [','.join(headers)]
    for r in rows_data:
        line = ','.join(str(r.get(h, '')) for h in headers)
        lines.append(line)
    return '\n'.join(lines)


class TestImportPerformanceHardening:
    """HIGH-02 test suite verifying batched persistence, relationships, and invariants."""

    def test_relationship_based_linkage_and_persistence(self, app, perf_seller):
        """Transaction.raw_import_id is correctly wired via ORM relationship without per-row flush."""
        client = app.test_client()
        client.post('/login', data={'username': perf_seller['u1_name'], 'password': 'Pass123!'}, follow_redirects=True)
        with client.session_transaction() as sess:
            sess['active_profile_id'] = perf_seller['p1_id']
            sess['return_period'] = '012025'

        csv_rows = [
            {
                'Invoice Number': f'INV-PERF-{i:03d}',
                'Invoice Date': '01-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '1000.00',
                'Rate': '18',
                'CGST': '90.00',
                'SGST': '90.00',
                'IGST': '0.00',
                'Total Value': '1180.00',
            }
            for i in range(1, 11)
        ]
        csv_text = _make_csv(csv_rows)
        data = {
            'file': (io.BytesIO(csv_text.encode('utf-8')), 'test_perf_link.csv'),
            'platform': 'Generic'
        }

        resp = client.post('/import/upload', data=data, content_type='multipart/form-data', follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            ih = ImportHistory.query.filter_by(file_name='test_perf_link.csv', profile_id=perf_seller['p1_id']).first()
            assert ih is not None
            assert ih.processing_status == 'COMPLETED'
            assert ih.total_rows == 10
            assert ih.success_rows == 10
            assert ih.error_rows == 0
            assert ih.skipped_rows == 0

            # Verify every Transaction has a valid raw_import_id matching a RawImport
            txs = Transaction.query.filter_by(import_history_id=ih.id).all()
            assert len(txs) == 10

            raws = RawImport.query.filter_by(import_history_id=ih.id).all()
            assert len(raws) == 10

            raw_ids = {r.id for r in raws}
            for tx in txs:
                assert tx.raw_import_id is not None
                assert tx.raw_import_id in raw_ids
                assert tx.raw_import is not None
                assert tx.raw_import.id == tx.raw_import_id
                assert tx.profile_id == perf_seller['p1_id']
                assert tx.taxable_value == Decimal('1000.00')

    def test_duplicate_detection_in_file_and_repeated_import(self, app, perf_seller):
        """Duplicate detection in-file and against existing DB works and preserves counts."""
        client = app.test_client()
        client.post('/login', data={'username': perf_seller['u1_name'], 'password': 'Pass123!'}, follow_redirects=True)
        with client.session_transaction() as sess:
            sess['active_profile_id'] = perf_seller['p1_id']
            sess['return_period'] = '012025'

        # File containing 3 rows: row 1 unique, row 2 unique, row 3 duplicate of row 1
        csv_rows = [
            {
                'Invoice Number': 'INV-DUP-001',
                'Invoice Date': '05-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '500.00',
                'Rate': '18',
                'CGST': '45.00',
                'SGST': '45.00',
                'IGST': '0.00',
                'Total Value': '590.00',
            },
            {
                'Invoice Number': 'INV-DUP-002',
                'Invoice Date': '05-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '600.00',
                'Rate': '18',
                'CGST': '54.00',
                'SGST': '54.00',
                'IGST': '0.00',
                'Total Value': '708.00',
            },
            {
                'Invoice Number': 'INV-DUP-001',
                'Invoice Date': '05-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '500.00',
                'Rate': '18',
                'CGST': '45.00',
                'SGST': '45.00',
                'IGST': '0.00',
                'Total Value': '590.00',
            },
        ]
        csv_text = _make_csv(csv_rows)
        data = {
            'file': (io.BytesIO(csv_text.encode('utf-8')), 'test_dup.csv'),
            'platform': 'Generic'
        }

        resp = client.post('/import/upload', data=data, content_type='multipart/form-data', follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            ih1 = ImportHistory.query.filter_by(file_name='test_dup.csv', profile_id=perf_seller['p1_id']).first()
            assert ih1 is not None
            assert ih1.total_rows == 3
            assert ih1.success_rows == 2
            assert ih1.skipped_rows == 1

            # Only 2 Transactions created
            txs = Transaction.query.filter_by(import_history_id=ih1.id).all()
            assert len(txs) == 2

            # 3 RawImports created (one SKIPPED)
            raws = RawImport.query.filter_by(import_history_id=ih1.id).all()
            assert len(raws) == 3
            skipped_raws = [r for r in raws if r.status == 'SKIPPED']
            assert len(skipped_raws) == 1
            assert 'Duplicate of row' in skipped_raws[0].errors

        # Now re-upload the same file with allow_duplicate_file=1 to test DUPLICATE_EXISTING
        data2 = {
            'file': (io.BytesIO(csv_text.encode('utf-8')), 'test_dup.csv'),
            'platform': 'Generic',
            'allow_duplicate_file': '1'
        }
        resp2 = client.post('/import/upload', data=data2, content_type='multipart/form-data', follow_redirects=True)
        assert resp2.status_code == 200

        with app.app_context():
            histories = ImportHistory.query.filter_by(file_name='test_dup.csv', profile_id=perf_seller['p1_id']).order_by(ImportHistory.id.desc()).all()
            ih2 = histories[0]
            assert ih2.id != ih1.id
            assert ih2.total_rows == 3
            assert ih2.success_rows == 0
            assert ih2.skipped_rows == 3  # All 3 rows detected as duplicate against existing DB / in-file

            # No new Transactions created for the re-import
            txs_ih2 = Transaction.query.filter_by(import_history_id=ih2.id).all()
            assert len(txs_ih2) == 0

    def test_injected_failure_rolls_back_everything_cleanly(self, app, perf_seller, monkeypatch):
        """Injected failure rolls back any batched/flushed rows and preserves ImportHistory=FAILED."""
        client = app.test_client()
        client.post('/login', data={'username': perf_seller['u1_name'], 'password': 'Pass123!'}, follow_redirects=True)
        with client.session_transaction() as sess:
            sess['active_profile_id'] = perf_seller['p1_id']
            sess['return_period'] = '012025'

        csv_rows = [
            {
                'Invoice Number': f'INV-FAIL-{i:03d}',
                'Invoice Date': '01-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '1000.00',
                'Rate': '18',
                'CGST': '90.00',
                'SGST': '90.00',
                'IGST': '0.00',
                'Total Value': '1180.00',
            }
            for i in range(1, 15)
        ]
        csv_text = _make_csv(csv_rows)
        data = {
            'file': (io.BytesIO(csv_text.encode('utf-8')), 'test_batch_fail.csv'),
            'platform': 'Generic'
        }

        # Mock classify_transaction to succeed for the first 5 rows and fail on row 6
        call_count = [0]
        original_classify = None

        def fail_on_sixth(norm, profile, period):
            call_count[0] += 1
            if call_count[0] >= 6:
                raise RuntimeError("Simulated crash at batch row 6")
            from app.services.classification_service import classify_transaction
            return classify_transaction(norm, profile, period)

        monkeypatch.setattr('app.routes.import_routes.classify_transaction', fail_on_sixth)

        resp = client.post('/import/upload', data=data, content_type='multipart/form-data', follow_redirects=False)
        assert resp.status_code == 302

        with app.app_context():
            ih = ImportHistory.query.filter_by(file_name='test_batch_fail.csv', profile_id=perf_seller['p1_id']).first()
            assert ih is not None, "ImportHistory record must be preserved"
            assert ih.processing_status == 'FAILED'
            assert ih.processing_completed_at is not None

            # Verify complete rollback: ZERO transactions and ZERO raw imports remain
            tx_count = Transaction.query.filter_by(import_history_id=ih.id).count()
            assert tx_count == 0, f"Expected 0 transactions after rollback, found {tx_count}"

            raw_count = RawImport.query.filter_by(import_history_id=ih.id).count()
            assert raw_count == 0, f"Expected 0 raw imports after rollback, found {raw_count}"

    def test_tenant_isolation_preserved(self, app, perf_seller):
        """Identical transactions under profile 1 and profile 2 do NOT collide or leak."""
        csv_rows = [
            {
                'Invoice Number': 'INV-ISOLATION-01',
                'Invoice Date': '10-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '1000.00',
                'Rate': '18',
                'CGST': '90.00',
                'SGST': '90.00',
                'IGST': '0.00',
                'Total Value': '1180.00',
            }
        ]
        csv_text = _make_csv(csv_rows)

        # Import under profile 1
        client1 = app.test_client()
        client1.post('/login', data={'username': perf_seller['u1_name'], 'password': 'Pass123!'}, follow_redirects=True)
        with client1.session_transaction() as sess:
            sess['active_profile_id'] = perf_seller['p1_id']
            sess['return_period'] = '012025'

        resp1 = client1.post(
            '/import/upload',
            data={'file': (io.BytesIO(csv_text.encode('utf-8')), 'test_iso.csv'), 'platform': 'Generic'},
            content_type='multipart/form-data',
            follow_redirects=True
        )
        assert resp1.status_code == 200

        # Logout user 1
        client1.get('/logout', follow_redirects=True)

        # Import identical file under profile 2
        client2 = app.test_client()
        client2.post('/login', data={'username': perf_seller['u2_name'], 'password': 'Pass123!'}, follow_redirects=True)
        with client2.session_transaction() as sess:
            sess['active_profile_id'] = perf_seller['p2_id']
            sess['return_period'] = '012025'

        resp2 = client2.post(
            '/import/upload',
            data={'file': (io.BytesIO(csv_text.encode('utf-8')), 'test_iso.csv'), 'platform': 'Generic'},
            content_type='multipart/form-data',
            follow_redirects=True
        )
        assert resp2.status_code == 200

        with app.app_context():
            ih1 = ImportHistory.query.filter_by(file_name='test_iso.csv', profile_id=perf_seller['p1_id']).first()
            ih2 = ImportHistory.query.filter_by(file_name='test_iso.csv', profile_id=perf_seller['p2_id']).first()

            assert ih1 is not None and ih1.processing_status == 'COMPLETED'
            assert ih2 is not None and ih2.processing_status == 'COMPLETED'

            # Both profiles successfully imported 1 row (profile 2 did not treat profile 1 as duplicate)
            assert ih1.success_rows == 1
            assert ih2.success_rows == 1

            tx1 = Transaction.query.filter_by(import_history_id=ih1.id).first()
            tx2 = Transaction.query.filter_by(import_history_id=ih2.id).first()

            assert tx1.profile_id == perf_seller['p1_id']
            assert tx2.profile_id == perf_seller['p2_id']
            assert tx1.id != tx2.id

    def test_process_import_service_batching_and_linkage(self, app, perf_seller, tmp_path):
        """process_import service path works with batched flushes, chunk duplicate detection, and linkage."""
        file_path = str(tmp_path / 'service_test.csv')
        csv_rows = [
            {
                'Invoice Number': f'INV-SRV-{i:03d}',
                'Invoice Date': '01-01-2025',
                'Customer GSTIN': '27AAAAA0000A1Z5',
                'Place of Supply': '27',
                'Taxable Value': '2000.00',
                'Rate': '18',
                'CGST': '180.00',
                'SGST': '180.00',
                'IGST': '0.00',
                'Total Value': '2360.00',
            }
            for i in range(1, 21)
        ]
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(_make_csv(csv_rows))

        with app.app_context():
            res = process_import(
                file_path=file_path,
                profile_id=perf_seller['p1_id'],
                platform_name='Generic',
                user_id=perf_seller['u1_id'],
                return_period='012025',
                financial_year='2024-25'
            )
            assert res.total_rows == 20
            assert res.success_rows == 20
            assert res.error_rows == 0
            assert res.skipped_rows == 0

            ih = _db.session.get(ImportHistory, res.import_history_id)
            assert ih.processing_status == 'COMPLETED'

            txs = Transaction.query.filter_by(import_history_id=ih.id).all()
            assert len(txs) == 20
            total_taxable = sum(t.taxable_value for t in txs)
            assert total_taxable == Decimal('40000.00')
            for tx in txs:
                assert tx.raw_import_id is not None
                assert tx.raw_import is not None
                assert tx.raw_import.id == tx.raw_import_id
