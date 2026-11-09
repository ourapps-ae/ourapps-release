#!/usr/bin/env python3
"""Validate optional verified direct packages before release creation.

  validate <records.json> <version> <out.json>   check each record's package against its metadata
  entries <records.json> <urls.json> <out.json>  the install entries for the application manifest
"""
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


def https(value):
    parsed = urlparse(value if isinstance(value, str) else "")
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def validate(config, version):
    records = json.loads(Path(config).read_text())
    if not isinstance(records, list) or not records:
        raise ValueError("Expected a nonempty verified package list")
    result, names = [], set()
    for record in records:
        package = Path(record["file"]).resolve(strict=True)
        metadata = json.loads(Path(record["metadata"]).read_text(encoding="utf-8-sig"))
        platform = metadata["platform"]
        if platform not in ("macos", "windows"):
            raise ValueError("Only verified direct Mac/Windows packages are supported")
        extension = "dmg" if platform == "macos" else "exe"
        if package.suffix != "." + extension or metadata.get("format") != extension:
            raise ValueError("Package format mismatch")
        if metadata.get("version") != version or metadata.get("fileName") != package.name:
            raise ValueError("Package version/name mismatch")
        digest = hashlib.sha256()
        with package.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if metadata.get("sha256") != digest.hexdigest() or metadata.get("size") != package.stat().st_size:
            raise ValueError("Package hash/size mismatch")
        if metadata.get("distributionReady") is not True or metadata.get("signing") != "signed":
            raise ValueError("Unsigned/build-only evidence cannot enable distribution")
        if not https(record.get("runtimeEvidence")) or not https(record.get("storeEvidence")):
            raise ValueError("Verified runtime and deployed store evidence addresses are required")
        architectures = metadata.get("architectures")
        if not isinstance(architectures, list) or not architectures or any(arch not in ("arm64", "x64") for arch in architectures) or len(set(architectures)) != len(architectures):
            raise ValueError("Invalid package architectures")
        if platform == "windows" and architectures != ["x64"]:
            raise ValueError("Windows package must be x64")
        if not re.fullmatch(r"\d+(?:\.\d+){0,3}", metadata.get("minOsVersion", "")):
            raise ValueError("Missing or invalid minimum system version")
        if platform == "macos" and metadata.get("notarization") != "notarized":
            raise ValueError("Verified direct Mac distribution requires notarization")
        name = package.name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name in names:
            raise ValueError("Package asset names must be safe and unique")
        names.add(name)
        contract = {key: metadata[key] for key in ("format", "architectures", "minOsVersion", "signing")}
        if platform == "macos":
            contract["notarization"] = "notarized"
        result.append({"file": str(package), "name": name, "platform": platform, "package": contract})
    return result


def install_entries(records, urls):
    result = {}
    for record in records:
        url = urls[record["name"]]
        if not https(url):
            raise ValueError("Release package address must use https")
        result.setdefault(record["platform"], {"packages": []})["packages"].append({"url": url, **record["package"]})
    return result


if __name__ == "__main__":
    try:
        if sys.argv[1] == "validate":
            result = validate(sys.argv[2], sys.argv[3])
        elif sys.argv[1] == "entries":
            result = install_entries(json.loads(Path(sys.argv[2]).read_text()), json.loads(Path(sys.argv[3]).read_text()))
        else:
            raise ValueError("Expected validate or entries")
        Path(sys.argv[-1]).write_text(json.dumps(result, indent=2) + "\n")
    except (ValueError, KeyError, OSError, TypeError, IndexError) as error:
        sys.exit(str(error))
