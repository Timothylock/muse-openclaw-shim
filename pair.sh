#!/usr/bin/env bash
# pair.sh - pair a Muse Gadget and write ONE pairing bundle file.
#
# Standalone: copy this file alone to any Linux machine with Bluetooth (BlueZ)
# and run it. It knows nothing about Docker, images, hosts or SSH. Its only
# output is a bundle (a tar of identity.json, pairing.json, sdk_token) plus the
# base64 env form of it, MUSE_PAIRING_B64=..., which the runtime consumes.
#
# Usage:
#   ./pair.sh                       pair (or bundle an existing pairing)
#   ./pair.sh --force               re-pair even if already paired
#   ./pair.sh --state DIR           bundle an EXISTING musegadget state dir,
#                                   no pairing, no Bluetooth, no SDK needed
#   ./pair.sh --output FILE         bundle path (default ./muse-pairing-<node-id>.tar)
#   ./pair.sh --print-env           print only the MUSE_PAIRING_B64= line on stdout
#
# Environment:
#   MUSEGADGET_SDK_TOKEN   SDK token (mgst_...) from gadgets.muse.ai; otherwise
#                          you are prompted (hidden input)
#   MUSE_PAIR_STATE_DIR    state dir (default $XDG_DATA_HOME/muse-pair/state)
#
# Uses `musegadget` from PATH if present, else installs the pinned Muse Gadget
# SDK into a private venv under $XDG_DATA_HOME/muse-pair/. Creates nothing
# outside that directory, its state dir, and the bundle file you ask for.
# Never echoes or logs the SDK token. Needs: bash, GNU tar, coreutils, python3
# (and for pairing: bluez, python3-dbus, python3-gi, python3-cryptography, git,
# curl).
set -euo pipefail
umask 077
export LC_ALL=C

# ---- bundle format contract: duplicated from pairing_bundle.py (tests pin it)
BUNDLE_MEMBERS=(identity.json pairing.json sdk_token)
MAX_MEMBER_BYTES=65536
MAX_BUNDLE_BYTES=262144
SDK_TOKEN_RE='^mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$'

# ---- pinned SDK install (same commit / uv / lock method as the Dockerfile)
MUSEGADGET_SDK_REPO=https://github.com/facebookincubator/muse-gadget-sdk
MUSEGADGET_SDK_REF=55cbf4822976592002ee05579fca5eb1f79c676b
UV_VERSION=0.9.9

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BASE="$DATA_HOME/muse-pair"
VENV="$BASE/venv"

say()  { printf '%s\n' "$*" >&2; }
warn() { printf 'pair.sh: warning: %s\n' "$*" >&2; }
die()  { printf 'pair.sh: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' >&2; }

OUTPUT="" FROM_STATE="" FORCE=0 PRINT_ENV=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output)    [ "$#" -ge 2 ] || die "--output needs a FILE"; OUTPUT="$2"; shift 2 ;;
    --output=*)  OUTPUT="${1#*=}"; shift ;;
    --state)     [ "$#" -ge 2 ] || die "--state needs a DIR"; FROM_STATE="$2"; shift 2 ;;
    --state=*)   FROM_STATE="${1#*=}"; shift ;;
    --force)     FORCE=1; shift ;;
    --print-env) PRINT_ENV=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage; die "unknown argument: $1" ;;
  esac
done
if [ -n "$FROM_STATE" ] && [ "$FORCE" -eq 1 ]; then
  die "--state bundles an existing pairing; it cannot be combined with --force"
fi

TMP_OUT=""
cleanup() { if [ -n "$TMP_OUT" ]; then rm -f -- "$TMP_OUT"; fi; }
trap cleanup EXIT

# ---- validation (mirrors pairing_bundle.py) ----------------------------------

token_ok() { [[ "$1" =~ $SDK_TOKEN_RE ]]; }

# Validate DIR's three files; print the node id on success.
validate_state() {
  local dir="$1" f size tok
  [ -d "$dir" ] || die "state dir not found: $dir"
  for f in "${BUNDLE_MEMBERS[@]}"; do
    if [ ! -e "$dir/$f" ]; then die "$f not found in $dir"; fi
    if [ -L "$dir/$f" ] || [ ! -f "$dir/$f" ]; then die "$f in $dir is not a regular file"; fi
    size="$(stat -c %s -- "$dir/$f")"
    [ "$size" -gt 0 ] || die "$f in $dir is empty"
    [ "$size" -le "$MAX_MEMBER_BYTES" ] || die "$f is larger than $((MAX_MEMBER_BYTES / 1024)) KiB"
  done
  tok="$(tr -d '[:space:]' < "$dir/sdk_token")"
  token_ok "$tok" || die "sdk_token in $dir is not a valid SDK token (expected mgst_...)"
  tok=""
  command -v python3 >/dev/null 2>&1 || die "python3 is needed to validate the JSON files"
  python3 -I - "$dir" <<'PY'
import json, os, re, sys
d = sys.argv[1]
def obj(name):
    try:
        with open(os.path.join(d, name), encoding="utf-8") as f:
            o = json.load(f)
    except Exception:
        sys.exit(f"pair.sh: {name} is not valid JSON")
    if not isinstance(o, dict):
        sys.exit(f"pair.sh: {name} is not a JSON object")
    return o
ident, pairing = obj("identity.json"), obj("pairing.json")
mac = ident.get("mac")
if not (isinstance(mac, str) and re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac)):
    sys.exit("pair.sh: identity.json has no valid device mac")
if not all(isinstance(pairing.get(k), str) and pairing[k].strip()
           for k in ("access_token", "refresh_token")):
    sys.exit("pair.sh: pairing.json lacks device tokens")
print("homelink-" + mac.replace(":", "")[-6:])
PY
}

# ---- bundle -------------------------------------------------------------------

write_bundle() {
  local dir="$1" node="$2" out outdir size sum b64
  out="${OUTPUT:-./muse-pairing-$node.tar}"
  outdir="$(dirname -- "$out")"
  [ -d "$outdir" ] || die "output directory does not exist: $outdir"
  TMP_OUT="$(mktemp "$outdir/.muse-pairing.XXXXXX")"
  tar --format=ustar --owner=0 --group=0 --numeric-owner --mode=0600 --mtime=@0 \
      -C "$dir" -cf "$TMP_OUT" -- "${BUNDLE_MEMBERS[@]}" \
    || die "tar failed (GNU tar is required)"
  chmod 0600 "$TMP_OUT"
  size="$(stat -c %s -- "$TMP_OUT")"
  [ "$size" -le "$MAX_BUNDLE_BYTES" ] || die "bundle exceeds $((MAX_BUNDLE_BYTES / 1024)) KiB"
  mv -f -- "$TMP_OUT" "$out"
  TMP_OUT=""
  out="$(cd -- "$outdir" && pwd)/$(basename -- "$out")"
  sum="$(sha256sum -- "$out" | cut -d' ' -f1)"
  b64="$(base64 -w0 -- "$out")"

  if [ "$PRINT_ENV" -eq 1 ]; then
    printf 'MUSE_PAIRING_B64=%s\n' "$b64"
  else
    {
      printf '\n===== pairing bundle =====\n'
      printf 'Bundle:  %s\n' "$out"
      printf 'SHA-256: %s\n' "$sum"
      printf 'Size:    %s bytes\n' "$size"
      printf 'Node id: %s\n' "$node"
      printf '\nPaste this line into the .env next to your compose file:\n\n'
      printf 'MUSE_PAIRING_B64=%s\n' "$b64"
      printf '\n==========================\n'
    }
  fi
  b64=""
  say ""
  say "IMPORTANT: the bundle (and the MUSE_PAIRING_B64 line) is a live credential"
  say "  for your Muse. Store it like a password; never commit it or paste it into"
  say "  chats or issues. Delete copies you no longer need."
  say "IMPORTANT: only ONE runtime may use a pairing at a time. Stop the old one"
  say "  before starting another; re-pair (./pair.sh --force) for a second gadget."
}

if [ -n "$FROM_STATE" ]; then
  node="$(validate_state "$FROM_STATE")"
  say "State in $FROM_STATE is valid (node $node)."
  write_bundle "$FROM_STATE" "$node"
  exit 0
fi

# ---- pairing mode ---------------------------------------------------------------

STATE="${MUSE_PAIR_STATE_DIR:-$BASE/state}"

bootstrap_sdk() {
  local c lock uvdir="$BASE/uv"
  for c in curl git; do
    command -v "$c" >/dev/null 2>&1 || die "$c is needed to install the Muse Gadget SDK"
  done
  [ -x /usr/bin/python3 ] || die "/usr/bin/python3 is needed (the SDK uses the distro Python)"
  /usr/bin/python3 -c 'import dbus, gi, cryptography' 2>/dev/null \
    || die "missing distro Python modules. On Debian/Ubuntu: sudo apt install python3-dbus python3-gi python3-cryptography bluez"
  mkdir -p -m 0700 "$BASE"
  export UV_CACHE_DIR="$BASE/uv-cache" UV_PYTHON_DOWNLOADS=never UV_NO_CONFIG=1
  if ! "$uvdir/uv" --version 2>/dev/null | grep -Fq "uv $UV_VERSION"; then
    say "Installing uv $UV_VERSION into $uvdir"
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
      | env UV_UNMANAGED_INSTALL="$uvdir" sh -s -- --quiet
    "$uvdir/uv" --version | grep -Fq "uv $UV_VERSION" || die "uv $UV_VERSION install failed"
  fi
  if [ "$(cat "$VENV/.muse-pair-sdk-ref" 2>/dev/null || true)" != "$MUSEGADGET_SDK_REF" ] \
     || ! "$VENV/bin/musegadget" --help >/dev/null 2>&1; then
    say "Installing the Muse Gadget SDK ($MUSEGADGET_SDK_REF) into $VENV"
    rm -rf -- "$VENV"
    "$uvdir/uv" venv --quiet --python /usr/bin/python3 --system-site-packages "$VENV"
    "$uvdir/uv" pip install --quiet --python "$VENV/bin/python" --no-deps \
      "git+$MUSEGADGET_SDK_REPO@$MUSEGADGET_SDK_REF#subdirectory=linux"
    lock="$(find "$VENV" -type f -name requirements.lock -path '*musegadget*')"
    { [ -n "$lock" ] && [ "$(printf '%s\n' "$lock" | wc -l)" -eq 1 ]; } \
      || die "could not find the SDK's requirements.lock"
    "$uvdir/uv" pip install --quiet --python "$VENV/bin/python" --require-hashes -r "$lock"
    "$VENV/bin/musegadget" --help >/dev/null || die "musegadget does not run"
    printf '%s\n' "$MUSEGADGET_SDK_REF" > "$VENV/.muse-pair-sdk-ref"
    rm -rf -- "$BASE/uv-cache"
  fi
  MG="$VENV/bin/musegadget"
}

check_bluetooth() {
  if command -v bluetoothctl >/dev/null 2>&1; then
    if ! timeout 5 bluetoothctl list 2>/dev/null | grep -q '^Controller'; then
      warn "no Bluetooth controller found (bluetoothctl list); pairing will likely fail"
    fi
  else
    warn "bluetoothctl not found; is BlueZ installed and running?"
  fi
  if [ -r /etc/bluetooth/main.conf ] \
     && ! grep -Eq '^[[:space:]]*ExchangeMTU[[:space:]]*=[[:space:]]*(2(5[6-9]|[6-9][0-9])|[3-9][0-9]{2}|[0-9]{4,})' /etc/bluetooth/main.conf; then
    warn "BlueZ ExchangeMTU is not >= 256 in /etc/bluetooth/main.conf ([GATT] section);"
    warn "  the Android Muse app may fail to pair until it is (iOS is usually fine)."
  fi
  if [ "$(id -u)" -ne 0 ]; then
    say "Note: not running as root. If pairing fails with a D-Bus/BlueZ permission"
    say "  error, rerun with sudo (keep MUSE_PAIR_STATE_DIR in mind: sudo changes \$HOME)."
  fi
}

mtime() { stat -c %Y -- "$1" 2>/dev/null || echo 0; }

if command -v musegadget >/dev/null 2>&1; then
  MG="$(command -v musegadget)"
else
  bootstrap_sdk
fi

mkdir -p -m 0700 "$STATE"
chmod 0700 "$STATE"
export MUSEGADGET_STATE_DIR="$STATE"
PAIRING="$STATE/pairing.json"
TOKEN_FILE="$STATE/sdk_token"

already=0
if [ -s "$PAIRING" ] && [ "$FORCE" -eq 0 ]; then already=1; fi

existing_token_ok=0
if [ -f "$TOKEN_FILE" ] && token_ok "$(tr -d '[:space:]' < "$TOKEN_FILE")"; then
  existing_token_ok=1
fi

# SDK token: env, else a valid one already in the state dir, else prompt.
token="${MUSEGADGET_SDK_TOKEN:-}"
unset MUSEGADGET_SDK_TOKEN   # the SDK must read the same token we bundle
if [ -z "$token" ]; then
  if [ "$existing_token_ok" -eq 1 ]; then
    if [ "$already" -eq 0 ]; then say "Using the SDK token already in $STATE."; fi
  else
    say "Paste your SDK token from gadgets.muse.ai (input is hidden):"
    { IFS= read -rs token < /dev/tty; } 2>/dev/null || true
    say ""
    [ -n "$token" ] || die "no SDK token: set MUSEGADGET_SDK_TOKEN or run this in a terminal"
  fi
fi
if [ -n "$token" ]; then
  token="$(printf '%s' "$token" | tr -d '[:space:]')"
  token_ok "$token" || { token=""; die "that is not a valid SDK token (expected mgst_ + 43 characters)"; }
  tmp_tok="$(mktemp "$STATE/.sdk_token.XXXXXX")"
  printf '%s\n' "$token" > "$tmp_tok"
  chmod 0600 "$tmp_tok"
  mv -f -- "$tmp_tok" "$TOKEN_FILE"
  token=""
  if [ "$already" -eq 1 ]; then
    warn "already paired: a new SDK token only takes effect after ./pair.sh --force"
  fi
fi

if [ "$already" -eq 1 ]; then
  say "Already paired ($PAIRING exists); bundling it. Use --force to pair again."
else
  check_bluetooth
  # `musegadget info` creates identity.json; show it without the token hint.
  info="$("$MG" info)" || die "musegadget info failed"
  node="$(printf '%s\n' "$info" | sed -n 's/^node id:[[:space:]]*//p')"
  ble="$(printf '%s\n' "$info" | sed -n 's/^BLE name:[[:space:]]*//p')"
  [ -n "$ble" ] || die "could not read the BLE name from musegadget info"
  say ""
  say "Device: $node   BLE name: $ble"
  say ""
  say "=== Pair in the Muse app ==="
  say "1. Open the Muse app and go to Settings > Devices."
  say "2. Turn on Developer mode."
  say "3. Tap Add Device."
  say "4. Pick \"$ble\"."
  say "5. Accept the community-device warning."
  say "6. Finish setup in the app."
  say "The pairing window stays open for about 10 minutes."
  say ""
  before="$(mtime "$PAIRING")"
  rc=0
  if [ "$FORCE" -eq 1 ]; then "$MG" pair --force || rc=$?; else "$MG" pair || rc=$?; fi
  if [ ! -s "$PAIRING" ] || [ "$(mtime "$PAIRING")" = "$before" ]; then
    die "NOT paired: no new pairing.json in $STATE (musegadget pair exit $rc)"
  fi
  say "Paired."
fi

chmod 0600 "$STATE"/identity.json "$PAIRING" "$TOKEN_FILE" 2>/dev/null || true
node="$(validate_state "$STATE")"
write_bundle "$STATE" "$node"
