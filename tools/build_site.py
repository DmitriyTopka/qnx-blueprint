"""Build the GitHub Pages site: reports for every recorded scenario, the codelab, a landing page.

    python tools/build_site.py [out_dir]       # default: site/

Nothing here needs QNX or an API key: the reports come from `blueprint demo`
on the recorded traces, the explanations from the cache. The codelab is
rendered from docs/codelab/*.md with a small converter for the subset of
Markdown it uses (and the codelab "Positive" / "Negative" asides), so the
site has no dependencies beyond Blueprint itself.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_URL = "https://github.com/DmitriyTopka/qnx-blueprint"


def esc(text: str) -> str:
    return html.escape(text, quote=True)


# ---- a small Markdown converter ---------------------------------------------------

def inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for p in parts:
        if p.startswith("`") and p.endswith("`") and len(p) > 1:
            out.append(f"<code>{esc(p[1:-1])}</code>")
            continue
        p = esc(p)
        p = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)",
                   lambda m: f'<img src="{m.group(2)}" alt="{m.group(1)}" loading=lazy>', p)
        p = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', p)
        p = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", p)
        out.append(p)
    return "".join(out)


def markdown(text: str) -> str:
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>" + esc("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
        elif line.strip() in ("Positive", "Negative") and i + 1 < len(lines) and lines[i + 1].startswith(": "):
            kind = line.strip().lower()
            body = [lines[i + 1][2:]]
            j = i + 2
            while j < len(lines) and lines[j].strip():
                body.append(lines[j])
                j += 1
            out.append(f'<aside class="{kind}">{inline(" ".join(body))}</aside>')
            i = j
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            out.append("<div class=tablewrap><table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table></div>")
        elif re.match(r"^(\s*)([-*]|\d+\.) ", line):
            ordered = bool(re.match(r"^\s*\d+\. ", line))
            items = []
            while i < len(lines) and (re.match(r"^\s*([-*]|\d+\.) ", lines[i]) or
                                      (lines[i].startswith("  ") and lines[i].strip() and items)):
                if re.match(r"^\s*([-*]|\d+\.) ", lines[i]):
                    items.append(re.sub(r"^\s*([-*]|\d+\.) ", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(t)}</li>" for t in items) + f"</{tag}>")
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")
            i += 1
        elif line.strip() == "---" or not line.strip():
            i += 1
        else:
            para = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|```|\||\s*([-*]|\d+\.) )", lines[i]) \
                    and lines[i].strip() not in ("Positive", "Negative"):
                para.append(lines[i])
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)


# ---- pages ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6470;--line:#d9dde4;--ok:#1f8a4c;--bad:#c8312f;
--accent:#2f5fd0;--code:#eef1f6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e8eaef;--muted:#9aa3b2;
--line:#2a303b;--ok:#3fbf73;--bad:#ff6b5e;--accent:#7aa2ff;--code:#222833}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1040px;margin:0 auto;padding:32px 16px 64px}
img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:10px;display:block;margin:12px 0}
a{color:var(--accent)}h1{font-size:34px;line-height:1.2;margin:0 0 12px}h2{margin:40px 0 12px}
.lead{font-size:19px;color:var(--muted);max-width:760px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin:16px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
code{font:13px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);padding:1px 6px;border-radius:5px}
pre{background:var(--code);border-radius:10px;padding:14px;overflow-x:auto}pre code{background:none;padding:0}
table{border-collapse:collapse;width:100%;font-size:15px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}.tablewrap{overflow-x:auto}
.pass{color:var(--ok);font-weight:700}.fail{color:var(--bad);font-weight:700}
nav.steps{position:sticky;top:16px}nav.steps ol{padding-left:20px}nav.steps li{margin:4px 0}
.layout{display:grid;grid-template-columns:240px minmax(0,1fr);gap:24px}
@media (max-width:820px){.layout{grid-template-columns:minmax(0,1fr)}nav.steps{position:static}}
.step{scroll-margin-top:16px}.duration{color:var(--muted);font-size:14px}
aside{border-left:4px solid var(--ok);background:var(--card);padding:10px 14px;margin:12px 0;border-radius:6px}
aside.negative{border-color:var(--bad)}
footer{margin-top:48px;color:var(--muted);font-size:14px}
"""


def page(title: str, body: str, description: str = "") -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><meta name=description content='{esc(description)}'>"
            f"<style>{CSS}</style></head><body><main>{body}"
            f"<footer>Blueprint is open source (Apache-2.0): <a href='{REPO_URL}'>{REPO_URL}</a>. "
            "QNX is a trademark of BlackBerry Limited; this project is not affiliated with or endorsed by BlackBerry."
            "</footer></main></body></html>")


def codelab(src: str) -> str:
    text = open(src, encoding="utf-8").read()
    head, _, rest = text.partition("\n\n\n")
    meta = dict(l.split(": ", 1) for l in head.splitlines() if ": " in l)
    # The first chunk is the page title (# ...); every "## " starts a step.
    steps = re.split(r"\n## ", "\n" + rest)[1:]
    sections, nav = [], []
    for n, block in enumerate(steps, 1):
        title, _, body = block.partition("\n")
        dur = ""
        m = re.match(r"Duration: ([\d:]+)\n", body)
        if m:
            dur, body = m.group(1), body[m.end():]
        sid = f"step-{n}"
        nav.append(f"<li><a href='#{sid}'>{inline(title)}</a></li>")
        sections.append(f"<section class=step id={sid}><h2>{n}. {inline(title)}</h2>"
                        + (f"<div class=duration>{esc(dur)} min</div>" if dur else "") + markdown(body) + "</section>")
    body = (f"<p><a href='../'>Blueprint</a> &rsaquo; codelab</p><h1>{inline(meta.get('title', 'Codelab'))}</h1>"
            f"<p class=lead>{inline(meta.get('summary', ''))}</p>"
            f"<p class=duration>By {esc(meta.get('authors', ''))}. "
            f"<a href='{REPO_URL}/blob/main/docs/codelab/{os.path.basename(src)}'>Source</a>.</p>"
            f"<div class=layout><nav class=steps><ol>{''.join(nav)}</ol></nav><div>{''.join(sections)}</div></div>")
    return page(meta.get("title", "Codelab"), body, meta.get("summary", ""))


def index(scenarios: list, stock_ok: bool) -> str:
    rows = []
    for sc in scenarios:
        verdict = f"<span class={sc['expect']}>{sc['expect'].upper()}</span>"
        checks = ", ".join(f"<code>{esc(c)}</code>" for c in sc.get("checks", [])) or "-"
        rows.append(f"<tr><td><code>{esc(sc['name'])}</code></td><td>{esc(sc['story'])}</td><td>{verdict}</td>"
                    f"<td>{checks}</td><td><a href='reports/{esc(sc['name'])}.html'>report</a> &middot; "
                    f"<a href='reports/{esc(sc['name'])}.perfetto.json'>Perfetto</a></td></tr>")
    kernel = markdown("""| Build | Kernel | Blueprint |
|---|---|---|
| as designed | allows everything it does | PASS |
| backdoor: display calls `name_open("cockpit-gps")` | `Operation not permitted` | `kernel-denied`: the refused connection is in the trace |
| backdoor through the resource manager: `open("/dev/cockpit/gps")` | `Operation not permitted` | `kernel-denied`, the same |
| deadlock: gps asks nav for a hint | refuses gps's connection to nav, so there is no loop | `kernel-denied` for gps -> nav, and `stuck-reply` |""")
    body = f"""
<h1>Blueprint</h1>
<p class=lead>One contract, two enforcers: Blueprint checks a QNX 8.0 system against its message architecture
in CI, and turns the same file into a security policy the kernel enforces.</p>
<div class=grid>
 <div class=card><h3>Write the design down</h3><p>A short YAML contract: who may send messages to whom, how long a
 server may keep a client waiting, which process must survive when another fails.</p></div>
 <div class=card><h3>Before release: the trace</h3><p>Blueprint rebuilds every message from a QNX kernel trace and
 fails the build when the system breaks the contract, with the trace events to prove it.</p></div>
 <div class=card><h3>After release: the kernel</h3><p>The same file becomes the channel rules of a QNX security
 policy. In secure mode a backdoor gets <code>Operation not permitted</code>.</p></div>
</div>
<p><a href='https://youtu.be/Z-WK1Dc1k-M'>Watch the 1:49 video</a> of all of it.</p>
<h2>Try it</h2>
<pre><code>git clone {REPO_URL}.git
cd qnx-blueprint
python3 -m venv .venv &amp;&amp; . .venv/bin/activate
python3 -m pip install .
blueprint demo</code></pre>
<p>No QNX target, SDP or API key needed. Or read the step-by-step <a href='codelab/'>codelab</a>.</p>
<h2>Every recorded scenario</h2>
<p>Traces recorded on QNX 8.0 x86_64 in QEMU from the demo cockpit. Each report is the HTML Blueprint writes;
open the Perfetto file at <a href='https://ui.perfetto.dev'>ui.perfetto.dev</a> to see every message as an arrow
between threads. The expected result of each was written down before the checks ran.</p>
<div class=tablewrap><table><thead><tr><th>Scenario</th><th>What happens</th><th>Result</th><th>Checks</th><th>Open</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<h2>The kernel enforcing the same contract</h2>
<p>QNX 8.0 booted in fully secure mode, with a security policy that matches the contract (aarch64, recorded; x86_64 by hand,
same answers):</p>
<div class=tablewrap>{kernel}</div>
<p>Serial logs of both secure boots, and checksums showing that the policy the kernel loaded
(<code>/proc/boot/secpol.bin</code>) is the one compiled from this repository:
<a href='{REPO_URL}/tree/main/demo/cockpit/secpol/evidence'>evidence</a>.</p>
<h2>On a system we didn't write</h2>
<p>A QNX image with only QNX's own services running: <code>blueprint init --all</code> maps eight message paths
between the shell, procnto, devb-eide, io-sock, pipe and io-usb-otg, and the check passes
(<a href='reports/stock.html'>report</a>{'' if stock_ok else ', not built'}).</p>
<h2>Is the parser right?</h2>
<p>Every trace in the repository was also printed with QNX's own <code>traceprinter</code> and compared event by event:
1,071,509 events in 25 traces, on x86_64 and aarch64, with and without wide mode and in secure mode. None missing, extra or different.
<a href='{REPO_URL}/blob/main/docs/TRACEPRINTER.md'>The table</a>.</p>
<p><a href='{REPO_URL}'>Source, README and FAQ on GitHub</a>.</p>
"""
    return page("Blueprint: architecture contracts for QNX 8.0", body,
                "Blueprint checks a QNX 8.0 system against its message architecture and turns it into a security policy.")


def main(out: str) -> int:
    py = sys.executable
    reports = os.path.join(out, "reports")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(reports)
    subprocess.run([py, "-m", "blueprint", "demo", "--out", reports], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)
    stock = subprocess.run([py, "-m", "blueprint", "check", "demo/stock/stock.kev", "-c", "demo/stock/blueprint.yaml",
                            "--html", os.path.join(reports, "stock.html")], cwd=ROOT, stdout=subprocess.DEVNULL)
    with open(os.path.join(ROOT, "demo", "cockpit", "scenarios.yaml"), encoding="utf-8") as f:
        scenarios = yaml.safe_load(f)["scenarios"]
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(index(scenarios, stock.returncode == 0))
    os.makedirs(os.path.join(out, "codelab"))
    shutil.copytree(os.path.join(ROOT, "docs", "codelab", "img"), os.path.join(out, "codelab", "img"))
    with open(os.path.join(out, "codelab", "index.html"), "w", encoding="utf-8") as f:
        f.write(codelab(os.path.join(ROOT, "docs", "codelab", "blueprint-architecture-contracts.md")))
    open(os.path.join(out, ".nojekyll"), "w").close()
    print(f"site in {out}: {len(os.listdir(reports))} report files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "site")))
