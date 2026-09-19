"""Database schema.

The DB is a rebuildable index over the original FIT files kept on disk: there is no
migration framework. When SCHEMA_VERSION changes, the derived tables are dropped and every
file is reparsed (see services/importer.reindex_user). Account tables are never dropped.

Every row belongs to a user. `files.user_id` is the ownership root; `sessions.user_id` and a
few others are denormalised copies so scoped queries stay single-table.
All datetimes are naive UTC.
"""
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()

SCHEMA_VERSION = 1


@event.listens_for(Engine, 'connect')
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA journal_mode=WAL')
    cur.execute('PRAGMA synchronous=NORMAL')
    cur.execute('PRAGMA busy_timeout=5000')
    cur.execute('PRAGMA foreign_keys=ON')
    cur.close()


def utcnow() -> datetime:
    return datetime.utcnow()


# --------------------------------------------------------------------------- accounts

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True)  # stored case-folded
    display_name = db.Column(db.String(120))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default='user')   # admin | user
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    garmin_web_connect = db.Column(db.Boolean, nullable=False, default=False)  # admin-enabled
    created_at = db.Column(db.DateTime, default=utcnow)
    last_login_at = db.Column(db.DateTime)

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'username': self.username, 'display_name': self.display_name,
            'role': self.role, 'is_active': self.is_active,
            'garmin_web_connect': self.garmin_web_connect,
            'created_at': self.created_at, 'last_login_at': self.last_login_at,
        }


class DeviceToken(db.Model):
    """Remember-me tokens. Only the SHA-256 is stored; the raw token lives in the cookie."""
    __tablename__ = 'device_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    token_sha256 = db.Column(db.String(64), nullable=False, unique=True)
    device_name = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=utcnow)
    last_used_at = db.Column(db.DateTime, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime)


class Invite(db.Model):
    __tablename__ = 'invites'
    id = db.Column(db.Integer, primary_key=True)
    token_sha256 = db.Column(db.String(64), nullable=False, unique=True)
    role = db.Column(db.String(16), nullable=False, default='user')
    note = db.Column(db.String(200))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)


class Meta(db.Model):
    __tablename__ = 'meta'
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(255))


class Job(db.Model):
    """Work queue consumed by the single job worker (app/worker.py)."""
    __tablename__ = 'jobs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = db.Column(db.String(32), nullable=False)      # import | scan | reindex | sync | health
    payload = db.Column(db.Text, default='{}')
    status = db.Column(db.String(16), nullable=False, default='queued', index=True)  # queued|running|done|failed
    progress = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, default=0)
    message = db.Column(db.String(500))
    result = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)


# --------------------------------------------------------------------------- fit index

class FitFile(db.Model):
    __tablename__ = 'files'
    __table_args__ = (db.UniqueConstraint('user_id', 'sha256', name='uq_files_user_sha'),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    sha256 = db.Column(db.String(64), nullable=False)
    original_name = db.Column(db.String(255))
    size = db.Column(db.Integer)
    source = db.Column(db.String(16), default='upload')  # upload | zip | scan | garmin
    garmin_activity_id = db.Column(db.String(32), index=True)
    file_type = db.Column(db.String(32))
    manufacturer = db.Column(db.String(64))
    product = db.Column(db.String(64))
    serial_number = db.Column(db.String(32))
    time_created = db.Column(db.DateTime)
    utc_offset_s = db.Column(db.Integer)
    start_time = db.Column(db.DateTime, index=True)
    parse_status = db.Column(db.String(16), default='ok')  # ok | partial | failed
    parse_error = db.Column(db.String(500))
    message_counts = db.Column(db.Text)
    schema_version = db.Column(db.Integer, default=SCHEMA_VERSION)
    imported_at = db.Column(db.DateTime, default=utcnow)

    sessions = db.relationship('Session', backref='file', cascade='all, delete-orphan',
                               order_by='Session.idx', passive_deletes=True)


class Session(db.Model):
    """One sport leg. The unit every view works on; a triathlon file has several."""
    __tablename__ = 'sessions'
    __table_args__ = (db.Index('ix_sessions_user_start', 'user_id', 'start_time'),
                      db.Index('ix_sessions_user_sport', 'user_id', 'sport'))
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey('files.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    idx = db.Column(db.Integer, default=0)
    name = db.Column(db.String(200))
    sport = db.Column(db.String(50), nullable=False)
    sub_sport = db.Column(db.String(50))
    start_time = db.Column(db.DateTime, nullable=False)
    timer_s = db.Column(db.Float)
    elapsed_s = db.Column(db.Float)
    distance_m = db.Column(db.Float)
    calories = db.Column(db.Integer)
    avg_hr = db.Column(db.Integer)
    max_hr = db.Column(db.Integer)
    avg_speed = db.Column(db.Float)
    max_speed = db.Column(db.Float)
    ascent_m = db.Column(db.Float)
    descent_m = db.Column(db.Float)
    avg_power = db.Column(db.Float)
    max_power = db.Column(db.Float)
    norm_power = db.Column(db.Float)
    work_j = db.Column(db.Float)
    avg_cadence = db.Column(db.Float)
    max_cadence = db.Column(db.Float)
    avg_temp = db.Column(db.Float)
    te_aerobic = db.Column(db.Float)
    te_anaerobic = db.Column(db.Float)
    pool_length_m = db.Column(db.Float)
    num_laps = db.Column(db.Integer)
    start_lat = db.Column(db.Float)
    start_lon = db.Column(db.Float)
    vo2max = db.Column(db.Float)
    # derived at import (services/metrics.py)
    n_records = db.Column(db.Integer, default=0)
    has_gps = db.Column(db.Boolean, default=False)
    has_power = db.Column(db.Boolean, default=False)
    trimp = db.Column(db.Float)
    tss = db.Column(db.Float)
    intensity_factor = db.Column(db.Float)
    load = db.Column(db.Float)
    load_model = db.Column(db.String(8))
    efficiency = db.Column(db.Float)     # speed-or-power per heart beat
    decoupling = db.Column(db.Float)     # % drift, first vs second half
    total_strokes = db.Column(db.Integer)
    avg_swolf = db.Column(db.Float)
    total_sets = db.Column(db.Integer)
    total_reps = db.Column(db.Integer)
    volume_kg = db.Column(db.Float)

    laps = db.relationship('Lap', cascade='all, delete-orphan', order_by='Lap.idx', passive_deletes=True)
    lengths = db.relationship('Length', cascade='all, delete-orphan', order_by='Length.idx', passive_deletes=True)
    sets = db.relationship('StrengthSet', cascade='all, delete-orphan', order_by='StrengthSet.idx', passive_deletes=True)
    best_efforts = db.relationship('BestEffort', cascade='all, delete-orphan', passive_deletes=True)
    zone_times = db.relationship('ZoneTime', cascade='all, delete-orphan', passive_deletes=True)


class Lap(db.Model):
    __tablename__ = 'laps'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False, index=True)
    idx = db.Column(db.Integer)
    start_time = db.Column(db.DateTime)
    timer_s = db.Column(db.Float)
    elapsed_s = db.Column(db.Float)
    distance_m = db.Column(db.Float)
    avg_hr = db.Column(db.Integer)
    max_hr = db.Column(db.Integer)
    avg_speed = db.Column(db.Float)
    max_speed = db.Column(db.Float)
    avg_power = db.Column(db.Float)
    max_power = db.Column(db.Float)
    norm_power = db.Column(db.Float)
    avg_cadence = db.Column(db.Float)
    ascent_m = db.Column(db.Float)
    descent_m = db.Column(db.Float)
    calories = db.Column(db.Integer)
    intensity = db.Column(db.String(24))
    trigger = db.Column(db.String(24))
    swim_stroke = db.Column(db.String(24))
    num_lengths = db.Column(db.Integer)


class Record(db.Model):
    """1 Hz samples. Clustered on (session_id, idx) and WITHOUT ROWID so one session's series
    is a single sequential range read. Never scanned across sessions."""
    __tablename__ = 'records'
    __table_args__ = {'sqlite_with_rowid': False}
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id', ondelete='CASCADE'), primary_key=True)
    idx = db.Column(db.Integer, primary_key=True)
    t = db.Column(db.Integer)            # seconds since session start
    hr = db.Column(db.Integer)
    speed = db.Column(db.Float)          # m/s
    dist = db.Column(db.Float)           # m
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    alt = db.Column(db.Float)
    cad = db.Column(db.Integer)
    power = db.Column(db.Integer)
    temp = db.Column(db.Integer)
    grade = db.Column(db.Float)
    lrb = db.Column(db.Float)            # left share of power, %
    vo = db.Column(db.Float)             # vertical oscillation, mm
    vr = db.Column(db.Float)             # vertical ratio, %
    gct = db.Column(db.Float)            # ground contact (stance) time, ms
    gct_bal = db.Column(db.Float)        # stance time balance, % left
    step_len = db.Column(db.Float)       # mm
    l_te = db.Column(db.Float)           # torque effectiveness %
    r_te = db.Column(db.Float)
    l_ps = db.Column(db.Float)           # pedal smoothness %
    r_ps = db.Column(db.Float)


RECORD_COLUMNS = [c.name for c in Record.__table__.columns]


class Length(db.Model):
    __tablename__ = 'lengths'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False, index=True)
    idx = db.Column(db.Integer)
    start_time = db.Column(db.DateTime)
    timer_s = db.Column(db.Float)
    length_type = db.Column(db.String(16))
    stroke = db.Column(db.String(24))
    strokes = db.Column(db.Integer)
    avg_speed = db.Column(db.Float)
    cadence = db.Column(db.Integer)
    swolf = db.Column(db.Float)


class StrengthSet(db.Model):
    __tablename__ = 'sets'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False, index=True)
    idx = db.Column(db.Integer)
    start_time = db.Column(db.DateTime)
    duration_s = db.Column(db.Float)
    set_type = db.Column(db.String(16))
    category = db.Column(db.String(64))
    subtype = db.Column(db.String(64))
    reps = db.Column(db.Integer)
    weight_kg = db.Column(db.Float)


class Device(db.Model):
    __tablename__ = 'devices'
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey('files.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    seen_at = db.Column(db.DateTime)
    device_index = db.Column(db.String(16))
    manufacturer = db.Column(db.String(64))
    product = db.Column(db.String(64))
    serial = db.Column(db.String(32))
    device_type = db.Column(db.String(64))
    source_type = db.Column(db.String(32))
    sw_version = db.Column(db.String(16))
    battery_status = db.Column(db.String(16))
    battery_voltage = db.Column(db.Float)
    operating_time_s = db.Column(db.Integer)


class ProfileSnapshot(db.Model):
    """Body + zone settings as the watch knew them when an activity was recorded."""
    __tablename__ = 'profile_snapshots'
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey('files.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    at = db.Column(db.DateTime, index=True)
    weight_kg = db.Column(db.Float)
    height_m = db.Column(db.Float)
    resting_hr = db.Column(db.Integer)
    max_hr = db.Column(db.Integer)
    threshold_hr = db.Column(db.Integer)
    ftp = db.Column(db.Integer)
    activity_class = db.Column(db.Float)
    gender = db.Column(db.String(8))


class BestEffort(db.Model):
    __tablename__ = 'best_efforts'
    __table_args__ = (db.Index('ix_best_user_kind_window', 'user_id', 'kind', 'window'),)
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    at = db.Column(db.DateTime)
    sport = db.Column(db.String(50))
    kind = db.Column(db.String(8))       # power (window = seconds, value = W) | pace (window = m, value = s)
    window = db.Column(db.Integer)
    value = db.Column(db.Float)
    offset_s = db.Column(db.Integer)


class ZoneTime(db.Model):
    __tablename__ = 'zone_time'
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id', ondelete='CASCADE'), primary_key=True)
    kind = db.Column(db.String(8), primary_key=True)   # hr | power
    zone = db.Column(db.Integer, primary_key=True)
    seconds = db.Column(db.Integer)


# --------------------------------------------------------------------------- garmin sync

class GarminAccount(db.Model):
    __tablename__ = 'garmin_accounts'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    status = db.Column(db.String(24), default='disconnected')  # ok | login_required | disconnected
    connected_at = db.Column(db.DateTime)
    last_sync_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(500))
    backfill_done = db.Column(db.Boolean, default=False)
    health_synced_to = db.Column(db.Date)


class GarminActivity(db.Model):
    """Resume state for the sync, and the metadata FIT files lack (activity name)."""
    __tablename__ = 'garmin_activities'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    activity_id = db.Column(db.String(32), primary_key=True)
    name = db.Column(db.String(200))
    type_key = db.Column(db.String(64))
    start_time = db.Column(db.DateTime)
    list_json = db.Column(db.Text)
    file_id = db.Column(db.Integer, db.ForeignKey('files.id', ondelete='SET NULL'))
    status = db.Column(db.String(16), default='new', index=True)  # new|imported|duplicate|no_original|failed
    error = db.Column(db.String(500))
    attempts = db.Column(db.Integer, default=0)
    fetched_at = db.Column(db.DateTime)


class DailyHealth(db.Model):
    __tablename__ = 'daily_health'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    day = db.Column(db.Date, primary_key=True)
    resting_hr = db.Column(db.Integer)
    hrv_avg = db.Column(db.Float)
    hrv_status = db.Column(db.String(24))
    sleep_s = db.Column(db.Integer)
    sleep_score = db.Column(db.Integer)
    bb_min = db.Column(db.Integer)
    bb_max = db.Column(db.Integer)
    stress_avg = db.Column(db.Integer)
    weight_kg = db.Column(db.Float)
    vo2max_run = db.Column(db.Float)
    vo2max_bike = db.Column(db.Float)
    training_status = db.Column(db.String(32))
    readiness = db.Column(db.Integer)


# Tables rebuilt by a reindex. Account, job and sync-state tables are not listed on purpose.
DERIVED_TABLES = ['zone_time', 'best_efforts', 'profile_snapshots', 'devices', 'sets',
                  'lengths', 'records', 'laps', 'sessions']
