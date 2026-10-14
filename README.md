# Shepherdr

Mirror Herdr agent sessions to Slack and start tasks on demand.

Shepherdr runs as a monitor inside a Herdr pane. It watches the agent sessions
Herdr reports, gives each one a Slack thread, sends your replies back to the
same agent session, and can start new agent runs in their own workspace,
worktree, and tab.

- One Slack thread per agent session: your prompt, progress, final answer, and
  follow-up turns.
- A channel notice when a turn ends, with a link to its thread.
- On-demand runtimes from any terminal: `shepherdr runtime --prompt-file …`.
- Cron schedules that start an agent in a fresh worktree.
- Idle-session suspension and temporary-error recovery for Codex.

## Layout

This directory is both the `shepherdr` Python package and the Git repository. In
this working copy the rest of the project sits one level up, in the parent
directory:

| Path | Contents |
| --- | --- |
| `../pyproject.toml` | package metadata, dependencies, the `shepherdr` console script |
| `../tests/` | unittest suite |
| `../docs/operations.md` | full operations guide |
| `../examples/` | agent, job, and config examples |
| `../skills/shepherdr-runtime/` | runtime skill and its helper script |
| `../scripts/install-user.py` | installs the CLI and skill links |
| `../slack-app-manifest.yaml` | Slack app manifest |
| `../README.md` | product guide |

The package imports only its own modules plus `croniter` and `slack_sdk`, so
these files can be installed or vendored without that scaffolding.

## Requirements

- Herdr, with the integration for each agent you use:
  `herdr integration install codex`, `herdr integration install omp`, or
  `herdr integration install pi`. Check with `herdr integration status`.
- Python 3.10 or later and `uv`.
- A Slack app (bot mode, the default) or a Slack client token (client mode).
  `--slack-mode none` runs the monitor without Slack.

## Set up the monitor

Run these commands from the project root (the parent directory) in a Herdr pane.

1. Install dependencies:

   ```sh
   uv sync
   ```

2. Create a Slack app from `slack-app-manifest.yaml`, install it in the
   workspace, and invite it to the target channel. Create an app-level token
   with `connections:write` (`xapp-…`) and copy the bot token (`xoxb-…`).

3. Put the tokens and IDs in the environment. Use IDs, not names:

   ```sh
   export SLACK_APP_TOKEN='xapp-…'
   export SLACK_BOT_TOKEN='xoxb-…'
   export SLACK_CHANNEL_ID='C…'
   export SLACK_USER_ID='U…'
   ```

4. Check Herdr and the native histories without Slack:

   ```sh
   uv run shepherdr --check
   ```

   It prints the pane count per agent, identified sessions, history files,
   missing identities, and the actions each agent supports. Every supported pane
   needs an exact native identity: a Codex session ID, or an absolute session
   file path for omp and Pi.

5. Start the monitor in a Herdr pane and leave it running:

   ```sh
   uv run shepherdr --slack-mode bot
   ```

   `--slack-mode none` starts the monitor without Slack. Runtime and scheduled
   jobs still run, but there are no threads, no remote replies, and no idle
   suspension. The monitor logs every run transition instead of posting a
   notice, and `shepherdr runtime --status REQUEST_ID` reports the run,
   including the agent's final answer.

Client mode posts as your own Slack user instead of a bot app. Put a client
token and cookie in a private file and start the monitor with
`--slack-mode client`:

```json
{"token": "xoxc-…", "cookie": "xoxd-…"}
```

`~/.config/shepherdr/slack-client.json` is the default location; use
`--client-credentials PATH` for another file.

## Choose the agent

`~/.config/shepherdr/config.json` selects the default agent. Use `--config PATH`
for another file; an absent file selects Codex.

```json
{
  "agent": {
    "integration": "omp",
    "command": ["omp"]
  }
}
```

`integration` must be `codex`, `omp`, or `pi`. `command` is a nonempty array: the
first item is the executable or wrapper, and each remaining item is one argument.
Do not write a shell command string; put shell logic in the wrapper. See
`../examples/config-omp.json`, `../examples/config.json`, and
`../examples/config-pi.json`.

| Action | Codex | omp | Pi |
| --- | --- | --- | --- |
| Runtime and scheduled launch | Yes | Yes | Yes |
| Native routing identity | Session UUID | Absolute session file path | Absolute session file path |
| Saved user text, progress, final answer, Slack replies | Yes | Yes | Yes |
| Automatic idle suspension and Slack resume | Yes | No | No |
| Native temporary error recovery, capacity model change | Yes | No | No |

## Agent identity hook

`herdr-agent-state.sh` in this repository is the session hook an agent runs at
startup to report its native session identity to Herdr. Herdr ships its own
per-agent copies of this hook; this version is agent-agnostic and takes the
agent from the environment:

| Variable | Meaning |
| --- | --- |
| `HERDR_AGENT` | Path to, or name of, the agent this hook runs under. The reported kind is its basename, lowercased: `/opt/homebrew/bin/codex` → `codex`. `AGENT` is accepted as a fallback, but other runtimes already export a bare `AGENT=1`, which is ignored. |
| `AGENT_KIND` | Explicit kind for wrappers whose basename is not the agent kind, e.g. `HERDR_AGENT='/path with spaces/my-pi-wrapper' AGENT_KIND=pi`. |
| `AGENT_SESSION_REF` | Force `id` or `path` when an agent reports both. Default: a UUID session ID, else an absolute transcript path, else the raw ID. |

The agent invokes it with the action as the first argument and its hook payload
on stdin. Codex registers it in `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "command": "env HERDR_AGENT=codex bash '/path/to/herdr-agent-state.sh' session",
            "timeout": 10,
            "type": "command"
          }
        ]
      }
    ]
  }
}
```

`herdr integration install <agent>` writes that registration for you (and
`[features] hooks = true` in `~/.codex/config.toml`); point its command at this
script to use this version. Codex runs a hook only after it is trusted once —
approve the hook prompt in an interactive Codex session, because scheduled and
runtime agents start without a terminal. For automation, Codex offers
`--dangerously-bypass-hook-trust`.

Herdr's own integrations already report identities for omp and Pi. Use this hook
for agents Herdr has no integration for, or to keep the reporting in one place.

## Start tasks on demand

Install the CLI and the runtime skill, then ask an existing agent for a new
runtime or use the CLI:

```sh
python3 scripts/install-user.py
shepherdr runtime --name check-batch --repo /path/to/repo \
  --prompt "Check the latest batch with read-only access. Report the findings."
shepherdr runtime --status REQUEST_ID
```

The monitor must be running: the CLI queues the request in the monitor's state
database and refuses if the scheduler is not polling. A request creates a new
workspace, tab, worktree, and branch, starts the configured agent, and mirrors
the run to its Slack thread. `--no-worktree` uses the existing checkout,
`--base REF` selects another local reference, and `--config PATH` overrides the
agent for that request only.

`--status REQUEST_ID` reports the request and run state, the pane, session,
branch, and error if any, the agent's final answer once the turn ends, and a
thread link when Slack is configured. That status is how you follow a run with
`--slack-mode none`.

## Scheduled jobs

Put jobs in `~/.config/shepherdr/jobs.json` and validate them before enabling
anything:

```sh
uv run shepherdr --check-jobs --jobs examples/jobs.json
uv run shepherdr --job-runs
```

Jobs use the monitor's agent unless they set their own `agent` object. Every
example job is disabled.

## State and configuration

| Path | Contents |
| --- | --- |
| `~/.config/shepherdr/config.json` | default agent |
| `~/.config/shepherdr/jobs.json` | cron schedules |
| `~/.config/shepherdr/slack-client.json` | client-mode credentials |
| `~/.local/state/shepherdr/bridge.sqlite` | monitor state; keep it private, it can hold pending Slack replies |
| `~/.local/state/shepherdr/worktrees/` | scheduled and runtime worktrees |

One monitor process per state file; a lock file rejects a second one.

## Development

Run from the project root:

```sh
uv run python -m unittest discover -s tests
uv run ruff check shepherdr tests
uv run ruff format shepherdr tests
```

Scope Ruff to the project directories: the parent directory also holds unrelated
projects, and a bare `ruff check .` walks all of them.

`uv run shepherdr --check` is the read-only smoke test: it talks to the live
Herdr socket and reads native histories without Slack credentials.

## Limits worth knowing

- Mirroring needs Slack. `--slack-mode none` runs the monitor for runtime and
  scheduled jobs only: no threads, no remote replies, no idle suspension, and
  each run transition is logged instead of posted. The runtime queue lives
  inside the monitor, so the monitor must be running in either mode.
- A pane is mirrored only when Herdr reports an exact native identity for it.
  Shepherdr never guesses one from the working directory or the launch command.
- Two panes that report the same identity hold their Slack input until you fix
  the duplicate; `--check` lists them.
- omp and Pi support history, prompts, and Slack replies. Automatic suspension,
  resume, and error recovery are Codex-only.
- Job panes are typed into only when their last line is a recognized shell
  prompt, so keep a prompt that ends in `$ % # > ❯ ❱ ➜ ➤ → λ »` or starts with
  one of those glyphs. A pane whose last line is not a prompt fails the run
  after two minutes instead of receiving the launch command.
