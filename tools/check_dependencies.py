"""Daily check of the bundled third-party components.

For each component in tools/dependencies.json:
- known vulnerabilities in the pinned version, from OSV (including PSF runtime advisories
  matched against the official CPython release commit);
- whether a newer version exists, and whether it is at least two weeks old, the
  project's rule before adopting a release.

Writes a Markdown report (stdout, --report file, and the GitHub job summary) and sets the
job output attention=true when something needs doing. Exits 1 for vulnerabilities and
2 for incomplete checks. Security findings are reported without the 14-day update delay.
Usage:  py -3 tools/check_dependencies.py [--report report.md]
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from urllib.error import HTTPError
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


def python_commit(version):
    """Resolve the official release tag; CPython advisories use Git ranges, not PyPI."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"unsupported Python release version: {version}")
    base = "https://api.github.com/repos/python/cpython/git"
    obj = get_json(f"{base}/ref/tags/v{version}")["object"]
    for _ in range(5):
        sha = obj["sha"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("invalid CPython release object SHA")
        if obj["type"] == "commit":
            return sha
        if obj["type"] != "tag":
            raise ValueError("CPython release tag does not resolve to a commit")
        obj = get_json(f"{base}/tags/{sha}")["object"]
    raise ValueError("too many annotated CPython release tags")


def osv_results(queries):
    """Return complete results in query order, refusing incomplete/malformed responses."""
    collected = [[] for _ in queries]
    pending = list(enumerate(queries))
    tokens = [set() for _ in queries]
    for _ in range(10):
        batch = get_json("https://api.osv.dev/v1/querybatch", {"queries": [q for _, q in pending]})
        results = batch["results"]
        if not isinstance(results, list) or len(results) != len(pending):
            raise ValueError("OSV returned an incomplete result batch")
        following = []
        for (index, query), result in zip(pending, results):
            if not isinstance(result, dict) or "error" in result:
                raise ValueError("OSV returned an invalid query result")
            vulns = result.get("vulns", [])  # OSV uses {} for a complete, empty result.
            if not isinstance(vulns, list) or any(
                    not isinstance(v, dict) or not isinstance(v.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9-]+", v["id"]) for v in vulns):
                raise ValueError("OSV returned invalid vulnerability IDs")
            collected[index].extend(vulns)
            token = result.get("next_page_token")
            if token:
                if not isinstance(token, str) or token in tokens[index]:
                    raise ValueError("OSV returned an invalid/repeated page token")
                tokens[index].add(token)
                following.append((index, dict(query, page_token=token)))
        if not following:
            return collected
        pending = following
    raise ValueError("OSV pagination did not finish")


def vulnerabilities(components):
    """{component name: [(id, summary)]}, including the bundled CPython runtime."""
    queries = []
    for c in components:
        if c["ecosystem"] == "python.org":
            queries.append({"commit": python_commit(c["version"])})
        elif c["ecosystem"] in ("PyPI", "npm"):
            queries.append({"package": {"name": c["package"], "ecosystem": c["ecosystem"]},
                            "version": c["version"]})
        else:
            raise ValueError(f"unsupported vulnerability ecosystem: {c['ecosystem']}")
    found = {}
    for component, result in zip(components, osv_results(queries)):
        seen = set()
        # Prefer PSF records over duplicate CVE aliases for the same runtime finding.
        for vuln in sorted(result, key=lambda v: (not v["id"].startswith("PSF-"), v["id"])):
            detail = get_json(f"https://api.osv.dev/v1/vulns/{vuln['id']}")
            if detail["id"] != vuln["id"]:
                raise ValueError("OSV returned a mismatched advisory")
            if detail.get("withdrawn"):
                continue
            aliases = detail.get("aliases", [])
            if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
                raise ValueError("OSV returned invalid advisory aliases")
            ids = {detail["id"], *aliases}
            if seen.intersection(ids):
                continue
            seen.update(ids)
            summary = detail.get("summary") or detail.get("details") or "(no summary)"
            if not isinstance(summary, str):
                raise ValueError("OSV returned invalid advisory text")
            summary = " ".join(summary.split())[:500]
            cves = [a for a in aliases if a.startswith("CVE-")]
            if cves:
                summary = f"{', '.join(cves)}: {summary}"
            found.setdefault(component["name"], []).append((vuln["id"], summary))
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
            except HTTPError as e:
                if e.code != 404:
                    raise
        raise ValueError("no Python embeddable release could be verified")
    raise ValueError(f"unknown ecosystem {eco}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # advisory text can be any Unicode
    parser =argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    components = json.loads(MANIFEST.read_text(encoding="utf-8"))["components"]
    now = datetime.now(timezone.utc)

    # Advisories reviewed as not affecting NewsDesk's configuration are listed, not failed on.
    vulns, reviewed, errors = {}, {}, []
    try:
        findings = vulnerabilities(components)
    except (OSError, ValueError, KeyError, TypeError) as e:
        findings = {}
        errors.append(f"Vulnerability coverage incomplete: {e}")
    for name, items in findings.items():
        notes = next(c for c in components if c["name"] == name).get("reviewed", {})
        for vid, summary in items:
            target = reviewed if vid in notes else vulns
            target.setdefault(name, []).append((vid, notes.get(vid, summary)))
    rows, ready = [], 0
    for c in components:
        try:
            newest, released = latest(c)
        except (OSError, KeyError, ValueError, TypeError) as e:
            rows.append(f"| {c['name']} | {c['version']} | ? | could not check: {e} |")
            errors.append(f"Version check incomplete for {c['name']}: {e}")
            continue
        if version_key(newest) <= version_key(c["version"]):
            status = "up to date"
        elif c["name"] in vulns:
            status, ready = f"**security review: {newest} available; do not wait 14 days**", ready + 1
        elif released and now - released >= WAIT:
            status, ready = f"**update to {newest}** (released {released:%Y-%m-%d})", ready + 1
        else:
            status = f"{newest} is out; wait until {(released + WAIT):%Y-%m-%d}" if released else f"{newest} is out"
        rows.append(f"| {c['name']} | {c['version']} | {newest} | {status} |")

    lines = ["## Bundled dependencies", ""]
    if errors:
        lines += ["### Incomplete checks — attention required", "", *[f"- {e}" for e in errors], ""]
    if vulns:
        lines += ["### Known vulnerabilities in the pinned versions", ""]
        for name, items in vulns.items():
            lines += [f"- **{name}**: " + "; ".join(f"[{vid}](https://osv.dev/vulnerability/{vid}) {summary}"
                                                  for vid, summary in items)]
        lines.append("")
    elif not errors:
        lines += ["No unreviewed vulnerabilities reported by OSV for the checked pins, including the bundled "
                  "CPython release commit (PSF runtime advisories). This is not a guarantee that none exist.", ""]
    lines += ["Sources: [OSV](https://osv.dev), including the "
              "[PSF advisory database](https://github.com/psf/advisory-database); Python is queried by its "
              "[official release tag's commit](https://github.com/python/cpython/tags).", ""]
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
            f.write(f"attention={'true' if vulns or ready or errors else 'false'}\n")
    return 2 if errors else 1 if vulns else 0


if __name__ == "__main__":
    sys.exit(main())
