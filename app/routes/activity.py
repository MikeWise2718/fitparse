import os
import csv
from io import StringIO
from flask import Blueprint, render_template, redirect, url_for, flash, current_app, Response
from ..models import Activity, db
from ..services.fit_parser import parse_fit_file

activity_bp = Blueprint('activity', __name__)


@activity_bp.route('/<int:id>')
def detail(id):
    activity = Activity.query.get_or_404(id)
    return render_template('activity.html', activity=activity)


@activity_bp.route('/<int:id>/delete', methods=['POST'])
def delete(id):
    activity = Activity.query.get_or_404(id)

    # Delete the file if it exists
    if os.path.exists(activity.filepath):
        try:
            os.remove(activity.filepath)
        except OSError as e:
            current_app.logger.error(f"Error deleting file: {e}")

    db.session.delete(activity)
    db.session.commit()

    flash('Activity deleted successfully', 'success')
    return redirect(url_for('main.index'))


@activity_bp.route('/<int:id>/reparse', methods=['POST'])
def reparse(id):
    activity = Activity.query.get_or_404(id)

    if not os.path.exists(activity.filepath):
        flash('FIT file not found', 'error')
        return redirect(url_for('activity.detail', id=id))

    try:
        parsed = parse_fit_file(activity.filepath)

        activity.sport = parsed.sport
        activity.subsport = parsed.subsport
        activity.activity_date = parsed.activity_date
        activity.vo2_max_min = parsed.vo2_max_min
        activity.vo2_max_max = parsed.vo2_max_max
        activity.vo2_samples = parsed.vo2_samples
        activity.event_samples = parsed.event_samples
        activity.duration_seconds = parsed.duration_seconds

        db.session.commit()
        flash('Activity re-parsed successfully', 'success')

    except Exception as e:
        current_app.logger.error(f"Error re-parsing file: {e}")
        flash(f'Error re-parsing file: {str(e)}', 'error')

    return redirect(url_for('activity.detail', id=id))


@activity_bp.route('/export')
def export_csv():
    activities = Activity.query.order_by(Activity.activity_date.desc()).all()

    output = StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow([
        'Date', 'Sport', 'Subsport', 'VO2 Max Min', 'VO2 Max Max',
        'VO2 Samples', 'Duration (seconds)', 'Filename'
    ])

    for activity in activities:
        writer.writerow([
            activity.activity_date.isoformat() if activity.activity_date else '',
            activity.sport,
            activity.subsport or '',
            activity.vo2_max_min or '',
            activity.vo2_max_max or '',
            activity.vo2_samples,
            activity.duration_seconds or '',
            activity.filename
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=activities.csv'}
    )
