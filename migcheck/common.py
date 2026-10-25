"""Types and helpers that the database adapters share."""

import re
import subprocess
from dataclasses import dataclass, field

# Appended to a duplicate key error when a row that the head migrations
# inserted holds the key. The old release might check for that row before
# inserting, which replay can't see, so the report explains this case.
MIGRATED_KEY = ", on a key that a row from the head migrations holds"


@dataclass
class Statement:
    sql: str
    # Statements from one workload transaction share a block. Replay rolls
    # back a block's effects if the workload rolled back that transaction,
    # the way a test runner discards each test's data.
    block: int
    keep: bool
    # [table, id] for each row the statement inserted, in insert order.
    ids: list = field(default_factory=list)


def run(cmd, stdin=None):
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... failed:\n{proc.stderr.strip()}")
    return proc.stdout


# Finding the tables a statement reads or writes, so the report can name the
# table behind a column error that names only an alias, or nothing.
_IDENT = r"""(?:"(?:[^"]|"")+"|`[^`]+`|[^\W\d][\w$]*)"""
_NAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT})*"
# Diesel and others wrap joins in parentheses, as in FROM ((a JOIN b ON ...).
_TABLE_REF = re.compile(r"\b(FROM|JOIN|UPDATE|INTO)(?=[\s(])[\s(]*", re.IGNORECASE)
# One table in a FROM list, with its alias, followed by a comma if the list goes on.
_TABLE = re.compile(rf"(?:ONLY\s+)?({_NAME})(\s*\()?(?:\s+(?:AS\s+)?({_IDENT}))?(\s*,\s*)?", re.IGNORECASE)
_LITERAL = re.compile(r"'(?:[^'\\]|''|\\.)*'")
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
# Words that can follow a table where an alias would go.
_NOT_ALIAS = {
    "as", "cross", "default", "except", "for", "from", "full", "group", "having", "inner", "intersect", "join",
    "lateral", "left", "limit", "natural", "offset", "on", "order", "outer", "returning", "right", "select", "set",
    "straight_join", "tablesample", "union", "using", "values", "where", "window", "with",
}
# Words after FROM or JOIN that start a subquery, not a table.
_NOT_TABLE = {"lateral", "select", "values", "with"}
# Words before UPDATE or FROM that make it something other than a table
# reference, such as FOR UPDATE, DO UPDATE SET, and IS DISTINCT FROM.
_NOT_TABLE_AFTER = {"for", "do", "key", "distinct"}


def _unquote(name, fold):
    """Returns the last part of a possibly qualified name, without quotes.
    With `fold`, unquoted names are lowercased, as Postgres does."""
    part = re.findall(_IDENT, name)[-1]
    if part[0] == '"':
        return part[1:-1].replace('""', '"')
    if part[0] == "`":
        return part[1:-1]
    return part.lower() if fold else part


def tables(sql, fold):
    """Returns ({alias or table name: {tables}}, every table) for the table
    references in `sql`, or None for the tables when the statement also reads
    from a function or subquery, so it can't be known to use only those."""
    sql = _COMMENT.sub(" ", _LITERAL.sub("''", sql))
    names, found, complete = {}, set(), True
    for ref in _TABLE_REF.finditer(sql):
        before = re.search(r"(\w+)\s*$", sql[max(0, ref.start() - 20):ref.start()])
        if before and before.group(1).lower() in _NOT_TABLE_AFTER:
            continue
        keyword, pos = ref.group(1).lower(), ref.end()
        while m := _TABLE.match(sql, pos):
            if m.group(2) and keyword != "into":  # A function, such as unnest(...).
                complete = False
                break
            table = _unquote(m.group(1), fold)
            if m.group(1).lower() in _NOT_TABLE:
                break
            found.add(table)
            names.setdefault(table, set()).add(table)
            alias = m.group(3)
            if alias and alias.lower() not in _NOT_ALIAS:
                names.setdefault(_unquote(alias, fold), set()).add(table)
            if keyword != "from" or not m.group(4) or (alias and alias.lower() in _NOT_ALIAS):
                break
            pos = m.end()
    if re.search(r"\b(FROM|JOIN)\s*\(", sql, re.IGNORECASE):
        complete = False
    return names, found if complete else None


def column_owner(sql, alias, fold):
    """Returns the table that `alias` names in `sql`, or the only table the
    statement uses when `alias` is None. Returns None if that isn't certain."""
    names, found = tables(sql, fold)
    if alias is None:
        candidates = found or set()
    else:
        candidates = names.get(_unquote(alias, fold), set())
    return next(iter(candidates)) if len(candidates) == 1 else None
