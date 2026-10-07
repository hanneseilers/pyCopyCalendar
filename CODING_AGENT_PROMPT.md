# Coding Agent Prompt

Copy the prompt below into a coding agent that has access to the project directory containing `TECHNICAL_SPECIFICATION.md`.

---

You are a senior Python engineer implementing a safety-critical, one-way Nextcloud calendar synchronization tool for a Linux shared-webspace environment.

## Objective

Implement the complete project described in `TECHNICAL_SPECIFICATION.md`.

The application is a finite Python batch process. It reads multiple source calendars from one Nextcloud instance, selects event occurrences by configured `LOCATION` aliases, and maintains one target calendar in the same Nextcloud instance.

It must:

- add newly eligible event occurrences;
- update changed eligible occurrences;
- delete managed target occurrences whose source was deleted or cancelled;
- delete managed target occurrences when their location becomes ineligible or empty;
- create occurrences whose location becomes eligible;
- correctly handle those transitions for recurrence exceptions;
- never modify any source calendar;
- leave unmanaged events in the target calendar untouched;
- use an existing Nextcloud username and app password;
- store every application-controlled file below the project root, including SQLite, journals, backups, credentials, logs, lock, dependencies, and temporary files;
- run safely when invoked periodically by cron, although the cron configuration and exact cron command are out of scope.

## Authoritative specification

Read `TECHNICAL_SPECIFICATION.md` completely before changing files. Treat it as authoritative. If this prompt and the specification appear to differ, follow the specification unless doing so would create a clear security or data-loss problem. In that case, stop and explain the conflict.

Do not invent actual calendar URLs, usernames, app passwords, or production data. Use placeholders in examples. Never commit a real secret.

## Required working method

1. Inspect the current directory, existing files, and version-control status.
2. Preserve unrelated user changes. Do not reset, discard, or overwrite them.
3. Produce a short implementation plan tied to the specification's requirements.
4. Implement in small, testable layers.
5. Use fake HTTP transports and fixtures for automated tests. Do not contact a real Nextcloud instance unless the operator explicitly authorizes a live integration test and supplies a dedicated test target.
6. Run formatting, static checks, and the full automated test suite.
7. Review the completed code against every acceptance criterion in the specification.
8. Finish with a concise report listing implemented functionality, tests run and their results, known limitations, and the exact files the operator must configure.

If no starter code exists, create the full project structure. If partial code exists, first determine whether it already implements any required behavior and extend it without unnecessary rewrites.

## Non-negotiable safety rules

### Source calendars are read-only

The existing Nextcloud user may have write permission on source calendars. Permission configuration is therefore not an adequate safeguard.

Enforce source read-only behavior in code:

- define a source gateway that exposes only discovery/read/report/get operations;
- define a separate target gateway as the only component with mutation methods;
- never call `save`, `put`, `create`, `update`, or `delete` on a source calendar or source resource object;
- route every DAV mutation through one central target-containment guard;
- validate canonical source and target URLs during startup;
- reject equal or overlapping source and target collections;
- reject a write URL that is not a direct child of the exact target collection;
- reject traversal, encoded traversal, unexpected queries, foreign origins, and redirects outside the target collection;
- test a complete create/update/delete run with a recording transport and assert zero `PUT`, `POST`, `PATCH`, and `DELETE` requests against all source URL prefixes.

Do not weaken these rules for convenience or because a high-level CalDAV library exposes a generic save method.

### Fail closed

- If any enabled source cannot be read authoritatively, perform no target mutations.
- Do not interpret an authentication error, timeout, truncated response, or material parse failure as an empty calendar.
- Apply absolute and ratio-based deletion limits before the first write.
- Apply creates and updates before deletes.
- Require valid ownership metadata before deleting a target event.
- Use ETag preconditions for updates and deletes, and a no-overwrite precondition for creates where supported.
- Default to dry-run. Mutations require explicit apply mode when configured that way.
- Use a non-blocking exclusive file lock to prevent overlapping runs.

## Technical baseline

- Target Python 3.10 or later.
- Use a `src/` package layout.
- Use JSON configuration so Python 3.10 does not require `tomllib`.
- Use `pathlib` for paths.
- Derive the project root from code location, never from the process working directory.
- Reject normalized configured paths outside the project root.
- Use `sqlite3` with foreign keys and explicit transactions.
- Prefer SQLite's default rollback journal for this single-process shared-hosting use case unless a tested reason requires another mode.
- Store the database at `data/sync.sqlite3` by default.
- Store logs at `logs/sync.log` and the lock at `run/calendar-sync.lock` by default.
- Store local credentials in `secrets/nextcloud.env` and parse the file as data, not shell code.
- Use the existing `NEXTCLOUD_USERNAME` and `NEXTCLOUD_APP_PASSWORD` values from that file.
- Ensure sensitive values are redacted centrally.
- Use HTTPS with certificate verification enabled by default.
- Use explicit connect/read timeouts and bounded retry with exponential backoff and jitter.
- Pin tested dependency versions in `requirements.lock`.

Suitable libraries include `caldav` for CalDAV access and `icalendar` for iCalendar processing. Use a small controlled HTTP layer when conditional requests or redirect containment cannot be guaranteed through the high-level library. Do not implement RRULE expansion yourself.

## Required project contents

Create or complete at least:

```text
README.md
TECHNICAL_SPECIFICATION.md
CODING_AGENT_PROMPT.md
pyproject.toml
requirements.lock
.gitignore
.htaccess
config/config.example.json
config/.htaccess
secrets/.htaccess
data/.htaccess
logs/.htaccess
run/.htaccess
src/calendar_sync/__init__.py
src/calendar_sync/__main__.py
src/calendar_sync/cli.py
src/calendar_sync/paths.py
src/calendar_sync/config.py
src/calendar_sync/credentials.py
src/calendar_sync/models.py
src/calendar_sync/source_gateway.py
src/calendar_sync/target_gateway.py
src/calendar_sync/transport.py
src/calendar_sync/ical.py
src/calendar_sync/recurrence.py
src/calendar_sync/location.py
src/calendar_sync/transform.py
src/calendar_sync/reconcile.py
src/calendar_sync/state.py
src/calendar_sync/safety.py
src/calendar_sync/logging_setup.py
tests/fixtures/
tests/unit/
tests/integration/
```

Do not create a real `secrets/nextcloud.env` containing credentials. Supply a safely named template such as `secrets/nextcloud.env.example`, ensure the real file is ignored, and document how the operator creates it locally.

The `.htaccess` protection must deny direct HTTP access to configuration, credentials, database files and journals, logs, locks, and hidden files when the project is deployed below a document root. Also document that deployment outside the public document root is preferred and that the deny behavior must be verified on the actual host.

## Configuration behavior

Implement the example schema from the specification, including:

- Nextcloud base URL and timeouts;
- stable list of source IDs and exact calendar collection URLs;
- exact target collection URL;
- canonical locations and aliases;
- case-insensitive normalized-exact matching by default;
- timezone, look-back, look-ahead, and outside-window policy;
- copied-field switches;
- project-relative database, log, lock, and backup paths;
- dry-run default, deletion limits, runtime budget, and all-sources requirement.

Validate all configuration before opening a write path. Provide clear, secret-free validation messages.

## Synchronization model

Use occurrences as synchronization units.

Build the stable instance key from:

```text
source_id | source_uid | recurrence_key
```

Use `SINGLE` for non-recurring events and a type-preserving normalized original `RECURRENCE-ID` for recurring instances. Generate a deterministic UUIDv5 target UID in the application namespace.

Materialize every eligible recurring occurrence as a standalone target `VEVENT`; do not copy the source recurrence rule into the target. This is required so one recurrence exception can enter or leave the allowed location set independently.

Add and validate the provenance properties defined in the specification. An SQLite row alone must not authorize deletion.

Build a semantic SHA-256 fingerprint from a canonical representation of mirrored fields. Exclude ETags, generated timestamps, line folding, and property order. An unchanged second run must issue no write.

Default location normalization must use parsed iCalendar text, Unicode NFKC, trimmed and collapsed whitespace, and Unicode case-folding. Match the complete normalized value against normalized aliases.

Create target components from an explicit allowlist. Exclude organizer, attendees, scheduling method, alarms, and attachments by default so the mirror cannot generate invitations or duplicate reminders.

## Reconciliation order

Implement explicit phases:

1. resolve project paths and acquire lock;
2. load and validate configuration and credentials;
3. initialize redacted logging and SQLite;
4. preflight authentication, collections, and URL separation;
5. establish one immutable time window;
6. read all enabled sources authoritatively;
7. expand/materialize occurrences within bounds;
8. apply location matching and transformation;
9. read managed target state;
10. create a deterministic plan;
11. check deletion and runtime safety limits;
12. report only in dry-run, or apply creates, updates, then deletes;
13. persist confirmed remote outcomes immediately;
14. store the final run summary and release resources.

Remote CalDAV and SQLite do not provide one distributed transaction. Design for convergence after interruption rather than pretending they do. Never mark a remote action successful before confirmation.

## Time and recurrence requirements

- Calculate one half-open interval `[window_start, window_end)` at run start.
- Use the configured timezone for semantics and UTC for query boundaries.
- Preserve DATE versus DATE-TIME, timezone identifiers, floating times, and exclusive all-day end dates.
- Prefer verified server recurrence expansion; otherwise use a mature bounded expansion library.
- Preserve original recurrence identity when an exception moves its start.
- Handle `EXDATE`, cancelled exceptions, moved exceptions, and exception-specific locations.
- Enforce a maximum expansion count.
- Do not delete old managed events merely because the rolling look-back boundary advanced when `outside_window_policy` is `retain`.

## SQLite and migrations

Implement versioned schema migrations for:

- `schema_version`;
- `source_calendar`;
- `mirror_mapping`;
- `sync_run`.

Use the fields and semantics from the specification. Before migration, write a timestamped backup below `data/backups/`, retain a bounded number, and preserve recoverability after failure.

Database state must support:

- recreation of missing target events;
- source-qualified UID collisions;
- ETag-based conditional operations;
- semantic change detection;
- deletion decisions limited to an authoritative scope;
- run diagnostics without storing the app password or unnecessary personal event content.

## CLI and observability

Implement:

```text
--config
--dry-run
--apply
--validate-config
--preflight
--verbose
--allow-large-delete
```

Implement the documented exit-code classes. Human-readable output should be concise and cron-friendly. Local rotating logs should be structured and privacy-conscious. Include a run ID and summary counts.

Do not implement or document a concrete STRATO cron invocation; document only that the module is designed for periodic external invocation.

## Test requirements

Write meaningful tests, not placeholder assertions.

At minimum cover:

- project-root and path escape protection;
- credential parsing and log redaction;
- source/target URL overlap and mutation containment;
- no source mutations in create/update/delete scenarios;
- Unicode location matching and eligibility transitions;
- deterministic instance keys, target UIDs, and fingerprints;
- multiple sources with equal source UIDs;
- recurrence rules, exceptions, cancellation, `EXDATE`, all-day values, floating values, and DST in `Europe/Berlin`;
- create, update, delete, unchanged, recreate, and collision/quarantine decisions;
- idempotent second run;
- partial source failure causing zero target writes;
- dry-run causing zero target writes;
- deletion-limit abort;
- ETag conflicts and safe retry/quarantine;
- SQLite migrations, backup, and transaction behavior;
- lock contention and runtime-budget behavior.

For fake DAV responses, include realistic XML, ETags, and ICS fixtures. Tests must not depend on network access.

## Quality expectations

- Use explicit types and small cohesive functions.
- Keep protocol I/O separate from domain logic.
- Inject clocks, transports, and repositories where needed for deterministic tests.
- Avoid global mutable state.
- Preserve original exceptions as causes while emitting sanitized user-facing messages.
- Document server-dependent behavior and make unsupported capability failures explicit.
- Do not add unnecessary frameworks or a web UI.
- Do not silently broaden the synchronization scope.
- Do not optimize with sync tokens until full-window reconciliation is correct and tested.

## Definition of done

Do not call the work complete until:

1. the project can be installed entirely below its own directory;
2. configuration validation and preflight can run without mutations;
3. dry-run produces a deterministic plan and no mutating HTTP methods;
4. all unit and fake-transport integration tests pass;
5. source write-protection tests pass;
6. a second unchanged synchronization performs no write;
7. every acceptance criterion in `TECHNICAL_SPECIFICATION.md` is either demonstrated by a test or explicitly documented as requiring a live Nextcloud test;
8. the README gives complete setup and recovery instructions without exposing a real credential;
9. no runtime path escapes the project root;
10. no cron configuration has been added.

When finished, report:

- the implemented architecture;
- the files added or changed;
- commands used for verification;
- test counts and results;
- any remaining live-server verification steps;
- any assumptions that the operator must confirm on IONOS or STRATO.

---
