import fitparse
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ParsedActivity:
    """Result of parsing a FIT file"""
    sport: str
    subsport: Optional[str]
    activity_date: datetime
    activity_end: datetime
    vo2_max_min: Optional[float]
    vo2_max_max: Optional[float]
    vo2_samples: int
    event_samples: int

    @property
    def duration_seconds(self) -> int:
        return int((self.activity_end - self.activity_date).total_seconds())


def parse_fit_file(filepath: str) -> ParsedActivity:
    """
    Parse a FIT file and extract VO2 max data.

    Args:
        filepath: Path to the .fit file

    Returns:
        ParsedActivity with extracted data

    Raises:
        ValueError: If file cannot be parsed or has no activity data
    """
    fitparsed = fitparse.FitFile(filepath)

    # Extract sport type
    sport = "unknown"
    subsport = None
    for record in fitparsed.get_messages("sport"):
        vals = record.get_values()
        sport = vals.get('sport', 'unknown')
        subsport = vals.get('subsport')

    # Extract date range from events
    dates = []
    for record in fitparsed.get_messages("event"):
        ts = record.get_values().get('timestamp')
        if ts:
            dates.append(ts)

    if not dates:
        raise ValueError("No event timestamps found in file")

    activity_date = min(dates)
    activity_end = max(dates)
    event_samples = len(dates)

    # Extract VO2 max from unknown_140 messages
    vo2_values = []
    for record in fitparsed.get_messages("unknown_140"):
        raw = record.get_values().get('unknown_7')
        if raw:
            vo2 = round(raw * 3.5 / 65536, 2)
            vo2_values.append(vo2)

    vo2_max_min = min(vo2_values) if vo2_values else None
    vo2_max_max = max(vo2_values) if vo2_values else None

    return ParsedActivity(
        sport=sport,
        subsport=subsport,
        activity_date=activity_date,
        activity_end=activity_end,
        vo2_max_min=vo2_max_min,
        vo2_max_max=vo2_max_max,
        vo2_samples=len(vo2_values),
        event_samples=event_samples
    )


def is_cycling(sport: str, subsport: str = None) -> bool:
    """Check if activity is a cycling activity"""
    cycling_sports = {'cycling', 'biking'}
    cycling_subsports = {'indoor_cycling', 'road', 'mountain', 'gravel', 'cyclocross',
                         'virtual_activity', 'e_bike'}
    return sport in cycling_sports or (subsport and subsport in cycling_subsports)


def is_running(sport: str, subsport: str = None) -> bool:
    """Check if activity is a running activity"""
    running_sports = {'running', 'trail_running'}
    running_subsports = {'treadmill', 'trail', 'track', 'virtual_activity'}
    return sport in running_sports or (subsport and subsport in running_subsports)
