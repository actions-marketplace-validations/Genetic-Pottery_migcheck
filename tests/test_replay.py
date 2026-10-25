"""Replays statements against real databases in Docker.

These tests start a Postgres and a MySQL container, so they take a minute.
They're skipped when Docker isn't available.
"""

import unittest

from migcheck.common import MIGRATED_KEY, Statement
from migcheck.mysql import MySQL
from migcheck.postgres import Postgres

from .helpers import have_docker, mysql_stdin

OK = (None, None)


@unittest.skipUnless(have_docker(), "needs Docker")
class PostgresReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Postgres()
        try:
            cls.db.start()
            cls.db.create_db("base")
            cls.db._psql("base", """
                CREATE TABLE users (id serial PRIMARY KEY, email text UNIQUE);
                CREATE TABLE posts (id serial PRIMARY KEY, user_id int REFERENCES users DEFERRABLE INITIALLY DEFERRED);
            """)
        except Exception:
            cls.db.stop()
            raise
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()

    def replay(self, statements, setup=None, migration=None):
        # Replay sets up its own schema, so each call needs a fresh database.
        type(self).count += 1
        name = f"replay{self.count}"
        self.db.create_db(name, template="base")
        if setup:
            self.db._psql(name, setup)
        mark = None
        if migration:
            mark = self.db.mark()
            self.db._psql(name, migration)
        return self.db.replay(name, statements, mark)

    def test_reports_each_failure(self):
        results = self.replay([
            Statement("SELECT 1", 0, True),
            Statement("SELECT missing FROM users", 1, True),
            Statement("SELECT 2", 1, True),
        ])
        self.assertEqual(results[0], OK)
        self.assertEqual(results[1], ("42703", 'column "missing" does not exist'))
        self.assertEqual(results[2], OK)

    def test_later_statements_see_earlier_writes(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, True),
            Statement("INSERT INTO users (email) VALUES ('a')", 1, True),
        ])
        self.assertEqual(results[0], OK)
        self.assertEqual(results[1][0], "23505")

    def test_rolled_back_blocks_leave_no_rows(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, False),
            Statement("INSERT INTO users (email) VALUES ('a')", 1, True),
        ])
        self.assertEqual(results, [OK, OK])

    def test_pins_inserted_ids(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, True, [["users", 42]]),
            Statement("INSERT INTO posts (user_id) VALUES (42)", 1, True),
        ])
        self.assertEqual(results, [OK, OK])

    def test_skips_pinning_ids_that_a_migration_took(self):
        # A migration that inserts rows uses ids the workload also got.
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, True, [["users", 1]]),
        ], setup="INSERT INTO users (email) VALUES ('from-migration');")
        self.assertEqual(results, [OK])

    def test_explicit_ids_still_collide(self):
        results = self.replay([
            Statement("INSERT INTO users (id, email) VALUES (1, 'a')", 0, True, [["users", 1]]),
        ], setup="INSERT INTO users (email) VALUES ('from-migration');")
        self.assertEqual(results[0][0], "23505")

    def test_labels_keys_that_a_migration_inserted(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('seeded')", 0, True),
        ], migration="INSERT INTO users (email) VALUES ('seeded');")
        self.assertEqual(results[0][0], "23505")
        self.assertTrue(results[0][1].endswith(MIGRATED_KEY))

    def test_does_not_label_keys_from_before_the_migrations(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('old')", 0, True),
        ], setup="INSERT INTO users (email) VALUES ('old');", migration="SELECT 1;")
        self.assertEqual(results[0], ("23505", 'duplicate key value violates unique constraint "users_email_key"'))

    def test_does_not_label_keys_that_replay_inserted(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('twice')", 0, True),
            Statement("INSERT INTO users (email) VALUES ('twice')", 1, True),
        ], migration="SELECT 1;")
        self.assertEqual(results[1], ("23505", 'duplicate key value violates unique constraint "users_email_key"'))

    def test_labels_keys_after_a_rolled_back_block(self):
        # Test runners often roll back the first transaction.
        results = self.replay([
            Statement("SELECT 1", 0, False),
            Statement("INSERT INTO users (email) VALUES ('seeded')", 1, True),
        ], migration="INSERT INTO users (email) VALUES ('seeded');")
        self.assertTrue(results[1][1].endswith(MIGRATED_KEY))

    def test_labels_keys_after_a_session_reset(self):
        results = self.replay([
            Statement("RESET ALL", 0, True),
            Statement("INSERT INTO users (email) VALUES ('seeded')", 1, True),
        ], migration="INSERT INTO users (email) VALUES ('seeded');")
        self.assertTrue(results[1][1].endswith(MIGRATED_KEY))

    def test_session_resets_undo_the_apps_role(self):
        # A pool resets each connection that the app returns, so one
        # session's role doesn't carry over to the next.
        results = self.replay([
            Statement("SET ROLE replay_tenant", 0, True),
            Statement("SET SESSION AUTHORIZATION DEFAULT; RESET ALL", 0, True),
            Statement("INSERT INTO users (email) VALUES ('after')", 1, True),
        ], setup="DO $$ BEGIN CREATE ROLE replay_tenant; EXCEPTION WHEN duplicate_object THEN NULL; END $$;")
        self.assertEqual(results, [OK, OK, OK])

    def test_checking_a_key_leaves_no_rows(self):
        # The second row collides. Checking it runs the insert again, and the
        # first row of that run must not stay.
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a'), ('seeded')", 0, True),
            Statement("INSERT INTO users (email) VALUES ('a')", 0, True),
        ], migration="INSERT INTO users (email) VALUES ('seeded');")
        self.assertTrue(results[0][1].endswith(MIGRATED_KEY))
        self.assertEqual(results[1], OK)

    def test_deferred_foreign_keys_fail_their_statement(self):
        results = self.replay([
            Statement("INSERT INTO posts (user_id) VALUES (999)", 0, True),
            Statement("SELECT 1", 1, True),
        ])
        self.assertEqual(results[0][0], "23503")
        self.assertEqual(results[1], OK)

    def test_truncate_skips_missing_tables(self):
        results = self.replay([
            Statement("SELECT _migcheck.truncate(ARRAY['users', 'gone']::text[], True)", 0, True),
        ])
        self.assertEqual(results, [OK])

    def test_schema_tracks_migrations(self):
        self.db.create_db("empty")
        self.db.create_db("same1", template="base")
        self.db.create_db("same2", template="base")
        self.db.create_db("changed", template="base")
        self.db._psql("changed", "ALTER TABLE users ADD COLUMN name text;")
        self.assertFalse(self.db.has_tables("empty"))
        self.assertTrue(self.db.has_tables("same1"))
        self.assertEqual(self.db.schema("same1"), self.db.schema("same2"))
        self.assertNotEqual(self.db.schema("same1"), self.db.schema("changed"))

    def test_statements_containing_dollar_quotes(self):
        results = self.replay([Statement("SELECT $$it's$$, $x$ $mc_ $x$", 0, True)])
        self.assertEqual(results, [OK])


@unittest.skipUnless(have_docker(), "needs Docker")
class MySQLReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = MySQL()
        try:
            cls.db.start()
            cls.db.create_db("base")
            mysql_stdin(cls.db, "base", b"""
                CREATE TABLE users (id int AUTO_INCREMENT PRIMARY KEY, email varchar(50) UNIQUE);
                CREATE TABLE blobs (data varbinary(10));
            """)
        except Exception:
            cls.db.stop()
            raise
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()

    def replay(self, statements):
        type(self).count += 1
        name = f"replay{self.count}"
        self.db.create_db(name, template="base")
        return self.db.replay(name, statements)

    def test_maps_errors_to_their_statements(self):
        results = self.replay([
            Statement("SELECT 1", 0, True),
            Statement("SELECT\n  missing\nFROM users", 1, True),
            Statement("SELECT 2 -- trailing comment", 1, True),
            Statement("SELECT * FROM gone", 2, True),
        ])
        self.assertEqual(results[0], OK)
        self.assertEqual(results[1][0], "1054")
        self.assertEqual(results[2], OK)
        self.assertEqual(results[3][0], "1146")

    def test_rolled_back_blocks_leave_no_rows(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, False),
            Statement("INSERT INTO users (email) VALUES ('a')", 1, True),
            Statement("INSERT INTO users (email) VALUES ('a')", 2, True),
        ])
        self.assertEqual(results[:2], [OK, OK])
        self.assertEqual(results[2][0], "1062")

    def test_truncate_skips_missing_tables(self):
        results = self.replay([
            Statement("CALL _migcheck_truncate('users')", 0, True),
            Statement("CALL _migcheck_truncate('gone')", 1, True),
        ])
        self.assertEqual(results, [OK, OK])

    def test_schema_tracks_migrations(self):
        self.db.create_db("empty")
        self.db.create_db("same1", template="base")
        self.db.create_db("same2", template="base")
        self.db.create_db("changed", template="base")
        mysql_stdin(self.db, "changed", b"ALTER TABLE users ADD COLUMN name text;")
        # Rows move the auto-increment counter, which isn't part of the schema.
        mysql_stdin(self.db, "same2", b"INSERT INTO users (email) VALUES ('a');")
        self.assertFalse(self.db.has_tables("empty"))
        self.assertTrue(self.db.has_tables("same1"))
        self.assertEqual(self.db.schema("same1"), self.db.schema("same2"))
        self.assertNotEqual(self.db.schema("same1"), self.db.schema("changed"))

    def test_binary_values(self):
        results = self.replay([Statement("INSERT INTO blobs VALUES (_binary '\udcff\udcfe')", 0, True)])
        self.assertEqual(results, [OK])


if __name__ == "__main__":
    unittest.main()
