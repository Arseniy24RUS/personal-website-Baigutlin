#!/usr/bin/env bash
# Run from the isolated copy; never depend on access to the runner's home.
set -euo pipefail
stage="${1:?}"
python="${2:?}"
sources="${3:-}"
cd "$stage"
export HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
export XDG_CONFIG_HOME="${RUNNER_TEMP:?}/config"
export XDG_CACHE_HOME="$RUNNER_TEMP/cache"
export XDG_RUNTIME_DIR="$RUNNER_TEMP/xdg"
export TMPDIR="$RUNNER_TEMP"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
xvfb-run -a "$python" scripts/browser_preflight.py
exec xvfb-run -a "$python" scripts/refresh_pipeline.py collect --stage "$stage" --only "$sources"
