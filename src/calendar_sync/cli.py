"""Command-line entry point and exit-code contract.

The exact cron invocation/schedule is intentionally out of scope (spec
section 19) - this module only needs to behave correctly when invoked
periodically by *something*, which is why every exit code is distinct
and every failure mode logs before returning rather than raising past
main().
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from . import reconcile
from .config import ConfigError, load_config
from .credentials import CredentialsError, Redactor, load_credentials
from .logging_setup import set_current_run_id, setup_logging
from .paths import PROJECT_ROOT
from .safety import DeletionLimitExceededError, LockHeldError, ProcessLock, RuntimeBudgetExceededError
from .source_gateway import SourceReadError
from .state import MigrationError, StateRepository, open_database
from .target_gateway import TargetWriteError

log = logging.getLogger("calendar_sync.cli")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_AUTH_ERROR = 3
EXIT_SOURCE_NOT_AUTHORITATIVE = 4
EXIT_TARGET_WRITE_ERROR = 5
EXIT_SAFETY_GUARD = 6
EXIT_LOCK_HELD = 7
EXIT_INTERNAL_ERROR = 8


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calendar-sync", description="One-way Nextcloud calendar location mirror."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/config.json"),
        help="Path to the JSON config file (relative to the project root by default).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Force dry-run for this invocation.")
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform real mutations (required when safety.dry_run_default is true).",
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate configuration and exit without contacting Nextcloud.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Validate configuration and verify source/target reachability, without any mutation.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG-level logging.")
    parser.add_argument(
        "--allow-large-delete",
        action="store_true",
        help="Override the deletion-limit guard for this one run only. Logged prominently.",
    )
    return parser


def _resolve_config_path(raw: Path) -> Path:
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = _resolve_config_path(args.config)
    try:
        config = load_config(config_path, project_root=PROJECT_ROOT)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.validate_config:
        print("Configuration is valid.")
        return EXIT_OK

    try:
        credentials = load_credentials(config.resolved_path(config.nextcloud.credentials_file))
    except CredentialsError as exc:
        print(f"Invalid credentials: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    redactor = Redactor.from_credentials(credentials)
    setup_logging(config.logging, config.resolved_path(config.storage.log_file), redactor, verbose=args.verbose)

    if args.preflight:
        try:
            results = reconcile.preflight(config, credentials)
        except Exception as exc:
            log.error("Preflight failed: %s", str(exc))
            return EXIT_AUTH_ERROR
        for name, reachable in results.items():
            log.info("Preflight %s: %s", name, "OK" if reachable else "FAILED")
        return EXIT_OK if all(results.values()) else EXIT_AUTH_ERROR

    dry_run = config.safety.dry_run_default
    if args.apply:
        dry_run = False
    if args.dry_run:
        dry_run = True
    if args.allow_large_delete:
        log.warning("--allow-large-delete is set: the deletion-limit guard is overridden for this run only.")

    run_id = str(uuid.uuid4())
    set_current_run_id(run_id)

    try:
        lock = ProcessLock(config.resolved_path(config.storage.lock_file))
        with lock:
            conn = open_database(
                config.resolved_path(config.storage.database),
                backup_dir=config.resolved_path(config.storage.backup_directory),
                backup_retention=config.storage.backup_retention,
            )
            try:
                repo = StateRepository(conn)
                summary = reconcile.run(
                    config,
                    credentials,
                    repo,
                    dry_run=dry_run,
                    allow_large_delete=args.allow_large_delete,
                    run_id=run_id,
                )
            finally:
                conn.close()
    except LockHeldError as exc:
        log.error(str(exc))
        return EXIT_LOCK_HELD
    except MigrationError as exc:
        log.error(str(exc))
        return EXIT_INTERNAL_ERROR
    except reconcile.PreflightError as exc:
        log.error(str(exc))
        return EXIT_AUTH_ERROR
    except reconcile.SourcesNotAuthoritativeError as exc:
        log.error("Aborting with zero target mutations: %s", exc)
        return EXIT_SOURCE_NOT_AUTHORITATIVE
    except DeletionLimitExceededError as exc:
        log.error(str(exc))
        return EXIT_SAFETY_GUARD
    except RuntimeBudgetExceededError as exc:
        log.error(str(exc))
        return EXIT_INTERNAL_ERROR
    except (SourceReadError, TargetWriteError) as exc:
        log.error(str(exc))
        return EXIT_TARGET_WRITE_ERROR
    except Exception:
        log.exception("Unexpected internal error")
        return EXIT_INTERNAL_ERROR

    log.info(
        "status=%s dry_run=%s created=%d updated=%d deleted=%d unchanged=%d quarantined=%d skipped=%d failed=%d",
        summary.status, summary.dry_run, summary.created, summary.updated,
        summary.deleted, summary.unchanged, summary.quarantined, summary.skipped, summary.failed,
    )
    print(
        f"{summary.status}: {summary.created} created, {summary.updated} updated, "
        f"{summary.deleted} deleted, {summary.unchanged} unchanged, {summary.quarantined} quarantined "
        f"(dry_run={summary.dry_run}, run_id={summary.run_id})"
    )
    return EXIT_OK if summary.status == "success" else EXIT_TARGET_WRITE_ERROR
