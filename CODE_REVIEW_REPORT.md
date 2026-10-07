# Minto 2.0.0 Production Audit

**Audit date:** 2026-10-07  
**Repository:** Hraone/minto  
**Reviewed baseline:** main at f71550a980909f8c38fbc90b7a127ada42b40ed3  
**Scope:** Backend and templates, SQL/schema/migrations, authentication/session handling, RLS, personal finance and Trip Mode calculations, reports, static assets, configuration and deployment hints.

## Executive summary

Minto has a coherent separation between personal account movements and trip expense shares. Shared-trip SQL functions use database transactions, validate member/owner authority, and make share acceptance idempotent. The database sources include owner RLS for personal tables and membership-aware policies for Trip Mode. The 24 avatar IDs exist in the SVG sprite, username uniqueness is case-insensitive in SQL, and theme-aware favicon selection is present.

The app is **not production-audit ready** based on this source review. The audit branch now fails closed when required Net Worth, trip, fixed-expense, or card-EMI reads fail: affected financial pages return an explicit unavailable response instead of displaying incomplete totals, and snapshot calculation stops before its write. This still needs deployment and live verification. Trip Case B, ledger atomicity, CSRF, and other open findings remain. Production UI and actual Supabase RLS behavior could not be exercised here.

The audit branch includes three mitigations: signup/login no longer display raw Supabase exception text, destructive confirmations use escaped HTML data attributes instead of inserting dynamic names into inline JavaScript, and required financial reads fail closed. These changes are not live until the PR is merged and deployed.

## Severity summary

| Severity | Finding | Status |
|---|---|---|
| P0 | Failed Net Worth, trip, fixed-expense or EMI reads can become zero/empty financial data; snapshots can save incomplete totals | Fixed on audit branch; deployment/live verification pending |
| P1 | Trip Case B expected outstanding is absent while a Minto friend's share is pending | Open; requires coordinated Python and SQL change |
| P1 | Some ledger/transfer workflows span independent database requests and rely on best-effort cleanup | Open |
| P1 | Account-balance and dashboard transaction reads are unpaged, unlike report reads | Open; impact depends on PostgREST row cap and account size |
| P1 | Cookie-authenticated POST routes have no CSRF token or origin validation | Open |
| P2 | Safe to Spend forecast omits existing card outstanding; “after salary” adds salary without later commitments | Open |
| P2 | Three manifest icon paths and the PDF logo path point to absent files | Open |
| P2 | Some default transaction dates use database current_date, not the app's Asia/Kolkata date | Open |
| P2 | Security-definer membership helper RPCs accept arbitrary user UUIDs and can reveal membership/owner booleans | Open; limited metadata disclosure |
| P3 | Desktop/mobile/light/dark/accessibility behavior was not visually/browser tested | Not testable here |
| P4 | README repeats 2.0.0 prose while application version constant is centralized | Documentation duplication |

## Critical finding

### P0 — Financial read failures can look like valid zeroes

On the reviewed baseline, several helpers logged and returned a financially meaningful empty value on failure:

- get_trip_payables() returns 0 when its RPC fails (app.py:2381).
- Net Worth manual items and snapshots return [] if the service client is missing or a query fails (app.py:522, app.py:541).
- Active card-loan/EMI rows return {} on query failure (app.py:611).
- Fixed expenses return [] on query failure (app.py:3823); get_upcoming_commitments() also converts a fixed-expense read failure into [] (app.py:694).

That allowed the dashboard to calculate wealth, fixed commitments and Safe to Spend from incomplete data. Missing fixed expenses could make spendable cash look higher; missing EMI data could hide card commitments; missing manual items or trip payables could lower liabilities. A snapshot could then be saved with the incomplete total.

**Status:** fixed on the audit branch. These helpers now raise a dedicated unavailable error, the affected routes return a styled 503 page with the affected totals hidden, and the dashboard no longer swallows commitment/fixed-expense read failures. A failed read during snapshot calculation exits before the snapshot upsert. Existing formulas are unchanged. Failure-injection and live database tests remain unverified because the workspace lacks the Flask/Supabase runtime and database credentials.

## Major findings

### P1 — Trip Case B does not meet the requested model

calculate_trip_balances() and trip_payables_for_user() count only accepted/settled shares (trip_accounting.py:34, trip_accounting.py:120); SQL trip_member_net() and current_trip_payables() use the same statuses (minto_v2_migration.sql:952, minto_v2_migration.sql:969). A newly created Minto member share is pending (minto_v2_migration.sql:566). Before the friend accepts, the payer sees no friend debt/outstanding; requested Case B expects the friend responsibility and ₹2,000 outstanding immediately. Case C then expects acceptance to record the friend's personal expense without changing the already-existing outstanding amount.

This is internally consistent with the current “acceptance acknowledges responsibility” rule, but it **fails the audit brief's explicit Case B result**. Changing it affects trip balances, settlement validation, Net Worth payables and reject/reassign behavior. It must be implemented as one coordinated behavior change with a new SQL migration and tests, not a Python-only patch.

### P1 — Multi-table finance writes are not atomic

insert_entry_and_transaction() inserts an entries row and transactions row in separate requests, then attempts to delete the first row if the second fails (app.py:372). Cash withdrawals, bank transfers and card payments create paired legs through separate requests and best-effort cleanup (app.py:3404, app.py:3488, app.py:2010). Card-loan payment also updates the loan row and inserts payment history separately. If cleanup fails after partial success, the ledger may retain an unmatched movement or stale EMI balance. Shared-trip RPC operations do not have this design: their related writes execute within a database function transaction.

**Required fix:** move paired ledger operations and card-loan updates into database RPCs with constraints/idempotency, plus migrations, then test forced failure at every step. Compensating deletes do not guarantee atomicity.

### P1 — Unpaged reads can truncate account and dashboard totals

The report helper explicitly pages in 1,000-row batches because Supabase/PostgREST can cap results (app.py:1422). In contrast, compute_source_balances() fetches all user transactions in one request (app.py:3561); the dashboard period query is also unpaged (app.py:4340); lending summary has an unpaged all-time query. If the project's PostgREST maximum is 1,000 rows, users beyond that limit get incomplete balances, investments, lending and dashboard totals. This is especially serious for all-time balance calculations.

**Required fix:** use a shared deterministic paging helper for every aggregate read, or verify/configure a safe higher database cap and test beyond it. Verify ordering and deduplication.

### P1 — Cookie-authenticated POSTs have no CSRF token or origin check

The Flask session uses a signed cookie with HttpOnly, Secure by default and SameSite=Lax (app.py:28). No CSRF token validation or Origin/Referer check was found. Lax reduces ordinary cross-site form POST exposure but is not a complete origin policy; same-site sibling origins and other browser flows remain outside that mitigation. Authenticated routes mutate profile, finances, friends, trips and Net Worth. /logout and /mode also accept GET and change session state.

**Required fix:** implement synchronizer tokens for HTML forms and same-origin AJAX, validate them on unsafe methods, exempt only explicitly authenticated machine calls (cron already has a separate secret), and convert state-changing GETs to POST. Add cross-origin request tests.

### P2 — Safe to Spend does not fully account for card obligations or salary-cycle commitments

The formula is liquid cash/cash-source balance less commitments due during the current calendar month; future-month commitments are intentionally excluded (app.py:4340). Card forecast logic excludes existing opening/current card outstanding and includes only new cycle activity (app.py:611). This can leave an already-issued statement or imported card debt out of the current commitment total. Also safe_to_spend_after_salary is current Safe to Spend plus the next salary and does not reserve commitments due after payday (app.py:4340); this can overstate what will actually be available.

**Required fix:** define whether current statement balance is an upcoming obligation and include its due date/amount when known. Rename the salary figure as a simple “cash plus salary” estimate or calculate post-payday commitments explicitly. Preserve and test the current-month boundary rule.

## Database, RLS and authorization

- schema.sql enables RLS for profiles, sources, entries, transactions and categories, and defines owner policies. It also defines RLS for fixed expenses/payments, report history, credit-card loans/payments, budgets and Net Worth tables.
- minto_v2_migration.sql enables RLS on friends, friend requests, trip members, shares, settlement events and legacy splits; it replaces trip owner-only reads with active-member visibility and limits PostgREST column grants. Shared writes use security-definer RPCs with search_path = public, pg_temp and explicit auth checks.
- Net Worth security/items/snapshots have no authenticated row policies; they use the server-only service-role client. The service key is not included in template globals; only the anon key is sent to browser auth code.
- Personal routes generally filter by session user ID and execute with the user's Supabase JWT; trip routes use membership-aware RLS and/or authorization-checking RPCs. No obvious route was found that trusts a browser-supplied user UUID to access another user's personal rows.
- is_trip_member(integer, uuid) and is_trip_owner(integer, uuid) are security-definer helpers granted to authenticated and accept caller-supplied p_user_id. They can answer membership/ownership questions for a UUID other than auth.uid(). They do not return trip contents, but should enforce p_user_id = auth.uid() or be replaced with identity-bound functions.
- SQL sources were reviewed, but no database connection or credentials were available. RLS, grants, policies, migrations and RPC behavior were not executed against Supabase. Cross-user spoofing and production migration state remain NOT TESTABLE.

## Authentication, secrets and error handling

- login_required periodically verifies the Supabase user; refresh rotates the refresh token and saves it in the session. Cookie settings are present and service-role use is server-side.
- Flask's default session is a signed client-side cookie, not encrypted. Supabase access and refresh tokens are stored in that cookie. A copied cookie exposes its contents to whoever holds it and can be replayed; logout clears the browser cookie but does not itself revoke the Supabase refresh token. Consider an opaque server-side session with revocation.
- No CSRF middleware/tokens or security response headers such as CSP were found.
- No committed credentials were identified in reviewed source. Required secrets are environment-driven. The scan did not read local environment files or query deployment configuration.
- Signup/login previously flashed raw provider exceptions. The audit branch replaces those with generic messages and logs only exception type. Provider details should remain server-side.
- The generic error handler rethrows unexpected server exceptions; users receive an error response rather than a false success, while the helper-specific fallbacks above still mask failures.

## Personal finance and accounting logic

### Verified from source

- Savings and cash balances are opening balance + inflows − outflows. Credit-card outstanding is max(opening balance + outflows − inflows, 0). Rows with affects_source_balance = false do not move account balances (app.py:3561).
- Transfers are represented by paired legs and excluded from income/spending totals. Card payments reduce bank/cash and card outstanding without counting as spending. Dashboard and PDF both exclude transfer, lending, trip payment and trip settlement categories from ordinary money-in/out totals (app.py:4340, report_pdf.py:81).
- parse_money() rejects negative values, non-finite values, required zeroes and values over ₹100,000,000, then rounds to two decimals. A very small positive amount can round to 0.00 and still be accepted; positive-only inputs should reject amounts that round to zero.
- PDF transaction reads page through results and use inclusive ISO date ranges. PDF rendering was not exercised.

### Safe to Spend

The code subtracts unpaid fixed expenses/investments and expected credit-card commitments due in the current calendar month from liquid bank/cash, then clamps at zero. It includes current-month overdue fixed items, loads the next month for the 31-day upcoming list, and filters future-month items out of this month's Safe to Spend. Existing card outstanding and salary-cycle commitments remain the gaps above.

### Net Worth

The formula includes bank/cash balances, investment contributions net of redemptions, net lending, accepted trip payables, current card debt, manual assets and manual liabilities. total_invested is contribution net, not current market value; users need manual values for current valuation. Trip payables and manual items can be omitted during failed reads, which invalidates the headline and snapshots.

## Trip Mode accounting cases A–F

The nine existing tests in tests/test_trip_accounting.py passed. They are pure helper tests; they do not call Supabase or SQL RPCs.

| Case | Expected behavior | Source result | Verification |
|---|---|---|---|
| A — solo ₹4,000 | Expense ₹4,000; no outstanding; account −₹4,000 | PASS by code review and pure balance test. Full payment moves the account; own share is non-balance. | Unit test covers zero net trip balance; no DB integration |
| B — payer ₹4,000, shares ₹2,000/₹2,000 | Friend responsibility and outstanding ₹2,000 before acceptance; friend account unchanged | FAIL. Pending shares are excluded until acceptance, so current outstanding is ₹0 before acceptance. | Existing unit test explicitly confirms pending exclusion |
| C — friend accepts | Friend expense ₹2,000; no account movement; outstanding remains ₹2,000 | PASS by code review. Acceptance writes a non-balance personal expense; payable remains until settlement. | SQL RPC not run against DB |
| D — friend settles | Friend −₹2,000; recipient +₹2,000; outstanding zero; no duplicate expense | PASS only after both actions: payer marks paid (outflow/status), recipient separately records receipt (inflow/receivable reversal). Receipt is idempotent. Recipient's account is not updated automatically when payer marks paid. | Source review only; UI should explain two steps |
| E — accept twice | One personal transaction | PASS by source: share row is locked, accepted/settled returns existing transaction ID. | No RPC integration test |
| F — guest member | Works without account | PASS for name-only member accounting; guest shares are accepted by default. | Pure balance test and source review; full DB flow not testable |

The migration copies legacy guest participants and split names into stable member snapshots and creates accepted shares from old splits. This is source-reviewed only; no database rehearsal was available.

## Friends, usernames and avatars

- Usernames are normalized to lowercase and validated as 3–24 letters/digits/underscores. The database has a unique index on lower(username); this is the authoritative race-safe check. The availability RPC is feedback, not a uniqueness guarantee. The update handler recognizes unique conflicts.
- Friend search returns username/display name/avatar/relationship only, not email or account UUID. SQL checks self-request and pending/duplicate states; friendship pairs are sorted. No rate limiting is visible on lookup/request RPCs; consider abuse controls if usernames are intended to be unlisted.
- The profile registry generates avatar-01 through avatar-24, and the SVG sprite contains 24 unique matching symbols. Legacy emoji fallback and avatar allowlist validation are present. No old profile_emojis references were found. SVG rendering, first login and mobile selection were not browser-tested.
- Profile radio inputs have labels and accessible names; keyboard/focus behavior was not tested with assistive technology.

## Frontend, UI, responsive layout, dark mode and accessibility

- Static checks covered all 20 HTML templates. No literal url_for() endpoint was missing, no duplicate literal IDs were found, no legacy plural profile_emojis references remain, and no inline onsubmit=confirm(...) remains in the audit branch.
- Destructive-action names now use escaped data-confirm attributes and window.confirm reads them as text. This closes the reviewed HTML-attribute-to-inline-JavaScript injection pattern.
- CSS uses theme variables for most surfaces/colors. Remaining white values appear in positive-action text, toggle knobs and translucent highlights. Static inspection found no obvious hard-coded black/white dark-mode regression.
- Responsive rules exist for navigation, profile, dashboard, reports, sources and trips. Static inspection cannot establish no overflow/overlap at target sizes. Desktop 1920×1080 through 1024×768, mobile 430×932 through 360×800, landscape, reduced-motion, real contrast, touch targets and screen-reader behavior are NOT TESTABLE here.
- Manifest paths /static/icons/icon-192.png, icon-512.png and icon-maskable-512.png are absent. report_pdf.py points to icon-192.png too, so generated reports omit the logo. Install-icon delivery and PDF branding need correction.
- base.html contains reduced-motion rules; their effect was not browser-tested.

## Reports, dates, versioning and operations

- _fetch_transactions() pages report data and applies inclusive date filters. PDF totals exclude transfer/lending/trip movement categories and include expense categories/source totals. PDF/web agreement, single-day/month boundaries and large datasets were not exercised end-to-end.
- App dates use Asia/Kolkata, but omitted personal transaction dates rely on the database current_date default. If the database session is UTC, entries near Indian midnight may receive the previous date. PDF “Generated” also uses date.today() rather than APP_TZ.
- MINTO_VERSION is defined once in app.py and About uses the injected variable. README repeats 2.0.0 as documentation.
- The service worker caches only same-origin /static/ GET assets, not pages/API responses. Cache-Control: no-store applies to non-static routes. /health only returns OK; it does not verify Supabase readiness.
- requirements.txt packages are unpinned. No lockfile, deployment manifest or production URL was available. Production server configuration and dependency resolution were not verified.
- Missing manifest icons and report logo are confirmed from repository paths. No remote app was available to verify PWA installation.

## Route matrix

“Auth” means login_required; personal rows use the signed-in Supabase client/RLS and session user ID. “Trip” means membership-aware RLS and/or an authorization-checking trip RPC. Every row is source-reviewed only; live RLS behavior was not executed.

| Route | Methods | Auth | Ownership/authorization | Risk | Status |
|---|---|---|---|---|---|
| /signup | GET, POST | Public | Supabase Auth; profile insert uses returned user | Raw provider text fixed in audit branch; no rate limit/CSRF | Reviewed |
| /login | GET, POST | Public | Supabase password auth | Raw provider text fixed in audit branch; no rate limit/CSRF | Reviewed |
| /auth/passkey-login | POST | Public | Verifies Supabase access token before session creation | Token-pair binding/login CSRF not integration-tested | Reviewed |
| /auth/passkey-flag | POST | Auth | Current profile via user JWT/RLS | CSRF missing | Reviewed |
| /auth/passkey-token | POST | Auth | Refresh token from current session | Returns token material to same-origin JS; CSRF missing | Reviewed |
| /profile | GET | Auth | Profile/account queries use current user/RLS | Friend RPC failures can look like empty friend center | Reviewed |
| /profile/username | POST | Auth | Current profile row | Unique DB index handles races; CSRF missing | Reviewed |
| /profile/username/availability | GET | Auth | RPC derives current identity | Username enumeration/rate limit not tested | Reviewed |
| /profile/default-expense-account | POST | Auth | Source must belong to current user | CSRF missing | Reviewed |
| /friends/requests | POST | Auth | RPC derives auth.uid() | CSRF/rate limit missing | Reviewed |
| /friends/requests/<request_id> | POST | Auth | RPC verifies receiver | CSRF missing | Reviewed |
| /friends/remove | POST | Auth | RPC derives caller and target username | CSRF missing | Reviewed |
| /profile/genz-mode | POST | Auth | Current profile row | CSRF missing | Reviewed |
| /profile/emoji | POST | Auth | Avatar allowlist and current profile | Legacy route name retained for DB compatibility | Reviewed |
| /profile/money-plan | POST | Auth | Current profile row | CSRF missing | Reviewed |
| /profile/theme | POST | Auth | Current profile row | CSRF missing | Reviewed |
| /profile/name | POST | Auth | Current profile row | CSRF missing | Reviewed |
| /reports | GET | Auth | History filtered by current user/RLS | Empty history on read error | Reviewed |
| /reports/history | GET | Auth | Current user/RLS | Empty history on read error | Reviewed |
| /reports/download | GET | Auth | Transaction query filters current user | PDF generation not executed | Reviewed |
| /internal/monthly-reports | POST | Cron secret | Constant-time secret check; service client server-side | Returns 503 if cron secret unset | Reviewed |
| /info | GET | Public | No private data | Passkey controls only for signed-in users | Reviewed |
| /logout | GET | Public | Clears current cookie session | State-changing GET/logout CSRF | Reviewed |
| /net-worth | GET, POST | Auth | Separate unlock; service queries filtered by session user | Silent failed reads can save wrong snapshot (P0) | Reviewed |
| /net-worth/lock | POST | Auth | Clears current unlock session | CSRF missing | Reviewed |
| / | GET, POST | Auth | Personal rows/source checks use current user | Multi-call write and best-effort rollback | Reviewed |
| /pay-cc-bill | GET, POST | Auth | Sources and loans filtered to current user | Multi-call payment/loan update | Reviewed |
| /categories/add | POST | Auth | User-owned category | CSRF missing | Reviewed |
| /transactions/<entry_id>/delete | POST | Auth | Current-user transaction and paired rows | Multi-step delete; CSRF missing | Reviewed |
| /mode | GET, POST | Auth | Changes current session/profile mode | State-changing GET/CSRF | Reviewed |
| /trip-home | GET | Auth | Current user's member-visible trips | RLS dependent | Reviewed |
| /trips | GET, POST | Auth | Queries rely on RLS; owner row on create | Guest/member setup can partially fail after trip creation | Reviewed |
| /trips/<trip_id>/update | POST | Auth | Owner helper and mutable-trip checks | CSRF missing | Reviewed |
| /trips/<trip_id>/friends | POST | Auth | Owner-only member RPC | CSRF missing | Reviewed |
| /trips/<trip_id>/friends/<friend_id>/delete | POST | Auth | Owner/mutable trip checks | CSRF missing | Reviewed |
| /trips/<trip_id>/expenses | POST | Auth | Active member; RPC validates payer/member/source/shares | Atomic RPC; CSRF missing | Reviewed |
| /trips/<trip_id>/expenses/<expense_id>/edit | GET, POST | Auth | Membership, author RPC and mutable check | Linked history is blocked from edit | Reviewed |
| /trips/<trip_id>/expenses/<expense_id>/delete | POST | Auth | Trip/author/linked-state checks | RPC used; CSRF missing | Reviewed |
| /trips/<trip_id>/settlements | POST | Auth | Membership and participant/owner RPC checks | Atomic RPC; CSRF missing | Reviewed |
| /trips/<trip_id>/settlements/<settlement_id>/status | POST | Auth | Payer/recipient/owner RPC rules | Two-step receipt; CSRF missing | Reviewed |
| /trips/<trip_id>/shares/<share_id>/accept | POST | Auth | Share member verified in RPC | Idempotent RPC; CSRF missing | Reviewed |
| /trips/<trip_id>/shares/<share_id>/reject | POST | Auth | Share member verified in RPC | Pending share excluded from balances | Reviewed |
| /trips/<trip_id>/shares/<share_id>/reassign | POST | Auth | Author/owner and replacement checked in RPC | Atomic RPC; CSRF missing | Reviewed |
| /trips/<trip_id>/settlements/<settlement_id>/receipt | POST | Auth | Recipient and source checked in RPC | Idempotent receipt; CSRF missing | Reviewed |
| /transactions/<transaction_id>/linked | GET | Auth | Transaction filtered by current user | RLS/ownership source-reviewed | Reviewed |
| /trips/<trip_id>/delete | POST | Auth | Owner and mutable status checked | Cascades history; confirmation; CSRF missing | Reviewed |
| /trips/<trip_id>/status | POST | Auth | Owner and status allowlist | Archived restrictions; CSRF missing | Reviewed |
| /trips/<trip_id> | GET | Auth | Active trip member helper/RLS | Full trip access not live-tested | Reviewed |
| /withdraw-cash | GET, POST | Auth | Both sources selected from current user | Paired writes use compensation; CSRF missing | Reviewed |
| /bank-transfer | GET, POST | Auth | Both sources selected from current user | Paired writes use compensation; CSRF missing | Reviewed |
| /budgets | GET, POST | Auth | Current user row/RLS | Failed budget read looks empty; CSRF missing | Reviewed |
| /budgets/<budget_id>/delete | POST | Auth | Delete filters current user | CSRF missing | Reviewed |
| /fixed-expenses | GET, POST | Auth | Rows use current user/RLS | Failed reads look empty; CSRF missing | Reviewed |
| /fixed-expenses/<id>/pay | POST | Auth | Expense/source ownership checked | Transaction/payment rows are multiple calls | Reviewed |
| /fixed-expenses/<id>/delete | POST | Auth | Delete filters current user | CSRF missing | Reviewed |
| /sources/<id>/cc-loan | POST | Auth | Card/loan queries filter current user | EMI write errors generic; CSRF missing | Reviewed |
| /sources/<id>/card-cycle | POST | Auth | Source ownership checked | CSRF missing | Reviewed |
| /sources | GET, POST | Auth | Source rows use current user/RLS | Failed EMI read may appear empty; CSRF missing | Reviewed |
| /dashboard | GET | Auth | Transactions/sources use current user/RLS | Silent financial fallbacks and unpaged reads | Reviewed |
| /sw.js | GET | Public | Serves static service worker | Static-only cache behavior | Reviewed |
| /manifest.json | GET | Public | Serves static manifest | Referenced icons absent | Reviewed |
| /favicon.ico | GET | Public | Redirects based on session theme | Browser not exercised | Reviewed |
| /health | GET | Public | No user data | Liveness only; no DB readiness | Reviewed |

## Test matrix

PASS is used only for executed checks. Static review is WARNING; unavailable production/browser access is NOT TESTABLE.

| Feature | Desktop | Mobile | Light | Dark | Logic | Security | Result |
|---|---|---|---|---|---|---|---|
| Signup/login | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | WARNING — source reviewed; CSRF unresolved | WARNING |
| Dashboard/account balances | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | WARNING — read failures/unpaged totals | WARNING — RLS not live-tested | WARNING |
| Safe to Spend | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | WARNING — current-month formula; card/salary gaps | WARNING — failed reads can be empty | WARNING |
| Net Worth/snapshots | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | FAIL — incomplete snapshot may be persisted | WARNING — service-role path source-reviewed | FAIL |
| Personal transactions/transfers | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | WARNING — multi-request partial-write risk | WARNING — ownership source-reviewed | WARNING |
| Reports/PDF | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | WARNING — pagination reviewed; PDF not built | WARNING — owner filters reviewed | WARNING |
| Friends/username | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | WARNING — DB uniqueness/live check source-reviewed | WARNING — RPC/RLS not live-tested | WARNING |
| Avatars | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | PASS — 24 matching SVG IDs verified | WARNING — authorization not live-tested | WARNING |
| Trip Case A | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | PASS — unit test and source review | WARNING — SQL not live-tested | PASS (unit logic) |
| Trip Case B | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | FAIL — pending share is not outstanding before accept | WARNING — SQL not live-tested | FAIL |
| Trip Cases C–F | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | WARNING — source-reviewed; pure tests pass; no RPC tests | WARNING — SQL not live-tested | WARNING |
| Responsive UI/overflow | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE |
| Dark mode/favicon | NOT TESTABLE | NOT TESTABLE | WARNING — theme source-reviewed | WARNING — favicon source-reviewed | NOT TESTABLE | NOT TESTABLE | WARNING |
| SQL RLS/cross-user access | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE — no live Supabase | NOT TESTABLE |
| PWA install/PDF logo | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | NOT TESTABLE | FAIL — manifest/PDF paths absent | NOT TESTABLE | FAIL |

## Verification and limitations

- python.exe -m unittest discover -s tests -v: **9 tests passed**.
- Standard-library static scan: 20 templates; 52 form tags; no missing literal url_for() endpoint, duplicate literal ID, old profile_emojis reference or inline onsubmit=confirm in the audit branch.
- SVG XML parse: 24 unique avatar symbols from avatar-01 through avatar-24.
- Each manifest icon path was checked against repository files: all 3 are absent.
- Source scan found no innerHTML, outerHTML, insertAdjacentHTML, document.write, eval or new Function. This does not prove all browser behavior safe.
- This workspace lacks Flask, Jinja and Supabase Python dependencies, so the web app could not be imported. ReportLab is installed, but PDF rendering was not exercised. No production URL, Supabase credentials, deployment settings or browser session were available. Jinja compilation, route integration, live RLS, migration execution, PDF rendering, visual breakpoints, accessibility tree and production smoke/regression checks remain NOT TESTABLE.
- No tests were added. The audit branch fixes are narrow source changes; the existing 9 trip tests were rerun.

## Recommended order

1. Fail closed on required financial reads; prevent incomplete snapshots and show explicit unavailable states.
2. Decide and implement Case B pending-share behavior across Python, SQL migration, settlement validation, rejection/reassignment and tests.
3. Move paired ledger and card-loan operations into atomic RPCs; add failure-injection tests.
4. Page every aggregate transaction query and test beyond the configured PostgREST cap.
5. Add CSRF tokens and Origin validation; convert GET state changes to POST.
6. Revisit Safe to Spend's current card bill and post-salary commitment semantics.
7. Add PWA/PDF icon assets or point all references to verified existing assets.
8. Run visual/browser, accessibility, live RLS and migration rehearsal checks against staging before calling the release audit-ready.

