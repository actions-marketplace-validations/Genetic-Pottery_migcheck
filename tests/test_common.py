import unittest

from migcheck.common import column_owner
from migcheck.mysql import MySQL
from migcheck.postgres import Postgres


class ColumnOwnerTest(unittest.TestCase):
    def test_resolves_aliases(self):
        self.assertEqual(column_owner("SELECT u0.id FROM users AS u0", "u0", True), "users")
        self.assertEqual(column_owner("SELECT 1 FROM a x JOIN b y ON true", "y", True), "b")
        self.assertEqual(column_owner("SELECT 1 FROM a x, b y", "y", True), "b")
        self.assertEqual(column_owner("UPDATE users u0 SET a = 1", "u0", True), "users")

    def test_finds_tables_in_parenthesized_joins_and_subqueries(self):
        sql = 'SELECT "local_user"."x" FROM (("local_user" INNER JOIN "person" ON true) LEFT JOIN "site" ON true)'
        self.assertEqual(column_owner(sql, '"local_user"', True), "local_user")
        self.assertEqual(column_owner('SELECT * FROM (SELECT "local_user"."x" FROM "local_user") s', "local_user", True),
                         "local_user")
        self.assertIsNone(column_owner(sql, None, True))

    def test_a_table_name_resolves_to_itself(self):
        self.assertEqual(column_owner("SELECT users.id FROM public.users", "users", True), "users")

    def test_keeps_the_case_of_quoted_names(self):
        self.assertEqual(column_owner('SELECT a."Status" FROM "AccessRequest" AS a', "a", True), "AccessRequest")
        self.assertEqual(column_owner("SELECT A.x FROM Users A", "A", True), "users")
        self.assertEqual(column_owner("SELECT A.x FROM Users A", "A", False), "Users")

    def test_finds_the_only_table(self):
        self.assertEqual(column_owner("SELECT MAX(pin_order) FROM issue WHERE repo_id = 1", None, True), "issue")
        self.assertEqual(column_owner("INSERT INTO t (a, b) VALUES (1, 2)", None, True), "t")
        self.assertEqual(column_owner("SELECT * FROM t ORDER BY a, b FOR UPDATE NOWAIT", None, True), "t")
        self.assertEqual(
            column_owner("INSERT INTO t (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET a = 2", None, True), "t"
        )
        self.assertEqual(column_owner("INSERT INTO t SET a = 1 ON DUPLICATE KEY UPDATE b = 2", None, False), "t")

    def test_ignores_quoted_text_and_comments(self):
        sql = "/* from a b */ INSERT INTO t (a) VALUES ('copied from x y') -- from c d"
        self.assertEqual(column_owner(sql, None, True), "t")

    def test_returns_none_when_unsure(self):
        self.assertIsNone(column_owner("SELECT 1 FROM a, b", None, True))
        self.assertIsNone(column_owner("SELECT * FROM unnest(ARRAY[1]) u JOIN t ON true", None, True))
        self.assertIsNone(column_owner("SELECT * FROM (SELECT 1) s JOIN t ON true", None, True))
        self.assertIsNone(column_owner("SELECT 1 FROM a u0 JOIN (SELECT * FROM b u0) z ON true", "u0", True))
        self.assertIsNone(column_owner("SELECT 1 FROM a", "missing", True))


class CauseTest(unittest.TestCase):
    def test_postgres(self):
        cases = [
            ('column "name" of relation "users" does not exist', "", "users.name"),
            ("column u0.name does not exist", "SELECT u0.name FROM users u0", "users.name"),
            ('column a."Status" does not exist', 'SELECT 1 FROM "AccessLease" a', "AccessLease.Status"),
            ('column "name" does not exist', "SELECT name FROM users", "users.name"),
            ('null value in column "uuid" of relation "users" violates not-null constraint', "", "users.uuid"),
            ('relation "public.posts" does not exist', "", "posts"),
            ("function thread_visibility(unknown, text) does not exist", "", "thread_visibility()"),
            ('column "name" does not exist', "SELECT name FROM a, b", None),
            ("there is no unique or exclusion constraint matching the ON CONFLICT specification", "", None),
        ]
        for msg, sql, want in cases:
            self.assertEqual(Postgres.cause(msg, sql), want, msg)

    def test_mysql(self):
        cases = [
            ("Unknown column 'ipe.ORG_ID' in 'field list'", "select ipe.X from IDP ipe", "IDP.ORG_ID"),
            ("Unknown column 'ORG_ID' in 'field list'", "insert into IDP (A, ORG_ID) values (1, 2)", "IDP.ORG_ID"),
            ("Field 'REALM_ID' doesn't have a default value", "insert into ORG_DOMAIN (NAME) values (1)",
             "ORG_DOMAIN.REALM_ID"),
            ("Column 'name' cannot be null", "insert into users (name) values (NULL)", "users.name"),
            ("Table 'mc_new.products' doesn't exist", "", "products"),
            ("FUNCTION mc_new.score does not exist", "", "score()"),
            ("Duplicate entry '1' for key 'PRIMARY'", "", None),
        ]
        for msg, sql, want in cases:
            self.assertEqual(MySQL.cause(msg, sql), want, msg)
