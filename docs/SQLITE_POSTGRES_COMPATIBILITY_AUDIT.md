# Empire E-Story — SQLite/PostgreSQL compatibility audit

Audit target: `main` at `71b76cced74f34f1fdec8a3b48f3159f2f9b1262`.
This is a static source audit only. No production database was accessed and no migration was run.

## Findings

| Area | `app.py` | `empire_store.py` |
|---|---:|---:|
| Source size | 167,702 characters | 251,760 characters |
| Direct `sqlite3.connect` calls | 1 | 1 |
| `sqlite3.Row` row factories | 1 | 1 |
| `executescript` calls | 1 | 1 |
| `PRAGMA` occurrences | 6 | 8 |
| `BEGIN IMMEDIATE` occurrences | 1 | 27 |
| `INSERT OR IGNORE` occurrences | 2 | 6 |
| Qmark parameter markers | 250 | 523 |
| SQLite `AUTOINCREMENT` occurrences | 7 | 0 |
| `sqlite_master` references | 0 | 3 |

The two modules each create their own connection to the same `NAB_DATABASE` file using Flask's `g`. The store contains its own schema initialization and compatibility/migration logic. The bank has a separate schema initializer and column inspection. These cannot safely be replaced with a PostgreSQL URL by changing environment variables.

## Confirmed Supabase target

The owner supplied these non-secret settings from the Supabase dashboard:

- Project ref: `agxldwryrzmhvcuytjoj`
- Project URL: `https://agxldwryrzmhvcuytjoj.supabase.co`
- PostgreSQL: 17.12
- Shared Transaction Pooler host: `aws-0-eu-central-1.pooler.supabase.com`
- Port: `6543`
- Database: `postgres`
- Pooler user: `postgres.agxldwryrzmhvcuytjoj`

The password is not available in this session and is intentionally excluded from repository content. Direct connections use IPv6 by default; the transaction pooler is a candidate for IPv4-only hosting, but must be tested from Faable.

## PostgreSQL work that remains

- Consolidate both modules onto one connection/transaction abstraction while retaining request-scoped lifecycle and consistent row mapping.
- Replace all qmark placeholders with driver-safe binding, and review SQL strings for PostgreSQL type and syntax compatibility.
- Rework SQLite transaction assumptions. PostgreSQL has no direct equivalent of SQLite's `BEGIN IMMEDIATE`; the affected financial flows need explicit row locking or serializable/atomic conditional updates.
- Replace all `PRAGMA table_info` and `sqlite_master` schema inspection with PostgreSQL catalog queries or, preferably, versioned migrations.
- Convert bank schema DDL, including seven SQLite autoincrement declarations, to PostgreSQL identity columns/sequences.
- Convert the store schema and its incremental migrations. Review each `INSERT OR IGNORE` conflict target and each `ON CONFLICT` statement explicitly.
- Preserve double-entry invariants, atomic bank/store transfers, idempotency keys, constraints, and concurrent balance updates.
- Keep the bank and store in one shared PostgreSQL database. Do not create separate databases.
- Build a one-time, resumable data migration from a consistent SQLite backup. Do not rerun seed/bootstrap code in a way that duplicates founder assets, ledger openings, or balances.
- Move uploaded images and payment proofs to durable object storage, retaining authorization and private proof access.

## Storage findings

- `empire_store.py` creates a local `IMAGE_DIR` from `EMPIRE_IMAGE_DIR` and writes uploads there.
- The Dockerfile and compose configuration use a local `/data` volume, but that does not establish that Faable's deployment has a persistent volume.
- No Supabase Storage bucket, object-key schema, signed URL flow, or storage client integration is configured in the inspected repository.

## Release blockers

1. No verified Faable deployment/secret-manager API is available to this session.
2. The Supabase database password and server-side Storage key are not available to the application.
3. No consistent production SQLite backup has been obtained for a data-preserving migration.
4. No production-like PostgreSQL instance is available here to run migration and financial concurrency checks.
5. The current application code remains SQLite-specific; these connection details do not make it PostgreSQL-compatible.

Therefore this audit intentionally does not introduce an untested PostgreSQL compatibility shim, does not change the running database configuration, and does not trigger deployment. The implementation is not complete until these blockers are resolved and the full migration is validated.
