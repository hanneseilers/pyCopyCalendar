#!/usr/bin/env bash
# Build a clean, deployable copy of calendar-sync in build/upload/, ready
# to upload as-is to a shared webspace (SFTP/SCP/rsync) and start there.
#
# Usage:
#   tools/build.sh
#
# What ends up in build/upload/: the application source (src/),
# setup.php (the web setup wizard), connection_test.sh (the SSH
# connectivity check), the config/secrets templates and their .htaccess
# guards, pyproject.toml/requirements.lock, README.md and LICENSE -
# exactly the version-controlled files, taken via `git ls-files` so
# nothing untracked (your local config.yaml, secrets/nextcloud.env,
# the SQLite database, logs, .venv, __pycache__, ...) can accidentally
# end up in the bundle.
#
# Left out on purpose, even though they're version-controlled:
# tests/, TECHNICAL_SPECIFICATION.md, CODING_AGENT_PROMPT.md,
# requirements-dev.lock, .gitignore, and this tools/ directory itself -
# none of those are needed to run the application.
#
# This script does NOT create a Python virtualenv inside the bundle:
# compiled dependencies (e.g. lxml) are built for *this* machine's
# Python version and CPU architecture, which will generally not match
# your shared webspace's - copying a venv across would likely just
# fail to import there. Create the venv on the server itself after
# uploading instead (see README's "Installation" and the printed next
# steps below).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build/upload"

cd "$PROJECT_ROOT"

if ! command -v git >/dev/null 2>&1; then
    echo "error: git is required to determine which files to bundle" >&2
    exit 1
fi
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "error: must be run inside the git working copy (no repository found at $PROJECT_ROOT)" >&2
    exit 1
fi

echo "Building deployable copy in: $BUILD_DIR"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# Version-controlled files only (see header comment for what's excluded
# and why), copied with their directory structure preserved.
EXCLUDE_REGEX='^(tests/|TECHNICAL_SPECIFICATION\.md$|CODING_AGENT_PROMPT\.md$|\.gitignore$|tools/|requirements-dev\.lock$)'

copied=0
while IFS= read -r -d '' file; do
    if [[ "$file" =~ $EXCLUDE_REGEX ]]; then
        continue
    fi
    dest="$BUILD_DIR/$file"
    mkdir -p "$(dirname "$dest")"
    cp "$file" "$dest"
    copied=$((copied + 1))
done < <(git ls-files -z)

chmod +x "$BUILD_DIR/connection_test.sh" 2>/dev/null || true

echo "Done: $copied files copied."
echo
echo "Next steps:"
echo "  1. Upload the *contents* of build/upload/ to your webspace, e.g.:"
echo "       rsync -av --delete \"$BUILD_DIR/\" youruser@yourhost:/path/to/calendar-sync/"
echo "     (or drag-and-drop the contents via SFTP)."
echo "  2. SSH in and set up the virtualenv there (see README \"Installation\"):"
echo "       cd /path/to/calendar-sync"
echo "       python3 -m venv .venv && .venv/bin/pip install -r requirements.lock"
echo "  3. Configure Nextcloud access: open setup.php in your browser, or"
echo "     create config/config.yaml and secrets/nextcloud.env by hand from"
echo "     the .example templates."
echo "  4. Run ./connection_test.sh once over SSH to verify the connection,"
echo "     then set up a cron job (see README)."
