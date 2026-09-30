# Runtime hooks copied from affaan-m/ECC

- Repo: https://github.com/affaan-m/ECC (MIT, see LICENSE). Version 2.2.2, commit c70874fae9eb0e5ad0365beb7e2955899fd1d30f.
- Copied unmodified: only the hook scripts NDC registers and their transitive `require` closure (28 files).
- Audited before copying: no network modules, no `fetch`. Child processes: `git`, `which`/`where`, and `claude`
  (lib/llm-summary.js, disabled by ECC_SKIP_LLM_SUMMARY, which NDC always sets).
- Not copied on purpose: gateguard (blocks edits), mcp-health-check and plan-canvas (network/browser),
  observe-runner (spawns `claude` in the background), PostToolUse dispatchers (run `npx`), desktop-notify.
- `hooks.json` here is NDC's own registry (which hook, on which event, in which profile).
