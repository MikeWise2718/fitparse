from flask import Blueprint, g, redirect, render_template, url_for

from .. import auth

pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
def index():
    return render_template('app.html', user=g.user, csrf=auth.csrf_token())


@pages_bp.route('/login')
def login_page():
    if g.user:
        return redirect(url_for('pages.index'))
    return render_template('login.html')


@pages_bp.route('/invite/<token>')
def invite_page(token):
    return render_template('invite.html', token=token, valid=auth.find_valid_invite(token) is not None)
