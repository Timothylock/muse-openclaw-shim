#!/bin/bash
# Entrypoint for ghcr.io/timothylock/muse-openclaw-shim. tini is PID 1.
#
#   run      (default) musegadget service + the OpenAI-compatible shim
#   service  only the musegadget service (split deployments, shared socket)
#   shim     only the shim (expects an external musegadget socket)
#   info | doctor   musegadget passthrough
#   healthcheck     used by the image HEALTHCHECK
#
# Pairing state comes from a bundle made by pair.sh (see README / DESIGN.md):
#   MUSE_PAIRING_B64   base64 of the bundle tar       (first choice)
#   MUSE_PAIRING_FILE  path to the bundle tar, e.g. /run/secrets/muse_pairing
#   otherwise          an already-unpacked state dir mounted at
#                      $MUSEGADGET_STATE_DIR (default /state)
# A bundle is unpacked into tmpfs (/dev/shm/musegadget-state, override with
# MUSE_STATE_DIR) and the script then re-executes itself WITHOUT the bundle in
# its environment, so no long-lived process inherits it.
#
# Never prints the bundle, the SDK token, or the contents of the state files.
set -euo pipefail
umask 077

APP_DIR=/opt/muse-openclaw-shim
HELPER="$APP_DIR/pairing_bundle.py"
SHIM="$APP_DIR/shim.py"
SOCKET="${MUSEGADGET_SOCKET:-/run-shared/musegadget.sock}"
RUN_AS="${MUSEGADGET_RUN_AS:-muse}"
SHIM_USER="${SHIM_RUN_AS:-shim}"
MOUNTED_STATE="${MUSEGADGET_STATE_DIR:-/state}"
TMPFS_STATE_DEFAULT=/dev/shm/musegadget-state
SOCKET_WAIT="${MUSE_SOCKET_WAIT:-30}"
MODE_FILE=/run/muse-entrypoint.mode
export MUSEGADGET_SOCKET="$SOCKET" MUSEGADGET_RUN_AS="$RUN_AS"

say() { printf 'entrypoint: %s\n' "$*" >&2; }
die() { printf 'entrypoint: error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat >&2 <<'EOF'
usage: entrypoint.sh [run|service|shim|info|doctor|healthcheck] [args...]
  run      musegadget service + shim on $PORT (default 8080)   [default]
  service  musegadget service only (shares $MUSEGADGET_SOCKET)
  shim     shim only (needs an external musegadget socket)
  info, doctor   musegadget passthrough
Pairing: set MUSE_PAIRING_B64 (from ./pair.sh) or MUSE_PAIRING_FILE, or mount
an unpacked state dir at $MUSEGADGET_STATE_DIR (default /state).
EOF
}

is_root() { [ "$(id -u)" -eq 0 ]; }

# For the HEALTHCHECK; best effort (read-only rootfs, non-root user).
record_mode() { { printf '%s\n' "$1" > "$MODE_FILE"; } 2>/dev/null || true; }

# Filesystem type of PATH, or of its nearest existing ancestor.
fs_type() {
  local p="$1"
  while [ ! -e "$p" ]; do p="$(dirname -- "$p")"; done
  stat -f -c %T -- "$p"
}

# Can the Muse command account read PATH? (It must not: Muse runs commands as it.)
readable_by_run_as() {
  is_root && id "$RUN_AS" >/dev/null 2>&1 || return 1
  setpriv --reuid="$RUN_AS" --regid="$RUN_AS" --init-groups test -r "$1" 2>/dev/null
}

# State dir root-owned 0700, files 0600 (fixes hand-filled volumes).
secure_state() {
  local d="$1" f
  chmod 0700 "$d"
  if is_root; then chown root:root "$d"; fi
  for f in identity.json pairing.json sdk_token .bundle-sha256; do
    if [ -e "$d/$f" ]; then
      chmod 0600 "$d/$f"
      if is_root; then chown root:root "$d/$f"; fi
    fi
  done
}

# Unpack a bundle (pass 1) or adopt a mounted/already-unpacked state dir.
# Sets STATE_DIR. Returns 0 if a bundle was unpacked (caller must re-exec).
UNPACKED=0
prepare_state() {
  if [ -n "${MUSE_PAIRING_B64:-}" ] || [ -n "${MUSE_PAIRING_FILE:-}" ]; then
    is_root || die "unpacking a bundle needs root (the state must be unreadable to '$RUN_AS')"
    STATE_DIR="${MUSE_STATE_DIR:-$TMPFS_STATE_DEFAULT}"
    case "$(fs_type "$STATE_DIR")" in
      tmpfs|ramfs) ;;
      *) say "WARNING: MUSE_STATE_DIR=$STATE_DIR is not tmpfs; the pairing credentials will sit on disk there." ;;
    esac
    if [ -z "${MUSE_PAIRING_B64:-}" ] && readable_by_run_as "$MUSE_PAIRING_FILE"; then
      say "WARNING: $MUSE_PAIRING_FILE is readable by '$RUN_AS', the account Muse runs commands as."
      say "         Make it root-only (e.g. owner root, mode 0400) so Muse cannot read its own credentials."
    fi
    python3 -I "$HELPER" unpack --dest "$STATE_DIR" >/dev/null \
      || die "could not unpack the pairing bundle (see the message above)"
    secure_state "$STATE_DIR"
    say "pairing bundle unpacked into $STATE_DIR"
    UNPACKED=1
    return 0
  fi
  STATE_DIR="$MOUNTED_STATE"
  if [ ! -s "$STATE_DIR/pairing.json" ]; then
    die "not paired: no pairing bundle and no $STATE_DIR/pairing.json.
  Make a bundle with ./pair.sh on a machine with Bluetooth, then pass it as
    -e MUSE_PAIRING_B64=...            (the line pair.sh prints; e.g. via --env-file .env)
  or as a file / Docker secret:
    -e MUSE_PAIRING_FILE=/run/secrets/muse_pairing
  or mount an unpacked musegadget state dir at $MOUNTED_STATE."
  fi
  if is_root; then secure_state "$STATE_DIR"; fi
  return 0
}

# Pass 1 unpacks, then re-execs without the bundle in the environment.
with_state() {
  local mode="$1"; shift
  prepare_state
  if [ "$UNPACKED" -eq 1 ]; then
    unset MUSE_PAIRING_B64 MUSE_PAIRING_FILE
    exec env -u MUSE_PAIRING_B64 -u MUSE_PAIRING_FILE \
      MUSEGADGET_STATE_DIR="$STATE_DIR" "$0" "$mode" "$@"
  fi
  export MUSEGADGET_STATE_DIR="$STATE_DIR"
}

# Command line that runs the shim as the shim account (if we are root) and
# never with secrets other than SHIM_API_KEY in its environment.
shim_cmd() {
  SHIM_CMD=(env -u MUSE_PAIRING_B64 -u MUSE_PAIRING_FILE -u MUSEGADGET_SDK_TOKEN
            SHIM_BACKEND="${SHIM_BACKEND:-gadget}")
  if is_root; then
    id "$SHIM_USER" >/dev/null 2>&1 || die "shim account '$SHIM_USER' does not exist"
    SHIM_CMD+=(setpriv --reuid="$SHIM_USER" --regid="$SHIM_USER" --init-groups)
  fi
  SHIM_CMD+=(python3 -I "$SHIM")
}

warn_api_key() {
  if [ -z "${SHIM_API_KEY:-}" ]; then
    say "WARNING: SHIM_API_KEY is not set; the shim generates one into \$SHIM_DATA_DIR/api_key."
    say "         Set SHIM_API_KEY yourself so you know the key clients must send."
  fi
}

mode_service() {
  with_state service "$@"
  is_root || die "the musegadget service must run as root (it drops to '$RUN_AS' per command)"
  install -d -m 0755 "$(dirname -- "$SOCKET")"
  record_mode service
  exec env -u MUSE_PAIRING_B64 -u MUSE_PAIRING_FILE -u SHIM_API_KEY \
    musegadget run --run-as "$RUN_AS" "$@"
}

mode_shim() {
  [ "$#" -eq 0 ] || die "shim mode takes no arguments"
  warn_api_key
  record_mode shim
  shim_cmd
  exec "${SHIM_CMD[@]}"
}

mode_run() {
  [ "$#" -eq 0 ] || die "run mode takes no arguments"
  with_state run
  is_root || die "run mode must start as root (the service drops to '$RUN_AS', the shim to '$SHIM_USER')"
  unset MUSE_PAIRING_B64 MUSE_PAIRING_FILE
  warn_api_key
  install -d -m 0755 "$(dirname -- "$SOCKET")"
  if [ -S "$SOCKET" ]; then rm -f -- "$SOCKET"; fi
  record_mode run

  local svc="" shim_pid="" rc=0 i
  shim_cmd
  on_term() {
    trap - TERM INT
    kill -TERM ${svc:+"$svc"} ${shim_pid:+"$shim_pid"} 2>/dev/null || true
  }
  trap on_term TERM INT

  env -u MUSE_PAIRING_B64 -u MUSE_PAIRING_FILE -u SHIM_API_KEY \
    musegadget run --run-as "$RUN_AS" &
  svc=$!

  # Wait for the service socket (the shim talks to it).
  i=0
  while [ ! -S "$SOCKET" ]; do
    if ! kill -0 "$svc" 2>/dev/null; then
      wait "$svc" || rc=$?
      die "musegadget service exited during startup (exit $rc)"
    fi
    i=$((i + 1))
    if [ "$i" -gt $((SOCKET_WAIT * 2)) ]; then
      kill -TERM "$svc" 2>/dev/null || true
      wait "$svc" 2>/dev/null || true
      die "musegadget socket $SOCKET did not appear within ${SOCKET_WAIT}s"
    fi
    sleep 0.5
  done
  say "musegadget service is up (socket $SOCKET); starting the shim"

  "${SHIM_CMD[@]}" &
  shim_pid=$!

  # Whichever exits first takes the container down with it.
  wait -n "$svc" "$shim_pid" || rc=$?
  if ! kill -0 "$svc" 2>/dev/null; then
    say "musegadget service exited (status $rc); stopping the shim"
  elif ! kill -0 "$shim_pid" 2>/dev/null; then
    say "shim exited (status $rc); stopping the musegadget service"
  fi
  kill -TERM "$svc" "$shim_pid" 2>/dev/null || true
  wait "$svc" 2>/dev/null || true
  wait "$shim_pid" 2>/dev/null || true
  exit "$(( rc == 0 ? 1 : rc ))"
}

mode_healthcheck() {
  local m port="${PORT:-8080}"
  m="$(cat "$MODE_FILE" 2>/dev/null || echo run)"
  if [ "$m" = run ] || [ "$m" = service ]; then
    [ -S "$SOCKET" ] || exit 1
  fi
  if [ "$m" = run ] || [ "$m" = shim ]; then
    python3 -I -c 'import sys, urllib.request
urllib.request.urlopen("http://127.0.0.1:%s/healthz" % sys.argv[1], timeout=4).read()' \
      "$port" >/dev/null 2>&1 || exit 1
  fi
  exit 0
}

mode="${1:-run}"
if [ "$#" -gt 0 ]; then shift; fi

case "$mode" in
  run)          mode_run "$@" ;;
  service)      mode_service "$@" ;;
  shim)         mode_shim "$@" ;;
  info|doctor)  with_state "$mode" "$@"
                exec env -u MUSE_PAIRING_B64 -u MUSE_PAIRING_FILE -u SHIM_API_KEY \
                  musegadget "$mode" "$@" ;;
  healthcheck)  mode_healthcheck ;;
  -h|--help|help) usage; exit 0 ;;
  *)            say "unknown mode '$mode'"; usage; exit 2 ;;
esac
