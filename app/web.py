"""fitmon-web: the web process.

    uv run fitmon-web            web only (run `uv run fitmon-worker` beside it)
    uv run fitmon-web -w         web + job worker in one process (development)
"""
import argparse
import os

from rich_argparse import RichHelpFormatter

from .config import PORT


def main() -> int:
    parser = argparse.ArgumentParser(prog='fitmon-web', description='Run the fitmon web app.',
                                     formatter_class=RichHelpFormatter)
    parser.add_argument('-p', '--port', type=int, default=int(os.environ.get('FITMON_PORT', PORT)))
    parser.add_argument('-H', '--host', default='0.0.0.0', help='0.0.0.0 so the fleet probe can reach /api/ping')
    parser.add_argument('-w', '--with-worker', action='store_true', help='run the job worker as a thread')
    args = parser.parse_args()

    from . import create_app
    app = create_app()

    # With the reloader this module runs twice; only the serving child starts the worker.
    if args.with_worker and os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        from .worker import start_inline
        start_inline(app)

    # Reloader on (a deploy is `git pull`), interactive debugger OFF: the Werkzeug debugger is
    # remote code execution for anyone who can reach the port, and other people can.
    app.run(host=args.host, port=args.port, debug=False, use_reloader=True, threaded=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
