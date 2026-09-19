import os
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from werkzeug.utils import secure_filename
from ..models import Activity, db
from ..services.fit_parser import parse_fit_file

upload_bp = Blueprint('upload', __name__)


def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']


@upload_bp.route('/', methods=['GET'])
def upload_form():
    return render_template('upload.html')


@upload_bp.route('/', methods=['POST'])
def upload_file():
    if 'files[]' not in request.files:
        flash('No file part', 'error')
        return redirect(request.url)

    files = request.files.getlist('files[]')
    if not files or all(f.filename == '' for f in files):
        flash('No selected file', 'error')
        return redirect(request.url)

    success_count = 0
    error_count = 0
    duplicate_count = 0

    for file in files:
        if file and file.filename and allowed_file(file.filename):
            result = process_uploaded_file(file)
            if result == 'success':
                success_count += 1
            elif result == 'duplicate':
                duplicate_count += 1
            else:
                error_count += 1

    if success_count:
        flash(f'Successfully uploaded {success_count} file(s)', 'success')
    if duplicate_count:
        flash(f'{duplicate_count} file(s) already exist', 'warning')
    if error_count:
        flash(f'Failed to process {error_count} file(s)', 'error')

    return redirect(url_for('main.index'))


def process_uploaded_file(file):
    """Process a single uploaded file"""
    try:
        original_filename = secure_filename(file.filename)
        # Add UUID prefix to avoid collisions
        unique_filename = f"{uuid.uuid4().hex}_{original_filename}"
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)

        # Save file temporarily to parse
        file.save(filepath)

        # Parse the FIT file
        parsed = parse_fit_file(filepath)

        # Check for duplicate (same filename and date)
        existing = Activity.query.filter_by(
            filename=original_filename,
            activity_date=parsed.activity_date
        ).first()

        if existing:
            # Remove the duplicate file
            os.remove(filepath)
            return 'duplicate'

        # Create activity record
        activity = Activity(
            filename=original_filename,
            filepath=filepath,
            sport=parsed.sport,
            subsport=parsed.subsport,
            activity_date=parsed.activity_date,
            vo2_max_min=parsed.vo2_max_min,
            vo2_max_max=parsed.vo2_max_max,
            vo2_samples=parsed.vo2_samples,
            event_samples=parsed.event_samples,
            duration_seconds=parsed.duration_seconds
        )
        db.session.add(activity)
        db.session.commit()
        return 'success'

    except Exception as e:
        current_app.logger.error(f"Error processing file: {e}")
        # Clean up file if it was saved
        if 'filepath' in locals() and os.path.exists(filepath):
            os.remove(filepath)
        return 'error'


@upload_bp.route('/directory', methods=['POST'])
def scan_directory():
    directory = request.form.get('directory', '').strip()

    if not directory:
        flash('Please enter a directory path', 'error')
        return redirect(url_for('upload.upload_form'))

    if not os.path.isdir(directory):
        flash(f'Directory not found: {directory}', 'error')
        return redirect(url_for('upload.upload_form'))

    success_count = 0
    error_count = 0
    duplicate_count = 0

    for filename in os.listdir(directory):
        if filename.lower().endswith('.fit'):
            source_path = os.path.join(directory, filename)
            result = process_directory_file(source_path, filename)
            if result == 'success':
                success_count += 1
            elif result == 'duplicate':
                duplicate_count += 1
            else:
                error_count += 1

    if success_count:
        flash(f'Successfully imported {success_count} file(s)', 'success')
    if duplicate_count:
        flash(f'{duplicate_count} file(s) already exist', 'warning')
    if error_count:
        flash(f'Failed to process {error_count} file(s)', 'error')
    if success_count == 0 and duplicate_count == 0 and error_count == 0:
        flash('No .fit files found in directory', 'warning')

    return redirect(url_for('main.index'))


def process_directory_file(source_path, filename):
    """Process a file from a local directory"""
    try:
        # Parse directly from source location
        parsed = parse_fit_file(source_path)

        # Check for duplicate
        existing = Activity.query.filter_by(
            filename=filename,
            activity_date=parsed.activity_date
        ).first()

        if existing:
            return 'duplicate'

        # Copy file to uploads folder
        unique_filename = f"{uuid.uuid4().hex}_{secure_filename(filename)}"
        dest_path = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)

        with open(source_path, 'rb') as src, open(dest_path, 'wb') as dst:
            dst.write(src.read())

        # Create activity record
        activity = Activity(
            filename=filename,
            filepath=dest_path,
            sport=parsed.sport,
            subsport=parsed.subsport,
            activity_date=parsed.activity_date,
            vo2_max_min=parsed.vo2_max_min,
            vo2_max_max=parsed.vo2_max_max,
            vo2_samples=parsed.vo2_samples,
            event_samples=parsed.event_samples,
            duration_seconds=parsed.duration_seconds
        )
        db.session.add(activity)
        db.session.commit()
        return 'success'

    except Exception as e:
        current_app.logger.error(f"Error processing file {filename}: {e}")
        return 'error'
