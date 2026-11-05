"""What the command line does, as functions: for the MCP server and for scripts.

    from blueprint import api
    result = api.check("run.kev", "blueprint.yaml")   # the dict `check --json` writes
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from . import checks, contract, init_contract, ipc, kev, secpol, snapshot, topology


def stem(trace_path: str) -> str:
    """run.kev or run.kev.gz -> run"""
    for ext in (".kev.gz", ".kev"):
        if trace_path.endswith(ext):
            return trace_path[: -len(ext)]
    return os.path.splitext(trace_path)[0]


def run(trace_path: str, contract_path: str, snapshot_path: str = "", policy_path: str = "",
        types: Optional[Dict[str, str]] = None
        ) -> Tuple[kev.Trace, contract.Contract, List[checks.Finding], topology.Topology, Optional[str]]:
    """Load the files and run every check. Raises OSError, ContractError,
    KevFormatError or PolicyError for a file that cannot be used, and
    FileNotFoundError for a snapshot that was asked for and is missing."""
    con = contract.load(contract_path)
    trace = kev.read(trace_path)
    snap, used = None, None
    path = snapshot_path or stem(trace_path) + ".pidin"
    if os.path.exists(path):
        snap, used = snapshot.load(path), path
    elif snapshot_path:
        raise FileNotFoundError(f"snapshot {snapshot_path} not found")
    findings, topo = checks.run(trace, con, snap)
    if policy_path:
        with open(policy_path, encoding="utf-8") as f:
            findings += secpol.compare(con, secpol.parse(f.read()), policy_path, types or {})
    return trace, con, findings, topo, used


def result(con: contract.Contract, trace_path: str, contract_path: str, findings: List[checks.Finding],
           topo: topology.Topology, explanations: Optional[dict] = None) -> dict:
    """The report as plain data: what `check --json` writes."""
    explanations = explanations or {}
    errors = [f for f in findings if f.severity == checks.ERROR]
    return {
        "system": con.system, "trace": trace_path, "contract": contract_path,
        "result": "fail" if errors else "pass",
        "summary": {"errors": len(errors), "warnings": len(findings) - len(errors)},
        "findings": [dict(f.to_json(), explanation=explanations[i].to_json() if i in explanations else None)
                     for i, f in enumerate(findings)],
        "topology": topo.to_json(),
    }


def check(trace_path: str, contract_path: str, snapshot_path: str = "", policy_path: str = "") -> dict:
    trace, con, findings, topo, _ = run(trace_path, contract_path, snapshot_path, policy_path)
    return result(con, trace_path, contract_path, findings, topo)


def draft_contract(trace_path: str, only: Optional[List[str]] = None, include_system: bool = False,
                   system: str = "") -> str:
    """A first contract from what the trace shows (`blueprint init`)."""
    trace = kev.read(trace_path)
    model = ipc.build(trace)
    processes, _left_out = init_contract.select(model, only, include_system=include_system)
    if not processes:
        raise ValueError("no two application processes exchange messages in this trace; name them with only")
    return init_contract.draft(trace, model, processes, os.path.basename(trace_path), system)


def write_policy(contract_path: str, attach: Optional[Dict[str, List[str]]] = None,
                 types: Optional[Dict[str, str]] = None) -> str:
    con = contract.load(contract_path)
    return secpol.generate(con, attach or {}, types or {}, os.path.basename(contract_path))


def check_policy(policy_path: str, contract_path: str, types: Optional[Dict[str, str]] = None) -> dict:
    con = contract.load(contract_path)
    with open(policy_path, encoding="utf-8") as f:
        findings = secpol.compare(con, secpol.parse(f.read()), policy_path, types or {})
    errors = [f for f in findings if f.severity == checks.ERROR]
    return {"policy": policy_path, "contract": contract_path, "result": "fail" if errors else "pass",
            "findings": [f.to_json() for f in findings]}
