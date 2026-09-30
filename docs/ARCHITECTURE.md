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

## DevFleet (ECC `claude-devfleet`)

Kept in the `software` domain as a skill. It needs a separate server on port 18801 and is not wired into the runner: the NDC runner already provides a DAG (task dependencies) and per-task model choice. Worktree isolation and parallel agents are what DevFleet would add; integrate it once the sequential loop is proven.

## Open items

1. Watch for changes in the `/usage` output format (parser is strict).
2. Calibrating default costs with real runs.
3. Parallel execution (worktrees) with a shared budget.
4. Domain-specific agents for marketing, research etc. (ECC has few; expect to write them).
