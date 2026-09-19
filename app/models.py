from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Activity(db.Model):
    __tablename__ = 'activities'

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    filepath = db.Column(db.String(512), nullable=False)
    sport = db.Column(db.String(50), nullable=False, index=True)
    subsport = db.Column(db.String(50))
    activity_date = db.Column(db.DateTime, nullable=False, index=True)
    vo2_max_min = db.Column(db.Float)
    vo2_max_max = db.Column(db.Float)
    vo2_samples = db.Column(db.Integer, default=0)
    event_samples = db.Column(db.Integer, default=0)
    duration_seconds = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def vo2_max_avg(self):
        if self.vo2_max_min and self.vo2_max_max:
            return round((self.vo2_max_min + self.vo2_max_max) / 2, 2)
        return None

    @property
    def has_vo2_data(self):
        return self.vo2_samples > 0

    @property
    def duration_formatted(self):
        if not self.duration_seconds:
            return "-"
        hours, remainder = divmod(self.duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def __repr__(self):
        return f'<Activity {self.sport} {self.activity_date}>'
