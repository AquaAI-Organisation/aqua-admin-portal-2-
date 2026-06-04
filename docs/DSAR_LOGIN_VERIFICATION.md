# DSAR identity verification via aquaai.uk login — platform integration spec

This document is for whoever maintains the **aquaai.uk platform** (the main user
app — a different codebase from this admin portal).

## Goal

When a user asks for their data (DSAR), we must be sure it is really them before
any data is released. The agreed mechanism:

1. The user receives an email with a secure link.
2. The link sends them to the **real aquaai.uk login**.
3. After they **successfully log in**, aquaai.uk redirects them back to the admin
   portal with a **signed proof** of who logged in.
4. The admin portal records "login confirmed". Only then can an admin send the data.

The admin portal side is already built. The platform only needs to implement the
redirect-back-with-signature described below.

## The link the user receives

The verification email contains:

```
https://aquaai.uk/login?next=<URL-ENCODED CALLBACK>
```

where the (decoded) callback is:

```
https://admin-control.aquaai.uk/admin-portal/data-requests/verify/callback/?token=<TOKEN>
```

`<TOKEN>` is an opaque one-time value. The platform does **not** need to interpret
it — just carry it through to the callback unchanged.

## What the platform must do

1. Treat `?next=` as a post-login redirect target, and allow the admin-portal host
   (`admin-control.aquaai.uk`) as a permitted external redirect for this flow.
   (By default Django/most frameworks block external `next` to prevent open
   redirects — this one host must be allow-listed for this path.)
2. Require the user to authenticate (show the normal login screen).
3. **After a successful login**, do not redirect straight to `next`. Instead,
   build a signed callback URL using the **currently authenticated** user's
   identity (never values taken from the query string) and redirect there:

```
<CALLBACK>&uid=<UID>&email=<EMAIL>&ts=<TS>&sig=<SIG>
```

| Param   | Value                                                              |
|---------|--------------------------------------------------------------------|
| `uid`   | The authenticated user's id — `user_auth_user.id` (UUID)           |
| `email` | The authenticated user's email                                     |
| `ts`    | Current Unix time in **seconds**, UTC                              |
| `sig`   | HMAC-SHA256 signature (see below)                                  |

> Security: `uid`/`email` MUST come from the authenticated session, not from the
> incoming `next`/query. This is what stops a user from confirming on behalf of
> someone else.

## Signature

```
message = f"{token}|{uid}|{email.strip().lower()}|{ts}"
sig     = hex( HMAC_SHA256(secret = DSAR_LOGIN_SIGNING_SECRET, msg = message) )
```

- `token` is the value carried in from the callback URL.
- The shared secret lives in the env var `DSAR_LOGIN_SIGNING_SECRET` and must be
  **identical** on both systems. Generate a long random value, e.g.
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

### Python reference

```python
import hmac, hashlib, time
from urllib.parse import urlencode, urlparse, parse_qs

def signed_callback(callback_url: str, uid: str, email: str, secret: str) -> str:
    token = parse_qs(urlparse(callback_url).query)["token"][0]
    ts = str(int(time.time()))
    msg = f"{token}|{uid}|{email.strip().lower()}|{ts}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    sep = "&" if "?" in callback_url else "?"
    return callback_url + sep + urlencode({"uid": uid, "email": email, "ts": ts, "sig": sig})
```

## What the admin portal does on callback

- Verifies the signature (constant-time) and that `ts` is within
  `DSAR_LOGIN_CALLBACK_MAX_AGE` seconds (default 600) — rejects stale/replayed links.
- Confirms `uid` (or `email`) matches the account the DSAR was filed for — otherwise
  it is rejected as an "account mismatch".
- Marks the request **login-confirmed**. An admin can then release the data; the
  "Approve and send" action is blocked until this confirmation exists.

## Environment variables

Set on **both** systems (same secret):

| Variable                        | Where            | Example                                   |
|---------------------------------|------------------|-------------------------------------------|
| `DSAR_LOGIN_SIGNING_SECRET`     | platform + admin | (long random string, identical on both)   |
| `PLATFORM_LOGIN_URL`            | admin portal     | `https://aquaai.uk/login`                 |
| `LEGACY_ADMIN_REDIRECT_URL`     | admin portal     | `https://admin-control.aquaai.uk`         |
| `DSAR_LOGIN_CALLBACK_MAX_AGE`   | admin portal     | `600` (seconds, optional)                 |

Until the platform side ships, DSAR requests will sit at "Awaiting aquaai.uk
login" and cannot be sent — which is the intended safe default.
