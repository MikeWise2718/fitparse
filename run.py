#!/usr/bin/env python
"""Convenience entry point: `uv run python run.py -w`. Same as `uv run fitmon-web`."""
from app.web import main

if __name__ == '__main__':
    raise SystemExit(main())
