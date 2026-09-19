from flask import Blueprint, render_template, request, jsonify
from datetime import datetime
from ..models import Activity, db

cycling_bp = Blueprint('cycling', __name__)

CYCLING_SPORTS = ['cycling', 'biking']


@cycling_bp.route('/')
def cycling_list():
    page = request.args.get('page', 1, type=int)
    per_page = 20

    # Date filters
    date_from = request.args.get('from')
    date_to = request.args.get('to')
    subsport = request.args.get('subsport')

    query = Activity.query.filter(Activity.sport.in_(CYCLING_SPORTS))

    if date_from:
        try:
            query = query.filter(Activity.activity_date >= datetime.fromisoformat(date_from))
        except ValueError:
            pass

    if date_to:
        try:
            query = query.filter(Activity.activity_date <= datetime.fromisoformat(date_to))
        except ValueError:
            pass

    if subsport:
        query = query.filter(Activity.subsport == subsport)

    # Get unique subsports for filter dropdown
    subsports = db.session.query(Activity.subsport).filter(
        Activity.sport.in_(CYCLING_SPORTS),
        Activity.subsport.isnot(None)
    ).distinct().all()
    subsports = [s[0] for s in subsports if s[0]]

    activities = query.order_by(Activity.activity_date.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return render_template('cycling.html',
                           activities=activities,
                           subsports=subsports,
                           current_subsport=subsport,
                           date_from=date_from,
                           date_to=date_to)


@cycling_bp.route('/chart')
def cycling_chart():
    date_from = request.args.get('from')
    date_to = request.args.get('to')
    subsport = request.args.get('subsport')

    query = Activity.query.filter(
        Activity.sport.in_(CYCLING_SPORTS),
        Activity.vo2_samples > 0
    )

    if date_from:
        try:
            query = query.filter(Activity.activity_date >= datetime.fromisoformat(date_from))
        except ValueError:
            pass

    if date_to:
        try:
            query = query.filter(Activity.activity_date <= datetime.fromisoformat(date_to))
        except ValueError:
            pass

    if subsport:
        query = query.filter(Activity.subsport == subsport)

    activities = query.order_by(Activity.activity_date.asc()).all()

    labels = [a.activity_date.strftime('%Y-%m-%d') for a in activities]
    data = [a.vo2_max_max for a in activities]

    return jsonify({
        "labels": labels,
        "datasets": [{
            "label": "VO2 Max",
            "data": data,
            "borderColor": "#0d6efd",
            "backgroundColor": "rgba(13, 110, 253, 0.1)",
            "tension": 0.1,
            "fill": True
        }]
    })
