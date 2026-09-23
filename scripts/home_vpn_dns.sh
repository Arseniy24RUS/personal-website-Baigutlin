#!/usr/bin/env bash
set -euo pipefail
if test "${script_type:-}" = down; then
  resolvectl revert "${dev:?}" >/dev/null 2>&1 || true
  exit 0
fi
dns=()
for name in ${!foreign_option_@}; do
  read -r kind option value <<< "${!name}"
  if test "$kind" = dhcp-option && test "$option" = DNS && [[ "$value" =~ ^[0-9.]+$ ]]; then dns+=("$value"); fi
done
if test "${#dns[@]}" = 0; then dns=(1.1.1.1); fi
resolvectl dns "${dev:?}" "${dns[@]}" >/dev/null
resolvectl domain "$dev" '~.' >/dev/null
resolvectl default-route "$dev" true >/dev/null
