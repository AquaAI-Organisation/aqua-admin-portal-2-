# Running the mailbox + DSAR automation on a normal server

The privacy/DSAR pipeline is fully automatic once a poller is running. A poller
fetches the support/privacy mailbox, auto-analyses privacy mail, creates DSAR
requests, sends verification links, and delivers the data export after the
requester logs in at aquaai.uk — no "Refresh inbox" / "Analyse email" clicks.

Pick **one** of these (don't run two pollers at once):

## Option A — in-process (zero extra setup)
Keep `INBOX_AUTOREFRESH=true` (the default). As long as the app is served by
gunicorn/uvicorn **without `--preload`** (so the scheduler starts post-fork in
each worker), the mailbox is polled every `INBOX_AUTOREFRESH_INTERVAL` seconds
(default 120). A Postgres advisory lock ensures only one worker runs each cycle.

If you're still having to click "Refresh inbox", verify on the running server:
- the deployed code includes this automation (rebuild/redeploy the latest branch), and
- `INBOX_AUTOREFRESH` is **not** set to `false`.

## Option B — dedicated worker (recommended for a plain server)
Run the loop as its own always-on process and set `INBOX_AUTOREFRESH=false` on
the web app.

- **systemd:** see [`aqua-automation.service`](./aqua-automation.service).
- **Docker Compose:** see [`automation-worker.compose.yml`](./automation-worker.compose.yml).
- **cron / external scheduler:** run a single pass on a timer instead of a loop:
  `python manage.py run_automation --once` (e.g. every 2–5 minutes).

Confirm it's working: send a test privacy email to the mailbox and watch a DSAR
request appear within one interval, with the verification email sent — no manual
clicks.
