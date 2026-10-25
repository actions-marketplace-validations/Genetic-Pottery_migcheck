import csv
import io
import unittest

from migcheck import postgres
from migcheck.postgres import TRUNCATE, _inline_params, _lenient_truncate, parse_log

from .helpers import blocks, fixture


def log(*rows):
    """Builds a csvlog from (database, session, message[, detail[, severity]])
    tuples, with the other columns left empty."""
    out = io.StringIO()
    writer = csv.writer(out)
    for db, session, message, *rest in rows:
        detail = rest[0] if rest else ""
        severity = rest[1] if len(rest) > 1 else "LOG"
        row = [""] * 23
        row[postgres.COL_DATABASE] = db
        row[postgres.COL_SESSION] = session
        row[postgres.COL_SEVERITY] = severity
        row[postgres.COL_MESSAGE] = message
        row[postgres.COL_DETAIL] = detail
        writer.writerow(row)
    out.seek(0)
    return out


class ParseCapturedLogTest(unittest.TestCase):
    """Parses a log recorded from a real Postgres server by
    tests/capture_fixtures.py."""

    def setUp(self):
        with open(fixture("postgres.csv"), newline="", encoding="utf-8") as f:
            self.statements = parse_log(f, exclude={"mc_template"})

    def test_blocks(self):
        self.assertEqual(blocks(self.statements), [
            (True, ["INSERT INTO users (name) VALUES ('ada');"]),
            (True, [
                "INSERT INTO users (name) VALUES ('grace'), ('alan');",
                "UPDATE users SET name = 'Grace' WHERE name = 'grace';",
            ]),
            (False, ["DELETE FROM users;"]),
            (True, ["SELECT name, '$1' AS literal FROM users WHERE name = 'O''Brien'"]),
            (True, ["INSERT INTO posts (user_id, body) VALUES ('1', 'hello')"]),
            (True, ["SELECT missing FROM users;"]),
            # Replay ignores savepoints, so the rolled-back insert stays.
            (True, ["INSERT INTO users (name) VALUES ('tmp');"]),
            (True, ["SELECT _migcheck.truncate(ARRAY['users', 'posts']::text[], True)"]),
            # The session exited with this transaction open.
            (False, ["INSERT INTO users (name) VALUES ('never');"]),
        ])

    def test_row_ids(self):
        ids = [s.ids for s in self.statements]
        self.assertEqual(ids[0], [["users", 1]])
        self.assertEqual(ids[1], [["users", 2], ["users", 3]])
        self.assertEqual(ids[2], [])
        self.assertEqual(ids[5], [["posts", 1]])

    def test_block_numbers_are_contiguous(self):
        self.assertEqual(sorted({s.block for s in self.statements}), list(range(9)))


class ParseLogTest(unittest.TestCase):
    def test_interleaved_sessions_get_separate_blocks(self):
        statements = parse_log(log(
            ("app", "a", "statement: BEGIN"),
            ("app", "b", "statement: BEGIN"),
            ("app", "a", "statement: SELECT 'a1'"),
            ("app", "b", "statement: SELECT 'b1'"),
            ("app", "b", "statement: ROLLBACK"),
            ("app", "a", "statement: SELECT 'a2'"),
            ("app", "a", "statement: COMMIT"),
        ), exclude=set())
        self.assertEqual(blocks(statements), [
            (True, ["SELECT 'a1'", "SELECT 'a2'"]),
            (False, ["SELECT 'b1'"]),
        ])

    def test_row_ids_attach_to_their_session(self):
        statements = parse_log(log(
            ("app", "a", "statement: INSERT INTO t VALUES (DEFAULT)"),
            ("app", "b", "statement: SELECT 1"),
            ("app", "a", 'migcheck-id ["t", 7]'),
        ), exclude=set())
        self.assertEqual([s.ids for s in statements], [[["t", 7]], []])

    def test_row_ids_after_a_dropped_statement_are_ignored(self):
        statements = parse_log(log(
            ("app", "a", "statement: CREATE TABLE t (id serial)"),
            ("app", "a", 'migcheck-id ["t", 1]'),
            ("app", "a", "statement: SELECT 1"),
        ), exclude=set())
        self.assertEqual([(s.sql, s.ids) for s in statements], [("SELECT 1", [])])

    def test_skips_excluded_and_system_databases(self):
        statements = parse_log(log(
            ("postgres", "a", "statement: SELECT 'postgres'"),
            ("template1", "a", "statement: SELECT 'template1'"),
            ("mc_template", "a", "statement: SELECT 'excluded'"),
            ("test_app", "b", "statement: SELECT 'kept'"),
        ), exclude={"mc_template"})
        self.assertEqual([s.sql for s in statements], ["SELECT 'kept'"])

    def test_skips_non_log_lines(self):
        statements = parse_log(log(
            ("app", "a", "statement: SELECT 1"),
            ("app", "a", 'column "x" does not exist', "", "ERROR"),
            ("app", "a", "duration: 0.1 ms"),
        ), exclude=set())
        self.assertEqual([s.sql for s in statements], ["SELECT 1"])

    def test_inlines_named_prepared_statement_parameters(self):
        statements = parse_log(log(
            ("app", "a", "execute s1: SELECT $1, $2", "Parameters: $1 = '1', $2 = NULL"),
        ), exclude=set())
        self.assertEqual([s.sql for s in statements], ["SELECT '1', NULL"])

    def test_drops_transaction_control_and_ddl(self):
        statements = parse_log(log(
            ("app", "a", "statement: start transaction"),
            ("app", "a", "statement: SAVEPOINT s"),
            ("app", "a", "statement: RELEASE s"),
            ("app", "a", "statement: SET CONSTRAINTS ALL DEFERRED"),
            ("app", "a", "statement: CREATE INDEX i ON t (x)"),
            ("app", "a", "statement: SELECT 1"),
            ("app", "a", "statement: end"),
        ), exclude=set())
        self.assertEqual(blocks(statements), [(True, ["SELECT 1"])])

    def test_rewrites_discard_all(self):
        # DISCARD ALL can't run in a transaction. Replay keeps the parts that
        # can, so a pooled connection's reset still undoes the app's role and
        # settings.
        statements = parse_log(log(
            ("app", "a", "statement: DISCARD ALL"),
            ("app", "a", "statement: discard temp;"),
            ("app", "a", "statement: RESET ALL"),
        ), exclude=set())
        self.assertEqual([s.sql for s in statements], [
            "SET SESSION AUTHORIZATION DEFAULT; RESET ALL",
            "discard temp;",
            "RESET ALL",
        ])

    def test_commit_prepared_does_not_end_the_transaction(self):
        statements = parse_log(log(
            ("app", "a", "statement: BEGIN"),
            ("app", "a", "statement: SELECT 1"),
            ("app", "a", "statement: COMMIT PREPARED 'x'"),
        ), exclude=set())
        # The transaction never closed, so it never committed.
        self.assertEqual(blocks(statements), [(False, ["SELECT 1"])])


class InlineParamsTest(unittest.TestCase):
    def test_replaces_placeholders(self):
        self.assertEqual(_inline_params("SELECT $1, $2", {1: "'a'", 2: "NULL"}), "SELECT 'a', NULL")

    def test_two_digit_placeholders(self):
        self.assertEqual(_inline_params("SELECT $1, $10", {1: "'one'", 10: "'ten'"}), "SELECT 'one', 'ten'")

    def test_leaves_quoted_text_alone(self):
        sql = """SELECT '$1', 'it''s $1', "col$1", $1"""
        self.assertEqual(_inline_params(sql, {1: "'x'"}), """SELECT '$1', 'it''s $1', "col$1", 'x'""")

    def test_leaves_unknown_placeholders(self):
        self.assertEqual(_inline_params("SELECT $2", {1: "'x'"}), "SELECT $2")

    def test_quotes_in_comments_dont_start_a_string(self):
        # Akkoma's activity queries carry a comment like this one.
        sql = "SELECT $1 -- friends won't see $1\n, $2 /* it's $2 */ LIMIT $3"
        self.assertEqual(
            _inline_params(sql, {1: "'a'", 2: "'b'", 3: "20"}),
            "SELECT 'a' -- friends won't see $1\n, 'b' /* it's $2 */ LIMIT 20",
        )

    def test_nested_block_comments(self):
        sql = "SELECT /* a /* b' */ c' */ $1"
        self.assertEqual(_inline_params(sql, {1: "'x'"}), "SELECT /* a /* b' */ c' */ 'x'")


    def test_dollar_quoted_text(self):
        sql = "SELECT $$ /* $1 $$, $tag$ -- it's $tag$, $1"
        self.assertEqual(_inline_params(sql, {1: "'x'"}), "SELECT $$ /* $1 $$, $tag$ -- it's $tag$, 'x'")

    def test_escape_strings(self):
        sql = r"SELECT E'it\'s -- $1', e'\\', $1"
        self.assertEqual(_inline_params(sql, {1: "'x'"}), r"SELECT E'it\'s -- $1', e'\\', 'x'")

    def test_non_ascii_dollar_quote_tags(self):
        self.assertEqual(_inline_params("SELECT $é$ -- $é$, $1", {1: "9"}), "SELECT $é$ -- $é$, 9")

    def test_placeholder_right_after_a_dollar_quote(self):
        self.assertEqual(_inline_params("SELECT $a$x$a$$1", {1: "9"}), "SELECT $a$x$a$9")

    def test_dollar_signs_in_identifiers(self):
        self.assertEqual(_inline_params("SELECT col$1, $1", {1: "'x'"}), "SELECT col$1, 'x'")

    def test_unterminated_string_runs_to_the_end(self):
        self.assertEqual(_inline_params("SELECT $1, 'a $1", {1: "'x'"}), "SELECT 'x', 'a $1")

    def test_long_statements(self):
        # Bulk inserts can carry thousands of placeholders.
        n = 20000
        sql = "INSERT INTO t VALUES " + ", ".join(f"(${i})" for i in range(1, n + 1))
        out = _inline_params(sql, {i: "1" for i in range(1, n + 1)})
        self.assertEqual(out, "INSERT INTO t VALUES " + ", ".join("(1)" for _ in range(n)))


class LenientTruncateTest(unittest.TestCase):
    def rewrite(self, sql):
        match = TRUNCATE.match(sql)
        self.assertIsNotNone(match, sql)
        return _lenient_truncate(match)

    def test_one_table(self):
        self.assertEqual(self.rewrite("TRUNCATE users"), "SELECT _migcheck.truncate(ARRAY['users']::text[], False)")

    def test_options(self):
        self.assertEqual(
            self.rewrite("truncate table only a, b * restart identity cascade;"),
            "SELECT _migcheck.truncate(ARRAY['a', 'b']::text[], True)",
        )

    def test_quoted_names(self):
        self.assertEqual(
            self.rewrite('TRUNCATE "public"."it\'s"'),
            "SELECT _migcheck.truncate(ARRAY['\"public\".\"it''s\"']::text[], False)",
        )


if __name__ == "__main__":
    unittest.main()
