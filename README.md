# pyCopyCalendar

Copies/syncs events from one Nextcloud calendar into a second calendar in
the same Nextcloud instance - but only events whose **location** contains
one of several configured places.

Every run fully reconciles the target calendar: new matching events are
created, changed ones updated, and events that no longer match or were
deleted from the source are removed from the target again. Events added
manually to the target calendar are left untouched. All settings come from
`config.yaml`.

## Installation

```bash
cd pyCopyCalendar
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` (see the comments inside): Nextcloud URL, username, an
app password (Nextcloud -> Settings -> Security -> "Create new app
password"), the display names of the source/target calendars, and the list
of locations to filter on.

## Testing

```bash
.venv/bin/python copy_calendar.py --dry-run
```

Shows what would happen without changing the target calendar. Run again
without `--dry-run` for the real run.

## Invocation

`config.yaml` is looked up next to the script by default, so running it
automatically just needs the full path to the venv Python and the script:

```
/path/to/pyCopyCalendar/.venv/bin/python /path/to/pyCopyCalendar/copy_calendar.py
```

Schedule this command to run once a day (e.g. via cron) to keep both
calendars in sync.

If the project folder lives inside a web-accessible document root (common
on shared hosting), the included `.htaccess` blocks all HTTP access to it
(Apache only) - only cron ever needs to reach these files.

## Configuration options (overview)

See `config.example.yaml` for details, including:

- `nextcloud.verify_ssl` / `password_env` (read the password from an
  environment variable instead)
- `sync.match_case_sensitive`, `sync.time_window`
- `sync.strip_fields` (drop fields like `DESCRIPTION` from the copies)
- `logging.level` / `logging.file`
