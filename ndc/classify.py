"""Deterministic floor for complexity and risk. No LLM tokens spent.

The PO may raise a classification, never lower it below this floor.
"""
from __future__ import annotations

import re

ORDER = ["S", "M", "L", "XL"]
RISK_WORDS = ("auth", "login", "password", "token", "secret", "crypto", "payment", "billing", "permission",
              "security", "migration", "schema", "delete", "drop ", "concurren", "race condition", "deploy",
              "autentic", "senha", "pagamento", "cobran", "permiss", "segurança", "seguranca", "migra", "apagar",
              "exclu", "concorr", "implanta")
BIG_WORDS = ("refactor", "migrate", "redesign", "architecture", "rewrite", "integrate", "across",
             "refator", "reescrev", "arquitetura", "integra")
PATH_RE = re.compile(r"[\w./-]+\.\w{1,5}\b")


def _max(a: str, b: str) -> str:
    return ORDER[max(ORDER.index(a), ORDER.index(b))]


def assess(title: str, desc: str = "", complexity: str | None = None):
    """Returns (complexity, risk, reasons)."""
    text = f"{title}\n{desc}".lower()
    floor, reasons = "S", []
    files = {m for m in PATH_RE.findall(text) if "/" in m or m.count(".") == 1}
    if len(files) >= 6:
        floor, reasons = _max(floor, "L"), reasons + [f"{len(files)} files mentioned -> at least L"]
    elif len(files) >= 3:
        floor, reasons = _max(floor, "M"), reasons + [f"{len(files)} files mentioned -> at least M"]
    if any(w in text for w in BIG_WORDS):
        floor, reasons = _max(floor, "L"), reasons + ["structural keyword -> at least L"]
    if len(desc) > 1200:
        floor, reasons = _max(floor, "M"), reasons + ["long description -> at least M"]
    hits = [w.strip() for w in RISK_WORDS if w in text]
    risk = "high" if hits else "low"
    if hits:
        floor, reasons = _max(floor, "M"), reasons + [f"risk keyword ({hits[0]}) -> at least M, never junior for work"]
    final = _max(complexity, floor) if complexity else floor if floor != "S" else "M"
    if complexity and final != complexity:
        reasons.append(f"raised {complexity} -> {final}")
    return final, risk, reasons
