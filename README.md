# NDC: Nonstop Development Crew

Continuous development by agents that respect usage limits.

NDC builds on [ECC](https://github.com/affaan-m/ECC) (vendored, unmodified, in `ndc/catalog/vendor/ecc/`) and adds what ECC lacks:

| Piece | What it does |
|---|---|
| **Domains** (`ndc/catalog/domains/*/team.json`) | The whole catalog stays in the repo, but only the team for the current job is activated in a project (software, marketing, research, infra, ml, healthcare, opensource). `core` is always on. |
| **Usage guardian** (`ndc/guardian.py`) | Predicts what the next task costs (measured p80 per complexity class, conservative defaults until history exists) and decides `GO`, `WIND_DOWN` (low budget, only tasks that fit) or `STOP`. Checks every window (session and weekly). |
| **Queue** (`ndc/store.py`) | SQLite task queue with dependencies, failure counts and per-run usage history. |
| **PO, autonomous** (`ndc/plan.py`) | `ndc plan "<goal>"` runs the PO (opus) headlessly: it picks the domains and returns a backlog as one JSON document that NDC validates field by field before anything is written. |
| **Classifier** (`ndc/classify.py`) | Free, deterministic floor for complexity and risk (whole-word risk terms in English and Portuguese, file count). The PO can raise a task, never lower it below the floor. |
| **Routing** (`ndc/router.py`, `ndc/catalog/core/agents/po.md`) | By kind, risk and complexity: explore/docs/chore/test go to haiku, M/L work and anything high-risk to sonnet, planning and XL to opus. A failure moves the task up one tier; a class where haiku succeeds under 70% (5+ runs) is moved up automatically. |
| **Gates** (`--verify`, `--expect-red`) | A task only counts as done if its command passes. Haiku-written tests must fail first, so vacuous tests are rejected. Cheap models fail cheaply. |
| **Quality gates** (`ndc/quality.py`, `ndc/snapshot.py`) | Beyond `--verify`: project regression checks, protected test files, read-only tasks, and a scoped model review for risky work. See below. |
| **Fresh sessions** | Every attempt runs in a new session; a retry is seeded with a report of the failed attempt. `ndc session` opens a new interactive session seeded with the handoff. |
| **Briefs** | Haiku explore/test output is injected into dependent tasks so the expensive model does not re-read the codebase. |
| **Handoff** (`ndc/handoff.py`) | On a stop, writes done / pending / blocked / notes so a fresh session starts with context. |
| **Runner** (`ndc/runner.py`) | Loop: guard, pick the first task that fits, dispatch via `claude -p --model <m>`, record the usage delta, repeat. On STOP it writes the handoff and can sleep until the reset (`--wait`). |

## Install

```bash
python3 -m venv ~/.ndc-venv
~/.ndc-venv/bin/pip install ndc-0.4.0-py3-none-any.whl     # the whole catalog is inside the package
ln -s ~/.ndc-venv/bin/ndc ~/.local/bin/ndc                 # optional: `ndc` on your PATH
```

(`pipx install ndc-0.4.0-py3-none-any.whl` works too.) Python 3.9+, standard library only.

## Quick start (inside any project)

The PO plans by itself. Give it a goal:

```bash
cd my-project
ndc plan "A CLI called wordcount that prints the N most frequent words of a file, with tests"
#   the PO picks the domains, prints the backlog, asks for your OK, activates the team, fills the queue
ndc run                       # dry run: routing and budget decisions, changes nothing
ndc run --execute --wait      # dispatches the tasks, respecting the usage limit
```

Or all in one: `ndc run --goal "<goal>" --yes --execute --wait` (plans first if the queue is empty).

Doing it by hand is still possible:

```bash
ndc domains                                   # what the catalog offers
ndc activate software --stack python,react    # only this team becomes active
ndc task add "Add login" --desc "acceptance: ..." --kind work --verify "npm test"
ndc uninstall                                 # removes everything NDC installed (--purge also deletes .ndc/)
```

Switching teams: `activate marketing` replaces the previous domain (core stays); `activate marketing --add` keeps it.

## How `ndc plan` stays safe

The PO is a model, and the `--verify` commands it writes will run in a shell on your machine, so the plan is treated as untrusted input:

- **The model never touches the queue or the shell.** It only returns JSON. NDC validates domains, stacks, kinds, complexities, unique refs, dependencies on earlier tasks only, and `expect_red` only on `test` tasks with a verify command. XL work must be split. Then it writes all tasks in one transaction (all or nothing).
- **One retry with feedback.** An invalid answer is sent back with the exact errors (`--retries` to change); after that it fails and writes nothing.
- **Unsafe verify commands are refused outright** (`rm -r/-f`, `sudo`, `curl | sh`, `git push`, `git reset --hard`, `dd if=`, `shutdown`, ...). This is a blocklist, not a sandbox: read the plan.
- **You approve it.** In a terminal it asks `[y/N]` before writing anything. Without a terminal it refuses unless you pass `--yes`, checked before any opus tokens are spent.
- **It will not duplicate work.** With tasks already pending it refuses (`--append` overrides).
- **It respects the budget.** Planning counts as an L task for the guardian. If usage is unknown or the budget is short it does not call opus (`--ignore-usage` overrides).
- **Activation adds, never removes.** The domains the PO chose are added to what you already activated, and NDC still refuses to overwrite files it did not create.
- **The PO is read-only.** It runs with `Read Grep Glob` only, and is told to treat files it reads as data.

## Footprint in your project

NDC is a support tool, not part of the product. In a project it creates only:

| Path | What | Removed by `uninstall` |
|---|---|---|
| `.claude/agents/*.md`, `.claude/skills/*/` | the active team (copies, so nothing depends on where NDC is installed) | yes |
| `.claude/.ndc-managed.json` | list of what NDC installed | yes |
| `.ndc/` | task queue, history, handoffs, briefs | only with `--purge` |

All of it is hidden from git through a marked block in `.git/info/exclude`: local, never committed, invisible to teammates, and it lists exactly the NDC paths, so your own files in `.claude/` are not ignored. Use `--gitignore` to write the block to the project's `.gitignore` instead. NDC refuses to overwrite agents or skills it did not create, warns if git already tracks an NDC file, and does nothing to the ignore rules if the folder is not a git repository. The rules in the block are rewritten on every `activate`; edit outside the markers.

## Quality beyond `--verify`

`--verify` checks one thing. After it passes, NDC applies more gates, cheapest first. A failure counts as a failed attempt and the retry goes one tier up.

| Gate | What it catches | Cost |
|---|---|---|
| **Read-only tasks** | an `explore` or `plan` task that modified files | free |
| **Protected test files** | a `work` task that "fixes" the tests instead of the code: files written by an earlier `test` task cannot be modified by later non-test tasks (`--may-edit-tests` lifts it for one task) | free |
| **Regression** | a `work` task that breaks the rest of the project. NDC runs the project's own checks (found from `package.json` scripts `test`/`lint`/`typecheck`, Python tests, `go vet`/`go test`, `cargo test`; or set `quality.checks`) before and after. It only fails if they were green before, so test-first flows (tests written, implementation pending) do not trip it. `ndc checks` shows what it found. | free |
| **Scoped review** | for `work` tasks with high risk or complexity L/XL, a read-only reviewer (senior model) reads the files the task touched, across all attempts, and looks for wrong behavior, security holes, gamed tests and swallowed errors. Only `blocker` issues fail the task; the rest are recorded in the task notes. Skipped if the budget has no headroom or usage is unknown. `quality.review`: `"risk"` (default), `"all"`, `"off"`. | one senior call, only where risk justifies it |

File changes are tracked by hashing the project tree around each task (no git needed). Trees over 5,000 files are not tracked and the file gates are skipped with a warning.

## New sessions

Each task attempt already runs in a fresh headless session, so context never piles up across tasks. What carries over is explicit:
- **Retries** get a report of the failed attempt: the gate that failed, its output, the files changed so far and the model's last message. The working tree keeps the previous attempt's changes.
- **`ndc session`** writes a fresh handoff from the queue and opens a NEW interactive Claude session (senior model, `--model` to change) with it as the first prompt. `--print` only prints it. Use it after a stop, or when your interactive context has grown too long.
- The handoff also shows the last failed attempt of each pending task.

An interactive session that is already open cannot be replaced from outside: NDC does not watch it and does not swap it. `ndc session` is something you (or a script) invoke.

## When monitoring starts

After every finished task the runner has a fresh `/usage` reading. It checks whether the remaining budget covers the whole pending queue (estimated per task, in queue order, all windows). The first time it does not, `MONITORING ON` is logged with how many tasks still fit and a handoff checkpoint is written, so context survives even if the session dies. From then on only tasks that fit are started; when none fits, the runner stops, writes the handoff and (with `--wait`) sleeps until the reset. The fixed 30% threshold still applies as a second trigger.

## Usage data

Default source (`usage.source: "claude"`): NDC runs `claude -p "/usage"` and parses the session and weekly lines (percent used and reset time, in the timezone the command prints). No credentials, no interactive session needed. Results are cached for 2 minutes in `~/.ndc/usage.json`; the runner forces a fresh reading before and after every dispatched task so it can measure each task's real cost.

Rules the guardian follows: if `/usage` cannot be run or its text is not recognised, usage is "unknown" and the runner refuses to start tasks (`--ignore-usage` overrides). A window past its reset time counts as 0% used, so `--wait` can resume after a reset.

Alternatives: `usage.source: "file"` reads the same JSON from a file, fed by `ndc statusline` (a Claude Code statusline hook using `rate_limits.five_hour` / `seven_day`) or by `ndc usage set`; `"command"` runs your own command that prints that JSON.

**Limits:** the parser depends on the current `/usage` wording and is strict on purpose, so a format change fails closed instead of guessing. I did not measure whether `/usage` itself costs model tokens. Only the session and "all models" weekly windows are read; per-model weekly limits are ignored.

## Status

v0.4. Implemented and covered by `python3 -m unittest discover -s tests` (97 tests): guardian and queue-wide budget monitoring, router, classifier, gates, handoff, activator and footprint control, runner, the autonomous PO (`ndc plan`, `ndc run --goal`), the quality gates and `ndc session`. The gates run against a fake `claude` in the tests, so no tokens are spent testing them.

Proven on real runs: a 7-task project built by haiku and sonnet from a backlog written by hand, and a backlog written by the PO itself from a one-line goal (about 40 s). See the release notes for the end-to-end result of executing a PO-written backlog.

Not validated or not built:
- Monitoring under a genuinely tight usage limit, and resuming after a reset (`--wait`): simulated tests only.
- The PO has been tried on small goals only. On large or ambiguous goals it may produce weak backlogs; review them.
- The verify blocklist is not a sandbox.
- No scheduler: NDC runs only when you invoke it. No parallel tasks or worktrees (ECC's DevFleet is not wired in). It cannot swap an interactive session that is already open (see New sessions).
- The quality gates are heuristics: passing them does not mean the code is correct. The reviewer is a model and can miss things or, rarely, flag good code; only blockers stop a task. Regression checks are only as good as the project's own tests.
- Only the software domain has a complete team; the others are thin.
- Tested on macOS with Python 3.9.

`/usage` reports whole percentages, so a task cheaper than one point is recorded as 0.5. Estimates for small tasks stay coarse. The default cost estimates in `ndc/guardian.py` are assumptions, not measurements, replaced by real history after 3 runs per class and window.

License: MIT. ECC is (c) Affaan Mustafa, MIT, see `ndc/catalog/vendor/ecc/LICENSE`.
