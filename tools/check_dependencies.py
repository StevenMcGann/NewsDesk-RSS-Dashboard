"""Daily check of the bundled third-party components.

For each component in tools/dependencies.json:
- known vulnerabilities in the pinned version, from the OSV database (osv.dev);
- whether a newer version exists, and whether it is at least two weeks old, the
  project's rule before adopting a release.

Writes a Markdown report (stdout, --report file, and the GitHub job summary) and sets the
job output attention=true when something needs doing. Exits 1 only for vulnerabilities.
Usage:  py -3 tools/check_dependencies.py [--report report.md]
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tools" / "dependencies.json"
WAIT = timedelta(days=14)
UA = {"User-Agent": "NewsDesk-dependency-check (+https://github.com/StevenMcGann/NewsDesk-RSS-Dashboard)"}


def get(url, data=None, method=None):
    headers = dict(UA, **({"Content-Type": "application/json"} if data else {}))
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read(), resp.headers


def get_json(url, payload=None):
    body, _ = get(url, json.dumps(payload).encode() if payload is not None else None)
    return json.loads(body)


def version_key(text):
    return tuple(int(p) for p in re.findall(r"\d+", text)[:4])


def vulnerabilities(components):
    """{component name: [(id, summary)]} for components OSV knows about."""
    queryable = [c for c in components if c["ecosystem"] in ("PyPI", "npm")]
    batch = get_json("https://api.osv.dev/v1/querybatch", {"queries": [
        {"package": {"name": c["package"], "ecosystem": c["ecosystem"]}, "version": c["version"]} for c in queryable]})
    found = {}
    for component, result in zip(queryable, batch.get("results", [])):
        for vuln in result.get("vulns", []):
            detail = get_json(f"https://api.osv.dev/v1/vulns/{vuln['id']}")
            found.setdefault(component["name"], []).append((vuln["id"], detail.get("summary") or "(no summary)"))
    return found


def latest(component):
    """(newest stable version, its release date) from the component's own registry."""
    eco, pkg = component["ecosystem"], component["package"]
    if eco == "PyPI":
        data = get_json(f"https://pypi.org/pypi/{pkg}/json")
        version = data["info"]["version"]
        uploaded = data["releases"][version][0]["upload_time_iso_8601"]
        return version, datetime.fromisoformat(uploaded.replace("Z", "+00:00"))
    if eco == "npm":
        data = get_json(f"https://registry.npmjs.org/{pkg.replace('/', '%2F')}")
        version = data["dist-tags"]["latest"]
        return version, datetime.fromisoformat(data["time"][version].replace("Z", "+00:00"))
    if eco == "python.org":  # newest patch release of the same minor that ships an embeddable zip
        minor = ".".join(component["version"].split(".")[:2])
        listing = get("https://www.python.org/ftp/python/")[0].decode()
        versions = sorted(set(re.findall(rf'href="({re.escape(minor)}\.\d+)/"', listing)), key=version_key, reverse=True)
        for version in versions:
            try:
                _, headers = get(f"https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip",
                                 method="HEAD")
                return version, parsedate_to_datetime(headers["Last-Modified"])
            except OSError:
                continue
        return component["version"], None
    raise ValueError(f"unknown ecosystem {eco}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # advisory text can be any Unicode
    parser =argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    components = json.loads(MANIFEST.read_text(encoding="utf-8"))["components"]
    now = datetime.now(timezone.utc)

    # Advisories reviewed as not affecting NewsDesk's configuration are listed, not failed on.
    vulns, reviewed = {}, {}
    for name, items in vulnerabilities(components).items():
        notes = next(c for c in components if c["name"] == name).get("reviewed", {})
        for vid, summary in items:
            target = reviewed if vid in notes else vulns
            target.setdefault(name, []).append((vid, notes.get(vid, summary)))
    rows, ready = [], 0
    for c in components:
        try:
            newest, released = latest(c)
        except (OSError, KeyError, ValueError) as e:
            rows.append(f"| {c['name']} | {c['version']} | ? | could not check: {e} |")
            continue
        if version_key(newest) <= version_key(c["version"]):
            status = "up to date"
        elif released and now - released >= WAIT:
            status, ready = f"**update to {newest}** (released {released:%Y-%m-%d})", ready + 1
        else:
            status = f"{newest} is out; wait until {(released + WAIT):%Y-%m-%d}" if released else f"{newest} is out"
        rows.append(f"| {c['name']} | {c['version']} | {newest} | {status} |")

    lines = ["## Bundled dependencies", ""]
    if vulns:
        lines += ["### Known vulnerabilities in the pinned versions", ""]
        for name, items in vulns.items():
            lines += [f"- **{name}**: " + "; ".join(f"[{vid}](https://osv.dev/vulnerability/{vid}) {summary}"
                                                  for vid, summary in items)]
        lines.append("")
    else:
        lines += ["No unreviewed vulnerabilities in the pinned versions (source: [osv.dev](https://osv.dev)).", ""]
    if reviewed:
        lines += ["### Reviewed advisories that don't affect NewsDesk", ""]
        for name, items in reviewed.items():
            lines += [f"- **{name}** [{vid}](https://osv.dev/vulnerability/{vid}): {why}" for vid, why in items]
        lines.append("")
    lines += ["| Component | Pinned | Newest | Status |", "|---|---|---|---|", *rows, "",
              "How to update: see \"Updating vendored code\" in docs/ARCHITECTURE.md, then change the version "
              "in tools/dependencies.json and THIRD-PARTY.md."]
    report = "\n".join(lines)
    print(report)
    if args.report:
        args.report.write_text(report + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(report + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"attention={'true' if vulns or ready else 'false'}\n")
    return 1 if vulns else 0


if __name__ == "__main__":
    sys.exit(main())
