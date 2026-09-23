"""
GST Online Seller - Flask Application Factory
"""
import os
import logging
from pathlib import Path
from flask import Flask
from .extensions import db, login_manager, csrf


def _ensure_schema_compat(app):
    """Add model columns missing from pre-existing SQLite tables.

    ``db.create_all()`` creates missing *tables* but never adds columns to
    tables that already exist. On a developer SQLite database created before
    a model gained a column, inserts then fail with "no such column".
    This shim adds any missing nullable/defaulted columns in place.

    Only ADD COLUMN is performed — no data is dropped or rewritten. Databases
    other than SQLite are skipped (use a real migration tool there).
    """
    try:
        from sqlalchemy import inspect, text
        engine = db.engine
        if engine.dialect.name != 'sqlite':
            return
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        for table in db.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            current = {c['name'] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in current:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    # Cannot safely add a NOT NULL column without a value.
                    continue
                col_type = column.type.compile(engine.dialect)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                if not column.nullable:
                    ddl += ' NOT NULL'
                if column.server_default is not None:
                    ddl += f' DEFAULT {column.server_default.arg}'
                elif column.default is not None and getattr(column.default, 'arg', None) is not None \
                        and not callable(column.default.arg):
                    ddl += f' DEFAULT {column.default.arg}'
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                app.logger.warning(
                    'schema-compat: added missing column %s.%s', table.name, column.name
                )
    except Exception as exc:  # never block app startup on the compat shim
        app.logger.warning('schema-compat shim skipped: %s', exc)


def create_app(config_name=None):
    """
    Flask application factory.

    Args:
        config_name: Configuration name ('development', 'production', 'testing')
                     or a config object/string path.
    """
    app = Flask(__name__)

    # Load configuration
    if config_name is None:
        config_name = os.environ.get('FLASK_CONFIG', 'development')

    if isinstance(config_name, str) and config_name in ('development', 'production', 'testing'):
        from .config import config
        app.config.from_object(config[config_name])
    elif isinstance(config_name, str):
        try:
            app.config.from_object(config_name)
        except Exception:
            from .config import config
            app.config.from_object(config['default'])
    else:
        # config_name is a config object
        app.config.from_object(config_name)

    # Ensure required directories exist
    for folder_key in ('UPLOAD_FOLDER', 'GENERATED_FOLDER'):
        folder = app.config.get(folder_key)
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)

    # Ensure instance directory exists
    instance_path = Path(app.config.get('SQLALCHEMY_DATABASE_URI', '').replace('sqlite:///', ''))
    if instance_path.suffix == '.db':
        instance_path.parent.mkdir(parents=True, exist_ok=True)

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Configure login manager
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'warning'

    # User loader for Flask-Login
    @login_manager.user_loader
    def load_user(user_id):
        from .models.user import User
        return db.session.get(User, int(user_id))

    # Context processor to inject active profile and GSTIN
    @app.context_processor
    def inject_context():
        from flask import session, request
        from flask_login import current_user
        from .models.gst_profile import GSTProfile
        active_gstin = 'Not Selected'
        active_profile = None
        if current_user and current_user.is_authenticated:
            prof_id = request.args.get('profile_id') or session.get('active_profile_id')
            if prof_id:
                try:
                    active_profile = GSTProfile.query.filter_by(id=int(prof_id), user_id=current_user.id).first()
                except (ValueError, TypeError):
                    active_profile = None
                if not active_profile:
                    session.pop('active_profile_id', None)
                else:
                    session['active_profile_id'] = active_profile.id
                    active_gstin = active_profile.gstin
            if not active_profile:
                first_prof = GSTProfile.query.filter_by(user_id=current_user.id).first()
                if first_prof:
                    active_profile = first_prof
                    session['active_profile_id'] = first_prof.id
                    active_gstin = first_prof.gstin
        return dict(active_profile=active_profile, active_gstin=active_gstin)

    # Register blueprints
    from .routes import register_blueprints
    register_blueprints(app)

    # Create database tables
    with app.app_context():
        # Import all models so they are registered with SQLAlchemy
        from .models import (
            User, GSTProfile, ImportHistory, RawImport,
            Transaction, AuditLog, GSTR1Generation, TCSReconciliation
        )
        db.create_all()
        _ensure_schema_compat(app)

    # Configure logging
    if not app.debug and not app.testing:
        log_dir = Path(app.config.get('SQLALCHEMY_DATABASE_URI', '').replace('sqlite:///', '')).parent.parent / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / 'gst_online_seller.log')
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        ))
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        app.logger.setLevel(logging.INFO)
        app.logger.info('GST Online Seller startup')

    # Register error handlers
    @app.errorhandler(404)
    def not_found(error):
        from flask import render_template, request, jsonify
        if request.path.startswith('/api/'):
            return jsonify({'error': True, 'message': 'Resource not found'}), 404
        return render_template('base.html'), 404

    @app.errorhandler(500)
    def internal_error(error):
        from flask import render_template, request, jsonify
        db.session.rollback()
        if request.path.startswith('/api/'):
            return jsonify({'error': True, 'message': 'Internal server error'}), 500
        return render_template('base.html'), 500

    # CSRF exemption for API endpoints that use token auth
    csrf.exempt('api')

    return app
