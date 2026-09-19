"""fitmon-import: load .fit / .zip files from disk into one user's index, in-process."""
import argparse
import os

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich_argparse import RichHelpFormatter

console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(prog='fitmon-import', formatter_class=RichHelpFormatter,
                                     description='Import FIT files (or zips of them) for one user.')
    parser.add_argument('paths', nargs='*', help='files to import')
    parser.add_argument('-d', '--dir', action='append', default=[], help='directory to scan recursively (repeatable)')
    parser.add_argument('-u', '--username', required=True, help='owner of the imported files')
    parser.add_argument('-v', '--verbose', action='store_true', help='print every file, not just problems')
    args = parser.parse_args()

    from .. import create_app
    from ..auth import normalize_username
    from ..models import User
    from ..services import importer

    app = create_app()
    with app.app_context():
        user = User.query.filter_by(username=normalize_username(args.username)).first()
        if not user:
            console.print(f'[red]No such user:[/red] {args.username}')
            return 1
        paths = list(args.paths)
        for directory in args.dir:
            if not os.path.isdir(directory):
                console.print(f'[red]Not a directory:[/red] {directory}')
                return 1
            paths += importer.scan_paths(directory)
        if not paths:
            console.print('[yellow]Nothing to import.[/yellow]')
            return 0

        home = app.config['FITMON_HOME']
        counts: dict = {}
        problems = []
        with Progress(TextColumn('[cyan]{task.description}'), BarColumn(), MofNCompleteColumn(),
                      TimeRemainingColumn(), console=console) as progress:
            task = progress.add_task('importing', total=len(paths))
            for path in paths:
                progress.update(task, description=os.path.basename(path)[:40])
                try:
                    with open(path, 'rb') as fh:
                        raw = fh.read()
                    results = list(importer.import_upload(home, user.id, raw, path, source='scan'))
                except OSError as exc:
                    results = [{'status': 'failed', 'name': path, 'reason': str(exc)}]
                for r in results:
                    counts[r['status']] = counts.get(r['status'], 0) + 1
                    if r['status'] in ('failed', 'rejected', 'partial'):
                        problems.append(r)
                    if args.verbose:
                        progress.console.print(f"  {r['status']:10s} {r['name']}")
                progress.advance(task)

        table = Table(title=f'Import summary for {user.username}', header_style='bold cyan')
        table.add_column('Status')
        table.add_column('Files', justify='right')
        for status, n in sorted(counts.items()):
            table.add_row(status, str(n))
        console.print(table)
        for r in problems[:20]:
            console.print(f"  [yellow]{r['status']}[/yellow] {r['name']}: {r.get('reason')}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
