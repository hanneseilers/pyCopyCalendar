# Nextcloud Calendar Location Mirror

A periodically-executed (cron) Python batch process that mirrors event
occurrences from one or more Nextcloud calendars into a single target
calendar — but only occurrences whose **location** matches one of several
configured places. It is designed to run unprivileged on shared webspace
(the original deployment target was Nextcloud hosted at IONOS, invoked
from cron on STRATO shared hosting), with every file it touches — code,
config, credentials, database, logs, lock — living below its own project
directory.

See `TECHNICAL_SPECIFICATION.md` for the full design and
`CODING_AGENT_PROMPT.md` for the prompt this implementation was built
from. This README covers day-to-day setup and operation.

## What it does

- Reads every enabled source calendar in the configured time window.
- Mirrors an occurrence into the target calendar only if its effective
  `LOCATION` exactly matches (case-insensitive by default, Unicode
  normalized) one of the configured location aliases.
- On every run, reconciles the target so it exactly matches "what should
  be there": new eligible occurrences are created, changed ones updated,
  and previously-mirrored occurrences deleted once their source is gone,
  cancelled, or its location no longer matches — including individual
  recurrence exceptions moving in or out of scope.
- Never modifies a source calendar. Never touches an event in the target
  calendar it didn't create itself.
- Defaults to **dry-run**: nothing is written until you pass `--apply`
  (or set `safety.dry_run_default` to `false`).

## Installation

```bash
cd calendar-sync
python3 -m venv .venv          # optional but recommended; not required by the app itself
.venv/bin/pip install -r requirements.lock
cp config/config.example.yaml config/config.yaml
cp secrets/nextcloud.env.example secrets/nextcloud.env
```

Everything the application writes — the SQLite database, its backups,
logs, and the run lock — is created automatically below `data/`, `logs/`
and `run/` on first run; nothing needs to be created by hand beyond the
two files copied above.

### Credentials

Edit `secrets/nextcloud.env`:

```dotenv
NEXTCLOUD_USERNAME=your-nextcloud-username
NEXTCLOUD_APP_PASSWORD=your-app-password
```

Use an existing Nextcloud user and an **app password** (Nextcloud web UI
→ Settings → Security → "Create new app password") — not your real login
password. This file is parsed as plain `KEY=VALUE` data, never sourced or
executed, and is gitignored; never commit it.

### Alternative: web setup wizard (`setup.php`)

If PHP is available on your webspace, `setup.php` in the project root is
a one-time browser-based alternative to editing `config/config.yaml` and
`secrets/nextcloud.env` by hand: it shows a form for the full
configuration, including the Nextcloud username and app password, and
only writes both files after actually testing the connection to
Nextcloud with what you entered (via this application's own
`--validate-config` and `--preflight`) — nothing is kept if that test
fails, both files are removed again and the form is re-shown. On
success it deletes itself; if it's ever loaded while either
`config/config.yaml` or `secrets/nextcloud.env` already exists, it does
nothing and deletes itself again instead of showing the form. It can
therefore only ever complete once, for a fresh install.

It has **no authentication or access protection of its own** — no
login, no token, no HTTPS requirement; reaching the URL is enough. That
is intentional: upload it, run it once, it deletes itself, rather than
adding a gate to get through. Treat the link as private for the short
time it exists — delete `setup.php` by hand if you're not going to use
it. See the file's own header comment for the full model, and this
README's Troubleshooting section for `connection_test.sh`, the
SSH-based equivalent.

### Configuration

Edit `config/config.yaml` (see the comments and defaults in
`config/config.example.yaml`):

- `nextcloud.base_url` — your Nextcloud's CalDAV root, normally
  `https://your-domain/remote.php/dav/`.
- `sources[]` — one entry per source calendar: a stable `id` (used to
  namespace UIDs so the same event UID from two different calendars
  never collides in the target) and its exact CalDAV `calendar_url`.
- `target.calendar_url` — the calendar events are mirrored into. It must
  share the same Nextcloud origin as the sources and must not equal or
  be nested inside any source collection; this is enforced before any
  network request is made. It is **not** created automatically — create
  it once in the Nextcloud web UI first.
- `location_filter.locations[]` — one entry per place you care about, as
  a `canonical` name plus a list of `aliases` (the different ways that
  place might actually be written in someone's `LOCATION` field). An
  event is mirrored only if its location exactly equals one alias, after
  Unicode normalization and (by default) case-folding — not a substring
  match, to avoid accidentally matching the wrong place.
- `window.lookback_days` / `window.lookahead_days` — the rolling sync
  window. Events entirely outside this window are neither created nor
  touched.
- `mirroring.*` — per-field toggles for what gets copied
  (`copy_description`, `copy_url`, `copy_categories`, `copy_alarms`,
  `copy_attendees`, `copy_organizer`; all default to a private-by-default
  posture — `ORGANIZER`/`ATTENDEE`/alarms are off by default so the
  mirror can never generate an invitation or a duplicate reminder). Add
  further field names to `mirroring.strip_fields` to drop them from the
  copies entirely (e.g. `["DESCRIPTION"]`) — this extends the toggles
  above with arbitrary iCalendar property names; `UID`, `DTSTAMP`,
  `DTSTART` and `RECURRENCE-ID` can never be stripped, since removing
  them would break either the calendar format or the sync's ability to
  match events.
- `safety.*` — `dry_run_default`, the absolute/ratio deletion limits,
  the runtime budget, and whether all sources must read successfully
  before any write happens.

#### Finding calendar URLs

Nextcloud's web UI often shows a calendar (especially one shared with
you) under a friendlier name than its real CalDAV collection URL. The
reliable way to find the exact URL for `sources[].calendar_url` /
`target.calendar_url` is a `PROPFIND` against your principal's calendar
home, e.g.:

```bash
curl -u your-username --request PROPFIND \
  --header "Depth: 1" \
  "https://your-domain/remote.php/dav/calendars/your-username/"
```

(enter your app password when prompted). Each `<d:href>` in the response
is one calendar's CalDAV path; append it to your Nextcloud origin to get
the full `calendar_url`.

### Networking note (WAF / firewall workarounds)

Some hosting providers put a firewall in front of Nextcloud that blocks
the TLS/HTTP handshake produced by Python's common HTTP libraries
(`requests`, `niquests`) while still allowing e.g. `curl` through
unaffected — this was diagnosed against IONOS-hosted Nextcloud behind
STRATO shared webspace in the predecessor of this project. To work
regardless, this application talks to Nextcloud through a small
stdlib-only (`http.client` + `ssl`) transport instead (see
`StdlibTransport` in `src/calendar_sync/transport.py`), which behaves
like `curl` on the wire, and sends a plain `User-Agent`
(`nextcloud.user_agent` in the config, in case a firewall also filters on
that header specifically). If you get a bare `405 Method Not Allowed`
from nginx (with none of Nextcloud's usual response headers), try
changing `nextcloud.user_agent` to something else, e.g. a real browser's
user agent string.

## Validating before touching anything

```bash
.venv/bin/python -m calendar_sync --validate-config
```

Loads and validates `config/config.yaml` (source/target separation,
path containment, alias collisions, …) without touching credentials or
the network at all.

```bash
.venv/bin/python -m calendar_sync --preflight
```

Additionally authenticates and verifies every enabled source and the
target collection are reachable — still without any mutation.

## Dry run

```bash
.venv/bin/python -m calendar_sync --dry-run
```

Builds and logs the exact same plan a real run would (create/update/
delete counts, and each planned change), but sends no `PUT`/`POST`/
`PATCH`/`DELETE` request. Safe to run as often as you like.

## First real run

```bash
.venv/bin/python -m calendar_sync --apply
```

If `safety.dry_run_default` is `true` (the default), `--apply` is
required for any invocation to actually write; otherwise every
invocation writes unless you pass `--dry-run`.

Re-run the same command (e.g. daily via cron) to keep the target
calendar in sync — an unchanged run performs zero writes.

## Deletion safety

Every run checks the planned deletes against `safety.max_deletes_absolute`
and `safety.max_delete_ratio` (relative to previously-mirrored events)
*before* writing anything. If either limit is exceeded, the run aborts
with no changes at all. If you're certain a large deletion is correct
(e.g. you intentionally removed a source calendar), re-run once with
`--allow-large-delete` — this is logged prominently and only applies to
that single invocation.

## Invocation via cron

`config/config.yaml` is looked up relative to the project root by
default, so a cron entry just needs the full paths:

```
/path/to/calendar-sync/.venv/bin/python -m calendar_sync --config config/config.yaml --apply
```

(run from any working directory — every path the application uses is
resolved from its own installed location, never from cron's cwd). The
exact schedule/cron syntax is intentionally not prescribed here; once a
day is a reasonable starting point.

A local, non-blocking lock (`run/calendar-sync.lock`) prevents two
invocations from running concurrently — a second run started while one
is still in progress exits immediately (exit code `7`) rather than
racing it.

## Backup and recovery

- Every SQLite schema migration backs up the existing database into
  `data/backups/` first (bounded by `storage.backup_retention`, default
  10) before making any change; a failed migration leaves the
  pre-migration file untouched.
- If the database is lost entirely, the next run recreates it from
  scratch and treats every managed target event it can still verify
  ownership of (its `X-CALMIRROR-*` properties are self-consistent) as
  already-mirrored, rather than mirroring duplicates.
- If a managed target event is deleted manually, the next run recreates
  it (as long as its source occurrence is still eligible). If it's
  edited manually, the next run restores the mirrored content — the
  target calendar is a derived view, not a place for manual edits meant
  to persist. Events you add to the target calendar yourself (not
  carrying the application's provenance markers) are never touched.

## Troubleshooting

If the connection to Nextcloud is rejected when run from your own
machine, that alone doesn't tell you *why* — it could be the WAF/
User-Agent issue above, or something specific to your network/IP
(residential IPs are treated differently by some WAFs/CDNs than hosting
IPs, and Nextcloud's own brute-force protection can temporarily throttle
an IP after repeated failed attempts during testing). `connection_test.sh`
in the project root runs the same checks (config validation, a raw
`curl` PROPFIND, and this application's own `--preflight`) directly on
the target webspace over SSH, so you can compare the result from there
against your own machine and tell the two apart:

```bash
scp -r . youruser@your-strato-host:/path/to/calendar-sync
ssh youruser@your-strato-host
cd /path/to/calendar-sync
./connection_test.sh
```

It never prints the app password and makes no changes to Nextcloud.

- **Exit code `2`**: invalid configuration or project layout — see
  stderr for the specific validation error; nothing was touched.
- **Exit code `3`**: authentication/authorization failure, or the target
  calendar wasn't reachable — check `secrets/nextcloud.env` and that the
  target calendar exists.
- **Exit code `4`**: one or more required sources couldn't be read
  authoritatively — the run made zero target mutations, by design.
- **Exit code `5`**: a target read/write failed after preflight passed.
- **Exit code `6`**: a safety guard (deletion limit) stopped the run —
  see the logged message; use `--allow-large-delete` only if the large
  delete is actually intended.
- **Exit code `7`**: another invocation is still running.
- **Exit code `8`**: runtime budget exceeded, or an unexpected internal
  error — check `logs/sync.log`.
- Logs (`logs/sync.log`, rotated, plus stdout) never include the app
  password or full raw calendar payloads; each line carries the run ID
  from the summary printed at the end of the invocation.

## Deployment / web exposure

The project's own file layout is protected against accidental web
exposure if it ends up inside a document root: the root `.htaccess`
denies all HTTP access, and `config/`, `secrets/`, `data/`, `logs/` and
`run/` each carry their own `.htaccess` deny rule as a second layer.
Deploying the project **outside** the public document root is still
strongly preferred; verify the deny behavior against representative
files on your actual host before relying on it in production.

`setup.php` (see above) is the one deliberate exception, carved out by
name in the root `.htaccess`. If you don't plan to use it, or once
you're done with it, just delete `setup.php` — the exception then grants
access to nothing.

## Running the test suite

```bash
.venv/bin/pip install -r requirements.lock -r requirements-dev.lock
.venv/bin/pip install -e .
.venv/bin/python -m pytest
```

All tests run against an in-memory fake CalDAV transport
(`tests/fixtures/fake_dav.py`) — none of them contact a real Nextcloud
instance or the network.

## Out of scope

- The exact cron invocation/schedule.
- Bidirectional sync, or mirroring anything other than events (tasks,
  contacts, files).
- Automatically creating the target calendar, or provisioning Nextcloud
  users/app passwords.
- Sending invitations or notifications (by design — see `mirroring.*`
  above).
