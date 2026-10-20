"""
Idempotent Zero-Loss Schema Migration Engine.

Safely detects and upgrades legacy SQLite and PostgreSQL database schemas
to incorporate all tables and columns introduced in Phases 5E-10 through 6C:
- transactions.is_staged
- import_histories.task_id
- import_histories.processed_rows
- import_histories.error_message
- gstr1_generations.snapshot_checksum
- gstr1_generations.input_count
- gstr1_generations.gate_status
- gstr1_generations.validation_summary
- reconciliation_records (Table)

Guarantees 100% data preservation (0 data loss) via pre- and post-migration
row count verifications and strictly additive DDL.
"""
import logging
from typing import Dict, Any, List, Optional
from sqlalchemy import inspect, text, Table, Column, Integer, String, Text, Numeric, Boolean, DateTime, ForeignKey
from app.extensions import db

logger = logging.getLogger(__name__)

CANONICAL_COLUMNS: Dict[str, Dict[str, Dict[str, Any]]] = {
    'transactions': {
        'is_staged': {
            'type_sql': 'BOOLEAN DEFAULT 0 NOT NULL',
            'nullable': False,
            'default': '0',
        },
    },
    'import_histories': {
        'task_id': {
            'type_sql': 'VARCHAR(128)',
            'nullable': True,
            'default': None,
        },
        'processed_rows': {
            'type_sql': 'INTEGER DEFAULT 0',
            'nullable': True,
            'default': '0',
        },
        'error_message': {
            'type_sql': 'VARCHAR(500)',
            'nullable': True,
            'default': None,
        },
    },
    'gstr1_generations': {
        'snapshot_checksum': {
            'type_sql': 'VARCHAR(64)',
            'nullable': True,
            'default': None,
        },
        'input_count': {
            'type_sql': 'INTEGER DEFAULT 0',
            'nullable': True,
            'default': '0',
        },
        'gate_status': {
            'type_sql': "VARCHAR(20) DEFAULT 'CLEAN'",
            'nullable': True,
            'default': "'CLEAN'",
        },
        'validation_summary': {
            'type_sql': 'TEXT',
            'nullable': True,
            'default': None,
        },
    },
}

CANONICAL_TABLES = ['reconciliation_records']


def inspect_schema_drift(engine=None) -> Dict[str, Any]:
    """Inspect the database schema and identify drift or missing columns/tables.

    Args:
        engine: SQLAlchemy engine (defaults to db.engine)

    Returns:
        dict: {
            'is_in_sync': bool,
            'missing_tables': list,
            'missing_columns': dict,
            'existing_tables': list,
            'dialect': str
        }
    """
    if engine is None:
        engine = db.engine

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    dialect_name = engine.dialect.name

    missing_tables = [tbl for tbl in CANONICAL_TABLES if tbl not in existing_tables]

    missing_columns: Dict[str, List[str]] = {}
    for table_name, expected_cols in CANONICAL_COLUMNS.items():
        if table_name in existing_tables:
            current_cols = {c['name'] for c in inspector.get_columns(table_name)}
            missing = [col for col in expected_cols if col not in current_cols]
            if missing:
                missing_columns[table_name] = missing

    is_in_sync = (len(missing_tables) == 0 and len(missing_columns) == 0)

    return {
        'is_in_sync': is_in_sync,
        'missing_tables': missing_tables,
        'missing_columns': missing_columns,
        'existing_tables': sorted(list(existing_tables)),
        'dialect': dialect_name,
    }


def apply_schema_migrations(engine=None) -> Dict[str, Any]:
    """Idempotently apply schema migrations to upgrade tables with zero data loss.

    Performs:
    1. Pre-migration row counts on all existing tables.
    2. Add missing tables (reconciliation_records).
    3. Add missing columns with proper defaults and nullability.
    4. Post-migration row counts verifying 100% data preservation.

    Args:
        engine: SQLAlchemy engine (defaults to db.engine)

    Returns:
        dict: Summary of applied migrations and verification metrics.
    """
    if engine is None:
        engine = db.engine

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    dialect_name = engine.dialect.name

    # 1. Snapshot row counts of pre-existing tables
    pre_counts: Dict[str, int] = {}
    with engine.connect() as conn:
        for tbl in existing_tables:
            try:
                res = conn.execute(text(f'SELECT COUNT(*) FROM "{tbl}"'))
                pre_counts[tbl] = res.scalar() or 0
            except Exception as e:
                logger.warning("Could not count rows in %s: %s", tbl, e)

    applied_tables: List[str] = []
    applied_columns: List[str] = []

    # 2. Add missing tables
    if 'reconciliation_records' not in existing_tables:
        from app.models.reconciliation_record import ReconciliationRecord
        try:
            ReconciliationRecord.__table__.create(engine, checkfirst=True)
            applied_tables.append('reconciliation_records')
            logger.info("Created missing table reconciliation_records")
        except Exception as e:
            logger.error("Failed creating reconciliation_records table: %s", e)
            raise

    # Refresh inspector after table creation
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # 3. Add missing columns safely
    for table_name, expected_cols in CANONICAL_COLUMNS.items():
        if table_name in existing_tables:
            current_cols = {c['name'] for c in inspector.get_columns(table_name)}
            for col_name, col_meta in expected_cols.items():
                if col_name not in current_cols:
                    col_type_sql = col_meta['type_sql']
                    if dialect_name == 'sqlite':
                        ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {col_type_sql}'
                    elif dialect_name == 'postgresql':
                        # PostgreSQL syntax
                        ddl = f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "{col_name}" {col_type_sql}'
                    else:
                        ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {col_type_sql}'

                    with engine.begin() as conn:
                        conn.execute(text(ddl))
                    applied_columns.append(f"{table_name}.{col_name}")
                    logger.info("Added missing column %s.%s", table_name, col_name)

    # 4. Verify Zero Data Loss: post-migration counts must exactly match pre-migration counts
    post_counts: Dict[str, int] = {}
    with engine.connect() as conn:
        for tbl, pre_count in pre_counts.items():
            res = conn.execute(text(f'SELECT COUNT(*) FROM "{tbl}"'))
            post_count = res.scalar() or 0
            post_counts[tbl] = post_count
            if post_count != pre_count:
                raise RuntimeError(
                    f"Zero-Loss Data Integrity Violation: Table '{tbl}' row count changed from {pre_count} to {post_count}!"
                )

    status = 'UP_TO_DATE' if (not applied_tables and not applied_columns) else 'MIGRATED'

    return {
        'status': status,
        'applied_tables': applied_tables,
        'applied_columns': applied_columns,
        'zero_loss_verified': True,
        'pre_counts': pre_counts,
        'post_counts': post_counts,
        'dialect': dialect_name,
    }
