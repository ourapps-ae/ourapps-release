#!/usr/bin/env python3
"""What one run of the release workflow builds, from the settings aepm rendered into release.json at install.

  release_plan.py <release.json> [<target>=<path> ...]

<target>=<path> names the file whose presence shows that the application has that target's project, for example
web=web/vite.config.ts android=android/generate-project.ts windows=desktop/package.json ios=ios/generate-project.ts.
A target is built when the settings' targets include it and its file exists (ios only with store credentials, which the
workflow decides). Written to GITHUB_OUTPUT when it is set, and printed:

  version, build   the pushed v<x.y.z> tag (else the latest v* tag, else 0.0.0) and the run number
  tag              the pushed v* tag, empty for other runs
  application      the application id; name the application name
  pipeline         the targets built, separated by spaces (ios left out)
  <target>         true or false for each named target
  store_address    the store's address
  store, android_secret, web_secret, hosting_secret
                   the repository secret name prefix of each reference (store credentials, Android signing, web hosting,
                   server hosting): capitals, digits and underscores; empty when the reference is none
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TARGETS = ("web", "android", "windows", "ios")
REFERENCES = {"store": "storeCredentials", "android_secret": "androidSigning", "web_secret": "webDeploy", "hosting_secret": "hostingAccount"}


def secret_name(reference):
    """The repository secret name prefix for a reference name; empty for none."""
    if reference in ("", "none"):
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_./-]{0,127}", reference):
        raise ValueError(f"{reference!r} is not a reference name")
    return re.sub(r"[^A-Z0-9]+", "_", reference.upper()).strip("_")


def version(ref, describe):
    """The version from the pushed tag, or from the latest v* tag for other runs."""
    tag = ref[len("refs/tags/"):] if ref.startswith("refs/tags/") else ""
    found = tag or describe() or "v0.0.0"
    if not re.fullmatch(r"v\d+\.\d+\.\d+", found):
        raise ValueError(f"tag {found} is not v<major>.<minor>.<patch>")
    return found[1:], tag


def latest_tag():
    result = subprocess.run(["git", "describe", "--tags", "--match", "v*", "--abbrev=0"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def plan(settings, markers, ref, run_number, exists=os.path.exists, describe=latest_tag):
    for key in ("applicationId", "applicationName", "targets", "storeAddress", *REFERENCES.values()):
        if not isinstance(settings.get(key), str):
            raise ValueError(f"release.json has no {key}")
    targets = settings["targets"].split()
    present = {target: target in targets and exists(path) for target, path in markers.items()}
    number, tag = version(ref, describe)
    if not re.fullmatch(r"[1-9]\d*", run_number):
        raise ValueError("the run number must be a positive number")
    out = {"version": number, "build": run_number, "tag": tag, "application": settings["applicationId"], "name": settings["applicationName"],
           "pipeline": " ".join(t for t in TARGETS if present.get(t) and t != "ios"), "store_address": settings["storeAddress"].rstrip("/")}
    out.update({target: "true" if present.get(target) else "false" for target in markers})
    out.update({key: secret_name(settings[name]) for key, name in REFERENCES.items()})
    return out


def main(argv):
    try:
        if not argv:
            raise ValueError(__doc__)
        settings = json.loads(Path(argv[0]).read_text())
        markers = {}
        for pair in argv[1:]:
            target, _, path = pair.partition("=")
            if target not in TARGETS or not path or target in markers:
                raise ValueError(f"expected <target>=<path> with a target of {', '.join(TARGETS)}: {pair}")
            markers[target] = path
        out = plan(settings, markers, os.environ.get("GITHUB_REF", ""), os.environ.get("GITHUB_RUN_NUMBER", "1"))
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"release-plan: {error}", file=sys.stderr)
        return 1
    lines = [f"{key}={value}" for key, value in out.items()]
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
