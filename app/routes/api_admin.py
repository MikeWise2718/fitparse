"""Admin: users, invites, global settings, system status. Never another user's fitness data."""
import shutil

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import func

from .. import auth
from ..events import event_logger
from ..models import FitFile, Invite, Job, Session, User, db, utcnow
from ..settings import get_global_settings, save_global_settings

admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/api/admin/users')
@auth.admin_required
def users():
    counts = dict(db.session.query(FitFile.user_id, func.count(FitFile.id)).group_by(FitFile.user_id).all())
    sizes = dict(db.session.query(FitFile.user_id, func.sum(FitFile.size)).group_by(FitFile.user_id).all())
    out = []
    for user in User.query.order_by(User.id).all():
        row = user.to_dict()
        row['files'] = counts.get(user.id, 0)
        row['bytes'] = int(sizes.get(user.id) or 0)
        out.append(row)
    return jsonify({'users': out})


def _target(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if not user:
        from flask import abort
        abort(404)
    return user


def _other_active_admins(user: User) -> int:
    return User.query.filter(User.role == 'admin', User.is_active.is_(True), User.id != user.id).count()


@admin_bp.route('/api/admin/users/<int:user_id>', methods=['POST'])
@auth.admin_required
def update_user(user_id):
    user = _target(user_id)
    data = request.get_json(silent=True) or {}
    losing_admin = (('is_active' in data and not data['is_active']) or
                    ('role' in data and data['role'] != 'admin')) and user.is_admin
    if losing_admin and not _other_active_admins(user):
        return jsonify({'error': 'last_admin', 'message': 'There must be at least one active admin.'}), 400
    if 'is_active' in data:
        user.is_active = bool(data['is_active'])
        if not user.is_active:
            auth.revoke_all_tokens(user.id)
    if data.get('role') in ('admin', 'user'):
        user.role = data['role']
    if 'garmin_web_connect' in data:
        user.garmin_web_connect = bool(data['garmin_web_connect'])
    db.session.commit()
    event_logger.info('admin.user_updated', f'{g.user.username} updated {user.username}',
                      admin_id=g.user.id, user_id=user.id,
                      changes={k: data[k] for k in ('is_active', 'role', 'garmin_web_connect') if k in data})
    return jsonify({'user': user.to_dict()})


@admin_bp.route('/api/admin/invites')
@auth.admin_required
def invites():
    now = utcnow()
    rows = Invite.query.order_by(Invite.created_at.desc()).limit(100).all()
    return jsonify({'invites': [{
        'id': i.id, 'role': i.role, 'note': i.note, 'created_at': i.created_at,
        'expires_at': i.expires_at, 'used_at': i.used_at, 'revoked_at': i.revoked_at,
        'state': ('used' if i.used_at else 'revoked' if i.revoked_at
                  else 'expired' if i.expires_at < now else 'open'),
    } for i in rows]})


@admin_bp.route('/api/admin/invites', methods=['POST'])
@auth.admin_required
def new_invite():
    data = request.get_json(silent=True) or {}
    invite, raw = auth.create_invite(g.user.id, role=data.get('role', 'user'), note=data.get('note'))
    base = get_global_settings(current_app.config['FITMON_HOME'])['public_base_url'] or request.host_url
    # The raw token is shown exactly once; only its hash is stored.
    return jsonify({'id': invite.id, 'expires_at': invite.expires_at,
                    'url': f"{base.rstrip('/')}/invite/{raw}"})


@admin_bp.route('/api/admin/invites/<int:invite_id>/revoke', methods=['POST'])
@auth.admin_required
def revoke_invite(invite_id):
    invite = db.session.get(Invite, invite_id)
    if not invite:
        return jsonify({'error': 'not_found'}), 404
    invite.revoked_at = utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@admin_bp.route('/api/admin/settings', methods=['GET', 'POST'])
@auth.admin_required
def global_settings():
    home = current_app.config['FITMON_HOME']
    if request.method == 'POST':
        return jsonify(save_global_settings(home, request.get_json(silent=True) or {}))
    return jsonify(get_global_settings(home))


@admin_bp.route('/api/admin/status')
@auth.admin_required
def status():
    home = current_app.config['FITMON_HOME']
    usage = shutil.disk_usage(home)
    db_file = home / 'data' / 'fitmon.db'
    return jsonify({
        'users': User.query.count(),
        'files': FitFile.query.count(),
        'sessions': Session.query.count(),
        'jobs_queued': Job.query.filter_by(status='queued').count(),
        'jobs_running': Job.query.filter_by(status='running').count(),
        'db_bytes': db_file.stat().st_size if db_file.exists() else 0,
        'disk_free_bytes': usage.free,
        'disk_total_bytes': usage.total,
        'home': str(home),
    })


@admin_bp.route('/api/admin/events')
@auth.admin_required
def events():
    return jsonify({'events': event_logger.tail(200, request.args.get('prefix'))})
