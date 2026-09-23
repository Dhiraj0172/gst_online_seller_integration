from urllib.parse import urlsplit, unquote
from flask import render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from app.models import User
from app.extensions import db
from . import auth_bp


def is_safe_redirect_url(target: str) -> bool:
    """Validate that a redirect target URL is safe and strictly internal.

    Rejects:
    - None / empty / non-string targets
    - External absolute URLs (http://..., https://...)
    - Protocol-relative URLs (//..., ///...)
    - Backslash-based bypasses (\\..., /\\...)
    - Encoded bypass attempts (%5c, %2f%2f)
    - URLs with non-HTTP schemes (javascript:, data:)
    - Relative paths not starting with a single '/'
    """
    if not target or not isinstance(target, str):
        return False
    target = target.strip()
    if not target:
        return False
    if '\\' in target:
        return False
    unquoted = unquote(target).replace('\\', '/')
    if unquoted.startswith('//') or not unquoted.startswith('/'):
        return False
    if not target.startswith('/') or target.startswith('//'):
        return False
    try:
        parsed = urlsplit(target)
    except Exception:
        return False
    if parsed.scheme or parsed.netloc:
        return False
    try:
        parsed_unquoted = urlsplit(unquoted)
        if parsed_unquoted.scheme or parsed_unquoted.netloc:
            return False
    except Exception:
        return False
    return True


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    if request.method == 'POST':
        identifier = (request.form.get('email') or request.form.get('username') or '').strip()
        password = request.form.get('password', '')
        
        user = User.query.filter(
            (User.email == identifier) | (User.username == identifier)
        ).first()
        
        if user and user.check_password(password):
            session.pop('active_profile_id', None)
            login_user(user, remember=True)
            next_page = request.args.get('next')
            if next_page and is_safe_redirect_url(next_page):
                return redirect(next_page)
            return redirect(url_for('main.dashboard'))
        
        flash('Please check your login details and try again.', 'danger')
        
    return render_template('auth/login.html')

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
        
    if request.method == 'POST':
        email = (request.form.get('email') or '').strip()
        password = request.form.get('password', '')
        username = (request.form.get('username') or request.form.get('name') or email.split('@')[0]).strip()
        
        existing = User.query.filter((User.email == email) | (User.username == username)).first()
        if existing:
            flash('An account with this email or username already exists', 'danger')
            return redirect(url_for('auth.register'))
            
        new_user = User(
            email=email,
            username=username
        )
        new_user.set_password(password)
        
        db.session.add(new_user)
        db.session.commit()
        
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('auth.login'))
        
    return render_template('auth/register.html')

@auth_bp.route('/logout')
@login_required
def logout():
    session.pop('active_profile_id', None)
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))
