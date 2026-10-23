# Changelog

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
