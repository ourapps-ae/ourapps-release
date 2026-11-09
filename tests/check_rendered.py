#!/usr/bin/env python3
"""Parse every rendered workflow as YAML (when PyYAML is installed) and report the jobs and what each job needs."""
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
root = Path(sys.argv[1])
try:
    import yaml
except ImportError:
    print(json.dumps({"yaml": False}))
    sys.exit(0)
result = {"yaml": True, "workflows": {}}
for path in sorted((root / ".github/workflows").glob("*.yml")):
    document = yaml.safe_load(path.read_text())
    jobs = document["jobs"]
    for name, job in jobs.items():
        if "runs-on" not in job or not isinstance(job.get("steps"), list):
            raise SystemExit(f"{path.name}: job {name} lacks runs-on or steps")
        for need in ([job["needs"]] if isinstance(job.get("needs"), str) else job.get("needs", [])):
            if need not in jobs:
                raise SystemExit(f"{path.name}: job {name} needs {need}, which is not a job")
    if "on" not in document and True not in document:
        raise SystemExit(f"{path.name}: no trigger")
    listed = lambda value: [value] if isinstance(value, str) else list(value or [])
    result["workflows"][path.name] = {name: listed(job.get("needs")) for name, job in jobs.items()}
print(json.dumps(result))
