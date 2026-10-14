# What the admin checks when reviewing a new provider account

The human half of provider onboarding: which screens an admin uses, exactly what
information is put in front of them, what they can decide, who is allowed to
decide it, and what trace it leaves.

Companion to `PROVIDER_SIGNUP_VETTING.md`, which covers the automated pipeline.
Every claim is traced to a file and line. Written 14 Oct 2026 against
`claude/lucid-mccarthy-bN6qj`.

---

## 0. The short version

**The admin is not checking the application. They are checking the AI's summary
of the application.**

The review screen shows the company name, a truncated bio, the website URL, and
the model's scores and rationale. It does **not** show the applicant's claimed
certifications, years of experience, specialisation, business address, business
phone, social handles, or any uploaded document — none of those fields are
rendered on any admin screen (§4.2).

And right now there is nothing to review: both intake queues are **empty**,
because auto-activation approves every account within ~2 minutes of signup.

Production to date: **51 reviews, 48 approved, 3 pending, 0 rejected, 0 flagged.**
Of the 17 that an admin touched, **17 were approved. None were ever rejected.**

---

## 1. The three screens

| Screen | URL | View | Purpose |
|---|---|---|---|
| **Pending Intake** | `/intake/` | `views.py:377` | Accounts still awaiting a decision, read from the backend tables directly |
| **AI Reviews** | `/reviews/` | `views.py:584` | Every `AIAccountReview` ever produced, with its decision |
| **Entity Directory** | `/entities/` | `views.py:712` | All providers regardless of state; suspend / re-activate |

### 1.1 Pending Intake — the queue (`views.py:377`)

Two source queries, read live from the shared backend tables:

```python
breeders    = ExternalBreederProfile.objects.filter(is_verified=False)
consultants = ExternalConsultantProfile.objects.filter(admin_status="pending")
```

Filters: role, AI state (`not_scanned` / `pending` / `approved` / `rejected` /
`flagged` / `error`), and a free-text search over company name, email, username
and name. 25 per page, newest first.

> ⚠️ The consultant query matches `admin_status="pending"` only. A consultant in
> `needs_info` or `needs_review` — both valid states the discovery function
> accepts (`review_runner.py:182`) — **never appears in this queue**.

### 1.2 Columns the admin sees in the queue

| Column | Content |
|---|---|
| Created | timestamp |
| Role | breeder / consultant badge |
| Account | display name, email, username |
| Source Status | "Pending verification" / "Pending admin approval" + verified/active flags |
| AI State | decision badge, risk-bucket badge, override badge, confidence to 2dp, and the failure reason if the last scan errored |
| Actions | **Open review** · **Approve** · **Reject** · **Suspend**/**Re-activate** |

`intake_list.html:55–120`. Approve and Reject are one click with no confirmation
on Approve; Reject has a `confirm()` dialog.

---

## 2. What happens on each button

All routed through `intake_decide` (`views.py:537`), which accepts only
`approve`, `reject`, `suspend`, `reactivate`.

| Button | Effect |
|---|---|
| **Approve** | `_ensure_review(...)` creates a stub review if none exists, then `manual_override(review, "approved", "Approved from Pending Intake.", admin)` |
| **Reject** | same path with `"rejected"` |
| **Suspend** | `_set_entity_active_state(activate=False)` (`views.py:2011`) — deactivates profile **and user**, writes `account_status: suspended` plus a timestamped note into metadata |
| **Re-activate** | the same, `activate=True`; for a consultant this also sets `admin_status="approved"` |

`manual_override` (`review_runner.py:350`) records `original_decision`, sets
`manually_overridden=True`, stores the overriding admin and reason, clears any
stale AI error, then applies the real effect — `_approve(...)` or
`_deactivate(..., notify=True)`, which emails the applicant on a rejection.

Both paths write an `AdminAuditLog` row (`views.py:569`) — unlike the automated
pipeline, which writes none.

---

## 3. The decision screen — `review_detail` (`views.py:613`)

What an admin actually reads before deciding. Template: `review_detail.html`.

### 3.1 Header
Display name, role, creation date, decision badge, **risk-bucket badge**,
"manual override" badge if applicable, and a **Re-run AI** button (super-admin only).

### 3.2 Decision summary
- **Rationale** — the model's paragraph, or the auto-activate boilerplate
- **Confidence** — bar plus the raw figure to 3dp
- **Override record** — original decision, who overrode, and why (if applicable)

### 3.3 Review metadata
Role · risk bucket · email · **AI model** · decided-at · created-at.

The `AI model` field is the honest tell: `operational:auto-activate` means no
assessment was performed at all.

### 3.4 Dimension scores
The six scores as progress bars — `identity_clarity`, `business_legitimacy`,
`documentation`, `role_fit`, `trust_risk`, `behavioural_intelligence`.
**Only rendered if `evidence.scores` exists**, so auto-activated accounts show
nothing here.

### 3.5 Balanced policy basis
Policy · operational policy · reason · risk bucket · approve threshold ·
reject threshold · thin evidence (Yes/No) · **hard blocks**, listed in full.

This is the most useful panel on the page — it says *why* the decision came out
as it did, including which hard block fired.

### 3.6 Evidence
`evidence.bullets` if the model returned them; otherwise **a raw JSON dump** of
the evidence object (`review_detail.html:146`).

### 3.7 Action log
`recommended_actions` and `applied_actions`, both as **raw JSON dumps**.

### 3.8 External user record
Name · email · phone · verified · **trust score** · **at risk**.

> In production `trust_score` is `NULL` and `is_at_risk` is `false` for all 177
> users, because the trust engine has never run. These two rows are decoration
> today.

### 3.9 Profile snapshot
Company · **bio (truncated to 400 chars)** · website (clickable, `target=_blank`)
· verification level · active · verified · admin status (consultants only).

### 3.10 Signup review flags
Table of severity, reason (truncated to 140 chars), open/resolved, and a link to
the flag detail page.

### 3.11 Manual approve / reject — super-admins only
Radio: **Approve** / **Reject**. Then a mandatory reason code:

| Value | Label |
|---|---|
| `compliance_verified` | Compliance or documents manually verified |
| `false_positive` | AI false positive |
| `business_confirmed` | Business legitimacy confirmed |
| `policy_violation` | Policy violation confirmed |
| `duplicate_or_test` | Duplicate, spam, or test account |
| `insufficient_context` | AI lacked enough context |
| `other` | free text, min 10 chars |

The panel is hidden once `manually_overridden` is true — **an override cannot be
overridden** from the UI.

---

## 4. What the admin is *not* shown

### 4.1 Absent from the decision screen
Nothing on this page is fetched, resolved or verified. The website is a
hyperlink the admin may click; it is never checked by the system.

### 4.2 The application itself is never rendered 🔴

The applicant submits `certifications[]`, `years_experience`, `specialization`,
`business_address`, `business_phone`, `instagram`, `facebook`, `x`, `tik_tok`,
`linkedin` and `services[]`, all stored in `profile.metadata.application_data`
(`consultant/views.py:1570`).

A search across every admin template finds **no screen that renders
`application_data`, `certifications`, `credentials`, `years_experience` or
`business_address`** — the only matches are outbound email templates and
unrelated pages.

So when an admin approves a consultant, they cannot see:
- the qualification the applicant claims to hold
- how many years they say they have been trading
- their stated specialisation
- their business address or phone
- their social profiles
- any uploaded verification document

They see a company name, ≤400 characters of bio, a URL, and the model's opinion.
**The claimed BSc that makes an applicant look credible is invisible to the
person approving them.**

---

## 5. Who may do what

| Action | Decorator | Guest | Developer | Admin | Super Admin |
|---|---|---|---|---|---|
| View intake queue | `@admin_required` | ✅ | ✅ | ✅ | ✅ |
| View review list / detail | `@admin_required` | ✅ | ✅ | ✅ | ✅ |
| View entity directory | `@admin_required` | ✅ | ✅ | ✅ | ✅ |
| **Approve / Reject from intake** | `@operational_admin_required` | ❌ | ❌ | ✅ | ✅ |
| Suspend / Re-activate | `@operational_admin_required` | ❌ | ❌ | ✅ | ✅ |
| Re-run AI | `@operational_admin_required` | ❌ | ❌ | ✅ | ✅ |
| Trigger a scan (`process_now`) | `@operational_admin_required` | ❌ | ❌ | ✅ | ✅ |
| **Override on the review page** | `@super_admin_required` | ❌ | ❌ | ❌ | ✅ |

Two things follow from this table.

**5.1 The super-admin restriction is bypassable.** 🟠
`review_detail.html:254` tells the admin *"Only Steven and Ben can set the
outcome."* That is true of that form. But `intake_decide` is only
`@operational_admin_required` and calls the identical `manual_override()`
function (`views.py:556`). **Any Admin-role user can approve or reject the same
account from the intake queue.** The restriction is a UI convention on one screen,
not an enforced rule.

**5.2 Guests can read everything.** `@admin_required` admits guest and developer
roles, so read-only accounts can browse provider emails, phone numbers,
addresses and trust data across all three screens. Read-only is probably
intended; worth confirming it is intended for personal data.

---

## 6. What has actually happened in production

```
AI reviews total                     51
  approved                           48
  pending                             3
  rejected                            0
  flagged                             0

by model
  operational:auto-activate          32     ← the toggle; no assessment
  gpt-4o                              8     ← genuine AI reviews
  (blank)                            11     ← stubs created by Approve-from-intake

manually overridden                  17
signup flags raised                   6

INTAKE QUEUE — breeders pending       0
INTAKE QUEUE — consultants pending    0
```

### 6.1 Every override was an approval

All 17 manual overrides resulted in `approved`. **No admin has ever rejected an
account.** Breaking down what was overridden:

| Original decision | Count | What the admin wrote |
|---|---|---|
| `pending` | 9 | "Approved from Pending Intake." |
| `error` | 4 | *"AI made a wrong decision"*, *"its legitimate, just accept it"*, *"i have confirmed that it is a legit breeder"*, *"the account is ok, i will accept it"* |
| `rejected` | 2 | "Compliance or documents manually verified" (confidence 0.12 and 0.22) |
| `flagged` | 1 | "Approved from Pending Intake." (confidence 0.42) |
| `approved` | 1 | "Compliance or documents manually verified" |

The four `error` overrides are the honest ones — the AI failed (the OpenAI key
issue), and an admin made the call themselves.

The two `rejected → approved` overrides are the ones worth discussing. Both were
justified with **"Compliance or documents manually verified"** — but as §4.2
establishes, the screen shows no documents. Whatever verification happened,
it happened outside the system and left no record of what was checked.

### 6.2 Test and junk accounts were approved in production

Among the approved names: `6677777767788887688` (approved with "Compliance or
documents manually verified"), `[AQA-TEST] Farm`, `testuser7696@gmail.com`,
`Ghhh`, `Ben's Company`, `Ben Aqua`, `Aqua`.

These are presumably your own testing. But they are live, approved provider
accounts in the production database, and they are counted in the insurer-facing
provider totals (`PROVIDER_SIGNUP_VETTING.md` §1; the BI dashboards count 17
breeders and 19 consultants).

### 6.3 The queues are empty by construction

Auto-activation sets `is_verified=True` and `admin_status="approved"` within
~2 minutes of signup (`review_runner.py:440`). Both intake queries filter on
exactly those columns. **An account is approved before it can appear in the
queue**, which is why an admin has reviewed nothing since the toggle went on in
June.

---

## 7. Findings

| # | Finding | Severity |
|---|---|---|
| 7.1 | The application data is never rendered on any admin screen. Admins approve without seeing claimed credentials, experience, address or documents. | 🔴 |
| 7.2 | The "only super-admins can decide" rule is enforced on the review form but not on the intake queue, which reaches the same function. | 🟠 |
| 7.3 | Consultants in `needs_info` / `needs_review` never surface in the intake queue. | 🟠 |
| 7.4 | "Compliance or documents manually verified" is offered as a reason code, but the UI exposes no documents to verify. The reason code implies a check the tool cannot support. | 🟠 |
| 7.5 | Both queues are empty while auto-activation is on, so the human review path is dormant and untested in practice. | 🟡 |
| 7.6 | Test accounts are live and approved in production, and counted in provider totals. | 🟡 |
| 7.7 | Guest and developer roles can read all provider personal data. | 🟡 — confirm intended |

---

## 8. Suggested changes

1. **Render the application on the review screen.** `profile.metadata.application_data`
   is already stored — surfacing it is a template change, not a data change. This
   is the single highest-value fix: it turns the review from "do I trust the
   model?" into "does this applicant look real?".
2. **Show uploaded documents** — `user.verification_documents` and, for breeders,
   the `ExternalBreederVerification` row with its licence number, issuing
   authority and expiry.
3. **Decide the permission question** and enforce it in one place: either
   approving is an Admin action (then drop the super-admin gate and the misleading
   caption) or it is super-admin only (then gate `intake_decide` too).
4. **Widen the consultant intake filter** to `{pending, needs_info, needs_review}`
   so nothing can get stranded.
5. **Make the reason codes honest** — drop or rename `compliance_verified` until
   there are documents on the screen to verify.
6. **Clear the test accounts** out of production, or mark them so they are
   excluded from provider counts and insurer reporting.
