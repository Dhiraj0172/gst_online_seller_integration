from flask import render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from app.models import User
from app.extensions import db
from . import auth_bp

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
            return redirect(next_page if next_page else url_for('main.dashboard'))
        
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
