#!/usr/bin/env bash
set -euo pipefail
stage="${1:?}"
python="${2:?}"
cd "$stage"
export XDG_CONFIG_HOME="${RUNNER_TEMP:?}/config"
export XDG_CACHE_HOME="$RUNNER_TEMP/cache"
export XDG_RUNTIME_DIR="$RUNNER_TEMP/xdg"
export TMPDIR="$RUNNER_TEMP"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
xvfb-run -a "$python" scripts/browser_preflight.py
exec xvfb-run -a "$python" scripts/browser_sessions.py maintain --stage "$stage" --reports "${BROWSER_SESSION_REPORT_DIR:?}"
