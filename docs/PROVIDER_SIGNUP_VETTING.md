# Provider signup vetting — what is checked, how, and where

How a new **consultant** or **breeder** is assessed between pressing "apply" in
the providers app and their account going live.

Every claim below is traced to a file and line. Written 14 Oct 2026 against
`claude/lucid-mccarthy-bN6qj`.

---

## 0. Read this first — the current production state

**Nothing is being checked today.** `OperationalSettings.auto_activate_new_accounts`
is `true` in production (set 10 Jun 2026). That flag short-circuits the entire
assessment described in this document **before the dossier is built and before
the model is called** (`review_runner.py:202`).

Verified on the most recent signup — Aquatic Elements Ltd, 13 Oct 2026:

| | |
|---|---|
| Applied | 14:21:20 |
| Approved | 14:21:41 (**21 seconds**) |
| Decided by | the toggle |
| `risk_bucket` recorded | `"unreviewed"` |
| `ai_model` recorded | `operational:auto-activate` |
| Entry in `AdminAuditLog` | none |

Sections 1–9 describe what runs **when that toggle is off**. Section 11 lists
three defects that must be fixed *before* turning it off — see §12 for ordering.

---

## 1. The pipeline end to end

```
 PROVIDERS APP
      │  POST application
      ▼
 ① BACKEND: profile created, is_active=False, application_status="pending"
      │     consultant/views.py:1570   breeders/views.py:1210
      │
      │  (shared Postgres — the portal reads the same tables)
      ▼
 ② PORTAL SCHEDULER picks it up, ≤2 min later
      │     scheduler.py:156  →  process_pending(limit_per_type=10, max_runtime_seconds=20)
      ▼
 ③ DISCOVERY: is this profile still pending and not already reviewed?
      │     review_runner.py:152 / :173
      ▼
 ④ ┌── auto_activate_new_accounts == true? ──► APPROVE. No dossier. No model.    ◄── WE ARE HERE
   │       review_runner.py:202
   └── otherwise ▼
 ⑤ DOSSIER assembled from ~60 fields         openai_review.py:156 / :223
      ▼
 ⑥ DETERMINISTIC SIGNALS computed in Python  intelligence_adapter.py:74
      │   → hard_blocks[], thin_evidence
      ▼
 ⑦ LLM SCORES 6 dimensions                   openai_review.py:293 (SYSTEM_PROMPT :18)
      ▼
 ⑧ DETERMINISTIC DECISION                    openai_review.py:446  _balanced_decision
      │   → approved | flagged | rejected
      ▼
 ⑨ OPERATIONAL OVERRIDE                      review_runner.py:101  _operationalise_outcome
      │   → "flagged" is flipped to "approved"  (see §6.3 — this matters)
      ▼
 ⑩ ACTIONS APPLIED to the live tables        review_runner.py:398
      ▼
 ⑪ ONGOING: certificate expiry reminders     certificates.py (every 6h)
```

### Where it runs

| Trigger | Where | Cadence |
|---|---|---|
| In-process scheduler | `scheduler.py:137` `_do_cycle()` | every `INBOX_AUTOREFRESH_INTERVAL` (default **120s**), gated by `INBOX_AUTOREFRESH=true`, lease-locked so only one web dyno runs it |
| Management command | `python manage.py process_pending_reviews --limit 25` | manual / external cron |
| Worker dyno | `python manage.py run_automation --interval 120` | `Procfile`, scaled to 0 by default |

The 120-second cycle is why Aquatic Elements was approved 21 seconds after applying.

---

## 2. Stage ① — what the applicant actually submits

### Consultant — `consultant/views.py:1542`, serializer `consultant/serializers.py:12`

| Field | Required? |
|---|---|
| `company_name` | **yes** |
| `agree_terms` | **yes** (must be true) |
| `agree_guidelines` | **yes** (must be true) |
| `bio`, `website`, `instagram`, `facebook`, `x`, `tik_tok`, `linkedin` | no |
| `business_phone`, `business_address` | no |
| `years_experience` (0–50), `specialization` | no |
| `certifications[]` | no — **free text**, max 100 chars per entry |
| `services[]` | no |

Two real gates exist here:
- **One profile per user** — `consultant/views.py:1531`.
- **Subscription** — `has_feature(user, "become_consultant")`, Premium/Pro only
  (`consultant/views.py:1536`). This is the single strongest filter in the whole
  flow, because it costs money.

The profile is created `is_active=False`, `is_verified=False`, with metadata
`application_status: "pending"`, `requires_admin_approval: True`.

### Breeder — `breeders/views.py:1196`

Same shape and same initial state.

> ⚠️ **Asymmetry:** breeders have **no subscription gate**. `has_feature` appears
> in `breeders/views.py` only for *viewing contacts* (lines 87, 702), never on the
> signup path. Anyone can apply to be a breeder; only paying users can apply to be
> a consultant.

No field is validated against anything external. `website` is a `CharField`, not
a `URLField` — it is never fetched, resolved, or checked for existence.

---

## 3. Stage ③ — discovery

`review_runner.py:143–188`

**Consultants** (`discover_pending_consultants`, `:173`)
- `ExternalConsultantProfile.objects.exclude(admin_status="approved")`
- skip any `subject_id` already in `AIAccountReview` with a decision other than
  `error`/`pending` (`_already_reviewed_ids`, `:143`) — **each account is reviewed
  exactly once, ever**
- only statuses in `{"", pending, under_review, needs_info, needs_review}`

**Breeders** (`discover_pending_breeders`, `:152`)
- `is_verified=False`
- skip if `is_active` **and** `application_status` not in `{"", pending, under_review}`

Scan window is `limit * 4` rows ordered by `-created_at`.

---

## 4. Stage ⑤ — the dossier

`openai_review.py:156` (breeder) / `:223` (consultant). Roughly 60 fields.

**`user` block** — email, name, phone, `is_verified`, `date_joined`,
`verification_documents`, `current_trust_score`, `current_regulatory_tier`,
`is_at_risk`, `badges_count`, `successful_transactions`, `average_rating`,
`consultations_completed`, `avg_response_time_hours`, `last_activity_at`, plus 8
sub-scores (overall, responsibility, community, transaction, consistency, data
stewardship, habitat stability, trading reliability).

**`profile` block** — company_name, bio, website, business_phone,
business_address, rating, reviews_count, verification_level, credentials,
lat/long, booking or sales counters, complaint_count, response times,
inquiries, repeat clients, 5 quality scores, specializations, services_list,
full metadata.

**`intelligence` block** — everything in §5.

> Note: most trust and score fields are `NULL` in production because the trust
> engine has never run (`is_at_risk` is `false` for all 177 users,
> `last_trust_calculation` is `NULL`). The model is told that absence is neutral,
> so this doesn't cause harm — but it does mean roughly a third of the dossier is
> empty for everyone.

---

## 5. Stage ⑥ — deterministic signals (computed in Python, not by the model)

`intelligence_adapter.py:74` `build_signup_intelligence`

### 5.1 Identity — `_identity_signal` (`:168`)

Presence check on four fields: `email`, `name`, `company_name`, `bio`.

Then a **substring scan** over the concatenation of
`email + username + name + company_name + bio`, lowercased, against:

```python
SUSPICIOUS_IDENTITY_TOKENS = ("test", "fake", "spam", "asdf", "qwerty", "demo")
```

Any hit → **hard block** → forced rejection. See §11.1 — this is broken.

### 5.2 Role fit — `_role_fit_signal` (`:197`)

| Role | Fields required |
|---|---|
| Breeder | `service_area`, `specializations` |
| Consultant | `services_list`, `credentials` |

### 5.3 Trust and incidents (`:75–106`)

Latest `ExternalTrustScoreSnapshot`; up to 5 uncleared `ExternalIncidentLog` rows.
**Hard block** if any has `severity_level` in `{critical, high}`.

### 5.4 Messages — `_message_signal_for_user` (`:281`)

Last 30 non-deleted messages across up to 100 conversations, counted against:

```python
INTELLIGENCE_KEYWORDS = {
  "off_platform_contact": ["whatsapp","telegram","text me","email me","dm me","call me"],
  "payment_bypass":       ["bank transfer","paypal friends","cashapp","zelle","crypto","wire"],
  "urgency_pressure":     ["urgent","immediately","right now","asap"],
}
```

**Hard block** if `payment_bypass >= 2`.

### 5.5 Payments — `_payment_signal_for_user` (`:311`, block at `:316`)

Counts `ExternalPaymentFailureLog` for the user. **Hard block** if `>= 5`.

### 5.6 Network — `_network_signal_for_user` (`:324`)

Conversation count and distinct counterparts. Informational only.

### 5.7 Role-specific

**Breeder** — `_review_authenticity_signal` (`:215`): review count, average
rating, verified-purchase count, and reviews containing "whatsapp" / "telegram" /
"bank transfer". `_breeder_inquiry_signal` (`:235`). `_breeder_metrics` (`:262`):
healthy stock rate, mortality rate, disease rate, total/successful sales.

**Consultant** — `_consultant_booking_signal` (`:248`): failed bookings,
bookings rated ≤2, refund-like bookings. `_consultant_metrics` (`:272`):
completion rate, cancellation rate, complaint count, average response time.

### 5.8 Thin evidence (`:111`)

```python
thin_evidence = (identity.missing_required_count >= 3
                 or role_fit.missing_required_count >= 2
                 or (not company_name and not bio))
```

### 5.9 The four hard blocks, in full

| # | Condition | Source |
|---|---|---|
| 1 | Suspicious identity token in email/username/name/company/**bio** | `:187` |
| 2 | ≥2 payment-bypass keyword hits in message history | `:302` |
| 3 | ≥5 payment failures | `:316` |
| 4 | An uncleared incident of severity critical or high | `:106` |

Any one of these forces `decision = "rejected"` at `openai_review.py:476`, and
a hard block cannot be overridden by the operational layer (`review_runner.py:124`).

---

## 6. Stages ⑦–⑨ — scoring and decision

### 6.1 The model — `openai_review.py:293` `call_gpt4`

Prefers the Supabase edge function `signup_review` when
`SUPABASE_FUNCTION_SIGNUP_REVIEW_URL` is set (currently unset → direct OpenAI
call). Model from `OPENAI_MODEL` (`.env` = `gpt-4o`).

Six weighted dimensions (`SYSTEM_PROMPT`, `:18–140`):

| Dimension | Weight | Newcomer baseline |
|---|---|---|
| `identity_clarity` | 0.18 | 0.60–0.80 |
| `business_legitimacy` | 0.20 | 0.55–0.75 |
| `documentation` | 0.18 | 0.55–0.65 — *"a bonus, not a gate"* |
| `role_fit` | 0.14 | 0.55–0.70 |
| `trust_risk` | 0.18 | 0.80–1.00 (higher = safer) |
| `behavioural_intelligence` | 0.12 | 0.80–1.00 (higher = safer) |

`overall_confidence` = the weighted mean. A clean newcomer with nothing lands
~0.70–0.80 by design. A dimension the model omits defaults to `0.6`, not `0.0`
(`_NEUTRAL_SCORE`, `:419`).

The prompt is explicitly **fairness-first**: missing website, documents, reviews
and history are to be scored neutral and *never* penalised. It hunts instead for
a named scam catalogue — off-platform funnelling, payment bypass, impersonation
and plagiarised boilerplate, internal contradictions, unrealistic claims on a
brand-new account, and pre-existing risk.

### 6.2 The decision — `openai_review.py:446` `_balanced_decision`

Thresholds come from settings; **`.env` values win over the code defaults**:

| | `.env` (live) | code default |
|---|---|---|
| `AI_APPROVE_THRESHOLD` | **0.75** | 0.45 |
| `AI_REJECT_THRESHOLD` | **0.35** | 0.10 |

Evaluated in strict order — first match wins:

1. **`hard_blocks` non-empty → `rejected`**
2. `confidence < reject_t` **and** hint is `reject` **and** `clear_reject_signal`
   **and** not `thin_evidence` → `rejected`
3. All of: identity missing ≤1 · (`company_name` or `bio`) present · role missing ≤1 ·
   no critical flags · no hard blocks · **and** (`confidence >= approve_t`
   **or** (hint ≠ reject **and** not thin **and** not clear-reject)) → `approved`
4. `thin_evidence` → `flagged`
5. otherwise → `flagged`

where `clear_reject_signal` = any critical flag **or** `trust_risk < 0.20`
**or** `behavioural_intelligence < 0.20`.

Note the `or` in rule 3: an applicant **below** the 0.75 approve threshold is
still approved as long as the model didn't hint "reject", evidence isn't thin and
there's no clear reject signal. The threshold is a fast path, not a floor.

### 6.3 The operational override — `review_runner.py:101` ⚠️

Risk bucket (`:88`): `rejected` → `critical`; a critical flag → `critical`;
`confidence < 0.35` or a warning flag → `high`; `< 0.60` → `medium`; else `low`.

Then (`:121`):

```python
should_auto_admit = (outcome.decision in {"flagged", "rejected"}
                     and risk_bucket != "critical"
                     and not hard_blocks
                     and outcome.confidence >= 0.09)   # auto_admit_floor
```

Because a `rejected` decision always buckets as `critical`, this clause only ever
fires on `flagged`. Its effect:

> **The "flag for human review" outcome does not reach a human.** Any flagged
> account with confidence ≥ 0.09 and no *critical*-severity flag is silently
> promoted to `approved`, with the rationale prefixed *"Auto-approved with
> follow-up risk monitoring."*

The flag is still persisted and super-admins are still notified
(`_persist_review`, `:239`) — but the account is already live by then. In effect
there are two outcomes, not three: **approved**, or **rejected on a hard block /
critical flag**.

---

## 7. Stage ⑩ — what gets written

### On approve — `_approve` (`:440`)

```
user.is_active = True ; user.is_verified = True ; user.verified_at = now
profile.is_verified = True ; profile.verified_at = now ; profile.is_active = True
consultant only:  admin_status = "approved"
metadata: application_status="approved", approval_date=now, requires_admin_approval=False
```
then `_notify_account_approved` and `_request_certificate_if_missing`.

> The portal writes these columns **directly**. It never calls the backend's
> `ConsultantProfile.approve_application()` (`consultant/models.py:269`), so
> `metadata.approved_at` and `metadata.approved_by` are **never populated**, and
> nothing is written to `AdminAuditLog`. Any report asking "who approved this
> provider" returns empty for every account approved this way.

### On reject — `_deactivate` (`:655`)

`profile.is_active = False`; consultant `admin_status = "rejected"` with the
reason appended to `admin_notes` as `[AI] …`; metadata records the rejection;
a rejection email is sent when `notify=True`.

### Other actions — `_apply_actions` (`:398`)

`set_verification_level` is accepted only from the allowlist
`{none, basic, standard, premium}` (`:395`) — the model cannot invent a level.
`deactivate_pending_docs` deactivates without emailing.

### Staggered mode — `_process_staggered_auto_activate` (`:261`)

When `auto_activate_new_accounts` **and** `auto_activate_stagger_enabled` are both
on, the account is held `pending`, assigned a delay from a schedule, and
auto-approved once the delay elapses — *"so approvals look manual but are fully
automatic"*. Currently `auto_activate_stagger_enabled = false`.

---

## 8. The one real document check — breeder licences

Separate from signup review, and the only place a human inspects a document.

`marketplace_breederverification` / `ExternalBreederVerification`:
`document`, `licence_number`, `issuing_authority`, `awarded_date`,
`expiry_date`, `status`, `rejection_reason`, `reviewed_by_id`, `reviewed_at`.

Reviewed by an admin in Feature D — `admin_portal/views.py:277`
`feature_d_verification_action` → `approve_verification` / `reject_verification`,
**properly audited** via `audit.record_write`.

Two things to understand:
- It is **not required** to become a breeder. A breeder is approved by the flow
  in §6 regardless of whether a licence was ever submitted.
- Production holds **3 rows in total** (2 approved, 1 pending) against 17 breeder
  profiles.

**Consultants have no document review path at all.**

---

## 9. Stage ⑪ — ongoing monitoring

`certificates.py`, run from the scheduler behind a 6-hour cache throttle
(`scheduler.py:168`).

- Certificates **with** an expiry: reminders at **30 days**, **7 days**, and once
  on expiry (`EXPIRY_REMINDER_DAYS = (30, 7)`).
- Certificates **without** an expiry: first renewal nudge once over
  `FIRST_AGE_REMINDER_DAYS = 365` days old.

What counts as "has a certificate" — `_has_certificate` (`review_runner.py:480`):

| Role | Satisfied by |
|---|---|
| Any | `user.verification_documents` non-empty |
| Consultant | **`profile.credentials` non-empty** |
| Breeder | a non-rejected `ExternalBreederVerification` row |

See §11.2.

---

## 10. What is **not** checked — anywhere

Confirmed absent from the codebase (no Companies House, WHOIS, DNS/MX, or
disposable-domain code exists in either repo):

| Not checked | Consequence |
|---|---|
| Company registration / Companies House | "Ltd" in a name proves nothing |
| VAT number | — |
| Government ID / liveness | No identity proofing of any kind |
| **Qualification verified with the issuing body** | "BSc (Hons) Aquaculture" is accepted as typed |
| **Public liability insurance** | Never requested. Consultants enter customers' homes and premises |
| Professional indemnity insurance | Never requested |
| Website exists / resolves / domain age | `website` is stored and shown, never fetched |
| Email domain validity or disposability | The prompt *mentions* disposable domains; nothing implements it — the model guesses from the string |
| Phone ownership (OTP) | — |
| Address verification | — |
| Sanctions / PEP / AML | — |
| DBS / criminal record | — |
| Bank account name match | — |
| Duplicate-account / device / IP correlation | `_network_signal` counts conversations only |
| External reviews (Google, Trustpilot) | — |

**The system assesses plausibility and scam-language, not veracity.** It is a
reasonable coherence detector. It is not identity or credential verification, and
should not be described to an insurer or to Apple as if it were.

---

## 11. Defects found

### 11.1 The identity hard block rejects legitimate aquatics consultants 🔴

`SUSPICIOUS_IDENTITY_TOKENS` is matched as a **plain substring** over a field set
that includes the free-text `bio` (`intelligence_adapter.py:175–187`), and a hard
block forces `decision = "rejected"` (`openai_review.py:476`).

Run against realistic aquatics bios:

```
BLOCKED ['test']  We test water parameters weekly and maintain marine systems.
BLOCKED ['test']  Full water testing and pond maintenance across Surrey.
BLOCKED ['test']  Our greatest strength is bespoke aquarium installation.
BLOCKED ['test']  Latest techniques in reef keeping and coral propagation.
BLOCKED ['demo']  I demonstrate correct feeding routines to new owners.
BLOCKED ['test']  Protestant church pond restoration specialists.
```

Water testing is the single most common activity in the trade. Any applicant who
mentions it is auto-rejected, deactivated and emailed a rejection — with no human
in the loop. `la**test**`, `grea**test**`, `con**test**`, `**demo**nstrate` and
`pro**test**` all collide too.

Currently masked, because auto-activate means the scan never runs.

**Fix:** match on word boundaries, and drop `bio` from the identity-token field
set — a business description is not an identity field.

### 11.2 Self-declared text satisfies the certificate check 🟠

`_has_certificate` returns `True` for a consultant whenever `profile.credentials`
is non-empty (`review_runner.py:484`). `credentials` is free text the applicant
types at signup, capped at 100 characters per entry and never seen by a human.

So typing anything at all marks the provider as "has a certificate on file",
suppresses the certificate-request email, and satisfies the documentation signal.
Aquatic Elements' `"BSC (Hons) Aquaculture and fisheries management"` is exactly
this: self-asserted, unverified, and counted as a credential.

### 11.3 A hard block auto-rejects with no human review 🟠

`_apply_actions` calls `_deactivate(..., notify=True)` on a rejected decision
(`review_runner.py:433`), which deactivates the profile and emails the applicant.
Combined with 11.1 this means a false positive silently loses you a real provider
and sends them a rejection in your name. An automated reject is a heavy action to
take without review.

**Fix:** make hard blocks produce `flagged` and route to the intake queue.

### 11.4 Approvals are invisible to the audit log 🟡

See §7. No `AdminAuditLog` row, and `approved_at` / `approved_by` never set.
There is no answerable record of who or what approved any provider.

---

## 12. Recommended order of work

1. **Fix 11.1** — word-boundary matching, remove `bio` from the identity fields.
   Add a regression test asserting that "we test water parameters" is *not*
   blocked. Small and contained.
2. **Fix 11.3** — hard blocks become `flagged`, not `rejected`.
3. **Decide on 6.3** — either keep the auto-admit promotion (and stop calling it
   a three-tier system) or restore `flagged` as a real queue a human works.
4. **Then** turn `auto_activate_new_accounts` off. Doing this before 1–3 swaps
   "approve everyone unchecked" for "auto-reject a share of good applicants",
   which is the worse failure.
5. **Fix 11.4** — write an `AdminAuditLog` row on every automated decision.
6. **Policy, not code:** decide whether public liability insurance is mandatory
   for consultants. It is the single largest real-world exposure here, it is
   standard for in-home trades, and nothing currently asks for it.
