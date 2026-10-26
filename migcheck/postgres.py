"""Postgres adapter.

Runs a throwaway Postgres server in Docker with statement logging on, and
talks to it with the psql inside the container, so the host needs only Docker.
"""

import csv
import io
import json
import re
import subprocess
import time
import uuid

from .common import MIGRATED_KEY, Statement, column_owner
from .common import run as _run

DEFAULT_IMAGE = "postgres:17-alpine"

# Logged statements can be far larger than the csv module's 128 KiB default,
# for example inserts that carry file contents.
csv.field_size_limit(2**31 - 1)
PASSWORD = "migcheck"
LOG_FILE = "/tmp/pglog/pg.csv"

# csvlog column positions (stable since Postgres 9.0).
COL_DATABASE = 2
COL_SESSION = 5
COL_SEVERITY = 11
COL_MESSAGE = 13
COL_DETAIL = 14

# Statements that control the client's transaction. Replay keeps the
# transaction boundaries but issues its own control statements, so these are
# dropped.
TXN_BEGIN = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION)\b", re.IGNORECASE)
TXN_COMMIT = re.compile(r"^\s*(COMMIT|END)\b(?!\s+PREPARED)", re.IGNORECASE)
TXN_ROLLBACK = re.compile(r"^\s*(ROLLBACK|ABORT)\b(?!\s+(TO|PREPARED)\b)", re.IGNORECASE)
TXN_CONTROL = re.compile(
    r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE\s+TRANSACTION|SET\s+CONSTRAINTS)\b",
    re.IGNORECASE,
)
# Schema changes come from test runners building their database, not from the
# app's runtime queries.
DDL = re.compile(r"^\s*(CREATE|ALTER|DROP|COMMENT|GRANT|REVOKE)\b", re.IGNORECASE)
# Replay inlines statements that drivers prepare over the protocol, so it
# never creates the ones they free, as Go's pgx does. A statement prepared
# with PREPARE replays, so its DEALLOCATE does too.
DEALLOCATE = re.compile(r'^\s*DEALLOCATE\s+(?:PREPARE\s+)?"?([^";\s]+)"?\s*;?\s*$', re.IGNORECASE)
# Connection pools, such as Npgsql's, send DISCARD ALL to reset a connection
# that the app returns. It can't run in a transaction, so replay runs the
# parts that undo the app's role and settings.
DISCARD_ALL = re.compile(r"^\s*DISCARD\s+ALL\s*;?\s*$", re.IGNORECASE)
SESSION_RESET = "SET SESSION AUTHORIZATION DEFAULT; RESET ALL"
# Test runners empty tables between tests by name. On the new schema a listed
# table can be gone, or a new table can reference one and block the TRUNCATE.
# Either way the tables stay full and every later test collides with old rows,
# so replay empties whichever listed tables exist, with CASCADE.
TRUNCATE = re.compile(
    r"^\s*TRUNCATE\s+(?:TABLE\s+)?(.*?)(\s+(?:RESTART|CONTINUE|CASCADE|RESTRICT)\b.*)?;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
# Errors that name the schema object a statement used, so the report can
# group a dropped column's errors together however each statement named it.
COLUMN_OF = re.compile(r'^(?:column|null value in column) "(.+?)" of relation "(.+?)" ')
QUALIFIED_COLUMN = re.compile(r'^column ((?:"(?:[^"]|"")+"|[^".\s]+))\.(.+) does not exist$')
COLUMN = re.compile(r'^column "(.+)" does not exist$')
RELATION = re.compile(r'^relation "(?:.+\.)?(.+)" does not exist$')
FUNCTION = re.compile(r'^function (?:[\w$]+\.)?([\w$]+)\(.*\) does not exist$')
STATEMENT = re.compile(r"^(?:statement|execute ([^:]+)): (.*)$", re.DOTALL)
# One token of a statement, for finding the placeholders outside quoted text
# and comments. A quote inside a comment, such as "won't", doesn't start a
# string. An unterminated string or comment runs to the end.
SQL_TOKEN = re.compile(
    r"""--[^\n]*"""
    r"""|[Ee]'(?:\\.|''|[^'\\])*(?:'|\Z)"""
    r"""|'(?:''|[^'])*(?:'|\Z)"""
    r"""|"(?:""|[^"])*(?:"|\Z)"""
    r"""|\$(?P<tag>(?:[^\W\d]\w*)?)\$.*?(?:\$(?P=tag)\$|\Z)"""
    r"""|\$(?P<param>\d+)"""
    # Keywords and identifiers, which can hold $ but don't start a string.
    r"""|[^\W\d][\w$]*"""
    r"""|\d+|[^-'"$/\w]+|.""",
    re.DOTALL,
)
PARAM = re.compile(r"\$(\d+) = ('(?:[^']|'')*'|NULL)")
# PostgREST, which Supabase apps write through, binds a write's request body
# as a json parameter in this subquery. The log doesn't record the type, and
# an inlined literal in a select list is text, which json_to_record() rejects.
POSTGREST_BODY = re.compile(r"\(SELECT (\$\d+) AS json_data\) pgrst_payload\b")
ROW_ID = "migcheck-id "

# Many apps let the database generate primary keys, through a sequence or a
# function such as Mastodon's timestamp_id(). Replay would generate different
# keys than the workload got, and later statements that reference the
# original keys would fail on both schemas. So every table the workload
# creates logs the id of each row inserted, and replay pins inserts to those
# ids. Installing this in template1 reaches databases the test runner creates.
CAPTURE_IDS = """
CREATE FUNCTION public._migcheck_log_id() RETURNS trigger AS $f$
BEGIN
  RAISE LOG 'migcheck-id %', json_build_array(TG_TABLE_NAME, to_jsonb(NEW) -> 'id');
  RETURN NULL;
END
$f$ LANGUAGE plpgsql;

CREATE FUNCTION public._migcheck_attach() RETURNS event_trigger AS $f$
DECLARE r record;
BEGIN
  FOR r IN SELECT objid::regclass AS t FROM pg_event_trigger_ddl_commands()
           WHERE command_tag = 'CREATE TABLE' AND object_type = 'table' LOOP
    EXECUTE format('CREATE TRIGGER _migcheck_id AFTER INSERT ON %s FOR EACH ROW EXECUTE FUNCTION public._migcheck_log_id()', r.t);
  END LOOP;
END
$f$ LANGUAGE plpgsql;

CREATE EVENT TRIGGER _migcheck_attach ON ddl_command_end
  WHEN TAG IN ('CREATE TABLE') EXECUTE FUNCTION public._migcheck_attach();
"""

PIN_IDS = """
-- Whether a row that the head migrations wrote holds the key that row r takes
-- in the unique index idx. Those rows have an xmin between the mark taken
-- before the migrations and the start of replay.
CREATE FUNCTION _migcheck.holds_migrated_key(rel regclass, idx text, r anyelement) RETURNS bool AS $f$
DECLARE
  cond text;
  found bool;
BEGIN
  SELECT string_agg(format('%I = ($1).%I', a.attname, a.attname), ' AND '), bool_and(a.attname IS NOT NULL)
    INTO cond, found
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY k(attnum, ord)
    LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
   WHERE i.indrelid = rel AND c.relname = idx AND k.ord <= i.indnkeyatts;
  -- An expression index has no column to compare.
  IF cond IS NULL OR NOT found THEN
    RETURN false;
  END IF;
  EXECUTE format('SELECT EXISTS (SELECT FROM %s WHERE %s AND xmin::text::bigint >= $2 AND xmin::text::bigint < $3)', rel, cond)
    INTO found
    USING r, current_setting('migcheck.migrated_after')::bigint, current_setting('migcheck.replay_start')::bigint;
  RETURN found;
END
$f$ LANGUAGE plpgsql;

-- Runs a statement that failed on the unique index idx again, and returns
-- whether it collides there with a row the head migrations wrote: one written
-- after the mark migrated_after and before replay_start. The run always rolls
-- back.
CREATE FUNCTION _migcheck.migrated_key(stmt text, row_ids text, idx text, migrated_after bigint, replay_start bigint) RETURNS bool AS $f$
DECLARE
  hint text := '';
BEGIN
  -- Set for this check only. Replayed statements can reset session settings.
  PERFORM set_config('migcheck.migrated_after', migrated_after::text, true);
  PERFORM set_config('migcheck.replay_start', replay_start::text, true);
  PERFORM set_config('migcheck.ids', row_ids, true);
  PERFORM set_config('migcheck.pos', '0', true);
  PERFORM set_config('migcheck.probe', idx, true);
  BEGIN
    EXECUTE stmt;
    RAISE EXCEPTION 'migcheck probe succeeded';
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS hint = PG_EXCEPTION_HINT;
  END;
  PERFORM set_config('migcheck.probe', '', true);
  RETURN hint = 'migcheck-migrated-key';
END
$f$ LANGUAGE plpgsql;

-- Runs as its owner, because a replayed statement can switch to a role that
-- row-level security limits, as PostgREST's do, and the checks must see
-- every row.
CREATE FUNCTION _migcheck.pin_id() RETURNS trigger AS $f$
DECLARE
  ids jsonb := coalesce(nullif(current_setting('migcheck.ids', true), ''), '[]')::jsonb;
  pos int := coalesce(nullif(current_setting('migcheck.pos', true), ''), '0')::int;
  e jsonb;
  pinned record;
  taken bool;
BEGIN
  IF pos < jsonb_array_length(ids) THEN
    e := ids -> pos;
    IF e ->> 0 = TG_TABLE_NAME THEN
      PERFORM set_config('migcheck.pos', (pos + 1)::text, true);
      IF e -> 1 <> 'null'::jsonb THEN
        pinned := jsonb_populate_record(NEW, jsonb_build_object('id', e -> 1));
        -- A migration that inserts rows can take ids the workload also got.
        -- In production, the insert would get the next free id instead.
        EXECUTE format('SELECT EXISTS (SELECT FROM %s WHERE id = ($1).id)', TG_RELID::regclass)
          INTO taken USING pinned;
        IF NOT taken THEN
          NEW := pinned;
        END IF;
      END IF;
    END IF;
  END IF;
  IF coalesce(current_setting('migcheck.probe', true), '') <> ''
     AND _migcheck.holds_migrated_key(TG_RELID, current_setting('migcheck.probe'), NEW) THEN
    RAISE EXCEPTION 'migcheck probe' USING HINT = 'migcheck-migrated-key';
  END IF;
  RETURN NEW;
END
$f$ LANGUAGE plpgsql SECURITY DEFINER;

DO $f$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.oid::regclass AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema', '_migcheck')
             AND n.nspname NOT LIKE 'pg_toast%' LOOP
    EXECUTE format('CREATE TRIGGER _migcheck_pin BEFORE INSERT ON %s FOR EACH ROW EXECUTE FUNCTION _migcheck.pin_id()', r.t);
  END LOOP;
END
$f$;
"""


# The base and head schemas as JSON, for generating schema probes. It leaves
# out the catalogs, objects that extensions own, partitions, which their
# parent table covers, and migcheck's own objects.
APP_SCHEMAS = "n.nspname NOT IN ('pg_catalog', 'information_schema', '_migcheck') AND n.nspname NOT LIKE 'pg\\_toast%'"
CATALOG = f"""
COPY (SELECT json_build_object(
  'relations', (SELECT coalesce(json_agg(json_build_object(
      'schema', n.nspname, 'name', c.relname, 'kind', c.relkind,
      'columns', (SELECT coalesce(json_agg(json_build_object(
          'name', a.attname, 'type', format_type(a.atttypid, a.atttypmod), 'notnull', a.attnotnull,
          'default', a.atthasdef, 'generated', a.attgenerated <> '', 'identity', a.attidentity <> '')
          ORDER BY a.attnum), '[]')
        FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped))), '[]')
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition AND {APP_SCHEMAS}
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')),
  'fks', (SELECT coalesce(json_agg(json_build_object(
      'table', n.nspname || '.' || c.relname, 'ref', rn.nspname || '.' || rc.relname,
      'columns', (SELECT json_agg(a.attname ORDER BY k.i) FROM unnest(con.conkey) WITH ORDINALITY k(num, i)
                  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.num),
      'ref_columns', (SELECT json_agg(a.attname ORDER BY k.i) FROM unnest(con.confkey) WITH ORDINALITY k(num, i)
                      JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.num))), '[]')
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_class rc ON rc.oid = con.confrelid JOIN pg_namespace rn ON rn.oid = rc.relnamespace
    -- A foreign key to a partitioned table also has a row per partition.
    WHERE con.contype = 'f' AND con.conparentid = 0 AND {APP_SCHEMAS}),
  'functions', (SELECT coalesce(json_agg(json_build_object(
      'schema', n.nspname, 'name', p.proname,
      'args', (SELECT coalesce(json_agg(format_type(t, NULL) ORDER BY i), '[]')
               FROM unnest(p.proargtypes::oid[]) WITH ORDINALITY u(t, i)))), '[]')
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.prokind = 'f' AND {APP_SCHEMAS} AND p.proname NOT LIKE '\\_migcheck%'
      AND p.prorettype NOT IN ('trigger'::regtype, 'event_trigger'::regtype)
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')),
  'enums', (SELECT coalesce(json_object_agg(format_type(t.oid, NULL),
      (SELECT e.enumlabel FROM pg_enum e WHERE e.enumtypid = t.oid ORDER BY e.enumsortorder LIMIT 1)), '{{}}')
    FROM pg_type t WHERE t.typtype = 'e'),
  'domains', (SELECT coalesce(json_object_agg(format_type(t.oid, NULL), format_type(t.typbasetype, t.typtypmod)), '{{}}')
    FROM pg_type t WHERE t.typtype = 'd')
)::text) TO STDOUT WITH CSV;
"""
# Values for the columns an insert probe must set, by type name. Probes run
# after replay, so text and integers are random, to miss the unique values
# that replayed rows hold. Types with a length or precision take a value that
# fits any length.
LITERALS = {
    "smallint": "(1 + random() * 32000)::int", "integer": "(1 + random() * 2e9)::int",
    "bigint": "(1 + random() * 2e9)::bigint", "numeric": "0", "real": "0", "double precision": "0",
    "money": "0", "oid": "0", "boolean": "false", "uuid": "md5(random()::text)::uuid", "json": "'{}'",
    "jsonb": "'{}'", "bytea": "''", "interval": "'1 day'", "inet": "'127.0.0.1'", "cidr": "'127.0.0.1'",
    "macaddr": "'08:00:2b:01:02:03'", "tsvector": "''", "xml": "'<migcheck/>'", "point": "'(0,0)'",
    "date": "now()", "timestamp without time zone": "now()", "timestamp with time zone": "now()",
    "time without time zone": "now()", "time with time zone": "now()",
    # gen_random_uuid() is built in only from Postgres 13, and probes run on 12.
    "text": "md5(random()::text)", "citext": "md5(random()::text)", "name": "md5(random()::text)",
}
# Text with a length limit takes that many characters of a random value.
LIMITED_TEXT = re.compile(r"^(character varying|character)\((\d+)\)$")


class Dialect:
    """Writes probe SQL for Postgres."""

    @staticmethod
    def reads_back(column):
        """Whether an insert can read back a value the database filled in
        `column`. RETURNING reads back any column."""
        return True

    @staticmethod
    def ident(name):
        return '"' + name.replace('"', '""') + '"'

    @classmethod
    def table(cls, schema, name):
        return f"{cls.ident(schema)}.{cls.ident(name)}"

    @staticmethod
    def null(type_name):
        return f"NULL::{type_name}"

    @classmethod
    def insert(cls, rows):
        """Returns the rows as one statement: each parent row in a CTE that
        returns the columns its children use, then the last row."""
        ctes, select = [], ""
        for i, row in enumerate(rows):
            exprs, sources = [], []
            for value in row.values.values():
                if value[0] == "literal":
                    exprs.append(value[1])
                else:
                    exprs.append(f"p{value[1]}.{cls.ident(value[2])}")
                    if f"p{value[1]}" not in sources:
                        sources.append(f"p{value[1]}")
            q = cls.table(row.schema, row.table)
            cols = ", ".join(cls.ident(c) for c in row.values)
            ret = f" RETURNING {', '.join(cls.ident(c) for c in row.returning)}" if row.returning else ""
            if not row.values:
                select = f"INSERT INTO {q} DEFAULT VALUES{ret}"
            elif sources:
                select = f"INSERT INTO {q} ({cols}) SELECT {', '.join(exprs)} FROM {', '.join(sources)}{ret}"
            else:
                select = f"INSERT INTO {q} ({cols}) VALUES ({', '.join(exprs)}){ret}"
            if i < len(rows) - 1:
                ctes.append(f"p{i} AS ({select})")
        return [f"WITH {', '.join(ctes)} {select}" if ctes else select]

    @classmethod
    def literal(cls, type_name, catalog):
        if type_name.endswith("[]"):
            return f"'{{}}'::{type_name}"
        if type_name in catalog["enums"]:
            label = catalog["enums"][type_name].replace("'", "''")
            return f"'{label}'::{type_name}"
        if type_name in catalog["domains"]:
            base = cls.literal(catalog["domains"][type_name], catalog)
            return base and f"{base}::{type_name}"
        if m := LIMITED_TEXT.match(type_name):
            return f"left(md5(random()::text), {m.group(2)})::{type_name}"
        if type_name in ("character varying", "character"):
            return f"md5(random()::text)::{type_name}"
        value = LITERALS.get(re.sub(r"\(.*?\)", "", type_name).strip())
        return value and f"({value})::{type_name}"


# Removes the logging settings that roles and databases override, which
# would leave their statements out of the log.
LOG_EVERY_ROLE = """
DO $$
DECLARE
  s record;
  name text;
BEGIN
  FOR s IN SELECT setrole::regrole AS role, d.datname, setconfig FROM pg_db_role_setting
           LEFT JOIN pg_database d ON d.oid = setdatabase LOOP
    FOREACH name IN ARRAY ARRAY['log_statement', 'log_min_messages'] LOOP
      CONTINUE WHEN NOT EXISTS (SELECT FROM unnest(s.setconfig) c WHERE c LIKE name || '=%');
      IF s.role::oid = 0 THEN
        EXECUTE format('ALTER DATABASE %I RESET %I', s.datname, name);
      ELSIF s.datname IS NULL THEN
        EXECUTE format('ALTER ROLE %s RESET %I', s.role, name);
      ELSE
        EXECUTE format('ALTER ROLE %s IN DATABASE %I RESET %I', s.role, s.datname, name);
      END IF;
    END LOOP;
  END LOOP;
END
$$;
"""
# The copy of the postgres database that migcheck's databases start from, on
# images that set it up for the app.
BASE_DB = "mc_base"


def _server_command(image):
    """Returns the image's own server command, such as Supabase's `postgres -D
    /etc/postgresql`, so that migcheck's settings add to the image's config
    instead of replacing it. Returns [] for an image whose command isn't the
    server, and the entrypoint then starts it."""
    inspect = ["docker", "image", "inspect", image, "--format", "{{json .Config.Cmd}}"]
    if subprocess.run(inspect, capture_output=True).returncode != 0:
        _run(["docker", "pull", "-q", image])
    cmd = json.loads(_run(inspect)) or []
    return cmd if cmd[:1] == ["postgres"] else []


class Postgres:
    name = "Postgres"
    default_image = DEFAULT_IMAGE

    def __init__(self, image=DEFAULT_IMAGE):
        self.image = image
        self.container = None
        self.port = None
        # The role migcheck runs its own SQL as, which must be a superuser.
        self.admin = "postgres"
        # The database that migcheck's databases start from.
        self.base = "template1"

    def start(self):
        self.container = f"migcheck-{uuid.uuid4().hex[:8]}"
        _run([
            "docker", "run", "-d", "--rm", "--name", self.container,
            "-e", f"POSTGRES_PASSWORD={PASSWORD}",
            "-p", "127.0.0.1::5432",
            self.image,
            *_server_command(self.image),
            "-c", "log_statement=all",
            "-c", "logging_collector=on",
            "-c", "log_destination=csvlog",
            "-c", "log_directory=/tmp/pglog",
            "-c", "log_filename=pg.log",
            "-c", "log_rotation_size=0",
            # Postgres otherwise copies the whole statement into every log
            # line raised during it, including one per inserted row from the
            # id capture below. A large multi-row insert then fills the log
            # with gigabytes of copies. migcheck reads statements from their
            # own log lines, so it doesn't need the copies.
            "-c", "log_min_error_statement=panic",
            "-c", "fsync=off",
        ])
        mapping = _run(["docker", "port", self.container, "5432/tcp"])
        self.port = int(mapping.splitlines()[0].rsplit(":", 1)[1])
        # The image's init step runs a socket-only server first, so a TCP
        # check waits for the real one.
        for _ in range(60):
            check = subprocess.run(
                ["docker", "exec", self.container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                capture_output=True,
            )
            if check.returncode == 0:
                self._set_up()
                return
            time.sleep(0.5)
        raise RuntimeError("Postgres did not become ready in 30 seconds")

    def _set_up(self):
        # Some images, such as Supabase's, make postgres a regular role and
        # create a separate superuser.
        self.admin = self._psql("postgres", "COPY (SELECT rolname FROM pg_roles WHERE rolsuper AND rolcanlogin "
                                            "ORDER BY rolname <> 'postgres', rolname LIMIT 1) TO STDOUT;").strip()
        if not self.admin:
            raise RuntimeError(f"{self.image} has no superuser that can log in")
        self._psql("template1", CAPTURE_IDS)
        # An image that makes postgres a regular role sets up the database
        # the app gets, as Supabase's does with its auth schema and
        # extensions. Migrations depend on that setup, so migcheck's
        # databases start from a copy. Other images, such as postgis's, set
        # up the postgres database too, but for their own use.
        if self.admin != "postgres":
            self._copy_postgres(BASE_DB)
            self._psql(BASE_DB, CAPTURE_IDS)
            self.base = BASE_DB

    def _copy_postgres(self, name):
        # Background workers, such as pg_cron's and pg_net's, connect to the
        # postgres database, and Postgres copies only a database with no
        # other sessions. The workers reconnect after the copy.
        self._psql("template1", "ALTER DATABASE postgres ALLOW_CONNECTIONS false;")
        try:
            for _ in range(20):
                self._psql("template1", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                        "WHERE datname = 'postgres' AND pid <> pg_backend_pid();")
                try:
                    self._psql("template1", f'CREATE DATABASE "{name}" TEMPLATE postgres;')
                    return
                except RuntimeError as e:
                    if "is being accessed by other users" not in str(e):
                        raise
                time.sleep(0.5)
            raise RuntimeError("background sessions kept reconnecting to the postgres database, so migcheck couldn't copy it")
        finally:
            self._psql("template1", "ALTER DATABASE postgres ALLOW_CONNECTIONS true;")

    def stop(self):
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
            self.container = None

    def url(self, db):
        return f"postgresql://postgres:{PASSWORD}@127.0.0.1:{self.port}/{db}"

    def create_db(self, name, template=None):
        sql = f'CREATE DATABASE "{name}" TEMPLATE "{template or self.base}"'
        # The app connects as postgres, so it must own the databases that
        # a separate superuser creates.
        if self.admin != "postgres":
            sql += " OWNER postgres"
        self._psql("postgres", sql)

    def has_tables(self, db):
        """Whether the database has tables that its base database doesn't."""
        names = "COPY (SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')) TO STDOUT;"
        return bool(set(self._psql(db, names).splitlines()) - set(self._psql(self.base, names).splitlines()))

    def schema(self, db):
        """Returns the database's schema as text, for comparing two databases."""
        assert self.container
        dump = _run(["docker", "exec", self.container, "pg_dump", "-U", self.admin,
                     "--schema-only", "--no-owner", "--no-privileges", db])
        # Comments name the database, and pg_dump 17.6 and later add \restrict
        # lines with a random key.
        return "\n".join(line for line in dump.splitlines() if line and not line.startswith(("--", "\\")))

    dialect = Dialect

    def catalog(self, db):
        """Returns the database's tables, columns, foreign keys, and functions,
        for generating schema probes."""
        row = next(csv.reader(io.StringIO(self._psql(db, CATALOG))))
        raw = json.loads(row[0])
        fks = {}
        for fk in raw["fks"]:
            fks.setdefault(fk["table"], []).append(fk)
        return {
            "relations": {
                f"{r['schema']}.{r['name']}": {**r, "columns": {c["name"]: c for c in r["columns"]}}
                for r in raw["relations"]
            },
            "fks": fks,
            "functions": {f"{f['schema']}.{f['name']}({', '.join(f['args'])})": f for f in raw["functions"]},
            "enums": raw["enums"],
            "domains": raw["domains"],
        }

    def mark(self):
        """Returns a point in the server's transaction history. Rows written
        after it have a higher xmin."""
        return int(self._psql("postgres", "COPY (SELECT txid_current() % 4294967296) TO STDOUT;"))

    def record(self):
        """Starts recording statements. Postgres logs every statement from
        the start, except for roles and databases that turn logging down.
        Supabase's image does that for the roles its Auth and Storage servers
        log in as, so their writes, such as new users, would be missing from
        replay. Migrations can turn it down too, so this runs after them."""
        self._psql("postgres", LOG_EVERY_ROLE)

    @staticmethod
    def cause(msg, sql):
        """Returns the table, column, or function that an error says is
        missing or changed, such as "users.name", or None."""
        if m := COLUMN_OF.match(msg):
            return f"{m.group(2)}.{m.group(1)}"
        if m := QUALIFIED_COLUMN.match(msg):
            table, column = column_owner(sql, m.group(1), fold=True), m.group(2).strip('"')
            return table and f"{table}.{column}"
        if m := COLUMN.match(msg):
            table = column_owner(sql, None, fold=True)
            return table and f"{table}.{m.group(1)}"
        if m := RELATION.match(msg):
            return m.group(1)
        if m := FUNCTION.match(msg):
            return f"{m.group(1)}()"
        return None

    @staticmethod
    def is_integrity_error(state):
        return state.startswith("23")

    @staticmethod
    def is_foreign_key_error(state):
        return state == "23503"

    def statements(self, exclude):
        """Returns the statements logged against any database not in `exclude`,
        with bind parameters inlined.

        Test runners often create their own database, such as Django's
        test_<name>, so this doesn't filter on a single name.
        """
        # The log can be far larger than the statements kept from it, so read
        # it as a stream instead of loading it whole.
        proc = subprocess.Popen(["docker", "exec", self.container, "cat", LOG_FILE], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        statements = parse_log(io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline=""), exclude)
        if proc.wait() != 0:
            raise RuntimeError(f"reading the Postgres log failed with exit code {proc.returncode}")
        return statements

    def replay(self, db, statements, migrated_after=None):
        """Runs each statement in order and returns a list of (sqlstate, message).

        A statement that succeeds keeps its effects until the end of its
        block, so later statements see the rows earlier ones wrote. A failing
        statement rolls back on its own and replay continues. Both entries are
        None on success.

        With `migrated_after`, a mark taken before the head migrations ran, a
        duplicate key error on a key that a migration's row holds gets
        MIGRATED_KEY appended to its message.
        """
        tag = f"$mc_{uuid.uuid4().hex}$"
        lines = [
            "ALTER EVENT TRIGGER _migcheck_attach DISABLE;",
            "CREATE SCHEMA _migcheck;",
            "CREATE TABLE _migcheck.stmt (id int PRIMARY KEY, block int NOT NULL, keep bool NOT NULL, row_ids text NOT NULL, sql text NOT NULL);",
            "CREATE TABLE _migcheck.result (id int PRIMARY KEY, sqlstate text, message text);",
            """
CREATE FUNCTION _migcheck.truncate(names text[], restart bool) RETURNS void AS $f$
DECLARE
  tables text[] := '{}';
  t regclass;
BEGIN
  FOR i IN 1 .. coalesce(array_length(names, 1), 0) LOOP
    t := to_regclass(names[i]);
    IF t IS NOT NULL THEN
      tables := array_append(tables, t::text);
    END IF;
  END LOOP;
  IF array_length(tables, 1) > 0 THEN
    EXECUTE 'TRUNCATE ' || array_to_string(tables, ', ')
      || CASE WHEN restart THEN ' RESTART IDENTITY' ELSE '' END || ' CASCADE';
  END IF;
END
$f$ LANGUAGE plpgsql;""",
            PIN_IDS,
            f"SET migcheck.migrated_after = '{migrated_after if migrated_after is not None else -1}';",
            f"SET migcheck.migrated_key = '{MIGRATED_KEY}';",
        ]
        for i, s in enumerate(statements):
            ids = json.dumps(s.ids)
            lines.append(f"INSERT INTO _migcheck.stmt VALUES ({i}, {s.block}, {s.keep}, {tag}{ids}{tag}, {tag}{s.sql}{tag});")
        # Replay reads one block at a time. Without this index, each read
        # scans every statement, which takes hours for millions of statements.
        lines.append("CREATE INDEX ON _migcheck.stmt (block, id);")
        # Each block commits or rolls back as its own transaction. In one
        # transaction for the whole replay, Postgres keeps every version of a
        # row until the end, so a row updated thousands of times slows every
        # later read of it. Results collect in variables because a rolled-back
        # block also rolls back any rows it wrote.
        lines.append("""
-- Replayed statements can switch roles, and a statement that fails on a
-- unique key calls migrated_key() under its role.
GRANT USAGE ON SCHEMA _migcheck TO PUBLIC;
CREATE PROCEDURE _migcheck.replay() AS $do$
DECLARE
  b record;
  s record;
  ids int[] := '{}';
  states text[] := '{}';
  msgs text[] := '{}';
  st text;
  msg text;
  idx text;
  -- Read once, before any replayed statement can reset the session settings
  -- or a rolled-back block can undo them.
  migrated_after bigint := current_setting('migcheck.migrated_after')::bigint;
  migrated_note text := current_setting('migcheck.migrated_key');
  replay_start bigint := txid_current() % 4294967296;
BEGIN
  FOR b IN SELECT block, bool_and(keep) AS keep FROM _migcheck.stmt GROUP BY block ORDER BY block LOOP
    -- Deferred constraints (Django's foreign keys, for example) would
    -- otherwise fail the whole block at commit instead of failing the
    -- statement that broke them.
    SET CONSTRAINTS ALL IMMEDIATE;
    -- Blocks from different sessions run one after another here, so a role
    -- that one session set for itself mustn't reach the next block.
    SET SESSION AUTHORIZATION DEFAULT;
    RESET ROLE;
    FOR s IN SELECT id, row_ids, sql FROM _migcheck.stmt WHERE block = b.block ORDER BY id LOOP
      BEGIN
        PERFORM set_config('migcheck.ids', s.row_ids, true);
        PERFORM set_config('migcheck.pos', '0', true);
        EXECUTE s.sql;
        ids := array_append(ids, s.id);
        states := array_append(states, NULL::text);
        msgs := array_append(msgs, NULL::text);
      EXCEPTION WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS st = RETURNED_SQLSTATE, msg = MESSAGE_TEXT, idx = CONSTRAINT_NAME;
        IF st = '23505' AND migrated_after >= 0
           AND _migcheck.migrated_key(s.sql, s.row_ids, idx, migrated_after, replay_start) THEN
          msg := msg || migrated_note;
        END IF;
        ids := array_append(ids, s.id);
        states := array_append(states, st);
        msgs := array_append(msgs, msg);
      END;
    END LOOP;
    IF b.keep THEN
      COMMIT;
    ELSE
      ROLLBACK;
    END IF;
  END LOOP;
  SET SESSION AUTHORIZATION DEFAULT;
  RESET ROLE;
  INSERT INTO _migcheck.result SELECT * FROM unnest(ids, states, msgs);
END
$do$ LANGUAGE plpgsql;
-- Replay commits once per block, and nothing needs to survive a crash.
SET synchronous_commit = off;
CALL _migcheck.replay();
COPY (SELECT id, sqlstate, message FROM _migcheck.result ORDER BY id) TO STDOUT WITH CSV;
""")
        raw = self._psql(db, "\n".join(lines))
        results: list[tuple[str | None, str | None]] = [(None, None)] * len(statements)
        for row in csv.reader(io.StringIO(raw)):
            results[int(row[0])] = (row[1] or None, row[2] or None)
        return results

    def _psql(self, db, sql):
        return _run(
            ["docker", "exec", "-i", self.container,
             "psql", "-U", self.admin, "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1"],
            stdin=sql,
        )


def parse_log(log, exclude):
    """Returns the statements in a csvlog, read from the text stream `log`,
    logged against any database not in `exclude`, with bind parameters
    inlined."""
    exclude = {"postgres", "template0", "template1", BASE_DB, *exclude}
    blocks = []  # [keep, [[sql, ids], ...]] in the order transactions started
    open_blocks = {}  # session ID -> its open block
    last = {}  # session ID -> its last replayed [sql, ids], or None
    prepared = {}  # session ID -> names of statements prepared over the protocol
    for row in csv.reader(log):
        if row[COL_DATABASE] in exclude or row[COL_SEVERITY] != "LOG":
            continue
        session = row[COL_SESSION]
        if row[COL_MESSAGE].startswith(ROW_ID):
            if last.get(session):
                last[session][1].append(json.loads(row[COL_MESSAGE][len(ROW_ID):]))
            continue
        match = STATEMENT.match(row[COL_MESSAGE])
        if not match:
            continue
        sql = match.group(2).strip()
        last[session] = None
        if match.group(1):
            prepared.setdefault(session, set()).add(match.group(1))
        if TXN_BEGIN.match(sql):
            open_blocks[session] = [True, []]
            blocks.append(open_blocks[session])
        elif TXN_COMMIT.match(sql) or TXN_ROLLBACK.match(sql):
            block = open_blocks.pop(session, None)
            if block:
                block[0] = bool(TXN_COMMIT.match(sql))
        if not sql or TXN_CONTROL.match(sql) or DDL.match(sql):
            continue
        if (freed := DEALLOCATE.match(sql)) and freed.group(1) in prepared.get(session, ()):
            continue
        if row[COL_DETAIL].lower().startswith("parameters: "):
            params = {int(n): v for n, v in PARAM.findall(row[COL_DETAIL])}
            sql = _inline_params(POSTGREST_BODY.sub(r"(SELECT \1::json AS json_data) pgrst_payload", sql), params)
        if DISCARD_ALL.match(sql):
            sql = SESSION_RESET
        if truncate := TRUNCATE.match(sql):
            sql = _lenient_truncate(truncate)
        last[session] = [sql, []]
        if session in open_blocks:
            open_blocks[session][1].append(last[session])
        else:
            blocks.append([True, [last[session]]])  # autocommit
    # A transaction still open when the workload exits never committed.
    for block in open_blocks.values():
        block[0] = False
    return [
        Statement(sql, i, keep, ids)
        for i, (keep, stmts) in enumerate(b for b in blocks if b[1])
        for sql, ids in stmts
    ]


def _lenient_truncate(match):
    """Rewrites a TRUNCATE to call _migcheck.truncate, which replay defines."""
    names = []
    for name in match.group(1).split(","):
        name = re.sub(r"^\s*ONLY\s+", "", name, flags=re.IGNORECASE).strip().rstrip("*").strip()
        names.append("'" + name.replace("'", "''") + "'")
    restart = bool(re.search(r"RESTART\s+IDENTITY", match.group(2) or "", re.IGNORECASE))
    return f"SELECT _migcheck.truncate(ARRAY[{', '.join(names)}]::text[], {restart})"


def _inline_params(sql, params):
    """Replaces $N placeholders with logged literals, skipping quoted text and
    comments."""
    out = []
    i = 0
    while i < len(sql):
        if sql.startswith("/*", i):
            # Postgres nests block comments, which a regex can't match.
            depth, end = 1, i + 2
            while end < len(sql) and depth:
                if sql.startswith("/*", end):
                    depth, end = depth + 1, end + 2
                elif sql.startswith("*/", end):
                    depth, end = depth - 1, end + 2
                else:
                    end += 1
            out.append(sql[i:end])
            i = end
            continue
        m = SQL_TOKEN.match(sql, i)
        if m.group("param") and int(m.group("param")) in params:
            out.append(params[int(m.group("param"))])
        else:
            out.append(m.group(0))
        i = m.end()
    return "".join(out)

