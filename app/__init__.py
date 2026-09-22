"""
GST Online Seller - Flask Application Factory
"""
import os
import logging
from pathlib import Path
from flask import Flask
from .extensions import db, login_manager, csrf


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
        from flask import session
        from flask_login import current_user
        from .models.gst_profile import GSTProfile
        active_gstin = 'Not Selected'
        active_profile = None
        if current_user and current_user.is_authenticated:
            prof_id = session.get('active_profile_id')
            if prof_id:
                active_profile = db.session.get(GSTProfile, prof_id)
                if active_profile:
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
