# Minto Admin Panel MVP implementation notes

## Delivered in this branch
- Dedicated Admin Panel routes: dashboard, user management, feedback inbox/detail, and read-only activity logs.
- Live server-side role checks using the trusted Supabase service-role client and public.admin_roles. Authorization is re-evaluated on every protected request; role state is not trusted from the browser session.
- User directory is sourced from Supabase Auth and safe display-name fields only. It does not query transactions, balances, investments, or other private finance records.
- Grant/revoke actions are confirmed in the browser and persisted by a database function with a transaction-level advisory lock and last-admin protection.
- Feedback supports status/category/priority/message filtering and detail-page status/priority updates. Original feedback messages remain unchanged.
- Role and workflow mutations are recorded in admin_activity_logs in the same database transaction.

## Changed files
- app.py: role lookup, route protection, template context, and route registration.
- admin_panel.py: admin dashboard, user directory, role actions, feedback inbox/detail, and activity logs.
- admin_validation.py and tests/test_admin_validation.py: input validation and tests.
- templates/admin/: responsive admin layout and page templates.
- templates/base.html: conditional admin navigation link.
- migrations/admin_panel_mvp.sql: role store, audit log, priority field/index, guarded RPC functions, and bootstrap instructions.

## Security model
- Current user identity comes from the existing authenticated Flask session.
- Admin routes re-check the live role in public.admin_roles; missing service-role configuration fails closed.
- RLS is enabled on role and audit tables, with no grants to anon or authenticated.
- RPC functions are executable only by service_role and independently check that the supplied actor remains an administrator.
- Role changes are serialized and the final administrator cannot be revoked.
- The admin directory intentionally excludes private financial records and authentication secrets.

## Migration and bootstrap
1. Review migrations/admin_panel_mvp.sql against the intended Supabase project.
2. Apply it manually. This code change does not apply migrations automatically.
3. Replace REPLACE_WITH_TRUSTED_ADMIN_EMAIL in the commented bootstrap statement with the exact email of an existing trusted Auth user and run that statement separately.
4. Verify the row exists in public.admin_roles before testing the admin UI.
5. Do not deploy until migration and bootstrap are approved.

## MVP limitations
- User search/filtering is applied in memory after reading paginated Supabase Auth users. For a very large user base, replace this with a database-backed directory query.
- Last-active timestamps, suspension/reactivation, private notes, assignment/history, duplicate linking, feature-request decisions, notifications, and application health are not included.
- Activity logs display actor UUIDs rather than profile names.
- Live Supabase RPC/RLS checks and browser end-to-end verification require a configured environment and are not claimed as completed.

## Verification
Pure validation tests are included. CI results and live environment checks must be reviewed separately before declaring the full acceptance criteria complete.
