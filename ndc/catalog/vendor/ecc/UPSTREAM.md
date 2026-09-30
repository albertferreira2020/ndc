# Upstream: affaan-m/ECC

- Repo: https://github.com/affaan-m/ECC
- Version: 2.2.2
- Commit: c70874fae9eb0e5ad0365beb7e2955899fd1d30f (2026-09-29)
- License: MIT (see LICENSE)
- Vendored, unmodified: `agents/`, `skills/`, `rules/`, `commands/`, `mcp-configs/`, `.mcp.json`, `contexts/`, `schemas/`, `scaffolds/`, `manifests/`, `plugins/`, `.claude-plugin/`, `integrations/`, `legacy-command-shims/`, `ecc2/` (Rust, not built), `install.sh`, `install.ps1`, `SOUL.md`, `COMMANDS-QUICK-REF.md`, the three guides, and the other harness folders under `harnesses/` (from the repo's dot-folders: `.cursor` -> `harnesses/cursor`, ...).
- Not vendored: `ecc_dashboard.py` (NDC has its own `ndc dashboard`).

Do not edit files here. NDC customizations live in `core/` and `domains/`.
To update: re-copy `agents/` and `skills/` from a fresh clone and update this file.
