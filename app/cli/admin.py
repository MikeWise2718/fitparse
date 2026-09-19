"""fitmon-admin: bootstrap the first account and recover when a password is lost."""
import argparse
import getpass
import sys

from rich.console import Console
from rich.table import Table
from rich_argparse import RichHelpFormatter

console = Console()


def _password(args) -> str:
    if args.password:
        return args.password
    first = getpass.getpass('Password: ')
    if first != getpass.getpass('Again: '):
        console.print('[red]Passwords do not match.[/red]')
        sys.exit(1)
    return first


def _find(username):
    from ..auth import normalize_username
    from ..models import User
    user = User.query.filter_by(username=normalize_username(username)).first()
    if not user:
        console.print(f'[red]No such user:[/red] {username}')
        sys.exit(1)
    return user


def main() -> int:
    parser = argparse.ArgumentParser(prog='fitmon-admin', description='Manage fitmon accounts.',
                                     formatter_class=RichHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    create = sub.add_parser('create-user', help='create an account', formatter_class=RichHelpFormatter)
    create.add_argument('-u', '--username', required=True)
    create.add_argument('-p', '--password', help='omit to be prompted (preferred)')
    create.add_argument('-n', '--name', help='display name')
    create.add_argument('-a', '--admin', action='store_true', help='grant the admin role')

    reset = sub.add_parser('reset-password', help='set a new password and log the user out everywhere',
                           formatter_class=RichHelpFormatter)
    reset.add_argument('-u', '--username', required=True)
    reset.add_argument('-p', '--password')

    sub.add_parser('list-users', help='list accounts', formatter_class=RichHelpFormatter)

    for name, text in (('disable-user', 'block login'), ('enable-user', 'allow login again')):
        p = sub.add_parser(name, help=text, formatter_class=RichHelpFormatter)
        p.add_argument('-u', '--username', required=True)

    invite = sub.add_parser('invite', help='create a one-time invite link', formatter_class=RichHelpFormatter)
    invite.add_argument('-a', '--admin', action='store_true')
    invite.add_argument('-n', '--note')

    args = parser.parse_args()

    from .. import auth, create_app
    from ..models import User, db
    from ..settings import get_global_settings
    app = create_app()
    with app.app_context():
        if args.command == 'create-user':
            # Check before prompting: nobody should type a password twice to learn the name is taken.
            if User.query.filter_by(username=auth.normalize_username(args.username)).first():
                console.print(f'[red]That username is taken.[/red] To change its password: '
                              f'fitmon-admin reset-password -u {args.username}')
                return 1
            try:
                user = auth.create_user(args.username, _password(args),
                                        role='admin' if args.admin else 'user', display_name=args.name)
            except ValueError as exc:
                console.print(f'[red]{exc}[/red]')
                return 1
            console.print(f'[green]Created[/green] {user.username} (id {user.id}, {user.role})')
        elif args.command == 'reset-password':
            user = _find(args.username)
            password = _password(args)
            problem = auth.validate_new_password(password)
            if problem:
                console.print(f'[red]{problem}[/red]')
                return 1
            user.password_hash = auth.hash_password(password)
            db.session.commit()
            n = auth.revoke_all_tokens(user.id)
            console.print(f'[green]Password reset[/green] for {user.username}; {n} remembered session(s) ended')
        elif args.command == 'list-users':
            table = Table(header_style='bold cyan')
            for col in ('id', 'username', 'role', 'active', 'garmin web', 'last login'):
                table.add_column(col)
            for u in User.query.order_by(User.id).all():
                table.add_row(str(u.id), u.username, u.role, 'yes' if u.is_active else '[red]no[/red]',
                              'on' if u.garmin_web_connect else 'off', str(u.last_login_at or '-'))
            console.print(table)
        elif args.command in ('disable-user', 'enable-user'):
            user = _find(args.username)
            user.is_active = args.command == 'enable-user'
            db.session.commit()
            if not user.is_active:
                auth.revoke_all_tokens(user.id)
            console.print(f'{user.username}: active={user.is_active}')
        elif args.command == 'invite':
            admin = User.query.filter_by(role='admin').order_by(User.id).first()
            _, raw = auth.create_invite(admin.id if admin else None,
                                        role='admin' if args.admin else 'user', note=args.note)
            base = get_global_settings(app.config['FITMON_HOME'])['public_base_url'] or 'http://localhost:8640'
            console.print(f'Invite (valid {auth.INVITE_DAYS} days, single use):\n  {base.rstrip("/")}/invite/{raw}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
