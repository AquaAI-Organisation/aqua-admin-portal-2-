# AquaAI Gap-Doc Review and Backend Gap Map

## Executive Summary

I reviewed the full `C:\Users\bibe\Downloads\Aquaai-gap-doc` folder against the backend checkout at `C:\Users\bibe\Downloads\backend_aqua_ai-1 2\backend_aqua_ai-1`.

The document set is internally coherent, but the checked-out backend is **not** the same implementation baseline assumed by the documents.

The strongest finding is not one of the named Phase 5 gaps. It is a repo-baseline mismatch:

- The documents assume a `s3_optimized` branch with prior gap merges already landed.
- The current checkout is on `s3_optimized`, but its recent history (`16d8e60`, `6fed8b8`, `a603be9`) does not resemble the remediation sequence described in the handovers.
- Key documented surfaces do not exist in this tree at all: `core/services/legacy_client.py`, the `intelligence/` app, and `providers_chat/`.
- Several earlier trust and audit fixes documented as completed are also absent from this tree.

That means the Phase 5 plan in the docs is directionally sound, but it is **not directly executable against this checkout** until the source-of-truth branch/repo state is reconciled.

## Document Inventory

### Canonical anchors

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`
- `AquaAI_Phase5_Spec_v2.md`
- `AquaAI_Backend_Gap_Remediation_Brief_v1 (1).docx`
- `AquaAI_Handover.docx`
- `AquaAI_Unified_Technical_Documentation_v2.docx`

These are the primary sources for architecture intent, historical remediation status, and the active Phase 5 intelligence/trust work.

### Historical remediation packages

- `phase1 (1).zip`
- `gap5.zip`
- `batch_A.zip`
- `batch_B.zip`
- `batch_C_v2.zip`
- `batch_D.zip`
- `batch_D_part3.zip`
- `gap-3-followup.zip`
- `gap-8-v2.zip`
- `phase4_bundle.zip`
- `phase4_bundle_v2.zip`
- `gap_12_a_INSTRUCTIONS_FOR_TUSHAR.md`
- `gap_12_b_INSTRUCTIONS_FOR_TUSHAR.md`
- `gap_12_b_django_patch.md`
- `gap_12_a_role_split.sql`
- `gap_12_b_audit_mutation_utility.py`
- `test_gap_12_a_role_split.py`
- `test_gap_12_b_immutability.py`

These are structured implementation artifacts: patch instructions, migrations, test files, and deployment notes. They are useful as evidence of intended fixes, but many of their expected code changes are not present in the current checkout.

### Active Phase 5 / intelligence-trust sources

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`
- `AquaAI_Phase5_Spec_v2.md`
- `inteliigence gaps.pages`
- `inteliigence gaps (1).pages`
- `inteliigence gaps (2).pages`

The two markdown files are the strongest sources here. The `.pages` files appear secondary and were not needed to explain any unique gap that was not already captured in the markdown anchors.

### Adjacent but mostly out-of-scope packages

- `batch_E1_*`, `batch_E2_*`, `batch_E3_*`, `batch_E4_*`
- `defect_Q_user_none_guard_referral_code.md`
- `defect_R_user_none_guard_breeder_subscription.md`
- `STRIPE_DASHBOARD_INSTRUCTIONS.md`
- `batchd_part3_test_apiclient_fix_patch.md`
- `batchd_part3_test_cap_patch_target_fix.md`
- `gap4_enhancement_test_url_fix_patch.md`
- `gap9_amendment_user_id_metadata_patch.md`

These focus mostly on breeder subscriptions, promo handling, lifecycle webhooks, and Stripe plumbing. They are adjacent to platform trust and entitlement history, but they do not materially change the Phase 5 intelligence/trust map.

## Deduplicated Gap Map

### Meta-gap 0 - Source-of-truth branch mismatch

Severity: Critical

Source docs:

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`
- `AquaAI_Handover.docx`
- `batch_A.zip`
- `batch_B.zip`
- `phase1 (1).zip`

Current backend touchpoints:

- Git branch `s3_optimized`
- `badges/`, `core/`, `user_auth/`, `breeders/`, `marketplace/`

What is already present:

- A local badge and trust system exists.
- User trust fields exist in `user_auth/models.py`.
- Some badge audit/trust snapshot models exist.

What is missing or disconnected:

- The current checkout lacks the documented intelligence client and apps.
- The current checkout also lacks multiple earlier remediations that the documents treat as already merged.

Evidence:

- No `core/services/legacy_client.py` exists under `core/services/`.
- No `intelligence/` directory exists in the repo root.
- No `providers_chat/` directory exists in the repo root.
- No `tests/` directory exists, despite historical docs treating it as established by Gap 5.
- No launch-pricing fields exist in `user_auth/models.py` or `breeders/models.py`.
- `badges/migrations/` only contains `0001` to `0003`, not the later audit/integrity migrations described in Phase 4.

Recommended approach:

- Resolve this first.
- Confirm whether this is the intended backend baseline or whether the wrong local checkout was used.
- Do not implement Phase 5 against this tree until that reconciliation is done.

### Gap A - Intelligence integration never validated end-to-end

Severity: Critical

Source docs:

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`, sections 5.1 to 5.7
- `AquaAI_Phase5_Spec_v2.md`, Gap A and Stream 1

Current backend touchpoints:

- `core/services/`
- `ai_models/services/openai/`
- `chatbot/`
- `consultant/`
- `breeders/`
- `monitoring/`

What is already present:

- AI-facing code exists locally, but it is mostly direct OpenAI logic.
- `ai_models/services/openai/openai_recommendations.py` directly uses `OpenAI()`.
- `ai_models/services/openai/care_task_generator.py` is local deterministic logic, not an external intelligence call.

What is missing or disconnected:

- The documented intelligence client (`legacy_client`) is absent.
- The documented read/proxy surfaces (`intelligence/views.py`, `intelligence/admin_dashboard.py`) are absent.
- The documented env wiring for `AQUA_INTELLIGENCE_*` is absent from `aquaai/settings.py`.

Dependency order:

- Blocked by Meta-gap 0.

Recommended approach:

- Reconcile the correct baseline first.
- If this checkout is correct, then Phase 5 Stream 1 becomes a rebuild task, not a validation task.

### Gap B - Badge/trust pipeline is disconnected from intelligence enrichment

Severity: Critical

Source docs:

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`, gaps table
- `AquaAI_Phase5_Spec_v2.md`, Gap B and Streams 2/3

Current backend touchpoints:

- `badges/services/badge_trigger.py`
- `badges/services/trust_calculator.py`
- `badges/services/badge_evaluator.py`
- `ai_models/views/inference_views.py`
- `breeders/models.py`
- `consultant/models.py`
- `monitoring/models.py`

What is already present:

- Badge-trigger entrypoints exist across the product.
- `BadgeTriggerService` is invoked from user, breeder, consultant, monitoring, and AI-model flows.
- User trust state is written to `user.current_trust_score` and `user.overall_score`.

What is missing or disconnected:

- There is no intelligence modifier call anywhere in `badges/`.
- There is no `legacy_client` or equivalent client wrapper in this checkout.
- There is no signal write to an intelligence-side learning table.

Dependency order:

- After Meta-gap 0.
- After Gap C/D local trust-source consolidation.

Recommended approach:

- First establish one authoritative local trust path.
- Then layer the intelligence modifier into that path.
- Keep user-facing badge flows resilient if the external intelligence side is unavailable.

### Gap C - The local trust calculation path is internally split and partially outdated

Severity: High

Source docs:

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`, Gap C
- `AquaAI_Phase5_Spec_v2.md`, Gap C and Stream 2A
- `batch_A.zip`, Gap 1
- `batch_B.zip`, Gap 2

Current backend touchpoints:

- `badges/services/trust_calculator.py`
- `badges/services/badge_evaluator.py`
- `badges/services/badge_trigger.py`
- `badges/views.py`

What is already present:

- `BadgeEvaluatorService.calculate_trust_score()` includes incident penalties and tier gates.
- `TrustScoreCalculatorService.calculate_trust_score()` exists as a second implementation.

What is missing or disconnected:

- `TrustScoreCalculatorService` does not call `_calculate_incident_penalties()`.
- `TrustScoreCalculatorService._determine_tier()` does not use the gate-check helpers.
- `BadgeEvaluatorService.validate_and_update_badges()` still delegates to the outdated `TrustScoreCalculatorService`.
- `BadgeTriggerService` also calls the outdated `TrustScoreCalculatorService`.

Why this matters:

- The repo has two local trust calculators with different logic.
- The more complete one is not the one the event pipeline relies on.

Recommended approach:

- Collapse to one local trust implementation before any intelligence modifier work.
- Either promote `TrustScoreCalculatorService` to parity with `BadgeEvaluatorService`, or delete the duplication and route all writes through one service.

### Gap D - No canonical local trust source exists, even before adding intelligence

Severity: High

Source docs:

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`, Gap D
- `AquaAI_Phase5_Spec_v2.md`, Gap D and Stream 2B

Current backend touchpoints:

- `badges/services/trust_calculator.py`
- `badges/services/badge_evaluator.py`
- `badges/models.py` (`TrustScoreSnapshot`)
- `user_auth/models.py`
- `breeders/models.py`

What is already present:

- `user.current_trust_score`
- `user.overall_score`
- `TrustScoreSnapshot`
- `BreederProfile.local_trust_score`

What is missing or disconnected:

- No single service is clearly authoritative for local user trust.
- `BreederProfile.local_trust_score` introduces a second, unrelated trust concept at provider level.
- `TrustScoreSnapshot` has no fields for `local_baseline` or `intelligence_modifier`, so the hybrid design cannot be expressed transparently yet.

Recommended approach:

- Define one authoritative local user-trust service.
- Add explicit snapshot fields for baseline and modifier only after the authoritative path is chosen.
- Keep breeder-local scoring clearly separate from user trust in naming and API responses.

### Gap E - No mutation-event signal is pushed to intelligence

Severity: High

Source docs:

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`, Gap E
- `AquaAI_Phase5_Spec_v2.md`, Gap E and Stream 3

Current backend touchpoints:

- `badges/services/badge_trigger.py`
- `badges/models.py` (`UserActivity`)
- product event emitters in `breeders/models.py`, `consultant/models.py`, `monitoring/models.py`, `ai_models/views/inference_views.py`

What is already present:

- User-facing events create `UserActivity` rows.
- Badge evaluation is triggered for many domain events.

What is missing or disconnected:

- No `ai_learning_signals` write path exists.
- No `submit_feedback` path exists in this checkout.
- No intelligence notification wrapper exists.

Recommended approach:

- After Meta-gap 0, decide whether the backend should write directly to the shared database or through a dedicated intelligence endpoint.
- If the repo truly owns the shared Postgres instance, a direct additive write is the simplest first implementation.
- Make the write asynchronous and non-blocking.

### Gap F - JWT/service-token lifecycle is not just static; it is absent from this checkout

Severity: Medium

Source docs:

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`, Gap F
- `AquaAI_Phase5_Spec_v2.md`, Gap F

Current backend touchpoints:

- `aquaai/settings.py`
- `core/services/`

What is already present:

- No confirmed intelligence-auth config or token client was found in this checkout.

What is missing or disconnected:

- No `AQUA_INTELLIGENCE_URL`
- No `AQUA_INTELLIGENCE_TOKEN`
- No `AQUA_INTELLIGENCE_TIMEOUT`
- No `POST /api/auth/service-token` client wrapper

Recommended approach:

- Treat this as a second-order problem.
- First restore or locate the intelligence client baseline.
- Then decide whether to keep static JWT temporarily or implement rotation.

### Gap G - Schema mismatch is real, but the document's suggested patch does not cleanly fit this checkout

Severity: High

Source docs:

- `AquaAI_Handover_Phase4_Phase5_29April2026.md`, Gap G
- `AquaAI_Phase5_Spec_v2.md`, Gap G and Stream 5B

Current backend touchpoints:

- `breeders/models.py`
- `consultant/models.py`
- `marketplace/models.py`

What is already present:

- `BreederReview`, `ConsultantRating`, and `SellerReview` exist as separate review tables.
- `MarketplaceListing` exists.
- `BreederStock.species_name` exists.

What is missing or disconnected:

- There is no unified `reviews_review` model or table in the Django code.
- `MarketplaceListing` has no `species_name`.
- The doc-recommended fallback assumes `marketplace_marketplacelisting` can reach species data through `breeder_stock_id`, but this checkout's `MarketplaceListing` model has no `breeder_stock` link at all.

Recommended approach:

- The review-side diagnosis is valid: intelligence code should not assume `reviews_review`.
- But the exact SQL/view patch in the Phase 5 spec must be re-derived from the actual deployed schema before implementation.
- A synthetic `reviews_review` view still looks feasible.
- The `marketplace_listing` compatibility layer needs a fresh design because the documented FK path is not visible in this checkout.

### Historical gaps documented as complete but absent in this checkout

Severity: High

This is not a single new feature gap. It is a documentation-to-code discrepancy that will distort every later remediation step if ignored.

Examples:

- `phase1 (1).zip` says launch-pricing fields were foundational. They are absent from `user_auth/models.py` and `breeders/models.py`.
- `batch_A.zip` Gap 1 says incident penalties should be wired into `badges/services/trust_calculator.py`. They are not.
- `batch_B.zip` Gap 2 says tier gates should be wired into `badges/services/trust_calculator.py`. They are not.
- `batch_B.zip` Gap 3 says `BadgeAuditLog.save()` should backfill `evidence_hash`. It does not.
- `phase4_bundle_v2.zip` documents Gap 13/14 integrity and reconciliation work, but the matching migrations/files are not visible in this checkout.

Recommended approach:

- Treat these as branch-state discrepancies, not as optional cleanup.
- Confirm whether the intended implementation target is another branch, another clone, or an unmerged local state.

## Current Code Areas Mapped

### Trust calculation and snapshots

- `badges/services/trust_calculator.py`
- `badges/services/badge_evaluator.py`
- `badges/models.py` (`TrustScoreSnapshot`)
- `user_auth/models.py` (`current_trust_score`, `overall_score`)

### Badge evaluation and event entrypoints

- `badges/services/badge_trigger.py`
- `ai_models/views/inference_views.py`
- `breeders/models.py`
- `consultant/models.py`
- `monitoring/models.py`

### Review and segmentation-related schema surfaces

- `breeders/models.py` (`BreederReview`, `BreederStock`)
- `consultant/models.py` (`ConsultantRating`, `ConsultantBooking`)
- `marketplace/models.py` (`MarketplaceListing`, `SellerReview`)

### Observability quality

- `ai_models/services/openai/openai_recommendations.py`
- `breeders/models.py`
- `consultant/models.py`

The current codebase still contains many `print()`-based operational paths, which matches the Phase 5 concern about weak observability, even though the specific `legacy_client.py` file is absent here.

## Decisions Still Open

These decisions remain valid from the Phase 5 spec, but they are currently gated by the repo-baseline mismatch.

### Decision 1 - Sync vs async modifier call

Recommendation: async

Reason:

- The local trust path already has internal complexity.
- Blocking user-facing badge events on an external dependency is unnecessary.

### Decision 2 - How to push learning signals

Recommendation: direct additive database write, if the shared Supabase/Postgres ownership model is real

Reason:

- This checkout has no intelligence endpoint wrapper.
- A direct write is the least coupled first step once the correct baseline is confirmed.

### Decision 3 - Schema mismatch handling

Recommendation: compatibility views, but re-derived from the actual live schema

Reason:

- The `reviews_review` compatibility view still makes sense.
- The `marketplace_listing` compatibility patch in the spec assumes fields and relationships that are not visible in this checkout.

### Decision 4 - Phase 5.5 timing

Recommendation: still defer

Reason:

- JWT rotation and frontend cutover should not happen before baseline reconciliation and local trust-path consolidation.

## Prioritized Remediation Sequence

### 0. Reconcile the implementation target

- Verify whether `C:\Users\bibe\Downloads\backend_aqua_ai-1 2\backend_aqua_ai-1` is the real source of truth.
- If not, switch to the correct clone/branch before doing anything else.
- If yes, treat the Phase 5 docs as architecture intent, not as a literal patch plan.

### 1. Restore historical local trust/audit baseline

- Apply or port the missing historical trust fixes first.
- Minimum baseline:
  - incident penalties wired into `TrustScoreCalculatorService`
  - tier gates wired into `TrustScoreCalculatorService`
  - `BadgeAuditLog.save()` self-healing hash logic

### 2. Collapse duplicate local trust logic

- Pick one service as the authoritative local trust calculator.
- Route `BadgeTriggerService` and badge-update flows through it.
- Keep `TrustScoreSnapshot` generation in that same path.

### 3. Reconfirm actual intelligence integration surfaces

- If the intended intelligence client exists elsewhere, merge or restore it.
- If not, define the smallest viable client surface needed for modifier lookup and feedback/signal writes.

### 4. Implement hybrid trust layering

- Add explicit baseline/modifier representation in snapshots.
- Apply external modifier after local baseline only.
- Degrade safely to local-only when the intelligence side is unavailable.

### 5. Add mutation-event push

- Emit signal writes after successful badge/user-activity processing.
- Keep failures logged-only and non-blocking.

### 6. Rebuild schema compatibility from the actual database model

- Create review compatibility views if the intelligence side still expects them.
- Redesign marketplace/species compatibility against the real listing schema, not the doc's assumed FK path.

### 7. Add observability and end-to-end verification

- Replace `print()`-style error handling on critical external paths.
- Log endpoint, status, latency, fallback, and payload size for all intelligence calls.
- Only then run the doc's Stream 1/4 style validation.

### 8. Defer JWT rotation and frontend cutover

- Leave these for Phase 5.5 after the backend path is coherent.

## Bottom Line

The docs describe a sensible end-state:

- one local badge-based baseline
- one intelligence-layer modifier
- additive learning-signal writes
- schema compatibility on the Aqua AI side

But this checkout is not yet at the documented starting line.

The most important next move is to reconcile the backend source of truth. Without that, any direct implementation of the Phase 5 spec risks patching the wrong branch and compounding the divergence already visible between the docs and code.

## Addendum - Review of `Phase5_Intelligence_Routing_Reconciliation.html`

I also reviewed `C:\Users\bibe\Downloads\Phase5_Intelligence_Routing_Reconciliation.html`, dated April 29, 2026.

### What it adds

Compared with the markdown and docx anchors, the HTML adds:

- a stronger argument for hybrid trust unification
- three extra findings:
  - off-platform message scanning is not wired
  - sentiment analysis is never triggered from Django
  - an argument-order bug in `get_trust_score()` / `get_cohort_benchmark()`
- a different preferred remediation path:
  - new Intelligence ingest endpoint
  - fire-and-forget Django event bridge
  - dashboard trust should read Django snapshots as the authoritative source

### What aligns with the existing review

The HTML is aligned with the core architectural direction already identified in the anchor docs:

- Django should remain the source of truth for user state, badges, and tiers.
- Intelligence should enrich, not replace, the local trust pipeline.
- The badge and trust flow is disconnected from Intelligence.
- The RL feedback loop is incomplete.
- Static JWT handling remains a real concern, but not the first blocker.

### What does not fit the local checkout

The HTML has the same baseline problem as the other Phase 5 documents, and in some places it is even more explicit about code that is not present in the checked-out backend.

Examples:

- It assumes `core/services/legacy_client.py` exists and exposes:
  - `get_trust_modifier()`
  - `submit_feedback()`
  - `analyse_message_off_platform()`
  - `analyse_sentiment()`
- It assumes trust/cohort helper callsites exist in:
  - `consultant/consultant_views.py`
  - `breeders/views.py`
- It assumes an Intelligence-side dashboard/helper surface exists and can be retargeted to Django snapshots.

Those assumptions are not verifiable in this checkout:

- No `legacy_client.py` exists under `core/services/`.
- No `intelligence/` app exists.
- A repo-wide search did not find local references to:
  - `get_trust_score(`
  - `get_cohort_benchmark(`
  - `analyse_message_off_platform(`
  - `analyse_sentiment(`
  - `submit_feedback(`
- The consultant and breeder modules present locally do not expose the cited trust/cohort integration points.

### Updated interpretation

The HTML is useful as a second independent architecture opinion, and it reinforces the intended end-state. It should not, however, be treated as proof that the current local checkout has those integration surfaces waiting to be wired.

The extra HTML findings should therefore be classified as follows:

- Off-platform scanning gap:
  - plausible on the intended Phase 5 baseline
  - not verifiable in this checkout
- Sentiment-trigger gap:
  - plausible on the intended Phase 5 baseline
  - not verifiable in this checkout
- Argument-order bug:
  - specific and actionable if the cited helper functions exist
  - not verifiable in this checkout because the referenced helper callsites were not found

### Net effect on the remediation order

The HTML does not change the top priority.

Priority still remains:

1. Reconcile the backend source of truth and correct baseline.
2. Restore or locate the documented Intelligence client surfaces.
3. Consolidate the duplicate local trust paths.
4. Only then apply hybrid trust enrichment, event bridging, RL feedback closure, and dashboard unification.

So the HTML strengthens the conceptual remediation plan, but it does not remove the repo-baseline mismatch that blocks direct implementation against the current checkout.
