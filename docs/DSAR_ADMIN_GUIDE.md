# Aqua AI — DSAR Admin Guide

How the **Data Subject Access Request (DSAR)** workflow works in the admin
control plane, and how to operate it as an admin or DPO.

> A DSAR is a request from a person ("the data subject") to exercise their
> rights over their personal data under GDPR/UK-GDPR/CCPA. This portal captures,
> verifies, prepares, and fulfils those requests with a full audit trail.
>
> **Code:** `admin_portal/services/dsar.py`, views in `admin_portal/views.py`,
> models `DSARRequest` / `DSAREvent` / `DSARDeliverable`.
> **Queue:** **Data Requests** → `/data-requests/` *(Admin & Super-Admin)*.

---

## 1. Rights handled

The intake auto-classifies a request into one of six types from the subject and
body text (`detect_dsar_request_type`):

| Type | Right | Handling |
|---|---|---|
| **access** | Right of access (Art. 15) | **Automatable** — export prepared & sent |
| **portability** | Data portability (Art. 20) | **Automatable** — machine-readable JSON included |
| **erasure** | Right to be forgotten (Art. 17) | Routed to **manual** handling (`in_progress`) |
| **rectification** | Rectification (Art. 16) | Manual |
| **restriction** | Restriction of processing (Art. 18) | Manual |
| **objection** | Objection to processing (Art. 21) | Manual |

Only **access** and **portability** generate an automatic data package. The
other four are queued for a human to action and are tracked through the same
status/audit machinery.

---

## 2. How a request is created

There are two entry points; both create a `DSARRequest` and an initial
`request_received` event:

1. **Automatically from the Privacy inbox.** When the Support Inbox is refreshed,
   any message in the **privacy** lane is run through
   `ensure_dsar_request_from_inquiry()`. If the text matches a DSAR keyword set,
   a request is created and linked to that inquiry.
2. **Matched to an account by email.** The intake looks up an `ExternalUser` by
   the sender's email:
   - **Match found →** status **`verifying`**, a verification email is sent, and
     a **login baseline** is snapshotted (see §4).
   - **No match →** status **`unmatched`**, logged as `unmatched_request` — an
     admin must confirm identity another way before proceeding.

---

## 3. Statuses & lifecycle

A request moves through these statuses (`DSARRequest.status`):

| Status | Meaning |
|---|---|
| `unmatched` | No platform account confirmed from the request email yet |
| `verifying` | Verification email sent; awaiting the subject's login |
| `verified` | Identity confirmed via a fresh aquaai.uk login |
| `in_progress` | Manual-handling request (erasure/rectification/restriction/objection) queued |
| `awaiting_dpo_approval` | Export prepared; waiting for DPO approval to release |
| `extended` | Deadline extended (+30 days by default) with a recorded reason |
| `fulfilled` | Package emailed to the subject; complete |
| `rejected` | Declined with a recorded reason |
| `withdrawn` | Subject withdrew the request |

```
received ─▶ verifying ─▶ verified ─▶ (prepare) ─▶ awaiting_dpo_approval ─▶ fulfilled
   │            │                                         │
   │            └─(no account)─▶ unmatched                ├─▶ rejected
   │                                                      └─▶ extended ─▶ …
   └─ erasure/rectification/restriction/objection ─▶ in_progress (manual)
```

Every transition writes a `DSAREvent` (action + actor + details), giving you a
complete, timestamped history on the request detail page.

---

## 4. Identity verification — proof by aquaai.uk login

Aqua AI verifies identity by having the requester **log in to their real
aquaai.uk account** — no platform change and no emailed token to click through.
(Full rationale in [`DSAR_LOGIN_VERIFICATION.md`](./DSAR_LOGIN_VERIFICATION.md).)

How it works:

1. On intake, the portal records a **baseline** of the subject's currently-active
   login sessions (`login_baseline_keys`) and emails them a link to
   `PLATFORM_LOGIN_URL` (`https://aquaai.uk/login`), asking them to sign in
   **within 48 hours**.
2. When they log in, Django writes a **new session** to the shared
   `django_session` table.
3. The portal reads `django_session`, and if it sees a **new** session for that
   user (not in the baseline) **within the 48-hour window**, it marks the
   request **login-confirmed** (`login_confirmed_at` set, status → `verified`).

A *fresh* login is required — a session that existed before the request does not
count. Confirmation happens:

- **automatically** when an admin **opens the request** detail page,
- **on demand** via the **Re-check login** button
  (`/data-requests/<id>/recheck-login/`), and
- **in the background** via `python manage.py confirm_dsar_logins` (schedule it
  every few minutes).

> **Auto-send:** if the operational setting `dsar_auto_send` is **on** and the
> request is **access** or **portability**, the moment identity is confirmed the
> portal will **compile and email the package automatically** (an `auto_send`
> event is logged). With auto-send off, it waits for a human (§5–§6).

---

## 5. Preparing the export — what's inside

For **access/portability**, **Prepare** (`/data-requests/<id>/prepare/`,
*super-admin*) calls `build_subject_export()` to assemble everything the shared
application database holds about the subject, then renders two deliverables:

- **`aquaai-data-export.pdf`** — a clean, human-readable PDF (friendly section
  titles and humanised labels; **no table/column names** ever appear).
- **`aquaai-data-export.json`** — the machine-readable copy for the portability
  right.

Both are stored under `runtime_exports/dsar/<request_id>/`, registered as
`DSARDeliverable`s, and **expire after 7 days**. Status moves to
`awaiting_dpo_approval`. The export bundle covers:

| Section | Contents |
|---|---|
| Your account | The subject's user record |
| Your profiles | Breeder and/or consultant profile |
| Trust & regulatory record | Incident logs, trust-score snapshots |
| Payments & support history | Payment-failure logs, refunds, support inbox messages |
| Marketplace & provider activity | Breeder reviews, breeder inquiries, consultant bookings (given + requested) |
| Messages & conversations | The subject's conversations and messages |

**Third-party privacy protection:** counterparty identifiers (the other person
in a conversation, message, inquiry, or booking) are **redacted** — only the
subject's own data is disclosed. A summary block (counts) is included, and a
notes section explains scope and any downstream processors.

> Manual-type requests (erasure/etc.) don't build an export — **Prepare** simply
> moves them to `in_progress` and logs `queued_for_manual_handling` for a human.

---

## 6. Approving & sending (DPO release)

**Approve & send** (`/data-requests/<id>/approve/`, *super-admin / DPO*):

1. If no deliverables exist yet, it prepares them first.
2. Emails the package (PDF + JSON attached) to the verified address (the
   `verification_email`, falling back to the submitted email), from the privacy
   alias.
3. On success: status → **`fulfilled`**, `fulfilled_at` / `dpo_actioned_at` set,
   and the acting DPO recorded; a `fulfilment_sent` event is logged (a failure
   logs `fulfilment_failed` and leaves the request open to retry).

**Downloading deliverables:** admins can fetch the generated files directly via
`/data-requests/deliverables/<deliverable_id>/download/` (e.g. to inspect before
release) while they're within the 7-day window.

---

## 7. Other actions

| Action | Route | Effect |
|---|---|---|
| **Reject** | `/data-requests/<id>/reject/` | Status → `rejected`, reason recorded, `rejected` event *(super-admin)* |
| **Extend** | `/data-requests/<id>/extend/` | `+30 days` to `due_at`, status → `extended`, reason recorded *(super-admin)* — use for technically complex requests |
| **Re-check login** | `/data-requests/<id>/recheck-login/` | Force an identity re-check now *(operational admin)* |
| **View detail** | `/data-requests/<id>/` | Full request, verification state, deliverables, and event log; also auto-rechecks login on open |

---

## 8. Step-by-step: a standard access request

1. **Arrives** — privacy email → inbox refresh auto-creates the DSAR, matched to
   the account → status `verifying`, verification email sent, baseline snapshot
   taken.
2. **Subject logs in** at aquaai.uk within 48h.
3. **Confirmed** — on your next open of the request (or the background job), the
   portal detects the new session → status `verified`.
   - *If auto-send is on,* the package is sent automatically here — you're done.
4. **Prepare** *(super-admin)* — generates the PDF + JSON → `awaiting_dpo_approval`.
5. **Review** — optionally download and check the export.
6. **Approve & send** *(DPO)* — emails the package → `fulfilled`.
7. **Audit** — the event log shows received → verification_sent → login_confirmed
   → export_prepared → fulfilment_sent, each with actor and timestamp.

---

## 9. Configuration & jobs

| Setting | Where | Notes |
|---|---|---|
| `PLATFORM_LOGIN_URL` | env | Login link in the verification email (`https://aquaai.uk/login`) |
| `dsar_auto_send` | Operational Settings | Auto-compile & send access/portability on identity confirmation |
| Privacy alias | Operational Settings | `From` address for verification & fulfilment mail |
| Verification window | code | 48 hours (`verification_expires_at`) |
| Deliverable retention | code | 7 days (`expires_at`) |
| Extension length | code | 30 days default |

**Background jobs**
```bash
python manage.py confirm_dsar_logins   # detect fresh logins, auto-send if enabled
python manage.py poll_inbox            # ingest privacy mail → auto-create DSARs
```
Schedule both every few minutes (cron/worker).

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Request stuck in `verifying` | Subject hasn't logged in, or did so after the 48h window. Re-send/ask them to retry; **Re-check login** after they sign in. |
| "Approve & send" unavailable | Identity not yet confirmed (`login_confirmed_at` empty). |
| Request is `unmatched` | No account matched the request email — verify identity manually before proceeding. |
| Verification email not delivered | Check the privacy Gmail connection and alias under **Settings**; the `verification_sent` event records the delivery result. |
| Deliverable download 404 | The 7-day retention expired — re-**Prepare** to regenerate. |
| Auto-send didn't fire | `dsar_auto_send` is off, or the request type isn't access/portability, or an `auto_send_failed` event was logged — resend manually. |

---

*Companion docs: [Admin User Guide](./ADMIN_USER_GUIDE.md) ·
[DSAR login verification](./DSAR_LOGIN_VERIFICATION.md).*
