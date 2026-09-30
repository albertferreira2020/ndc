# NDC architecture

```
goal -> `ndc plan`: PO (opus, read-only) -> JSON -> validate -> approval -> activate domains + queue (SQLite) -> runner loop
                                              |
              usage bridge -> guardian: GO / WIND_DOWN / STOP
                                              |
                       router: junior haiku | senior sonnet | po opus
                                              |
                          claude -p --model X  (one task per call)
                                              |
                     record usage delta -> history -> better estimates
STOP -> handoff report -> (wait for reset) -> next task
```

## Decisions

- **Vendored ECC, untouched.** Domains reference ECC agents and skills by name, so upstream updates are a re-copy. `tests/test_ndc.py::test_every_catalog_reference_exists` fails if an update removes something a domain uses.
- **Model per level, applied at activation.** ECC agents ship with a fixed `model:`. On activation NDC rewrites it to the level defined in the domain (`po`/`senior`/`junior`) and `ndc.config.json`.
- **One task per `claude -p` call.** A stop between tasks is always clean. This is why the PO must split work into small tasks.
- **Fail closed on unknown usage.** Stale or missing data stops the runner instead of guessing.
- **Dry run by default.** `ndc run` never dispatches without `--execute`.

## Parallel tasks and DevFleet

`ndc run --parallel N` is implemented natively (`ndc/parallel.py`, `ndc/worktree.py`): a thread per task, each with its own SQLite connection, git worktree and branch; git operations that touch the main tree are serialized by one lock; the budget guard reserves the estimated cost of running tasks. ECC's `claude-devfleet` skill documents a separate server (port 18801) that ECC does not ship, so there was nothing to copy. The skill is still installed for the software domain.

## What comes from ECC and what is NDC's own

| Capability | Source |
|---|---|
| Agents, skills | ECC, copied unmodified (`vendor/ecc`) |
| Rules, slash commands | ECC, copied unmodified, installed per stack or domain |
| MCP configs | ECC, copied unmodified; `ndc mcp add` merges them |
| Dashboard | NDC's own (ECC's is a separate tool, not vendored) |
| Hook scripts and memory persistence | ECC, audited subset copied unmodified (`runtime/`); registry, installer and environment are NDC's |
| Security scan | NDC's own (ECC's needs `npx ecc-agentshield`) |
| Parallel worktrees | NDC's own (DevFleet is an external server) |
| Budget guard, queue, routing, PO, gates, handoff | NDC's own |

## Open items

1. Watch for changes in the `/usage` output format (parser is strict).
2. Calibrating default costs with real runs.
3. Parallel execution (worktrees) with a shared budget.
4. Only the software domain is supported; the others were removed.
