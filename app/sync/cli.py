"""fitmon-sync: connect a Garmin account and pull activities (and health data) for users.

    fitmon-sync login  -u mike          one-time; password from $GARMIN_PASSWORD or a prompt
    fitmon-sync run                     every connected user (what the nightly timer calls)
    fitmon-sync run    -u mike -n 20    one user, 20 most recent activities
    fitmon-sync status
"""
import argparse
import getpass
import os

from rich.console import Console
from rich.table import Table
from rich_argparse import RichHelpFormatter

console = Console()


class ConsoleProgress:
    def update(self, progress, message=None, total=None, force=False):
        if message:
            console.print(f'  [dim]{message}[/dim]')


def _user(username):
    from ..auth import normalize_username
    from ..models import User
    return User.query.filter_by(username=normalize_username(username)).first()


def cmd_login(app, args) -> int:
    from . import activities, client as gc
    from ..models import db, utcnow
    user = _user(args.username)
    if not user:
        console.print(f'[red]No such user:[/red] {args.username}')
        return 1
    email = args.email or os.getenv('GARMIN_EMAIL') or console.input('Garmin Connect email: ')
    password = os.getenv('GARMIN_PASSWORD') or getpass.getpass('Garmin Connect password: ')
    home = app.config['FITMON_HOME']
    try:
        try:
            gc.login_with_password(home, user.id, email, password)
        except gc.MfaRequired:
            gc.complete_mfa(home, user.id, console.input('MFA code: ').strip())
    except Exception as exc:
        console.print(f'[red]Login failed:[/red] {type(exc).__name__}: {exc}')
        text = str(exc).lower()
        if isinstance(exc, gc.SyncRateLimited) or '429' in text or 'exhausted' in text:
            # One `login` is up to five sign-in attempts (the library walks a chain of routes).
            # Retrying straight away only deepens an IP rate limit.
            console.print()
            console.print("[yellow]Do not retry right away[/yellow] - each run is several sign-in attempts "
                          "and Garmin's limit is per IP; give it an hour or more.")
            console.print('Meanwhile: check the password by signing in at connect.garmin.com in a browser '
                          '(that also shows whether Garmin wants a captcha or a code).')
            console.print("The sync is optional: Garmin's own export (garmin.com/account/datamanagement -> "
                          'Export Your Data) gives a zip you can drop on the Import tab.')
        return 1
    acct = activities.account(user.id)
    acct.status, acct.connected_at, acct.last_error = 'ok', utcnow(), None
    db.session.commit()
    console.print(f'[green]Connected[/green] Garmin for {user.username}; tokens stored encrypted.')
    return 0


def cmd_run(app, args) -> int:
    from . import activities, client as gc, health
    from ..models import User
    home = app.config['FITMON_HOME']
    users = [_user(args.username)] if args.username else User.query.filter_by(is_active=True).order_by(User.id).all()
    users = [u for u in users if u and gc.has_tokens(home, u.id)]
    if not users:
        console.print('[yellow]No connected users.[/yellow]')
        return 0
    if args.dry_run:
        for u in users:
            console.print(f'would sync {u.username}')
        return 0
    if args.queue:
        # What the nightly timer uses: hand the work to the job worker (the single bulk
        # writer) instead of running a second writer beside it.
        from ..services import jobs
        for u in users:
            job = jobs.enqueue(u.id, 'sync', {'full': args.full, 'health': args.health,
                                              'limit': args.limit}, unique=True)
            console.print(f'queued sync for {u.username} (job {job.id})')
        return 0
    progress = ConsoleProgress() if args.verbose else type('Quiet', (), {'update': lambda *a, **k: None})()
    table = Table(title='Sync summary', header_style='bold cyan')
    for col in ('user', 'listed', 'imported', 'duplicate', 'replaced', 'no original', 'failed', 'health days', 'stopped'):
        table.add_column(col)
    code = 0
    for u in users:
        console.print(f'[cyan]{u.username}[/cyan]')
        res = activities.sync_user(home, u.id, progress, full=args.full, limit=args.limit, delay=args.delay)
        days = '-'
        if args.health and not res['stopped']:
            hres = health.sync_user(home, u.id, progress, delay=args.delay)
            days, res['stopped'] = str(hres['days']), hres['stopped']
        table.add_row(u.username, *(str(res[k]) for k in ('listed', 'imported', 'duplicate', 'replaced',
                                                         'no_original', 'failed')), days, res['stopped'] or '-')
        if res['stopped'] in ('rate_limited', 'budget'):
            # One source IP for every account: stop the whole run, not just this user.
            console.print(f"[red]{res['stopped']}[/red] - ending the run; the next one resumes.")
            code = 2
            break
    console.print(table)
    return code


def cmd_status(app, args) -> int:
    from . import client as gc
    from ..models import GarminAccount, GarminActivity, User, db
    from sqlalchemy import func
    home = app.config['FITMON_HOME']
    table = Table(header_style='bold cyan')
    for col in ('user', 'auth', 'tokens', 'last sync', 'back-fill', 'activities by status'):
        table.add_column(col)
    for u in User.query.order_by(User.id).all():
        acct = db.session.get(GarminAccount, u.id)
        counts = dict(db.session.query(GarminActivity.status, func.count())
                      .filter(GarminActivity.user_id == u.id).group_by(GarminActivity.status).all())
        table.add_row(u.username, acct.status if acct else '-', 'yes' if gc.has_tokens(home, u.id) else 'no',
                      str(acct.last_sync_at or '-') if acct else '-',
                      'done' if acct and acct.backfill_done else 'pending',
                      ', '.join(f'{k}={v}' for k, v in sorted(counts.items())) or '-')
    console.print(table)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog='fitmon-sync', description='Garmin Connect sync for fitmon.',
                                     formatter_class=RichHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    login = sub.add_parser('login', help='connect a Garmin account', formatter_class=RichHelpFormatter)
    login.add_argument('-u', '--username', required=True, help='fitmon user')
    login.add_argument('-e', '--email', help='Garmin email (else $GARMIN_EMAIL or a prompt)')
    run = sub.add_parser('run', help='sync activities (and health)', formatter_class=RichHelpFormatter)
    run.add_argument('-u', '--username', help='only this user (default: every connected user)')
    run.add_argument('-n', '--limit', type=int, help='only the N most recent activities')
    run.add_argument('-f', '--full', action='store_true', help='page the whole account, not just new activity')
    run.add_argument('-d', '--delay', type=float, help='seconds between requests (default: global setting)')
    run.add_argument('-hl', '--health', action='store_true', help='also pull daily health metrics')
    run.add_argument('-q', '--queue', action='store_true', help='enqueue for the job worker instead of running here')
    run.add_argument('-y', '--dry-run', action='store_true', help='show who would be synced, then stop')
    run.add_argument('-v', '--verbose', action='store_true')
    sub.add_parser('status', help='auth state and counts per user', formatter_class=RichHelpFormatter)
    args = parser.parse_args()

    from .. import create_app
    app = create_app()
    with app.app_context():
        return {'login': cmd_login, 'run': cmd_run, 'status': cmd_status}[args.command](app, args)


if __name__ == '__main__':
    raise SystemExit(main())
