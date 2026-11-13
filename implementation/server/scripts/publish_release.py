#!/usr/bin/env python3
"""Creates a GitHub Release with the icon, screenshots, changelog, the APK (<application id>.apk), the web bundle and the
application manifest (my-app.json). Asset addresses inside the manifest are the api.github.com asset URLs.

  publish_release.py --app release/app.json --version 1.2.3 --build 45 --apk <path|-> --icon art/icon.png \\
      [--tag v1.2.3] [--attach file ...] [--screenshot p.png ...] [--ipad-screenshots dir] [--changelog CHANGELOG.md]

--tag is the release's tag (default release-<version>-<build>; an existing tag, such as the pushed v1.2.3, is used as it
is). --attach uploads a file the manifest does not declare, for example an unsigned Windows installer kept as evidence.

Environment: STORE_APP_ID, STORE_BUILD_TOKEN, STORE_API (see release_targets.py); GITHUB_REPOSITORY; gh signed in
(GH_TOKEN). Optional: WEB_URL (the web app's own address) or WEB_ZIP (a bundle the store serves instead),
TESTFLIGHT_URL (the build went to TestFlight in this run), PLATFORM_PACKAGES_JSON (verified Mac and Windows
packages). The store's target switches are read before anything is written and again before the manifest goes up:
a switched-off target keeps the latest release's entry, and a failed read leaves GitHub untouched.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import app_manifest  # noqa: E402
import platform_manifest  # noqa: E402
import release_targets  # noqa: E402


class Release:
    def __init__(self, args):
        self.args = args
        self.repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("GITHUB_REPOSITORY must be owner/name")
        self.app = json.loads(Path(args.app).read_text())
        self.tag = args.tag or f"release-{args.version}-{args.build}"
        self.tmp = Path(tempfile.mkdtemp(prefix="publish-release-"))
        self.release_id = ""

    def gh(self, *arguments, check=True):
        result = subprocess.run(["gh", *arguments], capture_output=True, text=True)
        if check and result.returncode != 0:
            sys.stderr.write(result.stderr)
            raise ValueError(f"gh {' '.join(arguments[:2])} failed")
        return result

    def upload(self, path):
        self.gh("release", "upload", self.tag, "--repo", self.repo, str(path), "--clobber")

    def asset_url(self, name):
        release = json.loads(self.gh("api", f"repos/{self.repo}/releases/{self.release_id}").stdout)
        for asset in release.get("assets", []):
            if asset.get("name") == name:
                return asset["url"]
        raise ValueError(f"asset {name} is not on the release")

    def notes(self):
        """This version's section of the changelog; no section, no release."""
        found, lines = False, []
        for line in Path(self.args.changelog).read_text().splitlines():
            if line.startswith("## ["):
                if found:
                    break
                found = line.startswith(f"## [{self.args.version}]")
                continue
            if found:
                lines.append(line)
        text = "\n".join(lines).strip("\n")
        if not text.strip():
            raise ValueError(f"{self.args.changelog} has no section for {self.args.version}")
        return text

    def read_targets(self):
        path = self.tmp / "targets.json"
        path.write_text(json.dumps(release_targets.read()))
        return path, json.loads(path.read_text())

    def run(self):
        args = self.args
        notes = self.notes()
        targets_path, targets = self.read_targets()
        packages_json = os.environ.get("PLATFORM_PACKAGES_JSON", "")
        platform_packages = []
        if packages_json:
            verified = platform_manifest.validate(packages_json, args.version)
            platform_packages = release_targets.packages(verified, targets)
        if not release_targets.publishes(targets, platform_packages):
            print("Nothing in this release is switched on in the store; nothing is published.")
            return 0
        apk = args.apk if targets["android"] else "-"
        web_url = os.environ.get("WEB_URL", "") if targets["web"] else ""
        web_zip = os.environ.get("WEB_ZIP", "") if targets["web"] else ""
        testflight = os.environ.get("TESTFLIGHT_URL", "") if targets["ios"] else ""

        # A draft until every asset is up, the manifest last, so the store never sees a latest release without it.
        created = self.gh("api", "-X", "POST", f"repos/{self.repo}/releases", "-f", f"tag_name={self.tag}",
                          "-f", f"name={self.app['name']} {args.version} (build {args.build})", "-f", f"body={notes}", "-F", "draft=true")
        self.release_id = str(json.loads(created.stdout)["id"]) if created.stdout.strip().startswith("{") else created.stdout.strip()
        if not self.release_id:
            raise ValueError(f"No release id for {self.tag}")
        self.upload(args.changelog)
        icon = self.tmp / "icon.png"
        shutil.copy(args.icon, icon)
        self.upload(icon)
        apk_name = f"{self.app['id']}.apk"
        if apk != "-":
            target = self.tmp / apk_name
            shutil.copy(apk, target)
            self.upload(target)
        for extra in args.attach:
            self.upload(Path(extra))
        shot_names = []
        for index, shot in enumerate(args.screenshot):
            name = f"screenshot-{index + 1}.png"
            shutil.copy(shot, self.tmp / name)
            shot_names.append(name)
            self.upload(self.tmp / name)
        if not web_url and web_zip:
            shutil.copy(web_zip, self.tmp / "web.zip")
            self.upload(self.tmp / "web.zip")
            web_url = self.asset_url("web.zip")

        ipad_urls = []
        if args.ipad_screenshots and Path(args.ipad_screenshots).is_dir():
            for shot in sorted(Path(args.ipad_screenshots).glob("*.png")):
                name = f"ipad-{shot.name}"
                shutil.copy(shot, self.tmp / name)
                self.upload(self.tmp / name)
                ipad_urls.append(self.asset_url(name))

        manifest = app_manifest.build(self.app, {
            "version": args.version, "iconUrl": self.asset_url("icon.png"),
            "apkUrl": self.asset_url(apk_name) if apk != "-" else "", "webUrl": web_url, "testflightUrl": testflight,
            "screenshots": [self.asset_url(name) for name in shot_names], "screenshotsByPlatform": {"ipad": ipad_urls}})
        if platform_packages:
            urls = {}
            for record in platform_packages:
                self.upload(record["file"])
                urls[record["name"]] = self.asset_url(record["name"])
            for platform, entry in platform_manifest.install_entries(platform_packages, urls).items():
                manifest["install"].setdefault(platform, {})
                manifest["install"][platform] = {**(manifest["install"][platform] or {}), **entry}

        # Read again just before the manifest; everything switched off since means the draft goes.
        targets_path, targets = self.read_targets()
        if not release_targets.publishes(targets, platform_packages):
            self.gh("release", "delete", self.tag, "--repo", self.repo, "--yes")
            return 0
        previous = {}
        latest = self.gh("api", f"repos/{self.repo}/releases/latest", check=False)
        if latest.returncode == 0:
            self.gh("release", "download", json.loads(latest.stdout)["tag_name"], "--repo", self.repo, "--pattern", "my-app.json", "--dir", str(self.tmp / "previous"))
            previous = json.loads((self.tmp / "previous/my-app.json").read_text())
        elif "HTTP 404" not in latest.stderr:
            sys.stderr.write(latest.stderr)
            raise ValueError("Could not read the latest release")
        merged = release_targets.merge(manifest, previous, targets)
        app_manifest.validate(merged)
        final = self.tmp / "my-app.json"
        final.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        self.upload(final)
        self.gh("release", "edit", self.tag, "--repo", self.repo, "--draft=false", "--latest")
        print(f"published {self.tag}")
        print(f"testflight: {testflight or 'none'}")
        print(f"web: {web_url or 'none'}")
        return 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--apk", default="-")
    parser.add_argument("--icon", required=True)
    parser.add_argument("--screenshot", action="append", default=[])
    parser.add_argument("--ipad-screenshots", default="")
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--tag", default="")
    parser.add_argument("--attach", action="append", default=[])
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version) or not re.fullmatch(r"[1-9]\d*", args.build):
        print("publish-release: version must be x.y.z and build a positive number", file=sys.stderr)
        return 2
    if args.tag and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", args.tag):
        print("publish-release: the tag must be letters, digits, dots, hyphens and underscores", file=sys.stderr)
        return 2
    for extra in args.attach:
        if not Path(extra).is_file() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", Path(extra).name) or Path(extra).name == "my-app.json":
            print(f"publish-release: cannot attach {extra}", file=sys.stderr)
            return 2
    try:
        return Release(args).run()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"publish-release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
