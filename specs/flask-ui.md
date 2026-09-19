# Flask UI for FIT File VO2 Max Analysis

## Overview

Web application to upload and analyze Garmin FIT files, displaying VO2 max trends separately for cycling and running activities.

---

## Architecture

```
fitparse/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── config.py            # Configuration settings
│   ├── models.py            # SQLAlchemy models
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── main.py          # Dashboard, home
│   │   ├── upload.py        # File upload handling
│   │   ├── cycling.py       # Cycling VO2 max views
│   │   └── running.py       # Running VO2 max views
│   ├── services/
│   │   ├── __init__.py
│   │   └── fit_parser.py    # FIT file parsing logic (extracted from fitparse.py)
│   ├── templates/
│   │   ├── base.html        # Base template with nav
│   │   ├── index.html       # Dashboard
│   │   ├── upload.html      # Upload interface
│   │   ├── cycling.html     # Cycling VO2 max table + chart
│   │   ├── running.html     # Running VO2 max table + chart
│   │   └── activity.html    # Single activity detail
│   └── static/
│       ├── css/
│       │   └── style.css
│       └── js/
│           └── charts.js    # Chart.js initialization
├── uploads/                  # Uploaded FIT files (gitignored)
├── instance/
│   └── fitparse.db          # SQLite database
└── run.py                   # Entry point
```

---

## Data Model

### Activity Table

| Column | Type | Description |
|--------|------|-------------|
| id | Integer, PK | Auto-increment ID |
| filename | String(255) | Original filename |
| filepath | String(512) | Path to stored file |
| sport | String(50) | "cycling", "running", "swimming", etc. |
| subsport | String(50) | "indoor_cycling", "treadmill", etc. |
| activity_date | DateTime | Start timestamp of activity |
| vo2_max_min | Float | Minimum VO2 max reading in session |
| vo2_max_max | Float | Maximum VO2 max reading in session |
| vo2_samples | Integer | Number of VO2 max samples |
| event_samples | Integer | Number of event records |
| duration_seconds | Integer | Activity duration |
| created_at | DateTime | When record was created |

```python
# app/models.py
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
```

---

## Routes

### Main Routes (`/`)

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Dashboard with summary stats and recent activities |

**Dashboard displays:**
- Total activities parsed
- Count by sport type
- Latest VO2 max for cycling
- Latest VO2 max for running
- Quick links to cycling/running views

### Upload Routes (`/upload`)

| Route | Method | Description |
|-------|--------|-------------|
| `/upload` | GET | Upload form |
| `/upload` | POST | Handle file upload(s) |
| `/upload/directory` | POST | Scan a local directory path |

**Upload handling:**
1. Accept multiple .fit files via drag-drop or file picker
2. Save to `uploads/` directory with UUID prefix to avoid collisions
3. Parse each file immediately
4. Store Activity records in database
5. Redirect to dashboard with success/error flash messages

### Cycling Routes (`/cycling`)

| Route | Method | Description |
|-------|--------|-------------|
| `/cycling` | GET | List all cycling activities with VO2 max |
| `/cycling/chart` | GET | JSON endpoint for chart data |

**View features:**
- Table: Date, Subsport, VO2 Max (min-max), Duration, Filename
- Line chart: VO2 max over time (use vo2_max_max as primary metric)
- Filter by date range
- Filter by subsport (road, indoor, gravel, etc.)
- Sort by date (default: newest first)

### Running Routes (`/running`)

| Route | Method | Description |
|-------|--------|-------------|
| `/running` | GET | List all running activities with VO2 max |
| `/running/chart` | GET | JSON endpoint for chart data |

**View features:**
- Same structure as cycling
- Filter by subsport (trail, treadmill, track, etc.)

### Activity Detail (`/activity/<id>`)

| Route | Method | Description |
|-------|--------|-------------|
| `/activity/<id>` | GET | Detailed view of single activity |
| `/activity/<id>/delete` | POST | Delete activity and file |
| `/activity/<id>/reparse` | POST | Re-parse the FIT file |

---

## FIT Parser Service

Extract and refactor logic from `fitparse.py`:

```python
# app/services/fit_parser.py
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
```

---

## Templates

### Base Template (`base.html`)

- Navigation bar with links: Dashboard, Upload, Cycling, Running
- Flash message display area
- Footer
- Include Bootstrap 5 CDN for styling
- Include Chart.js CDN for charts

### Dashboard (`index.html`)

```
+------------------------------------------+
|  FIT File VO2 Max Analyzer               |
+------------------------------------------+
|  [Upload Files]                          |
+------------------------------------------+
|  Summary                                 |
|  +----------------+  +----------------+  |
|  | Cycling        |  | Running        |  |
|  | 47 activities  |  | 32 activities  |  |
|  | Latest: 52.3   |  | Latest: 48.7   |  |
|  | [View All →]   |  | [View All →]   |  |
|  +----------------+  +----------------+  |
+------------------------------------------+
|  Recent Activities                       |
|  Date       Sport    VO2 Max   File      |
|  2025-01-09 cycling  51.2-52.3 ride.fit  |
|  2025-01-08 running  47.8-48.7 run.fit   |
|  ...                                     |
+------------------------------------------+
```

### Cycling/Running Views (`cycling.html`, `running.html`)

```
+------------------------------------------+
|  Cycling VO2 Max                         |
+------------------------------------------+
|  Filters: [Date From] [Date To] [Apply]  |
|           Subsport: [All ▼]              |
+------------------------------------------+
|  VO2 Max Trend                           |
|  [========= LINE CHART =========]        |
|  Shows vo2_max_max over time             |
+------------------------------------------+
|  Activities                              |
|  Date       Subsport  VO2 Max    Dur     |
|  2025-01-09 road      51.2-52.3  1:30:00 |
|  2025-01-07 indoor    50.8-51.5  0:45:00 |
|  ...                                     |
|  [< Prev]                    [Next >]    |
+------------------------------------------+
```

### Upload View (`upload.html`)

```
+------------------------------------------+
|  Upload FIT Files                        |
+------------------------------------------+
|  +------------------------------------+  |
|  |                                    |  |
|  |   Drag & drop .fit files here      |  |
|  |   or click to browse               |  |
|  |                                    |  |
|  +------------------------------------+  |
|                                          |
|  -- OR --                                |
|                                          |
|  Scan Directory:                         |
|  [________________________] [Scan]       |
|  (Enter full path to folder with .fit)   |
+------------------------------------------+
```

---

## Configuration

```python
# app/config.py
import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///instance/fitparse.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads')
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB max upload
    ALLOWED_EXTENSIONS = {'fit'}
```

---

## Implementation Order

### Phase 1: Foundation
1. Create Flask app factory and configuration
2. Set up SQLAlchemy with Activity model
3. Create base template with Bootstrap
4. Implement dashboard route with placeholder data

### Phase 2: Core Parsing
5. Extract fit_parser.py service from existing fitparse.py
6. Write unit tests for parser service
7. Implement upload route (single file)
8. Store parsed activities in database

### Phase 3: Sport Views
9. Implement cycling list view with table
10. Implement running list view with table
11. Add date filtering to both views
12. Add pagination

### Phase 4: Visualization
13. Add Chart.js line charts to cycling view
14. Add Chart.js line charts to running view
15. Create JSON endpoints for chart data

### Phase 5: Enhanced Upload
16. Add multi-file upload with drag-drop
17. Add directory scanning feature
18. Add duplicate detection (by filename + date)
19. Add progress indicator for bulk uploads

### Phase 6: Polish
20. Activity detail view
21. Delete functionality
22. Re-parse functionality
23. Export to CSV

---

## Dependencies

Add to requirements.txt:
```
flask>=3.0.0
flask-sqlalchemy>=3.1.0
python-dotenv>=1.0.0
```

Frontend (CDN):
- Bootstrap 5.3
- Chart.js 4.x

---

## API Endpoints for Charts

```python
# GET /cycling/chart?from=2024-01-01&to=2025-01-10
# Response:
{
    "labels": ["2024-01-05", "2024-01-12", ...],
    "datasets": [{
        "label": "VO2 Max",
        "data": [48.5, 49.2, 50.1, ...],
        "borderColor": "#0d6efd",
        "tension": 0.1
    }]
}
```

---

## Key Decisions

1. **SQLite database** - Simple, no setup required, sufficient for personal use
2. **Store files locally** - Keep original FIT files for re-parsing if schema changes
3. **Separate cycling/running views** - Per user requirement, different VO2 max contexts
4. **Use vo2_max_max for trends** - Most meaningful metric for tracking fitness
5. **Bootstrap + Chart.js** - Fast to implement, no build step required
6. **No user authentication** - Personal tool, runs locally
