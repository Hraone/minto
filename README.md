Minto – Personal Expense Tracker
A modern personal finance tracker built with Flask and Supabase that helps users log expenses,
manage multiple bank accounts and credit cards, and view their spending through a clean and responsive dashboard.

Overview

Minto is a web-based personal expense tracker designed to make managing daily finances simple and organized.
Users can record income and expenses, organize transactions across multiple savings accounts and credit cards,
and monitor their monthly spending from a centralized dashboard. Built with Flask and Supabase, Minto focuses on secure authentication,
reliable data storage, and a smooth user experience across desktop and mobile devices.

## Minto 2.0.0

Minto 2.0.0 adds username-based friends and friend requests, linked Minto trip
members alongside existing name-only guests, and shared trip expenses with
accept/reject, personal transaction links, and settlement tracking. A linked
trip payment records the real payer movement once; accepted shares record each
member's responsibility without moving their account balance. A settlement
changes balances only when the payment and, for a Minto recipient, receipt are
recorded.

### Database upgrade

Run `minto_v2_migration.sql` in the Supabase SQL editor after the existing
`schema.sql` and `trip_mode_v1_5_migration.sql`. The migration preserves legacy
trip rows and adds the username, friends, membership, share, transaction-link,
settlement-history, and row/column security rules required by 2.0.0.
