#!/usr/bin/env bash
# Diagnostic script for running calendar-sync's connectivity checks
# directly on the target webspace (e.g. STRATO) via SSH, to tell apart
# "this host's network/IP can't reach Nextcloud" from "the app's own
# HTTP client is rejected" from "the config/credentials are wrong" -
# see the README's "Networking note" and "Troubleshooting" sections.
#
# Usage (after uploading the whole project directory, e.g. via scp/sftp):
#   ssh youruser@your-strato-host
#   cd /path/to/calendar-sync
#   ./strato_connection_test.sh [path/to/config.yaml]
#
# Never prints the app password. Safe to run repeatedly; makes no
# changes to Nextcloud (every check here is read-only).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_PATH="${1:-config/config.yaml}"

TOTAL=0
FAILED=0

check_start() { TOTAL=$((TOTAL + 1)); printf '  %-58s' "$1"; }
check_ok()    { echo "OK"; }
check_fail()  { FAILED=$((FAILED + 1)); echo "FAIL${1:+ - $1}"; }
check_skip()  { echo "SKIP${1:+ - $1}"; }
section()     { echo; echo "=== $1 ==="; }

section "1. Environment"
echo "  Host:              $(hostname 2>/dev/null || echo '?')"
echo "  Date (UTC):        $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "  Project directory: $SCRIPT_DIR"
echo "  Shell user:        $(whoami 2>/dev/null || echo '?')"
if command -v curl >/dev/null 2>&1; then
  PUBLIC_IP="$(curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || true)"
  echo "  Outbound public IP (best-effort, via https://ifconfig.me): ${PUBLIC_IP:-could not determine}"
else
  echo "  curl not found - cannot determine outbound public IP or run checks below"
fi

section "2. Python interpreter"
PYBIN=""
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PYBIN="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYBIN="$(command -v python3)"
fi
check_start "python3 interpreter found"
if [ -n "$PYBIN" ]; then
  check_ok
  echo "    using: $PYBIN ($("$PYBIN" --version 2>&1))"
else
  check_fail "no python3 on PATH and no .venv/bin/python"
  echo
  echo "Cannot continue without Python. Ask your hosting provider which"
  echo "python3 binary/version is available and adjust PATH, then re-run."
  exit 1
fi

check_start "Python version >= 3.10"
if "$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  check_ok
else
  check_fail "found $("$PYBIN" --version 2>&1), need 3.10+"
fi

section "3. Virtual environment / dependencies"
if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  echo "  No .venv found - creating one and installing requirements.lock..."
  if "$PYBIN" -m venv "$SCRIPT_DIR/.venv" 2>/tmp/venv_err.$$ \
      && "$SCRIPT_DIR/.venv/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.lock" 2>>/tmp/venv_err.$$; then
    PYBIN="$SCRIPT_DIR/.venv/bin/python"
    echo "  venv created and dependencies installed: $PYBIN"
  else
    echo "  Could not set up a venv automatically. Details:"
    sed 's/^/    /' /tmp/venv_err.$$ 2>/dev/null
    echo "  Continuing with $PYBIN - dependency checks below will show what's missing."
    echo "  (A common shared-hosting failure here is 'lxml' needing a prebuilt wheel"
    echo "  for this exact Python version/architecture - check with your provider"
    echo "  which python3 they recommend for pip installs if this happens.)"
  fi
  rm -f /tmp/venv_err.$$
fi
export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
check_start "runtime dependencies importable"
if "$PYBIN" -c 'import caldav, icalendar, yaml, recurring_ical_events' 2>/tmp/import_err.$$; then
  check_ok
else
  check_fail "see below"
  sed 's/^/    /' /tmp/import_err.$$
  echo "    -> run: $SCRIPT_DIR/.venv/bin/pip install -r requirements.lock"
fi
rm -f /tmp/import_err.$$

section "4. Configuration and credentials"
check_start "$CONFIG_PATH exists"
if [ -f "$CONFIG_PATH" ]; then
  check_ok
else
  check_fail "copy config/config.example.yaml to $CONFIG_PATH first"
fi

check_start "secrets/nextcloud.env exists"
if [ -f "secrets/nextcloud.env" ]; then
  check_ok
else
  check_fail "copy secrets/nextcloud.env.example to secrets/nextcloud.env first"
fi

if [ -f "$CONFIG_PATH" ]; then
  check_start "calendar-sync --validate-config"
  if OUTPUT=$("$PYBIN" -m calendar_sync --config "$CONFIG_PATH" --validate-config 2>&1); then
    check_ok
  else
    check_fail
    echo "$OUTPUT" | sed 's/^/    /'
  fi
fi

section "5. Raw curl PROPFIND (bypasses this application's own HTTP client entirely)"
if [ -f "secrets/nextcloud.env" ] && [ -f "$CONFIG_PATH" ] && command -v curl >/dev/null 2>&1; then
  # Credentials/base_url are extracted via this application's own parser
  # (never via shell string munging) and only ever placed in a 0600
  # temp file for curl's --netrc-file - never in argv or printed.
  CREDS_TSV=$("$PYBIN" -c "
from pathlib import Path
from calendar_sync.credentials import load_credentials
c = load_credentials(Path('secrets/nextcloud.env'))
print(c.username + '\t' + c.app_password)
" 2>/tmp/creds_err.$$)
  BASE_URL=$("$PYBIN" -c "
import yaml
print(yaml.safe_load(open('$CONFIG_PATH'))['nextcloud']['base_url'])
" 2>>/tmp/creds_err.$$)

  if [ -n "$CREDS_TSV" ] && [ -n "$BASE_URL" ]; then
    NC_USER="${CREDS_TSV%%$'\t'*}"
    NC_PASS="${CREDS_TSV#*$'\t'}"
    HOST_ONLY="$(echo "$BASE_URL" | sed -E 's#^https?://##; s#/.*##')"
    NETRC_FILE=$(mktemp)
    chmod 600 "$NETRC_FILE"
    printf 'machine %s login %s password %s\n' "$HOST_ONLY" "$NC_USER" "$NC_PASS" > "$NETRC_FILE"

    check_start "curl PROPFIND against nextcloud.base_url"
    RESP_FILE=$(mktemp)
    HTTP_CODE=$(curl -s -o "$RESP_FILE" -w '%{http_code}' --netrc-file "$NETRC_FILE" --max-time 20 \
      -X PROPFIND -H "Depth: 0" -H "Content-Type: application/xml" "$BASE_URL" 2>/dev/null)
    [ -z "$HTTP_CODE" ] && HTTP_CODE="000"
    rm -f "$NETRC_FILE"
    if [ "$HTTP_CODE" = "207" ] || [ "$HTTP_CODE" = "200" ]; then
      check_ok
    else
      check_fail "HTTP $HTTP_CODE"
      echo "    response body (first 5 lines):"
      head -n5 "$RESP_FILE" 2>/dev/null | sed 's/^/      /'
    fi
    rm -f "$RESP_FILE"
  else
    check_start "curl PROPFIND against nextcloud.base_url"
    check_skip "could not read username/base_url/app password"
    sed 's/^/    /' /tmp/creds_err.$$ 2>/dev/null
  fi
  rm -f /tmp/creds_err.$$
else
  check_start "curl PROPFIND against nextcloud.base_url"
  check_skip "config, credentials, or curl missing"
fi

section "6. This application's own transport (--preflight)"
if [ -f "$CONFIG_PATH" ] && [ -f "secrets/nextcloud.env" ]; then
  check_start "calendar-sync --preflight"
  if OUTPUT=$("$PYBIN" -m calendar_sync --config "$CONFIG_PATH" --preflight --verbose 2>&1); then
    check_ok
  else
    check_fail
  fi
  echo "$OUTPUT" | sed 's/^/    /'
else
  check_start "calendar-sync --preflight"
  check_skip "config or credentials missing"
fi

section "Summary"
echo "  $((TOTAL - FAILED))/$TOTAL checks passed."
echo
cat <<'EOF'
  How to read a curl-vs-preflight difference (steps 5 and 6):
    - curl OK, --preflight FAIL:
        This host still rejects this application's own HTTP client
        specifically - re-check nextcloud.user_agent in the config, and
        compare this script's output on this host against running it on
        your own machine.
    - curl FAIL here, but curl OR --preflight succeed on your own machine:
        Points at something specific to THIS network/IP reaching
        Nextcloud from here - not a Python-vs-curl issue. Check with
        your hosting provider whether outbound HTTPS to your Nextcloud
        host/port is actually permitted from this webspace.
    - curl FAIL both here and on your own machine:
        Check the app password, 2FA, nextcloud.base_url, and whether
        Nextcloud's brute-force/IP protection is currently throttling
        the IP(s) you tested from (Nextcloud admin settings ->
        Security, or server-side fail2ban/WAF logs) before assuming a
        code problem.
EOF

[ "$FAILED" -eq 0 ]
