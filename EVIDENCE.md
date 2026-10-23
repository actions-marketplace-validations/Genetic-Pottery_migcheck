# Test results

These results come from running migcheck against real upgrades of open-source
apps. Each run used the app's own test suite as the workload. All results
reflect migcheck 0.2.0. When a release changes how migcheck records or replays
queries, the affected results are rerun.

| App | ORM | Database | Upgrades | Result |
|---|---|---|---|---|
| Plane | Django | Postgres | 25 migrations | Caught 6 of 9 breaks |
| Gitea | xorm (Go) | Postgres, MySQL | 2 | Caught the 1 break |
| Ghost | Knex (Node) | MySQL | 2 | Caught all 3 breaks |
| Keycloak | Hibernate (Java) | Postgres, MySQL | 2 | Caught all 3 breaks |
| Mastodon | Active Record (Rails) | Postgres | 2 | Passed both, as designed |

No run raised a false alarm, after the fix described in the Mastodon section.

## Plane

migcheck checked 25 migrations from Plane's history. It caught 6 of the 9
breaking migrations. It missed the other 3 because Plane's tests never queried
the changed tables.

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
| v4.5.19 to v4.6.9 | Pre-deployment | 215,330 | Pass |
| v4.6.9 to v4.7.3 | Pre-deployment | 237,781 | Pass |
| v4.6.9 to v4.7.3 | All | 237,583 | Fail |

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
