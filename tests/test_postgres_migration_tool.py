import importlib.util
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sqlite_to_postgres.py"
SPEC = importlib.util.spec_from_file_location("empire_pg_migration", MODULE_PATH)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class PostgreSQLMigrationUtilityTests(unittest.TestCase):
    def test_row_fingerprint_is_stable_across_mapping_order(self):
        first = {"username": "alice", "balance": 120, "active": True}
        second = {"active": True, "balance": 120, "username": "alice"}
        self.assertEqual(
            migration.row_fingerprint(first),
            migration.row_fingerprint(second),
        )

    def test_digest_is_order_independent_and_counts_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.db"
            engine = create_engine(f"sqlite:///{path}")
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)"))
                connection.execute(
                    text("INSERT INTO entries (id, value) VALUES (1, 'a'), (2, 'b'), (3, 'c')")
                )
            with engine.connect() as connection:
                table = migration.MetaData()
                table.reflect(bind=connection)
                entries = table.tables["entries"]
                expected = migration.digest_rows(connection, entries)
                connection.execute(text("SELECT 1"))
                actual = migration.digest_rows(connection, entries)
                self.assertEqual(expected, actual)
                self.assertEqual(expected[0], 3)
            engine.dispose()

    def test_sqlite_source_url_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "readonly.db"
            writable = create_engine(f"sqlite:///{path}")
            with writable.begin() as connection:
                connection.execute(text("CREATE TABLE sample (id INTEGER PRIMARY KEY)"))
            writable.dispose()

            read_only_url = URL.create(
                "sqlite",
                database=f"file:{path.resolve()}",
                query={"mode": "ro", "uri": "true"},
            )
            read_only = create_engine(read_only_url)
            with read_only.connect() as connection:
                with self.assertRaises(Exception):
                    connection.execute(text("INSERT INTO sample (id) VALUES (1)"))
            read_only.dispose()

    def test_postgres_url_enforces_tls_and_psycopg(self):
        url = migration.database_url(
            "postgresql://postgres:example@localhost:5432/postgres"
        )
        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual(url.query.get("sslmode"), "require")


if __name__ == "__main__":
    unittest.main()
