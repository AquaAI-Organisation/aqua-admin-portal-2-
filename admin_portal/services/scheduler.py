"""Platform-independent background scheduler.

Runs the core automation *inside the web process* on a timer, so the mailbox
auto-refreshes, verification emails go out, and new breeder/consultant signups
are reviewed (and auto-approved when the operational toggle is on) without any
external worker, cron, or Heroku Scheduler. Works the same on Heroku, a VPS,
Docker, Render, etc. — wherever the web app runs.

Safe with multiple web workers/instances: each cycle claims a single-row
AutomationLease (one atomic UPDATE) so only one process does the work at a
time (no duplicate sends). This is pooler-safe, unlike a session-level advisory
lock, which leaks through a transaction-mode pooler and can silently stall the
scheduler forever; the lease also self-heals via a TTL if a holder crashes.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.db import connection, models

logger = logging.getLogger(__name__)

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
        finally:
            # Don't hold an idle DB connection while the thread sleeps. Done here
            # (after the lock is released), NOT inside the cycle — closing it mid-cycle
            # invalidated the advisory-lock cursor ("cursor already closed").
            try:
                connection.close()
            except Exception:
                pass
        time.sleep(interval)


def _lease_ttl_seconds() -> int:
    interval = max(30, int(getattr(settings, "INBOX_AUTOREFRESH_INTERVAL", 120)))
    # Lease outlives a cycle (max ~35s of work) with headroom, so a crashed holder
    # is reclaimed after the TTL; short enough that a dead process doesn't stall
    # automation for long.
    return interval * 3


def _claim_lease() -> bool:
    """Elect a single runner via a pooler-safe atomic UPDATE on a lease row.

    Unlike session-level advisory locks (which leak through a transaction-mode
    pooler and can silently stop automation forever), this is one atomic statement
    with no session state, and self-heals via the lease TTL.
    """
    import os, socket
    from django.utils import timezone
    from ..models import AutomationLease

    now = timezone.now()
    until = now + timedelta(seconds=_lease_ttl_seconds())
    holder = f"{socket.gethostname()}:{os.getpid()}"

    AutomationLease.objects.get_or_create(id=1)
    # Claim only if free or expired — a single atomic UPDATE ... WHERE.
    claimed = AutomationLease.objects.filter(
        models.Q(pk=1)
        & (models.Q(locked_until__isnull=True) | models.Q(locked_until__lt=now))
    ).update(locked_until=until, holder=holder)
    return bool(claimed)


def _release_lease() -> None:
    """Expire our lease so the next cycle can claim immediately (best-effort)."""
    try:
        from django.utils import timezone
        from ..models import AutomationLease
        AutomationLease.objects.filter(pk=1).update(locked_until=timezone.now())
    except Exception:
        pass


def _run_cycle_if_leader() -> None:
    """Run one automation cycle, but only if we win the lease (pooler-safe)."""
    if connection.vendor != "postgresql":
        # Single process assumed in local/sqlite dev.
        _do_cycle()
        return

    if not _claim_lease():
        return  # another instance holds the lease for this window
    try:
        _do_cycle()
    finally:
        _release_lease()


def _do_cycle() -> None:
    from .dsar import run_due_login_checks
    from .issue_runner import process_pending_issues
    from .mailbox import fetch_support_inbox
    from .review_runner import process_pending

    try:
        fetch_support_inbox(limit=25)
    except Exception as exc:
        logger.warning("Auto inbox fetch failed: %s", exc)
    try:
        run_due_login_checks()
    except Exception as exc:
        logger.warning("Auto DSAR login check failed: %s", exc)
    # Process new breeder/consultant signups so the "automatic account
    # activation" toggle (and normal AI review) actually applies without an
    # admin clicking "Process now" or an external cron. Time-bounded to keep
    # the web process responsive; the next cycle picks up any remainder.
    try:
        process_pending(limit_per_type=10, max_runtime_seconds=20)
    except Exception as exc:
        logger.warning("Auto account-review pass failed: %s", exc)
    try:
        process_pending_issues(limit_per_type=5, max_runtime_seconds=15)
    except Exception as exc:
        logger.warning("Auto issue-triage pass failed: %s", exc)
    # Certificate renewal / re-verification reminders. Sends are de-duplicated, so
    # this is safe to run each cycle, but throttle the scan to keep it cheap.
    try:
        from django.core.cache import cache

        if cache.add("cert_checks_throttle", "1", 6 * 3600):
            from .certificates import run_certificate_checks

            run_certificate_checks()
    except Exception as exc:
        logger.warning("Auto certificate-reminder pass failed: %s", exc)
