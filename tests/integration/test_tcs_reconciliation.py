"""
Integration tests for the TCS reconciliation workflow.

Covers the end-to-end path:
  marketplace transactions (DB) -> Section 52 net-value aggregation
  -> portal TCS report parsing -> matching/reconciliation -> export.

Official bases referenced in assertions:
  - CGST Act s.52(1): TCS on net value of taxable supplies
    (aggregate taxable supplies minus taxable supplies returned to suppliers).
  - Notification 15/2024-Central Tax dated 10-07-2024: TCS rate 1% -> 0.5%.
"""
import os
import sys
import io
import openpyxl
import pytest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.extensions import db as _db
from app.models import User, GSTProfile, Transaction, TCSReconciliation, AuditLog
from app.services.ecom_service import get_ecom_supplies, aggregate_ecom
from app.services.tcs_service import (
    TCSReportParser, import_tcs_report, reconcile_tcs,
    apply_adjustment, export_reconciliation,
)


SELLER_GSTIN = '27AABCU9603R1ZM'
ECOM_GSTIN_A = '27AABCA1234B1ZM'


def _make_profile(user_id, gstin=SELLER_GSTIN):
    return GSTProfile(
        user_id=user_id,
        gstin=gstin,
        legal_name='Test Seller Pvt Ltd',
        trade_name='Test Seller',
        state_code=gstin[:2],
        state_name='Maharashtra',
        financial_year='2024-25',
        filing_frequency='monthly',
    )


def _tx(profile_id, import_id, raw_id, **kw):
    """Build a Transaction with sane defaults."""
    data = dict(
        profile_id=profile_id,
        import_history_id=import_id,
        raw_import_id=raw_id,
        invoice_number=kw.get('invoice_number', 'INV-1'),
        invoice_date=kw.get('invoice_date', date(2024, 8, 15)),
        place_of_supply=kw.get('place_of_supply', '29'),
        seller_gstin=SELLER_GSTIN,
        hsn_sac='6109',
        taxable_value=Decimal(str(kw.get('taxable_value', 10000))),
        tax_rate=Decimal(str(kw.get('tax_rate', 18))),
        igst_amount=Decimal(str(kw.get('igst_amount', 1800))),
        cgst_amount=Decimal(str(kw.get('cgst_amount', 0))),
        sgst_amount=Decimal(str(kw.get('sgst_amount', 0))),
        cess_amount=Decimal('0'),
        ecommerce_gstin=kw.get('ecommerce_gstin', ECOM_GSTIN_A),
        marketplace_name='Amazon',
        supply_type=kw.get('supply_type', 'B2CS'),
        return_flag=kw.get('return_flag', False),
        return_reason=kw.get('return_reason'),
        note_type=kw.get('note_type'),
        is_deleted=False,
    )
    return Transaction(**data)


@pytest.fixture
def tcs_env(app, db):
    """Seed a user, profile, import history rows and a set of transactions."""
    import uuid
    suffix = uuid.uuid4().hex[:8]
    created = {}
    with app.app_context():
        user = User(username=f'tcs_user_{suffix}', email=f'tcs_{suffix}@test.com')
        user.set_password('testpass123')
        db.session.add(user)
        db.session.commit()
        created['user_id'] = user.id

        # Unique GSTIN per test run (27 + 5 letters + 4 digits + letter + digit + Z + digit)
        gstin = f'27TSTC{suffix[:2].upper()}1234A1Z{len(suffix) % 10}'
        profile = _make_profile(user.id, gstin=gstin)
        db.session.add(profile)
        db.session.commit()
        created['profile_id'] = profile.id

        from app.models import ImportHistory, RawImport
        ih = ImportHistory(
            user_id=user.id, profile_id=profile.id,
            file_name='amazon_sample.xlsx', original_file_name='amazon_sample.xlsx',
            file_hash='hash', file_size=100, platform_name='Amazon',
            return_period='082024', financial_year='2024-25',
            processing_status='COMPLETED',
        )
        db.session.add(ih)
        db.session.commit()
        ri = RawImport(import_history_id=ih.id, sheet_name='Sheet1',
                       row_number=1, raw_data='{}', status='SUCCESS')
        db.session.add(ri)
        db.session.commit()

        txs = [
            # gross taxable supply, interstate, 18%
            _tx(profile.id, ih.id, ri.id, invoice_number='INV-1', taxable_value=10000),
            # supplier return -> deducted from Section 52 net value
            _tx(profile.id, ih.id, ri.id, invoice_number='RET-1', taxable_value=2000,
                return_flag=True, return_reason='SUPPLIER_RETURN'),
            # cancellation -> NOT deducted
            _tx(profile.id, ih.id, ri.id, invoice_number='CANC-1', taxable_value=1000,
                return_flag=True, return_reason='CANCELLATION'),
            # credit note -> NOT deducted
            _tx(profile.id, ih.id, ri.id, invoice_number='CN-1', taxable_value=500,
                note_type='CREDIT'),
            # refund -> NOT deducted
            _tx(profile.id, ih.id, ri.id, invoice_number='REF-1', taxable_value=800,
                return_flag=True, return_reason='REFUND'),
            # debit note -> NOT added
            _tx(profile.id, ih.id, ri.id, invoice_number='DN-1', taxable_value=300,
                note_type='DEBIT'),
        ]
        db.session.add_all(txs)
        db.session.commit()

        yield {
            'app': app, 'user_id': user.id, 'profile_id': profile.id,
            'import_id': ih.id, 'raw_id': ri.id,
        }

        # Teardown: remove everything this fixture created so the next test
        # starts clean (the in-memory SQLite DB is shared across the session).
        try:
            TCSReconciliation.query.filter_by(profile_id=created['profile_id']).delete()
            AuditLog.query.filter_by(user_id=created['user_id']).delete()
            db.session.delete(profile)
            db.session.delete(user)
            db.session.commit()
        except Exception:
            db.session.rollback()


class TestSection52NetValue:
    """Section 52 net value = aggregate taxable supplies - supplier returns."""

    def test_only_supplier_returns_reduce_net_value(self, tcs_env):
        app = tcs_env['app']
        with app.app_context():
            rows = get_ecom_supplies(tcs_env['profile_id'], '082024')
            assert len(rows) == 6, 'all six e-commerce events loaded'

            agg = aggregate_ecom(rows, '082024', seller_state='27')
            assert len(agg) == 1
            a = agg[0]

            # Only the genuine supply row forms the Section 52 supplies figure
            assert a['gross_taxable_value'] == Decimal('10000.00')
            assert a['all_rows_value'] == Decimal('14600.00')
            assert a['supplier_returns'] == Decimal('2000.00')
            assert a['cancellations'] == Decimal('1000.00')
            assert a['credit_notes'] == Decimal('500.00')
            assert a['refunds'] == Decimal('800.00')
            assert a['debit_notes'] == Decimal('300.00')

            # 10000 supplies - 2000 supplier returns only
            assert a['net_taxable_value'] == Decimal('8000.00')

    def test_tcs_charged_on_net_value_post_july_2024(self, tcs_env):
        """0.5% IGST (inter-state) on net value 8000 -> 40.00."""
        app = tcs_env['app']
        with app.app_context():
            rows = get_ecom_supplies(tcs_env['profile_id'], '082024')
            a = aggregate_ecom(rows, '082024', seller_state='27')[0]
            assert a['supply_type'] == 'INTER'
            assert a['our_tcs_igst'] == Decimal('40.00')
            assert a['our_tcs_total'] == Decimal('40.00')

    def test_pre_july_2024_uses_1_percent(self, tcs_env):
        """Same data in a June-2024 period -> 1% IGST = 80.00."""
        app = tcs_env['app']
        with app.app_context():
            # move invoice dates into June 2024
            Transaction.query.filter_by(profile_id=tcs_env['profile_id']).update(
                {'invoice_date': date(2024, 6, 15)}
            )
            _db.session.commit()
            rows = get_ecom_supplies(tcs_env['profile_id'], '062024')
            a = aggregate_ecom(rows, '062024', seller_state='27')[0]
            assert a['net_taxable_value'] == Decimal('8000.00')
            assert a['our_tcs_igst'] == Decimal('80.00')
            assert a['our_tcs_total'] == Decimal('80.00')


class TestTCSReportParser:
    """Schema-specific parsing of portal / marketplace reports."""

    def test_parse_gstr8_state_wise_fixture(self):
        path = os.path.join(os.path.dirname(__file__), '..', 'fixtures',
                            'tcs_portal_report.xlsx')
        if not os.path.exists(path):
            pytest.skip('tcs_portal_report.xlsx fixture not generated')
        fmt, rows = TCSReportParser.parse_file(path)
        assert fmt == 'gstr8'
        assert len(rows) == 4
        states = {r['state_code'] for r in rows}
        assert states == {'27', '29', '07', '33'}
        for r in rows:
            assert 'ecommerce_gstin' not in r
            assert r['taxable_value'] > 0

    def test_parse_marketplace_certificate(self, tmp_path):
        """Marketplace certificate carries E-Commerce GSTIN -> finer matching."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['E-Commerce GSTIN', 'State Code', 'State Name',
                   'Taxable Value', 'TCS Amount', 'CGST TCS', 'SGST TCS', 'IGST TCS'])
        ws.append([ECOM_GSTIN_A, '29', 'Karnataka', 10000, 50, 0, 0, 50])
        ws.append(['29AABCF5678D1ZP', '29', 'Karnataka', 5000, 25, 0, 0, 25])
        path = os.path.join(str(tmp_path), 'marketplace_tcs.xlsx')
        wb.save(path)

        fmt, rows = TCSReportParser.parse_file(path)
        assert fmt == 'marketplace'
        assert len(rows) == 2
        assert {r['ecommerce_gstin'] for r in rows} == {ECOM_GSTIN_A, '29AABCF5678D1ZP'}

    def test_unknown_schema_is_not_silently_accepted(self, tmp_path):
        """A file without a state column yields no rows (no bogus defaults)."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Foo', 'Bar'])
        ws.append([1, 2])
        path = os.path.join(str(tmp_path), 'junk.xlsx')
        wb.save(path)
        fmt, rows = TCSReportParser.parse_file(path)
        assert rows == []


class TestReconciliationMatching:
    """Matching behaviour and AMBIGUOUS surfacing."""

    def _import_gstr8(self, app, profile_id, user_id, tmp_path, states):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['State Code', 'State Name', 'Taxable Value', 'TCS Amount',
                   'CGST TCS', 'SGST TCS', 'IGST TCS'])
        for code, name, taxable, tcs in states:
            ws.append([code, name, taxable, tcs, 0, tcs, 0])
        path = os.path.join(str(tmp_path), 'portal.xlsx')
        wb.save(path)
        with app.app_context():
            res = import_tcs_report(path, profile_id, '082024', user_id)
            assert not res.errors, res.errors
        return path

    def test_matched_state_marks_matched(self, tcs_env, tmp_path):
        app = tcs_env['app']
        # internal net = 8000, TCS (0.5% inter) = 40.00
        self._import_gstr8(app, tcs_env['profile_id'], tcs_env['user_id'],
                           tmp_path, [('29', 'Karnataka', 8000, 40.00)])
        with app.app_context():
            result = reconcile_tcs(tcs_env['profile_id'], '082024', tcs_env['user_id'])
            assert result['status'] == 'COMPLETED'
            assert result['matched'] == 1
            assert result['mismatched'] == 0

    def test_mismatched_state_marks_mismatch(self, tcs_env, tmp_path):
        app = tcs_env['app']
        self._import_gstr8(app, tcs_env['profile_id'], tcs_env['user_id'],
                           tmp_path, [('29', 'Karnataka', 99999, 500.00)])
        with app.app_context():
            result = reconcile_tcs(tcs_env['profile_id'], '082024', tcs_env['user_id'])
            assert result['mismatched'] == 1

    def test_multiple_ecom_gstins_same_state_flags_ambiguous(self, tcs_env, tmp_path):
        """Portal has state-level rows only; internal has 2 ECOs in that state."""
        app = tcs_env['app']
        with app.app_context():
            # add a second e-commerce operator in the same state (29)
            extra = _tx(tcs_env['profile_id'], tcs_env['import_id'], tcs_env['raw_id'],
                        invoice_number='INV-2', taxable_value=5000,
                        ecommerce_gstin='29AABCF5678D1ZP')
            _db.session.add(extra)
            _db.session.commit()

        self._import_gstr8(app, tcs_env['profile_id'], tcs_env['user_id'],
                           tmp_path, [('29', 'Karnataka', 13000, 65.00)])
        with app.app_context():
            result = reconcile_tcs(tcs_env['profile_id'], '082024', tcs_env['user_id'])
            assert result['ambiguous'] == 1
            row = TCSReconciliation.query.filter_by(
                profile_id=tcs_env['profile_id'], state_code='29'
            ).first()
            assert row.match_status == 'AMBIGUOUS'
            assert row.ambiguity_details
            assert ECOM_GSTIN_A in row.ambiguity_details
            assert '29AABCF5678D1ZP' in row.ambiguity_details

    def test_undated_rows_cannot_enter_the_tcs_base(self, tcs_env, tmp_path):
        """A row with no invoice date never silently enters a TCS base.

        The period query filters on invoice_date, so an undated row is excluded
        from aggregation entirely instead of being assigned a guessed rate. The
        defensive dict-level path (aggregate_ecom with invoice_date=None) is
        covered by the unit test test_undated_supply_is_flagged_not_rate_guessed.
        """
        app = tcs_env['app']
        with app.app_context():
            tx = Transaction.query.filter_by(
                profile_id=tcs_env['profile_id'], invoice_number='INV-1'
            ).first()
            tx.invoice_date = None
            _db.session.commit()

            rows = get_ecom_supplies(tcs_env['profile_id'], '082024')
            # undated row is not returned -> cannot be aggregated
            assert all(r['invoice_number'] != 'INV-1' for r in rows)
            agg = aggregate_ecom(rows, '082024', seller_state='27')[0]
            assert agg['undated_supplies'] == Decimal('0.00')
            assert agg['has_undated_supplies'] is False
            # remaining net = only the supplier return remains -> net 0 - 2000
            assert agg['gross_taxable_value'] == Decimal('0.00')

        self._import_gstr8(app, tcs_env['profile_id'], tcs_env['user_id'],
                           tmp_path, [('29', 'Karnataka', 0, 0.00)])
        with app.app_context():
            result = reconcile_tcs(tcs_env['profile_id'], '082024', tcs_env['user_id'])
            # the reconciliation always exposes the undated-supply channel
            assert 'undated_supplies' in result
            assert 'warnings' in result
            assert result['undated_suppliers'] == []

    def test_missing_in_source_detected(self, tcs_env, tmp_path):
        """Portal reports a state with no internal supplies."""
        app = tcs_env['app']
        self._import_gstr8(app, tcs_env['profile_id'], tcs_env['user_id'],
                           tmp_path, [('33', 'Tamil Nadu', 50000, 250.00)])
        with app.app_context():
            result = reconcile_tcs(tcs_env['profile_id'], '082024', tcs_env['user_id'])
            assert result['missing_in_source'] >= 1
            assert result['missing_in_portal'] >= 1  # state 29 not in portal


class TestAdjustmentsAndExport:
    def test_apply_adjustment_writes_audit_log(self, tcs_env, tmp_path):
        app = tcs_env['app']
        with app.app_context():
            row = TCSReconciliation(
                profile_id=tcs_env['profile_id'], user_id=tcs_env['user_id'],
                return_period='082024', state_code='29', state_name='Karnataka',
                our_net_taxable_value=Decimal('12600.00'),
                our_calculated_tcs=Decimal('63.00'),
                portal_taxable_value=Decimal('99999.00'),
                portal_tcs=Decimal('500.00'),
                match_status='MISMATCH',
            )
            _db.session.add(row)
            _db.session.commit()
            rid = row.id

            assert apply_adjustment(rid, 'ACCEPT_PORTAL', 'test acceptance',
                                    tcs_env['user_id']) is True
            refreshed = _db.session.get(TCSReconciliation, rid)
            assert refreshed.match_status == 'MATCHED'
            assert refreshed.is_adjusted is True
            assert refreshed.adjusted_by == tcs_env['user_id']

            audits = AuditLog.query.filter_by(
                entity_type='TCSReconciliation', entity_id=rid
            ).all()
            assert len(audits) == 1
            assert audits[0].action == 'TCS_ADJUSTMENT'

    def test_export_contains_reconciliation_rows(self, tcs_env, tmp_path):
        app = tcs_env['app']
        self_import = None
        with app.app_context():
            row = TCSReconciliation(
                profile_id=tcs_env['profile_id'], user_id=tcs_env['user_id'],
                return_period='082024', state_code='29', state_name='Karnataka',
                our_net_taxable_value=Decimal('12600.00'),
                our_calculated_tcs=Decimal('63.00'),
                portal_taxable_value=Decimal('12600.00'),
                portal_tcs=Decimal('63.00'),
                match_status='MATCHED',
            )
            _db.session.add(row)
            _db.session.commit()

            csv_data = export_reconciliation(tcs_env['profile_id'], '082024')
            assert 'State Code' in csv_data
            assert 'Karnataka' in csv_data
            assert 'MATCHED' in csv_data
            assert '12600.00' in csv_data


class TestTCSWebRoutes:
    """HTTP-level verification of the TCS UI wiring (C6)."""

    def _login(self, client, email, password='testpass123'):
        return client.post('/login', data={'email': email, 'password': password},
                           follow_redirects=True)

    def test_tcs_page_renders_with_active_profile(self, tcs_env, client):
        app = tcs_env['app']
        with app.app_context():
            user = _db.session.get(User, tcs_env['user_id'])
            email = user.email
        r = self._login(client, email)
        assert r.status_code == 200

        # select the profile as active
        client.post(f"/profiles/{tcs_env['profile_id']}/select",
                    follow_redirects=True)

        page = client.get('/tcs')
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert 'TCS Reconciliation' in body
        # template must not contain an unresolvable url_for
        assert 'import_routes.index' not in body

    def test_tcs_upload_reconcile_export_flow(self, tcs_env, client, tmp_path):
        app = tcs_env['app']
        with app.app_context():
            senders = _db.session.get(User, tcs_env['user_id'])
            email = senders.email
        self._login(client, email)
        client.post(f"/profiles/{tcs_env['profile_id']}/select",
                    follow_redirects=True)

        # Build a state-wise portal report for the uploaded period
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['State Code', 'State Name', 'Taxable Value', 'TCS Amount',
                   'CGST TCS', 'SGST TCS', 'IGST TCS'])
        ws.append(['29', 'Karnataka', 12600, 63.00, 0, 0, 63.00])
        path = os.path.join(str(tmp_path), 'portal_upload.xlsx')
        wb.save(path)

        with open(path, 'rb') as fh:
            up = client.post('/tcs/upload', data={
                'return_period': '082024',
                'file': (io.BytesIO(fh.read()), 'portal_upload.xlsx'),
            }, content_type='multipart/form-data', follow_redirects=True)
        assert up.status_code == 200, up.get_data(as_text=True)[:400]

        rec = client.post('/tcs/reconcile', data={'return_period': '082024'},
                          follow_redirects=True)
        assert rec.status_code == 200

        exp = client.get('/tcs/export?return_period=082024')
        assert exp.status_code == 200
        csv_body = exp.get_data(as_text=True)
        assert 'State Code' in csv_body
        assert 'Karnataka' in csv_body
