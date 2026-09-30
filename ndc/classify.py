"""Deterministic floor for complexity and risk. No LLM tokens spent.

The PO may raise a classification, never lower it below this floor.
"""
from __future__ import annotations

import re

ORDER = ["S", "M", "L", "XL"]
# Whole-word patterns: "tokenize" (NLP) or "author" (docs) must not look like security work.
RISK_RE = re.compile("|".join([
    r"\bauth(?:entic\w*|oriz\w*)?\b", r"\blogin\b", r"\bpasswords?\b", r"\btokens?\b", r"\bsecrets?\b",
    r"\bcrypto\w*", r"\bpayments?\b", r"\bbilling\b", r"\bpermissions?\b", r"\bsecurity\b",
    r"\bmigrations?\b", r"\bschemas?\b", r"\bdelete\b", r"\bdrop\s+table\b", r"\bconcurren\w+",
    r"\brace\s+condition", r"\bdeploy\w*",
    r"\bautentic\w+", r"\bsenhas?\b", r"\bpagamentos?\b", r"\bcobran\w+", r"\bpermiss\w+",
    r"\bseguran\w+", r"\bmigra\w+", r"\bapagar\b", r"\bexclu\w+", r"\bconcorr\w+", r"\bimplanta\w*",
]), re.I)
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
    hit = RISK_RE.search(text)
    risk = "high" if hit else "low"
    if hit:
        floor, reasons = _max(floor, "M"), reasons + [f"risk keyword ({hit.group(0)}) -> at least M, never junior for work"]
    final = _max(complexity, floor) if complexity else floor if floor != "S" else "M"
    if complexity and final != complexity:
        reasons.append(f"raised {complexity} -> {final}")
    return final, risk, reasons
