"""The job worker: `fitmon-worker`. Exactly one should run per FITMON_HOME."""
import argparse
import threading
import time

from rich.console import Console
from rich_argparse import RichHelpFormatter

console = Console()
_inline_started = False


def work_loop(app, poll_seconds: float = 1.0, once: bool = False, stop: threading.Event | None = None) -> int:
    from .services import jobs
    done = 0
    with app.app_context():
        jobs.requeue_interrupted()
        while not (stop and stop.is_set()):
            job = jobs.claim_next()
            if job is None:
                if once:
                    return done
                time.sleep(poll_seconds)
                continue
            jobs.execute(app.config['FITMON_HOME'], job)
            done += 1
    return done


def start_inline(app) -> None:
    """Development convenience: run the worker as a thread of the web process."""
    global _inline_started
    if _inline_started:
        return
    _inline_started = True
    threading.Thread(target=work_loop, args=(app,), name='fitmon-worker', daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(prog='fitmon-worker', description='Run the fitmon job worker.',
                                     formatter_class=RichHelpFormatter)
    parser.add_argument('-1', '--once', action='store_true', help='drain the queue, then exit')
    parser.add_argument('-p', '--poll', type=float, default=1.0, help='seconds between queue polls')
    args = parser.parse_args()

    from . import __version__, create_app
    app = create_app()
    console.print(f'[green]fitmon-worker[/green] v{__version__}  home={app.config["FITMON_HOME"]}')
    try:
        n = work_loop(app, args.poll, args.once)
    except KeyboardInterrupt:
        console.print('[yellow]stopped[/yellow]')
        return 0
    console.print(f'{n} job(s) processed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
