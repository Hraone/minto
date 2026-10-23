# Minto Wishlist Feature

## Goal
Give every signed-in Minto user a private wishlist for planned purchases and personal savings goals.

## User-facing behavior
- Add a Wishlist link inside the profile dropdown, not as a top-level navigation item.
- Wishlist page is responsive on desktop and mobile and follows the existing Minto theme variables.
- A user can create an item with:
  - Name (required, 1–120 characters)
  - Target amount in INR (required, greater than zero)
  - Already-saved amount (optional, defaults to zero)
  - Target date (optional)
  - Priority (low, medium, high)
  - Notes (optional)
- Display each item with target amount, saved amount, savings progress, target date, priority, notes, and status.
- Let the user update target/saved amounts; pause or reactivate an item; mark it purchased; or delete it.
- Show total target amount and saved amount across active wishlist items.
- Validate values on the server; never trust browser-side validation alone.
- Restrict all reads and writes to the signed-in user's own records.

## Data model
Create `public.wishlists` with:
- `id`: generated primary key
- `user_id`: auth user foreign key
- `name`, `target_amount`, `saved_amount`
- `target_date`, `priority`, `status`, `notes`
- `created_at`, `updated_at`

Use Supabase Row Level Security with a policy that only permits `auth.uid() = user_id`. Keep `saved_amount` between zero and `target_amount`; target amount must be positive.

## Database setup
Apply `wishlist_migration.sql` in the Supabase SQL Editor before using the feature. Keep the table definition in sync with `schema.sql`.

## Acceptance checks
1. A signed-in user can open Wishlist from the profile menu.
2. Creating an item persists after refresh.
3. A user sees only their own items.
4. Invalid amounts, dates, names, and priorities are rejected.
5. Progress percent never exceeds 100%.
6. Updating amounts refreshes totals and progress.
7. Pause, reactivate, purchased, and delete actions persist.
8. Page layout works at desktop and narrow mobile widths.
9. If the migration is missing, the page shows a useful error rather than silently implying the feature is ready.

## Verification status
Implementation is on a draft PR. Live database and browser checks still need to be completed after applying the migration. Do not merge until those checks pass.
