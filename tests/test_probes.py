import os
import subprocess
import tempfile
import unittest

from migcheck import probes
from migcheck.postgres import Dialect, Postgres

from .helpers import have_docker


def col(name, type_name="integer", notnull=False, default=False):
    return name, {"name": name, "type": type_name, "notnull": notnull, "default": default,
                  "generated": False, "identity": False}


def catalog(tables, fks=None, functions=None):
    return {
        "relations": {
            f"public.{name}": {"schema": "public", "name": name, "kind": kind, "columns": dict(cols)}
            for name, (kind, cols) in tables.items()
        },
        "fks": fks or {},
        "functions": functions or {},
        "enums": {},
        "domains": {},
    }


USERS = ("r", [col("id", notnull=True, default=True), col("name", "text", notnull=True), col("bio", "text")])


class GenerateTest(unittest.TestCase):
    def names(self, old, new):
        found, skipped = probes.generate(old, new, Dialect)
        return [(p.name, p.kind) for p in found], skipped

    def test_no_change_no_probes(self):
        self.assertEqual(self.names(catalog({"users": USERS}), catalog({"users": USERS})), ([], []))

    def test_dropped_table_and_column(self):
        old = catalog({"users": USERS, "posts": ("r", [col("id")])})
        new = catalog({"users": ("r", USERS[1][:2])})
        names, _ = self.names(old, new)
        self.assertEqual(names, [("users.bio", "column dropped"), ("posts", "table dropped")])

    def test_new_required_column(self):
        new = catalog({"users": ("r", [*USERS[1], col("plan", "text", notnull=True)])})
        found, _ = probes.generate(catalog({"users": USERS}), new, Dialect)
        self.assertEqual([(p.name, p.kind) for p in found], [("users.plan", "new column requires a value")])
        self.assertEqual(found[0].statements, ["""INSERT INTO "public"."users" ("name") VALUES ((md5(random()::text))::text)"""])

    def test_new_column_with_a_default_needs_no_probe(self):
        new = catalog({"users": ("r", [*USERS[1], col("plan", "text", notnull=True, default=True)])})
        self.assertEqual(self.names(catalog({"users": USERS}), new), ([], []))

    def run_probe(self, found, error):
        """Returns the findings when the probes pass on the old schema and the
        first statement fails on the new one with `error`."""
        new = [error] + [(None, None)] * (sum(len(p.statements) for p in found) - 1)
        return probes.findings(found, [(None, None)] * len(new), new, set(), Postgres.cause)[0]

    def test_column_made_required(self):
        new = catalog({"users": ("r", [*USERS[1][:2], col("bio", "text", notnull=True)])})
        found, _ = probes.generate(catalog({"users": USERS}), new, Dialect)
        results = self.run_probe(found, ("23502", 'null value in column "bio" of relation "users" violates not-null constraint'))
        self.assertEqual([(f.probe.name, f.probe.kind) for f in results], [("users.bio", "column no longer accepts NULL")])

    def test_column_that_lost_its_default(self):
        old = catalog({"users": ("r", [*USERS[1], col("status", "text", notnull=True, default=True)])})
        new = catalog({"users": ("r", [*USERS[1], col("status", "text", notnull=True)])})
        found, _ = probes.generate(old, new, Dialect)
        self.assertEqual([(p.name, p.kind) for p in found], [("users.status", "column lost its default")])

    def test_new_column_comes_before_a_column_made_required(self):
        old = catalog({"users": USERS})
        new = catalog({"users": ("r", [*USERS[1][:2], col("bio", "text", notnull=True), col("plan", "text", notnull=True)])})
        found, _ = probes.generate(old, new, Dialect)
        # The probe sets bio, which the base release might always set, so only plan can fail.
        self.assertEqual(found[0].statements, [
            """INSERT INTO "public"."users" ("name", "bio") VALUES ((md5(random()::text))::text, (md5(random()::text))::text)"""
        ])
        results = self.run_probe(found, ("23502", 'null value in column "plan" of relation "users" violates not-null constraint'))
        self.assertEqual([(f.probe.name, f.probe.kind, f.others) for f in results],
                         [("users.plan", "new column requires a value", [("bio", "column no longer accepts NULL")])])

    def test_sets_a_foreign_key_made_required_through_its_parent(self):
        posts = [col("id", notnull=True, default=True), col("team_id")]
        fks = {"public.posts": [{"table": "public.posts", "ref": "public.users",
                                 "columns": ["team_id"], "ref_columns": ["id"]}]}
        old = catalog({"users": USERS, "posts": ("r", posts)}, fks)
        new = catalog({"users": USERS, "posts": ("r", [posts[0], col("team_id", notnull=True),
                                                       col("plan", "text", notnull=True)])}, fks)
        found, _ = probes.generate(old, new, Dialect)
        self.assertEqual(found[0].statements, [
            """WITH p0 AS (INSERT INTO "public"."users" ("name") VALUES ((md5(random()::text))::text) RETURNING "id") """
            """INSERT INTO "public"."posts" ("team_id") SELECT p0."id" FROM p0"""
        ])

    def test_leaves_out_columns_it_cant_make_a_value_for(self):
        old = catalog({"users": ("r", [*USERS[1], col("geo", "geometry")])})
        new = catalog({"users": ("r", [*USERS[1], col("geo", "geometry", notnull=True), col("plan", "text", notnull=True)])})
        found, skipped = probes.generate(old, new, Dialect)
        self.assertEqual(skipped, [])
        self.assertEqual([p.name for p in found], ["users.plan"])

    def test_a_parent_tables_own_probe_names_its_failure(self):
        posts = [col("id", notnull=True, default=True), col("user_id", notnull=True)]
        fks = {"public.posts": [{"table": "public.posts", "ref": "public.users",
                                 "columns": ["user_id"], "ref_columns": ["id"]}]}
        old = catalog({"users": USERS, "posts": ("r", posts)}, fks)
        new = catalog({"users": ("r", [*USERS[1], col("plan", "text", notnull=True)]),
                       "posts": ("r", [*posts, col("plan", "text", notnull=True)])}, fks)
        found, _ = probes.generate(old, new, Dialect)
        self.assertEqual([p.name for p in found], ["users.plan", "posts.plan"])
        error = ("23502", 'null value in column "plan" of relation "users" violates not-null constraint')
        # Both probes fail on users.plan: the posts probe in its parent row.
        results, _ = probes.findings(list(reversed(found)), [(None, None)] * 2, [error, error], set(), Postgres.cause)
        self.assertEqual([(f.probe.name, f.probe.kind) for f in results], [("users.plan", "new column requires a value")])

    def test_skips_references_to_tables_the_catalog_leaves_out(self):
        tables = {"events": ("r", [col("id", notnull=True, default=True), col("part_id", notnull=True)])}
        fks = {"public.events": [{"table": "public.events", "ref": "public.parts_1",
                                  "columns": ["part_id"], "ref_columns": ["id"]}]}
        new = catalog({"events": ("r", [*tables["events"][1], col("x", "text", notnull=True)])}, fks)
        found, skipped = probes.generate(catalog(tables, fks), new, Dialect)
        self.assertEqual(found, [])
        self.assertEqual(skipped, [("events.x", "it references parts_1, which migcheck doesn't read, such as a partition")])

    def test_names_relations_in_other_schemas(self):
        old = catalog({})
        old["relations"]["app.daily_stats"] = {"schema": "app", "name": "daily_stats", "kind": "m", "columns": {}}
        new = catalog({})
        new["relations"]["app.daily_stats"] = {"schema": "app", "name": "daily_stats", "kind": "v", "columns": {}}
        found, _ = probes.generate(old, new, Dialect)
        self.assertEqual([(p.name, p.subject) for p in found], [("app.daily_stats", (None, "daily_stats"))])

    def test_insert_creates_required_parent_rows(self):
        tables = {"users": USERS, "posts": ("r", [col("id", notnull=True, default=True),
                                                  col("user_id", notnull=True), col("editor_id")])}
        fks = {"public.posts": [
            {"table": "public.posts", "ref": "public.users", "columns": ["user_id"], "ref_columns": ["id"]},
            {"table": "public.posts", "ref": "public.users", "columns": ["editor_id"], "ref_columns": ["id"]},
        ]}
        statements = Dialect.insert(probes.plan(catalog(tables, fks), "public.posts", Dialect))
        self.assertEqual(statements, [
            """WITH p0 AS (INSERT INTO "public"."users" ("name") VALUES ((md5(random()::text))::text) RETURNING "id") """
            """INSERT INTO "public"."posts" ("user_id") SELECT p0."id" FROM p0"""
        ])

    def test_skips_cycles(self):
        tables = {"nodes": ("r", [col("id", notnull=True, default=True), col("parent_id", notnull=True)])}
        fks = {"public.nodes": [{"table": "public.nodes", "ref": "public.nodes",
                                 "columns": ["parent_id"], "ref_columns": ["id"]}]}
        new = catalog({"nodes": ("r", [*tables["nodes"][1], col("x", notnull=True)])}, fks)
        found, skipped = probes.generate(catalog(tables, fks), new, Dialect)
        self.assertEqual(found, [])
        self.assertEqual(skipped, [("nodes.x", "its required foreign keys form a cycle")])

    def test_dropped_function(self):
        funcs = {"public.score(integer, text)": {"schema": "public", "name": "score", "args": ["integer", "text"]}}
        found, _ = probes.generate(catalog({}, functions=funcs), catalog({}), Dialect)
        self.assertEqual(found[0].sql, 'SELECT "public"."score"(NULL::integer, NULL::text)')


class SearchTest(unittest.TestCase):
    def test_singular(self):
        self.assertEqual([probes._singular(w) for w in ("users", "entries", "addresses", "batches", "status", "data")],
                         ["user", "entry", "address", "batch", "statu", "data"])

    def test_finds_mentions_outside_migrations(self):
        with tempfile.TemporaryDirectory() as root:
            files = {
                "app/models/user.rb": "class User\n  attr :skip_thread_containment",
                "src/user.ts": "users.skipThreadContainment = true",
                "test/user_test.rb": "User skip_thread_containment: true",
                "app/post.rb": "skip_thread_containment",
                "db/migrate/1_add.rb": "add_column :users, :skip_thread_containment",
                "node_modules/x/index.js": "skip_thread_containment",
                "app/other.rb": "skip_thread_containments",
                "CHANGELOG.md": "users: dropped skip_thread_containment",
            }
            for path, text in files.items():
                os.makedirs(os.path.join(root, os.path.dirname(path)), exist_ok=True)
                with open(os.path.join(root, path), "w") as f:
                    f.write(text)
            finding = probes.Finding(probes.Probe("users.skip_thread_containment", "column dropped", [""]), "", "")
            probes.search(root, [finding])
            self.assertEqual(finding.mentions, 3)
            # In a git checkout, files the workload generated don't count.
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(["git", "-C", root, "add", "app", "test", "db"], check=True)
            probes.search(root, [finding])
            self.assertEqual(finding.files, ["app/models/user.rb", "test/user_test.rb"])
        self.assertEqual(finding.mentions, 2)


@unittest.skipUnless(have_docker(), "needs Docker")
class PostgresProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Postgres()
        try:
            cls.db.start()
        except Exception:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()

    def test_finds_untested_breaks(self):
        db = self.db
        db.create_db("base")
        db._psql("base", """
            CREATE TYPE mood AS ENUM ('happy', 'sad');
            CREATE TABLE users (id serial PRIMARY KEY, name varchar(20) NOT NULL, mood mood NOT NULL,
                                created timestamptz NOT NULL, bio text);
            CREATE TABLE posts (id bigserial PRIMARY KEY, user_id int NOT NULL REFERENCES users,
                                body text NOT NULL, tags text[] NOT NULL);
            CREATE TABLE notes (id serial PRIMARY KEY, body text);
            CREATE TABLE settings (id serial PRIMARY KEY, data jsonb NOT NULL);
            CREATE MATERIALIZED VIEW summary AS SELECT count(*) AS n FROM users;
            CREATE FUNCTION score(a int) RETURNS int AS 'SELECT a' LANGUAGE sql;
        """)
        db.create_db("old", template="base")
        db.create_db("new", template="base")
        db._psql("new", """
            ALTER TABLE users DROP COLUMN bio;
            ALTER TABLE posts ADD COLUMN slug text NOT NULL;
            ALTER TABLE settings ADD COLUMN kind text NOT NULL DEFAULT 'x';
            DROP TABLE notes;
            DROP MATERIALIZED VIEW summary;
            CREATE TABLE summary (n bigint);
            DROP FUNCTION score(int);
            CREATE FUNCTION score(a int, b int) RETURNS int AS 'SELECT a + b' LANGUAGE sql;
        """)
        found, skipped = probes.generate(db.catalog("old"), db.catalog("new"), Dialect)
        self.assertEqual(skipped, [])
        statements = probes.as_statements(found, 0)
        old, new = db.replay("old", statements), db.replay("new", statements)
        results, unchecked = probes.findings(found, old, new, set(), Postgres.cause)
        self.assertEqual(unchecked, [])
        self.assertEqual(sorted(f.probe.name for f in results),
                         ["notes", "posts.slug", "score()", "summary", "users.bio"])


class MySQLDialectTest(unittest.TestCase):
    def test_insert_sets_parent_keys_through_variables(self):
        from migcheck.mysql import Dialect as MyDialect

        rows = [
            probes.Row("", "users", {"name": ("literal", "MD5(RAND())")}, ["id"]),
            probes.Row("", "teams", {"code": ("literal", "LEFT(MD5(RAND()), 8)")}, ["code"]),
            probes.Row("", "posts", {"user_id": ("ref", 0, "id"), "team_code": ("ref", 1, "code")}),
        ]
        self.assertEqual(MyDialect.insert(rows), [
            "INSERT INTO `users` (`name`) VALUES (MD5(RAND()))",
            "SET @mc0_0 = LAST_INSERT_ID()",
            "SET @mc1_0 = LEFT(MD5(RAND()), 8)",
            "INSERT INTO `teams` (`code`) VALUES (@mc1_0)",
            "INSERT INTO `posts` (`user_id`, `team_code`) VALUES (@mc0_0, @mc1_0)",
        ])

    def test_parents_set_keys_that_a_default_fills(self):
        from migcheck.mysql import Dialect as MyDialect

        tables = {
            "groups": ("r", [col("code", "char(8)", notnull=True, default=True)]),
            "teams": ("r", [col("code", "char(8)", notnull=True)]),
            "members": ("r", [col("id", "int", notnull=True, default=True), col("team_code", "char(8)", notnull=True)]),
        }
        tables["members"][1][0][1]["identity"] = True
        fks = {
            ".teams": [{"table": ".teams", "ref": ".groups", "columns": ["code"], "ref_columns": ["code"]}],
            ".members": [{"table": ".members", "ref": ".teams", "columns": ["team_code"], "ref_columns": ["code"]}],
        }
        cat = catalog(tables, fks)
        cat["relations"] = {f".{k.split('.', 1)[1]}": {**v, "schema": ""} for k, v in cat["relations"].items()}
        self.assertEqual(MyDialect.insert(probes.plan(cat, ".members", MyDialect)), [
            "SET @mc0_0 = LEFT(MD5(RAND()), 8)",
            "INSERT INTO `groups` (`code`) VALUES (@mc0_0)",
            "SET @mc1_0 = @mc0_0",
            "INSERT INTO `teams` (`code`) VALUES (@mc0_0)",
            "INSERT INTO `members` (`team_code`) VALUES (@mc1_0)",
        ])

    def test_skips_parent_keys_the_database_generates(self):
        from migcheck.mysql import Dialect as MyDialect

        tables = {"teams": ("r", [col("code", "char(8)", notnull=True)]),
                  "members": ("r", [col("team_code", "char(8)", notnull=True)])}
        tables["teams"][1][0][1]["generated"] = True
        fks = {".members": [{"table": ".members", "ref": ".teams", "columns": ["team_code"], "ref_columns": ["code"]}]}
        cat = catalog(tables, fks)
        cat["relations"] = {f".{k.split('.', 1)[1]}": {**v, "schema": ""} for k, v in cat["relations"].items()}
        with self.assertRaisesRegex(probes.ProbeError, "parent key teams.code is generated"):
            probes.plan(cat, ".members", MyDialect)

    def test_literals(self):
        from migcheck.mysql import Dialect as MyDialect

        cases = {
            "int unsigned": "FLOOR(1 + RAND() * 2000000000)", "tinyint(1)": "FLOOR(1 + RAND() * 100)",
            "varchar(191)": "LEFT(MD5(RAND()), 191)", "binary(16)": "LEFT(UNHEX(MD5(RAND())), 16)",
            "datetime(6)": "NOW()", "enum('Draft','published')": "'Draft'", "json": "JSON_OBJECT()",
            "ENUM('İstanbul','x')": "'İstanbul'",
            "geometry": None,
        }
        for type_name, want in cases.items():
            self.assertEqual(MyDialect.literal(type_name, {}), want, type_name)


@unittest.skipUnless(have_docker(), "needs Docker")
class MySQLProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from migcheck.mysql import MySQL

        cls.db = MySQL()
        try:
            cls.db.start()
        except Exception:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()

    def test_finds_untested_breaks(self):
        from migcheck.mysql import MySQL

        db = self.db
        base = """
            CREATE TABLE users (id int AUTO_INCREMENT PRIMARY KEY, name varchar(20) NOT NULL,
                                status enum('active','gone') NOT NULL, created datetime NOT NULL, bio text);
            CREATE TABLE teams (code char(8) PRIMARY KEY, title text NOT NULL);
            CREATE TABLE posts (id bigint AUTO_INCREMENT PRIMARY KEY, user_id int NOT NULL,
                                team_code char(8) NOT NULL, body text NOT NULL,
                                FOREIGN KEY (user_id) REFERENCES users (id),
                                FOREIGN KEY (team_code) REFERENCES teams (code));
            CREATE TABLE notes (id int AUTO_INCREMENT PRIMARY KEY, body text);
            CREATE TABLE settings (id int AUTO_INCREMENT PRIMARY KEY, data json NOT NULL);
            CREATE FUNCTION score(a int) RETURNS int DETERMINISTIC RETURN a;
        """
        for name in ("old", "new"):
            db.create_db(name)
            _run_mysql(db, name, base)
        _run_mysql(db, "new", """
            ALTER TABLE users DROP COLUMN bio;
            ALTER TABLE posts ADD COLUMN slug varchar(50) NOT NULL;
            ALTER TABLE settings ADD COLUMN kind varchar(10) NOT NULL DEFAULT 'x';
            DROP TABLE notes;
            DROP FUNCTION score;
        """)
        found, skipped = probes.generate(db.catalog("old"), db.catalog("new"), db.dialect)
        self.assertEqual(skipped, [])
        statements = probes.as_statements(found, 0)
        old, new = db.replay("old", statements), db.replay("new", statements)
        results, unchecked = probes.findings(found, old, new, set(), MySQL.cause)
        self.assertEqual(unchecked, [])
        self.assertEqual(sorted(f.probe.name for f in results), ["notes", "posts.slug", "score()", "users.bio"])


def _run_mysql(db, name, sql):
    from .helpers import mysql_stdin

    mysql_stdin(db, name, sql.encode())
