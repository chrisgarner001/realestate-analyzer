# TODOS

## Design System

### Create a formal DESIGN.md

**What:** Document the existing navy/gold token system (`--navy-deep`, `--gold-light`, `--brand`, Inter font) and component patterns as an open-format `DESIGN.md`, via `/design-consultation`.

**Why:** `/plan-design-review` had to reverse-engineer the visual system from `admin.html`'s inline CSS variables this session — a real `DESIGN.md` would let future design reviews calibrate against a stated system instead of re-deriving it each time.

**Context:** `docs/designs/self-serve-solo-tenant.md`'s "UI/UX Specification" section documents the specific tokens used for the solo-signup feature; a full `DESIGN.md` would generalize that across the whole app.

**Effort:** M
**Priority:** P3
**Depends on:** None.

## Self-Serve Solo Tenant

### Logo upload for solo tenants

**What:** Add a file-upload flow (storage, validation, resize) so solo tenants can use a real logo instead of the initials-avatar fallback.

**Why:** The initials avatar (color + name) is the v1 branding depth. A real logo is likely wanted once solo tenants validate the segment, but building it now would be premature — the same "build the platform before proving the wedge" trap Approach B was rejected for.

**Context:** `admin.html` already has a `logo_url` field / branding tab for partner tenants. A `tier='solo'` tenant may already be able to reuse that same tab post-signup without any new upload plumbing — check this first before building anything new. Revisit once Jerry (or future solo tenants) give real feedback that the initials avatar isn't enough.

**Effort:** S (if the existing branding tab already covers it) / M (if new upload/storage is actually needed)
**Priority:** P3
**Depends on:** Real usage signal from solo tenants, post-launch.

### Card-fingerprint dedup for free-token abuse

**What:** Detect the same physical card attached to a new Stripe Customer across repeat signups with disposable emails, to stop farming multiple free tokens.

**Why:** The card-on-file gate (Stripe SetupIntent) stops disposable-email abuse but not card-reuse abuse — explicitly scoped out as acceptable residual risk while signup volume is near-zero (Assignment tests with exactly Jerry).

**Context:** `docs/designs/self-serve-solo-tenant.md`'s Constraints section already documents this as a known, accepted gap. Stripe card fingerprinting or a Radar rule would close it. Do not build speculatively — this is worth doing once the public `/signup` endpoint sees real traffic, not before.

**Effort:** S
**Priority:** P3
**Depends on:** Evidence of actual abuse, or real signup volume beyond the current single-tester validation.

## Security

### Remove balances and invite template from the public tenant endpoint

**What:** Move `token_balance`, `daily_limit`, and `invite_template` off the unauthenticated `GET /api/tenant/{slug}` to authenticated endpoints.

**Why:** Anyone who knows a partner's slug can read its token balance, daily limit, contact details, and invite email copy. Branding needs to be public; the rest doesn't.

**Pros:** Closes a low-severity data exposure; aligns with the owned-asset design's choice to keep entitlements on `/api/me`.

**Cons:** Need to audit which pages read those fields before login (`index.html`, `admin.html`) so nothing breaks.

**Context:** Found during `/plan-eng-review` of `docs/designs/owned-asset-hold-sell-refi.md` (R12/T1, 2026-09-24). `web/server.py:1120` `get_tenant_branding` has no auth dependency. Start by grepping `api/tenant/` callers.

**Effort:** S (human ~2h / CC ~15min)
**Priority:** P2
**Depends on:** None.

## Reports

### Owned-asset records (Approach B) for the Hold/Sell/Refi module

**What:** Add an `OwnedProperty` table per tenant (fields = `OwnedAssetInputs` names) with CSV import, so IRES enters each asset once and re-runs reports; foundation for portfolio batch and monitoring.

**Why:** v1 (Approach A) makes IRES re-enter loan/rent/opex on every run. That friction grows with portfolio size.

**Pros:** Enter once, re-run anytime; unlocks batch runs and a portfolio summary; stickier for large managers.

**Cons:** New CRUD UI, CSV parsing, validation, and a migration; premature until IRES uses v1 regularly.

**Context:** Deferred in `docs/designs/owned-asset-hold-sell-refi.md` (Approach B, 2026-09-24; T3). `OwnedAssetInputs` field names were chosen to become the table's columns, so this is a migration, not a rename.

**Effort:** M (human ~3 weeks / CC ~1 day)
**Priority:** P3
**Depends on:** Hold/Sell/Refi v1 shipped, and IRES running properties regularly or asking for batch with a property count.

### Refund tokens on failed reports for every report type

**What:** Apply the Hold report's refund rule (refund unless the model's narrative started) to all 12 existing report types.

**Why:** Existing types debit 1 token before streaming and never refund on failure, while Hold reports will refund. Agents lose tokens on failed runs.

**Pros:** Reuses the tested Hold refund logic; consistent billing across report types.

**Cons:** Intentionally changes the R14 regression contract's "no refund on failure" assertion; must update that test deliberately.

**Context:** Found during `/plan-eng-review` of `docs/designs/owned-asset-hold-sell-refi.md` (T2, 2026-09-24). See `stream_response` `except` branches in `web/server.py` `/analyze`.

**Effort:** S (human ~1h / CC ~10min)
**Priority:** P3
**Depends on:** Hold/Sell/Refi v1 (refund helper exists).
