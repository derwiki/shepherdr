#!/bin/sh
# Agent-agnostic Herdr session hook.
#
# Herdr installs one of these per agent integration, each with the agent name
# hardcoded. This version takes the agent from the environment instead, so the
# same file can serve as any agent's session hook:
#
#   ~/.codex/herdr-agent-state.sh            HERDR_AGENT=codex
#   ~/.claude/hooks/herdr-agent-state.sh     HERDR_AGENT=claude
#   ~/.codex/herdr-agent-state.sh            HERDR_AGENT=/path/to/other-agent
#
# Herdr needs an exact native session identity on the pane. This reports the
# agent's own session id (or its session file path when that is what the agent
# exposes) through `pane.report_agent_session`.
#
# Environment:
#   HERDR_AGENT        Required (AGENT is accepted as a fallback). Path to, or
#                      command name of, the agent this hook runs under. The
#                      reported kind is its basename, lowercased, without a
#                      script or executable extension:
#                      /opt/homebrew/bin/codex -> codex.
#                      Prefer HERDR_AGENT: other agent runtimes already export
#                      a bare AGENT marker (for example AGENT=1), which names
#                      no agent and is ignored here.
#   AGENT_KIND         Optional. Explicit kind, for wrappers whose basename is
#                      not the agent kind:
#                      HERDR_AGENT='/path with spaces/my-pi-wrapper' AGENT_KIND=pi
#   AGENT_SESSION_REF  Optional. Force which identity to report: `id` or
#                      `path`. Defaults to the session id when it looks like a
#                      UUID, a transcript path otherwise, and the raw id last.
#
# The agent contract: the hook action arrives as $1 (`session` for the events
# that carry a session identity) and the agent's hook payload arrives on stdin
# as JSON. Every failure path exits 0 with no output, because a session hook
# must never fail or delay the agent it belongs to.
#
# The kind has to be one Herdr recognizes (codex, pi, omp, claude, copilot,
# cursor, kimi, opencode, kilo, droid, devin, hermes, qodercli, grok,
# mastracode, ...) for Herdr to classify the pane; `herdr integration status`
# lists them. The agent must also be started with HERDR_AGENT in its
# environment, since the hook runs as its child.

set -eu

hook_input_file="$(mktemp "${TMPDIR:-/tmp}/herdr-agent-hook.XXXXXX")" || exit 0
trap 'rm -f "$hook_input_file"' EXIT HUP INT TERM
cat >"$hook_input_file" 2>/dev/null || true

agent_path="${HERDR_AGENT:-${AGENT:-}}"
agent_kind="${AGENT_KIND:-}"
if [ -z "$agent_kind" ]; then
  [ -n "$agent_path" ] || exit 0
  agent_kind="${agent_path##*/}"
  case "$agent_kind" in
    *.sh | *.py | *.js | *.mjs | *.cjs | *.ts | *.exe) agent_kind="${agent_kind%.*}" ;;
  esac
fi
agent_kind="$(printf '%s' "$agent_kind" | tr '[:upper:]' '[:lower:]')"
# Herdr's agent labels are identifiers that start with a letter, so a bare
# marker such as AGENT=1 reports nothing instead of a bogus kind.
case "$agent_kind" in
  [a-z]*) ;;
  *) exit 0 ;;
esac
case "$agent_kind" in
  *[!a-z0-9_-]*) exit 0 ;;
esac

case "${1:-}" in
  session) ;;
  *) exit 0 ;;
esac

[ "${HERDR_ENV:-}" = "1" ] || exit 0
[ -n "${HERDR_SOCKET_PATH:-}" ] || exit 0
[ -n "${HERDR_PANE_ID:-}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

HERDR_AGENT_KIND="$agent_kind" \
HERDR_AGENT_SESSION_REF="${AGENT_SESSION_REF:-}" \
HERDR_HOOK_INPUT_FILE="$hook_input_file" \
python3 - <<'PY'
import json
import os
import random
import re
import socket
import time

kind = os.environ.get("HERDR_AGENT_KIND") or ""
preference = os.environ.get("HERDR_AGENT_SESSION_REF") or ""
pane_id = os.environ.get("HERDR_PANE_ID")
socket_path = os.environ.get("HERDR_SOCKET_PATH")
hook_input_file = os.environ.get("HERDR_HOOK_INPUT_FILE")

if not kind or not pane_id or not socket_path or preference not in ("", "id", "path"):
    raise SystemExit(0)

hook_input = {}
if hook_input_file:
    try:
        with open(hook_input_file, encoding="utf-8") as handle:
            content = handle.read()
        if content.strip():
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                hook_input = parsed
    except Exception:
        hook_input = {}


def first_text(*keys):
    for key in keys:
        value = hook_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# Subagent hooks describe work inside the main session; they must not claim the
# pane's identity.
if first_text("agent_id", "agentId"):
    raise SystemExit(0)

# Only session-start payloads carry a session identity. Agents spell that event
# "SessionStart", "session_start", or "sessionStart", so compare a normalized
# name and allow a missing field for forward compatibility.
event = first_text("hook_event_name", "hookEventName")
if event is not None and re.sub(r"[^a-z]", "", event.lower()) != "sessionstart":
    raise SystemExit(0)

# The agent's own environment is the most reliable place to find its session.
prefix = re.sub(r"[^A-Z0-9]", "_", kind.upper())
session_id = os.environ.get(prefix + "_SESSION_ID") or first_text(
    "session_id", "sessionId"
)
transcript_path = first_text("transcript_path", "transcriptPath")

# An id inherited from a parent process running the same agent names the parent
# session, so reporting it would steal the parent pane's identity.
inherited = os.environ.get(prefix + "_THREAD_ID")
if inherited and inherited != session_id:
    raise SystemExit(0)

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
absolute_path = transcript_path if transcript_path and transcript_path.startswith("/") else None
if preference == "id":
    identity = {"agent_session_id": session_id} if session_id else {}
elif preference == "path":
    identity = {"agent_session_path": absolute_path} if absolute_path else {}
elif session_id and UUID.fullmatch(session_id):
    identity = {"agent_session_id": session_id}
elif absolute_path:
    identity = {"agent_session_path": absolute_path}
elif session_id:
    identity = {"agent_session_id": session_id}
else:
    identity = {}
if not identity:
    raise SystemExit(0)

source = "herdr:" + kind
params = {"pane_id": pane_id, "source": source, "agent": kind, "seq": time.time_ns()}
params.update(identity)
session_start_source = first_text("source", "session_start_source")
if session_start_source:
    params["session_start_source"] = session_start_source
request = {
    "id": "{0}:{1}:{2:06d}".format(source, int(time.time() * 1000), random.randrange(1_000_000)),
    "method": "pane.report_agent_session",
    "params": params,
}

try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.5)
    client.connect(socket_path)
    client.sendall((json.dumps(request) + "\n").encode())
    try:
        client.recv(4096)
    except Exception:
        pass
    client.close()
except Exception:
    pass
PY
