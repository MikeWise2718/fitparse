"""Make fitmon's landing-page pictures (app/static/screenshot.png and app/static/screenshots/).

    uv run --with playwright --with pillow python tools/screenshot.py -af D:/somewhere/run.fit
    uv run --with playwright python tools/screenshot.py -sv        # just serve the demo instance to look around

The pictures are public - /api/screenshot(s) need no sign-in and the landing page shows them
to guests. Three of them:

1. The dashboard, and 3. the Trends tab, from an INVENTED training year (a build, a race
   taper, a rebuild) for a "Demo athlete", stamped "Demo data" so they cannot pass for anyone's
   real numbers.
2. An animated playback of ONE REAL activity, given with -af and shown with Mike's consent
   (2026-10-15): the marker running the route while the cursor sweeps the zone-coloured heart
   rate. Its route passes the front door, so the first and last 500 m of the track are cut off
   the map in the capture, the way Garmin's privacy zones work. The FIT file is never copied
   into the repo. Without -af the existing animation is left as it is.

Everything runs against a throwaway fitmon home in a temp directory, served on localhost and
signed into with passwords generated in this process and never shown. 1280x800, en-GB,
Europe/Berlin. Fleet rules: D:/hw/pokeflute/docs/service-screenshots.md. Re-run when the UI changes.
"""
import argparse
import hashlib
import io
import json
import math
import os
import random
import secrets
import shutil
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich_argparse import RichHelpFormatter

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / 'app' / 'static'
DEFAULT_OUT = STATIC / 'screenshot.png'
PLAYBACK_OUT = STATIC / 'screenshots' / '2-playback.webp'
TRENDS_OUT = STATIC / 'screenshots' / '3-trends.png'
PRIVACY_M = 500                  # route cut from each end of the real activity's map
FRAMES, FRAME_MS = 64, 90        # the playback loop: about 6 s
WIDTH, HEIGHT = 1280, 800
UTC_OFFSET_S = 2 * 3600          # shown as Berlin summer time; the demo does not need DST
console = Console()

# weekday -> sessions (sport, sub_sport, base minutes, local start hour, name)
WEEK = {
    0: [('swimming', 'lap_swimming', 45, 7, 'Pool swim')],
    1: [('running', 'generic', 50, 18, 'Easy run')],
    2: [('cycling', 'road', 75, 18, 'Tempo ride')],
    3: [('running', 'generic', 45, 7, 'Run intervals'), ('training', 'strength_training', 40, 19, 'Strength')],
    4: [('swimming', 'lap_swimming', 40, 7, 'Technique swim')],
    5: [('cycling', 'road', 150, 9, 'Long ride')],
    6: [('running', 'generic', 80, 9, 'Long run')],
}
RACE_WEEK = 38                     # of 52: the season's main race, taper before, rest after


def week_factor(w: int) -> float:
    if w in (RACE_WEEK - 2, RACE_WEEK - 1):
        return 0.6                 # taper
    if w in (RACE_WEEK, RACE_WEEK + 1):
        return 0.45                # race week and the week after
    if w % 4 == 3:
        return 0.65                # recovery week
    if w < RACE_WEEK:
        return 0.6 + 0.4 * w / RACE_WEEK
    return 0.7 + 0.15 * (w - RACE_WEEK) / (52 - RACE_WEEK)


def seed(user_id: int, today: date, rng: random.Random) -> int:
    from app.models import DailyHealth, FitFile, ProfileSnapshot, Session, db

    first = today - timedelta(days=today.weekday()) - timedelta(weeks=51)
    n = 0
    last_file = None
    for w in range(52):
        f = week_factor(w)
        progress = w / 51
        for wd, sessions in WEEK.items():
            day = first + timedelta(weeks=w, days=wd)
            if day > today:
                break
            for sport, sub, minutes, hour, name in sessions:
                if rng.random() < 0.12 or (wd == 4 and f < 0.7):
                    continue                               # life happens; easy weeks drop a swim
                if w == RACE_WEEK and wd == 6:
                    sport, sub, minutes, hour, name = 'running', 'generic', 95, 8, 'Race: half marathon'
                mins = max(20, minutes * f * rng.uniform(0.9, 1.1))
                local = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour, minutes=rng.randint(0, 40))
                start = local - timedelta(seconds=UTC_OFFSET_S)
                n += 1
                last_file = FitFile(user_id=user_id, sha256=hashlib.sha256(f'demo-{n}'.encode()).hexdigest(),
                                    original_name=f'demo-{n}.fit', size=0, source='upload', file_type='activity',
                                    manufacturer='garmin', product='demo', utc_offset_s=UTC_OFFSET_S,
                                    start_time=start, time_created=start)
                db.session.add(last_file)
                db.session.flush()
                s = Session(file_id=last_file.id, user_id=user_id, idx=0, name=name, sport=sport, sub_sport=sub,
                            start_time=start, timer_s=mins * 60, elapsed_s=mins * 62, excluded=False,
                            vo2max_carried=False, has_gps=sport in ('running', 'cycling'), has_power=False)
                race = name.startswith('Race')
                if sport == 'running':
                    speed = (3.35 if race else rng.uniform(2.75, 2.95)) + 0.15 * progress
                    hr = rng.randint(158, 164) if race else rng.randint(138, 148)
                    s.avg_speed, s.avg_hr, s.max_hr = speed, hr, hr + rng.randint(8, 15)
                    s.ascent_m = s.descent_m = rng.randint(25, 90)
                    s.load = round(mins * (2.2 if race else rng.uniform(1.2, 1.5)), 1)
                    s.trimp = s.load
                    s.vo2max = round(46.6 + 2.2 * progress + rng.uniform(-0.25, 0.25), 1)
                    s.te_aerobic, s.te_anaerobic = round(rng.uniform(2.6, 3.8), 1), round(rng.uniform(0, 1.5), 1)
                elif sport == 'cycling':
                    np_ = rng.uniform(185, 215) + 15 * progress
                    intensity = np_ / 255
                    s.avg_speed, s.avg_hr = rng.uniform(7.3, 8.1), rng.randint(126, 138)
                    s.max_hr = s.avg_hr + rng.randint(15, 25)
                    s.has_power, s.avg_power, s.norm_power = True, round(np_ * 0.93), round(np_)
                    s.intensity_factor = round(intensity, 3)
                    s.tss = round(mins / 60 * intensity ** 2 * 100, 1)
                    s.load = s.tss
                    s.ascent_m = s.descent_m = rng.randint(150, 900) * mins / 120
                    s.vo2max = round(50.5 + 1.8 * progress + rng.uniform(-0.2, 0.2), 1)
                    s.te_aerobic, s.te_anaerobic = round(rng.uniform(2.4, 3.6), 1), round(rng.uniform(0, 1.2), 1)
                elif sport == 'swimming':
                    s.avg_speed, s.avg_hr = rng.uniform(0.88, 0.98) + 0.05 * progress, rng.randint(122, 134)
                    s.max_hr, s.pool_length_m = s.avg_hr + 15, 25.0
                    s.load = round(mins * 0.9, 1)
                    s.te_aerobic, s.te_anaerobic = round(rng.uniform(2.0, 3.0), 1), 0.3
                else:
                    s.avg_hr = rng.randint(104, 116)
                    s.max_hr = s.avg_hr + 30
                    s.load = round(mins * 0.6, 1)
                    s.te_aerobic, s.te_anaerobic = 1.8, round(rng.uniform(0.5, 1.6), 1)
                if s.avg_speed:
                    s.distance_m = s.avg_speed * mins * 60
                    s.max_speed = s.avg_speed * 1.35
                s.calories = round(mins * (11 if sport != 'training' else 6))
                db.session.add(s)

    for i in range(180):
        day = today - timedelta(days=i)
        fit = 1 - i / 365
        db.session.add(DailyHealth(
            user_id=user_id, day=day, resting_hr=round(49 - 3 * fit + rng.uniform(-1.5, 1.5)),
            hrv_avg=round(64 + 8 * fit + rng.uniform(-6, 6), 1), hrv_status='balanced',
            sleep_s=round(rng.uniform(6.6, 8.1) * 3600), sleep_score=rng.randint(68, 90),
            bb_min=rng.randint(10, 30), bb_max=rng.randint(70, 98), stress_avg=rng.randint(20, 34),
            weight_kg=round(70.6 - 0.8 * fit + rng.uniform(-0.3, 0.3), 1) if i % 4 == 0 else None,
            vo2max_run=round(46.6 + 2.2 * fit, 1), training_status='Productive', readiness=rng.randint(48, 88)))
    db.session.add(ProfileSnapshot(file_id=last_file.id, user_id=user_id, at=datetime.combine(today, datetime.min.time()),
                                   weight_kg=70.4, height_m=1.78, resting_hr=47, max_hr=184, threshold_hr=168, ftp=255))
    db.session.commit()
    return n


def _metres(a, b) -> float:
    """Haversine distance between two [lon, lat] points."""
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def privacy_cut(track: dict, metres: float) -> dict:
    """Drop the track's points within `metres` of where it starts and ends, wherever they fall
    in the run (a loop passes its start again). Property arrays are cut the same way."""
    coords = track['geometry']['coordinates']
    if not coords:
        return track
    first, last = coords[0], coords[-1]
    keep = [i for i, c in enumerate(coords) if _metres(c, first) > metres and _metres(c, last) > metres]
    track['geometry']['coordinates'] = [coords[i] for i in keep]
    track['properties'] = {k: [v[i] for i in keep] for k, v in track['properties'].items()}
    return track


DEMO_BADGE = """() => { const b = document.createElement('div'); b.textContent = 'Demo data';
    Object.assign(b.style, {position: 'fixed', top: '60px', right: '16px', zIndex: 5000, padding: '6px 14px',
        background: '#d98e04', color: '#fff', font: '600 15px system-ui, sans-serif', letterSpacing: '.04em',
        borderRadius: '6px', boxShadow: '0 1px 4px rgba(0,0,0,.25)'});
    document.body.appendChild(b); }"""


# The invented year has session summaries but no second-by-second samples, so these Trends
# panels would show as empty. A capture leaves them out; for a real user they hold data.
TRENDS_NEEDS_SAMPLES = ['tr-power-chart', 'tr-best-table', 'tr-eff-chart', 'tr-strength-chart']


def capture_still(browser, base: str, password: str, view: str, out: Path, hide: list | None = None) -> None:
    """A demo-user tab, stamped "Demo data" (top half, so it survives the 16:10 crop)."""
    context = browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT}, locale='en-GB', timezone_id='Europe/Berlin')
    context.request.post(f'{base}/api/auth/login', data={'username': 'demo', 'password': password})
    page = context.new_page()
    page.goto(f'{base}/#{view}', wait_until='networkidle')
    page.wait_for_timeout(1500)                 # let the charts finish drawing
    if hide:
        page.evaluate("(ids) => ids.forEach((id) => { const el = document.getElementById(id); if (el) el.closest('.panel').style.display = 'none'; })", hide)
        page.wait_for_timeout(800)              # the remaining charts resize into the space
    page.evaluate(DEMO_BADGE)
    page.mouse.move(WIDTH - 1, HEIGHT - 1)      # no hover state on anything
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))
    context.close()


def capture_playback(browser, base: str, password: str, session_id: int, out: Path) -> int:
    """Step the scrubber through the real activity and write the frames as an animated WebP.

    The frame shows the playback bar, the map with the time-in-zone bars beside it, and the
    zone-coloured heart-rate graph below. The loop starts a third of the way in, so the first
    frame - the still for reduced-motion viewers - has the marker mid-route and the cursor
    mid-graph."""
    from PIL import Image
    context = browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT}, locale='en-GB', timezone_id='Europe/Berlin')
    context.request.post(f'{base}/api/auth/login', data={'username': 'runner', 'password': password})
    page = context.new_page()

    def cut_track(route):
        resp = route.fetch()
        route.fulfill(response=resp, body=json.dumps(privacy_cut(resp.json(), PRIVACY_M)),
                      headers={**resp.headers, 'content-type': 'application/json'})
    page.route('**/api/sessions/*/track', cut_track)

    page.goto(f'{base}/#detail/{session_id}', wait_until='networkidle')
    page.wait_for_timeout(2000)
    # Bring the map row up under the sticky header and playback bar.
    page.evaluate("""() => { const bar = document.getElementById('pb-bar'), hdr = document.querySelector('header.top');
        const map = document.getElementById('det-map-panel');
        window.scrollTo(0, map.getBoundingClientRect().top + window.scrollY - hdr.offsetHeight - bar.offsetHeight - 22); }""")
    page.wait_for_timeout(600)
    page.mouse.move(WIDTH - 1, HEIGHT - 1)
    t0, t1 = page.eval_on_selector('#pb-scrub', 'e => [Number(e.min), Number(e.max)]')
    frames = []
    for k in range(FRAMES):
        f = (1 / 3 + k / FRAMES) % 1.0
        page.eval_on_selector('#pb-scrub', "(e, t) => { e.value = t; e.dispatchEvent(new Event('input')); }", t0 + f * (t1 - t0))
        page.wait_for_timeout(30)
        frames.append(Image.open(io.BytesIO(page.screenshot())).convert('RGB'))
    context.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=FRAME_MS, loop=0, quality=82, method=4)
    return len(frames)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=RichHelpFormatter)
    parser.add_argument('-af', '--activity-fit', type=Path, help='a real FIT file for the playback animation (not copied anywhere)')
    parser.add_argument('-lg', '--leg', type=int, default=None, help='which leg of a multisport file (default: the first run)')
    parser.add_argument('-te', '--trim-end', type=int, help='end the activity at this many seconds, as fitmon\'s trim would')
    parser.add_argument('-mh', '--max-hr', type=int, help='max HR for the zones of the real activity: the value fitmon uses for that date '
                        '(one file alone may carry none, and then the heart rate is not zone-coloured)')
    parser.add_argument('-on', '--owner-name', default='Mike', help='display name shown on the real activity (default: Mike)')
    parser.add_argument('-sv', '--serve', action='store_true', help='only serve the demo instance until Ctrl+C; capture nothing')
    parser.add_argument('-pt', '--port', type=int, default=0, help='port for the local server (default: any free one)')
    args = parser.parse_args()

    # Before the app is created: plain-HTTP localhost, so the session cookie must not be Secure.
    os.environ['FITMON_INSECURE_COOKIES'] = '1'
    sys.path.insert(0, str(REPO))
    from werkzeug.serving import make_server
    from app import auth, create_app
    from app.models import Session, SessionTrim, db
    from app.services import importer
    from app.settings import save_user_settings

    home = Path(tempfile.mkdtemp(prefix='fitmon-demo-'))
    password = secrets.token_urlsafe(18)              # this process only; never printed or stored
    try:
        app = create_app(home)
        session_id = None
        with app.app_context():
            user = auth.create_user('demo', password, role='user')
            user.display_name = 'Demo athlete'
            n = seed(user.id, date.today(), random.Random(7))
            save_user_settings(home, user.id, {'default_range_days': 365})
            if args.activity_fit:
                runner = auth.create_user('runner', password, role='user', display_name=args.owner_name)
                if args.max_hr:
                    save_user_settings(home, runner.id, {'zones': {'max_hr': args.max_hr}})
                importer.import_bytes(home, runner.id, args.activity_fit.read_bytes(), args.activity_fit.name)
                legs = Session.query.filter_by(user_id=runner.id).order_by(Session.idx).all()
                leg = (next(s for s in legs if s.idx == args.leg) if args.leg is not None
                       else next((s for s in legs if s.sport == 'running'), legs[0]))
                if args.trim_end:
                    db.session.add(SessionTrim(file_id=leg.file_id, idx=leg.idx, user_id=runner.id, end_t=args.trim_end))
                    db.session.commit()
                    importer.reindex_file(home, leg.file)
                session_id = Session.query.filter_by(file_id=leg.file_id, idx=leg.idx).first().id
        console.print(f'[green]Seeded[/green] {n} invented activities' + (f' and {args.activity_fit.name}' if args.activity_fit else ''))

        server = make_server('127.0.0.1', args.port, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        if args.serve:
            console.print(f'Serving the demo instance at [bold]{base}[/bold]; the passwords are random and not shown. Ctrl+C to stop.')
            threading.Event().wait()

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            capture_still(browser, base, password, 'dashboard', DEFAULT_OUT)
            capture_still(browser, base, password, 'trends', TRENDS_OUT, hide=TRENDS_NEEDS_SAMPLES)
            written = [DEFAULT_OUT, TRENDS_OUT]
            if session_id:
                n_frames = capture_playback(browser, base, password, session_id, PLAYBACK_OUT)
                written.append(PLAYBACK_OUT)
                console.print(f'[green]Playback:[/green] {n_frames} frames')
            else:
                console.print('[yellow]No -af given:[/yellow] the playback animation was left as it is.')
            browser.close()
        server.shutdown()
        for path in written:
            console.print(f'[green]Wrote[/green] {path.relative_to(REPO)} ({path.stat().st_size // 1024} KB)')
        console.print('Look at them before committing.')
        return 0
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
