#!/usr/bin/env python3
"""Muse Gadget pairing bundle: validate, unpack, and pack.

Stdlib only. The bundle is the ONLY contract between pairing (pair.sh) and the
runtime image. Format (pinned; pair.sh duplicates these constants):

  * an uncompressed tar archive with exactly these members at the archive
    root, all regular files, no directories, no paths:
        identity.json   JSON object, {"mac": "xx:xx:xx:xx:xx:xx", ...}
        pairing.json    JSON object with non-empty access_token/refresh_token
        sdk_token       mgst_ token (optional only when MUSEGADGET_SDK_TOKEN
                        is supplied to the runtime separately)
  * each member <= 64 KiB, whole archive <= 256 KiB
  * transported as a file (MUSE_PAIRING_FILE, e.g. a Docker secret) or as
    base64 of that file in one env var (MUSE_PAIRING_B64)

Commands:
  unpack --dest DIR [--replace]   bundle from env -> DIR (0700, files 0600)
  check                           validate the bundle from env, write nothing
  pack --state DIR --output FILE  state dir -> bundle file (0600)

This module never prints bundle contents, tokens, or file contents. On
success `unpack` prints only the destination path; on failure it prints a
reason to stderr, exits 1 and leaves no partial state behind.
"""

import argparse
import base64
import binascii
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile

# ---- the pinned format contract ---------------------------------------------
IDENTITY_FILE = "identity.json"
PAIRING_FILE = "pairing.json"
TOKEN_FILE = "sdk_token"
MEMBERS = (IDENTITY_FILE, PAIRING_FILE, TOKEN_FILE)
MAX_MEMBER_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 256 * 1024
# From the Muse Gadget SDK (musegadget/config.py): mgst_ + 43 base64url chars.
SDK_TOKEN_RE = re.compile(r"mgst_[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
# From the SDK (musegadget/identity.py). A missing/invalid mac makes the SDK
# silently mint a NEW identity, which orphans the pairing; so we reject it.
MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")

# ---- the only environment variables this module reads -----------------------
ENV_B64 = "MUSE_PAIRING_B64"
ENV_FILE = "MUSE_PAIRING_FILE"
ENV_SDK_TOKEN = "MUSEGADGET_SDK_TOKEN"

# Written into the unpacked dir: sha256 of the bundle it was seeded from.
# Lets a persistent state dir keep its rotated tokens across restarts when the
# same bundle is supplied again (see DESIGN.md, "token rotation").
SEED_MARKER = ".bundle-sha256"


class BundleError(Exception):
    """A user-facing failure. Messages never contain secret material."""


# ---- obtaining the raw bundle ---------------------------------------------

def _b64_decode(text):
    compact = "".join(text.split())          # tolerate pasted line breaks
    if not compact:
        raise BundleError(f"{ENV_B64} is empty")
    if len(compact) > 4 * ((MAX_BUNDLE_BYTES + 2) // 3):
        raise BundleError(f"{ENV_B64} is larger than the "
                          f"{MAX_BUNDLE_BYTES // 1024} KiB bundle limit")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise BundleError(f"{ENV_B64} is not valid base64") from None
    return data


def _read_file(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        raise BundleError(f"cannot open {ENV_FILE} ({e.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BundleError(f"{ENV_FILE} is not a regular file")
        if st.st_size > MAX_BUNDLE_BYTES:
            raise BundleError(f"{ENV_FILE} is larger than the "
                              f"{MAX_BUNDLE_BYTES // 1024} KiB bundle limit")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_BUNDLE_BYTES + 1)
    finally:
        os.close(fd)
    return data


def bundle_from_env(environ=None):
    """Return (raw bundle bytes, source env name). Reads ONLY ENV_B64/ENV_FILE."""
    environ = os.environ if environ is None else environ
    b64 = environ.get(ENV_B64, "")
    path = environ.get(ENV_FILE, "")
    if b64.strip():
        data, source = _b64_decode(b64), ENV_B64
    elif path.strip():
        data, source = _read_file(path.strip()), ENV_FILE
    else:
        raise BundleError(f"no pairing bundle: set {ENV_B64} (base64 of the "
                          f"bundle tar) or {ENV_FILE} (path to it)")
    if not data:
        raise BundleError(f"the bundle from {source} is empty")
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError(f"the bundle from {source} is larger than the "
                          f"{MAX_BUNDLE_BYTES // 1024} KiB limit")
    return data, source


# ---- validation ------------------------------------------------------------

def _json_object(name, raw):
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BundleError(f"{name} is not valid JSON") from None
    if not isinstance(obj, dict):
        raise BundleError(f"{name} is not a JSON object")
    return obj


def validate_files(files, token_from_env=False):
    """Check a {name: bytes} mapping against the format. Returns it normalised.

    `token_from_env`: a missing sdk_token is acceptable because the runtime
    gets MUSEGADGET_SDK_TOKEN separately.
    """
    for name in files:
        if name not in MEMBERS:
            raise BundleError("the bundle has an unexpected member")
    for name in (IDENTITY_FILE, PAIRING_FILE):
        if name not in files:
            raise BundleError(f"the bundle is missing {name}")
    for name, raw in files.items():
        if len(raw) > MAX_MEMBER_BYTES:
            raise BundleError(f"{name} is larger than {MAX_MEMBER_BYTES // 1024} KiB")
        if not raw.strip():
            raise BundleError(f"{name} is empty")

    ident = _json_object(IDENTITY_FILE, files[IDENTITY_FILE])
    mac = ident.get("mac")
    if not (isinstance(mac, str) and MAC_RE.fullmatch(mac)):
        raise BundleError(f"{IDENTITY_FILE} has no valid device mac")

    pairing = _json_object(PAIRING_FILE, files[PAIRING_FILE])
    for key in ("access_token", "refresh_token"):
        v = pairing.get(key)
        if not (isinstance(v, str) and v.strip()):
            raise BundleError(f"{PAIRING_FILE} lacks device tokens")

    out = dict(files)
    if TOKEN_FILE in files:
        try:
            token = files[TOKEN_FILE].decode("ascii").strip()
        except UnicodeDecodeError:
            token = ""
        if not SDK_TOKEN_RE.fullmatch(token):
            raise BundleError(f"{TOKEN_FILE} is not a valid SDK token (expected mgst_...)")
        out[TOKEN_FILE] = (token + "\n").encode("ascii")
    elif not token_from_env:
        raise BundleError(f"the bundle is missing {TOKEN_FILE} "
                          f"(allowed only when {ENV_SDK_TOKEN} is set)")
    return out


def parse_bundle(data, token_from_env=False):
    """Validate raw tar bytes; return {name: bytes}. Never extracts to disk."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError(f"the bundle is larger than {MAX_BUNDLE_BYTES // 1024} KiB")
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
            for m in tf.getmembers():
                name = m.name
                if name.startswith("/") or os.path.isabs(name):
                    raise BundleError("the bundle has a member with an absolute path")
                if ".." in name.replace("\\", "/").split("/"):
                    raise BundleError("the bundle has a member with a '..' path")
                if not m.isreg():
                    raise BundleError("the bundle has a member that is not a regular file")
                if name not in MEMBERS:
                    raise BundleError("the bundle has an unexpected member "
                                      f"(allowed: {', '.join(MEMBERS)} at the root)")
                if name in files:
                    raise BundleError(f"the bundle has {name} twice")
                if m.size > MAX_MEMBER_BYTES:
                    raise BundleError(f"{name} is larger than {MAX_MEMBER_BYTES // 1024} KiB")
                f = tf.extractfile(m)
                raw = f.read(MAX_MEMBER_BYTES + 1) if f else b""
                if len(raw) > MAX_MEMBER_BYTES:
                    raise BundleError(f"{name} is larger than {MAX_MEMBER_BYTES // 1024} KiB")
                files[name] = raw
    except tarfile.TarError:
        raise BundleError("the bundle is not a valid (uncompressed) tar archive") from None
    except EOFError:
        raise BundleError("the bundle is truncated") from None
    return validate_files(files, token_from_env)


def _env_token_ok(environ):
    tok = environ.get(ENV_SDK_TOKEN, "").strip()
    if not tok:
        return False
    if not SDK_TOKEN_RE.fullmatch(tok):
        raise BundleError(f"{ENV_SDK_TOKEN} is set but is not a valid SDK token")
    return True


# ---- writing ----------------------------------------------------------------

def _write_file(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _populate(d, files, fingerprint):
    for name, raw in files.items():
        _write_file(os.path.join(d, name), raw)
    _write_file(os.path.join(d, SEED_MARKER), (fingerprint + "\n").encode())


def _same_seed(dest, fingerprint):
    try:
        with open(os.path.join(dest, SEED_MARKER)) as f:
            if f.read().strip() != fingerprint:
                return False
        return all(os.path.getsize(os.path.join(dest, n)) > 0
                   for n in (IDENTITY_FILE, PAIRING_FILE))
    except OSError:
        return False


def install(files, dest, fingerprint, replace=False):
    """Install validated files into dest (0700, files 0600). All-or-nothing
    for a new dest; per-file atomic replace for an existing one (e.g. a
    mounted volume, which cannot be renamed). Returns "kept" or "written"."""
    dest = os.path.abspath(dest)
    if os.path.islink(dest):
        raise BundleError("the destination is a symlink")
    if os.path.isdir(dest):
        if not replace and _same_seed(dest, fingerprint):
            # Same bundle as last time: keep the (possibly rotated) state.
            os.chmod(dest, 0o700)
            return "kept"
        tmp = tempfile.mkdtemp(prefix=".unpack-", dir=dest)
        try:
            _populate(tmp, files, fingerprint)
            for name in list(files) + [SEED_MARKER]:
                os.replace(os.path.join(tmp, name), os.path.join(dest, name))
            for name in MEMBERS:
                if name not in files:
                    try:
                        os.unlink(os.path.join(dest, name))
                    except FileNotFoundError:
                        pass
            os.rmdir(tmp)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        os.chmod(dest, 0o700)
        return "written"
    if os.path.lexists(dest):
        raise BundleError("the destination exists and is not a directory")
    parent = os.path.dirname(dest)
    os.makedirs(parent, mode=0o755, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="." + os.path.basename(dest) + ".tmp-", dir=parent)
    try:
        os.chmod(tmp, 0o700)
        _populate(tmp, files, fingerprint)
        os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return "written"


def unpack(dest, replace=False, environ=None):
    environ = os.environ if environ is None else environ
    data, _source = bundle_from_env(environ)
    files = parse_bundle(data, token_from_env=_env_token_ok(environ))
    fingerprint = hashlib.sha256(data).hexdigest()
    return install(files, dest, fingerprint, replace=replace)


def build_tar(files):
    """Deterministic ustar archive of the validated files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name in MEMBERS:
            if name not in files:
                continue
            info = tarfile.TarInfo(name)
            info.size = len(files[name])
            info.mode = 0o600
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(files[name]))
    return buf.getvalue()


def pack(state, output):
    files = {}
    for name in MEMBERS:
        path = os.path.join(state, name)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            raise BundleError(f"{name} not found in the state dir") from None
        except OSError as e:
            raise BundleError(f"cannot read {name} ({e.strerror})") from None
        if not stat.S_ISREG(st.st_mode):
            raise BundleError(f"{name} in the state dir is not a regular file")
        if st.st_size > MAX_MEMBER_BYTES:
            raise BundleError(f"{name} is larger than {MAX_MEMBER_BYTES // 1024} KiB")
        try:
            with open(path, "rb") as f:
                files[name] = f.read(MAX_MEMBER_BYTES + 1)
        except OSError as e:
            raise BundleError(f"cannot read {name} ({e.strerror})") from None
    files = validate_files(files)
    data = build_tar(files)
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError("the bundle would exceed the size limit")
    out = os.path.abspath(output)
    fd, tmp = tempfile.mkstemp(prefix=".bundle-", suffix=".tmp",
                               dir=os.path.dirname(out))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out


# ---- CLI --------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="pairing_bundle.py",
                                 description="Muse Gadget pairing bundle tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("unpack", help=f"unpack the bundle from {ENV_B64} or {ENV_FILE}")
    u.add_argument("--dest", required=True)
    u.add_argument("--replace", action="store_true",
                   help="overwrite even if DIR was seeded from this same bundle")
    sub.add_parser("check", help="validate the bundle from the environment only")
    p = sub.add_parser("pack", help="bundle an existing state dir")
    p.add_argument("--state", required=True)
    p.add_argument("--output", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "unpack":
            unpack(args.dest, replace=args.replace)
            print(os.path.abspath(args.dest))
        elif args.cmd == "check":
            data, source = bundle_from_env()
            parse_bundle(data, token_from_env=_env_token_ok(os.environ))
            print(f"bundle from {source}: ok")
        else:
            print(pack(args.state, args.output))
    except BundleError as e:
        print(f"pairing_bundle: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"pairing_bundle: I/O error ({e.__class__.__name__}: {e.strerror})",
              file=sys.stderr)
        return 1
    except Exception as e:  # never dump a traceback that could hold data
        print(f"pairing_bundle: internal error ({e.__class__.__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
