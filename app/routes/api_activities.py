import csv
import io
from datetime import datetime, timedelta

from flask import Blueprint, Response, current_app, g, jsonify, request, send_file
from sqlalchemy import func

from .. import auth
from ..events import event_logger
from ..models import (BestEffort, Device, FitFile, Lap, Length, RECORD_COLUMNS, Record, Session,
                      SessionTrim, StrengthSet, ZoneTime, db)
from ..services import importer, metrics

activities_bp = Blueprint('activities', __name__)

LIST_COLUMNS = ['id', 'file_id', 'idx', 'name', 'sport', 'sub_sport', 'start_time', 'timer_s',
                'elapsed_s', 'distance_m', 'calories', 'avg_hr', 'max_hr', 'avg_speed', 'avg_power',
                'norm_power', 'avg_cadence', 'decoupling', 'ascent_m', 'te_aerobic', 'te_anaerobic', 'vo2max', 'load',
                'load_model', 'tss', 'trimp', 'has_gps', 'has_power', 'trimmed_s', 'avg_swolf', 'total_sets',
                'volume_kg']
SORTABLE = {'start_time', 'distance_m', 'timer_s', 'avg_hr', 'max_hr', 'avg_power', 'norm_power',
            'load', 'vo2max', 'sport', 'te_aerobic', 'te_anaerobic', 'ascent_m', 'avg_speed',
            'calories', 'avg_cadence', 'decoupling', 'tss'}

# Numeric columns the list can be filtered on, as ?min_<name>= / ?max_<name>=. Server-side so a
# filter applies to every activity, not just the page already loaded in the browser.
FILTERABLE = {'distance_m', 'timer_s', 'avg_hr', 'max_hr', 'avg_power', 'norm_power', 'load',
              'vo2max', 'ascent_m', 'avg_speed', 'te_aerobic', 'te_anaerobic', 'calories',
              'avg_cadence', 'decoupling', 'tss'}


def row_dict(obj, columns=None) -> dict:
    columns = columns or obj.__table__.columns.keys()
    return {c: getattr(obj, c) for c in columns}


def parse_day(value: str | None):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def filtered_sessions():
    """Session query for the current user with the shared ?sport=&sub_sport=&from=&to= filters."""
    q = auth.scoped(Session)
    sport = request.args.get('sport')
    if sport:
        q = q.filter(Session.sport.in_(sport.split(',')))
    if request.args.get('sub_sport'):
        q = q.filter(Session.sub_sport == request.args['sub_sport'])
    start, end = parse_day(request.args.get('from')), parse_day(request.args.get('to'))
    if start:
        q = q.filter(Session.start_time >= start)
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))
    for name in FILTERABLE:
        column = getattr(Session, name)
        for prefix, op in (('min_', column.__ge__), ('max_', column.__le__)):
            raw = request.args.get(prefix + name)
            if raw not in (None, ''):
                try:
                    # A row with no value is excluded rather than treated as zero: "rides over
                    # 200 W" should not sweep in every ride that recorded no power at all.
                    q = q.filter(column.isnot(None), op(float(raw)))
                except ValueError:
                    pass
    return q


@activities_bp.route('/api/activities')
def list_activities():
    q = filtered_sessions()
    text = (request.args.get('q') or '').strip()
    if text:
        like = f'%{text}%'
        q = q.filter(db.or_(Session.name.ilike(like), Session.sport.ilike(like),
                            Session.sub_sport.ilike(like)))
    if request.args.get('hide_transitions', '1') == '1':
        q = q.filter(Session.sport != 'transition')
    sort = request.args.get('sort', 'start_time')
    column = getattr(Session, sort if sort in SORTABLE else 'start_time')
    q = q.order_by(column.asc() if request.args.get('dir') == 'asc' else column.desc())
    total = q.count()
    limit = min(request.args.get('limit', 100, type=int), 500)
    rows = q.offset(request.args.get('offset', 0, type=int)).limit(limit).all()

    file_ids = {r.file_id for r in rows}
    files = {f.id: f for f in auth.scoped(FitFile).filter(FitFile.id.in_(file_ids)).all()} if file_ids else {}
    legs = dict(db.session.query(Session.file_id, func.count(Session.id))
                .filter(Session.file_id.in_(file_ids)).group_by(Session.file_id).all()) if file_ids else {}
    out = []
    for r in rows:
        item = row_dict(r, LIST_COLUMNS)
        f = files.get(r.file_id)
        item.update({'parse_status': f.parse_status if f else None,
                     'utc_offset_s': f.utc_offset_s if f else None,
                     'legs': legs.get(r.file_id, 1)})
        out.append(item)
    return jsonify({'total': total, 'activities': out})


@activities_bp.route('/api/sports')
def sports():
    rows = (db.session.query(Session.sport, Session.sub_sport, func.count(Session.id))
            .filter(Session.user_id == g.user.id).group_by(Session.sport, Session.sub_sport).all())
    out: dict = {}
    for sport, sub, n in rows:
        entry = out.setdefault(sport, {'sport': sport, 'count': 0, 'sub_sports': {}})
        entry['count'] += n
        if sub:
            entry['sub_sports'][sub] = n
    return jsonify({'sports': sorted(out.values(), key=lambda e: -e['count'])})


@activities_bp.route('/api/sessions/<int:session_id>')
def session_detail(session_id):
    sess = auth.get_owned(Session, session_id)
    f = sess.file
    siblings = [{'id': s.id, 'idx': s.idx, 'sport': s.sport, 'sub_sport': s.sub_sport,
                 'timer_s': s.timer_s, 'distance_m': s.distance_m}
                for s in Session.query.filter_by(file_id=f.id).order_by(Session.idx).all()]
    devices = Device.query.filter_by(file_id=f.id).all()
    return jsonify({
        'session': row_dict(sess),
        'file': {'id': f.id, 'name': f.original_name, 'parse_status': f.parse_status,
                 'parse_error': f.parse_error, 'utc_offset_s': f.utc_offset_s, 'product': f.product,
                 'manufacturer': f.manufacturer, 'source': f.source, 'size': f.size,
                 'garmin_activity_id': f.garmin_activity_id},
        'siblings': siblings,
        'laps': [row_dict(x) for x in sess.laps],
        'lengths': [row_dict(x) for x in sess.lengths],
        'sets': [row_dict(x) for x in sess.sets],
        'zones': [row_dict(x) for x in sorted(sess.zone_times, key=lambda z: (z.kind, z.zone))],
        'best_efforts': [row_dict(x, ['kind', 'window', 'value', 'offset_s'])
                         for x in sorted(sess.best_efforts, key=lambda b: (b.kind, b.window))],
        'devices': [row_dict(d) for d in devices],
    })


@activities_bp.route('/api/sessions/<int:session_id>/records')
def session_records(session_id):
    sess = auth.get_owned(Session, session_id)
    wanted = [c for c in (request.args.get('fields') or 'hr,speed,alt,cad,power').split(',')
              if c in RECORD_COLUMNS and c not in ('session_id', 'idx', 't')]
    columns = [Record.t] + [getattr(Record, c) for c in wanted]
    rows = (db.session.query(*columns).filter(Record.session_id == sess.id)
            .order_by(Record.idx).all())
    target = min(request.args.get('downsample', 1500, type=int), 20000)
    step = max(1, len(rows) // target) if target else 1
    series = {name: [] for name in ['t'] + wanted}
    # Bucket means rather than every n-th sample, so short power/HR spikes still move the line.
    for start in range(0, len(rows), step):
        bucket = rows[start:start + step]
        series['t'].append(bucket[0][0])
        for i, name in enumerate(wanted, start=1):
            values = [r[i] for r in bucket if r[i] is not None]
            series[name].append(round(sum(values) / len(values), 2) if values else None)
    return jsonify({'session_id': sess.id, 'points': len(series['t']), 'step': step, 'series': series})


@activities_bp.route('/api/sessions/<int:session_id>/track')
def session_track(session_id):
    sess = auth.get_owned(Session, session_id)
    rows = (db.session.query(Record.lon, Record.lat, Record.t, Record.hr, Record.speed, Record.power)
            .filter(Record.session_id == sess.id, Record.lat.isnot(None), Record.lon.isnot(None))
            .order_by(Record.idx).all())
    step = max(1, len(rows) // 3000)
    rows = rows[::step]
    return jsonify({
        'type': 'Feature',
        'geometry': {'type': 'LineString', 'coordinates': [[r[0], r[1]] for r in rows]},
        'properties': {'t': [r[2] for r in rows], 'hr': [r[3] for r in rows],
                       'speed': [r[4] for r in rows], 'power': [r[5] for r in rows]},
    })


@activities_bp.route('/api/sessions/<int:session_id>/name', methods=['POST'])
def rename_session(session_id):
    sess = auth.get_owned(Session, session_id)
    sess.name = ((request.get_json(silent=True) or {}).get('name') or '').strip()[:200] or None
    db.session.commit()
    return jsonify({'ok': True, 'name': sess.name})


@activities_bp.route('/api/sessions/<int:session_id>/trim', methods=['POST'])
def trim_session(session_id):
    """Mark where the activity really ended, when the watch was left recording afterwards.

    No sample is deleted: the trim decides which of them the derived figures are computed over,
    and is stored per (file, leg) so it survives a re-index. Post {"end_t": null} to undo.
    """
    sess = auth.get_owned(Session, session_id)
    end_t = (request.get_json(silent=True) or {}).get('end_t')
    existing = db.session.get(SessionTrim, (sess.file_id, sess.idx))
    if end_t is None:
        if existing:
            db.session.delete(existing)
            db.session.commit()
    else:
        try:
            end_t = int(end_t)
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid_end_t'}), 400
        if end_t < 60:
            return jsonify({'error': 'invalid_end_t', 'message': 'Keep at least a minute.'}), 400
        if existing:
            existing.end_t = end_t
        else:
            db.session.add(SessionTrim(file_id=sess.file_id, idx=sess.idx, user_id=g.user.id, end_t=end_t))
        db.session.commit()
    event_logger.info('session.trimmed', f'session {sess.id} trim set to {end_t}',
                      user_id=g.user.id, session_id=sess.id, end_t=end_t)
    # Re-index the file so every derived figure is recomputed from the trimmed samples.
    importer.reindex_file(current_app.config['FITMON_HOME'], sess.file)
    fresh = Session.query.filter_by(file_id=sess.file_id, idx=sess.idx).first()
    return jsonify({'ok': True, 'session_id': fresh.id if fresh else None})


@activities_bp.route('/api/sessions/<int:session_id>/trim/suggest')
def suggest_session_trim(session_id):
    sess = auth.get_owned(Session, session_id)
    rows = (db.session.query(Record.t, Record.dist, Record.speed, Record.hr)
            .filter(Record.session_id == sess.id).order_by(Record.idx).all())
    records = [{'t': r[0], 'dist': r[1], 'speed': r[2], 'hr': r[3]} for r in rows]
    trim = db.session.get(SessionTrim, (sess.file_id, sess.idx))
    return jsonify({'suggestion': metrics.suggest_trim(records, sess.sport),
                    'current': trim.end_t if trim else None,
                    'last_t': records[-1]['t'] if records else None})


@activities_bp.route('/api/files/<int:file_id>/reparse', methods=['POST'])
def reparse_file(file_id):
    f = auth.get_owned(FitFile, file_id)
    status = importer.reindex_file(current_app.config['FITMON_HOME'], f)
    return jsonify({'ok': True, 'parse_status': status, 'parse_error': f.parse_error})


@activities_bp.route('/api/files/<int:file_id>/delete', methods=['POST'])
def delete_file(file_id):
    f = auth.get_owned(FitFile, file_id)
    event_logger.info('file.deleted', f'{f.original_name} deleted', user_id=g.user.id, file_id=f.id)
    importer.delete_file(current_app.config['FITMON_HOME'], f)
    return jsonify({'ok': True})


@activities_bp.route('/api/files/<int:file_id>/download')
def download_file(file_id):
    f = auth.get_owned(FitFile, file_id)
    # The path is derived from the owned row's hash - never from anything the client sent.
    path = importer.fit_path(current_app.config['FITMON_HOME'], g.user.id, f.sha256)
    if not path.exists():
        return jsonify({'error': 'not_found'}), 404
    return send_file(path, as_attachment=True, download_name=f.original_name or f'{f.sha256[:12]}.fit',
                     mimetype='application/octet-stream')


@activities_bp.route('/api/export/activities.csv')
def export_csv():
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(LIST_COLUMNS)
    for sess in filtered_sessions().order_by(Session.start_time).all():
        writer.writerow([getattr(sess, c) if getattr(sess, c) is not None else '' for c in LIST_COLUMNS])
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=fitmon-activities.csv'})
