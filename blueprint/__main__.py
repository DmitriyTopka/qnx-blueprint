"""Command line entry point: python -m blueprint <command> ..."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import os

from . import api, checks, contract, explain, init_contract, ipc, junit, kev, perfetto, record, report, secpol, snapshot, topology


def cmd_pairs(args: argparse.Namespace) -> int:
    trace = kev.read(args.trace)
    model = ipc.build(trace)
    t0 = trace.events[0].t_cycles if trace.events else 0
    shown = 0
    for tx in model.transactions:
        if args.pid and args.pid not in (tx.client_pid, tx.server_pid):
            continue
        dt = ""
        if tx.t_reply is not None:
            dt = f"{(tx.t_reply - tx.t_send) / trace.cycles_per_sec * 1e6:8.1f} us"
        print(
            f"{trace.seconds(tx.t_send - t0):9.6f}s "
            f"{model.name(tx.client_pid)}[{tx.client_pid}/{tx.client_tid}] "
            f"coid={tx.coid:#x} -> chid={tx.chid if tx.chid is not None else '?'} "
            f"{model.name(tx.server_pid)}[{tx.server_pid}/{tx.server_tid}] "
            f"{tx.status:8} {dt} {tx.preview!r}->{tx.reply_preview!r}"
        )
        shown += 1
    print(f"# {shown} transactions, {len(model.connections)} connections, "
          f"{len(model.channels)} channels, {len(trace.events)} events", file=sys.stderr)
    return 0


def cmd_topology(args: argparse.Namespace) -> int:
    model = ipc.build(kev.read(args.trace))
    only = [p for p in args.only.split(",") if p] if args.only else None
    topo = topology.build(model, only)
    if args.json:
        json.dump(topo.to_json(), sys.stdout, indent=2)
        print()
    else:
        print(topology.format_text(topo))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        trace, con, findings, topo, snap_path = api.run(args.trace, args.contract, args.snapshot,
                                                        args.secpol, _types(args) if args.secpol else None)
    except (OSError, contract.ContractError, kev.KevFormatError, secpol.PolicyError) as e:
        print(f"blueprint: {e}", file=sys.stderr)
        return 2
    snap = snap_path
    errors = [f for f in findings if f.severity == checks.ERROR]
    explanations = {}
    if args.explain:
        model = ipc.build(trace)
        with open(args.contract, encoding="utf-8") as f:
            contract_text = f.read()
        sources = {}
        for path in args.source:
            with open(path, encoding="utf-8") as f:
                sources[path] = f.read()
        cache = args.cache or os.path.join(os.path.dirname(os.path.abspath(args.contract)), "explanations")
        for i, finding in enumerate(findings):
            if finding.severity != checks.ERROR:
                continue
            try:
                explanations[i] = explain.explain(trace, model, finding, con, contract_text,
                                                  sources, cache, offline=args.offline)
            except explain.ExplainError as e:
                print(f"blueprint: no explanation for '{finding.title}': {e}", file=sys.stderr)
    result = api.result(con, args.trace, args.contract, findings, topo, explanations)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
    if args.junit:
        with open(args.junit, "w", encoding="utf-8") as f:
            f.write(junit.render(result))
    if args.markdown:
        with open(args.markdown, "a", encoding="utf-8") as f:
            f.write(_markdown(result, explanations))
    if args.perfetto or args.html:
        model = ipc.build(trace)
        if args.perfetto:
            perfetto.write(args.perfetto, perfetto.export(trace, model, con.processes, findings))
        if args.html:
            with open(args.html, "w", encoding="utf-8") as f:
                f.write(report.render(trace, model, con, findings, topo, args.trace, explanations))
    for i, f in enumerate(findings):
        mark = "FAIL" if f.severity == checks.ERROR else "warn"
        print(f"{mark}  [{f.check}] {f.title}")
        print(f"      {f.detail}")
        if f.data.get("where"):
            print(f"      where: {f.data['where']}")
        if f.events:
            print(f"      evidence: events {', '.join(map(str, f.events))}")
        if i in explanations:
            e = explanations[i]
            print(f"      why: {e.cause}")
            print(f"      fix: {e.fix_summary}")
    if snap is not None:
        print(f"(thread states from {snap_path})")
    verdict = "FAIL" if errors else "PASS"
    print(f"{verdict}: {con.system or 'system'} "
          f"{checks.counts(len(errors), len(findings) - len(errors))} ({args.trace})")
    return 1 if errors else 0


def cmd_init(args: argparse.Namespace) -> int:
    try:
        trace = kev.read(args.trace)
    except (OSError, kev.KevFormatError) as e:
        print(f"blueprint: {e}", file=sys.stderr)
        return 2
    model = ipc.build(trace)
    only = [p for p in args.only.split(",") if p] if args.only else None
    try:
        processes, left_out = init_contract.select(model, only, include_system=args.all)
    except ValueError as e:
        print(f"blueprint: {e}", file=sys.stderr)
        return 2
    if not processes:
        print("blueprint: no two application processes exchange messages in this trace; "
              "name them with --only", file=sys.stderr)
        return 2
    text = init_contract.draft(trace, model, processes, os.path.basename(args.trace), args.system)
    try:
        contract.parse(__import__("yaml").safe_load(text))     # the draft must be a valid contract
    except contract.ContractError as e:
        print(f"blueprint: internal error, the draft is not a valid contract: {e}", file=sys.stderr)
        return 2
    if args.review:
        from . import review
        sources = {}
        for path in args.source:
            with open(path, encoding="utf-8") as f:
                sources[path] = f.read()
        cache = args.cache or os.path.join(os.path.dirname(os.path.abspath(args.trace)), "explanations")
        try:
            text, marks = review.review(trace, model, processes, text, sources, cache, offline=args.offline)
        except explain.ExplainError as e:
            print(f"blueprint: no review: {e}", file=sys.stderr)
            return 2
        flagged = [k for k, m in marks.items() if m["verdict"] != "keep"]
        print(f"review: {len(flagged)} of {len(marks)} edges marked for a question", file=sys.stderr)
    if args.out:
        if os.path.exists(args.out) and not args.force:
            print(f"blueprint: {args.out} exists; pass --force to overwrite it", file=sys.stderr)
            return 2
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    if left_out:
        print(f"left out (system services or no messages with the others): {', '.join(left_out)}",
              file=sys.stderr)
    return 0


def _types(args: argparse.Namespace) -> dict:
    return {k: v[-1] for k, v in secpol.parse_pairs(args.type, "type").items()}


def cmd_secpol(args: argparse.Namespace) -> int:
    try:
        con = contract.load(args.contract)
        types = _types(args)
        if args.check:
            with open(args.check, encoding="utf-8") as f:
                policy = secpol.parse(f.read())
        else:
            text = secpol.generate(con, secpol.parse_pairs(args.attach, "attach"), types,
                                   os.path.basename(args.contract))
    except (OSError, contract.ContractError, secpol.PolicyError) as e:
        print(f"blueprint: {e}", file=sys.stderr)
        return 2
    if not args.check:
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(text)
        return 0
    try:
        findings = secpol.compare(con, policy, args.check, types)
    except secpol.PolicyError as e:
        print(f"blueprint: {e}", file=sys.stderr)
        return 2
    errors = [f for f in findings if f.severity == checks.ERROR]
    for f in findings:
        print(f"{'FAIL' if f.severity == checks.ERROR else 'warn'}  [{f.check}] {f.title}")
        print(f"      {f.detail}")
    print(f"{'FAIL' if errors else 'PASS'}: {args.check} "
          f"{checks.counts(len(errors), len(findings) - len(errors))} against {args.contract}")
    return 1 if errors else 0


def cmd_record(args: argparse.Namespace) -> int:
    try:
        injections = [record.Injection.parse(t) for t in args.inject]
        con = contract.load(args.contract) if args.contract else None
    except (record.RecordError, OSError, contract.ContractError) as e:
        print(f"blueprint: {e}", file=sys.stderr)
        return 2
    if con is not None:
        unknown = [i.process for i in injections if i.process not in con.processes]
        if unknown:
            print(f"blueprint: --inject {unknown[0]} is not a process in {args.contract}", file=sys.stderr)
            return 2
    try:
        result = record.record(args.host, args.out, args.duration, injections,
                               start=args.start, ssh=args.ssh, buffers=args.buffers)
    except (record.RecordError, subprocess.TimeoutExpired) as e:
        print(f"blueprint: {e}", file=sys.stderr)
        return 2
    record.print_log(result)
    trace = kev.read(result["trace"])
    model = ipc.build(trace)
    for w in record.check_injections(result, [model.name(pid) for pid in model.deaths]):
        print(f"warning: {w}", file=sys.stderr)
    print(f"wrote {result['trace']}, {result['snapshot']} ({len(trace.events)} events)")
    if con is None or args.no_check:
        return 0
    # Start from the check command's own defaults, so options added to check
    # later cannot be missing here.
    check_args = build_parser().parse_args(["check", result["trace"], "--contract", args.contract])
    check_args.json, check_args.perfetto, check_args.html = args.json, args.perfetto, args.html
    check_args.junit = getattr(args, "junit", "")
    return cmd_check(check_args)


def _md(text: str) -> str:
    """Make text from a trace or a model safe for a Markdown table or paragraph.

    Collapses newlines and escapes the characters that would start links,
    images, HTML, emphasis or table cells.
    """
    text = " ".join(str(text).split())
    for ch in "\\`*_{}[]()<>#+!|~":
        text = text.replace(ch, "\\" + ch)
    return text


def _markdown(result: dict, explanations: dict) -> str:
    """Short summary for a CI page (GitHub step summary)."""
    verdict = "PASS" if result["result"] == "pass" else "FAIL"
    lines = [f"### Blueprint: {_md(result['system'] or 'system')} {verdict}", "",
             f"{checks.counts(result['summary']['errors'], result['summary']['warnings'])} "
             f"in {_md(result['trace'])}", ""]
    if result["findings"]:
        lines += ["| | Check | Finding |", "|---|---|---|"]
        for f in result["findings"]:
            mark = "error" if f["severity"] == checks.ERROR else "warning"
            lines.append(f"| {mark} | {_md(f['check'])} | {_md(f['title'])} |")
        for f in result["findings"]:
            if f.get("data", {}).get("where"):
                lines += ["", f"**{_md(f['title'])}**: {_md(f['data']['where'])}"]
        for f in result["findings"]:
            e = f.get("explanation")
            if e:
                lines += ["", f"**{_md(f['title'])}**", "", _md(e["cause"]), "",
                          f"Fix: {_md(e['fix_summary'])}"]
    return "\n".join(lines) + "\n\n"


DEMO_DEFAULT = os.path.join("demo", "cockpit", "scenarios.yaml")


def cmd_mcp(args: argparse.Namespace) -> int:
    from . import mcp_server
    return mcp_server.main()


def cmd_demo(args: argparse.Namespace) -> int:
    import yaml

    if not os.path.exists(args.scenarios) and args.scenarios == DEMO_DEFAULT:
        # Not in a clone: use the copy installed with the package.
        args.scenarios = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo", "cockpit", "scenarios.yaml")
    if not os.path.exists(args.scenarios):
        print(f"blueprint demo: {args.scenarios} not found; run it from a clone of "
              "github.com/DmitriyTopka/qnx-blueprint or pass --scenarios", file=sys.stderr)
        return 2
    base = os.path.dirname(os.path.abspath(args.scenarios))
    with open(args.scenarios, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    contract_path = os.path.join(base, spec["contract"])
    con = contract.load(contract_path)
    with open(contract_path, encoding="utf-8") as f:
        contract_text = f.read()
    sources = {}
    if spec.get("source"):
        src = os.path.join(base, spec["source"])
        with open(src, encoding="utf-8") as f:
            sources[spec["source"]] = f.read()
    os.makedirs(args.out, exist_ok=True)
    cache = os.path.join(base, "explanations")
    ok_all = True
    print(f"{'scenario':<22}{'expected':<10}{'got':<6}{'checks':<34}story")
    for sc in spec["scenarios"]:
        trace_path = os.path.join(base, "traces", sc["name"] + ".kev")
        trace = kev.read(trace_path)
        snap_path = api.stem(trace_path) + ".pidin"
        snap = snapshot.load(snap_path) if os.path.exists(snap_path) else None
        findings, topo = checks.run(trace, con, snap)
        errors = [f for f in findings if f.severity == checks.ERROR]
        got = "fail" if errors else "pass"
        got_checks = sorted({f.check for f in errors})
        ok = got == sc["expect"] and (not sc.get("checks") or got_checks == sorted(sc["checks"]))
        ok_all &= ok
        model = ipc.build(trace)
        explanations = {}
        for i, finding in enumerate(findings):
            if finding.severity == checks.ERROR:
                try:
                    explanations[i] = explain.explain(trace, model, finding, con, contract_text, sources,
                                                      cache, offline=not args.online)
                except explain.ExplainError:
                    pass
        stem = os.path.join(args.out, sc["name"])
        with open(stem + ".html", "w", encoding="utf-8") as f:
            f.write(report.render(trace, model, con, findings, topo, os.path.relpath(trace_path), explanations))
        perfetto.write(stem + ".perfetto.json", perfetto.export(trace, model, con.processes, findings))
        mark = "" if ok else "  <-- UNEXPECTED"
        print(f"{sc['name']:<22}{sc['expect']:<10}{got:<6}{', '.join(got_checks) or '-':<34}{sc['story']}{mark}")
    print(f"\nreports in {args.out}/ (open the .html; load .perfetto.json at ui.perfetto.dev)")
    print("all scenarios behaved as expected" if ok_all else "SOME SCENARIOS DID NOT BEHAVE AS EXPECTED")
    return 0 if ok_all else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blueprint")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pairs", help="list MsgSend/MsgReceive/MsgReply transactions")
    p.add_argument("trace", help="path to a .kev file from tracelogger -w")
    p.add_argument("--pid", type=int, default=0, help="only transactions involving this pid")
    p.set_defaults(func=cmd_pairs)
    p = sub.add_parser("topology", help="process-level message graph")
    p.add_argument("trace", help="path to a .kev file from tracelogger -w")
    p.add_argument("--only", default="", help="comma-separated process names to keep")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.set_defaults(func=cmd_topology)
    p = sub.add_parser("check", help="check a trace against an architecture contract")
    p.add_argument("trace", help="path to a .kev file from tracelogger -w")
    p.add_argument("--contract", "-c", default="blueprint.yaml", help="contract file")
    p.add_argument("--json", default="", help="also write the report as JSON to this file")
    p.add_argument("--perfetto", default="", help="also write a Perfetto (Chrome JSON) trace to this file")
    p.add_argument("--html", default="", help="also write a single-file HTML report to this file")
    p.add_argument("--markdown", default="", help="append a Markdown summary to this file (e.g. $GITHUB_STEP_SUMMARY)")
    p.add_argument("--junit", default="", help="also write JUnit XML, one test per check, to this file")
    p.add_argument("--snapshot", default="", help="pidin output taken when the trace stopped "
                   "(default: <trace>.pidin next to the trace, if present)")
    p.add_argument("--explain", action="store_true", help="explain each error with Claude (cached)")
    p.add_argument("--source", action="append", default=[], help="source file to show Claude (repeatable)")
    p.add_argument("--offline", action="store_true", help="only use cached explanations, never call the API")
    p.add_argument("--cache", default="", help="explanation cache directory (default: next to the contract)")
    p.add_argument("--secpol", default="", help="also compare this security policy (text form) with the contract")
    p.add_argument("--type", action="append", default=[], metavar="PROCESS=TYPE",
                   help="security type of a process if not <process>_t (repeatable)")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("init", help="draft a contract from a recorded trace")
    p.add_argument("trace", help="path to a .kev file from tracelogger -w")
    p.add_argument("--only", default="", help="comma-separated processes to put in the contract "
                   "(default: application processes that message each other)")
    p.add_argument("--all", action="store_true", help="keep QNX services too (procnto, devb-*, io-sock, ...), "
                   "to map a system as it is")
    p.add_argument("--system", default="my-system", help="system name for the contract")
    p.add_argument("--out", "-o", default="", help="write the draft here instead of stdout")
    p.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    p.add_argument("--review", action="store_true", help="ask Claude which edges of the draft look "
                   "undesigned; marks go into the draft as comments (cached, needs an API key the first time)")
    p.add_argument("--source", action="append", default=[], help="--review: source file to show Claude (repeatable)")
    p.add_argument("--offline", action="store_true", help="--review: only use a cached review")
    p.add_argument("--cache", default="", help="--review: cache directory (default: explanations/ next to the trace)")
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("secpol", help="write QNX security policy channel rules from the contract, "
                       "or compare a policy with it")
    p.add_argument("--contract", "-c", default="blueprint.yaml", help="contract file")
    p.add_argument("--attach", action="append", default=[], metavar="PROCESS=PATH",
                   help="path a server registers, e.g. nav=/dev/name/local/cockpit-nav (repeatable)")
    p.add_argument("--type", action="append", default=[], metavar="PROCESS=TYPE",
                   help="security type of a process if not <process>_t (repeatable)")
    p.add_argument("--out", "-o", default="", help="write the policy here instead of stdout")
    p.add_argument("--check", default="", metavar="POLICY",
                   help="compare this policy (text form) with the contract instead of writing one")
    p.set_defaults(func=cmd_secpol)
    p = sub.add_parser("record", help="record a trace on a running QNX target over ssh, "
                       "optionally killing processes, then check it")
    p.add_argument("host", help="ssh destination, e.g. root@192.168.1.20")
    p.add_argument("--out", "-o", default="run.kev", help="local trace file (.pidin and .inject.json go next to it)")
    p.add_argument("--duration", "-d", type=float, default=5.0, help="trace length in seconds (default 5)")
    p.add_argument("--inject", action="append", default=[], metavar="NAME[ARGS]@SECONDS[:stop]",
                   help="kill this process with SIGKILL this many seconds into the trace, or with "
                        ":stop freeze it with SIGSTOP and continue it after the trace (repeatable). "
                        "NAME[ARGS] picks only instances whose command line contains ARGS, "
                        "e.g. 'io-sock[prefix=/can]@2:stop'")
    p.add_argument("--start", default="", help="shell command that starts the system inside the trace "
                   "(default: attach to the system already running)")
    p.add_argument("--contract", "-c", default="", help="check the trace against this contract when done")
    p.add_argument("--no-check", action="store_true", help="only record, even if --contract is given")
    p.add_argument("--ssh", default=record.SSH_DEFAULT, help=f"ssh command (default: {record.SSH_DEFAULT!r})")
    p.add_argument("--buffers", type=int, default=0, help="tracelogger -k: kernel trace buffers per CPU (raise it if a trace loses events)")
    p.add_argument("--json", default="", help="check: also write the report as JSON")
    p.add_argument("--perfetto", default="", help="check: also write a Perfetto trace")
    p.add_argument("--html", default="", help="check: also write an HTML report")
    p.add_argument("--junit", default="", help="check: also write JUnit XML")
    p.set_defaults(func=cmd_record)
    p = sub.add_parser("mcp", help="run Blueprint as an MCP server on stdio, for coding assistants "
                       "(needs pip install 'qnx-blueprint[mcp]')")
    p.set_defaults(func=cmd_mcp)
    p = sub.add_parser("demo", help="replay the recorded cockpit scenarios (no QNX target needed)")
    p.add_argument("--scenarios", default=DEMO_DEFAULT)
    p.add_argument("--out", default="out")
    p.add_argument("--online", action="store_true", help="call Claude for explanations missing from the cache")
    p.set_defaults(func=cmd_demo)
    return parser


if __name__ == "__main__":
    sys.exit(main())
