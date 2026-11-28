#!/usr/bin/env python3
"""One-way, non-destructive SQLite -> PostgreSQL data migration for Empire E-Story.

This tool copies the complete shared SQLite database (bank + store) into an EMPTY
PostgreSQL public schema. It never deletes or modifies the SQLite source. It is
not an application database adapter and does not switch production traffic.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.engine import URL, make_url


BATCH_SIZE = 500
CONFIRMATION = "COPY-EMPIRE-SQLITE-TO-POSTGRES"


def database_url(raw: str):
    url = make_url(raw)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise RuntimeError("Target must be PostgreSQL using the psycopg driver")
    if "sslmode" not in url.query:
        url = url.update_query_dict({"sslmode": "require"})
    return url


def normalized(value: Any):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if hasattr(value, "isoformat"):
        return {"__iso__": value.isoformat()}
    if hasattr(value, "as_tuple"):
        return {"__decimal__": str(value)}
    return {"__repr__": repr(value)}


def row_fingerprint(row: dict[str, Any]) -> str:
    payload = {key: normalized(value) for key, value in row.items()}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def digest_rows(connection, table):
    fingerprints = []
    result = connection.execute(select(table))
    for row in result.mappings():
        fingerprints.append(row_fingerprint(dict(row)))
    fingerprints.sort()
    digest = hashlib.sha256()
    for item in fingerprints:
        digest.update(item.encode("ascii"))
    return len(fingerprints), digest.hexdigest()


def source_path() -> Path:
    raw = os.getenv("EMPIRE_SQLITE_SOURCE") or os.getenv(
        "NAB_DATABASE", "national_arab_bank.db"
    )
    if "://" in raw:
        raise RuntimeError(
            "EMPIRE_SQLITE_SOURCE/NAB_DATABASE must point to a SQLite file, not a URL"
        )
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"SQLite source file does not exist: {path}")
    return path


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"--check", "--apply"}:
        print("Usage: python scripts/migrate_sqlite_to_postgres.py --check|--apply")
        return 2

    target_raw = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if not target_raw:
        raise RuntimeError("Set DATABASE_URL or SUPABASE_DATABASE_URL in the environment")

    src_path = source_path()
    source = create_engine(URL.create("sqlite", database=str(src_path)),
                           connect_args={"timeout": 30})
    target = create_engine(database_url(target_raw), pool_pre_ping=True)

    try:
        with source.connect() as src:
            # A read transaction provides a consistent SQLite snapshot for the copy.
            src.exec_driver_sql("BEGIN")
            metadata = MetaData()
            metadata.reflect(bind=src)
            tables = list(metadata.sorted_tables)
            if not tables:
                raise RuntimeError("The SQLite source contains no user tables")

            source_counts = {}
            for table in tables:
                source_counts[table.name] = digest_rows(src, table)

            with target.connect() as dst:
                existing = inspect(dst).get_table_names(schema="public")
                if existing:
                    raise RuntimeError(
                        "Target public schema is not empty; refusing to overwrite or merge. "
                        f"Existing tables: {', '.join(sorted(existing)[:20])}"
                    )

            print(f"Source: {src_path.name}")
            print(f"Tables discovered: {len(tables)}")
            for name, (count, checksum) in source_counts.items():
                print(f"  {name}: {count} rows | sha256 {checksum}")

            if sys.argv[1] == "--check":
                print("CHECK ONLY: no PostgreSQL schema or data was changed.")
                return 0

            if os.getenv("EMPIRE_MIGRATION_CONFIRM") != CONFIRMATION:
                raise RuntimeError(
                    "Apply refused. Set EMPIRE_MIGRATION_CONFIRM to the exact "
                    f"value {CONFIRMATION!r} after taking a protected source backup."
                )

            # PostgreSQL needs identity/serial sequences advanced after explicit IDs
            # are copied. Make reflected foreign keys deferrable for a consistent
            # all-in-one transaction, including tables with circular references.
            for table in tables:
                for constraint in table.foreign_key_constraints:
                    constraint.deferrable = True
                    constraint.initially = "DEFERRED"

            with target.begin() as dst:
                metadata.create_all(dst)
                dst.execute(text("SET CONSTRAINTS ALL DEFERRED"))

                for table in tables:
                    result = src.execute(select(table))
                    batch = []
                    for row in result.mappings():
                        batch.append(dict(row))
                        if len(batch) >= BATCH_SIZE:
                            dst.execute(table.insert(), batch)
                            batch.clear()
                    if batch:
                        dst.execute(table.insert(), batch)

                # Verify every table before committing. A mismatch aborts the
                # transaction, leaving the target schema empty.
                for table in tables:
                    expected_count, expected_digest = source_counts[table.name]
                    actual_count, actual_digest = digest_rows(dst, table)
                    if (actual_count, actual_digest) != (expected_count, expected_digest):
                        raise RuntimeError(
                            f"Verification failed for {table.name}: "
                            f"source={expected_count}/{expected_digest}, "
                            f"target={actual_count}/{actual_digest}"
                        )

                # Reset generated integer primary-key sequences to the copied max.
                inspector = inspect(dst)
                for table in tables:
                    pk = inspector.get_pk_constraint(table.name, schema="public")
                    columns = pk.get("constrained_columns") or []
                    if len(columns) != 1:
                        continue
                    column_name = columns[0]
                    column = table.c.get(column_name)
                    if column is None or not getattr(column.type, "python_type", None) is int:
                        continue
                    sequence = dst.execute(
                        text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                        {"table_name": f'public."{table.name}"',
                         "column_name": column_name},
                    ).scalar_one_or_none()
                    if not sequence:
                        continue
                    maximum = dst.execute(
                        select(__import__("sqlalchemy").func.max(column))
                    ).scalar_one()
                    if maximum is not None:
                        dst.execute(
                            text("SELECT setval(:sequence_name, :value, true)"),
                            {"sequence_name": sequence, "value": maximum},
                        )

        print("MIGRATION AND ROW-CHECKSUM VERIFICATION SUCCEEDED.")
        print("The SQLite source was left unchanged. Production cutover is NOT performed.")
        return 0
    finally:
        source.dispose()
        target.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
