from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
from datetime import datetime
import openpyxl
import os
import hashlib
from app.extensions import db
from app.models import TCSReconciliation, Transaction, ImportHistory, RawImport
from app.services.ecom_service import get_ecom_supplies, aggregate_ecom
from app.services.gst_rules import calculate_tcs, get_tcs_rates_for_period
from app.services.import_service import ImportResult
from app.services.hsn_service import ReconciliationResult


class TCSReportParser:
    """Parse various TCS portal report formats."""

    # GSTR-8 format (state-wise, no ecom_gstin)
    GSTR8_HEADERS = {
        'state_code': ['State Code', 'StateCode'],
        'state_name': ['State Name', 'StateName'],
        'taxable_value': ['Taxable Value', 'TaxableValue', 'Aggregate Taxable Value'],
        'tcs_amount': ['TCS Amount', 'TCSAmount', 'Total TCS'],
        'cgst_tcs': ['CGST TCS', 'CGST_TCS', 'CGST TCS Amount'],
        'sgst_tcs': ['SGST TCS', 'SGST_TCS', 'SGST TCS Amount'],
        'igst_tcs': ['IGST TCS', 'IGST_TCS', 'IGST TCS Amount'],
    }

    # Marketplace TCS certificate format (has ecom_gstin)
    MARKETPLACE_HEADERS = {
        'ecommerce_gstin': ['Ecommerce GSTIN', 'E-Commerce GSTIN', 'ECO GSTIN', 'Operator GSTIN'],
        'state_code': ['State Code', 'StateCode', 'State'],
        'state_name': ['State Name', 'StateName'],
        'taxable_value': ['Taxable Value', 'TaxableValue', 'Aggregate Taxable Value'],
        'tcs_amount': ['TCS Amount', 'TCSAmount', 'Total TCS'],
        'cgst_tcs': ['CGST TCS', 'CGST_TCS', 'CGST TCS Amount'],
        'sgst_tcs': ['SGST TCS', 'SGST_TCS', 'SGST TCS Amount'],
        'igst_tcs': ['IGST TCS', 'IGST_TCS', 'IGST TCS Amount'],
    }

    @classmethod
    def detect_format(cls, headers: List[str]) -> str:
        """Detect report format from headers."""
        headers_lower = [h.lower().strip() for h in headers]

        # Check for marketplace format (has ecommerce_gstin column)
        for h in headers_lower:
            if 'ecommerce' in h or 'e-com' in h or 'eco gstin' in h or 'operator gstin' in h:
                return 'marketplace'

        # Default to GSTR-8
        return 'gstr8'

    @classmethod
    def map_headers(cls, headers: List[str], format_type: str) -> Dict[str, int]:
        """Map standard field names to column indices."""
        header_map = cls.MARKETPLACE_HEADERS if format_type == 'marketplace' else cls.GSTR8_HEADERS
        result = {}

        for field, possible_names in header_map.items():
            lowered = [n.lower().strip() for n in possible_names]
            for idx, header in enumerate(headers):
                header_clean = (header or '').strip().lower()
                if header_clean and header_clean in lowered:
                    result[field] = idx
                    break
        return result

    @classmethod
    def parse_row(cls, row: List[Any], col_map: Dict[str, int], format_type: str) -> Optional[Dict[str, Any]]:
        """Parse a single row into standardized dict."""
        def get_val(field: str, default=None):
            idx = col_map.get(field)
            if idx is not None and idx < len(row) and row[idx] is not None:
                val = row[idx]
                if isinstance(val, str):
                    val = val.strip()
                return val
            return default

        state_code = get_val('state_code')
        if not state_code:
            return None

        state_code = str(state_code).strip().zfill(2)

        # Parse numeric values
        def parse_decimal(val):
            if val is None or val == '':
                return Decimal('0.00')
            try:
                return Decimal(str(val).replace(',', '').strip())
            except:
                return Decimal('0.00')

        result = {
            'state_code': state_code,
            'state_name': get_val('state_name', ''),
            'taxable_value': parse_decimal(get_val('taxable_value')),
            'tcs_amount': parse_decimal(get_val('tcs_amount')),
            'cgst_tcs': parse_decimal(get_val('cgst_tcs')),
            'sgst_tcs': parse_decimal(get_val('sgst_tcs')),
            'igst_tcs': parse_decimal(get_val('igst_tcs')),
        }

        if format_type == 'marketplace':
            ecom_gstin = get_val('ecommerce_gstin')
            if ecom_gstin:
                result['ecommerce_gstin'] = str(ecom_gstin).strip()

        return result

    @classmethod
    def parse_file(cls, file_path: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Parse TCS report file. Returns (format_type, parsed_rows)."""
        wb = openpyxl.load_workbook(file_path, data_only=True)
        ws = wb.active

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return 'unknown', []

        headers = [str(h) if h else '' for h in rows[0]]
        format_type = cls.detect_format(headers)
        col_map = cls.map_headers(headers, format_type)

        parsed = []
        for row in rows[1:]:
            parsed_row = cls.parse_row(list(row), col_map, format_type)
            if parsed_row:
                parsed.append(parsed_row)

        return format_type, parsed


def import_tcs_report(file_path: str, profile_id: int, return_period: str, user_id: int) -> ImportResult:
    """Import TCS report from GST Portal or marketplace.

    Parses GSTR-8 (state-wise) or marketplace TCS certificate (ecom_gstin-wise),
    validates data, and stores in TCSReconciliation table.
    """
    result = ImportResult(platform='TCS_Portal', file_name=os.path.basename(file_path))

    try:
        # Calculate file hash for duplicate detection
        with open(file_path, 'rb') as f:
            file_hash = hashlib.sha256(f.read()).hexdigest()

        # Check for duplicate
        existing = TCSReconciliation.query.filter(
            TCSReconciliation.profile_id == profile_id,
            TCSReconciliation.return_period == return_period,
            # Could add file_hash column for better duplicate detection
        ).first()

        if existing:
            result.warnings.append("TCS reconciliation already exists for this period. Overwriting.")
            # Clear existing data for this period/profile
            TCSReconciliation.query.filter(
                TCSReconciliation.profile_id == profile_id,
                TCSReconciliation.return_period == return_period
            ).delete()

        # Parse the report
        format_type, portal_data = TCSReportParser.parse_file(file_path)

        if not portal_data:
            result.errors.append("No valid data rows found in TCS report")
            return result

        result.metadata['format'] = format_type
        result.metadata['rows_parsed'] = len(portal_data)

        # Store portal data in TCSReconciliation
        for row in portal_data:
            state_code = row['state_code']

            # Create or update reconciliation row
            recon = TCSReconciliation(
                profile_id=profile_id,
                user_id=user_id,
                return_period=return_period,
                ecommerce_gstin=row.get('ecommerce_gstin'),
                state_code=state_code,
                state_name=row.get('state_name', ''),
                portal_taxable_value=row['taxable_value'],
                portal_tcs=row['tcs_amount'],
                portal_cgst_tcs=row['cgst_tcs'],
                portal_sgst_tcs=row['sgst_tcs'],
                portal_igst_tcs=row['igst_tcs'],
                match_status='PENDING',  # Will be updated after reconciliation
            )
            db.session.add(recon)

        db.session.commit()
        result.success_rows = len(portal_data)
        result.total_rows = len(portal_data)

    except Exception as e:
        db.session.rollback()
        result.errors.append(f"Import error: {str(e)}")

    return result


def reconcile_tcs(profile_id: int, return_period: str, user_id: int) -> Dict[str, Any]:
    """Reconcile internal e-commerce TCS with portal TCS report.

    Matches by state_code. If internal data has multiple ecommerce_gstin
    for the same state but portal only has state-level data, marks as AMBIGUOUS.

    Returns reconciliation summary.
    """
    # Get internal e-com aggregation
    internal_transactions = get_ecom_supplies(profile_id, return_period)
    if not internal_transactions:
        return {
            'status': 'NO_INTERNAL_DATA',
            'message': 'No e-commerce transactions found for this period',
            'matched': 0,
            'mismatched': 0,
            'missing_in_source': 0,
            'missing_in_portal': 0,
            'ambiguous': 0,
            'details': []
        }

    internal_agg = aggregate_ecom(internal_transactions, return_period,
                                  seller_state=_seller_state_for(profile_id))

    # Rows without a usable invoice date cannot be assigned a defensible TCS
    # rate. They are excluded from the rate-bucketed base and surfaced here
    # rather than being silently given a guessed rate.
    undated_supplies = sum((a.get('undated_supplies', Decimal('0'))
                            for a in internal_agg), Decimal('0.00'))
    undated_suppliers = [a['ecommerce_gstin'] for a in internal_agg
                         if a.get('has_undated_supplies')]

    # Get portal data
    portal_rows = TCSReconciliation.query.filter(
        TCSReconciliation.profile_id == profile_id,
        TCSReconciliation.return_period == return_period
    ).all()

    if not portal_rows:
        return {
            'status': 'NO_PORTAL_DATA',
            'message': 'No portal TCS report imported for this period',
            'matched': 0,
            'mismatched': 0,
            'missing_in_source': 0,
            'missing_in_portal': 0,
            'ambiguous': 0,
            'details': []
        }

    # Build portal lookup by (state_code, ecommerce_gstin) if available
    portal_by_state = {}
    portal_by_state_ecom = {}
    for p in portal_rows:
        key = (p.state_code, p.ecommerce_gstin or '')
        portal_by_state.setdefault(p.state_code, []).append(p)
        portal_by_state_ecom[key] = p

    # Build internal lookup
    internal_by_state = {}
    internal_by_state_ecom = {}
    for agg in internal_agg:
        key = (agg['state_code'], agg['ecommerce_gstin'] or '')
        internal_by_state.setdefault(agg['state_code'], []).append(agg)
        internal_by_state_ecom[key] = agg

    # Reconcile
    results = []
    matched = mismatched = missing_source = missing_portal = ambiguous = 0

    all_states = set(list(internal_by_state.keys()) + list(portal_by_state.keys()))

    for state_code in sorted(all_states):
        internal_list = internal_by_state.get(state_code, [])
        portal_list = portal_by_state.get(state_code, [])

        # Check if we have ecom_gstin in portal data
        portal_has_ecom = any(p.ecommerce_gstin for p in portal_list)

        if portal_has_ecom:
            # Portal has ecom_gstin - match exactly
            for agg in internal_list:
                key = (state_code, agg['ecommerce_gstin'] or '')
                portal = portal_by_state_ecom.get(key)

                if portal:
                    # Exact match found
                    diff = compare_and_update(portal, agg)
                    portal.match_status = diff['status']
                    results.append(diff)
                    if diff['status'] == 'MATCHED':
                        matched += 1
                    elif diff['status'] == 'MISMATCH':
                        mismatched += 1
                else:
                    # Internal has ecom_gstin not in portal
                    portal = TCSReconciliation(
                        profile_id=profile_id,
                        user_id=user_id,
                        return_period=return_period,
                        ecommerce_gstin=agg['ecommerce_gstin'],
                        state_code=state_code,
                        state_name=agg.get('state', ''),
                        our_net_taxable_value=agg['net_taxable_value'],
                        our_calculated_tcs=agg['our_tcs_total'],
                        our_calculated_cgst=agg['our_tcs_cgst'],
                        our_calculated_sgst=agg['our_tcs_sgst'],
                        our_calculated_igst=agg['our_tcs_igst'],
                        match_status='MISSING_IN_PORTAL'
                    )
                    db.session.add(portal)
                    results.append({
                        'state_code': state_code,
                        'ecommerce_gstin': agg['ecommerce_gstin'],
                        'status': 'MISSING_IN_PORTAL',
                        'our_net_taxable': float(agg['net_taxable_value']),
                        'our_tcs': float(agg['our_tcs_total']),
                        'portal_taxable': 0,
                        'portal_tcs': 0,
                    })
                    missing_portal += 1

            # Check for portal entries not in internal
            for portal in portal_list:
                key = (state_code, portal.ecommerce_gstin or '')
                if key not in internal_by_state_ecom:
                    portal.match_status = 'MISSING_IN_SOURCE'
                    results.append({
                        'state_code': state_code,
                        'ecommerce_gstin': portal.ecommerce_gstin,
                        'status': 'MISSING_IN_SOURCE',
                        'our_net_taxable': 0,
                        'our_tcs': 0,
                        'portal_taxable': float(portal.portal_taxable_value),
                        'portal_tcs': float(portal.portal_tcs),
                    })
                    missing_source += 1
        else:
            # Portal only has state-level data (GSTR-8 format)
            # Sum all internal ecom_gstin for this state
            internal_total_net = sum(a['net_taxable_value'] for a in internal_list)
            internal_total_tcs = sum(a['our_tcs_total'] for a in internal_list)
            internal_total_cgst = sum(a['our_tcs_cgst'] for a in internal_list)
            internal_total_sgst = sum(a['our_tcs_sgst'] for a in internal_list)
            internal_total_igst = sum(a['our_tcs_igst'] for a in internal_list)

            # If multiple ecom_gstin, check for ambiguity
            ecom_gstins = [a['ecommerce_gstin'] for a in internal_list if a['ecommerce_gstin']]
            is_ambiguous = len(set(ecom_gstins)) > 1

            if portal_list and internal_list:
                portal = portal_list[0]  # Single state-level entry
                diff = compare_and_update(portal, {
                    'net_taxable_value': internal_total_net,
                    'our_tcs_total': internal_total_tcs,
                    'our_tcs_cgst': internal_total_cgst,
                    'our_tcs_sgst': internal_total_sgst,
                    'our_tcs_igst': internal_total_igst,
                    'state_code': state_code,
                })

                if is_ambiguous:
                    portal.match_status = 'AMBIGUOUS'
                    portal.ambiguity_details = ', '.join(sorted(set(ecom_gstins)))
                    diff['status'] = 'AMBIGUOUS'
                    ambiguous += 1
                else:
                    portal.match_status = diff['status']
                    if diff['status'] == 'MATCHED':
                        matched += 1
                    elif diff['status'] == 'MISMATCH':
                        mismatched += 1

                results.append(diff)
            elif portal_list and not internal_list:
                # Portal reports this state but we have no internal supplies
                for portal in portal_list:
                    portal.match_status = 'MISSING_IN_SOURCE'
                    results.append({
                        'state_code': state_code,
                        'ecommerce_gstin': portal.ecommerce_gstin,
                        'status': 'MISSING_IN_SOURCE',
                        'our_net_taxable': 0,
                        'our_tcs': 0,
                        'portal_taxable': float(portal.portal_taxable_value or 0),
                        'portal_tcs': float(portal.portal_tcs or 0),
                    })
                    missing_source += 1
            else:
                # No portal data for this state
                missing_portal += len(internal_list)
                for agg in internal_list:
                    results.append({
                        'state_code': state_code,
                        'ecommerce_gstin': agg['ecommerce_gstin'],
                        'status': 'MISSING_IN_PORTAL',
                        'our_net_taxable': float(agg['net_taxable_value']),
                        'our_tcs': float(agg['our_tcs_total']),
                        'portal_taxable': 0,
                        'portal_tcs': 0,
                    })

    db.session.commit()

    return {
        'status': 'COMPLETED',
        'matched': matched,
        'mismatched': mismatched,
        'missing_in_source': missing_source,
        'missing_in_portal': missing_portal,
        'ambiguous': ambiguous,
        'undated_supplies': float(undated_supplies),
        'undated_suppliers': undated_suppliers,
        'warnings': (
            [f"{len(undated_suppliers)} e-commerce supplier group(s) have rows with no "
             f"invoice date ({undated_supplies} taxable value). These are excluded from "
             f"the TCS base and must be dated before the reconciliation is relied upon."]
            if undated_suppliers else []
        ),
        'details': results
    }


def _seller_state_for(profile_id: int) -> str:
    """Return the state code of the profile's own GSTIN, for INTRA/INTER TCS.

    Falls back to '27' only if the profile has no usable state code.
    """
    try:
        from app.models import GSTProfile
        profile = db.session.get(GSTProfile, profile_id)
        if profile and profile.state_code:
            return str(profile.state_code).strip()[:2]
        if profile and profile.gstin:
            return str(profile.gstin).strip()[:2]
    except Exception:
        pass
    return '27'


def compare_and_update(portal: TCSReconciliation, internal: Dict[str, Any]) -> Dict[str, Any]:
    """Compare portal and internal values, update portal row, return diff."""
    tol = Decimal('1.00')  # 1 rupee tolerance

    # Net taxable comparison
    our_net = internal.get('net_taxable_value', Decimal('0'))
    portal_taxable = portal.portal_taxable_value or Decimal('0')
    diff_taxable = (our_net - portal_taxable).copy_abs()

    # TCS comparison
    our_tcs = internal.get('our_tcs_total', Decimal('0'))
    portal_tcs = portal.portal_tcs or Decimal('0')
    diff_tcs = (our_tcs - portal_tcs).copy_abs()

    # Component-wise
    our_cgst = internal.get('our_tcs_cgst', Decimal('0'))
    our_sgst = internal.get('our_tcs_sgst', Decimal('0'))
    our_igst = internal.get('our_tcs_igst', Decimal('0'))

    diff_cgst = (our_cgst - (portal.portal_cgst_tcs or Decimal('0'))).copy_abs()
    diff_sgst = (our_sgst - (portal.portal_sgst_tcs or Decimal('0'))).copy_abs()
    diff_igst = (our_igst - (portal.portal_igst_tcs or Decimal('0'))).copy_abs()

    # Determine status
    if diff_taxable <= tol and diff_tcs <= tol:
        status = 'MATCHED'
    else:
        status = 'MISMATCH'

    # Update portal row with internal values
    portal.our_net_taxable_value = our_net
    portal.our_calculated_tcs = our_tcs
    portal.our_calculated_cgst = our_cgst
    portal.our_calculated_sgst = our_sgst
    portal.our_calculated_igst = our_igst
    portal.difference_taxable = diff_taxable
    portal.difference_tcs = diff_tcs
    portal.difference_cgst = diff_cgst
    portal.difference_sgst = diff_sgst
    portal.difference_igst = diff_igst

    return {
        'state_code': portal.state_code,
        'ecommerce_gstin': portal.ecommerce_gstin,
        'status': status,
        'our_net_taxable': float(our_net),
        'portal_taxable': float(portal_taxable),
        'diff_taxable': float(diff_taxable),
        'our_tcs': float(our_tcs),
        'portal_tcs': float(portal_tcs),
        'diff_tcs': float(diff_tcs),
        'our_cgst': float(our_cgst),
        'our_sgst': float(our_sgst),
        'our_igst': float(our_igst),
    }


def apply_adjustment(reconciliation_id: int, adjustment_type: str, notes: str, user_id: int) -> bool:
    """Apply manual adjustment to a reconciliation row.

    adjustment_type: 'ACCEPT_PORTAL', 'ACCEPT_INTERNAL', 'MANUAL_OVERRIDE'
    """
    from app.models import AuditLog

    recon = db.session.get(TCSReconciliation, reconciliation_id)
    if not recon:
        return False

    # Log the adjustment
    old_status = recon.match_status
    old_our_tcs = float(recon.our_calculated_tcs)
    old_portal_tcs = float(recon.portal_tcs)

    if adjustment_type == 'ACCEPT_PORTAL':
        recon.our_calculated_tcs = recon.portal_tcs
        recon.our_calculated_cgst = recon.portal_cgst_tcs
        recon.our_calculated_sgst = recon.portal_sgst_tcs
        recon.our_calculated_igst = recon.portal_igst_tcs
        recon.match_status = 'MATCHED'
        recon.adjustment_notes = f"Accepted portal values. {notes}"
    elif adjustment_type == 'ACCEPT_INTERNAL':
        recon.portal_tcs = recon.our_calculated_tcs
        recon.portal_cgst_tcs = recon.our_calculated_cgst
        recon.portal_sgst_tcs = recon.our_calculated_sgst
        recon.portal_igst_tcs = recon.our_calculated_igst
        recon.match_status = 'MATCHED'
        recon.adjustment_notes = f"Accepted internal values. {notes}"
    elif adjustment_type == 'MANUAL_OVERRIDE':
        recon.match_status = 'MANUALLY_ADJUSTED'
        recon.adjustment_notes = notes

    recon.is_adjusted = True
    recon.adjusted_by = user_id
    recon.adjusted_at = datetime.utcnow()

    # Audit log
    audit = AuditLog(
        user_id=user_id,
        action='TCS_ADJUSTMENT'[:20],
        entity_type='TCSReconciliation',
        entity_id=recon.id,
        field_name='match_status',
        old_value=f"status={old_status}; our_tcs={old_our_tcs}; portal_tcs={old_portal_tcs}",
        new_value=f"status={recon.match_status}; our_tcs={float(recon.our_calculated_tcs)}; portal_tcs={float(recon.portal_tcs)}",
        reason=(notes or '')[:255]
    )
    db.session.add(audit)
    db.session.commit()

    return True


def export_reconciliation(profile_id: int, return_period: str) -> str:
    """Export reconciliation results to CSV."""
    import csv
    import io

    rows = TCSReconciliation.query.filter(
        TCSReconciliation.profile_id == profile_id,
        TCSReconciliation.return_period == return_period
    ).order_by(TCSReconciliation.state_code).all()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        'State Code', 'State Name', 'E-Commerce GSTIN',
        'Our Net Taxable', 'Portal Taxable', 'Diff Taxable',
        'Our TCS Total', 'Portal TCS Total', 'Diff TCS',
        'Our CGST TCS', 'Portal CGST TCS', 'Diff CGST',
        'Our SGST TCS', 'Portal SGST TCS', 'Diff SGST',
        'Our IGST TCS', 'Portal IGST TCS', 'Diff IGST',
        'Match Status', 'Ambiguity Details', 'Is Adjusted', 'Adjustment Notes'
    ])

    def money(v):
        """Format a Numeric value as a fixed 2-decimal string."""
        return f"{Decimal(str(v or 0)):.2f}"

    for r in rows:
        writer.writerow([
            r.state_code, r.state_name, r.ecommerce_gstin or '',
            money(r.our_net_taxable_value), money(r.portal_taxable_value), money(r.difference_taxable),
            money(r.our_calculated_tcs), money(r.portal_tcs), money(r.difference_tcs),
            money(r.our_calculated_cgst), money(r.portal_cgst_tcs), money(r.difference_cgst),
            money(r.our_calculated_sgst), money(r.portal_sgst_tcs), money(r.difference_sgst),
            money(r.our_calculated_igst), money(r.portal_igst_tcs), money(r.difference_igst),
            r.match_status, r.ambiguity_details or '', 'Yes' if r.is_adjusted else 'No',
            r.adjustment_notes or ''
        ])

    return output.getvalue()