# Empire E-Story — Supabase migration preparation

## Scope and safety

This document governs the move from the existing shared SQLite database to the owner's Supabase PostgreSQL project. The bank application and Empire Store must continue using **one shared database**. This is a preparation document, not evidence that the migration or production deployment has happened.

Do not merge this branch or deploy it until the database and storage credentials have been configured in Faable's secret manager and a deployment preview has been verified.

## Verified repository facts

- The Flask application uses a SQLite file selected by `NAB_DATABASE`.
- `app.py` and `empire_store.py` each open SQLite connections directly. They must be consolidated behind one shared database layer before PostgreSQL is enabled.
- The store module creates and incrementally alters many commerce tables in the same database as the bank.
- Image uploads currently use local filesystem directories. These are not durable on an ephemeral deployment filesystem.
- The Supabase project reference supplied by the owner is `agxldwryrzmhvcuytjoj`.

## Required implementation sequence

1. **Inventory and freeze schema behavior**
   - Enumerate every SQL statement and SQLite API in both modules and startup/migration code.
   - Record every table, index, constraint, foreign key, trigger, and migration in the current shared database.
   - Confirm the currently approved asset symbols and owner-account bootstrap rules before changing any financial schema.

2. **Build one database abstraction**
   - Use one connection factory and transaction API shared by bank and store.
   - Preserve row-name access, parameter binding, generated IDs, transaction boundaries, rollback behavior, and concurrency guarantees.
   - Replace SQLite-only constructs deliberately: `?` placeholders, `PRAGMA`, `executescript`, `INSERT OR IGNORE`, `lastrowid`, SQLite DDL/autoincrement, and `BEGIN IMMEDIATE`.
   - Do not silently fall back to SQLite in production if PostgreSQL is unavailable.

3. **Create versioned PostgreSQL migrations**
   - Convert the complete shared schema, not only the store tables.
   - Make migrations repeatable and safe to resume.
   - Keep bank balances and the store's double-entry ledger in the same PostgreSQL database.
   - Add integrity checks for balanced journal entries, non-negative balances where required, uniqueness/idempotency, and foreign keys.

4. **Move files to durable object storage**
   - Use a private Supabase Storage bucket for payment proofs and private documents, with authenticated access or short-lived signed URLs.
   - Use an appropriate public/private policy for marketplace images.
   - Store object keys in PostgreSQL, not temporary local absolute paths.
   - Preserve upload validation, size/type limits, and authorization.

5. **Configure deployment secrets**
   - Configure the PostgreSQL connection string, storage URL/bucket, and server-side storage key only in Faable's secret manager.
   - Never commit passwords, service-role keys, encryption keys, or production credentials.
   - Use a least-privilege database role for the application; keep administrative credentials out of runtime configuration.

6. **Validate before release**
   - Test schema initialization from an empty database.
   - Test transactions, rollback, concurrent transfers, duplicate payment callbacks, and double-entry ledger invariants.
   - Test authorized file upload/download and inaccessible private objects.
   - Verify backup/restore and deployment preview.
   - Only then schedule production cutover and confirm that the Faable deployment uses persistent external services.

## Release gate

No production deployment, real-money operation, or public launch is authorized by this document alone. Do not change currency symbols, initial balances, owner assets, or financial rules as part of this storage migration.
