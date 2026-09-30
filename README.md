# NDC: Nonstop Development Crew

Continuous development by agents that respect usage limits.

NDC builds on [ECC](https://github.com/affaan-m/ECC) (vendored, unmodified, in `ndc/catalog/vendor/ecc/`) and adds what ECC lacks:

| Piece | What it does |
|---|---|
| **Domains** (`ndc/catalog/domains/*/team.json`) | The whole catalog stays in the repo, but only the software team is activated in a project (with the stacks you choose). `core` is always on. |
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
| **Parallel tasks** (`ndc/parallel.py`, `ndc/worktree.py`) | `ndc run --execute --parallel N` runs up to N ready tasks at once, each in its own git worktree, and merges the ones that pass. The budget guard reserves the cost of every running task. |
| **Security scan** (`ndc/scan.py`) | `ndc scan`: secrets, risky code patterns and an audit of the Claude Code config. Also a gate on every task. Offline, no tokens. |
| **Rules** (`ndc/catalog/vendor/ecc/rules/`) | ECC's coding rules, installed per domain and stack. Most are scoped by file path, so they load only when needed. |
| **Hooks and memory** (`ndc/hooks.py`, `ndc/catalog/runtime/`) | Opt-in (`ndc hooks enable`): ECC's safety hooks (block `--no-verify`, protect linter configs) and session memory, audited and confined to `.ndc/`. |
| **Commands** (`.claude/commands/`) | ECC slash commands, installed per domain and stack by `ndc activate` (`/plan`, `/code-review`, `/quality-gate`, `/build-fix`, `python-review`, ...). Tracked in the manifest and removed by `uninstall`; NDC never overwrites a command you wrote. |
| **MCP configs** (`ndc mcp`) | `ndc mcp list` shows ECC's ready-made servers; `ndc mcp add context7 github` merges them into the project's `.mcp.json` without overwriting entries. Fill in API keys yourself. |
| **Dashboard** (`ndc dashboard`) | Read-only page on `127.0.0.1:8765`: queue, usage windows, recent runs, handoff. Stdlib only, spends no tokens. |
| **Runner** (`ndc/runner.py`) | Loop: guard, pick the first task that fits, dispatch via `claude -p --model <m>`, record the usage delta, repeat. On STOP it writes the handoff and can sleep until the reset (`--wait`). |

## Also vendored from ECC (not wired)

`ndc/catalog/vendor/ecc/` also holds ECC's other harness folders (`harnesses/`: Cursor, Codex, OpenCode, Gemini, Kiro, Zed, ...), `install.sh`/`install.ps1`, `manifests/`, `.claude-plugin/`, `contexts/`, `schemas/`, `scaffolds/`, `SOUL.md`, the three guides and `ecc2/` (Rust, not built). They are reference copies: NDC itself still runs only on Claude Code (`claude -p`), and ECC's own installer is the way to use them in another harness. Hooks now register through `runtime/scripts/ndc-run.js`, so the commands no longer use POSIX `VAR=x` syntax and should work on Windows (not tested there).

## Install

```bash
python3 -m venv ~/.ndc-venv
~/.ndc-venv/bin/pip install ndc-0.5.0-py3-none-any.whl     # the whole catalog is inside the package
ln -s ~/.ndc-venv/bin/ndc ~/.local/bin/ndc                 # optional: `ndc` on your PATH
```

(`pipx install ndc-0.5.0-py3-none-any.whl` works too.) Python 3.9+, standard library only.

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

Stacks: python, node, react, react-native, postgres, mysql, redis, clickhouse and devops (NDC's own skills in `ndc/catalog/core/skills/`: Traefik, Docker Compose/Swarm, Portainer, GitHub Actions deploy; plus ECC's kubernetes and canary-watch). `activate software --stack node` replaces the previous stacks; `--add` keeps them.

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
| `.claude/rules/ndc/<group>/` | the coding rules of the active domain and stacks | yes |
| `.claude/settings.local.json` | only with `ndc hooks enable`: the hook entries NDC owns (your own keys and hooks are preserved) | yes (the file too, if NDC created it) |
| `.claude/.ndc-managed.json` | list of what NDC installed | yes |
| `.ndc/` | task queue, history, handoffs, briefs; `runtime/` (hook scripts), `agent-data/` (hook memory), `worktrees/` (parallel runs, deleted when each task ends) | queue and memory only with `--purge`; `runtime/` on `hooks disable` |

All of it is hidden from git through a marked block in `.git/info/exclude`: local, never committed, invisible to teammates, and it lists exactly the NDC paths, so your own files in `.claude/` are not ignored. Use `--gitignore` to write the block to the project's `.gitignore` instead. NDC refuses to overwrite agents or skills it did not create, warns if git already tracks an NDC file, and does nothing to the ignore rules if the folder is not a git repository. The rules in the block are rewritten on every `activate`; edit outside the markers.

## Parallel tasks

`ndc run --execute --parallel 3` starts up to 3 ready tasks at once. Each one gets its own git worktree on its own branch (`.ndc/worktrees/task-N`, branch `ndc/task-N`), so agents cannot trample each other's files. When a task passes all its gates, its branch is committed and merged into the branch you have checked out (`ndc: merge task #N ...`); the worktree and branch are then deleted.

- **Budget:** the guardian reserves the estimated cost of every running task, so N workers never promise more quota than is left. With little budget left only one runs at a time.
- **Conflicts:** merges are serialized. A conflict aborts the merge cleanly, fails that attempt, and the retry starts again on a fresh worktree from the updated code.
- **Dependencies:** a task starts only after the tasks it depends on were merged, so it sees their code.
- **Requirements:** a git repository with at least one commit, run from its root, on a branch (not detached), with a clean working tree (commit or stash first: results are merged into it). Without `--execute` there is nothing to parallelize and NDC says so.
- **Usage deltas are not recorded** for tasks that overlapped in time (they would each be blamed for the other's usage), so cost estimates keep learning only from tasks that ran alone.
- Retries do not keep the previous attempt's files (the worktree is discarded); the retry prompt says so.

This is NDC's own implementation. ECC's DevFleet is a separate server that ECC does not ship, so it was not copied.

## Security scan

`ndc scan` needs no network and spends no tokens. It reports findings by severity and exits 1 when something reaches `--fail-on` (default `high`).

- **Secrets** (high): AWS, GitHub, Anthropic, OpenAI, Slack, Stripe and Google keys, private keys. Generic `password = "..."` style assignments are medium, with placeholders ignored. The output never repeats the secret. Suppress a false positive on one line with `ndc:allow-secret`.
- **Risky code patterns** (medium, heuristic): shell injection (`shell=True` with interpolation, `os.system(f"...")`), dynamic `eval`, SQL built by concatenation, unsafe `yaml.load`, `verify=False`, `innerHTML` with variables.
- **Claude Code config audit:** `.claude/settings*.json` (`Bash(*)` and other broad permissions, `bypassPermissions`, secrets in `env`, hooks that pipe to a shell or send files out), `.mcp.json` (unpinned `npx` packages, plain-http servers, secrets in `env`), and `CLAUDE.md`, rules, agents and skills (invisible Unicode, instruction-like text such as "ignore all previous instructions"). Items NDC installed are skipped unless `--include-managed`.
- **As a gate:** after every task, files it touched are scanned. A `high` finding fails the attempt (the retry report names the rule and file, never the secret); `medium` findings are noted. Config findings count only for config files the task touched. Turn it off with `quality.security_scan: false`.

ECC's own security scan runs `npx ecc-agentshield`, an npm package downloaded at run time; NDC does not do that, so this scanner is a separate, smaller implementation covering the categories ECC's skill lists. The ECC `security-scan` and `security-review` skills are still installed for the software domain.

## Rules

`ndc activate` also installs ECC's coding rules into `.claude/rules/ndc/`: `common` for software, plus the rules of each stack (`--stack python` adds `python`, `react` adds `react` and `web`, and so on). The activation output states the cost: for `software` + `python` the always-loaded part is about 18 KB (~4,600 tokens) per session; the language rules carry `paths:` frontmatter, so Claude Code loads them only when a matching file is opened. `--no-rules` skips them (and remembers that). Rules are context, not enforcement: for enforcement use hooks or the gates.

## Hooks and memory (opt-in)

Hooks run code on every tool call, so NDC never installs them implicitly. `ndc hooks enable [--profile minimal|standard|strict]` needs Node.js and does this:

- copies an audited subset of ECC's hook scripts into `.ndc/runtime/` (28 files, no network access, no model calls; see `ndc/catalog/runtime/UPSTREAM.md`);
- registers the hooks of the profile in `.claude/settings.local.json` (personal, hidden from git); anything else in that file is preserved, and NDC refuses to touch it if it is invalid JSON or committed to git;
- keeps all hook state inside `.ndc/agent-data/`.

`ndc hooks list` shows each hook. In short:

| Kind | Hooks | Profiles |
|---|---|---|
| Safety | block `git --no-verify`; block edits to existing linter/formatter configs; check staged files at commit (secrets, `debugger`) | first: all; second: standard, strict; third: strict |
| Session memory | save a summary at the end of a session and load it at the next start; log before compaction; suggest `/compact` | standard, strict (start/save also minimal) |
| Learning and metrics | extract patterns from long sessions; local token and cost metrics | all |

Two things differ from a stock ECC install, on purpose:
- ECC's session-end and pre-compact hooks call `claude --model haiku -p` to write a summary. NDC always sets `ECC_SKIP_LLM_SUMMARY=1`, so a hook never spends tokens behind your back.
- NDC's runner opens one session per task, and the memory hooks inject context at every session start. So NDC's own sessions (tasks, PO, reviewer) run with the memory hooks disabled (`hooks.runner_disabled`) and keep the safety hooks, which matter most when an agent runs unattended.

Cost to know: `session:start` injects the previous session summary (a short summary was about 300 tokens; it grows with the summary). Recalled memory is wrapped as "historical reference, not live instructions".

Not copied on purpose: gateguard (blocks edits until files are read), MCP health checks and plan-canvas (network, browser), the background observer of continuous-learning v2 (spawns `claude`), the PostToolUse dispatchers (run `npx`), desktop notifications. Hook commands use POSIX shell syntax (macOS and Linux), and absolute paths: if you move the project, run `ndc hooks enable` again (`ndc hooks status` tells you).

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

v0.5. Implemented and covered by `python3 -m unittest discover -s tests` (153 tests): everything above. The runner, gates, parallel mode and scanner are tested against a fake `claude` and real git repositories, so no tokens are spent testing them.

Proven on real runs: projects built by haiku and sonnet from hand-written and PO-written backlogs; a real Claude Code session that accepted NDC's hook settings and blocked `git commit --no-verify` (the commit count did not change); a real parallel run where two haiku tasks ran at once, merged, and a dependent third task saw both results.

Not validated or not built:
- Monitoring under a genuinely tight usage limit, and resuming after a reset (`--wait`): simulated tests only.
- The PO has been tried on small goals only. On large or ambiguous goals it may produce weak backlogs; review them.
- Parallel mode was tried live with 2 workers on trivial tasks. Real merge conflicts and larger repositories are covered by tests only.
- The model reviewer has not run live yet. The memory hooks were tested against real ECC scripts with synthetic transcripts, not yet across many real sessions.
- The verify blocklist and the security scan are heuristics, not a sandbox or a proof of safety.
- No scheduler: NDC runs only when you invoke it. It cannot swap an interactive session that is already open (see New sessions).
- The quality gates are heuristics: passing them does not mean the code is correct. Regression checks are only as good as the project's own tests.
- NDC covers only the software domain (other domains were removed on purpose).
- Hooks need Node.js. The registered command is shell-neutral, but only macOS is tested (Windows: untested).

`/usage` reports whole percentages, so a task cheaper than one point is recorded as 0.5. Estimates for small tasks stay coarse. The default cost estimates in `ndc/guardian.py` are assumptions, not measurements, replaced by real history after 3 runs per class and window.

License: MIT. ECC is (c) Affaan Mustafa, MIT, see `ndc/catalog/vendor/ecc/LICENSE`.
