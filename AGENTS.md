# Working in this repository

Shepherdr mirrors Herdr agent sessions to Slack and starts agent tasks on
demand. `README.md` covers setup for the people using it; this file covers the
code.

This directory is the `shepherdr` package and the Git repository. In this working
copy the project scaffolding (`pyproject.toml`, `tests/`, `docs/`, `examples/`,
`skills/`, `scripts/`, `slack-app-manifest.yaml`) lives in the parent directory.
Run every command below from that parent directory unless it says otherwise.

## Modules

| Module | Responsibility |
| --- | --- |
| `bridge.py` | Herdr socket client, SQLite store and schema, Codex rollout reader, Slack API wrapper, the monitor loop and turn mirroring, `main()` |
| `agents.py` | Agent identity and routing, agent config, screen-state helpers (composer, shell prompt), native history readers |
| `scheduler.py` | Cron jobs and on-demand runtimes: `Job`, `load_jobs`, the run state machine, final-answer detection, notices (Slack message or log line) |
| `suspender.py` | Codex idle suspension and resume |
| `recovery.py` | Codex temporary-error retries and capacity model fallback |
| `runtime.py` | The `shepherdr runtime` CLI; queues a request into the monitor's state database and reports its status, including the observed answer |
| `checkout.py` | Refresh a base checkout without starting an agent |
| `client_slack.py` | Client-mode Slack Web API over `urllib` |
| `herdr-agent-state.sh` | Agent-agnostic session hook: reports the pane's native session identity to Herdr |

A run moves through `planned → creating → environment_ready → starting_agent →
(submitting) → submitted → done`, with `failed`, `uncertain`, and
`prompt_uncertain` as terminal error states. `Scheduler.observe` decides
completion by reading the agent's own session file, not the screen, and stores
the final answer in `job_results`, which `shepherdr runtime --status` returns.

## Commands

```sh
uv run python -m unittest discover -s tests     # no network or Slack needed
uv run ruff check shepherdr tests               # never a bare `ruff check .`
uv run ruff format shepherdr tests
uv run shepherdr --check                         # live Herdr + native histories, no Slack
uv run shepherdr --check-jobs --jobs examples/jobs.json
uv run shepherdr --job-runs
```

Scope Ruff to the project directories: the parent directory also holds unrelated
projects, so `ruff check .` reports tens of thousands of foreign findings.

## Contracts that must not drift

- **Identity comes only from Herdr.** `session_key()` is the single place that
  accepts an identity: a Codex UUID with `kind == "id"`, or an absolute `.jsonl`
  path with `kind == "path"` for omp and Pi. Never derive one from the pane's
  directory, title, or launch command. Panes without an identity are reported by
  `--check` and skipped, and duplicate identities hold Slack input.
- **Session IDs are namespaced.** `omp:<path>`, `pi:<path>`, or a bare Codex
  UUID; `integration_for()` maps an ID back to its agent, and
  `INTEGRATIONS`/`PATH_INTEGRATIONS` are the only lists of supported agents.
- **`supports(session_id, capability)` gates every lifecycle action.** omp and Pi
  declare `history` and `prompt` only, so suspension, resume, and recovery never
  touch them.
- **Path-based histories share one ID namespace** (`"pi:" + header id + ":"`, see
  `SessionHistories.records`). The scheduler and the bridge both read the same
  file and must derive identical turn IDs; namespacing per agent breaks
  completion detection.
- **Slack message IDs are a persisted protocol.** `message_id()` hashes channel,
  session, kind, and key; changing the namespace duplicates messages for turns
  that are already in flight.
- **The hook never fails its agent.** `herdr-agent-state.sh` reads the payload on
  stdin, takes the action as `$1` (`session`), writes nothing to stdout, and
  exits 0 on every error path, including a missing socket, agent, or payload.
  Herdr overwrites its own installed copies, so never edit
  `~/.{codex,claude}/herdr-agent-state.sh` expecting it to survive.
- **`Bridge.slack` may be `None`.** In `--slack-mode none` the monitor short
  circuits to error recovery plus the scheduler: `Bridge.tick()` returns before
  `sync_roots`/`sync_rollouts`, `store.bind` is skipped, and no `Suspender` is
  attached. `Scheduler.notify` logs every run transition and writes a notice row
  per run state with an empty timestamp instead of posting, so follow a run
  through `shepherdr runtime --status` rather than a thread. Keep new Slack calls
  behind that check, and give any bridge double a `slack` attribute.
- **One monitor per state file.** The bridge takes an exclusive flock on
  `bridge.sqlite.lock`. The state database can contain pending Slack replies;
  never add it to a repository.

## Verification

- Read-only live check: `uv run shepherdr --check`. It needs a running Herdr and
  no Slack credentials, and it prints pane counts, identified sessions, missing
  identities, and per-agent capabilities.
- **Hook changes:** pipe a payload into the script with `HERDR_ENV=1`,
  `HERDR_PANE_ID`, `HERDR_SOCKET_PATH`, and a fake UNIX socket, then assert the
  exact `pane.report_agent_session` JSON. Do not point it at a real pane except
  with a pane ID that does not exist.
- **Scheduler or mirroring changes:** unit tests use `FakeHerdr`, `FakeSlack`,
  and `ImmediateExecutor` from `tests/`. Then run one live job in a scratch Git
  repository (not the project) before calling it done.
- **Clean up after live runs.** `worktree.create` leaves a workspace for the
  checkout *and* one for the worktree: close both with `workspace.close`, then
  `git worktree remove` the worktree. Do not leave temporary repositories,
  `~/.local/state/shepherdr`, extra Herdr workspaces, or scratch panes behind.

Herdr's own API is the ground truth for pane, agent, and socket behavior:
`herdr api schema --json`, `herdr api snapshot`, `herdr pane`/`agent`/`workspace`
subcommands, and the integration scripts embedded in `strings $(command -v herdr)`.

## Environment gotchas

- **`AGENT` is taken.** Agent runtimes export a bare `AGENT=1` marker to their
  children, so the hook takes its agent from `HERDR_AGENT` and ignores values
  that do not name an agent.
- **Codex only runs trusted hooks.** A hook in `~/.codex/hooks.json` fires after
  the user approves it once in an interactive session (persisted in
  `config.toml` under `hooks.state`). Scheduled and runtime agents are
  non-interactive, so trust it first — or run with
  `--dangerously-bypass-hook-trust`.
- **Slack is optional but load-bearing.** In `bot` (default) and `client` modes
  `bridge.main` refuses to start without a channel, a user, and either bot tokens
  or a client credentials file. `--slack-mode none` starts the monitor with
  `slack=None`: runtime and scheduled jobs run, but there is no mirroring, no
  remote reply delivery, no suspension, and notices are logged instead of
  posted.
- **Job panes need a recognizable shell prompt.** `shell_prompt_line()` accepts
  the usual glyph endings plus themes that start the line with a glyph
  (robbyrussell prints `➜  repo git:(main)`); the scheduler sends `ctrl+u` before
  typing the launch command. An unrecognized line fails the run after two
  minutes rather than typing into a draft.
- **Codex needs a live login.** `codex exec` fails with "access token could not
  be refreshed" until the user runs `codex login`. Session hooks still fire
  before that failure, so hook testing works without a valid login.

## Style

- Match the patterns already in the module you touch; never add a second
  convention for one call site, and delete what a change makes obsolete.
- Tests assert observable behavior — normalized records, mirrored Slack text,
  run states, exact requests on the wire — not wiring, forwarding, or source
  text. Prefer a new case in the existing test class over a new file.
- Keep the suite and `ruff format` clean before you finish, and run the live
  check for anything that touches identity, history, or the scheduler.
