"""Checks that a release's migrations don't break the release before it.

During a rolling deploy, or after a rollback, the previous release runs
against the new schema. migcheck records the queries the previous release
makes, then replays them against its schema and against the new one. A query
that works on the old schema and fails on the new one breaks production.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict

from .common import MIGRATED_KEY
from .mysql import MySQL
from .postgres import Postgres

ADAPTERS = {"postgres": Postgres, "mysql": MySQL}
# Marks migcheck's pull request comment, so a later run can update it.
MARKER = "<!-- migcheck -->"
MARKDOWN = None


def main():
    parser = argparse.ArgumentParser(prog="migcheck", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    parser.add_argument("--repo", default=".", help="git repository to check (default: .)")
    parser.add_argument("--base", required=True, help="ref of the release running in production")
    parser.add_argument("--head", required=True, help="ref of the release being deployed")
    parser.add_argument("--migrate", required=True, help="shell command that applies migrations to $DATABASE_URL")
    parser.add_argument("--workload", required=True, help="shell command that exercises the app against $DATABASE_URL, such as its test suite")
    parser.add_argument("--db", choices=ADAPTERS, default="postgres", help="database the app uses (default: postgres)")
    defaults = " or ".join(a.default_image for a in ADAPTERS.values())
    parser.add_argument("--image", help=f"database Docker image (default: {defaults})")
    parser.add_argument("--head-name", help="name to show for --head in the result, such as \"this pull request\"")
    parser.add_argument("--markdown", metavar="PATH", help="also write the result as Markdown to PATH, for a pull request comment")
    parser.add_argument(
        "--require-workload-success", action="store_true",
        help="exit with code 2 if the workload command fails, instead of checking the queries it ran",
    )
    args = parser.parse_args()
    global MARKDOWN
    MARKDOWN = args.markdown

    repo = os.path.abspath(args.repo)
    workdir = tempfile.mkdtemp(prefix="migcheck-")
    base_dir = os.path.join(workdir, "base")
    head_dir = os.path.join(workdir, "head")
    adapter = ADAPTERS[args.db]
    db = adapter(args.image or adapter.default_image)
    try:
        _git(repo, "worktree", "add", "--detach", "-q", base_dir, args.base)
        _git(repo, "worktree", "add", "--detach", "-q", head_dir, args.head)

        step(f"Starting {db.image}")
        db.start()

        step(f"Applying {args.base} migrations")
        db.create_db("mc_template")
        _sh(args.migrate, base_dir, db.url("mc_template"))
        # Without tables, both schemas stay empty and every query passes.
        if not db.has_tables("mc_template"):
            fail(
                "the migrate command created no tables in the database at $DATABASE_URL. "
                "Check that it reads its connection from DATABASE_URL, as described in RECIPES.md."
            )

        step(f"Recording {args.base} workload queries")
        db.create_db("mc_capture", template="mc_template")
        db.record()
        # Failing tests still issue queries, so a failing workload is a warning.
        workload_exit = _sh(args.workload, base_dir, db.url("mc_capture"), check=False)
        if workload_exit and args.require_workload_success:
            fail(f"the workload exited with code {workload_exit}, and --require-workload-success is set")
        statements = db.statements(exclude={"mc_template"})
        if not statements:
            fail("the workload ran no queries, so there is nothing to check")

        step(f"Applying {args.head} migrations")
        db.create_db("mc_old", template="mc_template")
        db.create_db("mc_new", template="mc_template")
        migrated_after = db.mark()
        _sh(args.migrate, head_dir, db.url("mc_new"))
        schema_changed = db.schema("mc_old") != db.schema("mc_new")

        step(f"Replaying {len(statements)} statements against both schemas")
        old = db.replay("mc_old", statements)
        new = db.replay("mc_new", statements, migrated_after)
    except Exception as e:  # Exit code 1 means a query breaks, so nothing else may use it.
        fail(f"{type(e).__name__}: {e}")
    finally:
        db.stop()
        for d in (base_dir, head_dir):
            if os.path.isdir(d):
                _git(repo, "worktree", "remove", "--force", d)
        shutil.rmtree(workdir, ignore_errors=True)

    sys.exit(report(db, args.base, args.head, statements, old, new, workload_exit, args.head_name, schema_changed))


def report(db, base, head, statements, old, new, workload_exit, head_name=None, schema_changed=True):
    """Prints the result, writes it as Markdown if requested, and returns the exit code."""
    head_md = head_name or f"`{head}`"
    head = head_name or head
    skipped = sum(1 for state, _ in old if state)
    if skipped == len(statements):
        fail(
            f"all {skipped} statements fail on {base}'s own schema, so there is nothing to compare. "
            f"First error: [{old[0][0]}] {old[0][1]}"
        )
    roots, knock_on = _classify(db, statements, old, new)
    groups = _group(db, roots)
    count = sum(len(v) for v in roots.values())
    causes = f"{len(groups)} cause{'s' if len(groups) != 1 else ''}"
    checked = len(statements) - skipped
    # Each note is (level, text, Markdown text). A workload that stopped early
    # can pass without having run the queries that break, so a pass then
    # carries a warning in its verdict.
    notes = []
    if workload_exit:
        text = f"The workload exited with code {workload_exit}. If it stopped early, the queries it didn't run weren't checked."
        if not roots:
            text += " To treat a failing workload as an error, use --require-workload-success."
        notes.append(("Warning" if not roots else "Note", text, text))
    # Statements that fail on both schemas aren't checked. When many do,
    # replay is often missing data the workload set up some other way.
    if skipped * 10 >= len(statements):
        text = (
            f"{skipped} of {len(statements)} statements ({skipped * 100 // len(statements)}%) fail on {base}'s own "
            "schema too, so migcheck didn't check them. Replay might be missing data that the workload set up "
            "outside the recorded queries. The most common errors are listed above."
        )
        notes.append(("Warning", text, text.replace(f"{base}'s", f"`{base}`'s")))
    # A migrate command that silently applies nothing passes every query.
    if not schema_changed:
        text = "{}'s migrations didn't change the schema. If {} adds migrations, check that --migrate applies them to $DATABASE_URL."
        notes.append(("Note", text.format(head, head), text.format(head_md, head_md)))

    print()
    if skipped:
        print(f"Skipped {skipped} statements that also fail on {base}'s schema. Most common errors:")
        for state, msg, n in _skip_reasons(old):
            print(f"    [{state}] {msg[:160]} ({n})")
        print()
    if not roots and workload_exit:
        print(f"PASS WITH WARNING: all {checked} statements from {base} still work after {head}'s migrations, "
              f"but the workload exited with code {workload_exit}.")
    elif not roots:
        print(f"PASS: all {checked} statements from {base} still work after {head}'s migrations.")
    else:
        print(f"FAIL: {count} statements from {base} break after {head}'s migrations, from {causes}:")
        width = min(40, max(len(_label(c, e)) for c, e in groups))
        for cause, errors in groups:
            print(f"    {_label(cause, errors):<{width}}  {sum(len(v) for v in errors.values())} statements")
        print()
        for cause, errors in groups:
            stmts = [s for v in errors.values() for s in v]
            if cause:
                print(f"  {cause}")
                for (state, msg), v in errors.items():
                    print(f"      [{state}] {msg} ({len(v)})")
            else:
                (state, msg), = errors
                print(f"  [{state}] {msg}")
            print(f"      {len(stmts)} statements in {_blocks(stmts)} transactions. Examples:")
            for sql in _examples(stmts, 160):
                print(f"      {sql}")
            if any(msg.endswith(MIGRATED_KEY) for _, msg in errors):
                print(f"      {_migrated_key_note(base, head)}")
            print()
        if knock_on:
            print("  Later failures in the same transactions, likely caused by the errors above:")
            for (state, msg), n in knock_on.items():
                print(f"      [{state}] {msg} ({n})")
            print()
        print(f"{base} fails during a rolling deploy of {head}, and after a rollback to {base}.")
    for level, text, _ in notes:
        print(f"\n{level}: {text}")

    if MARKDOWN:
        md = [MARKER]
        if not roots:
            md.append("### migcheck: PASS with a warning" if workload_exit else "### migcheck: PASS")
            md.append("")
            md.append(f"All {checked:,} statements from `{base}` still work after {head_md}'s migrations.")
        else:
            md.append("### migcheck: FAIL")
            md.append("")
            md.append(
                f"{count:,} statements from `{base}` break after {head_md}'s migrations, from {causes}. "
                f"`{base}` fails during a rolling deploy of {head_md}, and after a rollback to `{base}`."
            )
            md.append("")
            md.append("| Cause | Statements |")
            md.append("|---|---|")
            for cause, errors in groups:
                label = f"`{cause}`" if cause else _md_escape(_label(cause, errors))
                md.append(f"| {label.replace('|', '&#124;')} | {sum(len(v) for v in errors.values()):,} |")
            for cause, errors in groups:
                stmts = [s for v in errors.values() for s in v]
                md.append("")
                if cause:
                    md.append(f"**`{cause}`**")
                    md.append("")
                    for (state, msg), v in errors.items():
                        md.append(f"- `{state}` {_md_escape(msg)} ({len(v):,})")
                    md.append("")
                else:
                    (state, msg), = errors
                    md.append(f"**`{state}` {_md_escape(msg)}**")
                md.append(f"{len(stmts):,} statements in {_blocks(stmts):,} transactions. Examples:")
                md.append("")
                md.append("```sql")
                md.extend(_examples(stmts, 500))
                md.append("```")
                if any(msg.endswith(MIGRATED_KEY) for _, msg in errors):
                    md.append("")
                    md.append(_migrated_key_note(f"`{base}`", head_md))
            if knock_on:
                md.append("")
                md.append("<details><summary>Later failures in the same transactions, likely caused by the errors above</summary>")
                md.append("")
                for (state, msg), n in knock_on.items():
                    md.append(f"- `{state}` {_md_escape(msg)} ({n:,})")
                md.append("")
                md.append("</details>")
        if skipped:
            md.append("")
            md.append(f"<details><summary>Skipped {skipped:,} statements that also fail on <code>{base}</code>'s schema</summary>")
            md.append("")
            md.append("Most common errors:")
            md.append("")
            for state, msg, n in _skip_reasons(old):
                md.append(f"- `{state}` {_md_escape(msg[:500])} ({n:,})")
            md.append("")
            md.append("</details>")
        for level, _, text in notes:
            md.append("")
            md.append(f"> [!{level.upper()}]\n> {text}")
        _write_markdown(md)
    return 1 if roots else 0


def _classify(db, statements, old, new):
    """Splits the statements that break into root causes and knock-on errors.

    The first break in a transaction is a root cause. A later integrity error
    in the same transaction usually follows from it, such as a foreign key to
    a row whose insert failed. A foreign key error after any earlier break is
    also a knock-on, because apps often commit each request separately, so the
    failed insert and the reference to it can sit in different transactions.
    """
    roots = defaultdict(list)
    knock_on = defaultdict(int)
    broken_blocks = set()
    for s, (old_state, _), (new_state, new_msg) in zip(statements, old, new):
        if old_state is not None or new_state is None:
            continue
        key = (new_state, new_msg)
        follows_break = s.block in broken_blocks and db.is_integrity_error(new_state)
        if key not in roots and (follows_break or (broken_blocks and db.is_foreign_key_error(new_state))):
            knock_on[key] += 1
            continue
        broken_blocks.add(s.block)
        roots[key].append(s)
    return roots, knock_on


def _group(db, roots):
    """Groups the breaking statements by the table, column, or function that
    each one's error names, so a dropped column is one cause however each
    statement referred to it. Returns [(name, {(state, msg): statements})],
    largest first. An error that names nothing migcheck can resolve is a
    cause of its own, with None for its name."""
    groups = {}
    for key, stmts in roots.items():
        for s in stmts:
            name = db.cause(key[1], s.sql)
            groups.setdefault(name or key, {}).setdefault(key, []).append(s)
    grouped = [
        (name if isinstance(name, str) else None, dict(sorted(errors.items(), key=lambda e: -len(e[1]))))
        for name, errors in groups.items()
    ]
    return sorted(grouped, key=lambda g: -sum(len(v) for v in g[1].values()))


def _label(cause, errors):
    if cause:
        return cause
    (state, msg), = errors
    return f"[{state}] {msg[:60]}{'...' if len(msg) > 60 else ''}"


def _migrated_key_note(base, head):
    return (
        f"{head}'s migrations inserted a row with this key. If {base} checks for the row before "
        "inserting it, as code that seeds missing rows does, this isn't a break."
    )


def _skip_reasons(old, limit=5):
    """Returns the most common errors on the old schema as (state, example
    message, count), grouped by state, because messages often quote values."""
    counts = Counter(state for state, _ in old if state)
    examples = {}
    for state, msg in old:
        if state:
            examples.setdefault(state, msg)
    return [(state, examples[state], n) for state, n in counts.most_common(limit)]


def _blocks(stmts):
    return len({s.block for s in stmts})


def _examples(stmts, width):
    examples = dict.fromkeys(" ".join(s.sql.split())[:width] for s in stmts)
    return list(examples)[:3]


def _md_escape(text):
    return text.replace("<", "&lt;").replace("*", "\\*").replace("_", "\\_").replace("`", "'")


def _write_markdown(lines):
    with open(MARKDOWN, "w") as f:
        f.write("\n".join(lines) + "\n")


def fail(msg):
    """Exits with code 2, which means migcheck couldn't compare."""
    print(f"migcheck: {msg}", file=sys.stderr)
    if MARKDOWN:
        _write_markdown([MARKER, "### migcheck: couldn't compare", "", "```", msg, "```"])
    sys.exit(2)


def _version():
    # The GitHub Action runs migcheck from a checkout without installing it,
    # so a checkout's pyproject.toml comes first. Installed packages have no
    # pyproject.toml next to them.
    pyproject = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")
    if os.path.isfile(pyproject):
        with open(pyproject) as f:
            match = re.search(r'^version = "(.+)"$', f.read(), re.M)
        if match:
            return match.group(1)
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("migcheck")
    except PackageNotFoundError:
        return "unknown"


def step(msg):
    print(f"==> {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr)


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True)


def _sh(cmd, cwd, url, check=True):
    proc = subprocess.run(
        cmd, shell=True, cwd=cwd, env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True,
    )
    if proc.returncode != 0 and not check:
        print(f"migcheck: warning: command exited with {proc.returncode}: {cmd}", file=sys.stderr)
        sys.stderr.write(proc.stderr[-2000:])
    elif proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        fail(f"command failed in {cwd}: {cmd}")
    return proc.returncode


if __name__ == "__main__":
    main()
