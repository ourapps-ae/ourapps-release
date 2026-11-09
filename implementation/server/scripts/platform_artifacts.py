#!/usr/bin/env python3
"""Inspect platform build products and record verified packages.

  inspect-apple <macos|tvos> <archive> <version> <build> <out.json> <bundle-id> <app-name> [category]
      read an Xcode archive's application: architectures, versions, bundle identifier, platform, resources, signature
  check-export <macos|tvos|ios> <ExportOptions.plist> <team> <profile> <bundle-id> [installer-metadata.json]
      refuse an export that is not a local App Store Connect export signed for this team and profile
  installer-certificate <certificate.cer> <team>
  record-package <metadata.json> <package.dmg> <out.json>
  record-windows <desktop-dir> <out-dir> <file-prefix> <executable-name>
      copy the unsigned Windows installer and write its evidence after reading the executable header
"""
import hashlib
import json
import plistlib
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_apple(platform, archive, version, build, output, bundle_id, app_name, category=""):
    archive = Path(archive)
    app = archive / "Products/Applications" / app_name
    contents = app / "Contents" if platform == "macos" else app
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    executable = contents / "MacOS" / info["CFBundleExecutable"] if platform == "macos" else app / info["CFBundleExecutable"]
    architectures = command("xcrun", "lipo", "-archs", str(executable)).split()
    required = {"arm64", "x86_64"} if platform == "macos" else {"arm64"}
    if set(architectures) != required:
        raise ValueError(f"Expected {sorted(required)}, found {architectures}")
    if info["CFBundleShortVersionString"] != version or str(info["CFBundleVersion"]) != build:
        raise ValueError("Archive version/build mismatch")
    if info["CFBundleIdentifier"] != bundle_id:
        raise ValueError("Unexpected bundle identifier")
    if platform == "macos" and category and info.get("LSApplicationCategoryType") != category:
        raise ValueError(f"Mac archive category must be {category}")
    expected_platform = "MacOSX" if platform == "macos" else "AppleTVOS"
    if info.get("CFBundleSupportedPlatforms") != [expected_platform]:
        raise ValueError("Archive contains the wrong platform")
    minimum = info.get("LSMinimumSystemVersion") if platform == "macos" else info.get("MinimumOSVersion")
    if not minimum:
        raise ValueError("Missing platform minimum version")
    resources = contents / "Resources" if platform == "macos" else app
    if not (resources / "Assets.car").is_file() or not (resources / "Art").is_dir() or not any((resources / "Art").iterdir()):
        raise ValueError("Missing compiled asset catalog or Art resources")
    signature = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
    signature_info = subprocess.run(["codesign", "-dv", str(app)], capture_output=True, text=True)
    signed = signature.returncode == 0 and "Authority=" in signature_info.stderr
    write(output, {
        "platform": platform, "version": version, "build": build,
        "bundleId": info["CFBundleIdentifier"], "minOsVersion": minimum,
        "architectures": ["x64" if arch == "x86_64" else arch for arch in architectures],
        "signing": "signed" if signed else "unsigned", "notarization": "not-notarized",
        "archive": str(archive.resolve()), "application": str(app.resolve()),
        "resourcesVerified": True, "executableSha256": file_hash(executable),
        "purpose": "build-evidence", "distributionReady": False,
        **({"category": info["LSApplicationCategoryType"]} if platform == "macos" and "LSApplicationCategoryType" in info else {}),
    })


def installer_name(name, allow_selector=True):
    prefixes = ("Mac Installer Distribution:", "3rd Party Mac Developer Installer:")
    return isinstance(name, str) and ((allow_selector and name == "Mac Installer Distribution") or any(name.startswith(prefix) and name[len(prefix):].strip() for prefix in prefixes))


def installer_certificate(path, team):
    subject = command("openssl", "x509", "-inform", "DER", "-in", str(path), "-noout", "-subject", "-nameopt", "sep_multiline,sname")
    fields = {}
    for line in subject.splitlines():
        key, separator, value = line.strip().partition("=")
        if separator:
            fields.setdefault(key.strip(), []).append(value.strip())
    if fields.get("OU") != [team] or fields.get("UID") != [team]:
        raise ValueError("Installer certificate team mismatch")
    names = fields.get("CN", [])
    if len(names) != 1 or not installer_name(names[0], allow_selector=False):
        raise ValueError("Installer certificate common name is not an installer distribution identity")
    return {"fingerprint": hashlib.sha1(Path(path).read_bytes(), usedforsecurity=False).hexdigest().upper(),
            "commonName": names[0], "team": team}


def check_export(platform, path, team, profile, bundle_id, installer_metadata=""):
    options = plistlib.loads(Path(path).read_bytes())
    if options.get("method") != "app-store-connect" or options.get("destination", "export") != "export":
        raise ValueError("Only local App Store Connect export is supported")
    if options.get("teamID") != team or options.get("provisioningProfiles", {}).get(bundle_id) != profile:
        raise ValueError("Export team/profile mismatch")
    certificate = options.get("signingCertificate", "")
    if not isinstance(certificate, str) or (certificate != "Apple Distribution" and not certificate.startswith("Apple Distribution:")):
        raise ValueError("Apple Distribution application certificate required")
    if platform == "macos":
        installer = options.get("installerSigningCertificate", "")
        if installer_name(installer):
            return
        if not isinstance(installer, str) or not re.fullmatch(r"[A-Fa-f0-9]{40}", installer) or not installer_metadata:
            raise ValueError("Mac Installer Distribution certificate required")
        prepared = json.loads(Path(installer_metadata).read_text())
        certificate = installer_certificate(prepared["certificatePath"], team)
        if prepared.get("team") != team or prepared.get("profile") != profile or prepared.get("keyPairMatched") is not True or prepared.get("identityVisible") is not True:
            raise ValueError("Installer selector lacks prepared key/team/profile/identity proof")
        if not installer_name(prepared.get("serviceName"), allow_selector=False):
            raise ValueError("Installer selector lacks a verified service identity")
        if installer.upper() != certificate["fingerprint"] or prepared.get("fingerprint") != certificate["fingerprint"] or prepared.get("commonName") != certificate["commonName"] or prepared.get("identityCommonName") != certificate["commonName"]:
            raise ValueError("Installer selector does not match the prepared certificate identity")


def record_package(metadata, package, output):
    data = json.loads(Path(metadata).read_text())
    package = Path(package).resolve()
    digest = file_hash(package)
    data.update({"fileName": package.name, "format": "dmg", "size": package.stat().st_size, "sha256": digest})
    write(output, data)


def record_windows(desktop, output, prefix, executable_name):
    desktop, output = Path(desktop), Path(output)
    evidence = json.loads((desktop / "artifacts/package-evidence.json").read_text())
    package_info = json.loads((desktop / "package.json").read_text())
    if evidence["architecture"] != "x64" or evidence["minimumWindows"] != "10" or evidence["electron"] != package_info["devDependencies"]["electron"]:
        raise ValueError("Unexpected Windows package configuration")
    package = desktop / "artifacts" / f"{prefix}-{evidence['version']}-windows-x64-unsigned.exe"
    executable = desktop / "artifacts/win-unpacked" / executable_name
    with executable.open("rb") as source:
        if source.read(2) != b"MZ":
            raise ValueError("Missing Windows executable header")
        source.seek(0x3C)
        offset = struct.unpack("<I", source.read(4))[0]
        source.seek(offset)
        if source.read(4) != b"PE\x00\x00" or struct.unpack("<H", source.read(2))[0] != 0x8664:
            raise ValueError("Packaged Windows application is not x64")
    digest = file_hash(package)
    shutil.copy2(package, output / package.name)
    write(output / "package-metadata.json", {
        "platform": "windows", "version": evidence["version"], "fileName": package.name,
        "format": "exe", "architectures": ["x64"], "minOsVersion": "10.0",
        "signing": "unsigned", "signingVerification": "blocked-on-non-Windows",
        "sha256": digest, "size": package.stat().st_size, "electron": evidence["electron"],
        "purpose": "build-evidence", "distributionReady": False, "nativeInstallerTested": False,
    })


ACTIONS = {"inspect-apple": inspect_apple, "check-export": check_export, "record-package": record_package, "record-windows": record_windows}

if __name__ == "__main__":
    try:
        if sys.argv[1] == "installer-certificate":
            print(json.dumps(installer_certificate(*sys.argv[2:])))
        else:
            ACTIONS[sys.argv[1]](*sys.argv[2:])
    except (KeyError, TypeError, ValueError, OSError, IndexError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
