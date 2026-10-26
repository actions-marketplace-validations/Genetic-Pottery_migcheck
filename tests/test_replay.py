"""Replays statements against real databases in Docker.

These tests start a Postgres and a MySQL container, so they take a minute.
They're skipped when Docker isn't available.
"""

import subprocess
import unittest

from migcheck.common import MIGRATED_KEY, Statement
from migcheck.mysql import MySQL
from migcheck.postgres import Postgres

from .helpers import have_docker, mysql_stdin

OK = (None, None)
# A role that row-level security limits to its own notes.
ROLE_SETUP = """
DO $$ BEGIN CREATE ROLE replay_user; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE TABLE notes (id serial PRIMARY KEY, owner text NOT NULL);
ALTER TABLE notes ENABLE ROW LEVEL SECURITY;
CREATE POLICY own ON notes USING (owner = current_user) WITH CHECK (owner = current_user);
GRANT ALL ON notes, users TO replay_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO replay_user;
"""


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

    def test_switched_roles_get_row_level_security(self):
        # PostgREST switches to the signed-in user's role in each request's
        # transaction, and the role ends with it.
        results = self.replay([
            Statement("SELECT set_config('role', 'replay_user', true)", 0, True),
            Statement("INSERT INTO notes (owner) VALUES ('someone else')", 0, True),
            Statement("INSERT INTO notes (owner) VALUES ('someone else')", 1, True),
        ], setup=ROLE_SETUP)
        self.assertEqual(results[0], OK)
        self.assertEqual(results[1], ("42501", 'new row violates row-level security policy for table "notes"'))
        self.assertEqual(results[2], OK)

    def test_labels_keys_that_a_migration_inserted_under_a_switched_role(self):
        results = self.replay([
            Statement("SELECT set_config('role', 'replay_user', true)", 0, True),
            Statement("INSERT INTO users (email) VALUES ('seeded')", 0, True),
        ], setup=ROLE_SETUP, migration="INSERT INTO users (email) VALUES ('seeded');")
        self.assertTrue(results[1][1].endswith(MIGRATED_KEY))

    def test_pins_ids_that_row_level_security_hides(self):
        # The row with id 1 is hidden from the role, so pinning must check
        # for it as the superuser.
        results = self.replay([
            Statement("SELECT set_config('role', 'replay_user', true)", 0, True),
            Statement("INSERT INTO notes (owner) VALUES (current_user)", 0, True, [["notes", 1]]),
        ], setup=ROLE_SETUP + "INSERT INTO notes (owner) VALUES ('someone else');")
        self.assertEqual(results, [OK, OK])

    def test_a_role_set_for_the_session_ends_with_its_block(self):
        # Blocks from different sessions replay in turn, so the next block
        # runs as the superuser, which row-level security doesn't limit.
        results = self.replay([
            Statement("SET ROLE replay_user", 0, True),
            Statement("INSERT INTO notes (owner) VALUES ('someone else')", 1, True),
        ], setup=ROLE_SETUP)
        self.assertEqual(results, [OK, OK])

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


# An image like Supabase's: postgres isn't a superuser, the init script sets
# up the postgres database and turns logging off for a role, and the image's
# command configures the server.
SET_UP_IMAGE = "migcheck-test-set-up-image"
SET_UP_DOCKERFILE = """
FROM postgres:17-alpine
ENV POSTGRES_USER=mc_admin POSTGRES_DB=postgres
RUN printf '%s\\n' \\
  "CREATE ROLE postgres LOGIN PASSWORD 'migcheck';" \\
  "CREATE SCHEMA auth AUTHORIZATION postgres;" \\
  "CREATE TABLE auth.users (id int PRIMARY KEY);" \\
  "ALTER TABLE auth.users OWNER TO postgres;" \\
  "CREATE ROLE auth_server LOGIN;" \\
  "ALTER ROLE auth_server SET log_statement = none;" \\
  "ALTER ROLE auth_server IN DATABASE postgres SET log_statement = none;" \\
  "ALTER DATABASE postgres SET log_statement = none;" \\
  "ALTER ROLE auth_server SET log_min_messages = fatal;" \\
  > /docker-entrypoint-initdb.d/set-up.sql
CMD ["postgres", "-c", "shared_preload_libraries=pg_stat_statements"]
"""


@unittest.skipUnless(have_docker(), "needs Docker")
class SetUpImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["docker", "build", "-q", "-t", SET_UP_IMAGE, "-"],
                       input=SET_UP_DOCKERFILE, text=True, capture_output=True, check=True)
        cls.db = Postgres(SET_UP_IMAGE)
        try:
            cls.db.start()
        except Exception:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()
        subprocess.run(["docker", "rmi", "-f", SET_UP_IMAGE], capture_output=True)

    def test_keeps_the_images_server_command(self):
        setting = self.db._psql("postgres", "COPY (SELECT current_setting('shared_preload_libraries')) TO STDOUT;")
        self.assertEqual(setting.strip(), "pg_stat_statements")

    def test_logs_every_role(self):
        self.db.record()
        settings = self.db._psql("postgres", "COPY (SELECT count(*) FROM pg_db_role_setting, unnest(setconfig) c "
                                             "WHERE c LIKE 'log%') TO STDOUT;")
        self.assertEqual(settings.strip(), "0")

    def test_runs_as_the_images_superuser(self):
        self.assertEqual(self.db.admin, "mc_admin")

    def test_databases_start_from_the_postgres_database(self):
        self.db.create_db("app")
        owner = self.db._psql("app", "COPY (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'app') TO STDOUT;")
        self.assertEqual(owner.strip(), "postgres")
        self.assertFalse(self.db.has_tables("app"))
        self.db._psql("app", "SET ROLE postgres; CREATE TABLE todos (id serial PRIMARY KEY, user_id int REFERENCES auth.users);")
        self.assertTrue(self.db.has_tables("app"))

    def test_replays(self):
        self.db.create_db("replayed")
        self.db._psql("replayed", "INSERT INTO auth.users VALUES (1);")
        results = self.db.replay("replayed", [Statement("SELECT id FROM auth.users", 0, True)])
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
