"""Your data is yours: take all of it with you, or remove all of it."""
import csv
import io
import shutil
import zipfile

from flask import Blueprint, current_app, g, jsonify, make_response, request, send_file, session
from werkzeug.security import check_password_hash

from .. import auth
from ..events import event_logger
from ..models import DailyHealth, FitFile, Session, User, db
from ..services import importer
from ..settings import user_dir
from .api_activities import LIST_COLUMNS

account_bp = Blueprint('account', __name__)


@account_bp.route('/api/account/export')
def export():
    home = current_app.config['FITMON_HOME']
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in auth.scoped(FitFile).order_by(FitFile.start_time).all():
            path = importer.fit_path(home, g.user.id, f.sha256)
            if path.exists():
                stamp = f.start_time.strftime('%Y-%m-%d-%H-%M-%S') if f.start_time else f.sha256[:12]
                zf.write(path, f'fit/{stamp}_{f.sha256[:8]}.fit')
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(LIST_COLUMNS)
        for s in auth.scoped(Session).order_by(Session.start_time).all():
            writer.writerow([getattr(s, c) if getattr(s, c) is not None else '' for c in LIST_COLUMNS])
        zf.writestr('activities.csv', out.getvalue())
        out = io.StringIO()
        cols = [c for c in DailyHealth.__table__.columns.keys() if c != 'user_id']
        writer = csv.writer(out)
        writer.writerow(cols)
        for h in DailyHealth.query.filter_by(user_id=g.user.id).order_by(DailyHealth.day).all():
            writer.writerow([getattr(h, c) if getattr(h, c) is not None else '' for c in cols])
        zf.writestr('daily_health.csv', out.getvalue())
    buf.seek(0)
    event_logger.info('account.exported', f'{g.user.username} exported their data', user_id=g.user.id)
    return send_file(buf, as_attachment=True, download_name=f'fitmon-{g.user.username}.zip',
                     mimetype='application/zip')


@account_bp.route('/api/account/delete', methods=['POST'])
def delete():
    data = request.get_json(silent=True) or {}
    if not check_password_hash(g.user.password_hash, data.get('password') or ''):
        return jsonify({'error': 'invalid_credentials'}), 401
    if g.user.is_admin and not User.query.filter(User.role == 'admin', User.is_active.is_(True),
                                                 User.id != g.user.id).count():
        return jsonify({'error': 'last_admin', 'message': 'Promote another admin first.'}), 400
    home, user_id, username = current_app.config['FITMON_HOME'], g.user.id, g.user.username
    # Rows go via ON DELETE CASCADE from users; files, Garmin tokens and health JSON live on disk.
    db.session.delete(g.user)
    db.session.commit()
    shutil.rmtree(user_dir(home, user_id), ignore_errors=True)
    event_logger.info('account.deleted', f'{username} deleted their account', user_id=user_id)
    session.clear()
    return auth.clear_device_cookie(make_response(jsonify({'ok': True})))
