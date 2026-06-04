"""Platform-independent background scheduler.

Runs the inbox/DSAR automation *inside the web process* on a timer, so the
mailbox auto-refreshes and verification emails go out without any external
worker, cron, or Heroku Scheduler. Works the same on Heroku, a VPS, Docker,
Render, etc. — wherever the web app runs.

Safe with multiple web workers/instances: each cycle grabs a PostgreSQL
advisory lock so only one process does the work at a time (no duplicate sends).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

# Arbitrary, app-unique id for the advisory lock that serialises automation runs.
_AUTOMATION_LOCK_ID = 728193
_started = False


def _in_web_server_context() -> bool:
    """True only when running under a long-lived web server (gunicorn/uvicorn/
    runserver), not during one-off management commands like migrate or check."""
    argv = sys.argv or []
    prog = os.path.basename(argv[0]) if argv else ""
    if prog in ("manage.py", "manage", "django-admin"):
        # Only the dev server should host the scheduler when run via manage.py.
        return len(argv) > 1 and argv[1] == "runserver"
    # Launched by gunicorn/uvicorn/daphne/wsgi etc.
    return True


def maybe_start_background_scheduler() -> None:
    """Start the in-process automation loop once, if enabled and in a server context."""
    global _started
    if _started:
        return
    if not getattr(settings, "INBOX_AUTOREFRESH", True):
        return
    if not _in_web_server_context():
        return

    _started = True
    thread = threading.Thread(target=_loop, name="inbox-autorefresh", daemon=True)
    thread.start()
    logger.info("Inbox auto-refresh scheduler started.")


def _loop() -> None:
    interval = max(30, int(getattr(settings, "INBOX_AUTOREFRESH_INTERVAL", 120)))
    # Small initial delay so the app finishes booting before the first run.
    time.sleep(min(interval, 30))
    while True:
        try:
            _run_cycle_if_leader()
        except Exception:
            logger.exception("Inbox auto-refresh cycle failed")
        time.sleep(interval)


def _run_cycle_if_leader() -> None:
    """Run one automation cycle, but only if we win the advisory lock."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [_AUTOMATION_LOCK_ID])
            got_lock = cur.fetchone()[0]
            if not got_lock:
                return  # another process is handling this cycle
            try:
                _do_cycle()
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", [_AUTOMATION_LOCK_ID])
    else:
        # No advisory locks (e.g. sqlite in local dev) — single process assumed.
        _do_cycle()


def _do_cycle() -> None:
    from .dsar import run_due_login_checks
    from .mailbox import fetch_support_inbox

    try:
        fetch_support_inbox(limit=25)
    except Exception as exc:
        logger.warning("Auto inbox fetch failed: %s", exc)
    try:
        run_due_login_checks()
    except Exception as exc:
        logger.warning("Auto DSAR login check failed: %s", exc)
    finally:
        # Release any DB connection this thread opened so it isn't held idle.
        connection.close()
