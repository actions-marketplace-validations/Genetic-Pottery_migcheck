<p align="center">
  <img src="https://raw.githubusercontent.com/Genetic-Pottery/migcheck/main/assets/logo-light.png#gh-light-mode-only" alt="migcheck logo" width="280">
  <img src="https://raw.githubusercontent.com/Genetic-Pottery/migcheck/main/assets/logo.png#gh-dark-mode-only" alt="migcheck logo" width="280">
</p>

<p align="center">
  <a href="https://github.com/Genetic-Pottery/migcheck/actions/workflows/ci.yml"><img src="https://github.com/Genetic-Pottery/migcheck/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/Genetic-Pottery/migcheck/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Genetic-Pottery/migcheck" alt="License"></a>
  <a href="https://github.com/Genetic-Pottery/migcheck#run-it-in-github-actions"><img src="https://img.shields.io/badge/GitHub_Action-available-2088FF?logo=githubactions&logoColor=white" alt="GitHub Action"></a>
  <a href="https://github.com/Genetic-Pottery/migcheck/releases/latest"><img src="https://img.shields.io/github/v/release/Genetic-Pottery/migcheck" alt="Release"></a>
  <a href="https://pypi.org/project/migcheck/"><img src="https://img.shields.io/pypi/v/migcheck" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
</p>

migcheck checks that a release's database migrations don't break the release
running in production.

During a rolling deploy, the old release keeps serving traffic after the new
release's migrations run. After a rollback, it runs against the new schema
indefinitely. A migration that passes review and passes the new release's tests
can still take down the old release. Migration linters check the SQL. migcheck
runs the old release's queries against the new schema and tells you which ones
fail.

For example, this is migcheck's output for the
[demo](https://github.com/Genetic-Pottery/migcheck#try-the-demo), where release
`v2-rename` renames a column that `v1` uses:

```
FAIL: 4 statements from v1 break after v2-rename's migrations, from 1 cause:
    users.name  4 statements

  users.name
      [42703] column "name" of relation "users" does not exist (2)
      [42703] column "name" does not exist (2)
      4 statements in 1 transactions. Examples:
      INSERT INTO users (name, email) VALUES ('Ada O''Brien', 'ada@example.com') RETURNING id
      UPDATE users SET name = 'Ada Lovelace' WHERE id = '1'
      SELECT id, name, email FROM users WHERE id = '1'

v1 fails during a rolling deploy of v2-rename, and after a rollback to v1.
```

migcheck supports Postgres and MySQL. It's an early release.

## Tested with

migcheck has caught breaking migrations in real upgrades of the following
apps, using each app's own test suite. For the results, see
[EVIDENCE.md](EVIDENCE.md). For setup steps by framework, see
[RECIPES.md](RECIPES.md).

| Framework | Language | Database | App |
|---|---|---|---|
| Django | Python | Postgres | Plane |
| Active Record (Rails) | Ruby | Postgres | Mastodon |
| xorm | Go | Postgres, MySQL | Gitea |
| Hibernate | Java | Postgres, MySQL | Keycloak |
| Knex | Node | MySQL | Ghost |
| TypeORM | Node | Postgres | n8n |
| Prisma | Node | Postgres | Cal.com |
| Sequelize | Node | Postgres | Outline |
| Diesel | Rust | Postgres | Lemmy |
| Ecto | Elixir | Postgres | Akkoma |
| Laravel | PHP | MySQL | BookStack |
| Entity Framework | .NET | Postgres | Bitwarden |

## What it catches

migcheck catches any change that makes the old release's queries fail,
including the following:

- Renamed or dropped columns and tables.
- New `NOT NULL` columns without a database default. The old release's inserts
  don't set them.
- New constraints that the old release's writes violate.
- Row-level security policies that the old release's writes violate, when
  the app switches to the user's role, as Supabase's PostgREST does.
- Changed column types that the old release's values don't fit.

The `NOT NULL` case is easy to miss in Django. When a migration adds a field
with a `default`, Django fills existing rows with it and then drops the default
from the database. The migration looks safe, but the old release's inserts fail.

Run migcheck on the database you deploy to. The same query can pass on one
database and fail on another.

## Install

migcheck needs Docker, git, and Python 3.10 or later. It has no Python
dependencies and doesn't need a database client on the host.

```sh
pipx install migcheck
```

## Use it

Run migcheck from your app's repository:

```sh
migcheck --base <deployed-ref> --head <new-ref> \
  --migrate "<command that applies migrations>" \
  --workload "<command that runs your test suite>"
```

- `--base` is the release running in production, such as its tag.
- `--head` is the release you're about to deploy.
- `--migrate` and `--workload` are shell commands. migcheck runs them in a
  checkout of each ref, with `DATABASE_URL` pointing at a throwaway database.
- `--db` is the database your app uses: `postgres`, the default, or `mysql`.

Your app must read its database connection from `DATABASE_URL` in the test
environment. If it reads other variables, wrap the commands in a script that
sets them.

`DATABASE_URL` starts with `postgresql://` or `mysql://`. If your framework
expects another scheme, such as `mysql2://` for Rails, rewrite it in the
command:

```sh
migcheck --db mysql --base v5.2.0 --head main \
  --migrate 'DATABASE_URL=mysql2${DATABASE_URL#mysql} bin/rails db:migrate' \
  --workload 'DATABASE_URL=mysql2${DATABASE_URL#mysql} bin/rails test'
```

For example, for a Django app:

```sh
migcheck --base v1.4.0 --head main \
  --migrate "python manage.py migrate" \
  --workload "pytest"
```

For Rails, SQLAlchemy, Go, Node, Prisma, Sequelize, Rust, Elixir, Laravel,
Entity Framework, Supabase, and apps that migrate on startup, and for apps that
don't read `DATABASE_URL`, see [RECIPES.md](RECIPES.md).

migcheck exits with one of the following codes:

| Code | Meaning |
|---|---|
| `0` | Every query the old release made still works. |
| `1` | At least one query breaks. |
| `2` | migcheck couldn't compare, for example because every query failed on the old schema too. |

A failing workload, such as a test suite with failing tests, still issues
queries, so migcheck checks the queries it ran. If none of them break,
migcheck reports a pass with a warning, because a workload that stopped early
can skip the queries that break. To make a failing workload exit with code `2`
instead, use `--require-workload-success`.

### Apps that deploy in two steps

Some apps split their migrations into two sets. The first set runs before the
new release rolls out, and the second runs after every server of the old
release has stopped. Mastodon and GitLab call the second set post-deployment
migrations.

The old release never runs against the second set, so `--migrate` must apply
only the first. Otherwise migcheck reports breaks that the app's deploy
process already avoids. For example, Mastodon skips its post-deployment
migrations when `SKIP_POST_DEPLOYMENT_MIGRATIONS` is set.

### Options

| Flag | Description |
|---|---|
| `--base` | Ref of the release running in production. Required. |
| `--head` | Ref of the release being deployed. Required. |
| `--migrate` | Shell command that applies migrations to `$DATABASE_URL`. Required. |
| `--workload` | Shell command that exercises the app against `$DATABASE_URL`, such as its test suite. Required. |
| `--db` | Database the app uses: `postgres` or `mysql`. Default: `postgres` |
| `--repo` | Git repository to check. Default: `.` |
| `--image` | Database Docker image. Use the major version you run in production. Default: `postgres:17-alpine` or `mysql:8.4` |
| `--head-name` | Name to show for `--head` in the result, such as `this pull request`. |
| `--markdown` | Also write the result as Markdown to this path, for a pull request comment. |
| `--require-workload-success` | Exit with code `2` if the workload command fails, instead of checking the queries it ran. |
| `--fail-on-probes` | Exit with code `1` when a schema probe fails, even if every recorded statement still works. |
| `--no-probes` | Don't generate schema probes. |
| `--version` | Print migcheck's version and exit. |

## See it in action

The [migcheck-example](https://github.com/Genetic-Pottery/migcheck-example)
repository runs the action on its pull requests. Each pull request adds a
migration, and migcheck comments with the result:

- [Rename a column](https://github.com/Genetic-Pottery/migcheck-example/pull/1)
  fails, because the production release still uses the old name.
- [Add a `NOT NULL` column and drop its default](https://github.com/Genetic-Pottery/migcheck-example/pull/2)
  fails, because the production release's inserts don't set it.
- [Add a nullable column](https://github.com/Genetic-Pottery/migcheck-example/pull/3)
  passes.

## Run it in GitHub Actions

The migcheck action runs the check on each pull request and posts the result
as a comment. It updates the same comment when the pull request changes.

```yaml
name: migcheck
on:
  pull_request:
    paths: ["migrations/**"]

permissions:
  contents: read
  pull-requests: write

jobs:
  migcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      # Install your app's dependencies here.
      - uses: Genetic-Pottery/migcheck@v0
        with:
          migrate: python manage.py migrate
          workload: pytest
```

The checkout step must use `fetch-depth: 0`, so that migcheck can read the
base release and its tags.

By default, the base release is the newest tag reachable from the pull
request's base branch. If you tag releases differently, set `base` to the ref
running in production.

The action takes the following inputs:

| Input | Description |
|---|---|
| `migrate` | Shell command that applies migrations to `$DATABASE_URL`. Required. |
| `workload` | Shell command that exercises the app against `$DATABASE_URL`. Required. |
| `base` | Ref of the release running in production. Default: the newest tag reachable from the base branch. |
| `head` | Ref of the release being deployed. Default: `HEAD` |
| `db` | `postgres` or `mysql`. Default: `postgres` |
| `image` | Database Docker image. Default: `postgres:17-alpine` or `mysql:8.4` |
| `require-workload-success` | Whether a failing workload command is an error. Default: `false` |
| `fail-on-probes` | Whether a failing schema probe fails the job. Default: `false` |
| `working-directory` | Directory of the repository to check. Default: `.` |
| `comment` | Whether to post a pull request comment. Default: `true` |
| `comment-key` | Keeps comments separate when a workflow runs migcheck more than once. Default: `default` |

The action fails the job when a query breaks or migcheck can't compare, and
sets the `result` output to `pass`, `fail`, or `error`. It also writes the
result to the job summary.

Pull requests from forks get a read-only token, so the action can't comment
on them. The result still appears in the job summary.

## Try the demo

The demo builds a small repository with one base release and four candidate
releases. Three of the candidates break the base release. Replay catches two
of them, and a schema probe catches the third, which the demo's test never
reaches.

```sh
python3 -m venv /tmp/mc-venv
/tmp/mc-venv/bin/pip install "psycopg[binary]"
examples/make-demo.sh /tmp/mc-demo

migcheck --repo /tmp/mc-demo --base v1 --head v2-rename \
  --migrate "/tmp/mc-venv/bin/python migrate.py" \
  --workload "/tmp/mc-venv/bin/python test_app.py"
```

| Tag | Migration | Result |
|---|---|---|
| `v2-expand` | Adds `full_name` next to `name` | `PASS` |
| `v2-rename` | Renames `name` to `full_name` | `FAIL` |
| `v2-notnull` | Adds `plan text NOT NULL` with no default | `FAIL` |
| `v2-untested` | Drops `nickname`, which only untested code uses | `PASS WITH WARNING`, from a schema probe |

For a demo on Supabase, with PostgREST and row-level security, see
[Supabase](RECIPES.md#supabase) in `RECIPES.md`.

## How it works

1. migcheck checks out the base and head refs into temporary git worktrees.
2. It starts a throwaway database container with statement logging on.
3. It applies the base release's migrations, runs the base release's workload,
   and records every query the workload makes.
4. It makes two copies of the base schema and applies the head release's
   migrations to one of them.
5. It replays the recorded queries against both copies, with bind parameters
   inlined.
6. It reports each query that succeeds on the old schema and fails on the new
   one.

Replaying against both copies filters out queries that fail for reasons
unrelated to the migration, such as missing seed data. The report counts these
skipped queries and lists their most common errors, so you can tell whether
replay missed data that later queries needed. If 10% or more of the queries
are skipped, the report includes a warning.

migcheck also checks for two signs of a wrong setup. If the base release's
migrate command creates no tables in the database at `DATABASE_URL`, migcheck
exits with code `2`. If the head release's migrations don't change the schema,
the report includes a note.

Replay follows the workload's transactions. If the workload rolled back a
transaction, as test runners do after each test, replay rolls back its effects
too. The report lists the first failure in each transaction as a root cause and
groups later integrity errors in that transaction separately.

The report groups root causes by the table, column, or function that each
error names. Databases word one missing column differently in reads and writes,
and a read's error names the query's alias for the table, not the table. To
find the table, migcheck looks up the alias in the statement.

migcheck also accounts for how test runners use the database:

- It records queries from every database the workload touches, because test
  runners such as Django's create their own.
- It drops DDL and transaction-control statements, which come from the test
  runner rather than the app.
- It replays `TRUNCATE` by emptying whichever listed tables exist, so cleanup
  between tests doesn't depend on the schema.
- On Postgres, it records the primary key of each row the workload inserts
  and gives replayed inserts the same keys, so later statements that
  reference those rows find them. If a migration already inserted a row with
  that key, the replayed insert gets a new key instead, as it would in
  production. On MySQL, replay starts from the same
  auto-increment counters as the workload, so inserts get the same keys when
  the workload runs one test at a time.

### Schema probes

Replay checks only the queries the workload made, so it misses breaks in code
the tests never run. So migcheck also writes probe queries from the schema
change:

- For each table, view, column, or function that the head release's
  migrations drop or rename, it writes a query that uses it.
- For each table where an insert needs a value that it didn't need before,
  it writes an insert that sets only the columns the base schema requires. It
  inserts parent rows for the required foreign keys first. The report labels
  what changed:
  - "New column requires a value": a new `NOT NULL` column without a database
    default. Every insert from the base release leaves it out.
  - "Column lost its default": a `NOT NULL` column whose default was dropped.
    Every insert from the base release that relied on the default fails.
  - "Column no longer accepts NULL": a nullable column made `NOT NULL`. The
    base release might always set it, so the probe sets it when the table has
    a change of the first two kinds, and leaves it out otherwise.

Probes replay with the recorded queries, on both schemas, and roll back. A
probe that works on the old schema and fails on the new one is a finding.
Probes for changes that replay already caught are left out.

A finding shows what might break, not what does, because the base release
might never run the query. A column that the base release stopped using
before the head release dropped it is safe to drop. So findings appear in
their own section of the report, and they add a warning to a pass instead of
failing it. For each finding, the report lists the base release's files
outside its migrations that mention the name. For a column, a file must also
mention its table. To fail the run on findings, use `--fail-on-probes`.

Everything runs locally. The database container listens only on `127.0.0.1`,
and migcheck removes the container and worktrees when it finishes.

## Limitations

- migcheck checks only the queries the workload makes. If your tests never
  touch a table, replay can't see it break. Schema probes cover dropped and
  renamed tables, columns, and functions, and required columns. They don't
  cover changes that depend on values, such as a new unique index or a
  narrower type, or index changes that break an `ON CONFLICT`. On Postgres,
  probes need version 12 or later. On older versions, migcheck runs without
  them and says so in the report.
- migcheck applies migrations to an empty database. A migration that fails or
  locks for a long time on a large production table can pass here.
- Replay runs one statement at a time. Failures that depend on concurrency
  don't show up.
- migcheck checks for errors, not wrong results. A query that returns
  different rows on the new schema passes.
- On Postgres, replay ignores `SAVEPOINT` and `ROLLBACK TO SAVEPOINT`.
- On Postgres, replay resets the role at the start of each transaction. A
  role that an app sets once per connection, with `SET ROLE`, doesn't carry
  into the connection's later transactions.
- On MySQL, a workload that runs tests in parallel can leave replayed rows
  with different keys than the workload saw. Statements that reference those
  keys then fail on both schemas, and migcheck skips them.
- migcheck supports MySQL 8.0 and later. It doesn't support MariaDB, whose
  query log has a different format.
- If migcheck can't tell which table a column error refers to, the error
  stays a cause of its own. This happens when a statement reads from a
  subquery or a function, or when an error names neither the table nor an
  alias.

## Run the tests

To run the tests, run the following from the repository root:

```sh
python3 -m unittest
```

The replay tests start Postgres and MySQL containers, so they need Docker.
Without Docker, they're skipped.

The parser tests read logs that were recorded from real servers. To record
them again, for example after you change the workload in
`tests/capture_fixtures.py`, run `python3 -m tests.capture_fixtures`.

## Release a version

1. Set `version` in `pyproject.toml` to the new version, such as `0.2.0`.
1. Add a `## v0.2.0 - YYYY-MM-DD` section to `CHANGELOG.md`.
1. Commit, then tag and push:

   ```sh
   git tag -a v0.2.0 -m v0.2.0
   git push origin main v0.2.0
   ```

The release workflow checks that the tag matches `pyproject.toml` and that
`CHANGELOG.md` has a section for it, then runs the tests and builds the
package. If they pass, it publishes the package to PyPI, creates the GitHub
release with that section as its notes, and moves the major version tag, such
as `v0`, to the release. If a check fails before the PyPI upload, nothing is
released. To retry, delete the tag, fix the problem, and push the tag again.
PyPI never accepts the same version twice, so after the upload, a fix needs a
new version.

## License

Apache 2.0. See [LICENSE](https://github.com/Genetic-Pottery/migcheck/blob/main/LICENSE).
