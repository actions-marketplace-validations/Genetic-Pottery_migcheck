# Test results

These results come from running migcheck against real upgrades of open-source
apps. Each run used the app's own test suite as the workload.

migcheck 0.5.0 reran every Postgres upgrade on this page: Plane's 25
migrations and 23 upgrades of the other apps. Each run gave the same verdict
and the same causes as before. Statement counts differed by up to 2% from the
earlier runs, because test suites don't run exactly the same queries each
time. The statement counts in the tables come from the earlier runs.

The MySQL results for Ghost, Gitea, and Keycloak come from migcheck 0.2.0, and
the BookStack results come from 0.3.0. migcheck's MySQL replay hasn't changed
since then. When a release changes how migcheck records or replays queries,
the affected results are rerun.

| App | ORM | Database | Upgrades | Result |
|---|---|---|---|---|
| Plane | Django | Postgres | 25 migrations | Caught 6 of 11 breaks; schema probes flagged the other 5 |
| Gitea | xorm (Go) | Postgres, MySQL | 2 | Caught the 1 break |
| Ghost | Knex (Node) | MySQL | 2 | Caught all 3 breaks |
| Keycloak | Hibernate (Java) | Postgres, MySQL | 2 | Caught all 3 breaks |
| Mastodon | Active Record (Rails) | Postgres | 2 | Passed both, as designed |
| n8n | TypeORM (Node) | Postgres | 2 | Caught the 1 break, with 1 false alarm |
| Lemmy | Diesel (Rust) | Postgres | 2 | Caught the 1 break |
| Akkoma | Ecto (Elixir) | Postgres | 2 | Caught all 4 breaks |
| BookStack | Laravel (PHP) | MySQL | 4 | Caught all 4 breaks |
| Bitwarden | Entity Framework (.NET) | Postgres | 4 | Caught all 5 breaks |
| Cal.com | Prisma (Node) | Postgres | 3 | Caught 1 of 2 breaks; schema probes flagged the other |
| Outline | Sequelize (Node) | Postgres | 3 | Caught the 1 break |

migcheck also ran on Supabase's todo-list example, which isn't in this table.
The example isn't a production app, and its breaking releases were written
for the test. For details, see [Supabase example](#supabase-example).

The n8n run raised the only false alarm left after the fix described in the
Mastodon section. For details, see the n8n section.

The schema probes reflect migcheck 0.4.0. Their results come from the Plane
runs and from the upgrades listed in [Schema probes](#schema-probes).

## Plane

migcheck checked 25 migrations from Plane's history, `0098` through `0122`.
Replay caught 6 of the 11 breaking migrations. It missed the other 5 because
Plane's tests never queried the changed tables. The schema probes flagged all
5:

| Migration | Change the probes found |
|---|---|
| `0098` | `profiles.is_app_rail_docked` requires a value |
| `0100` | `profiles.has_marketing_email_consent` requires a value |
| `0102` | `pages.sort_order` requires a value |
| `0110` | `workspace_user_properties.navigation_control_preference` requires a value |
| `0118` | `workspace_user_properties.product_tour` is dropped, and `profiles.product_tour` requires a value |

Each of these migrations adds a field with a Python default. Django fills the
existing rows and then drops the default from the database, so the old
release's inserts, which don't set the new column, fail. An earlier version of
these results counted only 9 breaking migrations, because it counted `0102`
and `0110` as safe. The probes showed that they aren't.

The probes also found more breaks in three migrations that replay had caught.
`0104` makes `rich_filters` required in four more tables. `0113` makes
`webhooks.version`, `workspace_user_properties.product_tour`, and two
`profiles` columns required: `is_navigation_tour_completed` and
`is_subscribed_to_changelog`. `0117` drops `pages.description` and
`draft_issues.description`. The probes
flagged nothing in the other 14 migrations. `0099` couldn't run, because
Plane has two `0098` migrations at that commit.

## Gitea

migcheck replayed Gitea's API tests for two upgrades, v1.23.8 to v1.24.7 and
v1.24.7 to v1.25.5, on both Postgres and MySQL. The runs replayed about a
million statements in total.

migcheck caught the one breaking migration on both databases. That migration
drops a column that v1.23.8 queries.

## Ghost

migcheck replayed Ghost's API tests on MySQL for two upgrades, v6.69.0 to
v6.70.0 and v6.71.0 to v6.72.0. Each upgrade ran once with one test worker and
once with four. Each run replayed 180,000 to 190,000 statements.

migcheck caught all three breaks in v6.70.0: two dropped tables and a dropped
column. It passed v6.72.0. The four-worker runs gave the same results as the
one-worker runs.

## Keycloak

migcheck replayed Keycloak's test suite for two upgrades, 26.6.0 to 26.7.0 and
26.7.0 to 26.8.0, on both Postgres and MySQL. The four runs replayed about 14
million statements in total. The largest run replayed 4.8 million.

The workload ran the tests in Keycloak's `tests/base` module, except for two
kinds:

- `WelcomePageTest` deletes the admin account that the test framework signs
  in with. Keycloak's default test database starts empty each time the test
  server starts, but migcheck's database keeps its data. With this test
  included, every later test failed to sign in.
- The classes in `org.keycloak.tests.suites` rerun tests from other classes.

The Maven test pattern was
`org.keycloak.tests.**,!org.keycloak.tests.suites.**,!**/WelcomePageTest`.

| Upgrade | Postgres | MySQL |
|---|---|---|
| 26.6.0 to 26.7.0 | Pass | Fail |
| 26.7.0 to 26.8.0 | Fail | Fail |

On both databases, migcheck caught the two breaks in 26.8.0:

- 26.8.0 drops `IDENTITY_PROVIDER.ORGANIZATION_ID`, which 26.7.0 reads and
  writes.
- 26.8.0 makes `ORG_DOMAIN.REALM_ID` `NOT NULL`, and 26.7.0's inserts don't set
  it.

The database changed the verdict on 26.7.0. That release adds two columns to
the `JGROUPS_PING` table, and 26.6.0 inserts into it with a `VALUES` list and no
column names. Postgres fills the missing columns with `NULL`, so migcheck passed
26.7.0 on Postgres. MySQL rejects the insert, so migcheck failed 26.7.0 on
MySQL. On MySQL, a 26.6.0 node can't register with the cluster once the
database has the 26.7.0 schema.

26.7.0 also moves each realm's display name from the `REALM_ATTRIBUTE` table to
a new column. 26.6.0's queries still run, but they read and write the old
location. migcheck checks that queries succeed, not what they return, so it
doesn't catch data-only changes like this one.

Keycloak supports rolling upgrades only between patch releases. Between minor
releases, such as these, it requires stopping every node before the upgrade.
So these breaks show what migcheck detects, not bugs in Keycloak. The two patch
releases in these lines that change the schema add a SQL Server–only index and
backfill data, which migcheck doesn't check.

## Mastodon

Mastodon splits its migrations in two. It runs the pre-deployment
migrations, rolls out the new release, and runs the post-deployment
migrations after the old release stops. The old release must work with the
first set but not the second.

migcheck replayed Mastodon's specs for two upgrades, v4.5.19 to v4.6.9 and
v4.6.9 to v4.7.3, with only the pre-deployment migrations applied. It also
replayed v4.6.9 to v4.7.3 with all migrations applied.

| Upgrade | Migrations | Statements | Result |
|---|---|---|---|
| v4.5.19 to v4.6.9 | Pre-deployment | 213,180 | Pass |
| v4.6.9 to v4.7.3 | Pre-deployment | 237,257 | Pass |
| v4.6.9 to v4.7.3 | All | 234,771 | Fail |

Both pre-deployment runs passed, as Mastodon's design intends. The run with
all migrations failed on two post-deployment changes:

- v4.7.3 adds a unique index on `accounts.uri`. v4.6.9 inserts each local
  account with an empty `uri`, so the second insert fails.
- v4.7.3 turns `account_summaries` and `global_follow_recommendations` from
  materialized views into tables. v4.6.9 refreshes them as views.

These breaks are real, but Mastodon's deploy process avoids them, because
v4.6.9 has stopped by the time those migrations run.

The run with all migrations also exposed a false alarm in migcheck. A
post-deployment migration inserts rows into `keypairs`, and those rows took
keys that v4.6.9's replayed inserts also used. migcheck now gives a replayed
insert a new key when a migration already took its key.

The workload skipped Mastodon's browser specs and search specs. 14 of the
remaining 7,121 specs failed, because the Redis configuration specs expect a
default Redis URL. Those specs don't query the database.

## n8n

migcheck replayed n8n's CLI integration tests on Postgres for two upgrades,
2.40.6 to 2.41.7 and 2.44.6 to 2.45.3.

| Upgrade | Statements | Result |
|---|---|---|
| 2.40.6 to 2.41.7 | 486,810 | Fail |
| 2.44.6 to 2.45.3 | 625,107 | Pass |

migcheck caught the one break in 2.41.7. That release adds
`ai_preference.source` as `NOT NULL` with no default, on purpose, so that every
write must name its source. 2.40.6's 37 inserts into `ai_preference` don't set
it. The 2.45.3 migrations add columns with defaults, drop a `NOT NULL`, and add
a check that 2.44.6's rows meet, so migcheck passed it.

The 2.41.7 run also raised a false alarm. A 2.41.7 migration inserts three rows
into the `scope` table. At startup, 2.40.6 reads the `scope` table and inserts
the scopes that are missing. Replay runs the recorded insert, which includes
the three scopes the migration added, so it fails on the primary key. A real
2.40.6 server reads the table first and skips those scopes. The insert also
caused 227 later failures in the same transaction, which the report lists
separately.

migcheck still fails this insert, because an old release that inserts without
checking first breaks in production. The report labels it: it says that a row
from the head migrations holds the key, and that the insert isn't a break if
the old release checks for the row first.

Each run passed about 7,000 to 8,600 tests. 4 tests failed in each run, at
least 2 of them because `openssl` wasn't installed.

## Lemmy

migcheck replayed Lemmy's Rust tests on Postgres for two upgrades,
1.0.0-beta.0 to 1.0.0-beta.1 and 1.0.0-beta.1 to 1.0.0-beta.2.

| Upgrade | Statements | Result |
|---|---|---|
| 1.0.0-beta.0 to 1.0.0-beta.1 | 20,663 | Fail |
| 1.0.0-beta.1 to 1.0.0-beta.2 | 21,561 | Pass |

migcheck caught the one break in 1.0.0-beta.1. That release renames
`local_user.hide_media` to `hide_posts_with_media`. Diesel names every column
in its queries, so 445 of 1.0.0-beta.0's statements break: inserts and
updates of `local_user`, and the reads that join it, such as the post and
comment views.

1.0.0-beta.2 renames a column too, but the column is new in that release, so
1.0.0-beta.1 never uses it. migcheck passed it.

Lemmy sets `lemmy.protocol_and_hostname` as an option in its connection
string, and its triggers read the setting. The Postgres log doesn't record
connection options, so in the first runs, replay ran without the setting. 1,685
statements failed on both schemas and weren't checked. The runs in the table
set the option on the `postgres` role, and 60 statements failed on both
schemas. The verdicts didn't change.

Each run passed 270 to 280 tests. 2 tests failed in each run, because they
fetch pages from the internet. In one run of 1.0.0-beta.0's tests, 32 tests
failed on a duplicate person name, and the run recorded fewer statements. The
table shows the next run, which had the usual 2 failures.

## Akkoma

migcheck replayed Akkoma's tests on Postgres for two upgrades, v3.18.1 to
v3.19.0 and v3.19.0 to v3.20.1. Akkoma is a fork of Pleroma.

| Upgrade | Statements | Result |
|---|---|---|
| v3.18.1 to v3.19.0 | 76,358 | Fail |
| v3.19.0 to v3.20.1 | 74,285 | Pass |

migcheck caught all four breaks in v3.19.0, in 21,781 statements:

- v3.19.0 drops `users.skip_thread_containment`. Ecto names every field in
  its queries, so most of v3.18.1's reads and writes of `users` break.
- v3.19.0 drops `lists.ap_id`, which v3.18.1 reads and writes.
- v3.19.0 drops the `activity_visibility` and `thread_visibility` functions,
  which v3.18.1's timeline queries call.

v3.20.1's migrations add a column, replace indexes, and backfill data, so
migcheck passed it. v3.20.1 also replaces the unique index on
`users.nickname` with one under another name. v3.19.0 maps the old index's
name to a "nickname taken" error, so a duplicate nickname raises an exception
instead. The insert fails on both schemas, so migcheck doesn't catch this.
The tests didn't insert a duplicate nickname, so the run didn't show it.

Ecto's test sandbox runs each test in a transaction that it rolls back, and
turns the test's own transactions into savepoints. Replay rolls back each
test's transaction too, but it ignores savepoints, so it doesn't undo a
rollback to a savepoint. Only 18 statements failed on both schemas, so this
didn't cost the run any data that later queries needed.

This test found a bug in migcheck. One of Akkoma's queries has a comment with
"won't" in it, and migcheck read the apostrophe as the start of a string. It
left the query's later parameters unfilled, so the query failed on both
schemas. migcheck now skips comments.

Each run passed about 4,150 tests, and 1 to 3 tests failed in each. The first run of
v3.18.1's tests under migcheck had 96 failures, which didn't happen again
with the same random seed.

## BookStack

migcheck replayed BookStack's PHPUnit tests on MySQL for four upgrades. Each
workload ran BookStack's seeder, then its tests.

| Upgrade | Statements | Result |
|---|---|---|
| v24.02.3 to v24.05.4 | 56,572 | Fail |
| v25.07.3 to v25.11.6 | 60,536 | Fail |
| v25.11.6 to v25.12.9 | 65,319 | Pass |
| v26.05.5 to v26.09.2 | 75,951 | Pass |

migcheck caught the one break in v24.05.4, in 890 statements. v24.05.4
renames `activities.entity_id` and `activities.entity_type`, which v24.02.3
writes and filters on.

migcheck caught all three breaks in v25.11.6, in 16,298 statements:

- v25.11.6 merges the `books`, `chapters`, `pages`, and `bookshelves` tables
  into one `entities` table and drops the old tables.
- v25.11.6 renames `comments.entity_id` and `comments.entity_type`.
- v25.11.6 drops `comments.text`, which v25.07.3 writes.

v25.11.6 also drops two `role_permissions` columns that v25.07.3 doesn't
use, so they aren't breaks.

v25.12.9's migrations add two tables and an index, so migcheck passed it.
v26.09.2 has no migrations of its own: BookStack backported its only one to
v26.05.3. migcheck passed it and noted that the schema didn't change.

No statement failed on both schemas in any run. The tests ran in a PHP 8.3
image. In two runs, one or two image thumbnail tests failed because of the
image's GD build. The other tests passed.

## Bitwarden

migcheck replayed the Postgres tests in Bitwarden server's
`Infrastructure.IntegrationTest` project for four upgrades. Those tests run
Bitwarden's Entity Framework repositories against a real database. Bitwarden's
other tests use SQLite in memory, so they can't serve as a workload.

| Upgrade | Statements | Result |
|---|---|---|
| v2026.4.2 to v2026.5.0 | 8,653 | Pass |
| v2026.5.0 to v2026.6.2 | 5,996 | Pass |
| v2026.6.2 to v2026.7.2 | 6,750 | Fail |
| v2026.9.2 to v2026.10.0 | 10,626 | Fail |

migcheck caught both breaks in v2026.7.2, in 23 statements. v2026.7.2 drops
`OrganizationInviteLink.EncryptedOrgKey` and renames
`OrganizationInviteLink.EncryptedInviteKey`.

migcheck caught all three breaks in v2026.10.0, in 278 statements.
v2026.10.0 renames `AccessRequest.Status`, `AccessRequest.ResolvedDate`, and
`AccessLease.Status`. The report lists each one as a separate cause. Postgres
reports only the first missing column in a statement, so a statement that
uses both of `AccessRequest`'s renamed columns counts under the one Postgres
reports, which is usually `ResolvedDate`.

v2026.5.0 and v2026.6.2 add tables, indexes, and columns with defaults, so
migcheck passed them. v2026.6.2 also makes `Send.Key` and `Send.Data` NOT
NULL. v2026.5.0's tests never write a Send without them, so this isn't a
break for the tests either.

Bitwarden releases backport many migrations to patch releases. For example,
v2026.2.1 already includes v2026.3.0's migration that drops
`Send.EmailHashes`. These runs compare the latest patch of each release.

This test found a gap in migcheck. Npgsql sends `DISCARD ALL` each time the
app returns a connection to the pool. Those statements failed on both
schemas and made up 34% to 46% of each run, so the report warned that
migcheck hadn't checked them. migcheck now runs the parts of `DISCARD ALL`
that work in a transaction, which reset the session. The results didn't
change.

In v2026.9.2's runs, one test failed with a serialization error. It fails
about half the time without migcheck too. The tests ran in the .NET SDK image
that each release's `global.json` names.

## Cal.com

migcheck replayed Cal.com's integration tests on Postgres for three upgrades.
Each workload ran Cal.com's seed script, then the tests. v5.6.20 and v5.7.12
run Prisma's Rust query engine. v5.8.14 runs Prisma's TypeScript client with
the `@prisma/adapter-pg` driver adapter, which sends queries through node-pg.

| Upgrade | Statements | Result |
|---|---|---|
| v5.6.20 to v5.7.12 | 9,381 | Pass |
| v5.7.12 to v5.8.14 | 9,630 | Fail |
| v5.8.14 to v5.9.15 | 10,246 | Pass |

migcheck caught the break in v5.8.14, in 247 statements. v5.8.14 adds
`users.uuid`, fills it, and makes it NOT NULL, all in one release. The
database has no default for the column. Prisma's `@default(uuid())` generates
the value in v5.8.14's client, and v5.7.12's client doesn't know the column.
Every user that v5.7.12 creates breaks.

Replay missed a break in v5.7.12. v5.7.12 drops `Watchlist.severity` and
other `Watchlist` columns, and v5.6.20's watchlist checks filter on
`severity`. v5.6.20's integration tests and seed script never reach those
checks, so the run had no queries to replay. The schema probes flagged
`severity`, `createdAt`, `createdById`, `updatedAt`, and `updatedById`. The
source search finds 3 to 6 files in v5.6.20 that mention each column and
`Watchlist`, such as `packages/features/watchlist/watchlist.repository.ts`.

v5.9.15's migrations add tables, change column types in the tables they
add, and drop NOT NULL constraints, so migcheck passed it.

All integration tests passed in each run. The commands ran in a Node 20
image.

## Outline

migcheck replayed Outline's server tests on Postgres for three upgrades, with
eight test workers.

| Upgrade | Statements | Result |
|---|---|---|
| v1.7.1 to v1.8.0 | 40,773 | Fail |
| v1.8.1 to v1.9.0 | 43,763 | Pass |
| v1.11.0 to v1.12.0 | 61,748 | Pass |

migcheck caught the one break in v1.8.0, in 24 statements. v1.8.0 replaces
the unique index on `document_insights ("documentId", date)` with one that
adds `period`. v1.7.1's insight rollup upserts with `ON CONFLICT
("documentId", date)`, which needs the old index. The break changes no column
or table.

v1.9.0 drops `teams.collaborativeEditing`, which v1.8.1's model no longer
declares, so migcheck passed it. v1.12.0 replaces the migrations up to v1.3.0
with one baseline migration that does nothing on an existing database.
migcheck passed it too.

Outline's popularity task creates a working table, then fills and reads it.
Replay drops DDL, so the task's other statements failed on both schemas. They
were 36 to 54 statements per run, most of the statements that migcheck
skipped.

All server tests passed in each run.

## Supabase example

Supabase apps send most queries through PostgREST, which runs each request as
the signed-in user's database role, so row-level security (RLS) policies
apply. migcheck ran on Supabase's
[`nextjs-todo-list`](https://github.com/supabase/supabase/tree/master/examples/todo-list/nextjs-todo-list)
example to test the setup in [RECIPES.md](RECIPES.md#supabase). The example
isn't a production app, and its four candidate releases were written for this
test. These results show that the setup works, not how often migcheck catches
breaks in Supabase apps.

The runs used the versions that Supabase CLI 2.120.0 runs: the
`supabase/postgres:17.11.0.004` image, PostgREST v16.4, and Supabase Auth
v2.197.0. The workload was a test script that signs up two users with
`supabase-js` and runs the example's queries as each of them: it lists, adds,
toggles, and deletes todos. Each run replayed 155 statements.

| Release | Migration | Result |
|---|---|---|
| `v2-expand` | Adds `todos.priority` | Pass |
| `v2-rename` | Renames `todos.is_complete` to `done` | Fail |
| `v2-rls` | Adds lists, and a policy that allows a new todo only in one of the user's lists | Fail |
| `v2-untested` | Drops `todos.inserted_at`, which only an untested function reads | Pass with a warning |

- `v2-rename` broke 1 statement, the update that toggles a todo. The reads
  use `select('*')`, so they still work.
- `v2-rls` broke the 3 inserts, with an RLS error. Replay runs each request as
  the role PostgREST switched to. A workload that queries the database
  directly as `postgres` doesn't catch this break, because that role bypasses
  RLS. `v2-rls` gave the same result on the Postgres 15 image,
  `supabase/postgres:15.19.0.004`.
- `v2-untested` passed replay, because the tests never read `inserted_at`. A
  schema probe flagged the dropped column, and the report named the one file
  that mentions it, `lib/todos.mjs`.

To reproduce these runs, build the example with
[`examples/make-supabase-demo.sh`](examples/make-supabase-demo.sh) and run the
command in [RECIPES.md](RECIPES.md#supabase).

Before 0.5.0, migcheck couldn't run on Supabase's image, because the image
doesn't make `postgres` a superuser. Replay also skipped every write that
came through PostgREST.

## Schema probes

The schema probes ran on Plane's 25 migrations, on Cal.com v5.6.20 to v5.7.12,
and on the upgrades below. On the upgrades that replay failed, the table lists
only the findings that replay didn't already show.

| Upgrade | Database | Replay | Probe findings |
|---|---|---|---|
| Cal.com v5.8.14 to v5.9.15 | Postgres | Pass | None |
| Bitwarden v2026.4.2 to v2026.5.0 | Postgres | Pass | None |
| Bitwarden v2026.5.0 to v2026.6.2 | Postgres | Pass | `Send.Data` no longer accepts NULL |
| Outline v1.8.1 to v1.9.0 | Postgres | Pass | `teams.collaborativeEditing` is dropped |
| Outline v1.11.0 to v1.12.0 | Postgres | Pass | None |
| Akkoma v3.19.0 to v3.20.1 | Postgres | Pass | None |
| Lemmy 1.0.0-beta.1 to 1.0.0-beta.2 | Postgres | Pass | None |
| n8n 2.44.6 to 2.45.3 | Postgres | Pass | None |
| BookStack v24.02.3 to v24.05.4 | MySQL | Fail | None |
| BookStack v25.07.3 to v25.11.6 | MySQL | Fail | `role_permissions.display_name` and `role_permissions.description` are dropped |
| BookStack v25.11.6 to v25.12.9 | MySQL | Pass | None |
| BookStack v26.05.5 to v26.09.2 | MySQL | Pass | None |
| Gitea v1.23.8 to v1.24.7 | MySQL | Fail | None |
| Gitea v1.24.7 to v1.25.5 | MySQL | Pass | `notice.description` no longer accepts NULL |
| Ghost v6.69.0 to v6.70.0 | MySQL | Fail | None |
| Ghost v6.71.0 to v6.72.0 | MySQL | Pass | None |

None of these findings is a confirmed break. They show the limits of a query
written from the schema:

- `teams.collaborativeEditing` is a contract step. v1.8.1's model no longer
  declares the column, so dropping it is safe. The report said that no file
  in v1.8.1 mentions the column outside its migrations.
- BookStack's migration is named `remove_unused_columns`. In v25.07.3, the
  only mention of the two columns on the `RolePermission` model is a doc
  comment, and Eloquent reads them with `SELECT *`, so dropping them is
  probably safe. The source search can't tell a doc comment from a use.
- `Send.Data` and `notice.description` existed in the base release and
  accepted NULL there. The probe inserts a row without them, but the base
  release might always set them. v1.24.7 writes `notice.description` from a
  Go string, which is never NULL. These results don't show whether
  v2026.5.0 ever writes a `Send` without `Data`. The report labels this kind
  of change "column no longer accepts NULL", apart from new required columns,
  which every insert from the base release leaves out.
