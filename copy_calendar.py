#!/usr/bin/env python3
"""Copy/sync events with matching locations from one or more Nextcloud
calendars into another.

Every event in any of the configured source calendars whose LOCATION field
contains one of the configured location strings is mirrored into the
target calendar. Running the script again updates changed events and
removes copies whose source event was deleted or no longer matches - so
source and target stay in sync on every (e.g. daily) run. All settings
come from a YAML config file.
"""

import argparse
import base64
import hashlib
import http.client
import logging
import os
import ssl
import sys
from collections import namedtuple
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import caldav
import yaml
from icalendar import Calendar as ICalendar
from icalendar import Event as ICalEvent

DEFAULT_USER_AGENT = "pyCopyCalendar/1.0"

UID_PREFIX = "pycopycal-"
MANAGED_PROP = "X-PYCOPYCAL-MANAGED"
SOURCE_UID_PROP = "X-PYCOPYCAL-SOURCE-UID"
SOURCE_CALENDAR_PROP = "X-PYCOPYCAL-SOURCE-CALENDAR"
HASH_PROP = "X-PYCOPYCAL-HASH"

# Fields that must never be stripped, since removing them would break
# either the calendar format itself or the sync's ability to match events.
PROTECTED_FIELDS = {"UID", "RECURRENCE-ID", "DTSTART"}

log = logging.getLogger("pycopycalendar")


@dataclass
class Config:
    url: str
    username: str
    password: str
    verify_ssl: bool
    user_agent: str
    source_calendars: list
    target_calendar: str
    locations: list
    case_sensitive: bool
    past_days: Optional[int]
    future_days: Optional[int]
    strip_fields: set
    dry_run: bool = False


def resolve_password(nc: dict) -> str:
    password = nc.get("password")
    if not password and nc.get("password_env"):
        password = os.environ.get(nc["password_env"])
    if not password:
        raise ValueError(
            "No Nextcloud password configured (set 'nextcloud.password' or 'nextcloud.password_env')."
        )
    return password


# Minimal stand-in for requests.auth.HTTPBasicAuth / niquests.auth.HTTPBasicAuth,
# holding just what StdlibSession.request() reads off the "auth" object - this
# keeps us independent of whichever HTTP library caldav itself depends on.
BasicAuth = namedtuple("BasicAuth", ["username", "password"])


class _StdlibResponse:
    """Just enough of a requests.Response/niquests.Response for
    caldav.davclient.DAVResponse to read across supported caldav versions:
    status_code, reason, headers (case-insensitive, via http.client's own
    email.message.Message), content and text."""

    def __init__(self, status_code: int, reason: str, headers, content: bytes):
        self.status_code = status_code
        self.reason = reason
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class StdlibSession:
    """Drop-in replacement for the requests.Session/niquests.Session that
    caldav.DAVClient normally builds internally, performing requests using
    only Python's standard library (http.client + ssl).

    Some hosting providers put a firewall in front of Nextcloud that
    fingerprints and blocks the TLS/HTTP handshake produced by both
    `requests` and `niquests`, while a plain stdlib connection (and curl)
    gets through untouched - see the project README/history for how this
    was diagnosed."""

    def request(
        self,
        method: str,
        url: str,
        data=None,
        headers=None,
        proxies=None,
        auth=None,
        timeout=None,
        verify=True,
        cert=None,
    ) -> _StdlibResponse:
        parsed = urlsplit(url)
        if isinstance(verify, str):
            context = ssl.create_default_context(cafile=verify)
        else:
            context = ssl.create_default_context()
            if verify is False:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

        send_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        if auth is not None:
            username = getattr(auth, "username", None)
            password = getattr(auth, "password", None)
            if username is not None:
                token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
                send_headers["Authorization"] = f"Basic {token}"

        body = data
        if isinstance(body, str):
            body = body.encode("utf-8")
        if isinstance(body, bytes):
            # caldav's XML body builder emits a literal CRLF right after the
            # XML declaration. Some hosting firewalls flag any embedded \r\n
            # inside a request body as a CRLF-injection/request-smuggling
            # attempt and block the request outright - normalize it away
            # (irrelevant to XML parsing either way).
            body = body.replace(b"\r\n", b"\n")

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        conn = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 443, timeout=timeout, context=context
        )
        try:
            conn.request(method, path, body=body, headers=send_headers)
            resp = conn.getresponse()
            content = resp.read()
            return _StdlibResponse(resp.status, resp.reason, resp.headers, content)
        finally:
            conn.close()


def make_client(url: str, username: str, password: str, verify_ssl: bool, user_agent: str) -> caldav.DAVClient:
    client = caldav.DAVClient(
        url=url,
        username=username,
        password=password,
        ssl_verify_cert=verify_ssl,
        # Some hosting providers put a WAF in front of Nextcloud that blocks
        # WebDAV requests carrying the HTTP client library's default
        # User-Agent (while allowing e.g. curl) - so send a plain one.
        headers={"User-Agent": user_agent},
        # Set Basic auth upfront instead of letting caldav negotiate it
        # lazily after a first 401 - one less round-trip, and it lets our
        # StdlibSession read the credentials straight off this object.
        auth=BasicAuth(username, password),
    )
    client.session = StdlibSession()
    return client


def build_client(nc: dict) -> caldav.DAVClient:
    return make_client(
        url=nc["url"],
        username=nc["username"],
        password=resolve_password(nc),
        verify_ssl=nc.get("verify_ssl", True),
        user_agent=nc.get("user_agent", DEFAULT_USER_AGENT),
    )


def load_config(raw: dict) -> Config:
    nc = raw["nextcloud"]
    sync_cfg = raw["sync"]
    window = sync_cfg.get("time_window") or {}

    password = resolve_password(nc)

    locations = sync_cfg.get("locations") or []
    if not locations:
        raise ValueError("sync.locations must contain at least one location string.")

    source_calendars = sync_cfg.get("source_calendars")
    if source_calendars is None:
        legacy = sync_cfg.get("source_calendar")
        source_calendars = [legacy] if legacy else None
    if not source_calendars:
        raise ValueError("sync.source_calendars must contain at least one calendar name.")

    strip_fields = {str(f).upper() for f in (sync_cfg.get("strip_fields") or [])}
    ignored = strip_fields & PROTECTED_FIELDS
    if ignored:
        log.warning("Ignoring strip_fields entries that cannot be removed: %s", sorted(ignored))
    strip_fields -= PROTECTED_FIELDS

    return Config(
        url=nc["url"],
        username=nc["username"],
        password=password,
        verify_ssl=nc.get("verify_ssl", True),
        user_agent=nc.get("user_agent", DEFAULT_USER_AGENT),
        source_calendars=list(source_calendars),
        target_calendar=sync_cfg["target_calendar"],
        locations=locations,
        case_sensitive=sync_cfg.get("match_case_sensitive", False),
        past_days=window.get("past_days"),
        future_days=window.get("future_days"),
        strip_fields=strip_fields,
    )


def setup_logging(cfg: dict) -> None:
    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    handlers = [logging.StreamHandler()]
    log_file = cfg.get("file")
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def find_calendar(principal: caldav.Principal, name: str) -> caldav.Calendar:
    calendars = principal.calendars()
    for cal in calendars:
        try:
            if cal.get_display_name() == name:
                return cal
        except Exception:
            continue
    for cal in calendars:
        if name in str(cal.url):
            return cal
    available = []
    for cal in calendars:
        try:
            available.append(cal.get_display_name())
        except Exception:
            available.append(str(cal.url))
    raise LookupError(f"Calendar '{name}' not found. Available calendars: {available}")


def find_or_create_calendar(principal: caldav.Principal, name: str, dry_run: bool) -> Optional[caldav.Calendar]:
    try:
        return find_calendar(principal, name)
    except LookupError:
        if dry_run:
            log.info("[dry-run] would create target calendar '%s'", name)
            return None
        log.info("Target calendar '%s' does not exist yet, creating it", name)
        return principal.make_calendar(name=name, supported_calendar_component_set=["VEVENT"])


def location_matches(location: str, wanted: list, case_sensitive: bool) -> bool:
    if not location:
        return False
    haystack = location if case_sensitive else location.lower()
    for w in wanted:
        needle = w if case_sensitive else w.lower()
        if needle in haystack:
            return True
    return False


def fetch_source_events(calendar: caldav.Calendar, past_days, future_days):
    if past_days is None and future_days is None:
        return calendar.events()
    today = date.today()
    start = (
        datetime.combine(today - timedelta(days=past_days), datetime.min.time())
        if past_days is not None
        else None
    )
    end = (
        datetime.combine(today + timedelta(days=future_days), datetime.min.time())
        if future_days is not None
        else None
    )
    return calendar.search(event=True, start=start, end=end, expand=False)


def parse_vevents(ics_data: str):
    try:
        ical = ICalendar.from_ical(ics_data)
    except Exception as exc:
        log.warning("Skipping unparsable calendar object: %s", exc)
        return []
    return list(ical.walk("VEVENT"))


def get_master(vevents: list) -> ICalEvent:
    for v in vevents:
        if "RECURRENCE-ID" not in v:
            return v
    return vevents[0]


def make_target_uid(source_calendar_name: str, source_uid: str) -> str:
    # Namespaced by calendar name so the same UID from two different source
    # calendars never collides in the target calendar.
    key = f"{source_calendar_name}\x1f{source_uid}".encode("utf-8")
    return UID_PREFIX + hashlib.sha256(key).hexdigest()


def build_target_components(
    vevents: list,
    new_uid: str,
    source_uid: str,
    source_calendar_name: str,
    strip_fields: set,
):
    """Clone all VEVENT components sharing source_uid (master + recurrence
    overrides) under new_uid, and tag the master with sync metadata.

    Any property whose name is in strip_fields (e.g. "DESCRIPTION") is
    dropped from the copies, so no such details end up in the target
    calendar."""
    cloned = []
    master = None
    for v in vevents:
        nv = ICalEvent()
        for key, value in v.items():
            if key == "UID" or key in strip_fields:
                continue
            nv.add(key, value)
        nv["UID"] = new_uid
        cloned.append(nv)
        if master is None and "RECURRENCE-ID" not in nv:
            master = nv
    if master is None:
        master = cloned[0]

    combined_hash = hashlib.sha256(b"|".join(c.to_ical() for c in cloned)).hexdigest()
    master.add(SOURCE_UID_PROP, source_uid)
    master.add(SOURCE_CALENDAR_PROP, source_calendar_name)
    master.add(MANAGED_PROP, "1")
    master.add(HASH_PROP, combined_hash)
    return cloned, combined_hash


def to_ics_bytes(components: list) -> bytes:
    cal = ICalendar()
    cal.add("prodid", "-//pyCopyCalendar//DE")
    cal.add("version", "2.0")
    for c in components:
        cal.add_component(c)
    return cal.to_ical()


def get_managed_target_events(calendar: caldav.Calendar) -> dict:
    managed = {}
    for event in calendar.events():
        vevents = parse_vevents(event.data)
        if not vevents:
            continue
        master = get_master(vevents)
        if not master.get(MANAGED_PROP):
            continue
        uid = str(master.get("UID"))
        managed[uid] = (event, str(master.get(HASH_PROP, "")))
    return managed


def list_calendars(raw: dict) -> None:
    """Print every calendar visible to this Nextcloud account, with the
    exact name to put into config.yaml. Calendars shared with you often
    show a different, friendlier name in the Nextcloud web UI than their
    actual CalDAV display name - this prints the real one."""
    client = build_client(raw["nextcloud"])
    principal = client.principal()
    calendars = principal.calendars()

    print(f"{len(calendars)} calendar(s) found:\n")
    for cal in calendars:
        try:
            display_name = cal.get_display_name()
        except Exception:
            display_name = "(could not read display name)"
        print(f"- name: {display_name}")
        print(f"  URL:  {cal.url}")


def sync(config: Config) -> None:
    client = make_client(
        url=config.url,
        username=config.username,
        password=config.password,
        verify_ssl=config.verify_ssl,
        user_agent=config.user_agent,
    )
    principal = client.principal()

    target_cal = find_or_create_calendar(principal, config.target_calendar, config.dry_run)
    log.info("Source calendars: %s", ", ".join(config.source_calendars))
    log.info("Target calendar: %s", config.target_calendar)

    desired = {}  # new_uid -> (ics_bytes, hash, summary)
    for source_name in config.source_calendars:
        source_cal = find_calendar(principal, source_name)
        source_objects = fetch_source_events(source_cal, config.past_days, config.future_days)
        log.info("Fetched %d calendar object(s) from '%s'", len(source_objects), source_name)

        for obj in source_objects:
            vevents = parse_vevents(obj.data)
            if not vevents:
                continue
            master = get_master(vevents)
            location = str(master.get("LOCATION", ""))
            if not location_matches(location, config.locations, config.case_sensitive):
                continue
            source_uid = str(master.get("UID"))
            new_uid = make_target_uid(source_name, source_uid)
            cloned, combined_hash = build_target_components(
                vevents, new_uid, source_uid, source_name, config.strip_fields
            )
            desired[new_uid] = (
                to_ics_bytes(cloned),
                combined_hash,
                str(master.get("SUMMARY", "")),
            )

    log.info("%d source item(s) match the configured locations", len(desired))

    existing = get_managed_target_events(target_cal) if target_cal is not None else {}

    created = updated = deleted = unchanged = 0

    for new_uid, (ics_bytes, new_hash, summary) in desired.items():
        if new_uid not in existing:
            if config.dry_run:
                log.info("[dry-run] would create '%s' (%s)", summary, new_uid)
            else:
                target_cal.save_event(ics_bytes.decode("utf-8"))
                log.info("Created '%s' (%s)", summary, new_uid)
            created += 1
        else:
            target_event, old_hash = existing[new_uid]
            if old_hash != new_hash:
                if config.dry_run:
                    log.info("[dry-run] would update '%s' (%s)", summary, new_uid)
                else:
                    target_event.data = ics_bytes.decode("utf-8")
                    target_event.save()
                    log.info("Updated '%s' (%s)", summary, new_uid)
                updated += 1
            else:
                unchanged += 1

    for new_uid in set(existing) - set(desired):
        target_event, _ = existing[new_uid]
        if config.dry_run:
            log.info("[dry-run] would delete %s", new_uid)
        else:
            target_event.delete()
            log.info("Deleted %s", new_uid)
        deleted += 1

    log.info(
        "Sync complete: %d created, %d updated, %d deleted, %d unchanged",
        created,
        updated,
        deleted,
        unchanged,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).with_name("config.yaml"),
        help="Path to the YAML config file (default: config.yaml next to this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would change, without touching the target calendar",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all calendars visible to this Nextcloud account (with the exact "
        "names to use in config.yaml) and exit",
    )
    args = parser.parse_args()

    if not args.config.exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    setup_logging(raw.get("logging") or {})

    if args.list:
        try:
            list_calendars(raw)
        except Exception:
            log.exception("Could not list calendars")
            sys.exit(1)
        return

    try:
        config = load_config(raw)
    except Exception as exc:
        log.error("Invalid configuration: %s", exc)
        sys.exit(1)
    config.dry_run = args.dry_run

    try:
        sync(config)
    except Exception:
        log.exception("Sync failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
