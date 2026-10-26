# Changelog

## v0.5.0 - 2026-10-26

### Added

- migcheck runs on Supabase's Postgres image, and on other images where
  `postgres` isn't a superuser. migcheck keeps the image's server command and
  config, runs its own SQL as the image's superuser, and starts its databases
  from a copy of the `postgres` database that the image set up.
- `RECIPES.md` shows how to set up migcheck for Supabase, with scripts in
  `examples/supabase` that apply the Auth server's migrations and run the
  tests through PostgREST and the Auth server.
  `examples/make-supabase-demo.sh` builds a demo to try them on.
- `EVIDENCE.md` includes the Supabase example, and says which Keycloak tests
  the Keycloak runs leave out and why.
- Replay applies the role and settings that PostgREST switches to in each
  request, so a migration that changes a row-level security policy can break
  replayed writes.

### Fixed

- Replay skipped every write that came through PostgREST, because the
  request body lost its `json` type when migcheck inlined it.
- Replay dropped statements from roles and databases that turn
  `log_statement` or `log_min_messages` down, as Supabase's Auth and Storage
  roles do.
- Replay failed on `DEALLOCATE`, which drivers such as Go's pgx send for
  prepared statements that replay never creates.
- A replayed `SET ROLE` carried into the statements of later transactions,
  including other sessions' and the schema probes, and broke replay when the
  role couldn't read migcheck's own schema. Replay resets the role at the
  start of each transaction.

## v0.4.0 - 2026-10-25

### Added

- migcheck writes schema probes: queries generated from the schema change,
  for the tables, columns, and functions that the head release's migrations
  drop or rename, and for inserts that need a value they didn't need before.
  Probes run on Postgres and MySQL. A probe that works on the old schema and fails on the
  new one is reported in its own section, with the base release's files that
  mention the name. Findings add a warning to a pass. To fail the run on
  them, use `--fail-on-probes`, or the action's `fail-on-probes` input. To
  turn probes off, use `--no-probes`.
- `EVIDENCE.md` includes the schema probe results. On Plane, the probes
  flagged all 5 breaking migrations that replay missed, including 2 that the
  earlier results counted as safe.

## v0.3.0 - 2026-10-25

### Added

- `RECIPES.md` shows how to set up migcheck for TypeORM, tested with n8n.
- `EVIDENCE.md` includes the n8n test, which found a false alarm when a
  migration inserts rows that the old release inserts only if they're missing.
- `RECIPES.md` shows how to set up migcheck for Diesel, tested with Lemmy, and
  how to give replay the settings an app passes in its connection string.
- `EVIDENCE.md` includes the Lemmy test.
- `RECIPES.md` shows how to set up migcheck for Ecto, and `EVIDENCE.md`
  includes the Akkoma test.
- `RECIPES.md` shows how to set up migcheck for Laravel, and `EVIDENCE.md`
  includes the BookStack test on MySQL.
- `RECIPES.md` shows how to set up migcheck for Entity Framework, and
  `EVIDENCE.md` includes the Bitwarden test.
- `RECIPES.md` shows how to set up migcheck for Prisma, and `EVIDENCE.md`
  includes the Cal.com test.
- `RECIPES.md` shows how to set up migcheck for Sequelize, and `EVIDENCE.md`
  includes the Outline test.

### Changed

- The report groups breaking statements by the table, column, or function
  that each error names, such as `users.name`, and starts with a list of
  these causes and how many statements each one breaks. Before, one dropped
  column could appear as several groups, because a read's error names the
  query's alias for the table, and writes word the error differently. Errors
  that name nothing migcheck can resolve stay groups of their own.
- On Postgres, when an insert breaks because a row from the head release's
  migrations holds its key, the report says so. The insert isn't a break if
  the base release checks for the row before inserting it, which replay can't
  see. The result still fails.

### Fixed

- On Postgres, an apostrophe in an SQL comment, such as "won't", no longer
  stops migcheck from filling in the statement's later parameters. Neither
  does a backslash-escaped quote in an `E''` string. Those statements failed
  on both schemas and weren't checked.
- On Postgres, filling in parameters takes time in proportion to the
  statement's length. It took time in proportion to the square of the length,
  which slowed bulk inserts with thousands of parameters.
- On Postgres, replay runs `DISCARD ALL` as `SET SESSION AUTHORIZATION
  DEFAULT; RESET ALL`. Connection pools, such as Npgsql's, send `DISCARD ALL`
  when the app returns a connection, and it can't run in a transaction. It
  failed on both schemas and made up to 46% of the statements in a run, so
  the report warned that migcheck hadn't checked them.
- On Postgres, a session reset or a rolled-back first transaction no longer
  turns off the note on keys that a head migration inserted.

## v0.2.1 - 2026-10-23

### Added

- `--version` prints migcheck's version.

## v0.2.0 - 2026-10-22

### Added

- `--require-workload-success`, and the action's `require-workload-success`
  input, make a failing workload exit with code `2`.
- The report lists the most common errors among the statements it skipped.
- migcheck exits with code `2` if the migrate command creates no tables in the
  database at `DATABASE_URL`, because both schemas would then be empty and
  every query would pass.
- The report notes when the head release's migrations don't change the
  schema, and warns when 10% or more of the statements fail on the base
  schema too.
- `RECIPES.md` shows how to set up migcheck for Django, Rails, SQLAlchemy, Go,
  Knex, and apps that migrate on startup.

### Changed

- A pass from a failing workload is reported as a pass with a warning.
- On MySQL, the container allows 1,000 connections instead of 151, so test
  suites that restart the app between tests don't run out.

### Fixed

- On Postgres, replay time no longer grows with the square of the workload's
  size. Replay of Keycloak's 2.4 million test statements took over an hour,
  and takes about 9 minutes.
- On Postgres, a replayed insert no longer fails when a migration already
  inserted a row with the key the workload got. The insert gets a new key, as
  it would in production.

## v0.1.2 - 2026-10-21

### Added

- migcheck is on PyPI. To install it, run `pipx install migcheck`.

### Fixed

- On MySQL, replay no longer runs migcheck's own statement that turns off
  the query log as part of the workload.

## v0.1.1 - 2026-10-20

### Changed

- On pull requests, the action's comment refers to "this pull request"
  instead of `HEAD`.

### Added

- `--head-name` sets the name the result uses for `--head`.

## v0.1.0 - 2026-10-19

The first release.

### Added

- Check that a release's migrations don't break the release before it, by
  replaying the old release's recorded queries against both schemas.
- Postgres support, with `--db postgres`.
- MySQL 8.0 and later support, with `--db mysql`.
- Replay that follows the workload's transactions, so rolled-back test data
  doesn't leak into later statements.
- Root causes reported separately from the errors that follow from them.
- On Postgres, primary keys that the database generates are pinned to the
  values the workload got.
- `--markdown` writes the result as Markdown.
- A GitHub Action that runs the check on pull requests and posts the result
  as a comment.
