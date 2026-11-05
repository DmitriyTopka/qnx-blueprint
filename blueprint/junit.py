"""JUnit XML for a check result, so CI systems show each check as a test.

One <testcase> per check Blueprint runs on every trace. A check with an
error finding fails; warnings go to <system-out>. GitHub, GitLab and Jenkins
all read this format.
"""

from __future__ import annotations

from typing import Dict, List
from xml.sax.saxutils import escape, quoteattr

# The checks every run makes, in the order checks.run makes them.
CHECKS = ["trace-loss", "trace-mode", "unexpected-edge", "wait-cycle", "stuck-reply",
          "reply-budget", "isolation", "priority-inversion", "no-traffic"]


def render(result: dict) -> str:
    """Return a JUnit XML document for the dict cmd_check writes as JSON."""
    by_check: Dict[str, List[dict]] = {}
    for f in result["findings"]:
        by_check.setdefault(f["check"], []).append(f)
    names = CHECKS + sorted(c for c in by_check if c not in CHECKS)  # secpol, when --secpol is given
    suite = f"blueprint: {result['system']}"
    cases, failures = [], 0
    for name in names:
        found = by_check.get(name, [])
        errors = [f for f in found if f["severity"] == "error"]
        warnings = [f for f in found if f["severity"] != "error"]
        body = ""
        if errors:
            failures += 1
            text = "\n\n".join(_text(f) for f in errors)
            body += (f"<failure message={quoteattr(errors[0]['title'])} type={quoteattr(name)}>"
                     f"{escape(text)}</failure>")
        if warnings:
            body += f"<system-out>{escape(chr(10).join('warning: ' + _text(f) for f in warnings))}</system-out>"
        cases.append(f'  <testcase classname={quoteattr(suite)} name={quoteattr(name)} time="0"'
                     + (f">{body}</testcase>" if body else "/>"))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuites name="blueprint" tests="{len(names)}" failures="{failures}">\n'
            f'<testsuite name={quoteattr(suite)} tests="{len(names)}" failures="{failures}" errors="0" '
            f'skipped="0" file={quoteattr(result["trace"])}>\n'
            + "\n".join(cases) + "\n</testsuite>\n</testsuites>\n")


def _text(f: dict) -> str:
    lines = [f["title"], f["detail"]]
    if f.get("events"):
        lines.append("evidence: events " + ", ".join(map(str, f["events"])))
    e = f.get("explanation")
    if e:
        lines += [f"why: {e['cause']}", f"fix: {e['fix_summary']}"]
    return "\n".join(lines)
