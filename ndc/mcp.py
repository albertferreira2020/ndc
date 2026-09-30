"""Ready-made MCP server configs from ECC (`vendor/ecc/mcp-configs`), merged into a project's .mcp.json on request."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import catalog_root


def available() -> dict:
    d = json.loads((catalog_root() / "vendor/ecc/mcp-configs/mcp-servers.json").read_text())["mcpServers"]
    return {k: v for k, v in d.items() if isinstance(v, dict)}


def add(target: Path, names: list[str]) -> list[str]:
    """Adds servers to <target>/.mcp.json. Refuses unknown names and never overwrites an existing entry."""
    src = available()
    bad = [n for n in names if n not in src]
    if bad:
        raise KeyError(f"unknown MCP server(s): {bad}. See `ndc mcp list`")
    p = target / ".mcp.json"
    try:
        data = json.loads(p.read_text()) if p.exists() else {}
    except ValueError as e:
        raise ValueError(f"{p} is not valid JSON ({e}); NDC will not touch it") from e
    servers = data.setdefault("mcpServers", {})
    added = []
    for n in names:
        if n in servers:
            continue
        servers[n] = {k: v for k, v in src[n].items() if k != "description"}
        added.append(n)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)
    return added
