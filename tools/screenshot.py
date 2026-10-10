"""Make fitmon's landing-page screenshot (app/static/screenshot.png) from invented data.

    uv run --with playwright python tools/screenshot.py            # writes app/static/screenshot.png
    uv run --with playwright python tools/screenshot.py -sv        # just serve the demo instance to look around

The picture is public - /api/screenshot needs no sign-in and the landing page shows it to
guests - so it must never contain anyone's real activities or health numbers. This builds a
throwaway fitmon home in a temp directory, seeds a demo user with a made-up training year
(a build, a race taper, a rebuild), serves it on localhost, signs in with a password generated
in this process and never shown, and captures the dashboard at 1280x800, en-GB, Europe/Berlin,
with a "Demo data" badge on it so the picture cannot pass for anyone's real dashboard.
Fleet rules: D:\\hw\\pokeflute\\docs\\service-screenshots.md. Re-run when the dashboard changes.
"""
import argparse
import hashlib
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
DEFAULT_OUT = REPO / 'app' / 'static' / 'screenshot.png'
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], formatter_class=RichHelpFormatter)
    parser.add_argument('-o', '--output', type=Path, default=DEFAULT_OUT, help=f'PNG to write (default: {DEFAULT_OUT.relative_to(REPO)})')
    parser.add_argument('-vw', '--view', default='dashboard', help='which tab to capture (default: dashboard)')
    parser.add_argument('-sv', '--serve', action='store_true', help='only serve the demo instance until Ctrl+C; capture nothing')
    parser.add_argument('-pt', '--port', type=int, default=0, help='port for the local server (default: any free one)')
    args = parser.parse_args()

    # Before the app is created: plain-HTTP localhost, so the session cookie must not be Secure.
    os.environ['FITMON_INSECURE_COOKIES'] = '1'
    sys.path.insert(0, str(REPO))
    from werkzeug.serving import make_server
    from app import auth, create_app
    from app.settings import save_user_settings

    home = Path(tempfile.mkdtemp(prefix='fitmon-demo-'))
    password = secrets.token_urlsafe(18)              # this process only; never printed or stored
    try:
        app = create_app(home)
        with app.app_context():
            user = auth.create_user('demo', password, role='user')
            user.display_name = 'Demo athlete'
            n = seed(user.id, date.today(), random.Random(7))
            save_user_settings(home, user.id, {'default_range_days': 365})
        console.print(f'[green]Seeded[/green] {n} invented activities in {home}')

        server = make_server('127.0.0.1', args.port, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        if args.serve:
            console.print(f'Serving the demo instance at [bold]{base}[/bold] - user [bold]demo[/bold]; '
                          'the password is random and not shown, so this is for anonymous pages and the API. Ctrl+C to stop.')
            threading.Event().wait()

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT},
                                          locale='en-GB', timezone_id='Europe/Berlin')
            resp = context.request.post(f'{base}/api/auth/login', data={'username': 'demo', 'password': password})
            if not resp.ok:
                console.print(f'[red]Sign-in to the demo instance failed:[/red] {resp.status} {resp.text()[:200]}')
                return 1
            page = context.new_page()
            page.goto(f'{base}/#{args.view}', wait_until='networkidle')
            page.wait_for_timeout(1500)                 # let the charts finish drawing
            # Say so on the picture itself: on the landing card it could pass for someone's real
            # dashboard. Top half, so it survives the 16:10 crop, over the empty end of the filter bar.
            page.evaluate("""() => { const b = document.createElement('div'); b.textContent = 'Demo data';
                Object.assign(b.style, {position: 'fixed', top: '60px', right: '16px', zIndex: 5000, padding: '6px 14px',
                    background: '#d98e04', color: '#fff', font: '600 15px system-ui, sans-serif', letterSpacing: '.04em',
                    borderRadius: '6px', boxShadow: '0 1px 4px rgba(0,0,0,.25)'});
                document.body.appendChild(b); }""")
            page.mouse.move(WIDTH - 1, HEIGHT - 1)      # no hover state on anything
            args.output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.output))
            browser.close()
        server.shutdown()
        console.print(f'[green]Wrote[/green] {args.output} ({args.output.stat().st_size // 1024} KB). Look at it before committing.')
        return 0
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
