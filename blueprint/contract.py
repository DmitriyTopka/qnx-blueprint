"""The architecture contract: what the system is supposed to look like.

A contract is a small YAML file, for example:

    system: cockpit
    processes: [gps, nav, camera, logger, display]
    allow:                      # who may send messages to whom
      - display -> nav
      - display -> camera
      - nav -> gps
    reply_budget_ms:            # how long a server may keep a client waiting
      nav: 50
      default: 100
    isolation:                  # if this process dies or hangs ...
      camera:
        must_survive: [display, nav]   # ... these keep working
        min_progress: 3                 # completed messages after the fault
    priority_inversion_ms: 2    # experimental: a waiting client may lose at most
                                # this much time to lower priority work

Only the processes listed under `processes` are checked; messages to system
servers (procnto, pipe, devb-*) are outside the contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import yaml

ARROW = re.compile(r"[ \t]*([\w.-]+)[ \t]*->[ \t]*([\w.-]+)[ \t]*")
TOP_KEYS = {"system", "processes", "allow", "reply_budget_ms", "isolation", "priority_inversion_ms"}
ISOLATION_KEYS = {"must_survive", "min_progress"}


class ContractError(ValueError):
    pass


@dataclass
class Isolation:
    process: str
    must_survive: List[str]
    min_progress: int = 3


@dataclass
class Contract:
    system: str
    processes: List[str]
    allow: Set[Tuple[str, str]]
    reply_budget_ms: Dict[str, float] = field(default_factory=dict)
    isolation: Dict[str, Isolation] = field(default_factory=dict)
    source: str = ""
    priority_inversion_ms: Optional[float] = None   # None: check is off

    def budget_for(self, server: str) -> float | None:
        if server in self.reply_budget_ms:
            return self.reply_budget_ms[server]
        return self.reply_budget_ms.get("default")


def load(path: str) -> Contract:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    contract = parse(yaml.safe_load(text) or {})
    contract.source = path
    return contract


def parse(doc: dict) -> Contract:
    if not isinstance(doc, dict):
        raise ContractError("contract must be a mapping")
    unknown = set(doc) - TOP_KEYS
    if unknown:
        raise ContractError(f"unknown keys: {', '.join(sorted(unknown))}")

    processes = doc.get("processes") or []
    if not processes or not all(isinstance(p, str) for p in processes):
        raise ContractError("processes: expected a non-empty list of process names")
    known = set(processes)

    allow: Set[Tuple[str, str]] = set()
    for item in doc.get("allow") or []:
        m = ARROW.fullmatch(str(item))
        if not m:
            raise ContractError(f"allow: expected 'client -> server', got {item!r}")
        client, server = m.groups()
        for name in (client, server):
            if name not in known:
                raise ContractError(f"allow: {name} is not listed under processes")
        allow.add((client, server))

    budgets: Dict[str, float] = {}
    for name, value in (doc.get("reply_budget_ms") or {}).items():
        if name != "default" and name not in known:
            raise ContractError(f"reply_budget_ms: {name} is not listed under processes")
        try:
            budgets[name] = float(value)
        except (TypeError, ValueError):
            raise ContractError(f"reply_budget_ms.{name}: expected a number") from None

    isolation: Dict[str, Isolation] = {}
    for name, rule in (doc.get("isolation") or {}).items():
        if name not in known:
            raise ContractError(f"isolation: {name} is not listed under processes")
        rule = rule or {}
        unknown = set(rule) - ISOLATION_KEYS
        if unknown:
            raise ContractError(f"isolation.{name}: unknown keys: {', '.join(sorted(unknown))}")
        survivors = rule.get("must_survive") or []
        for s in survivors:
            if s not in known:
                raise ContractError(f"isolation.{name}.must_survive: {s} is not listed under processes")
        isolation[name] = Isolation(name, list(survivors), int(rule.get("min_progress", 3)))

    inversion = doc.get("priority_inversion_ms")
    if inversion is not None:
        try:
            inversion = float(inversion)
        except (TypeError, ValueError):
            raise ContractError("priority_inversion_ms: expected a number") from None
        if inversion < 0:
            raise ContractError("priority_inversion_ms: must not be negative")
    return Contract(str(doc.get("system", "")), list(processes), allow, budgets, isolation,
                    priority_inversion_ms=inversion)
