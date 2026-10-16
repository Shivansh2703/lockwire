#!/usr/bin/env bash
# Verify a macOS release zip: no AppleDouble files, codesign strict passes (also after quarantine),
# and record what Gatekeeper (spctl) says. Usage: scripts/verify_mac_zip.sh <zip>
set -euo pipefail
zip=${1:?usage: verify_mac_zip.sh <zip>}
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
# ditto -x folds ._ entries into xattrs, so look at the zip's own entry list
n=$(unzip -Z1 "$zip" | grep -cE '(^|/)\._' || true)
[ "$n" = 0 ] || { echo "FAIL: $n AppleDouble ._* entries in zip"; exit 1; }
echo "PASS: no ._* entries in zip"
ditto -x -k "$zip" "$tmp/x"
app=$(find "$tmp/x" -maxdepth 2 -name '*.app' -type d -prune | head -1)
[ -n "$app" ] || { echo "FAIL: no .app in zip"; exit 1; }
codesign --verify --deep --strict "$app" 2>"$tmp/err" || { echo "FAIL: codesign strict: $(cat "$tmp/err")"; exit 1; }
echo "PASS: codesign --verify --deep --strict"
ditto "$app" "$tmp/q/$(basename "$app")"
q="$tmp/q/$(basename "$app")"
xattr -rw com.apple.quarantine "0081;$(printf %x "$(date +%s)");Safari;" "$q"
codesign --verify --deep --strict "$q" 2>"$tmp/err" || { echo "FAIL: codesign strict after quarantine: $(cat "$tmp/err")"; exit 1; }
echo "PASS: codesign strict after quarantine"
# spctl rejects unsigned/ad-hoc apps; record the verdict, don't fail on it
verdict=$(spctl --assess --type execute -vv "$q" 2>&1 || true)
echo "INFO: spctl verdict: $(echo "$verdict" | tr '\n' ' ')"
