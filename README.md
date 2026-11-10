# muse-openclaw-shim

An OpenAI-compatible HTTP shim that lets OpenClaw-style clients talk to your
own Muse agent. For example: Even Realities G2 glasses via
the Even app, or anything else that speaks `POST /v1/chat/completions`.

The shim talks to Muse through the official
[Muse Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk): the
container runs as a paired Muse Gadget, and the shim forwards each message
with `musegadget send-user-msg`.

Pairing and running are separate. `pair.sh` pairs a gadget on any Linux
machine with Bluetooth and writes one **bundle** file. The image takes that
bundle as an environment variable and runs. They share nothing else.

## Quickstart (prebuilt image)

1. **Get an SDK token.** Sign in at [gadgets.muse.ai](https://gadgets.muse.ai)
   and copy your SDK token (it starts with `mgst_`).

2. **Pair.** On any Linux machine with Bluetooth (BlueZ), not necessarily
   your server:

   ```sh
   curl -fsSLO https://raw.githubusercontent.com/timothylock/muse-openclaw-shim/main/pair.sh
   less pair.sh            # read it first: it handles a credential
   chmod +x pair.sh
   ./pair.sh               # prompts for the token (hidden), or set MUSEGADGET_SDK_TOKEN
   ```

   The script prints a BLE name such as `MuseGadget1A2B3C`. In the Muse app,
   go to **Settings > Devices**, turn on **Developer mode**, tap **Add
   Device**, pick that BLE name, accept the community-device warning, and
   finish setup. When pairing completes, `pair.sh` prints the bundle path, its
   SHA-256 and size, and one line:

   ```
   MUSE_PAIRING_B64=...
   ```

3. **Create `.env`** on the server (`chmod 600 .env`, never commit it):

   ```sh
   MUSE_PAIRING_B64=...      # the whole line from pair.sh
   SHIM_API_KEY=...          # a long random key you choose, e.g.
                             # python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```

4. **Run the image:**

   ```sh
   docker run -d --name muse-openclaw-shim --restart unless-stopped \
     --env-file .env \
     --security-opt no-new-privileges \
     -p 127.0.0.1:18611:8080 \
     ghcr.io/timothylock/muse-openclaw-shim:latest
   ```

   Or with compose: `cp compose.example.yml compose.yml && docker compose up -d`.

5. **Point your client** at `http://your-server:18611` and use your
   `SHIM_API_KEY` as the API key (`Authorization: Bearer <key>`). The model id
   is `muse`. The port is bound to loopback above. To reach it from other
   machines, publish it on a LAN/VPN address or put a TLS reverse proxy in
   front. Don't expose plain HTTP to the internet.

Check it: `curl http://127.0.0.1:18611/healthz` returns `{"ok": true}`, and
`docker logs muse-openclaw-shim` shows the service connecting.

## The pairing bundle

**Format.** These rules are the contract between `pair.sh` and the image:

- an uncompressed tar archive (`muse-pairing-<node-id>.tar`) containing
  exactly these three regular files at the archive root, with no directories
  and no paths:
  - `identity.json`: the gadget identity (`{"mac": ...}`)
  - `pairing.json`: the device tokens from pairing
  - `sdk_token`: your `mgst_...` SDK token
- each file at most 64 KiB, the archive at most 256 KiB
- passed to the container as `MUSE_PAIRING_B64` (the archive, base64,
  `base64 -w0`) or as a file via `MUSE_PAIRING_FILE`.

`sdk_token` may be left out only if you pass `MUSEGADGET_SDK_TOKEN`
separately. The image rejects anything else: extra or missing members,
`..` or absolute paths, links, oversized files, invalid JSON, or a malformed
token. A rejected bundle leaves nothing behind.

Already have a working musegadget state dir? Bundle it without re-pairing:
`./pair.sh --state /path/to/state`.

**Security properties.**

- Inside the container, the bundle is unpacked only into tmpfs
  (`/dev/shm/musegadget-state`). The directory is root-only (0700) and each
  file is 0600. Nothing goes into the image layers or onto a volume.
- The entrypoint re-executes itself without `MUSE_PAIRING_B64` and starts the
  service and shim with `env -u MUSE_PAIRING_B64`, so no long-lived process
  inherits the bundle.
- The bundle, the token, and the file contents are never printed or logged,
  by `pair.sh` or by the image.
- Limits: anyone with root on the Docker host, or with access to
  `docker inspect` or the Docker socket, can read the bundle. Env vars appear
  in `docker inspect`, and Docker also stores them in the container's config
  on the host disk. Your `.env` file is a copy too. See [DESIGN.md](DESIGN.md).

**One live runtime per pairing.** Only one container (or host) may run a
given pairing at a time. Stop the old one before starting a new one. For a
second gadget, pair again to get a second bundle.

**Re-pair or rotate.** Run `./pair.sh --force` to get a new bundle, replace
`MUSE_PAIRING_B64` in `.env`, and restart the container
(`docker compose up -d` or `docker rm -f` plus `docker run`). A new bundle
plus a restart is the whole procedure. To revoke a pairing, remove the
device in the Muse app.

### Token rotation and restarts (read this)

The SDK service renews the device tokens about every 3 hours and rewrites
`pairing.json`. With the default tmpfs setup, those renewed tokens are lost
when the container restarts, and the container falls back to the tokens in
your bundle.

If the Muse backend still accepts the bundle's original refresh token, this
doesn't matter. If it rotates refresh tokens (invalidating the old one), a
restart more than about 3 hours after pairing will log `pairing revoked`,
and you'll need to re-pair. Whether old refresh tokens stay valid hasn't
been verified yet (see DESIGN.md).

If you need unattended restarts and this bites, keep the state on a volume
and accept that credentials then sit on disk:

```sh
docker run ... -e MUSE_STATE_DIR=/state -v muse-state:/state ...
```

The bundle then only seeds the volume. On later starts, if the same bundle
is supplied again, the rotated state on the volume is kept. A different
bundle replaces it. The entrypoint warns that the state is not on tmpfs.

### Passing the bundle as a file / Docker secret

```sh
sudo chown root:root muse-pairing-*.tar && sudo chmod 0400 muse-pairing-*.tar
docker run ... -e MUSE_PAIRING_FILE=/run/secrets/muse_pairing \
  -v "$PWD/muse-pairing-homelink-xxxxxx.tar:/run/secrets/muse_pairing:ro" ...
```

The file must not be readable by uid 1000 (the account Muse runs commands
as inside the container). The entrypoint warns if it is. With compose, use a
top-level `secrets:` entry with `file:` and the same ownership rule.

## Reference

### Modes (container command)

| Mode | What it does |
|---|---|
| `run` (default) | Unpacks the bundle (or uses mounted state), starts `musegadget run`, waits up to 30 s for its socket, then runs the shim. If either process exits, the container exits. |
| `service` | Only the musegadget service, for split deployments. Share `/run-shared` (the socket) with a `shim` container. |
| `shim` | Only the shim. Expects a musegadget socket at `$MUSEGADGET_SOCKET`. |
| `info`, `doctor` | `musegadget info` / `doctor`, with state from the bundle if one is given. |

### Environment

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `MUSE_PAIRING_B64` | | image | Base64 of the bundle tar. Takes precedence. |
| `MUSE_PAIRING_FILE` | | image | Path to the bundle tar (e.g. a Docker secret). |
| `MUSE_STATE_DIR` | `/dev/shm/musegadget-state` | image | Where a bundle is unpacked. You get a warning if it isn't tmpfs. |
| `MUSEGADGET_STATE_DIR` | `/state` | image | Mounted, already-unpacked state, used when no bundle is given. |
| `MUSEGADGET_SDK_TOKEN` | | image, pair.sh | SDK token, if not in the bundle (image) or to skip the prompt (pair.sh). |
| `MUSEGADGET_SOCKET` | `/run-shared/musegadget.sock` | image, shim | Service socket. |
| `MUSEGADGET_RUN_AS` | `muse` | image | Account Muse's commands run as. |
| `MUSE_SOCKET_WAIT` | `30` | image | Seconds to wait for the service socket in `run` mode. |
| `SHIM_RUN_AS` | `shim` | image | Account the shim runs as. |
| `SHIM_API_KEY` | generated | shim | Bearer key clients must send. If unset, one is generated into `$SHIM_DATA_DIR/api_key`. |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | shim | Listen address. |
| `SHIM_DATA_DIR` | `/var/lib/muse-shim` (image) | shim | Per-user thread map and generated key. |
| `SHIM_MODEL_ID` | `muse` | shim | Model id reported by `/v1/models`. |
| `SHIM_MSG_PREFIX` | terse-reply instruction for glasses | shim | Text prepended to every message. Set it to an empty string to disable. |
| `SHIM_HEARTBEAT` | `10` | shim | SSE heartbeat interval (s). |
| `GADGET_CLI` | `musegadget` | shim | The `musegadget` binary the shim runs. |
| `MUSE_PAIR_STATE_DIR` | `$XDG_DATA_HOME/muse-pair/state` | pair.sh | Where pair.sh keeps the gadget state. |

### Shim API

- `GET /healthz`: open, no auth.
- `GET /v1/models`: needs the Bearer key.
- `POST /v1/chat/completions` (any POST path works): needs the Bearer key.
  Streaming and non-streaming are both supported. Only the last user message
  is forwarded. Muse keeps the conversation in a per-user side chat. The
  user is taken from the request's `user` field, the `X-Shim-User` header, or
  the `x-openclaw-agent-id` header, in that order.

### `pair.sh` flags

`--output FILE` (bundle path), `--state DIR` (bundle an existing state dir;
no Bluetooth or SDK needed), `--force` (re-pair), `--print-env` (print only
the `MUSE_PAIRING_B64=` line).

`pair.sh` uses `musegadget` from your PATH if it is installed. Otherwise it
installs the pinned SDK into `~/.local/share/muse-pair/venv` and touches
nothing else on the system. Host requirements: bash, GNU tar, coreutils,
python3, plus bluez, python3-dbus, python3-gi, python3-cryptography, git and
curl for pairing. If the Android app won't pair, set `ExchangeMTU = 256` in
the `[GATT]` section of `/etc/bluetooth/main.conf` (pair.sh warns about
this but doesn't change it).

## Security notes

- **What the gadget can do.** Muse can run shell commands on a paired gadget;
  that's what a gadget is. In this image, commands run as the unprivileged
  `muse` user (uid 1000) in an otherwise empty container. That user can't
  read the pairing state (root-only tmpfs) or the shim's key and data (the
  shim runs as a separate `shim` user). It does have the container's network
  access. Don't mount host paths or the Docker socket into this container,
  and don't run it with `--privileged` or host networking.
- **Keep the bundle secret.** It is a live credential for your Muse.
  Store `.env` and `*.tar` bundles like passwords (mode 0600), never commit
  them (`.gitignore` covers them), and delete copies you don't need.
- **Pin image tags.** Use a release tag (`:v1.2.3`) or a digest
  (`@sha256:...`) rather than `:latest` for anything long-lived, and
  read the changes before upgrading.
- **Network.** The shim speaks plain HTTP with a Bearer key. Keep it on
  loopback, a LAN or a VPN, or behind a TLS reverse proxy.

## Building from source and running tests

```sh
docker build -t muse-openclaw-shim .          # build context = repo root
python3 -m unittest discover -s tests         # stdlib only, no Docker needed
bash -n pair.sh entrypoint.sh
```

The image pins the Muse Gadget SDK commit, the uv version, and the SDK's
hash-locked requirements (see the `ARG`s in the Dockerfile). `pair.sh` pins
the same values. CI (`.github/workflows/publish-image.yml`) runs the tests,
then publishes `ghcr.io/timothylock/muse-openclaw-shim`: `latest` from
`main`, `vX.Y.Z` from tags, and a short-sha tag for every build.

## License

MIT. See [LICENSE](LICENSE).
