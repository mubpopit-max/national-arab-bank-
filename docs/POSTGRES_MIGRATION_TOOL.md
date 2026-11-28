# SQLite to Supabase PostgreSQL migration utility

This repository contains a guarded, one-way migration utility at
`scripts/migrate_sqlite_to_postgres.py`.

It copies **all tables in the shared SQLite database** (the bank and Empire Store
use the same `NAB_DATABASE` file) into an **empty PostgreSQL `public` schema**.
It preserves explicit primary-key values, reflects table definitions and
constraints through SQLAlchemy, copies rows in batches, verifies per-table row
counts and SHA-256 row digests, and advances PostgreSQL serial sequences.

## Important production limitations

- This utility does not change the Flask application's runtime database driver.
  `app.py` and `empire_store.py` still use SQLite-specific SQL and are not yet
  compatible with a PostgreSQL URL.
- It does not switch traffic, alter Faable variables, delete the SQLite file,
  upload local files, or configure Supabase Storage.
- The source SQLite file must be available to the process running the utility.
  A copy from a developer device is not automatically the live Faable database.
- The destination schema must be empty. The utility refuses to merge into or
  overwrite any existing public tables.
- Take and protect a consistent backup of the source database before applying.
  Never run this against the only copy of production data.
- Run first with `--check`. Only run `--apply` after confirming that the source
  is the correct complete production snapshot and the Supabase credentials point
  to the intended empty project.
- Do not put database passwords or service keys in Git, chat, command history,
  or logs.

## Dependencies

```sh
python -m pip install -r requirements.txt
```

## Check only (no PostgreSQL writes)

Set `EMPIRE_SQLITE_SOURCE` to the SQLite snapshot path and
`DATABASE_URL` to the Supabase PostgreSQL connection string in the process
environment, then run:

```sh
python scripts/migrate_sqlite_to_postgres.py --check
```

Use the Supabase Transaction Pooler connection string where IPv4 connectivity is
required. The script enforces TLS by adding `sslmode=require` if it is absent.
It accepts `postgresql://` URLs and uses psycopg 3.

## Apply to an empty target

After a protected backup and a successful check, explicitly set:

```text
EMPIRE_MIGRATION_CONFIRM=COPY-EMPIRE-SQLITE-TO-POSTGRES
```

Then run:

```sh
python scripts/migrate_sqlite_to_postgres.py --apply
```

The schema creation, row copy, checksum verification, and sequence updates are
performed in one PostgreSQL transaction. Any exception rolls the transaction
back. The source database is opened read-only by this utility and is never
modified.

## Required next engineering stage

Before production cutover, implement and validate a PostgreSQL runtime adapter
for both modules, replacing SQLite-only schema inspection, PRAGMA statements,
`BEGIN IMMEDIATE`, qmark placeholders, and `INSERT OR IGNORE` behavior. Add
integration tests for concurrent transfers, wallet ledger atomicity, idempotent
payment review, foreign keys, and persistence after restart. Then implement
private Supabase Storage objects for payment proofs and uploads, verify access
controls, and perform a controlled cutover with a rollback snapshot.

A successful utility run alone is not evidence that the live app is using
PostgreSQL or that production migration is complete.
