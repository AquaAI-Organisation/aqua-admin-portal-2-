# DSAR identity verification via aquaai.uk login

Identity for a data request (DSAR) is proven by the requester **logging in to
their real account at aquaai.uk**. This is detected entirely on the admin-portal
side — **no change to the aquaai.uk platform is required**.

## Flow

1. A privacy request arrives and is matched to a platform account by email.
2. The admin portal records a **baseline** of that user's currently-active login
   sessions, then emails the requester a link to `PLATFORM_LOGIN_URL`
   (`https://aquaai.uk/login`), asking them to sign in within 48 hours.
3. When the user logs in, Django creates a **new session** (new `session_key`)
   in the shared `django_session` table, with their user id inside the signed
   session blob.
4. The admin portal reads `django_session`, decodes the session to read the user
   id, and if it sees a **new session for that user** (not in the baseline)
   within the 48-hour window, it marks the request **login-confirmed**.
5. Only then does the **Approve and send** action unlock for the admin.

Confirmation happens:
- automatically when an admin opens the request,
- on demand via the **Re-check login** button, and
- in the background via `python manage.py confirm_dsar_logins` (schedule this
  every few minutes, e.g. with cron / a worker).

## Why this works without platform changes

- The admin portal already shares the platform database, so it can read
  `django_session` directly.
- Django writes a fresh session row on every successful login (the user id lives
  in the session payload). We only need to read it — we do **not** need the
  platform's `SECRET_KEY`, because we read the row from the trusted shared
  database rather than trusting a value from the browser.

## Configuration

| Variable             | Where        | Example                     |
|----------------------|--------------|-----------------------------|
| `PLATFORM_LOGIN_URL` | admin portal | `https://aquaai.uk/login`   |

## Assumptions / notes

- The platform authenticates users with Django sessions (confirmed: the live
  `django_session` table is the active login store). If the platform later moves
  to a different login mechanism (e.g. stateless JWT with no server-side record),
  this detection would need revisiting.
- A *fresh* login is required — an already-open session from before the request
  does not count, which is the intended behaviour (the requester must actively
  log in to confirm).
- The 48-hour window is the existing DSAR verification window
  (`verification_expires_at`). After it lapses without a login, the request is
  not auto-confirmed and an admin can ask the requester to try again.
