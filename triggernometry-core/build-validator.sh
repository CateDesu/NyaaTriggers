#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TN_VALIDATOR_TMP=$(mktemp "$HERE/bin/.validator-XXXXXX.exe")
trap 'rm -f "$TN_VALIDATOR_TMP"' EXIT
mcs -target:exe -out:"$TN_VALIDATOR_TMP" \
  -r:"$HERE/bin/TriggernometryPlugin.dll" -r:System.Xml.dll \
  "$HERE/host/ValidatePack.cs"
chmod 644 "$TN_VALIDATOR_TMP"
mv -f "$TN_VALIDATOR_TMP" "$HERE/bin/triggernometry-validate.exe"
