"""Tests for pairing_bundle.py, the image's bundle-intake helper.

Bundles are built in temp dirs with tarfile/base64; the helper is driven as a
CLI (the way entrypoint.sh uses it) with a minimal, controlled environment.
"""

import base64
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HELPER = os.path.join(ROOT, "pairing_bundle.py")
sys.path.insert(0, ROOT)
import pairing_bundle as pb  # noqa: E402

# Built at runtime so no token-shaped literal lives in the repo.
FAKE_TOKEN = "mgst_" + "A" * 43
OTHER_TOKEN = "mgst_" + "B" * 42 + "A"
IDENTITY = {"mac": "02:11:22:33:44:55"}
PAIRING = {"access_token": "fake-access", "refresh_token": "fake-refresh",
           "access_token_saved_at": 0, "api_url_v2": "https://example.invalid"}
DECOY = "DECOY-SECRET-" + "Z" * 24


def member(name, data, **kw):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    for k, v in kw.items():
        setattr(info, k, v)
    return info, io.BytesIO(data)


def make_tar(entries):
    """entries: list of (name, bytes) or (TarInfo, fileobj|None)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for e in entries:
            if isinstance(e[0], tarfile.TarInfo):
                tf.addfile(*e)
            else:
                tf.addfile(*member(*e))
    return buf.getvalue()


def good_entries(token=FAKE_TOKEN, identity=IDENTITY, pairing=PAIRING):
    out = [("identity.json", json.dumps(identity).encode()),
           ("pairing.json", json.dumps(pairing).encode())]
    if token is not None:
        out.append(("sdk_token", (token + "\n").encode()))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmp, "shm", "state")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_helper(self, args, extra_env=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               # decoys: secret-looking vars the helper must never read or echo
               "MUSE_PAIRING_DECOY": DECOY, "PAIRING_B64": DECOY,
               "SHIM_API_KEY": DECOY}
        env.update(extra_env or {})
        p = subprocess.run([sys.executable, HELPER] + args, env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertNotIn(DECOY, p.stdout + p.stderr)
        self.assertNotIn("fake-access", p.stdout + p.stderr)
        self.assertNotIn("fake-refresh", p.stdout + p.stderr)
        self.assertNotIn(FAKE_TOKEN, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        return p

    def unpack_b64(self, data, extra=None, args=()):
        env = {"MUSE_PAIRING_B64": base64.b64encode(data).decode()}
        env.update(extra or {})
        return self.run_helper(["unpack", "--dest", self.dest, *args], env)

    def assert_rejected(self, data, extra=None, msg=None):
        p = self.unpack_b64(data, extra)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertTrue(p.stderr.startswith("pairing_bundle: "), p.stderr)
        if msg:
            self.assertIn(msg, p.stderr)
        self.assertFalse(os.path.exists(self.dest), "dest left behind")
        # no temp siblings either
        parent = os.path.dirname(self.dest)
        if os.path.isdir(parent):
            self.assertEqual(os.listdir(parent), [])
        return p


class TestUnpack(Base):
    def test_round_trip_b64(self):
        p = self.unpack_b64(make_tar(good_entries()))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), os.path.abspath(self.dest))
        self.assertEqual(p.stderr, "")
        with open(os.path.join(self.dest, "identity.json")) as f:
            self.assertEqual(json.load(f), IDENTITY)
        with open(os.path.join(self.dest, "pairing.json")) as f:
            self.assertEqual(json.load(f), PAIRING)
        with open(os.path.join(self.dest, "sdk_token")) as f:
            self.assertEqual(f.read().strip(), FAKE_TOKEN)

    def test_base64_with_line_breaks(self):
        b64 = base64.encodebytes(make_tar(good_entries())).decode()  # 76-col lines
        p = self.run_helper(["unpack", "--dest", self.dest], {"MUSE_PAIRING_B64": b64})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_round_trip_file(self):
        path = os.path.join(self.tmp, "bundle.tar")
        with open(path, "wb") as f:
            f.write(make_tar(good_entries()))
        p = self.run_helper(["unpack", "--dest", self.dest], {"MUSE_PAIRING_FILE": path})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "pairing.json")))

    def test_b64_takes_precedence_over_file(self):
        p = self.unpack_b64(make_tar(good_entries()),
                            {"MUSE_PAIRING_FILE": os.path.join(self.tmp, "nope.tar")})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_permissions(self):
        p = self.unpack_b64(make_tar(good_entries()))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.dest).st_mode), 0o700)
        for name in os.listdir(self.dest):
            st = os.stat(os.path.join(self.dest, name))
            self.assertTrue(stat.S_ISREG(st.st_mode), name)
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600, name)
        self.assertEqual(sorted(os.listdir(self.dest)),
                         sorted(["identity.json", "pairing.json", "sdk_token",
                                 pb.SEED_MARKER]))

    def test_missing_token_allowed_with_env_token(self):
        p = self.unpack_b64(make_tar(good_entries(token=None)),
                            {"MUSEGADGET_SDK_TOKEN": FAKE_TOKEN})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "sdk_token")))

    def test_same_bundle_keeps_rotated_state(self):
        data = make_tar(good_entries())
        self.assertEqual(self.unpack_b64(data).returncode, 0)
        rotated = dict(PAIRING, refresh_token="rotated")
        with open(os.path.join(self.dest, "pairing.json"), "w") as f:
            json.dump(rotated, f)
        self.assertEqual(self.unpack_b64(data).returncode, 0)
        with open(os.path.join(self.dest, "pairing.json")) as f:
            self.assertEqual(json.load(f)["refresh_token"], "rotated")
        # --replace forces the bundle's copy back
        self.assertEqual(self.unpack_b64(data, args=["--replace"]).returncode, 0)
        with open(os.path.join(self.dest, "pairing.json")) as f:
            self.assertEqual(json.load(f)["refresh_token"], "fake-refresh")

    def test_new_bundle_replaces_existing_dir(self):
        self.assertEqual(self.unpack_b64(make_tar(good_entries())).returncode, 0)
        new = make_tar(good_entries(token=OTHER_TOKEN,
                                    identity={"mac": "02:aa:bb:cc:dd:ee"}))
        self.assertEqual(self.unpack_b64(new).returncode, 0)
        with open(os.path.join(self.dest, "identity.json")) as f:
            self.assertEqual(json.load(f)["mac"], "02:aa:bb:cc:dd:ee")
        self.assertEqual(sorted(os.listdir(self.dest)),
                         sorted(["identity.json", "pairing.json", "sdk_token",
                                 pb.SEED_MARKER]))
        self.assertEqual(stat.S_IMODE(os.stat(self.dest).st_mode), 0o700)

    def test_invalid_bundle_leaves_existing_dir_untouched(self):
        self.assertEqual(self.unpack_b64(make_tar(good_entries())).returncode, 0)
        before = {n: open(os.path.join(self.dest, n), "rb").read()
                  for n in os.listdir(self.dest)}
        bad = make_tar(good_entries() + [("extra", b"x")])
        p = self.unpack_b64(bad)
        self.assertEqual(p.returncode, 1)
        after = {n: open(os.path.join(self.dest, n), "rb").read()
                 for n in os.listdir(self.dest)}
        self.assertEqual(before, after)


class TestReject(Base):
    def test_extra_member(self):
        self.assert_rejected(make_tar(good_entries() + [("notes.txt", b"hi")]),
                             msg="unexpected member")

    def test_missing_pairing(self):
        self.assert_rejected(make_tar(good_entries()[0:1] + good_entries()[2:]),
                             msg="missing pairing.json")

    def test_missing_identity(self):
        self.assert_rejected(make_tar(good_entries()[1:]), msg="missing identity.json")

    def test_missing_token_without_env(self):
        self.assert_rejected(make_tar(good_entries(token=None)), msg="missing sdk_token")

    def test_missing_token_with_bad_env_token(self):
        self.assert_rejected(make_tar(good_entries(token=None)),
                             {"MUSEGADGET_SDK_TOKEN": "mgst_short"},
                             msg="MUSEGADGET_SDK_TOKEN")

    def test_dotdot_member(self):
        self.assert_rejected(make_tar(good_entries() + [("../evil", b"x")]),
                             msg="'..' path")

    def test_dotdot_disguised_as_member(self):
        self.assert_rejected(make_tar([("../pairing.json", json.dumps(PAIRING).encode())]
                                      + good_entries()), msg="'..' path")

    def test_absolute_member(self):
        self.assert_rejected(make_tar(good_entries() + [("/etc/evil", b"x")]),
                             msg="absolute path")

    def test_nested_member(self):
        e = good_entries()
        e[1] = ("state/pairing.json", e[1][1])
        self.assert_rejected(make_tar(e), msg="unexpected member")

    def test_symlink_member(self):
        info = tarfile.TarInfo("sdk_token")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/shadow"
        self.assert_rejected(make_tar(good_entries(token=None) + [(info, None)]),
                             msg="not a regular file")

    def test_directory_member(self):
        info = tarfile.TarInfo("identity.json")
        info.type = tarfile.DIRTYPE
        self.assert_rejected(make_tar([(info, None)] + good_entries()[1:]),
                             msg="not a regular file")

    def test_duplicate_member(self):
        self.assert_rejected(make_tar(good_entries() + good_entries()[1:2]),
                             msg="twice")

    def test_oversize_member(self):
        big = dict(PAIRING, pad="x" * (pb.MAX_MEMBER_BYTES + 1))
        self.assert_rejected(make_tar(good_entries(pairing=big)), msg="larger than 64 KiB")

    def test_oversize_bundle(self):
        # Via MUSE_PAIRING_FILE: a >256 KiB bundle's base64 exceeds Linux's
        # per-env-string limit (MAX_ARG_STRLEN, 128 KiB), so exec would fail
        # with E2BIG before the helper ran. See DESIGN.md §3.
        data = make_tar(good_entries()) + b"\0" * (pb.MAX_BUNDLE_BYTES + 1)
        path = os.path.join(self.tmp, "big.tar")
        with open(path, "wb") as f:
            f.write(data)
        p = self.run_helper(["unpack", "--dest", self.dest], {"MUSE_PAIRING_FILE": path})
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertTrue(p.stderr.startswith("pairing_bundle: "), p.stderr)
        self.assertIn("256 KiB", p.stderr)
        self.assertFalse(os.path.exists(self.dest), "dest left behind")

    def test_oversize_bundle_file(self):
        path = os.path.join(self.tmp, "big.tar")
        with open(path, "wb") as f:
            f.write(b"\0" * (pb.MAX_BUNDLE_BYTES + 10))
        p = self.run_helper(["unpack", "--dest", self.dest], {"MUSE_PAIRING_FILE": path})
        self.assertEqual(p.returncode, 1)
        self.assertIn("256 KiB", p.stderr)
        self.assertFalse(os.path.exists(self.dest))

    def test_malformed_token(self):
        self.assert_rejected(make_tar(good_entries(token="mgst_" + "A" * 10)),
                             msg="not a valid SDK token")

    def test_token_bad_final_char(self):
        # 43rd char must be one of the canonical base64url endings
        self.assert_rejected(make_tar(good_entries(token="mgst_" + "A" * 42 + "B")),
                             msg="not a valid SDK token")

    def test_non_json_pairing(self):
        e = good_entries()
        e[1] = ("pairing.json", b"access_token=fake-access")
        self.assert_rejected(make_tar(e), msg="pairing.json is not valid JSON")

    def test_pairing_not_object(self):
        e = good_entries()
        e[1] = ("pairing.json", b"[1, 2]")
        self.assert_rejected(make_tar(e), msg="not a JSON object")

    def test_pairing_without_tokens(self):
        self.assert_rejected(make_tar(good_entries(pairing={"access_token": ""})),
                             msg="lacks device tokens")

    def test_identity_without_mac(self):
        self.assert_rejected(make_tar(good_entries(identity={"node": "x"})),
                             msg="no valid device mac")

    def test_not_base64(self):
        p = self.run_helper(["unpack", "--dest", self.dest],
                            {"MUSE_PAIRING_B64": "not*base64!" + DECOY})
        self.assertEqual(p.returncode, 1)
        self.assertIn("not valid base64", p.stderr)
        self.assertFalse(os.path.exists(self.dest))

    def test_not_tar(self):
        self.assert_rejected(b"just some bytes that are not a tar archive" * 20,
                             msg="not a valid")

    def test_gzip_rejected(self):
        import gzip
        self.assert_rejected(gzip.compress(make_tar(good_entries())), msg="not a valid")

    def test_no_bundle(self):
        p = self.run_helper(["unpack", "--dest", self.dest])
        self.assertEqual(p.returncode, 1)
        self.assertIn("MUSE_PAIRING_B64", p.stderr)
        self.assertFalse(os.path.exists(self.dest))

    def test_reads_only_documented_env_names(self):
        # A valid bundle under undocumented names must be ignored.
        b64 = base64.b64encode(make_tar(good_entries())).decode()
        p = self.run_helper(["unpack", "--dest", self.dest],
                            {"PAIRING_B64": b64, "MUSE_BUNDLE_B64": b64,
                             "MUSE_PAIRING": b64})
        self.assertEqual(p.returncode, 1)
        self.assertIn("no pairing bundle", p.stderr)
        self.assertNotIn(b64[:40], p.stdout + p.stderr)
        self.assertFalse(os.path.exists(self.dest))

    def test_missing_file(self):
        p = self.run_helper(["unpack", "--dest", self.dest],
                            {"MUSE_PAIRING_FILE": os.path.join(self.tmp, "missing")})
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot open MUSE_PAIRING_FILE", p.stderr)

    def test_dest_is_file(self):
        os.makedirs(os.path.dirname(self.dest))
        with open(self.dest, "w") as f:
            f.write("x")
        p = self.unpack_b64(make_tar(good_entries()))
        self.assertEqual(p.returncode, 1)
        self.assertIn("not a directory", p.stderr)


class TestPackAndCheck(Base):
    def make_state(self, token=FAKE_TOKEN):
        state = os.path.join(self.tmp, "state")
        os.makedirs(state)
        for name, raw in good_entries(token=token):
            with open(os.path.join(state, name), "wb") as f:
                f.write(raw)
        return state

    def test_pack_then_unpack(self):
        state = self.make_state()
        out = os.path.join(self.tmp, "b.tar")
        p = self.run_helper(["pack", "--state", state, "--output", out])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        with tarfile.open(out) as tf:
            self.assertEqual(sorted(tf.getnames()),
                             ["identity.json", "pairing.json", "sdk_token"])
        p = self.run_helper(["unpack", "--dest", self.dest], {"MUSE_PAIRING_FILE": out})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_pack_rejects_bad_token(self):
        state = self.make_state(token="mgst_nope")
        out = os.path.join(self.tmp, "b.tar")
        p = self.run_helper(["pack", "--state", state, "--output", out])
        self.assertEqual(p.returncode, 1)
        self.assertFalse(os.path.exists(out))
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".bundle-")], [])

    def test_pack_rejects_symlink(self):
        state = self.make_state()
        os.unlink(os.path.join(state, "sdk_token"))
        os.symlink("/etc/hostname", os.path.join(state, "sdk_token"))
        p = self.run_helper(["pack", "--state", state,
                             "--output", os.path.join(self.tmp, "b.tar")])
        self.assertEqual(p.returncode, 1)
        self.assertIn("not a regular file", p.stderr)

    def test_check(self):
        b64 = base64.b64encode(make_tar(good_entries())).decode()
        p = self.run_helper(["check"], {"MUSE_PAIRING_B64": b64})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "bundle from MUSE_PAIRING_B64: ok")


class TestPairShContract(unittest.TestCase):
    """pair.sh duplicates the format constants; keep both sides pinned."""

    def setUp(self):
        with open(os.path.join(ROOT, "pair.sh")) as f:
            self.sh = f.read()

    def test_member_names(self):
        self.assertIn('BUNDLE_MEMBERS=(identity.json pairing.json sdk_token)', self.sh)
        self.assertEqual(pb.MEMBERS, ("identity.json", "pairing.json", "sdk_token"))

    def test_token_regex(self):
        self.assertIn("^" + pb.SDK_TOKEN_RE.pattern + "$", self.sh)

    def test_size_caps(self):
        self.assertIn(f"MAX_MEMBER_BYTES={pb.MAX_MEMBER_BYTES}", self.sh)
        self.assertIn(f"MAX_BUNDLE_BYTES={pb.MAX_BUNDLE_BYTES}", self.sh)

    def test_sdk_pin_matches_dockerfile(self):
        import re
        with open(os.path.join(ROOT, "Dockerfile")) as f:
            dockerfile = f.read()
        for var in ("MUSEGADGET_SDK_REF", "UV_VERSION", "MUSEGADGET_SDK_REPO"):
            d = re.search(rf"^ARG {var}=(\S+)$", dockerfile, re.M)
            s = re.search(rf"^{var}=(\S+)$", self.sh, re.M)
            self.assertTrue(d and s, var)
            self.assertEqual(d.group(1), s.group(1), var)

    def test_env_name(self):
        self.assertIn("MUSE_PAIRING_B64=", self.sh)

    def test_bash_syntax(self):
        for script in ("pair.sh", "entrypoint.sh"):
            p = subprocess.run(["bash", "-n", os.path.join(ROOT, script)],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)


class TestPairShStateMode(unittest.TestCase):
    """End-to-end: pair.sh --state builds a bundle the image helper accepts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state, mode=0o700)
        for name, raw in good_entries():
            with open(os.path.join(self.state, name), "wb") as f:
                f.write(raw)
        # musegadget stub for `info` (node id lookup); pair.sh must not pair.
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "musegadget")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\n"
                    "if [ \"$1\" = info ]; then\n"
                    "  echo 'node id:   homelink-334455'; echo 'BLE name:  MuseGadget334455'\n"
                    "  exit 0\nfi\n"
                    "echo \"unexpected musegadget $*\" >&2; exit 9\n")
        os.chmod(stub, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_pair(self, *args, env_extra=None):
        env = {"PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": self.tmp, "XDG_DATA_HOME": os.path.join(self.tmp, "xdg")}
        env.update(env_extra or {})
        return subprocess.run(["bash", os.path.join(ROOT, "pair.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def test_state_mode_round_trip(self):
        out = os.path.join(self.tmp, "b.tar")
        p = self.run_pair("--state", self.state, "--output", out)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertNotIn(FAKE_TOKEN, p.stdout + p.stderr)
        self.assertIn("live credential", p.stdout + p.stderr)
        with tarfile.open(out) as tf:
            self.assertEqual(sorted(tf.getnames()),
                             ["identity.json", "pairing.json", "sdk_token"])
        lines = [l for l in p.stdout.splitlines() if l.startswith("MUSE_PAIRING_B64=")]
        self.assertEqual(len(lines), 1)
        b64 = lines[0].split("=", 1)[1]
        with open(out, "rb") as f:
            self.assertEqual(base64.b64decode(b64), f.read())
        dest = os.path.join(self.tmp, "unpacked")
        q = subprocess.run([sys.executable, HELPER, "unpack", "--dest", dest],
                           env={"MUSE_PAIRING_B64": b64, "PATH": "/usr/bin:/bin"},
                           capture_output=True, text=True)
        self.assertEqual(q.returncode, 0, q.stderr)
        # creates nothing outside its own dirs
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "xdg")))

    def test_default_output_name_uses_node_id(self):
        cwd = os.path.join(self.tmp, "work")
        os.makedirs(cwd)
        env = {"PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": self.tmp}
        p = subprocess.run(["bash", os.path.join(ROOT, "pair.sh"), "--state", self.state],
                           env=env, cwd=cwd, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertTrue(os.path.isfile(os.path.join(cwd, "muse-pairing-homelink-334455.tar")))

    def test_print_env_only(self):
        p = self.run_pair("--state", self.state, "--output",
                          os.path.join(self.tmp, "b.tar"), "--print-env")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.splitlines()), 1)
        self.assertTrue(p.stdout.startswith("MUSE_PAIRING_B64="))

    def test_state_mode_rejects_bad_token(self):
        with open(os.path.join(self.state, "sdk_token"), "w") as f:
            f.write("mgst_tooshort\n")
        out = os.path.join(self.tmp, "b.tar")
        p = self.run_pair("--state", self.state, "--output", out)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(out))
        self.assertNotIn("mgst_tooshort", p.stdout + p.stderr)

    def test_state_mode_rejects_missing_pairing(self):
        os.unlink(os.path.join(self.state, "pairing.json"))
        p = self.run_pair("--state", self.state, "--output", os.path.join(self.tmp, "b.tar"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("pairing.json", p.stderr)

    def test_state_mode_rejects_non_json(self):
        with open(os.path.join(self.state, "pairing.json"), "w") as f:
            f.write("not json")
        p = self.run_pair("--state", self.state, "--output", os.path.join(self.tmp, "b.tar"))
        self.assertNotEqual(p.returncode, 0)

    def test_state_mode_env_token_not_bundled_from_env(self):
        # --state bundles the files as they are; a token only in env is refused.
        os.unlink(os.path.join(self.state, "sdk_token"))
        p = self.run_pair("--state", self.state, "--output", os.path.join(self.tmp, "b.tar"),
                          env_extra={"MUSEGADGET_SDK_TOKEN": FAKE_TOKEN})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("sdk_token", p.stderr)


if __name__ == "__main__":
    unittest.main()
