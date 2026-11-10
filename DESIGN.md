# Design notes: decoupled pairing and a bundle-fed runtime

This file records the design decisions behind the bundle and the runtime
image, including where the implementation departs from the original brief
and why.

## 1. Why pairing and running are decoupled

Pairing needs a Bluetooth radio, the Muse app nearby, and a person. Running
needs none of that: just outbound network and the credentials pairing
produced. An earlier prototype paired inside the container and then copied
the state from host to host over SSH. That tied pairing to one particular
deployment topology (which hosts exist, how to reach them, where the volume
lives).

The decoupled design has one contract, the **bundle file**:

```
pair.sh (any Linux + BlueZ box)  ──bundle──▶  runtime image (any Docker host)
   knows: SDK, BLE, the format                 knows: the format, the SDK service
   not:   Docker, images, hosts, SSH           not:   how pairing happened
```

`pair.sh` is a single file you can copy on its own. It has no sibling
imports, and it uses `musegadget` from PATH or bootstraps the pinned SDK into
its own data dir. The image has no BlueZ, no D-Bus daemon, and no pair mode.

## 2. Bundle intake options

| | `MUSE_PAIRING_B64` (env) | `MUSE_PAIRING_FILE` (Docker secret / bind-mounted file) | Mounted state dir |
|---|---|---|---|
| `docker inspect` | **Shows the value** (Config.Env) | Shows only the mount path | Shows only the mount path |
| Host disk | `.env` file, plus Docker's container config (`/var/lib/docker/containers/<id>/config.v2.json`, root-only) | The bundle file on the host | The state files on the host/volume |
| `/proc/<pid>/environ` | PID 1 (tini) keeps it for the container's life. Readable by root only (same-uid or CAP_SYS_PTRACE rule). | Path only | Nothing |
| Persists token rotation | No (tmpfs) | No (tmpfs) | **Yes** |
| Ergonomics | Best: one line in `.env`, works with plain `docker run`, compose, Portainer and most PaaS UIs | Good with compose `secrets:` or swarm; host file ownership matters (see below) | Worst for "copy one thing"; good for long-lived single hosts |
| Readable by the in-container `muse` account | No (root's environ) | **Yes, if the host file is owned by uid 1000 or world-readable** (bind mounts keep host ownership). The entrypoint detects and warns. | No: the entrypoint chowns it to root and sets 0700/0600 |

**What is implemented:** all three, in this priority order: B64, then FILE,
then mounted state. Env stays the first-class path, as the owner asked.

- Any bundle is decoded and validated in memory by `pairing_bundle.py`, a
  stdlib-only, unit-tested helper, so the entrypoint shell stays thin. It is
  written to a temp sibling dir and renamed into place under
  `/dev/shm/musegadget-state` (0700 root, files 0600). If anything fails, it
  prints a reason, exits 1, and leaves nothing behind.
- The entrypoint then **re-execs itself** with `env -u MUSE_PAIRING_B64 -u
  MUSE_PAIRING_FILE`. It also starts the service and the shim with `env -u`.
  So the only process that still holds the bundle in its environment is PID 1
  (tini). An env var given to a container always lands there, and that can't
  be scrubbed.
- The SDK service already gives the commands Muse runs a clean, minimal
  environment (`HOME/USER/LOGNAME/PATH/LANG`). The scrub is defence in depth
  on top of that.
- `MUSE_STATE_DIR` overrides the unpack location. If it isn't tmpfs/ramfs, the
  entrypoint warns that credentials will sit on disk.

**Honest limits.** The goal is to keep the bundle out of image layers,
container writable layers and volumes (by default), logs, git, and the
environment of long-lived processes. Defeating the host's root is not a
goal. Anyone who is root on the host, can run `docker inspect`, or can reach
the Docker socket can read the bundle: from the env, from `/proc/1/environ`,
from the container's `/dev/shm`, or from process memory. With the env path
the bundle also exists on the host disk (`.env`, Docker's container config).
Compared with plain env, `MUSE_PAIRING_FILE` keeps it out of `docker
inspect` and PID 1's environ. That is the better choice when other people
have `docker inspect` access but not root.

## 3. The format is the contract

The format is: an uncompressed ustar/GNU tar containing exactly
`identity.json`, `pairing.json` and `sdk_token` at the archive root, all
regular files; each file ≤ 64 KiB and the archive ≤ 256 KiB; the token
matches `mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]` (the regex from the
public SDK).

- **Env size limit.** Linux caps each environment string at about 128 KiB
  (`MAX_ARG_STRLEN`). A larger `MUSE_PAIRING_B64` makes `exec` fail with
  `E2BIG` before the helper even starts. Real bundles are a few KiB, so the
  env intake is unaffected. Anything larger, including a bundle near the
  256 KiB cap, has to go through `MUSE_PAIRING_FILE` (Docker secret or file).
  The tests follow the same split: the total-bundle cap is tested through
  the file intake, and the per-member cap through the env intake.

- `pairing_bundle.py` owns the canonical constants. `pair.sh` duplicates them
  (`BUNDLE_MEMBERS`, `MAX_MEMBER_BYTES`, `MAX_BUNDLE_BYTES`,
  `SDK_TOKEN_RE`). `tests/test_pairing_bundle.py::TestPairShContract` pins
  the two copies against each other. An end-to-end test also runs
  `pair.sh --state` and feeds its output, both the file and the
  `MUSE_PAIRING_B64` line, to the image's helper.
- I followed the brief's recommendation: `pair.sh` packs with plain GNU tar
  (`--format=ustar --owner=0 --group=0 --numeric-owner --mode=0600
  --mtime=@0`) and runs its own small checks, using an inline `python3 -I`
  heredoc for the JSON checks. It does not call `pairing_bundle.py`, so it
  stays a single copyable file. `pairing_bundle.py` also has `pack` and
  `check` subcommands. These are handy from a checkout, but `pair.sh` doesn't
  depend on them.
- Fallback if someone has neither script: `tar --format=ustar -C STATE -cf
  bundle.tar identity.json pairing.json sdk_token` produces a valid bundle.
  The image validates it either way.

## 4. Changes compared with the brief, and why

1. **Token rotation versus an immutable bundle (the most important finding).**
   The SDK service rotates the device token pair about every 3 hours
   (`TOKEN_REFRESH_AGE_S`) and rewrites `pairing.json` with the **new
   refresh token**. If it gets a 401 on refresh, it **deletes**
   `pairing.json` ("pairing revoked").

   A tmpfs copy of a fixed env bundle therefore loses the rotated tokens on
   every container restart. If the backend invalidates superseded refresh
   tokens (common refresh-token-rotation practice), any restart more than
   about 3 hours after pairing breaks the pairing. I could not verify the
   backend's behaviour here.

   What I did:
   - Kept tmpfs as the default, as the brief asked.
   - Made the unpack **seed-aware**. The helper records the bundle's SHA-256
     in the state dir (`.bundle-sha256`). When the state dir already holds
     state seeded from the *same* bundle, it keeps that (rotated) state
     instead of overwriting it with stale tokens. A *different* bundle always
     replaces it, so "new bundle = restart" still holds. `--replace` forces a
     fresh copy.
   - Documented the opt-in `MUSE_STATE_DIR=/state` plus named volume
     (credentials on disk, warned about) for unattended restarts.

   **Test before relying on restarts:** pair, run for more than 3 hours
   (watch for `device token rotated` in the logs), `docker restart`, and
   check for `pairing revoked`. If it appears, make the volume mode the
   documented default for unattended hosts.

2. **The shim runs as its own `shim` user (uid 1001, in group `muse`), not as
   root or `muse`.** Muse can run arbitrary commands as `muse`. A shim
   running as `muse` would let those commands read the shim's
   `SHIM_API_KEY` from `/proc/<pid>/environ` and its thread map. Group
   `muse` is enough to reach the service socket (0660 root:muse). The
   service itself must run as root, because the SDK drops to `--run-as` per
   command.

3. **`identity.json` must contain a valid `mac`, and `pairing.json` must
   contain non-empty `access_token`/`refresh_token`.** The brief only asked
   for "parses as a JSON object". But if the SDK sees an identity without a
   valid mac, it **silently mints a new identity**, which orphans the
   pairing. Failing at intake is far easier to diagnose.

4. **No bluez/dbus daemon packages in the image.** The image never pairs.
   python3-dbus/gi/cryptography stay, exactly as in the verified install.

5. **`.dockerignore` is an allowlist.** Only the four files the image needs
   enter the build context, so a stray `.env` or `*.tar` in a checkout can
   never be baked into an image.

6. **The entrypoint re-execs itself after unpacking** (in addition to
   `env -u` on each child). Bash's `unset` doesn't change the process's
   initial environ block, which is what `/proc/<pid>/environ` shows. Only a
   fresh exec drops it.

7. **`tini -g`** delivers SIGTERM to the service and the shim alike. The
   `run` supervisor also traps TERM/INT, waits with `wait -n`, and stops the
   survivor when either process exits. An unexpected exit makes the
   container exit non-zero.

8. **`pair.sh` details.**
   - When it bootstraps uv itself, it uses `UV_UNMANAGED_INSTALL` instead of
     `UV_INSTALL_DIR`. The managed installer can write an install receipt
     under `~/.config`; the unmanaged mode writes no receipt, never
     self-updates, and leaves PATH alone. It also sets `UV_CACHE_DIR`, `UV_NO_CONFIG` and
     `UV_PYTHON_DOWNLOADS=never`, so nothing is created outside
     `~/.local/share/muse-pair`.
   - It strips the SDK token hint (the first 12 characters) from the
     `musegadget info` output it shows.
   - It warns about, but does not edit, BlueZ `ExchangeMTU`. Editing host
     config contradicts "creates nothing outside its dir".
   - If already paired without `--force`, it bundles the existing state
     instead of failing.
   - Human-readable output goes to stderr and only the summary block goes to
     stdout, so `--print-env` output is clean for redirection.

## 5. Coupling and risks that remain

- **Token rotation** (section 4.1): runtime state drifts from the bundle by
  design of the SDK. This is the one real coupling between "the bundle" and
  "the running instance", and it should be verified as described there.
- **One live runtime per pairing.** Two containers with the same bundle will
  fight over the same device identity, and each may rotate tokens out from
  under the other. This is documented but can't be enforced locally.
- **SDK pin duplicated** in `Dockerfile` and `pair.sh` (commit and uv
  version). Bump both together. A test
  (`TestPairShContract.test_sdk_pin_matches_dockerfile`) fails if they drift.
- **Shim threads are ephemeral.** `threads.json` lives in the container
  (`/var/lib/muse-shim`), so a restart starts new Muse side chats. This is
  harmless for glasses use. Mount a volume at `SHIM_DATA_DIR` if you want
  continuity.
- **`curl | sh` for uv** follows the SDK installer's own pinned-version
  method in both the image and `pair.sh`. It isn't checksum-pinned.
- **Static review only.** No Docker was available while writing this, so the
  image and entrypoint are reviewed statically. CI builds the image, and a
  first real run on a target host should exercise `run`, a `docker stop`
  (clean shutdown), and a kill of the service (container exits).
