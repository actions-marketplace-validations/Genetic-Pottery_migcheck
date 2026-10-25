# Setup recipes

These recipes show how to run migcheck on common frameworks. Each one comes
from an app that migcheck was tested against, listed under the recipe. For
the results, see [EVIDENCE.md](EVIDENCE.md).

## How migcheck runs your commands

migcheck runs `--migrate` twice:

1. In a checkout of the base release, against an empty database.
2. In a checkout of the head release, against a copy of the database from
   step 1.

So the migrate command must work on an empty database and on a database that
already has the base release's schema.

migcheck runs `--workload` once, in the base release's checkout, against
another copy of the database from step 1. Test runners that create their own
database on the same server, such as Django's `test_<name>`, also work.

Both commands run with `DATABASE_URL` set. The commands run on your machine,
so the services your tests need, other than the database, must already be
running. For example, Mastodon's specs need Redis.

## Apps that don't read DATABASE_URL

Many apps read the host, port, and credentials from separate settings. To
run one of them, wrap each command in a script that splits `DATABASE_URL` and
sets the app's own variables:

```sh
#!/usr/bin/env bash
# Splits $DATABASE_URL into separate variables, then runs the command.
set -euo pipefail
eval "$(python3 - <<'PY'
import os, shlex, urllib.parse as u
p = u.urlparse(os.environ["DATABASE_URL"])
for k, v in {"DB_HOST": p.hostname, "DB_PORT": p.port, "DB_USER": p.username,
             "DB_PASSWORD": p.password, "DB_NAME": p.path[1:]}.items():
    print(f"export {k}={shlex.quote(str(v))}")
PY
)"
exec "$@"
```

Rename the variables to the ones your app reads. If the app reads a config
file instead, generate the file from these variables before running the
command. Gitea needed a config step like this.

## Django

```sh
migcheck --base v1.4.0 --head main \
  --migrate "python manage.py migrate" \
  --workload "pytest --create-db"
```

- Your settings must read `DATABASE_URL`, for example with
  `dj-database-url`.
- pytest-django creates its own `test_<name>` database and runs the
  migrations in it. `--create-db` stops it from reusing a database from an
  earlier run.

Tested with Plane.

## Rails

```sh
migcheck --base v5.1.0 --head main \
  --migrate "RAILS_ENV=test bin/rails db:migrate" \
  --workload "RAILS_ENV=test bin/rails test"
```

- Rails reads `DATABASE_URL`. For MySQL, change the scheme to `mysql2://` in
  each command:

  ```sh
  --migrate 'DATABASE_URL=mysql2${DATABASE_URL#mysql} RAILS_ENV=test bin/rails db:migrate'
  ```

- `db:migrate` runs every migration from the start of the app's history on
  the empty database. If old migrations no longer run, use `db:prepare`,
  which loads `db/schema.rb` into an empty database and migrates an existing
  one.
- If your app has post-deployment migrations, apply only the
  pre-deployment set. See [Apps that deploy in two steps](README.md#apps-that-deploy-in-two-steps).
  In Mastodon, set `SKIP_POST_DEPLOYMENT_MIGRATIONS=true`. In the
  development environment, also set `SKIP_TEST_DATABASE=true`. Otherwise
  `db:prepare` loads the test database's schema, and that turns the
  post-deployment migrations back on.
- If your views need compiled frontend assets, build them in the workload
  before the tests run. Without them, view and controller tests fail early
  and skip their queries.

Tested with Mastodon on Postgres and Redmine on MySQL.

## SQLAlchemy and Alembic

```sh
migcheck --base v2.0.0 --head main \
  --migrate "alembic upgrade head" \
  --workload "pytest"
```

If your app applies migrations from its own startup script, run that script
instead. Mealie's `mealie/db/init_db.py` runs Alembic and creates the default
data.

Tested with Mealie, which reads separate `POSTGRES_*` variables, so its
commands ran inside a wrapper like the one above.

## Go

Go apps usually have their own migrate command. For Gitea:

```sh
migcheck --base v1.24.7 --head v1.25.5 \
  --migrate "./migrate.sh" \
  --workload "./workload.sh"
```

- Build the binary inside each script, because migcheck runs the scripts in
  a fresh checkout of each release.
- Gitea's migrate script writes an `app.ini` from `DATABASE_URL` and runs
  `gitea migrate`. Its workload script generates the integration test config
  and runs the compiled test binary.

Tested with Gitea on Postgres and MySQL.

## Node with Knex

```sh
migcheck --db mysql --base v6.69.0 --head v6.70.0 \
  --migrate "npx knex migrate:latest" \
  --workload "npm test"
```

- Install dependencies inside the commands. migcheck checks out each release
  fresh, so `node_modules` isn't there.
- Ghost uses `knex-migrator migrate --init` instead of `knex migrate:latest`,
  and reads its connection from `database__connection__*` variables.

Tested with Ghost on MySQL, with one test worker and with four.

## Node with Sequelize

```sh
migcheck --base v1.0.0 --head main \
  --migrate "yarn install && yarn sequelize db:migrate" \
  --workload "yarn test"
```

- With `use_env_variable: "DATABASE_URL"` in the Sequelize config, the
  migrations and the app both read `DATABASE_URL`.
- If the app loads a `.env` file, check that it doesn't override the
  `DATABASE_URL` that migcheck sets. dotenv and dotenvx don't override
  variables that are already set, unless you ask them to.
- If app code creates tables at runtime, for example as working tables,
  replay drops those statements, so later statements on those tables fail
  on both schemas.

Tested with Outline, with eight test workers.

## Node with TypeORM

```sh
migcheck --base v2.0.0 --head main \
  --migrate "npx typeorm migration:run -d dist/data-source.js" \
  --workload "npm test"
```

- If your app runs its migrations from its own code instead of the TypeORM
  CLI, call that code from a short script. n8n has no migrate command, so its
  migrate script ran this file from `packages/cli`:

  ```js
  require('reflect-metadata');
  const { Container } = require('@n8n/di');
  const { DbConnection } = require('@n8n/db');

  (async () => {
    const db = Container.get(DbConnection);
    await db.init();
    await db.migrate();
    await db.close();
  })().catch((error) => {
    console.error(error);
    process.exit(1);
  });
  ```

- If each test file creates its own database and runs every migration in it,
  parallel test workers can exhaust Postgres's lock table. The workload then
  fails with `out of shared memory`, and the hint names
  `max_locks_per_transaction`. Migrate one template database before the tests
  start, and have each test file create its database with
  `CREATE DATABASE ... TEMPLATE`. n8n supports this when
  `N8N_TEST_TEMPLATE_DB` is set. A Vitest global setup created the template
  with `testDb.initTemplateDb()` and set the variable.
- Name the template so that the test suite's own cleanup doesn't drop it.
  Postgres doesn't drop a database marked as a template.

Tested with n8n, which reads separate `DB_POSTGRESDB_*` variables, so its
commands ran inside a wrapper like the one above.

## Node with Prisma

```sh
migcheck --base v1.0.0 --head main \
  --migrate "npm ci && npx prisma migrate deploy" \
  --workload "npx prisma db seed && npm test"
```

- Use `prisma migrate deploy`, which applies existing migrations. `prisma
  migrate dev` can reset the database and create migrations.
- Seed the database in the workload, not in the migrate command. migcheck
  runs the migrate command a second time on a database that holds the first
  run's rows, so a seed there runs twice.
- Prisma fills `@default(uuid())`, `@default(cuid())`, and `@updatedAt` in
  the client, not in the database. A column with one of these and no
  database default breaks the old release once it's NOT NULL, because the
  old client doesn't send it. migcheck catches this when the workload
  inserts rows.
- Prisma's Rust query engine and its driver adapters, such as
  `@prisma/adapter-pg`, both work with replay.

Tested with Cal.com on both query engines.

## Rust with Diesel

```sh
migcheck --base v1.0.0 --head main \
  --migrate "diesel migration run" \
  --workload "cargo test"
```

- Diesel's CLI reads `DATABASE_URL`. If your app embeds its migrations, run
  the binary that applies them instead. Lemmy's migrate script ran
  `cargo run --package lemmy_diesel_utils --features full`.
- Set `CARGO_TARGET_DIR` to a directory outside the checkout. migcheck checks
  out each release fresh, so without it, each run compiles everything twice.
- If your app has git submodules, run `git submodule update --init` in each
  command. A fresh checkout doesn't include them.
- If your tests share one database, they run against `DATABASE_URL`
  directly. Lemmy's tests run one at a time with `#[serial]`.

Tested with Lemmy, which reads `LEMMY_DATABASE_URL`, so its scripts set it from
`DATABASE_URL`.

## Elixir with Ecto

```sh
migcheck --base v3.0.0 --head main \
  --migrate "MIX_ENV=test mix ecto.migrate" \
  --workload "mix test"
```

- Ecto reads its connection from the repo's config, and a `url` setting
  overrides the separate settings, such as `hostname` and `database`. Check
  the repo's `init/2` callback too. Akkoma's sets `url` from the
  `DATABASE_URL` variable, so a command that runs in a container must pass
  that variable in. Without it, `url` is empty and the separate settings
  apply.
- Ecto's SQL sandbox works with replay. Each test's statements replay in one
  transaction that rolls back, as they do in the test. Replay ignores
  savepoints, so a test that rolls back to a savepoint keeps those rows in
  replay.
- If your native dependencies are hard to install, run the commands in your
  CI image with `docker run --network host`, so the container reaches the
  database at `DATABASE_URL`.

Tested with Akkoma, whose commands ran in its CI image.

## PHP with Laravel

```sh
migcheck --db mysql --base v1.0.0 --head main \
  --migrate "composer install && php artisan migrate --force" \
  --workload "php artisan db:seed --force && php vendor/bin/phpunit"
```

- Laravel reads a connection's `url` setting and uses it over the separate
  settings. Point that setting's environment variable at `DATABASE_URL`.
  BookStack's test connection reads `TEST_DATABASE_URL`.
- Seed the database in the workload, not in the migrate command. migcheck
  runs the migrate command a second time on a database that holds the first
  run's rows, so a seeder there runs twice.
- Laravel's `DatabaseTransactions` trait works with replay. Each test's
  statements replay in one transaction that rolls back, as they do in the
  test.
- If PHP's extensions are hard to install, run the commands in a PHP image
  with `docker run --network host`, so the container reaches the database at
  `DATABASE_URL`.

Tested with BookStack on MySQL, whose commands ran in a PHP 8.3 image.

## .NET with Entity Framework

```sh
migcheck --base v1.0.0 --head main \
  --migrate "./with-conn.sh dotnet tool restore && ./with-conn.sh dotnet ef database update" \
  --workload "./with-conn.sh dotnet test"
```

- EF Core providers take a connection string, not a URL. `with-conn.sh`
  stands for a script that converts `DATABASE_URL` and passes the result in
  the setting your app reads, as described in
  [Apps that don't read DATABASE_URL](#apps-that-dont-read-database_url).
  For Npgsql, the form is
  `Host=...;Port=...;Username=...;Password=...;Database=...`. To set it for
  `dotnet ef database update`, pass `--connection`.
- Run tests that use the real database. Tests that use SQLite in memory, or
  EF Core's in-memory provider, send no queries to the server. Bitwarden's
  `Infrastructure.IntegrationTest` project reads its databases from
  `BW_TEST_DATABASES__*` variables.
- If the SDK is hard to install, run the commands in the
  `mcr.microsoft.com/dotnet/sdk` image that matches `global.json`, with
  `docker run --network host`, so the container reaches the database at
  `DATABASE_URL`.

Tested with Bitwarden server on Postgres.

## Apps that migrate on startup

Some apps, such as Keycloak and many Spring Boot apps with Flyway or
Liquibase, apply migrations when they start. For these, the migrate command
starts the app, waits until it's ready, and stops it:

```sh
#!/usr/bin/env bash
# Starts the app, which migrates $DATABASE_URL on startup, then stops it.
set -euo pipefail
log=$(mktemp)
# setsid gives the app its own process group, so the trap stops its children too.
setsid ./bin/start-app > "$log" 2>&1 &
pid=$!
trap 'kill -- -$pid 2>/dev/null; wait $pid 2>/dev/null || true' EXIT
for _ in $(seq 300); do
  grep -q "started in" "$log" && exit 0
  kill -0 $pid 2>/dev/null || { tail -20 "$log" >&2; exit 1; }
  sleep 1
done
echo "The app didn't start in 5 minutes" >&2
exit 1
```

Replace `./bin/start-app` with the app's start command, and `started in`
with the line it logs when it's ready.

- On the first start, create any admin user or seed data the tests expect.
  Keycloak's tests log in as a bootstrap admin, so the migrate script passed
  the same credentials.
- `setsid` is part of util-linux. On macOS, install it or stop the process
  another way.

Tested with Keycloak on Postgres and MySQL.

## Check your setup

A wrong setup often still produces a result. migcheck catches some setup
problems itself:

- If the migrate command creates no tables in the database at
  `DATABASE_URL`, migcheck exits with code `2`.
- If the head release's migrations don't change the schema, the result
  includes a note. That's expected for a release without migrations.
- If 10% or more of the statements fail on the base schema too, the result
  includes a warning, because migcheck didn't check those statements.

Check the following yourself before trusting a result:

- **The statement count.** If migcheck replays far fewer statements than
  your test suite makes, the workload probably stopped early or ran against
  another database.
- **The workload's exit code.** A pass with a warning means the workload
  failed. Read its output and fix test failures that come from the
  environment, such as missing assets or services.
- **The skipped statements.** The report lists the most common errors among
  the statements that fail on both schemas. Errors from your test suite's own
  setup are normal. Errors that point to missing data mean replay lost rows
  that later queries needed.
- **Connection options.** The Postgres log doesn't record options set in the
  connection string, such as `options=-c name=value`. Replay runs without
  them, so statements that read them fail on both schemas with
  `unrecognized configuration parameter`. To give replay the values, set
  them on the role in the migrate command:
  `ALTER ROLE postgres SET name = 'value'`.
- **The migrations that ran.** If the result reports breaks from migrations
  you meant to skip, log the migrate command's output and check which
  migrations it applied.

## Other stacks

For a stack without a recipe, the general steps above apply. migcheck doesn't
depend on the framework, but expect setup problems like the ones these recipes
describe.
