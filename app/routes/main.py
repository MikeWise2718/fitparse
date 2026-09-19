from flask import Blueprint, render_template
from ..models import Activity, db
from ..services.fit_parser import is_cycling, is_running

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    # Get summary stats
    total_activities = Activity.query.count()

    cycling_activities = Activity.query.filter(
        Activity.sport.in_(['cycling', 'biking'])
    ).count()

    running_activities = Activity.query.filter(
        Activity.sport.in_(['running', 'trail_running'])
    ).count()

    # Get latest VO2 max for cycling
    latest_cycling = Activity.query.filter(
        Activity.sport.in_(['cycling', 'biking']),
        Activity.vo2_samples > 0
    ).order_by(Activity.activity_date.desc()).first()

    # Get latest VO2 max for running
    latest_running = Activity.query.filter(
        Activity.sport.in_(['running', 'trail_running']),
        Activity.vo2_samples > 0
    ).order_by(Activity.activity_date.desc()).first()

    # Get recent activities
    recent_activities = Activity.query.order_by(
        Activity.activity_date.desc()
    ).limit(10).all()

    return render_template('index.html',
                           total_activities=total_activities,
                           cycling_activities=cycling_activities,
                           running_activities=running_activities,
                           latest_cycling=latest_cycling,
                           latest_running=latest_running,
                           recent_activities=recent_activities)
