"""MySQL adapter.

Runs a throwaway MySQL server in Docker, records the workload with the
general query log, and talks to the server with the mysql client inside the
container, so the host needs only Docker.
"""

import bisect
import json
import re
from collections import Counter
import subprocess
import time
import uuid

from .common import Statement, column_owner
from .common import run as _run

DEFAULT_IMAGE = "mysql:8.4"
PASSWORD = "migcheck"
LOG_FILE = "/tmp/general.log"
SYSTEM_DBS = {"mysql", "information_schema", "performance_schema", "sys"}

# A general log entry starts with a timestamp, the connection ID, and the
# command. A statement's own newlines continue it on the following lines.
ENTRY = re.compile(rb"^\d{4}-\d\d-\d\dT[\d:.]+Z?\t\s*(\d+) ([A-Za-z ]+?)\t(.*)$", re.DOTALL)
CONNECT_DB = re.compile(r" on (\S+) using ")

TXN_BEGIN = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION)\b(?!\s*;?\s*\S)", re.IGNORECASE)
TXN_COMMIT = re.compile(r"^\s*COMMIT\b", re.IGNORECASE)
TXN_ROLLBACK = re.compile(r"^\s*ROLLBACK\b(?!\s+(WORK\s+)?TO\b)", re.IGNORECASE)
AUTOCOMMIT = re.compile(
    r"^\s*SET\s+(?:SESSION\s+|@@(?:SESSION\.)?)?autocommit\s*=\s*(\S+?)\s*;?\s*$", re.IGNORECASE
)
USE = re.compile(r"^\s*USE\s+`?([^`\s;]+)`?\s*;?\s*$", re.IGNORECASE)
# Statements that only make sense in the client's own session. Replay issues
# its own transaction control. SQL-level PREPARE and EXECUTE are dropped
# because the general log also records the statement each EXECUTE runs.
SESSION_ONLY = re.compile(
    r"^\s*(XA|LOCK\s+TABLES?|UNLOCK\s+TABLES?|PREPARE|EXECUTE|DEALLOCATE|SET\s+TRANSACTION)\b", re.IGNORECASE
)
# Schema changes come from test runners building their database, not from the
# app's runtime queries. They commit any open transaction.
DDL = re.compile(r"^\s*(CREATE|ALTER|DROP|RENAME|GRANT|REVOKE)\b", re.IGNORECASE)
# Test runners empty tables between tests by name. On the new schema a listed
# table can be gone, or a new table can reference one and block the TRUNCATE,
# so replay empties the table only if it exists, ignoring foreign keys.
TRUNCATE = re.compile(r"^\s*TRUNCATE\s+(?:TABLE\s+)?((?:`[^`]+`|[\w$]+)(?:\.(?:`[^`]+`|[\w$]+))?)\s*;?\s*$", re.IGNORECASE)
ERROR = re.compile(r"^ERROR (\d+) \(\w+\) at line (\d+): (.*)$")

# Errors that name the schema object a statement used, so the report can
# group a dropped column's errors together however each statement named it.
UNKNOWN_COLUMN = re.compile(r"^Unknown column '(?:(.+)\.)?([^.']+)' in ")
NO_DEFAULT = re.compile(r"^(?:Field '(.+)' doesn't have a default value|Column '(.+)' cannot be null)$")
NO_TABLE = re.compile(r"^Table '(?:[^.']+\.)?(.+)' doesn't exist$")
NO_FUNCTION = re.compile(r"^FUNCTION (?:[^.\s]+\.)?(\S+) does not exist$")

# The schema as JSON, for generating schema probes. MySQL names a schema per
# database, and the base and head databases have different names, so names
# leave it out.
CATALOG = """
SELECT JSON_OBJECT(
  'relations', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT(
      'name', t.TABLE_NAME, 'kind', IF(t.TABLE_TYPE = 'VIEW', 'v', 'r'),
      'columns', (SELECT JSON_ARRAYAGG(JSON_OBJECT(
          'name', c.COLUMN_NAME, 'type', c.COLUMN_TYPE, 'notnull', c.IS_NULLABLE = 'NO',
          'default', c.COLUMN_DEFAULT IS NOT NULL, 'generated', c.EXTRA LIKE '%GENERATED%',
          'identity', c.EXTRA LIKE '%auto_increment%', 'position', c.ORDINAL_POSITION))
        FROM information_schema.COLUMNS c
        WHERE c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME))), JSON_ARRAY())
    FROM information_schema.TABLES t WHERE t.TABLE_SCHEMA = DATABASE()),
  'fks', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT(
      'table', k.TABLE_NAME, 'ref', k.REFERENCED_TABLE_NAME, 'name', k.CONSTRAINT_NAME,
      'column', k.COLUMN_NAME, 'ref_column', k.REFERENCED_COLUMN_NAME, 'position', k.ORDINAL_POSITION)), JSON_ARRAY())
    FROM information_schema.KEY_COLUMN_USAGE k
    WHERE k.TABLE_SCHEMA = DATABASE() AND k.REFERENCED_TABLE_NAME IS NOT NULL
      AND k.REFERENCED_TABLE_SCHEMA = DATABASE()),
  'functions', (SELECT COALESCE(JSON_ARRAYAGG(JSON_OBJECT(
      'name', r.ROUTINE_NAME,
      'args', (SELECT COALESCE(JSON_ARRAYAGG(p.DTD_IDENTIFIER), JSON_ARRAY()) FROM information_schema.PARAMETERS p
               WHERE p.SPECIFIC_SCHEMA = r.ROUTINE_SCHEMA AND p.SPECIFIC_NAME = r.SPECIFIC_NAME
                 AND p.ORDINAL_POSITION > 0))), JSON_ARRAY())
    FROM information_schema.ROUTINES r WHERE r.ROUTINE_SCHEMA = DATABASE() AND r.ROUTINE_TYPE = 'FUNCTION')
)
"""
# Values for the columns an insert probe must set, by type name. Probes run
# after replay, so text and integers are random, to miss the unique values
# that replayed rows hold.
LITERALS = {
    "tinyint": "FLOOR(1 + RAND() * 100)", "smallint": "FLOOR(1 + RAND() * 32000)",
    "mediumint": "FLOOR(1 + RAND() * 8000000)", "int": "FLOOR(1 + RAND() * 2000000000)",
    "integer": "FLOOR(1 + RAND() * 2000000000)", "bigint": "FLOOR(1 + RAND() * 2000000000)",
    "decimal": "0", "numeric": "0", "float": "0", "double": "0", "real": "0", "bit": "b'0'",
    "date": "NOW()", "datetime": "NOW()", "timestamp": "NOW()", "time": "NOW()", "year": "2000",
    "tinytext": "MD5(RAND())", "text": "MD5(RAND())", "mediumtext": "MD5(RAND())", "longtext": "MD5(RAND())",
    "tinyblob": "''", "blob": "''", "mediumblob": "''", "longblob": "''", "json": "JSON_OBJECT()", "set": "''",
}
SIZED = re.compile(r"^(char|varchar|binary|varbinary)\((\d+)\)")
ENUM = re.compile(r"^enum\('((?:[^']|'')*)'", re.IGNORECASE)


class Dialect:
    """Writes probe SQL for MySQL."""

    @staticmethod
    def reads_back(column):
        """Whether an insert can read back a value the database filled in
        `column`. MySQL has no RETURNING, so only LAST_INSERT_ID() reads an
        auto-increment key back."""
        return column["identity"]

    @staticmethod
    def ident(name):
        return "`" + name.replace("`", "``") + "`"

    @classmethod
    def table(cls, schema, name):
        return cls.ident(name)

    @staticmethod
    def null(type_name):
        return "NULL"

    @staticmethod
    def literal(type_name, catalog):
        t = type_name.lower()
        if m := ENUM.match(type_name):
            return f"'{m.group(1)}'"
        if m := SIZED.match(t):
            if m.group(1) in ("binary", "varbinary"):
                return f"LEFT(UNHEX(MD5(RAND())), {m.group(2)})"
            return f"LEFT(MD5(RAND()), {m.group(2)})"
        return LITERALS.get(re.split(r"[\s(]", t)[0])

    @classmethod
    def insert(cls, rows):
        """Returns the rows as statements, parents first. A column that a
        later row uses goes through a user variable, set before the insert
        from its value or after it from LAST_INSERT_ID()."""
        def var(j, col):
            return f"@mc{j}_{rows[j].returning.index(col)}"

        out = []
        for i, row in enumerate(rows):
            exprs = []
            for col, value in row.values.items():
                if value[0] == "ref":
                    exprs.append(var(value[1], value[2]))
                    if col in row.returning:
                        out.append(f"SET {var(i, col)} = {var(value[1], value[2])}")
                elif col in row.returning:
                    out.append(f"SET {var(i, col)} = {value[1]}")
                    exprs.append(var(i, col))
                else:
                    exprs.append(value[1])
            cols = ", ".join(cls.ident(c) for c in row.values)
            out.append(f"INSERT INTO {cls.ident(row.table)} ({cols}) VALUES ({', '.join(exprs)})")
            for col in row.returning:
                if col not in row.values:
                    out.append(f"SET {var(i, col)} = LAST_INSERT_ID()")
        return out


INTEGRITY_ERRORS = {"1022", "1048", "1062", "1169", "1216", "1217", "1364", "1451", "1452", "1557", "1586", "3819"}
FOREIGN_KEY_ERRORS = {"1216", "1217", "1451", "1452"}

REPLAY_SETUP = """\
DROP PROCEDURE IF EXISTS _migcheck_truncate;
DELIMITER //
CREATE PROCEDURE _migcheck_truncate(name varchar(64))
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = name) THEN
    SET @_migcheck_fk = @@foreign_key_checks, foreign_key_checks = 0;
    SET @_migcheck_sql = CONCAT('TRUNCATE TABLE `', REPLACE(name, '`', '``'), '`');
    PREPARE _migcheck_stmt FROM @_migcheck_sql;
    EXECUTE _migcheck_stmt;
    DEALLOCATE PREPARE _migcheck_stmt;
    SET foreign_key_checks = @_migcheck_fk;
  END IF;
END//
DELIMITER ;
"""


class MySQL:
    name = "MySQL"
    default_image = DEFAULT_IMAGE

    def __init__(self, image=DEFAULT_IMAGE):
        self.image = image
        self.container = None
        self.port = None

    def start(self):
        self.container = f"migcheck-{uuid.uuid4().hex[:8]}"
        _run([
            "docker", "run", "-d", "--rm", "--name", self.container,
            "-e", f"MYSQL_ROOT_PASSWORD={PASSWORD}",
            "-p", "127.0.0.1::3306",
            self.image,
            f"--general-log-file={LOG_FILE}",
            "--max-allowed-packet=1G",
            "--skip-log-bin",
            "--skip-name-resolve",
            "--innodb-flush-log-at-trx-commit=0",
            "--innodb-doublewrite=OFF",
            # Test suites that restart the app between tests can hold more
            # connections than the default limit of 151.
            "--max-connections=1000",
        ])
        mapping = _run(["docker", "port", self.container, "3306/tcp"])
        self.port = int(mapping.splitlines()[0].rsplit(":", 1)[1])
        # The image's init step runs a socket-only server first, so a TCP
        # check waits for the real one.
        for _ in range(120):
            check = subprocess.run(self._client("-h", "127.0.0.1", "-e", "SELECT 1"), capture_output=True)
            if check.returncode == 0:
                return
            time.sleep(0.5)
        raise RuntimeError("MySQL did not become ready in 60 seconds")

    def stop(self):
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
            self.container = None

    def url(self, db):
        return f"mysql://root:{PASSWORD}@127.0.0.1:{self.port}/{db}"

    def create_db(self, name, template=None):
        """Creates a database. MySQL has no template databases, so a
        template's tables and rows are copied with mysqldump."""
        _run(self._client("-e", f"CREATE DATABASE `{name}`"))
        if template:
            _run(["docker", "exec", "-e", f"MYSQL_PWD={PASSWORD}", self.container, "sh", "-c",
                  "set -o pipefail; mysqldump -uroot --single-transaction --routines --triggers --events "
                  f"--hex-blob --no-tablespaces --set-gtid-purged=OFF {template} | mysql -uroot {name}"])

    def has_tables(self, db):
        out = _run(self._client("-N", "-e", f"SELECT count(*) FROM information_schema.tables WHERE table_schema = '{db}'"))
        return int(out) > 0

    def schema(self, db):
        """Returns the database's schema as text, for comparing two databases."""
        assert self.container
        dump = _run(["docker", "exec", "-e", f"MYSQL_PWD={PASSWORD}", self.container, "mysqldump", "-uroot",
                     "--no-data", "--skip-comments", "--routines", "--triggers", "--events",
                     "--no-tablespaces", "--set-gtid-purged=OFF", db])
        # Inserted rows move the auto-increment counter, which isn't schema.
        return re.sub(r" AUTO_INCREMENT=\d+", "", dump)

    def record(self):
        """Starts recording statements. The log stays off until the workload
        runs, so setup statements don't appear in it."""
        _run(self._client("-e", "SET GLOBAL general_log = ON"))

    dialect = Dialect

    def catalog(self, db):
        """Returns the database's tables, columns, foreign keys, and functions,
        for generating schema probes."""
        raw = json.loads(_run(self._client("-N", "-B", "--raw", db, "-e", CATALOG)))
        fks = {}
        for k in sorted(raw["fks"], key=lambda k: (k["table"], k["name"], k["position"])):
            group = fks.setdefault(f".{k['table']}", {}).setdefault(
                k["name"], {"table": f".{k['table']}", "ref": f".{k['ref']}", "columns": [], "ref_columns": []})
            group["columns"].append(k["column"])
            group["ref_columns"].append(k["ref_column"])
        return {
            "relations": {
                f".{r['name']}": {
                    "schema": "", "name": r["name"], "kind": r["kind"],
                    "columns": {c["name"]: {**c, "notnull": bool(c["notnull"]), "default": bool(c["default"]),
                                            "generated": bool(c["generated"]), "identity": bool(c["identity"])}
                                for c in sorted(r["columns"], key=lambda c: c["position"])},
                }
                for r in raw["relations"]
            },
            "fks": {t: list(groups.values()) for t, groups in fks.items()},
            "functions": {f".{f['name']}({', '.join(f['args'])})": {**f, "schema": ""} for f in raw["functions"]},
            "enums": {},
            "domains": {},
        }

    @staticmethod
    def cause(msg, sql):
        """Returns the table, column, or function that an error says is
        missing or changed, such as "users.name", or None."""
        if m := UNKNOWN_COLUMN.match(msg):
            table = column_owner(sql, m.group(1), fold=False)
            return table and f"{table}.{m.group(2)}"
        if m := NO_DEFAULT.match(msg):
            table = column_owner(sql, None, fold=False)
            return table and f"{table}.{m.group(1) or m.group(2)}"
        if m := NO_TABLE.match(msg):
            return m.group(1)
        if m := NO_FUNCTION.match(msg):
            return f"{m.group(1)}()"
        return None

    @staticmethod
    def is_integrity_error(state):
        return state in INTEGRITY_ERRORS

    @staticmethod
    def is_foreign_key_error(state):
        return state in FOREIGN_KEY_ERRORS

    def statements(self, exclude):
        """Returns the statements the workload ran against any database not in
        `exclude`.

        Test runners often create their own database, such as Django's
        test_<name>, so this doesn't filter on a single name.
        """
        self._stop_recording()
        # The log can be far larger than the statements kept from it, so read
        # it as a stream instead of loading it whole.
        assert self.container
        proc = subprocess.Popen(["docker", "exec", self.container, "cat", LOG_FILE], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        statements = parse_log(proc.stdout, exclude)
        if proc.wait() != 0:
            raise RuntimeError(f"reading the MySQL log failed with exit code {proc.returncode}")
        return statements

    def mark(self):
        """MySQL doesn't record which transaction wrote a row, so replay can't
        tell rows from the head migrations apart. Returns None."""
        return None

    def replay(self, db, statements, migrated_after=None):
        """Runs each statement in order and returns a list of (error code, message).

        A statement that succeeds keeps its effects until the end of its
        block, so later statements see the rows earlier ones wrote. MySQL
        rolls back a failing statement on its own, and replay continues. Both
        entries are None on success.
        """
        script, starts = build_script(statements)
        assert self.container
        proc = subprocess.run(
            ["docker", "exec", "-i", "-e", f"MYSQL_PWD={PASSWORD}", self.container, "sh", "-c",
             f"mysql -uroot --force --binary-mode --max-allowed-packet=1G --default-character-set=utf8mb4 {db} >/dev/null"],
            input=script, capture_output=True,
        )
        results = parse_errors(proc.stderr.decode("utf-8", "replace"), starts, len(statements))
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"mysql exited with code {proc.returncode}:\n{proc.stderr.decode(errors='replace')[-2000:]}")
        return results

    def _stop_recording(self):
        # The log records this statement before it takes effect. Connecting
        # to a system database keeps it out of the workload, because
        # parse_log skips everything sent to those.
        _run(self._client("-D", "mysql", "-e", "SET GLOBAL general_log = OFF"))

    def _client(self, *args):
        assert self.container
        return ["docker", "exec", "-e", f"MYSQL_PWD={PASSWORD}", self.container, "mysql", "-uroot", *args]


class _Session:
    def __init__(self):
        self.db = None
        self.autocommit = True
        self.block = None  # the open transaction's block, if any


def parse_log(log, exclude):
    """Returns the statements in a general query log, read from the binary
    line stream `log`, that ran against any database not in `exclude`."""
    exclude = SYSTEM_DBS | set(exclude)
    blocks = []  # [keep, [sql, ...]] in the order transactions started
    sessions = {}  # connection ID -> _Session

    def close(session, keep):
        if session.block is not None:
            session.block[0] = keep
            session.block = None

    def add(session, sql):
        if session.db in exclude:
            return
        if session.block is None and not session.autocommit:
            session.block = [True, []]
            blocks.append(session.block)
        if session.block is not None:
            session.block[1].append(sql)
        else:
            blocks.append([True, [sql]])

    for conn, command, arg in _entries(log):
        session = sessions.setdefault(conn, _Session())
        if command == "Connect":
            m = CONNECT_DB.search(arg)
            session.db = m.group(1) if m else None
        elif command == "Init DB":
            session.db = arg.strip()
        elif command == "Quit":
            # A transaction still open at disconnect never committed.
            close(sessions.pop(conn), False)
        elif command in ("Query", "Execute"):
            sql = arg.strip()
            if m := AUTOCOMMIT.match(sql):
                session.autocommit = m.group(1).strip("'\"").upper() in ("1", "ON", "TRUE")
                if session.autocommit:
                    close(session, True)
            elif TXN_BEGIN.match(sql):
                close(session, True)
                session.block = [True, []]
                blocks.append(session.block)
            elif TXN_COMMIT.match(sql):
                close(session, True)
            elif TXN_ROLLBACK.match(sql):
                close(session, False)
            elif m := USE.match(sql):
                session.db = m.group(1)
            elif DDL.match(sql):
                close(session, True)
            elif SESSION_ONLY.match(sql):
                pass
            elif m := TRUNCATE.match(sql):
                # TRUNCATE commits any open transaction and can't be undone.
                close(session, True)
                autocommit, session.autocommit = session.autocommit, True
                add(session, _lenient_truncate(m.group(1)))
                session.autocommit = autocommit
            else:
                add(session, sql)
    # A transaction still open when the workload exits never committed.
    for session in sessions.values():
        close(session, False)
    return [
        Statement(sql, i, keep)
        for i, (keep, sqls) in enumerate(b for b in blocks if b[1])
        for sql in sqls
    ]


def build_script(statements):
    """Returns the replay script for `statements` and the script line each
    statement starts on."""
    parts = [REPLAY_SETUP]
    line = 1 + REPLAY_SETUP.count("\n")
    starts = []  # first script line of each statement
    sizes = Counter(s.block for s in statements)
    current = None
    in_txn = False
    end_txn = ""
    for s in statements:
        if s.block != current:
            if in_txn:
                parts.append(end_txn)
                line += 1
            current = s.block
            # A block with one statement runs on its own, as the
            # workload's autocommit statements did.
            in_txn = sizes[s.block] > 1 or not s.keep
            end_txn = "COMMIT;\n" if s.keep else "ROLLBACK;\n"
            if in_txn:
                parts.append("START TRANSACTION;\n")
                line += 1
        starts.append(line)
        # The terminator goes on its own line so a trailing comment can't
        # swallow it.
        text = s.sql.rstrip().rstrip(";") + "\n;\n"
        parts.append(text)
        line += text.count("\n")
    if in_txn:
        parts.append(end_txn)
    return "".join(parts).encode("utf-8", "surrogateescape"), starts


def parse_errors(stderr, starts, count):
    """Maps the mysql client's errors back to statements and returns a list
    of (error code, message), with both entries None on success."""
    results: list[tuple[str | None, str | None]] = [(None, None)] * count
    for raw in stderr.splitlines():
        m = ERROR.match(raw)
        if not m:
            continue
        at = int(m.group(2))
        i = bisect.bisect_right(starts, at) - 1
        if i < 0:
            raise RuntimeError(f"replay setup failed: {raw}")
        if results[i][0] is None:
            results[i] = (m.group(1), m.group(3))
    return results


def _entries(log):
    """Yields (connection ID, command, argument) for each general log entry."""
    entry = None
    for raw in log:
        m = ENTRY.match(raw)
        if m:
            if entry:
                yield _decode(entry)
            entry = [m.group(1), m.group(2), m.group(3)]
        elif entry:
            entry[2] += raw
    if entry:
        yield _decode(entry)


def _decode(entry):
    conn, command, arg = entry
    # Statements can carry binary values that aren't valid UTF-8.
    # surrogateescape keeps those bytes intact through to replay.
    return int(conn), command.decode().strip(), arg.decode("utf-8", "surrogateescape").removesuffix("\n")


def _lenient_truncate(name):
    """Rewrites a TRUNCATE to call _migcheck_truncate, which replay defines."""
    table = name.split(".")[-1].strip("`")
    return "CALL _migcheck_truncate('" + table.replace("\\", "\\\\").replace("'", "''") + "')"
