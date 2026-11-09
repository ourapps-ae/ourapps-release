#!/usr/bin/env python3
"""The store's per-target switches for this application, read fresh each time; any failed read stops the run.

  read <out.json> [--github-output]   write {ios, android, web, macos, windows, tvos} as booleans
  check <target>                      on=true|false to GITHUB_OUTPUT for that target
  require <target>                    fail unless that target is switched on
  merge <new> <previous> <targets> <out>
      the application manifest for a release: a switched-off target keeps the previous release's entry (or
      none, null for pwa, android and ios); Windows keeps the previous entry when this release brings no
      Windows package.
  packages <records> <targets> <out>  the verified direct packages whose target is switched on
  publishes <targets> [records]       exit 0 when this release builds something switched on, else 3

Environment: STORE_APP_ID and STORE_BUILD_TOKEN (the store credentials, from the pipeline's secret store),
STORE_API (the store's address, default https://ourapps.app), PIPELINE_TARGETS (optional, the targets this
pipeline builds, separated by spaces; the others read as switched off).
STORE_MODE=none (a pipeline rendered without store credentials): no store is called and every target in
PIPELINE_TARGETS (required then) reads as switched on.
"""
import json
import os
import re
import sys
import urllib.request

TARGETS = ("ios", "android", "web", "macos", "windows", "tvos")
# The install entry each target owns; web is the web app's address.
FIELDS = {"ios": "ios", "android": "android", "web": "pwa", "macos": "macos", "windows": "windows", "tvos": "tvos"}
# The store wants these named, null when absent.
REQUIRED = ("pwa", "android", "ios")
# What the publish step builds itself; Mac and Windows only arrive as verified packages.
BUILT = ("ios", "android", "web")
DEFAULT_STORE = "https://ourapps.app"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise ValueError("Target switch redirects are not accepted")


def pipeline_targets():
    text = os.environ.get("PIPELINE_TARGETS", "").strip()
    if not text:
        return None
    chosen = [word for word in text.split() if word]
    unknown = [word for word in chosen if word not in TARGETS]
    if unknown:
        raise ValueError(f"PIPELINE_TARGETS names unknown targets: {unknown}")
    return set(chosen)


def read():
    if os.environ.get("STORE_MODE") == "none":
        chosen = pipeline_targets()
        if chosen is None:
            raise ValueError("STORE_MODE=none needs PIPELINE_TARGETS")
        return {t: t in chosen for t in TARGETS}
    app, token = os.environ.get("STORE_APP_ID", ""), os.environ.get("STORE_BUILD_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", app) or not token:
        raise ValueError("STORE_APP_ID and STORE_BUILD_TOKEN are required")
    base = os.environ.get("STORE_API", DEFAULT_STORE)
    request = urllib.request.Request(f"{base}/api/build/{app}/targets",
                                     headers={"Authorization": "Bearer " + token, "User-Agent": "ourapps-release/1.0"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
        body = json.load(response)
    targets = body.get("targets") if isinstance(body, dict) else None
    if not isinstance(targets, dict) or any(not isinstance(targets.get(t), bool) for t in TARGETS):
        raise ValueError("Target switches answer is not six booleans")
    chosen = pipeline_targets()
    return {t: targets[t] and (chosen is None or t in chosen) for t in TARGETS}


def merge(new, previous, targets):
    out = json.loads(json.dumps(new))
    install = out.setdefault("install", {})
    before = (previous or {}).get("install") or {}
    for target, field in FIELDS.items():
        if not targets[target] or (target == "windows" and install.get(field) is None):
            if before.get(field) is not None:
                install[field] = before[field]
            elif field in REQUIRED:
                install[field] = None
            else:
                install.pop(field, None)
    return out


def packages(records, targets):
    return [r for r in records if targets[r["platform"]]]


def publishes(targets, records=()):
    return any(targets[t] for t in BUILT) or bool(packages(records, targets))


def load_json(path):
    with open(path) as f:
        return json.load(f)


def output_line(name, value):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"{name}={'true' if value else 'false'}\n")


def main(args):
    try:
        if args[:1] == ["read"] and len(args) in (2, 3):
            targets = read()
            with open(args[1], "w") as f:
                json.dump(targets, f)
            if args[2:] == ["--github-output"]:
                for t, on in targets.items():
                    output_line(t, on)
            print("targets: " + " ".join(f"{t}={'on' if on else 'off'}" for t, on in targets.items()))
        elif args[:1] == ["check"] and len(args) == 2 and args[1] in TARGETS:
            on = read()[args[1]]
            output_line("on", on)
            print(f"{args[1]} is switched {'on' if on else 'off'}")
        elif args[:1] == ["require"] and len(args) == 2 and args[1] in TARGETS:
            if not read()[args[1]]:
                raise ValueError("Target is disabled.")
            print(f"{args[1]} is switched on")
        elif args[:1] == ["packages"] and len(args) == 4:
            with open(args[3], "w") as f:
                json.dump(packages(load_json(args[1]), load_json(args[2])), f)
        elif args[:1] == ["publishes"] and len(args) in (2, 3):
            if not publishes(load_json(args[1]), load_json(args[2]) if len(args) == 3 else ()):
                print("Nothing in this release is switched on in the store; nothing is published.")
                return 3
        elif args[:1] == ["merge"] and len(args) == 5:
            load = lambda p: load_json(p) if p != "-" else {}
            with open(args[4], "w") as f:
                json.dump(merge(load(args[1]), load(args[2]), load(args[3])), f, ensure_ascii=False)
        else:
            raise ValueError(__doc__)
    except Exception as error:
        print(f"release-targets: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
