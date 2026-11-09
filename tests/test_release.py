#!/usr/bin/env python3
"""The pure parts of the release pipeline against local stand-ins: the store (a local http server), gh (a script on PATH)
and the Apple tools (scripts on PATH). No network, no account, no signing material."""
import hashlib
import http.server
import importlib.util
import json
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "implementation/server/scripts"
FIXTURES = ROOT / "tests/fixtures"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
import app_manifest  # noqa: E402
import platform_artifacts  # noqa: E402
import platform_manifest  # noqa: E402
import release_targets as TARGETS  # noqa: E402

ALL_ON = json.loads((FIXTURES / "switches.json").read_text())["allOn"]
APP = json.loads((FIXTURES / "app.json").read_text())
PREVIOUS = json.loads((FIXTURES / "previous-manifest.json").read_text())


class Store(http.server.BaseHTTPRequestHandler):
    answer = (200, json.dumps({"targets": ALL_ON}))
    # Answers given once each, in order, before falling back to answer.
    queue = []
    seen = []

    def do_GET(self):
        Store.seen.append((self.path, self.headers.get("Authorization")))
        status, body = Store.queue.pop(0) if Store.queue else Store.answer
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, shutil, sys
out = os.environ["FAKE_OUT"]
args = sys.argv[1:]
with open(os.path.join(out, "calls.txt"), "a") as f:
    f.write(json.dumps(args) + "\n")
assets_file = os.path.join(out, "assets.json")
assets = json.load(open(assets_file)) if os.path.exists(assets_file) else []
if args[:3] == ["api", "-X", "POST"]:
    print(json.dumps({"id": 777}))
elif args[:1] == ["api"] and args[1].endswith("/releases/latest"):
    latest = os.environ.get("FAKE_LATEST", "")
    if not latest:
        sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
    print(json.dumps({"tag_name": latest}))
elif args[:1] == ["api"]:
    print(json.dumps({"assets": [{"name": n, "url": "https://api.github.com/new/" + n} for n in assets]}))
elif args[:2] == ["release", "upload"]:
    os.makedirs(os.path.join(out, "uploads"), exist_ok=True)
    for a in args[3:]:
        if os.path.isfile(a):
            shutil.copy(a, os.path.join(out, "uploads", os.path.basename(a)))
            assets.append(os.path.basename(a))
    json.dump(assets, open(assets_file, "w"))
elif args[:2] == ["release", "download"]:
    d = args[args.index("--dir") + 1]
    os.makedirs(d, exist_ok=True)
    shutil.copy(os.environ["FAKE_PREVIOUS"], os.path.join(d, "my-app.json"))
elif args[:2] == ["release", "edit"]:
    open(os.path.join(out, "published"), "w").write(args[2])
elif args[:2] == ["release", "delete"]:
    open(os.path.join(out, "deleted"), "w").write(args[2])
'''


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Store)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Store.answer = (200, json.dumps({"targets": ALL_ON}))
        Store.seen.clear()
        Store.queue.clear()
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "STORE_API": self.base, "STORE_APP_ID": "f71e664f03b68cfd", "STORE_BUILD_TOKEN": "secret"}
        self.env.pop("PIPELINE_TARGETS", None)

    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "release_targets.py"), *args], env=env or self.env, capture_output=True, text=True)


class Read(Base):
    def test_reads_six_switches_with_the_build_token(self):
        Store.answer = (200, json.dumps({"targets": {**ALL_ON, "android": False}}))
        with tempfile.TemporaryDirectory() as d:
            out, gh = Path(d) / "t.json", Path(d) / "gh-output"
            r = self.run_cli("read", str(out), "--github-output", env={**self.env, "GITHUB_OUTPUT": str(gh)})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text())["android"], False)
            self.assertIn("android=false\n", gh.read_text())
            self.assertIn("ios=true\n", gh.read_text())
        self.assertEqual(Store.seen, [("/api/build/f71e664f03b68cfd/targets", "Bearer secret")])

    def test_failed_or_malformed_reads_stop_the_run(self):
        answers = [(404, '{"error":"not found"}'), (401, '{"error":"unauthorised"}'), (409, '{"error":"Target is disabled."}'),
                   (500, "oops"), (200, "not json"), (200, "[]"), (200, '{"targets": null}'),
                   (200, json.dumps({"targets": {k: v for k, v in ALL_ON.items() if k != "tvos"}})),
                   (200, json.dumps({"targets": {**ALL_ON, "web": "true"}}))]
        for answer in answers:
            Store.answer = answer
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as d:
                r = self.run_cli("read", str(Path(d) / "t.json"))
                self.assertEqual(r.returncode, 1)
                self.assertFalse((Path(d) / "t.json").exists())

    def test_missing_credentials_stop_the_run(self):
        env = {k: v for k, v in self.env.items() if k != "STORE_BUILD_TOKEN"}
        self.assertEqual(self.run_cli("require", "macos", env=env).returncode, 1)
        env = {**self.env, "STORE_APP_ID": "../etc"}
        self.assertEqual(self.run_cli("require", "macos", env=env).returncode, 1)
        self.assertEqual(Store.seen, [])

    def test_redirects_are_refused(self):
        Store.answer = (302, "")
        self.assertEqual(self.run_cli("require", "web").returncode, 1)

    def test_check_writes_the_switch_for_one_target(self):
        Store.answer = (200, json.dumps({"targets": {**ALL_ON, "web": False}}))
        with tempfile.TemporaryDirectory() as d:
            gh = Path(d) / "gh-output"
            env = {**self.env, "GITHUB_OUTPUT": str(gh)}
            self.assertEqual(self.run_cli("check", "web", env=env).returncode, 0)
            self.assertEqual(self.run_cli("check", "ios", env=env).returncode, 0)
            self.assertEqual(gh.read_text(), "on=false\non=true\n")
            Store.answer = (404, "{}")
            self.assertEqual(self.run_cli("check", "ios", env=env).returncode, 1)
            self.assertEqual(gh.read_text(), "on=false\non=true\n")

    def test_require(self):
        Store.answer = (200, json.dumps({"targets": {**ALL_ON, "macos": False}}))
        self.assertEqual(self.run_cli("require", "tvos").returncode, 0)
        r = self.run_cli("require", "macos")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Target is disabled.", r.stderr)
        self.assertEqual(self.run_cli("require", "linux").returncode, 1)

    def test_pipeline_targets_switch_off_what_the_pipeline_does_not_build(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "t.json"
            r = self.run_cli("read", str(out), env={**self.env, "PIPELINE_TARGETS": "web android"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text()), {"ios": False, "android": True, "web": True, "macos": False, "windows": False, "tvos": False})
            self.assertEqual(self.run_cli("read", str(out), env={**self.env, "PIPELINE_TARGETS": "web linux"}).returncode, 1)
            self.assertEqual(self.run_cli("read", str(out), env={**self.env, "PIPELINE_TARGETS": "web, android"}).returncode, 1)

    def test_store_mode_none_makes_no_store_call_and_reads_the_pipeline_targets(self):
        env = {k: v for k, v in self.env.items() if k not in ("STORE_APP_ID", "STORE_BUILD_TOKEN", "STORE_API")}
        env.update({"STORE_MODE": "none", "PIPELINE_TARGETS": "web android"})
        with tempfile.TemporaryDirectory() as d:
            out, gh = Path(d) / "t.json", Path(d) / "gh-output"
            r = self.run_cli("read", str(out), "--github-output", env={**env, "GITHUB_OUTPUT": str(gh)})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text()), {"ios": False, "android": True, "web": True, "macos": False, "windows": False, "tvos": False})
            self.assertIn("web=true\n", gh.read_text())
            self.assertIn("ios=false\n", gh.read_text())
            self.assertEqual(self.run_cli("check", "web", env={**env, "GITHUB_OUTPUT": str(gh)}).returncode, 0)
            self.assertEqual(self.run_cli("require", "android", env=env).returncode, 0)
            r = self.run_cli("require", "macos", env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("Target is disabled.", r.stderr)
            r = self.run_cli("read", str(Path(d) / "u.json"), env={k: v for k, v in env.items() if k != "PIPELINE_TARGETS"})
            self.assertEqual(r.returncode, 1)
            self.assertIn("needs PIPELINE_TARGETS", r.stderr)
        self.assertEqual(Store.seen, [])

    def test_without_store_mode_missing_credentials_stop_the_run(self):
        env = {k: v for k, v in self.env.items() if k not in ("STORE_APP_ID", "STORE_BUILD_TOKEN")}
        self.assertEqual(self.run_cli("require", "web", env=env).returncode, 1)
        self.assertEqual(self.run_cli("require", "web", env={**env, "STORE_MODE": "other"}).returncode, 1)
        self.assertEqual(Store.seen, [])


class Merge(unittest.TestCase):
    NEW = {"version": "0.6.0", "install": {
        "pwa": "https://new.example/", "android": {"apk": "https://api.github.com/new/apk"},
        "ios": {"bundleId": "b", "testflight": "https://testflight.apple.com/join/NEW", "devices": ["iphone", "ipad"]},
        "tvos": PREVIOUS["install"]["tvos"], "macos": PREVIOUS["install"]["macos"]}}

    def test_all_on_takes_new_entries_and_keeps_windows(self):
        out = TARGETS.merge(self.NEW, PREVIOUS, ALL_ON)
        self.assertEqual(out["install"]["android"], self.NEW["install"]["android"])
        self.assertEqual(out["install"]["windows"], PREVIOUS["install"]["windows"])

    def test_switched_off_targets_keep_the_previous_entries(self):
        off = {**ALL_ON, "android": False, "web": False, "ios": False}
        out = TARGETS.merge(self.NEW, PREVIOUS, off)
        for field in ("android", "pwa", "ios", "windows"):
            self.assertEqual(out["install"][field], PREVIOUS["install"][field], field)
        self.assertEqual(out["version"], "0.6.0")

    def test_switched_off_without_a_previous_entry_writes_none(self):
        out = TARGETS.merge(self.NEW, {}, {**ALL_ON, "android": False, "web": False, "ios": False, "windows": False, "tvos": False})
        for field in ("android", "pwa", "ios"):
            self.assertIn(field, out["install"])
            self.assertIsNone(out["install"][field])
        self.assertNotIn("windows", out["install"])
        self.assertNotIn("tvos", out["install"])

    def test_packages_and_publishes(self):
        records = [{"platform": "macos", "name": "a.dmg"}, {"platform": "windows", "name": "b.exe"}]
        off = {t: False for t in ALL_ON}
        self.assertEqual(TARGETS.packages(records, {**off, "windows": True}), [records[1]])
        self.assertFalse(TARGETS.publishes(off, records))
        self.assertTrue(TARGETS.publishes({**off, "windows": True}, records))
        self.assertFalse(TARGETS.publishes({**off, "windows": True, "tvos": True}))
        self.assertTrue(TARGETS.publishes({**off, "web": True}))

    def test_a_new_windows_package_wins_when_switched_on(self):
        new = json.loads(json.dumps(self.NEW))
        new["install"]["windows"] = {"packages": [{"url": "https://api.github.com/new/exe"}]}
        self.assertEqual(TARGETS.merge(new, PREVIOUS, ALL_ON)["install"]["windows"], new["install"]["windows"])
        self.assertEqual(TARGETS.merge(new, PREVIOUS, {**ALL_ON, "windows": False})["install"]["windows"], PREVIOUS["install"]["windows"])

    def test_merge_command_reads_and_writes_files(self):
        with tempfile.TemporaryDirectory() as d:
            new, previous, targets, out = (Path(d) / n for n in ("new.json", "previous.json", "targets.json", "out.json"))
            new.write_text(json.dumps(self.NEW)); previous.write_text(json.dumps(PREVIOUS)); targets.write_text(json.dumps({**ALL_ON, "web": False}))
            self.assertEqual(TARGETS.main(["merge", str(new), str(previous), str(targets), str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["install"]["pwa"], PREVIOUS["install"]["pwa"])
            self.assertEqual(TARGETS.main(["merge", str(new), "-", str(targets), str(out)]), 0)


class Manifest(unittest.TestCase):
    INPUTS = {"version": "0.6.0", "iconUrl": "https://api.github.com/new/icon.png", "apkUrl": "https://api.github.com/new/app.apk",
              "webUrl": "https://demo.example/", "testflightUrl": "https://testflight.apple.com/join/NEW",
              "screenshots": ["https://api.github.com/new/screenshot-1.png"], "screenshotsByPlatform": {"ipad": ["https://api.github.com/new/ipad-1.png"], "tvos": []}}

    def test_full_manifest_follows_the_contract(self):
        manifest = app_manifest.build(APP, self.INPUTS)
        app_manifest.validate(manifest)
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual((manifest["minPlayers"], manifest["maxPlayers"]), (2, 9))
        self.assertEqual(manifest["install"]["pwa"], "https://demo.example/")
        self.assertEqual(manifest["install"]["android"], {"apk": "https://api.github.com/new/app.apk"})
        self.assertEqual(manifest["install"]["ios"], {"bundleId": "app.example.demo", "testflight": "https://testflight.apple.com/join/NEW", "devices": ["iphone", "ipad"]})
        self.assertEqual(manifest["install"]["tvos"], {"bundleId": "app.example.demo", "minOsVersion": "17.0"})
        self.assertEqual(manifest["install"]["macos"]["packages"], [])
        self.assertEqual(manifest["screenshotsByPlatform"], {"ipad": ["https://api.github.com/new/ipad-1.png"]})
        self.assertNotIn("windows", manifest["install"])

    def test_only_configured_targets_appear_and_empty_inputs_are_null(self):
        manifest = app_manifest.build({**APP, "targets": ["web", "android", "ios"]}, {**self.INPUTS, "apkUrl": "", "testflightUrl": ""})
        self.assertEqual(manifest["install"], {"pwa": "https://demo.example/", "android": None, "ios": None})
        self.assertNotIn("tvos", manifest["install"])

    def test_extra_cannot_replace_store_fields(self):
        with self.assertRaises(ValueError):
            app_manifest.build({**APP, "extra": {"install": {}}}, self.INPUTS)
        for bad in ({"id": "Has Space"}, {"name": ""}, {"description": 3}):
            with self.assertRaises(ValueError):
                app_manifest.build({**APP, **bad}, self.INPUTS)

    def test_validation_refuses_what_the_store_would(self):
        good = app_manifest.build(APP, self.INPUTS)
        def bad(change):
            manifest = json.loads(json.dumps(good))
            change(manifest)
            with self.assertRaises(ValueError):
                app_manifest.validate(manifest)
        bad(lambda m: m.update(schema=2))
        bad(lambda m: m.update(icon="http://insecure.example/i.png"))
        bad(lambda m: m["install"].update(pwa="http://insecure.example/"))
        bad(lambda m: m["install"].update(android={"apk": "http://x"}))
        bad(lambda m: m["install"].update(ios={"bundleId": "b", "testflight": None}))
        bad(lambda m: m["install"].update(wasm={}))
        bad(lambda m: m.update(install={"pwa": None, "android": None}))
        bad(lambda m: m.update(screenshotsByPlatform={"fridge": ["https://x.example/a.png"]}))
        bad(lambda m: m["install"].update(macos={"packages": [{"url": "https://x.example/a.dmg", "format": "dmg", "architectures": ["arm64"], "minOsVersion": "14.0", "signing": "unsigned", "notarization": "notarized"}]}))
        bad(lambda m: m["install"].update(windows={"packages": [{"url": "https://x.example/a.exe", "format": "exe", "architectures": ["x64", "x64"], "minOsVersion": "10.0", "signing": "signed"}]}))
        bad(lambda m: m["install"].update(windows={"packages": [{"url": "https://x.example/a.exe", "format": "exe", "architectures": ["x64"], "minOsVersion": "10.0", "signing": "signed", "notarization": "notarized"}]}))

    def test_command_line(self):
        with tempfile.TemporaryDirectory() as d:
            app, inputs, out = Path(d) / "app.json", Path(d) / "inputs.json", Path(d) / "out.json"
            app.write_text(json.dumps(APP)); inputs.write_text(json.dumps(self.INPUTS))
            self.assertEqual(subprocess.run([sys.executable, "-B", str(SCRIPTS / "app_manifest.py"), "build", str(app), str(inputs), str(out)]).returncode, 0)
            self.assertEqual(subprocess.run([sys.executable, "-B", str(SCRIPTS / "app_manifest.py"), "validate", str(out)]).returncode, 0)
            out.write_text("{}")
            self.assertNotEqual(subprocess.run([sys.executable, "-B", str(SCRIPTS / "app_manifest.py"), "validate", str(out)], capture_output=True).returncode, 0)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Packages(unittest.TestCase):
    def package(self, directory, kind="windows", **change):
        platform = kind
        extension = "dmg" if platform == "macos" else "exe"
        file = Path(directory) / f"demo-0.6.0.{extension}"
        file.write_bytes(b"installer bytes")
        metadata = {"platform": platform, "version": "0.6.0", "fileName": file.name, "format": extension,
                    "architectures": ["x64"] if platform == "windows" else ["arm64", "x64"], "minOsVersion": "10.0" if platform == "windows" else "14.0",
                    "signing": "signed", "sha256": sha256(file), "size": file.stat().st_size, "distributionReady": True,
                    **({"notarization": "notarized"} if platform == "macos" else {}), **change}
        meta = Path(directory) / f"{platform}.json"
        meta.write_text(json.dumps(metadata))
        records = Path(directory) / f"{platform}-records.json"
        records.write_text(json.dumps([{"file": str(file), "metadata": str(meta), "runtimeEvidence": "https://evidence.example/run", "storeEvidence": "https://evidence.example/store"}]))
        return records

    def test_verified_packages_become_install_entries(self):
        with tempfile.TemporaryDirectory() as d:
            records = platform_manifest.validate(self.package(d, "macos"), "0.6.0") + platform_manifest.validate(self.package(Path(d), "windows"), "0.6.0")
            entries = platform_manifest.install_entries(records, {"demo-0.6.0.dmg": "https://api.github.com/a.dmg", "demo-0.6.0.exe": "https://api.github.com/a.exe"})
            self.assertEqual(entries["macos"]["packages"][0]["notarization"], "notarized")
            self.assertEqual(entries["windows"]["packages"][0], {"url": "https://api.github.com/a.exe", "format": "exe", "architectures": ["x64"], "minOsVersion": "10.0", "signing": "signed"})
            with self.assertRaises(ValueError):
                platform_manifest.install_entries(records[:1], {"demo-0.6.0.dmg": "http://insecure.example/a.dmg"})

    def test_unverified_packages_are_refused(self):
        cases = [dict(signing="unsigned"), dict(distributionReady=False), dict(version="0.5.0"), dict(sha256="0" * 64), dict(size=1),
                 dict(architectures=["arm64"]), dict(minOsVersion="ten"), dict(format="msi"), dict(platform="linux")]
        for change in cases:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):
                    platform_manifest.validate(self.package(d, "windows", **change), "0.6.0")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                platform_manifest.validate(self.package(d, "macos", notarization="not-notarized"), "0.6.0")
        with tempfile.TemporaryDirectory() as d:
            records = self.package(d, "windows")
            data = json.loads(records.read_text()); data[0]["storeEvidence"] = "http://insecure.example"
            records.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                platform_manifest.validate(records, "0.6.0")
            records.write_text("[]")
            with self.assertRaises(ValueError):
                platform_manifest.validate(records, "0.6.0")

    def test_command_line_writes_files_and_failures_exit_with_a_message(self):
        with tempfile.TemporaryDirectory() as d:
            records = self.package(d, "windows")
            out = Path(d) / "verified.json"
            script = str(SCRIPTS / "platform_manifest.py")
            self.assertEqual(subprocess.run([sys.executable, "-B", script, "validate", str(records), "0.6.0", str(out)]).returncode, 0)
            self.assertEqual(json.loads(out.read_text())[0]["platform"], "windows")
            failed = subprocess.run([sys.executable, "-B", script, "validate", str(records), "9.9.9", str(out)], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("version/name mismatch", failed.stderr)


class Artifacts(unittest.TestCase):
    def export(self, directory, **change):
        options = {"method": "app-store-connect", "destination": "export", "teamID": "TEAM1", "signingStyle": "manual",
                   "signingCertificate": "Apple Distribution", "provisioningProfiles": {"app.example.demo": "Profile"}, **change}
        path = Path(directory) / "ExportOptions.plist"
        path.write_bytes(plistlib.dumps(options))
        return path

    def test_exports_must_be_local_app_store_exports_for_this_team_and_profile(self):
        with tempfile.TemporaryDirectory() as d:
            platform_artifacts.check_export("tvos", self.export(d), "TEAM1", "Profile", "app.example.demo")
            platform_artifacts.check_export("macos", self.export(d, installerSigningCertificate="Mac Installer Distribution"), "TEAM1", "Profile", "app.example.demo")
            for change in (dict(method="ad-hoc"), dict(destination="upload"), dict(teamID="OTHER"), dict(provisioningProfiles={"app.example.demo": "Wrong"}),
                           dict(provisioningProfiles={"other.bundle": "Profile"}), dict(signingCertificate="Apple Development")):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    platform_artifacts.check_export("tvos", self.export(d, **change), "TEAM1", "Profile", "app.example.demo")
            with self.assertRaises(ValueError):
                platform_artifacts.check_export("macos", self.export(d), "TEAM1", "Profile", "app.example.demo")
            with self.assertRaises(ValueError):
                platform_artifacts.check_export("macos", self.export(d, installerSigningCertificate="not-a-fingerprint"), "TEAM1", "Profile", "app.example.demo")

    def test_installer_names(self):
        self.assertTrue(platform_artifacts.installer_name("Mac Installer Distribution"))
        self.assertFalse(platform_artifacts.installer_name("Mac Installer Distribution", allow_selector=False))
        self.assertTrue(platform_artifacts.installer_name("Mac Installer Distribution: Example Ltd (TEAM1)", allow_selector=False))
        self.assertTrue(platform_artifacts.installer_name("3rd Party Mac Developer Installer: Example"))
        self.assertFalse(platform_artifacts.installer_name("Mac Installer Distribution:   "))
        self.assertFalse(platform_artifacts.installer_name(None))

    def test_dmg_metadata_gets_name_size_and_hash(self):
        with tempfile.TemporaryDirectory() as d:
            package, meta, out = Path(d) / "demo.dmg", Path(d) / "meta.json", Path(d) / "out.json"
            package.write_bytes(b"dmg bytes"); meta.write_text(json.dumps({"platform": "macos", "version": "0.6.0"}))
            platform_artifacts.record_package(meta, package, out)
            data = json.loads(out.read_text())
            self.assertEqual((data["fileName"], data["format"], data["size"], data["sha256"]), ("demo.dmg", "dmg", 9, sha256(package)))

    def windows_tree(self, directory, machine=0x8664, magic=b"MZ", **evidence):
        desktop = Path(directory) / "desktop"
        (desktop / "artifacts/win-unpacked").mkdir(parents=True)
        info = {"version": "0.6.0", "architecture": "x64", "minimumWindows": "10", "electron": "45.0.0", **evidence}
        (desktop / "artifacts/package-evidence.json").write_text(json.dumps(info))
        (desktop / "package.json").write_text(json.dumps({"devDependencies": {"electron": "45.0.0"}}))
        header = bytearray(magic + b"\0" * 62)
        header += struct.pack("<I", 0x40) if False else b""
        header[0x3C:0x40] = struct.pack("<I", 0x80)
        exe = bytes(header) + b"\0" * (0x80 - len(header)) + b"PE\x00\x00" + struct.pack("<H", machine)
        (desktop / "artifacts/win-unpacked/Demo.exe").write_bytes(exe)
        (desktop / "artifacts/demo-0.6.0-windows-x64-unsigned.exe").write_bytes(b"installer")
        return desktop

    def test_windows_evidence_is_recorded_after_reading_the_executable_header(self):
        with tempfile.TemporaryDirectory() as d:
            desktop = self.windows_tree(d)
            out = Path(d) / "out"; out.mkdir()
            platform_artifacts.record_windows(desktop, out, "demo", "Demo.exe")
            data = json.loads((out / "package-metadata.json").read_text())
            self.assertEqual((data["fileName"], data["architectures"], data["signing"], data["distributionReady"]), ("demo-0.6.0-windows-x64-unsigned.exe", ["x64"], "unsigned", False))
            self.assertEqual(data["sha256"], sha256(out / data["fileName"]))

    def test_windows_evidence_refuses_the_wrong_machine_header_or_configuration(self):
        for kwargs in (dict(machine=0xAA64), dict(magic=b"ZM"), dict(architecture="arm64"), dict(minimumWindows="7"), dict(electron="44.0.0")):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as d:
                desktop = self.windows_tree(d, **kwargs)
                out = Path(d) / "out"; out.mkdir()
                with self.assertRaises(ValueError):
                    platform_artifacts.record_windows(desktop, out, "demo", "Demo.exe")

    def fake_apple_tools(self, directory, archs="arm64 x86_64", signed=True):
        bin = Path(directory) / "bin"
        bin.mkdir()
        (bin / "xcrun").write_text(f'#!/bin/sh\necho "{archs}"\n'); (bin / "xcrun").chmod(0o755)
        (bin / "codesign").write_text('#!/bin/sh\nif [ "$1" = "-dv" ]; then echo "Authority=Apple Distribution" >&2; fi\n' + ('' if signed else 'if [ "$1" = "--verify" ]; then exit 1; fi\n')); (bin / "codesign").chmod(0o755)
        return bin

    def archive(self, directory, platform="macos", **info):
        app = Path(directory) / "x.xcarchive/Products/Applications" / ("Demo.app" if platform == "macos" else "DemoTV.app")
        contents = app / "Contents" if platform == "macos" else app
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        resources = contents / "Resources" if platform == "macos" else app
        (resources / "Art").mkdir(parents=True, exist_ok=True)
        (resources / "Art/a.png").write_bytes(b"png"); (resources / "Assets.car").write_bytes(b"car")
        plist = {"CFBundleExecutable": "Demo", "CFBundleShortVersionString": "0.6.0", "CFBundleVersion": "45", "CFBundleIdentifier": "app.example.demo",
                 "CFBundleSupportedPlatforms": ["MacOSX"] if platform == "macos" else ["AppleTVOS"],
                 **({"LSMinimumSystemVersion": "14.0", "LSApplicationCategoryType": "public.app-category.games"} if platform == "macos" else {"MinimumOSVersion": "17.0"}), **info}
        (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
        executable = contents / "MacOS/Demo" if platform == "macos" else app / "Demo"
        executable.write_bytes(b"binary")
        return Path(directory) / "x.xcarchive"

    def inspect(self, platform, directory, archs, **info):
        bin = self.fake_apple_tools(directory, archs)
        archive = self.archive(directory, platform, **info)
        out = Path(directory) / "evidence.json"
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bin}:{old}"
        try:
            platform_artifacts.inspect_apple(platform, archive, "0.6.0", "45", out, "app.example.demo", "Demo.app" if platform == "macos" else "DemoTV.app", "public.app-category.games")
        finally:
            os.environ["PATH"] = old
        return json.loads(out.read_text())

    def test_apple_archives_are_inspected_with_fake_tools(self):
        with tempfile.TemporaryDirectory() as d:
            mac = self.inspect("macos", d, "arm64 x86_64")
            self.assertEqual((mac["architectures"], mac["signing"], mac["minOsVersion"], mac["distributionReady"]), (["arm64", "x64"], "signed", "14.0", False))
        with tempfile.TemporaryDirectory() as d:
            tv = self.inspect("tvos", d, "arm64")
            self.assertEqual((tv["platform"], tv["architectures"], tv["minOsVersion"]), ("tvos", ["arm64"], "17.0"))

    def test_apple_archive_mismatches_are_refused(self):
        cases = [("macos", "arm64", {}), ("tvos", "arm64 x86_64", {}), ("macos", "arm64 x86_64", {"CFBundleVersion": "46"}),
                 ("macos", "arm64 x86_64", {"CFBundleIdentifier": "other.bundle"}), ("macos", "arm64 x86_64", {"LSApplicationCategoryType": "public.app-category.utilities"}),
                 ("macos", "arm64 x86_64", {"CFBundleSupportedPlatforms": ["iPhoneOS"]}), ("macos", "arm64 x86_64", {"LSMinimumSystemVersion": ""})]
        for platform, archs, info in cases:
            if (platform, archs, info) == ("macos", "arm64", {}):
                continue
            with self.subTest(platform=platform, archs=archs, info=info), tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ValueError):
                    self.inspect(platform, d, archs, **info)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                self.inspect("macos", d, "arm64")


class Publish(Base):
    def publish(self, apk, targets=None, answer=None, latest="release-0.5.0-37", queue=(), env_extra=None, changelog=None, screenshots=1):
        Store.answer = answer or (200, json.dumps({"targets": targets or ALL_ON}))
        Store.queue[:] = list(queue)
        d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (d / "bin").mkdir()
        (d / "bin/gh").write_text(FAKE_GH)
        (d / "bin/gh").chmod(0o755)
        (d / "previous.json").write_text(json.dumps(PREVIOUS))
        (d / "icon.png").write_bytes(b"icon")
        for i in range(screenshots):
            (d / f"shot{i}.png").write_bytes(b"shot")
        (d / "ipad").mkdir()
        (d / "ipad/table.png").write_bytes(b"ipad")
        shutil.copy(changelog or FIXTURES / "CHANGELOG.md", d / "CHANGELOG.md")
        shutil.copy(FIXTURES / "app.json", d / "app.json")
        if apk != "-":
            (d / "app.apk").write_bytes(b"apk")
            apk = str(d / "app.apk")
        env = {**self.env, "PATH": f"{d / 'bin'}:{os.environ['PATH']}", "FAKE_OUT": str(d), "FAKE_LATEST": latest,
               "FAKE_PREVIOUS": str(d / "previous.json"), "GITHUB_REPOSITORY": "example/demo",
               "WEB_URL": "https://new.example/", "TESTFLIGHT_URL": "https://testflight.apple.com/join/NEW", **(env_extra or {})}
        command = [sys.executable, "-B", str(SCRIPTS / "publish_release.py"), "--app", str(d / "app.json"), "--version", "0.6.0", "--build", "38",
                   "--apk", apk, "--icon", str(d / "icon.png"), "--changelog", str(d / "CHANGELOG.md"), "--ipad-screenshots", str(d / "ipad")]
        for i in range(screenshots):
            command += ["--screenshot", str(d / f"shot{i}.png")]
        r = subprocess.run(command, cwd=d, env=env, capture_output=True, text=True)
        manifest = d / "uploads/my-app.json"
        return r, (json.loads(manifest.read_text()) if manifest.exists() else None), d

    @staticmethod
    def calls(d):
        f = d / "calls.txt"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def test_switched_on_targets_publish_and_windows_is_kept(self):
        r, manifest, d = self.publish(apk="apk")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(manifest["install"]["android"], {"apk": "https://api.github.com/new/app.apk"})
        self.assertEqual(manifest["install"]["pwa"], "https://new.example/")
        self.assertEqual(manifest["install"]["ios"]["testflight"], "https://testflight.apple.com/join/NEW")
        self.assertEqual(manifest["install"]["windows"], PREVIOUS["install"]["windows"])
        self.assertEqual(manifest["screenshots"], ["https://api.github.com/new/screenshot-1.png"])
        self.assertEqual(manifest["screenshotsByPlatform"], {"ipad": ["https://api.github.com/new/ipad-table.png"]})
        self.assertEqual((manifest["minPlayers"], manifest["maxPlayers"]), (2, 9))
        self.assertEqual((d / "published").read_text(), "release-0.6.0-38")
        create = next(c for c in self.calls(d) if c[:3] == ["api", "-X", "POST"])
        self.assertIn("body=### Added\n- Something new.", create)
        self.assertIn("name=Demo 0.6.0 (build 38)", create)
        self.assertEqual(self.calls(d)[-1][:2], ["release", "edit"])
        self.assertEqual(sorted(p.name for p in (d / "uploads").iterdir()), ["CHANGELOG.md", "app.apk", "icon.png", "ipad-table.png", "my-app.json", "screenshot-1.png"])

    def test_the_manifest_is_the_last_upload_before_the_draft_goes_public(self):
        r, _, d = self.publish(apk="apk")
        self.assertEqual(r.returncode, 0, r.stderr)
        writes = [c for c in self.calls(d) if c[:2] in (["release", "upload"], ["release", "edit"])]
        self.assertTrue(writes[-2][5].endswith("my-app.json"))
        self.assertEqual(writes[-1][:2], ["release", "edit"])
        self.assertNotIn("my-app.json", " ".join(" ".join(c) for c in writes[:-2]))

    def test_web_bundle_is_uploaded_when_there_is_no_web_address(self):
        d0 = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bundle = d0 / "web.zip"; bundle.write_bytes(b"zip")
        r, manifest, d = self.publish(apk="-", env_extra={"WEB_URL": "", "WEB_ZIP": str(bundle)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(manifest["install"]["pwa"], "https://api.github.com/new/web.zip")
        self.assertTrue((d / "uploads/web.zip").exists())

    def test_android_off_keeps_the_last_apk_while_siblings_update(self):
        r, manifest, d = self.publish(apk="-", targets={**ALL_ON, "android": False})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(manifest["install"]["android"], PREVIOUS["install"]["android"])
        self.assertEqual(manifest["install"]["pwa"], "https://new.example/")
        self.assertFalse((d / "uploads/app.apk").exists())

    def test_android_off_uploads_no_apk_even_when_given_one(self):
        r, manifest, d = self.publish(apk="apk", targets={**ALL_ON, "android": False})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((d / "uploads/app.apk").exists())
        self.assertEqual(manifest["install"]["android"], PREVIOUS["install"]["android"])

    def test_switched_off_during_the_run_keeps_the_previous_entry(self):
        r, manifest, _ = self.publish(apk="apk", queue=[(200, json.dumps({"targets": ALL_ON}))], answer=(200, json.dumps({"targets": {**ALL_ON, "ios": False, "web": False}})))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(manifest["install"]["ios"], PREVIOUS["install"]["ios"])
        self.assertEqual(manifest["install"]["pwa"], PREVIOUS["install"]["pwa"])

    def test_failed_switch_read_makes_no_github_call(self):
        for answer in [(404, '{"error":"not found"}'), (401, '{"error":"unauthorised"}'), (200, '{"targets": {}}')]:
            with self.subTest(answer=answer):
                r, manifest, d = self.publish(apk="apk", answer=answer)
                self.assertNotEqual(r.returncode, 0)
                self.assertIsNone(manifest)
                self.assertEqual(self.calls(d), [])

    def test_everything_switched_off_makes_no_github_call(self):
        r, manifest, d = self.publish(apk="apk", targets={t: False for t in ALL_ON})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing is published", r.stdout)
        self.assertEqual(self.calls(d), [])

    def test_only_mac_and_windows_on_without_packages_makes_no_github_call(self):
        r, _, d = self.publish(apk="apk", targets={**{t: False for t in ALL_ON}, "macos": True, "windows": True})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(d), [])

    def test_everything_switched_off_during_the_run_removes_the_draft(self):
        r, manifest, d = self.publish(apk="apk", queue=[(200, json.dumps({"targets": ALL_ON}))], answer=(200, json.dumps({"targets": {t: False for t in ALL_ON}})))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(manifest)
        self.assertFalse((d / "published").exists())
        self.assertEqual((d / "deleted").read_text(), "release-0.6.0-38")

    def test_failed_read_before_the_manifest_publishes_nothing(self):
        r, manifest, d = self.publish(apk="apk", queue=[(200, json.dumps({"targets": ALL_ON}))], answer=(500, "oops"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIsNone(manifest)
        self.assertFalse((d / "published").exists())

    def test_first_release_names_switched_off_required_entries_as_null(self):
        r, manifest, _ = self.publish(apk="-", targets={**ALL_ON, "android": False, "ios": False}, latest="", env_extra={"TESTFLIGHT_URL": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(manifest["install"]["android"])
        self.assertIsNone(manifest["install"]["ios"])
        self.assertEqual(manifest["install"]["pwa"], "https://new.example/")
        self.assertNotIn("windows", manifest["install"])

    def test_a_changelog_without_the_version_stops_before_any_call(self):
        d0 = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (d0 / "CHANGELOG.md").write_text("# Changelog\n\n## [0.4.0]\n- Old.\n")
        r, manifest, d = self.publish(apk="apk", changelog=d0 / "CHANGELOG.md")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no section for 0.6.0", r.stderr)
        self.assertEqual(self.calls(d), [])
        self.assertEqual(Store.seen, [])

    def test_verified_packages_are_uploaded_and_listed_when_their_target_is_on(self):
        d0 = Path(self.enterContext(tempfile.TemporaryDirectory()))
        records = Packages().package(d0, "windows")
        r, manifest, d = self.publish(apk="-", env_extra={"PLATFORM_PACKAGES_JSON": str(records)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(manifest["install"]["windows"]["packages"], [{"url": "https://api.github.com/new/demo-0.6.0.exe", "format": "exe", "architectures": ["x64"], "minOsVersion": "10.0", "signing": "signed"}])
        self.assertTrue((d / "uploads/demo-0.6.0.exe").exists())
        r, manifest, d = self.publish(apk="-", targets={**ALL_ON, "windows": False}, env_extra={"PLATFORM_PACKAGES_JSON": str(records)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(manifest["install"]["windows"], PREVIOUS["install"]["windows"])
        self.assertFalse((d / "uploads/demo-0.6.0.exe").exists())

    def test_unverified_packages_stop_the_run_before_any_call(self):
        d0 = Path(self.enterContext(tempfile.TemporaryDirectory()))
        records = Packages().package(d0, "windows", signing="unsigned")
        r, manifest, d = self.publish(apk="-", env_extra={"PLATFORM_PACKAGES_JSON": str(records)})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls(d), [])

    def test_a_pipeline_that_builds_only_web_ignores_the_other_switches(self):
        r, manifest, _ = self.publish(apk="-", env_extra={"PIPELINE_TARGETS": "web"}, latest="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(manifest["install"]["pwa"], "https://new.example/")
        self.assertIsNone(manifest["install"]["android"])
        self.assertIsNone(manifest["install"]["ios"])

    def test_without_store_credentials_publishes_the_pipeline_targets_with_no_store_call(self):
        env_extra = {"STORE_MODE": "none", "PIPELINE_TARGETS": "web android"}
        r, manifest, d = self.publish(apk="apk", env_extra=env_extra, answer=(500, "store must not be called"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Store.seen, [])
        self.assertEqual(manifest["install"]["android"], {"apk": "https://api.github.com/new/app.apk"})
        self.assertEqual(manifest["install"]["pwa"], "https://new.example/")
        self.assertEqual(manifest["install"]["ios"], PREVIOUS["install"]["ios"])
        self.assertEqual((d / "published").read_text(), "release-0.6.0-38")

    def test_without_store_credentials_nothing_in_the_pipeline_means_nothing_published(self):
        r, manifest, d = self.publish(apk="-", env_extra={"STORE_MODE": "none", "PIPELINE_TARGETS": "macos"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(d), [])
        self.assertEqual(Store.seen, [])

    def test_bad_arguments(self):
        r = subprocess.run([sys.executable, "-B", str(SCRIPTS / "publish_release.py"), "--app", "x", "--version", "1.2", "--build", "1", "--icon", "i"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
