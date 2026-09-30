"""Deterministic security scan: secrets and risky code in project files, and an audit of the Claude Code
configuration (settings, hooks, MCP servers, CLAUDE.md, rules, agents) for injection and exfiltration risks.

Pure Python, offline, no tokens. Heuristics: a clean scan is not a proof of safety. Suppress a false positive
on one line with the comment `ndc:allow-secret`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .snapshot import MAX_BYTES, MAX_FILES, SKIP_DIRS

SEV = {"high": 3, "medium": 2, "low": 1}
SUPPRESS = "ndc:allow-secret"

# provider-specific formats: a match is almost certainly a real credential
SECRET_RULES = [
    ("aws-access-key", "high", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", "high", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----")),
    ("github-token", "high", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("anthropic-key", "high", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", "high", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("slack-token", "high", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("stripe-live-key", "high", re.compile(r"\b[sr]k_live_[0-9a-zA-Z]{20,}")),
    ("google-api-key", "high", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
]
GENERIC_SECRET = re.compile(
    r"""(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|passw(?:or)?d|auth[_-]?token|access[_-]?token|token)\b"""
    r"""\s*[:=]\s*["']([^"'\s]{12,})["']""")
PLACEHOLDER = ("example", "your", "xxx", "changeme", "placeholder", "dummy", "sample", "test", "<", ">", "${", "{{",
               "%(", "process.env", "os.environ", "getenv", "secrets.", "redacted", "todo")

CODE_RULES = [
    ("shell-injection", re.compile(r"""shell\s*=\s*True""") , re.compile(r"""f["']|\.format\(|["']\s*%\s|["']\s*\+|\+\s*["']""")),
    ("shell-injection", re.compile(r"""\bos\.system\(\s*(?:f["']|[^"')]*\+)"""), None),
    ("shell-injection", re.compile(r"""\b(?:exec|execSync)\(\s*`[^`]*\$\{"""), None),
    ("eval-dynamic", re.compile(r"""\beval\(\s*[^"'\s)]"""), None),
    ("sql-concatenation", re.compile(r"""(?i)\b(?:execute|query)\(\s*(?:f["']\s*(?:select|insert|update|delete)\b[^"']*\{|["']\s*(?:select|insert|update|delete)\b[^"']*["']\s*(?:\+|%))"""), None),
    ("unsafe-yaml-load", re.compile(r"""\byaml\.load\((?![^)]*Loader)"""), None),
    ("tls-verification-off", re.compile(r"""\bverify\s*=\s*False\b"""), None),
    ("dom-injection", re.compile(r"""\.innerHTML\s*=\s*(?![\"'`]\s*[;<]|\"\"|'')[^;\n]*[A-Za-z_$]"""), None),
]
CODE_SEVERITY = "medium"

CONFIG_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude/CLAUDE.md")
INJECTION = [
    (re.compile(r"(?i)\bignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?)\b"), "medium"),
    (re.compile(r"(?i)\bdisregard (?:all )?(?:the )?(?:previous|prior|above|your) (?:instructions|rules)\b"), "medium"),
    (re.compile(r"(?i)\bdo not (?:tell|inform|mention (?:this )?to) the user\b"), "medium"),
    (re.compile(r"(?i)\bexfiltrat"), "medium"),
    (re.compile(r"(?i)\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z)?sh\b"), "high"),
]
HIDDEN_UNICODE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩]|(?<!^)﻿")
RISKY_ALLOW = {"rm", "sudo", "curl", "wget", "sh", "bash", "zsh", "eval", "ssh", "scp", "nc", "chmod", "chown", "dd", "mkfs"}
HOOK_RULES = [
    (re.compile(r"(?i)\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z)?sh\b"), "high", "pipe-to-shell", "downloads and runs code"),
    (re.compile(r"(?i)base64\s+(?:-d|--decode)[^\n]*\|\s*(?:ba|z)?sh"), "high", "obfuscated-exec", "decodes and runs a payload"),
    (re.compile(r"(?i)\b(?:curl|wget|nc)\b[^\n]*(?:\s-d\s*@|--data(?:-binary)?\s*@|\s-F\s*\S*@|\s-T\s|--upload-file)"), "high",
     "possible-exfiltration", "sends a local file over the network"),
    (re.compile(r"\beval\s"), "medium", "eval", "evaluates a dynamic string"),
    (re.compile(r"(?:~|\$HOME)/\.(?:ssh|aws|gnupg)\b|\.aws/credentials|\.netrc"), "medium", "credential-access", "touches credential files"),
]


@dataclass
class Finding:
    severity: str
    rule: str
    file: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.file}:{self.line} {self.rule}: {self.message}"


def _is_placeholder(value: str) -> bool:
    v = value.lower()
    return any(p in v for p in PLACEHOLDER) or len(set(v)) <= 2


def scan_text(text: str, name: str, code: bool = True) -> list[Finding]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if SUPPRESS in line or len(line) > 2000:
            continue
        for rule, sev, rx in SECRET_RULES:
            if rx.search(line):
                out.append(Finding(sev, rule, name, n, "looks like a real credential"))
        m = GENERIC_SECRET.search(line)
        if m and not _is_placeholder(m.group(1)) and not any(f.line == n for f in out):
            out.append(Finding("medium", "hardcoded-secret", name, n, "a secret-like name assigned a literal value"))
        if code:
            for rule, rx, also in CODE_RULES:
                if rx.search(line) and (also is None or also.search(line)):
                    out.append(Finding(CODE_SEVERITY, rule, name, n, "risky pattern (heuristic, check the context)"))
    return out


def _text_files(root: Path):
    import os
    count = 0
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".lock", ".min.js", ".map", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2")) \
                    or f in ("package-lock.json", "yarn.lock", "go.sum", "pnpm-lock.yaml", ".DS_Store"):
                continue
            count += 1
            if count > MAX_FILES:
                return
            yield (Path(dp) / f).relative_to(root)


def scan_files(root: Path, files=None) -> list[Finding]:
    """Secrets and risky code patterns. `files` (relative paths) narrows it, e.g. to what a task touched."""
    root, out = Path(root), []
    for rel in (files if files is not None else _text_files(root)):
        p = root / rel
        try:
            if not p.is_file() or p.stat().st_size > MAX_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        out += scan_text(data.decode("utf-8", "replace"), str(rel))
    return out


def _managed(root: Path) -> dict:
    from .activator import read_manifest
    return read_manifest(root)


def scan_config(root: Path, include_managed: bool = False) -> list[Finding]:
    root, out = Path(root), []
    m = {"agents": [], "skills": [], "rules": []} if include_managed else _managed(root)
    claude = root / ".claude"

    def md(p: Path):
        try:
            text = p.read_text(errors="replace")
        except OSError:
            return
        rel = str(p.relative_to(root))
        for n, line in enumerate(text.splitlines(), 1):
            if HIDDEN_UNICODE.search(line):
                out.append(Finding("high", "hidden-unicode", rel, n, "invisible characters: a known prompt-injection trick"))
            for rx, sev in INJECTION:
                if rx.search(line):
                    out.append(Finding(sev, "prompt-injection-pattern", rel, n, "instruction-like text aimed at the model"))
        out.extend(f for f in scan_text(text, rel, code=False))

    for name in CONFIG_FILES:
        if (root / name).is_file():
            md(root / name)
    if claude.is_dir():
        for p in sorted((claude / "rules").rglob("*.md")) if (claude / "rules").is_dir() else []:
            if not str(p.relative_to(claude / "rules")).startswith("ndc/") or include_managed:
                md(p)
        if (claude / "agents").is_dir():
            for p in sorted((claude / "agents").glob("*.md")):
                if p.stem not in m.get("agents", []):
                    md(p)
        if (claude / "skills").is_dir():
            for p in sorted((claude / "skills").glob("*/SKILL.md")):
                if p.parent.name not in m.get("skills", []):
                    md(p)
        for name in ("settings.json", "settings.local.json"):
            if (claude / name).is_file():
                out += _audit_settings(claude / name, root)
    if (root / ".mcp.json").is_file():
        out += _audit_mcp(root / ".mcp.json", root)
    return out


def _load(p: Path, root: Path, out: list):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        out.append(Finding("medium", "invalid-json", str(p.relative_to(root)), 1, f"cannot parse: {e}"))
        return None


def _audit_settings(p: Path, root: Path) -> list[Finding]:
    out, rel = [], str(p.relative_to(root))
    d = _load(p, root, out)
    if not isinstance(d, dict):
        return out
    perms = d.get("permissions") if isinstance(d.get("permissions"), dict) else {}
    allow = [a for a in perms.get("allow", []) if isinstance(a, str)]
    for a in allow:
        norm = a.replace(" ", "")
        if norm in ("Bash", "Bash(*)", "Bash(**)", "*", "Bash(:*)"):
            out.append(Finding("high", "broad-allow", rel, 1, f"permission `{a}` lets Claude run any shell command unasked"))
        else:
            m = re.match(r"Bash\(([A-Za-z0-9_.\-]+)[ :]", a)
            if m and m.group(1) in RISKY_ALLOW:
                out.append(Finding("medium", "risky-allow", rel, 1, f"permission `{a}` pre-approves a dangerous command"))
    if perms.get("defaultMode") == "bypassPermissions":
        out.append(Finding("high", "bypass-permissions", rel, 1, "defaultMode bypassPermissions disables all permission prompts"))
    if allow and not perms.get("deny"):
        out.append(Finding("low", "no-deny-list", rel, 1, "allow rules without any deny rules"))
    for k, v in (d.get("env") or {}).items() if isinstance(d.get("env"), dict) else []:
        if isinstance(v, str) and any(rx.search(v) for _, _, rx in SECRET_RULES):
            out.append(Finding("high", "secret-in-settings", rel, 1, f"env `{k}` holds a real-looking credential"))
    for event, groups in (d.get("hooks") or {}).items() if isinstance(d.get("hooks"), dict) else []:
        for g in groups if isinstance(groups, list) else []:
            for h in (g.get("hooks", []) if isinstance(g, dict) else []):
                cmd = h.get("command", "") if isinstance(h, dict) else ""
                for rx, sev, rule, why in HOOK_RULES:
                    if isinstance(cmd, str) and rx.search(cmd):
                        out.append(Finding(sev, f"hook-{rule}", rel, 1, f"{event} hook {why}: {cmd[:80]}"))
    return out


def _audit_mcp(p: Path, root: Path) -> list[Finding]:
    out, rel = [], str(p.relative_to(root))
    d = _load(p, root, out)
    servers = d.get("mcpServers") if isinstance(d, dict) else None
    for name, s in (servers or {}).items() if isinstance(servers, dict) else []:
        if not isinstance(s, dict):
            continue
        cmd, args = str(s.get("command", "")), [a for a in s.get("args", []) if isinstance(a, str)]
        if cmd in ("npx", "uvx", "pnpx", "bunx"):
            pkg = next((a for a in args if not a.startswith("-")), "")
            if pkg and not re.search(r"@[\dv~^]", pkg.lstrip("@")):
                out.append(Finding("medium", "unpinned-package", rel, 1, f"server `{name}` runs `{cmd} {pkg}` with no pinned version (supply chain)"))
        if cmd in ("sh", "bash", "zsh") and "-c" in args:
            out.append(Finding("medium", "shell-server", rel, 1, f"server `{name}` starts through `{cmd} -c`"))
        url = str(s.get("url", ""))
        if url.startswith("http://") and not re.match(r"http://(?:localhost|127\.0\.0\.1|\[::1\])", url):
            out.append(Finding("medium", "plain-http", rel, 1, f"server `{name}` talks to {url} without TLS"))
        for k, v in (s.get("env") or {}).items() if isinstance(s.get("env"), dict) else []:
            if isinstance(v, str) and any(rx.search(v) for _, _, rx in SECRET_RULES):
                out.append(Finding("high", "secret-in-mcp-env", rel, 1, f"server `{name}` env `{k}` holds a real-looking credential"))
    return out


def scan(root: Path, files=None, config: bool = True, include_managed: bool = False) -> list[Finding]:
    found = scan_files(root, files) + (scan_config(root, include_managed) if config else [])
    return sorted(found, key=lambda f: (-SEV[f.severity], f.file, f.line))


def at_least(findings: list[Finding], severity: str) -> list[Finding]:
    return [f for f in findings if SEV[f.severity] >= SEV[severity]]


def touches_config(paths) -> bool:
    return any(p in CONFIG_FILES or p == ".mcp.json" or p.startswith(".claude/settings") or p.startswith(".claude/agents/")
               or p.startswith(".claude/rules/") for p in paths)
