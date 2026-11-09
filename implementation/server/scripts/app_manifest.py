#!/usr/bin/env python3
"""The application manifest (my-app.json, schema 1) for one release.

  build <app.json> <inputs.json> <out.json>   write the manifest, validated
  validate <manifest.json>                    check a manifest against the contract

app.json (fixed per application):
  {"id": slug, "name": text, "description": text, "bundleId": "com.example.app" (Apple targets),
   "targets": ["web","android","ios","macos","tvos","windows"], "iosDevices": ["iphone","ipad"],
   "macos": {"minOsVersion": "14.0", "architectures": ["arm64","x64"]}, "tvos": {"minOsVersion": "17.0"},
   "extra": {...}  fields copied into the manifest as they are, for example minPlayers and maxPlayers}
inputs.json (per release):
  {"version": text, "iconUrl": https, "apkUrl": https|"", "webUrl": https|"", "testflightUrl": https|"",
   "screenshots": [https], "screenshotsByPlatform": {"ipad": [https]}}
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = 1
PLATFORMS = ("pwa", "android", "ios", "macos", "windows", "tvos")
SCREENSHOT_KEYS = ("iphone", "ipad", "android", "web", "macos", "windows", "tvos")
# The store's own fields; app.json "extra" may not replace them.
RESERVED = {"schema", "id", "name", "version", "description", "icon", "screenshots", "screenshotsByPlatform", "install"}


def https(value):
    parsed = urlparse(value if isinstance(value, str) else "")
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def build(app, inputs):
    if not isinstance(app.get("id"), str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", app["id"]):
        raise ValueError("app id must be a lowercase slug")
    for key in ("name", "description"):
        if not isinstance(app.get(key), str) or not app[key].strip():
            raise ValueError(f"app {key} is required")
    targets = app.get("targets", [])
    extra = app.get("extra", {})
    if set(extra) & RESERVED:
        raise ValueError(f"extra may not set {sorted(set(extra) & RESERVED)}")
    install = {}
    web = inputs.get("webUrl") or ""
    apk = inputs.get("apkUrl") or ""
    testflight = inputs.get("testflightUrl") or ""
    if "web" in targets:
        install["pwa"] = web or None
    if "android" in targets:
        install["android"] = {"apk": apk} if apk else None
    if "ios" in targets:
        install["ios"] = ({"bundleId": app["bundleId"], "testflight": testflight, "devices": app.get("iosDevices", ["iphone", "ipad"])}
                          if testflight else None)
    if "tvos" in targets:
        install["tvos"] = {"bundleId": app["bundleId"], "minOsVersion": app.get("tvos", {}).get("minOsVersion", "17.0")}
    if "macos" in targets:
        mac = app.get("macos", {})
        install["macos"] = {"packages": [], "testflight": {"bundleId": app["bundleId"], "minOsVersion": mac.get("minOsVersion", "14.0"),
                                                          "architectures": mac.get("architectures", ["arm64", "x64"])}}
    manifest = {"schema": SCHEMA, "id": app["id"], "name": app["name"], "version": inputs["version"], "description": app["description"],
                "icon": inputs.get("iconUrl", ""), **extra, "screenshots": list(inputs.get("screenshots", [])), "install": install}
    by_platform = {key: list(value) for key, value in inputs.get("screenshotsByPlatform", {}).items() if value}
    if by_platform:
        manifest["screenshotsByPlatform"] = by_platform
    return manifest


def validate(manifest):
    """The checks the store applies, so a bad manifest is refused before anything is uploaded."""
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("schema must be 1")
    if not isinstance(manifest.get("id"), str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", manifest["id"]):
        raise ValueError("id must be a lowercase slug")
    for key in ("name", "version", "description"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise ValueError(f"{key} is required")
    if not https(manifest.get("icon")):
        raise ValueError("icon must be an https address")
    for url in manifest.get("screenshots", []):
        if not https(url):
            raise ValueError("screenshots must be https addresses")
    for key, urls in manifest.get("screenshotsByPlatform", {}).items():
        if key not in SCREENSHOT_KEYS or any(not https(url) for url in urls):
            raise ValueError("screenshotsByPlatform has an unknown key or a non-https address")
    install = manifest.get("install")
    if not isinstance(install, dict) or set(install) - set(PLATFORMS):
        raise ValueError("install names an unknown platform")
    if all(install.get(platform) is None for platform in PLATFORMS):
        raise ValueError("at least one install entry is required")
    if install.get("pwa") is not None and not https(install["pwa"]):
        raise ValueError("pwa must be an https address")
    android = install.get("android")
    if android is not None and not https(android.get("apk")):
        raise ValueError("android apk must be an https address")
    ios = install.get("ios")
    if ios is not None and (not ios.get("bundleId") or not https(ios.get("testflight"))):
        raise ValueError("ios needs bundleId and an https testflight link")
    for platform, extension in (("macos", "dmg"), ("windows", "exe")):
        entry = install.get(platform)
        if entry is None:
            continue
        for package in entry.get("packages", []):
            if not https(package.get("url")) or package.get("format") != extension:
                raise ValueError(f"{platform} package needs an https url and format {extension}")
            if package.get("signing") not in ("unsigned", "signed"):
                raise ValueError(f"{platform} package signing must be unsigned or signed")
            if platform == "macos" and package.get("notarization") not in ("notarized", "not-notarized"):
                raise ValueError("macos package needs notarization")
            if platform == "macos" and package.get("signing") == "unsigned" and package.get("notarization") == "notarized":
                raise ValueError("an unsigned Mac package cannot claim notarization")
            if platform == "windows" and "notarization" in package:
                raise ValueError("windows packages omit notarization")
            archs = package.get("architectures")
            if not isinstance(archs, list) or not archs or len(set(archs)) != len(archs) or any(a not in ("arm64", "x64") for a in archs):
                raise ValueError(f"{platform} package architectures are invalid")
            if not isinstance(package.get("minOsVersion"), str):
                raise ValueError(f"{platform} package needs minOsVersion")
    return manifest


if __name__ == "__main__":
    try:
        if sys.argv[1] == "build" and len(sys.argv) == 5:
            result = build(json.loads(Path(sys.argv[2]).read_text()), json.loads(Path(sys.argv[3]).read_text()))
            Path(sys.argv[4]).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        elif sys.argv[1] == "validate" and len(sys.argv) == 3:
            validate(json.loads(Path(sys.argv[2]).read_text()))
        else:
            raise ValueError(__doc__)
    except (ValueError, KeyError, OSError, TypeError, IndexError) as error:
        sys.exit(f"app-manifest: {error}")
