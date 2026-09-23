from typing import List, Dict, Any

class CheckResult:
    def __init__(self, passed: bool, messages: List[str]):
        self.passed = passed
        self.messages = messages

class ReconciliationReport:
    def __init__(self):
        self.checks = []
        self.critical_failures = 0
        self.warnings = 0
        self.is_generation_blocked = False
        self.blocking_reasons = []

def run_full_reconciliation(profile_id: int, return_period: str) -> ReconciliationReport:
    report = ReconciliationReport()
    # Execute checks
    return report

def check_import_counts(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])

def check_b2b_totals(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])

def check_b2c_totals(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])

def check_cdnr_totals(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])

def check_nil_totals(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])

def check_hsn_reconciliation(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])

def check_tax_totals(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])

def check_state_totals(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])

def check_duplicates(profile_id: int, return_period: str = None) -> CheckResult:
    """Report active transactions that share the same business fingerprint.

    Two active transactions with the same fingerprint are the same source line
    imported twice; the import pipeline marks such rows as duplicates, so a hit
    here means data entered the table another way (manual edit/legacy rows).
    """
    from app.models import Transaction

    query = Transaction.query.filter(
        Transaction.profile_id == profile_id,
        Transaction.is_deleted.is_(False),
        Transaction.row_fingerprint.isnot(None),
    )
    rows = query.all()

    groups: Dict[str, List[Transaction]] = {}
    for transaction in rows:
        groups.setdefault(transaction.row_fingerprint, []).append(transaction)

    messages = []
    for fingerprint, transactions in groups.items():
        if len(transactions) < 2:
            continue
        ids = ', #'.join(str(transaction.id) for transaction in transactions)
        invoices = ', '.join(sorted({str(transaction.invoice_number or '') for transaction in transactions}))
        messages.append(
            f'Duplicate transactions for the same source line: #{ids} '
            f'(invoice/document {invoices or "n/a"})'
        )

    # Transactions imported before fingerprinting existed carry no fingerprint;
    # report the count so the gap is visible rather than silently ignored.
    unfingerprinted = Transaction.query.filter(
        Transaction.profile_id == profile_id,
        Transaction.is_deleted.is_(False),
        Transaction.row_fingerprint.is_(None),
    ).count()
    if unfingerprinted:
        messages.append(
            f'{unfingerprinted} transaction(s) predate duplicate fingerprinting and could '
            f'not be cross-checked'
        )

    return CheckResult(not any('Duplicate transactions' in message for message in messages), messages)

def check_unclassified(profile_id: int, return_period: str) -> CheckResult:
    return CheckResult(True, [])
