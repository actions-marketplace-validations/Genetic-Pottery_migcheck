"""Schema probes: queries generated from the schema diff.

Replay catches breaks only in code the workload runs. Probes cover the rest
of the schema change. For each table, column, or function that the head
migrations drop or rename, and each table where an insert needs a value that
it didn't need before, migcheck writes a query that the base release might
run. Probes replay like recorded statements, on both schemas, and a probe
that works on the old schema and fails on the new one is a finding.

A probe shows what might break, not what does: the base release might never
run the query. So the report lists findings apart from replay's breaks, and
says where the base release's source mentions each name.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field

from .common import Statement


@dataclass
class Probe:
    name: str  # The table, column, or function, such as "users.name".
    kind: str  # What changed, such as "column dropped".
    statements: list  # Run in order, in one block. The last one is the probe itself.
    # (table or None, word) for the report and the source search. Names from
    # the schema can carry a schema, so they don't split reliably on ".".
    subject: tuple = None
    # For an insert probe, what changed in each column it needs a value for.
    kinds: dict = field(default_factory=dict)

    @property
    def sql(self):
        return self.statements[-1]


@dataclass
class Finding:
    probe: Probe
    state: str
    msg: str
    files: list = field(default_factory=list)  # Base release files that mention the name.
    mentions: int = 0
    sql: str = ""  # The probe statement that failed.
    others: list = field(default_factory=list)  # [(column, kind)] the same table now needs too.


def short(qualified):
    """Drops the schema from "schema.table" when it's the default one."""
    schema, _, name = qualified.partition(".")
    return name if schema in ("public", "") else qualified


def generate(old, new, dialect):
    """Returns (probes, skipped) for the changes from catalog `old` to catalog
    `new`. `skipped` lists (name, reason) for changes that migcheck couldn't
    write a probe for."""
    probes, skipped = [], []
    old_rels, new_rels = old["relations"], new["relations"]
    for key, rel in old_rels.items():
        q = dialect.table(rel["schema"], rel["name"])
        label = "view" if rel["kind"] in ("v", "m") else "table"
        if key not in new_rels:
            probes.append(Probe(short(key), f"{label} dropped", [f"SELECT * FROM {q} LIMIT 0"],
                                (None, rel["name"])))
            continue
        new_rel = new_rels[key]
        if rel["kind"] == "m" and new_rel["kind"] != "m":
            probes.append(Probe(short(key), "materialized view changed", [f"REFRESH MATERIALIZED VIEW {q}"],
                                (None, rel["name"])))
        for col in rel["columns"]:
            if col not in new_rel["columns"]:
                probes.append(Probe(
                    f"{short(key)}.{col}", "column dropped", [f"SELECT {dialect.ident(col)} FROM {q} LIMIT 0"],
                    (rel["name"], col),
                ))
        if rel["kind"] in ("r", "p") and new_rel["kind"] in ("r", "p"):
            kinds = {c: _change(rel["columns"].get(c)) for c, info in new_rel["columns"].items()
                     if _requires_value(info) and not (c in rel["columns"] and _requires_value(rel["columns"][c]))}
            if kinds:
                # Columns that accepted NULL before might be set by every
                # insert from the base release, so the probe sets them, and
                # only a column that every such insert leaves out can fail.
                # With none of those, the probe leaves the columns out.
                certain = [c for c, kind in kinds.items() if kind != NOT_NULL]
                set_too = [c for c, kind in kinds.items() if kind == NOT_NULL and _can_set(old, key, c, dialect)]
                set_too = set_too if certain else []
                first = (certain or list(kinds))[0]
                try:
                    statements = dialect.insert(plan(old, key, dialect, set_too))
                except ProbeError as e:
                    skipped.extend((f"{short(key)}.{c}", str(e)) for c in kinds)
                else:
                    probes.append(Probe(f"{short(key)}.{first}", kinds[first], statements, (rel["name"], first), kinds))
    new_funcs = set(new["functions"])
    for key, func in old["functions"].items():
        if key not in new_funcs:
            args = ", ".join(dialect.null(t) for t in func["args"])
            probes.append(Probe(
                f"{func['name']}()", "function dropped or changed",
                [f"SELECT {dialect.table(func['schema'], func['name'])}({args})"], (None, func["name"]),
            ))
    return probes, skipped


NEW_COLUMN = "new column requires a value"
NOT_NULL = "column no longer accepts NULL"
NO_DEFAULT = "column lost its default"


def _change(old_col):
    """Returns what changed for a column that now requires a value, given
    the column in the old schema, which is None for a new column."""
    if old_col is None:
        return NEW_COLUMN
    return NO_DEFAULT if old_col["notnull"] else NOT_NULL


def _can_set(catalog, key, column, dialect):
    """Whether an insert probe can give `column` a value: through its foreign
    key's parent row, or with a literal of its type."""
    if any(column in fk["columns"] for fk in catalog["fks"].get(key, [])):
        return True
    return dialect.literal(catalog["relations"][key]["columns"][column]["type"], catalog) is not None


def _literal(dialect, info, column, catalog):
    literal = dialect.literal(info["type"], catalog)
    if literal is None:
        raise ProbeError(f"migcheck can't make a value of type {info['type']} for {column}")
    return literal


def _requires_value(col):
    return col["notnull"] and not (col["default"] or col["generated"] or col["identity"])


class ProbeError(Exception):
    pass


@dataclass
class Row:
    """One row an insert probe writes. Each value is ("literal", sql) or
    ("ref", index of a parent row, its column)."""
    schema: str
    table: str
    values: dict
    returning: list = field(default_factory=list)  # Columns that later rows use.


def plan(catalog, key, dialect, also=()):
    """Returns the rows for an insert into table `key` that sets the columns
    the old schema requires, and the columns in `also`: parent rows for its
    required foreign keys first, then the row itself."""
    rows = []
    _plan(catalog, key, dialect, rows, (), set(also))
    return rows


def _plan(catalog, key, dialect, rows, path, also=frozenset()):
    if key in path:
        raise ProbeError("its required foreign keys form a cycle")
    if len(path) > 8:
        raise ProbeError("its required foreign keys are nested too deeply")
    if key not in catalog["relations"]:
        raise ProbeError(f"it references {short(key)}, which migcheck doesn't read, such as a partition")
    rel = catalog["relations"][key]
    values = {}
    for fk in catalog["fks"].get(key, []):
        # A foreign key the insert sets needs its parent row, so a column in
        # `also` gets one too.
        if not all(_requires_value(rel["columns"][c]) or c in also for c in fk["columns"]):
            continue
        parent = _plan(catalog, fk["ref"], dialect, rows, (*path, key))
        parent_cols = catalog["relations"][fk["ref"]]["columns"]
        for c, ref in zip(fk["columns"], fk["ref_columns"]):
            values[c] = ("ref", parent, ref)
            if ref not in rows[parent].returning:
                rows[parent].returning.append(ref)
            if ref not in rows[parent].values and not dialect.reads_back(parent_cols[ref]):
                if parent_cols[ref]["generated"]:
                    raise ProbeError(f"its parent key {short(fk['ref'])}.{ref} is generated, and can't be read back")
                rows[parent].values[ref] = ("literal", _literal(dialect, parent_cols[ref], ref, catalog))
    for c, info in rel["columns"].items():
        if c not in values and (_requires_value(info) or c in also):
            values[c] = ("literal", _literal(dialect, info, c, catalog))
    rows.append(Row(rel["schema"], rel["name"], values))
    return len(rows) - 1


def as_statements(probes, first_block):
    """Returns the probes' statements, each probe in its own block that
    replay rolls back."""
    return [Statement(sql, first_block + i, False) for i, p in enumerate(probes) for sql in p.statements]


def findings(probes, old, new, known, cause):
    """Returns (findings, unchecked) for the probes' replay results, in
    statement order. A probe whose statements fail on the old schema can't
    be checked. Otherwise its first failure on the new schema is a finding,
    named by what the error names, through `cause`, when that resolves.
    Findings for names in `known`, the causes replay already found, are left
    out, because replay shows those breaks with the workload's own queries."""
    found, unchecked, seen = [], [], set(known)
    # An insert probe can fail in a parent row's table, which has a probe of
    # its own when it changed too. Those failures come last, so the parent
    # table's own probe names the finding.
    direct, indirect = [], []
    pos = 0
    for p in probes:
        n = len(p.statements)
        old_results, new_results = old[pos:pos + n], new[pos:pos + n]
        pos += n
        failed_old = next(((st, msg) for st, msg in old_results if st is not None), None)
        if failed_old:
            unchecked.append((p.name, f"the probe fails on the old schema too: [{failed_old[0]}] {failed_old[1]}"))
            continue
        failed = next(((sql, st, msg) for sql, (st, msg) in zip(p.statements, new_results) if st is not None), None)
        if failed:
            resolved = cause(failed[2], failed[0])
            what = subject(resolved) if resolved else p.subject
            same_table = not p.kinds or not resolved or what[0] == p.subject[0]
            (direct if same_table else indirect).append((p, failed, resolved, what))
    for p, (sql, state, msg), resolved, what in direct + indirect:
        name = resolved or p.name
        if name in seen:
            continue
        seen.add(name)
        if p.kinds and what[0] == p.subject[0]:
            kind = p.kinds.get(what[1], p.kind)
            others = [(c, k) for c, k in p.kinds.items() if c != what[1]]
        elif p.kinds:
            kind, others = f"insert into a parent row of {p.subject[0]} fails", []
        else:
            kind, others = p.kind, []
        found.append(Finding(Probe(name, kind, p.statements, what, p.kinds), state, msg, sql=sql, others=others))
    return found, unchecked


# Directories that hold migrations, dependencies, or build output rather
# than the app's own code. Some apps commit these too.
SKIP_DIRS = re.compile(
    r"^(\..*|node_modules|vendor|dist|build|target|out|coverage|tmp|log|logs|public|__pycache__|venv|"
    r".*migrat.*)$",
    re.IGNORECASE,
)
# Lockfiles, build output, schema dumps, and documentation, which mention
# names without using them.
SKIP_FILES = re.compile(
    r"(\.lock|-lock\.json|-lock\.yaml|\.min\.js|\.map|\.snap|^schema\.rb|^structure\.sql"
    r"|\.md|\.rst|\.adoc|\.txt)$",
    re.IGNORECASE,
)


def _forms(word):
    """Returns a pattern for `word` as written or without underscores,
    ignoring case, so it matches snake_case, camelCase, and PascalCase."""
    forms = {re.escape(word), re.escape(word.replace("_", ""))}
    return rf"(?<![\w$])(?:{'|'.join(sorted(forms))})(?![\w$])"


def _singular(word):
    for plural, single in (("ies", "y"), ("sses", "ss"), ("shes", "sh"), ("ches", "ch"), ("xes", "x")):
        if word.endswith(plural):
            return word[: -len(plural)] + single
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def subject(name):
    """Returns (table, word) for a finding's name: the table that a column
    belongs to, or None, and the column, table, or function name."""
    parts = name.removesuffix("()").split(".")
    return (parts[-2] if len(parts) > 1 else None), parts[-1]


def _source_files(root):
    """Returns the paths under `root` that git tracks, which leaves out what
    the workload built or installed, such as generated clients. Outside a git
    checkout, returns every file."""
    proc = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True)
    if proc.returncode == 0:
        return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)
    return sorted(
        os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
        for d, _, files in os.walk(root) for f in files
    )


def search(root, found, limit=3):
    """Fills in each finding's files: the base release's files outside
    migrations that mention its name, as written or without underscores,
    ignoring case. For a column, a file must also mention the table, by its
    name or its singular, the way models are often named. Only files that
    git tracks count. Test files sort last."""
    patterns = []
    for f in found:
        table, word = f.probe.subject or subject(f.probe.name)
        tables = {table, _singular(table)} if table else set()
        patterns.append((
            re.compile(_forms(word), re.IGNORECASE),
            re.compile("|".join(_forms(t) for t in sorted(tables)), re.IGNORECASE) if tables else None,
        ))
    hits = [[] for _ in found]
    for rel in _source_files(root):
        parts = rel.split("/")
        if any(SKIP_DIRS.match(d) for d in parts[:-1]) or SKIP_FILES.search(parts[-1]):
            continue
        path = os.path.join(root, rel)
        try:
            if os.path.getsize(path) > 2_000_000:
                continue
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\0" in data[:1024]:
            continue
        text = data.decode("utf-8", "replace")
        for i, (word, table) in enumerate(patterns):
            if word.search(text) and (table is None or table.search(text)):
                hits[i].append(rel)
    for f, files in zip(found, hits):
        files.sort(key=lambda p: bool(re.search(r"(^|/)(tests?|spec|__tests__)(/|$)|[._-](test|spec)\.", p)))
        f.files, f.mentions = files[:limit], len(files)
