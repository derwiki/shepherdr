"""Stop idle Codex terminals and resume their exact saved session on Slack input."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

from shepherdr.agents import shell_prompt_line, supports

LOG = logging.getLogger("shepherdr")
MANAGED_STATES = {"suspending", "suspended", "resuming", "resume_failed"}


def last_saved_reply(path: Path) -> str | None:
    """Read a bounded tail when the session predates Slack mirroring."""
    reply = None
    try:
        with path.open("rb") as stream:
            start = max(0, path.stat().st_size - 2 * 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            tail = stream.read(2 * 1024 * 1024)
        for line in tail.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload") or {}
            if (
                record.get("type") != "response_item"
                or (record.get("metadata") or {}).get("compaction_output")
                or payload.get("type") != "message"
                or payload.get("role") != "assistant"
                or payload.get("phase") not in {"commentary", "final_answer"}
            ):
                continue
            text = "\n".join(
                item["text"]
                for item in payload.get("content", [])
                if item.get("type") == "output_text"
                and isinstance(item.get("text"), str)
            ).strip()
            if text:
                reply = text
    except OSError:
        LOG.warning("Cannot read saved reply for the shell summary")
    return reply


def suspension_summary(tab: str, reply: str | None) -> str:
    """Make a bounded breadcrumb from recorded text, without inventing a next step."""

    def compact(text: str, limit: int) -> str:
        text = re.sub(r"\[([^\]]+)\]\([^\n)]*\)", r"\1", text)
        text = "".join(
            character
            for character in text
            if character.isspace()
            or not unicodedata.category(character).startswith("C")
        )
        text = " ".join(text.split()).strip("#>*` -")
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    lines = [line.strip() for line in (reply or "").splitlines() if line.strip()]
    next_step = ""
    result_end = len(lines)
    for index, line in enumerate(lines):
        cleaned = line.strip("#>*` -")
        if re.match(
            r"(?i)^(?:next(?: steps?| action)?|remaining(?: work| checks| CI| jobs)?|"
            r"still (?:required|needed)|pending|awaiting|blocked)\b",
            cleaned,
        ):
            following = lines[index + 1] if index + 1 < len(lines) else ""
            next_step = compact(cleaned + " " + following, 200)
            result_end = index
            break
    result = lines[:result_end]
    body = [line for line in result if not line.startswith(("#", "```"))]
    last = compact(" ".join(body or result), 280) or "No result recorded."
    summary = f"# summary: {compact(tab, 80)} | Last reply: {last}"
    if next_step:
        summary += f" | {next_step}"
    return summary


class NotSafeToSuspend(ValueError):
    pass


def empty_composer(text: str) -> bool:
    """Accept only a known empty Codex composer; unknown UI stays untouched."""
    prompts = [
        line.strip() for line in text.splitlines() if line.lstrip().startswith("›")
    ]
    return bool(prompts) and prompts[-1] in {"›", "› Ask Codex to do anything"}


def resume_arguments(argv: list[str], metadata: dict[str, Any]) -> list[str]:
    """Keep launch overrides, replace old prompt/ID and stale permission flags."""
    if not metadata.get("model") or not metadata.get("reasoning_effort"):
        raise NotSafeToSuspend("The saved model settings are unknown")
    flags = {
        "--search",
        "--no-alt-screen",
        "--no-daemon",
        "--strict-config",
        "--oss",
        "--dangerously-bypass-hook-trust",
    }
    values = {"-c", "--config", "-p", "--profile", "--add-dir", "--enable", "--disable"}
    stale_values = {
        "-m",
        "--model",
        "-s",
        "--sandbox",
        "-a",
        "--ask-for-approval",
        "-C",
        "--cd",
        "-i",
        "--image",
    }
    stale_flags = {
        "--yolo",
        "--dangerously-bypass-approvals-and-sandbox",
        "--approve-for-me",
        "--worktree",
        "--last",
        "--all",
        "--include-non-interactive",
    }
    result = []
    index = 1
    while index < len(argv):
        value = argv[index]
        index += 1
        if value in {"resume", "fork"}:
            continue
        if not value.startswith("-"):
            # Positional session names and prompts are deliberately replaced.
            continue
        key, equal, inline = value.partition("=")
        if key in stale_flags:
            continue
        if key in flags:
            result.append(value)
            continue
        if key not in values | stale_values:
            raise NotSafeToSuspend(f"Unsupported Codex launch option {key}")
        if not equal:
            if index >= len(argv):
                raise NotSafeToSuspend("Incomplete Codex launch option")
            inline = argv[index]
            index += 1
        if key in stale_values:
            continue
        if key in {"-c", "--config"} and inline.partition("=")[0].strip().split(".")[
            0
        ] in {
            "model",
            "model_reasoning_effort",
            "approval_policy",
            "sandbox_mode",
            "permissions",
            "sandbox_workspace_write",
            "sandbox_read_only",
        }:
            continue
        result.extend([key, inline])

    policy = json.loads(metadata["sandbox_policy"])
    mode = policy.get("type")
    if mode in {"disabled", "danger-full-access"}:
        mode = "danger-full-access"
    elif mode == "read-only":
        if any(key != "type" for key in policy):
            raise NotSafeToSuspend("The saved read-only policy has unsupported options")
    elif mode == "workspace-write":
        result.extend(
            [
                "-c",
                "sandbox_workspace_write.writable_roots="
                + json.dumps(policy.get("writable_roots", [])),
            ]
        )
        for option in ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"):
            if option in policy:
                result.extend(
                    [
                        "-c",
                        f"sandbox_workspace_write.{option}="
                        + json.dumps(policy[option]),
                    ]
                )
    else:
        raise NotSafeToSuspend(
            "The saved permission profile cannot be restored exactly"
        )
    approval = metadata["approval_mode"]
    if approval not in {"never", "on-request"}:
        raise NotSafeToSuspend("The saved approval mode cannot be restored")
    result.extend(["--sandbox", mode, "--ask-for-approval", approval])
    if metadata.get("model"):
        result.extend(["--model", metadata["model"]])
    if metadata.get("reasoning_effort"):
        result.extend(
            ["-c", "model_reasoning_effort=" + json.dumps(metadata["reasoning_effort"])]
        )
    result.extend(["resume", metadata["id"], "--cd", metadata["cwd"]])
    return result


class CodexCatalog:
    def __init__(self, home: Path):
        self.home = home

    def get(self, session_id: str) -> dict[str, Any]:
        databases = sorted(
            (
                p
                for p in self.home.glob("state_*.sqlite")
                if re.fullmatch(r"state_\d+\.sqlite", p.name)
            ),
            key=lambda p: int(p.stem.split("_")[-1]),
            reverse=True,
        )
        for path in databases:
            with closing(
                sqlite3.connect(
                    path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
                )
            ) as db:
                db.row_factory = sqlite3.Row
                columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
                required = {
                    "id",
                    "cwd",
                    "rollout_path",
                    "approval_mode",
                    "sandbox_policy",
                    "updated_at",
                    "model",
                    "reasoning_effort",
                }
                if not required <= columns:
                    continue
                row = db.execute(
                    "SELECT * FROM threads WHERE id=?", (session_id,)
                ).fetchone()
                if row is None:
                    continue
                data = dict(row)
                rollout = Path(data["rollout_path"])
                if not rollout.is_file() or not Path(data["cwd"]).is_dir():
                    raise NotSafeToSuspend(
                        "Saved session file or working directory is missing"
                    )
                with rollout.open("rb") as stream:
                    first = json.loads(stream.readline(2 * 1024 * 1024))
                identity = first.get("payload") or {}
                if first.get("type") != "session_meta" or session_id not in {
                    identity.get("id"),
                    identity.get("session_id"),
                }:
                    raise NotSafeToSuspend("Saved session identity does not match")
                recorded = data.get("updated_at_ms")
                updated = (
                    recorded / 1000
                    if isinstance(recorded, (int, float))
                    else data["updated_at"]
                )
                if not isinstance(updated, (int, float)) or updated <= 0:
                    raise NotSafeToSuspend("No reliable session activity time")
                data["last_activity"] = max(updated, rollout.stat().st_mtime)
                data["rollout_size"] = rollout.stat().st_size
                return data
        raise NotSafeToSuspend("Saved session not found")

    def has_pending_work(self, session_id: str) -> bool:
        for filename, query in (
            ("queue_1.sqlite", "SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1"),
            (
                "goals_1.sqlite",
                "SELECT 1 FROM thread_goals WHERE thread_id=? AND status='active' LIMIT 1",
            ),
        ):
            path = self.home / filename
            if path.exists():
                with closing(
                    sqlite3.connect(
                        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
                    )
                ) as db:
                    if db.execute(query, (session_id,)).fetchone():
                        return True
        return False


class Suspender:
    def __init__(
        self, bridge: Any, idle_seconds: float = 86400, grace_seconds: float = 60
    ):
        self.bridge = bridge
        self.store = bridge.store
        self.herdr = bridge.herdr
        self.catalog = CodexCatalog(bridge.rollouts.home)
        self.idle_seconds = idle_seconds
        self.grace_seconds = grace_seconds
        self.idle_observed: dict[str, float] = {}
        self.store.write("""
            CREATE TABLE IF NOT EXISTS suspensions (
                session_id TEXT PRIMARY KEY,
                pane_id TEXT NOT NULL,
                terminal_id TEXT NOT NULL,
                resume_args TEXT NOT NULL,
                executable TEXT,
                shell_prompt TEXT,
                resume_attempt_at REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL,
                changed_at REAL NOT NULL,
                last_wake REAL NOT NULL DEFAULT 0,
                notified_state TEXT,
                last_error TEXT
            )
        """)
        columns = {r[1] for r in self.store.all("PRAGMA table_info(suspensions)")}
        for column in ("executable", "shell_prompt", "shell_summary"):
            if column not in columns:
                self.store.write(f"ALTER TABLE suspensions ADD COLUMN {column} TEXT")
        if "resume_attempt_at" not in columns:
            self.store.write(
                "ALTER TABLE suspensions ADD COLUMN resume_attempt_at REAL NOT NULL DEFAULT 0"
            )
        if "summary_attempted" not in columns:
            self.store.write(
                "ALTER TABLE suspensions ADD COLUMN summary_attempted INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def executable(path: str | None) -> str:
        if not path or not Path(path).is_absolute() or not Path(path).is_file():
            raise NotSafeToSuspend("The original Codex executable is missing")
        if not os.access(path, os.X_OK):
            raise NotSafeToSuspend("The original Codex executable cannot run")
        return path

    def shell_prompt(self, pane_id: str) -> str:
        text = self.herdr.request(
            "pane.read", {"pane_id": pane_id, "source": "detection", "lines": 4}
        )["read"]["text"]
        line = shell_prompt_line(text)
        if line is None:
            raise NotSafeToSuspend("The shell prompt is unknown or contains a draft")
        return line

    def report_resumed_identity(self, row: Any) -> bool:
        """Older Codex versions need Herdr's explicit session-report API."""
        try:
            self.pane_matches(row)
            live = self.herdr.request("agent.get", {"target": row["pane_id"]})["agent"]
            if live.get("agent") != "codex" or live.get("agent_session"):
                return False
            expected = [row["executable"], *json.loads(row["resume_args"])]
            foreground = (
                self.process_info(row["pane_id"]).get("foreground_processes") or []
            )
            codex = [p for p in foreground if p.get("name") == "codex"]
            if len(codex) != 1 or codex[0].get("argv") != expected:
                return False
            self.catalog.get(row["session_id"])
            self.herdr.request(
                "pane.report_agent_session",
                {
                    "pane_id": row["pane_id"],
                    # Herdr accepts saved identity only from this native source.
                    "source": "herdr:codex",
                    "agent": "codex",
                    "agent_session_id": row["session_id"],
                    "session_start_source": "resume",
                    "seq": time.time_ns(),
                },
            )
            return True
        except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error):
            return False

    def leave_summary(self, row: Any, prompt: str) -> None:
        if not row["shell_summary"] or row["summary_attempted"]:
            return
        pane = self.pane_matches(row)
        if (
            pane.get("agent")
            or not self.shell_ready(self.process_info(row["pane_id"]))
            or self.shell_prompt(row["pane_id"]) != prompt
        ):
            raise NotSafeToSuspend("The shell changed before the summary was printed")
        # Record the attempt before sending: a lost response must not repeat it.
        self.store.write(
            "UPDATE suspensions SET summary_attempted=1 WHERE session_id=?",
            (row["session_id"],),
        )
        try:
            # Interactive zsh may not treat '#' as a comment. Print a quoted
            # string so reply text cannot execute shell substitutions or keys.
            self.herdr.request(
                "pane.send_input",
                {
                    "pane_id": row["pane_id"],
                    "text": shlex.join(["printf", "%s\\n", row["shell_summary"]]),
                    "keys": ["Enter"],
                },
            )
        except Exception:
            LOG.exception("Cannot confirm shell summary for %s", row["session_id"])

    def pending(self, sid: str) -> bool:
        return bool(
            self.store.one(
                """
            SELECT 1 FROM inbound WHERE session_id=? AND
                (state IN ('pending', 'sending') OR (state='submitted' AND seen_in_rollout=0))
            LIMIT 1
        """,
                (sid,),
            )
        )

    def managed(self, sid: str) -> bool:
        row = self.store.one("SELECT state FROM suspensions WHERE session_id=?", (sid,))
        return row is not None and row["state"] in MANAGED_STATES

    def process_info(self, pane_id: str) -> dict[str, Any]:
        return self.herdr.request("pane.process_info", {"pane_id": pane_id})[
            "process_info"
        ]

    @staticmethod
    def shell_ready(info: dict[str, Any]) -> bool:
        processes = info.get("foreground_processes") or []
        return bool(processes) and all(
            p.get("pid") == info.get("shell_pid")
            and p.get("name") in {"zsh", "bash", "fish", "sh"}
            for p in processes
        )

    def pane_matches(self, row: Any) -> dict[str, Any]:
        pane = self.herdr.request("pane.get", {"pane_id": row["pane_id"]})["pane"]
        if pane.get("terminal_id") != row["terminal_id"]:
            raise NotSafeToSuspend("The original pane now has a different terminal")
        return pane

    def prepare(self, agent: dict[str, Any], now: float) -> dict[str, Any]:
        sid = agent["session_id"]
        if not supports(sid, "suspend"):
            raise NotSafeToSuspend(
                "This integration cannot suspend and resume a session"
            )
        if agent["status"] not in {"idle", "done"}:
            raise NotSafeToSuspend("Agent is not idle")
        if agent.get("focused") or agent["pane_id"] == os.environ.get("HERDR_PANE_ID"):
            raise NotSafeToSuspend("Pane is focused or contains the monitor's caller")
        if self.pending(sid) or self.catalog.has_pending_work(sid):
            raise NotSafeToSuspend("Session has queued input or an active goal")
        recovery = getattr(self.bridge, "recovery", None)
        if recovery is not None and recovery.pending(sid):
            raise NotSafeToSuspend("Session is waiting for transient error recovery")
        metadata = self.catalog.get(sid)
        if now - metadata["last_activity"] < self.idle_seconds:
            raise NotSafeToSuspend("Session activity is too recent")
        session = self.store.one("SELECT * FROM sessions WHERE session_id=?", (sid,))
        if (
            session is None
            or not session["thread_ts"]
            or session["offset"] < metadata["rollout_size"]
        ):
            raise NotSafeToSuspend("The Slack mirror has unread session records")
        live = self.herdr.request("agent.get", {"target": agent["pane_id"]})["agent"]
        identity = live.get("agent_session") or {}
        if (
            live.get("agent") != "codex"
            or identity.get("kind") != "id"
            or identity.get("value") != sid
        ):
            raise NotSafeToSuspend("Pane session changed")
        if live.get("agent_status") not in {"idle", "done"} or live.get("focused"):
            raise NotSafeToSuspend("Pane became active")
        screen = self.herdr.request(
            "agent.read",
            {
                "target": agent["pane_id"],
                "source": "detection",
                "lines": 30,
            },
        )["read"]["text"]
        if not empty_composer(screen):
            raise NotSafeToSuspend("Composer has a draft or its state is unknown")
        if re.search(
            r"\b[1-9]\d* background (?:terminal|task)|background terminal.*running",
            screen,
            re.IGNORECASE,
        ):
            raise NotSafeToSuspend("A background command is visible")
        info = self.process_info(agent["pane_id"])
        processes = info.get("foreground_processes") or []
        codex = [p for p in processes if p.get("name") == "codex"]
        if (
            not info.get("shell_pid")
            or len(codex) != 1
            or info.get("shell_pid") in {p.get("pid") for p in processes}
        ):
            raise NotSafeToSuspend("Codex has no preserved parent shell")
        if any(p.get("name") not in {"codex", "node"} for p in processes):
            raise NotSafeToSuspend("Another foreground process is present")
        reply = self.store.one(
            "SELECT text FROM assistant_turns WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
            (sid,),
        )
        return {
            "session_id": sid,
            "pane_id": agent["pane_id"],
            "terminal_id": live["terminal_id"],
            "args": resume_arguments(codex[0]["argv"], metadata),
            "executable": self.executable(codex[0]["argv"][0]),
            "last_activity": metadata["last_activity"],
            "shell_summary": suspension_summary(
                session["tab"],
                reply["text"]
                if reply
                else last_saved_reply(Path(metadata["rollout_path"])),
            ),
        }

    def notify(self, row: Any, text: str) -> None:
        session = self.store.one(
            "SELECT thread_ts FROM sessions WHERE session_id=?", (row["session_id"],)
        )
        if session and session["thread_ts"]:
            self.bridge.post_slack(
                text,
                thread_ts=session["thread_ts"],
                client_msg_id=self.bridge.lifecycle_message_id(
                    row["session_id"], row["state"], row["changed_at"]
                ),
            )
        self.store.write(
            "UPDATE suspensions SET notified_state=? WHERE session_id=?",
            (row["state"], row["session_id"]),
        )

    def notify_safely(self, row: Any, text: str) -> None:
        try:
            self.notify(row, text)
        except Exception:
            LOG.exception("Cannot post lifecycle notice for %s", row["session_id"])

    def state(self, sid: str, value: str, now: float, error: str | None = None) -> None:
        self.store.write(
            "UPDATE suspensions SET state=?,changed_at=?,last_error=? WHERE session_id=?",
            (value, now, error, sid),
        )
        if value in MANAGED_STATES:
            self.store.write(
                "UPDATE sessions SET status=? WHERE session_id=?", (value, sid)
            )
        LOG.info("Session %s lifecycle: %s", sid, value)

    def reconcile(self, agents: list[dict[str, Any]], now: float) -> bool:
        active = {a["session_id"]: a for a in agents}
        changed = False
        for row in self.store.all("SELECT * FROM suspensions WHERE state != 'active'"):
            sid = row["session_id"]
            try:
                if sid in active and row["state"] != "suspending":
                    self.state(sid, "active", now)
                    self.store.write(
                        "UPDATE suspensions SET last_wake=? WHERE session_id=?",
                        (now, sid),
                    )
                    changed = True
                    continue
                if (
                    row["state"] in {"resuming", "resume_failed"}
                    and row["resume_attempt_at"]
                    and self.report_resumed_identity(row)
                ):
                    changed = True
                    continue
                if row["state"] in {"suspended", "resume_failed"} and not self.pending(
                    sid
                ):
                    if (
                        row["state"] == "suspended"
                        and row["notified_state"] != "suspended"
                    ):
                        self.notify_safely(
                            row,
                            f"Session suspended after the idle time limit. Reply here to resume it.\nManual resume: codex resume {sid}",
                        )
                    continue
                if row["state"] == "resume_failed" and now - row["changed_at"] < 60:
                    continue
                if row["state"] == "resume_failed" and row["resume_attempt_at"]:
                    retry = self.store.one(
                        "SELECT 1 FROM inbound WHERE session_id=? AND state='pending' AND created_at>? LIMIT 1",
                        (sid, row["resume_attempt_at"]),
                    )
                    if not retry:
                        continue
                pane = self.pane_matches(row)
                info = self.process_info(row["pane_id"])
                if row["state"] == "suspending":
                    if self.shell_ready(info):
                        prompt = self.shell_prompt(row["pane_id"])
                        self.store.write(
                            "UPDATE suspensions SET shell_prompt=? WHERE session_id=?",
                            (prompt, sid),
                        )
                        self.leave_summary(row, prompt)
                        self.state(sid, "suspended", now)
                        changed = True
                    elif now - row["changed_at"] > 45:
                        self.state(
                            sid,
                            "active",
                            now,
                            "Codex did not exit; no forced kill was sent",
                        )
                        self.store.write(
                            "UPDATE suspensions SET last_wake=? WHERE session_id=?",
                            (now, sid),
                        )
                elif row["state"] in {"suspended", "resume_failed"} and self.pending(
                    sid
                ):
                    if row["state"] == "resume_failed" and now - row["changed_at"] < 60:
                        continue
                    if not self.shell_ready(info) or pane.get("agent"):
                        raise NotSafeToSuspend(
                            "The original pane is no longer an available shell"
                        )
                    self.catalog.get(sid)  # Saved ID and directory must still exist.
                    executable = self.executable(row["executable"])
                    if (
                        not row["shell_prompt"]
                        or self.shell_prompt(row["pane_id"]) != row["shell_prompt"]
                    ):
                        raise NotSafeToSuspend(
                            "The shell has changed or contains a draft"
                        )
                    command = shlex.join([executable, *json.loads(row["resume_args"])])
                    self.state(sid, "resuming", now)
                    self.store.write(
                        "UPDATE suspensions SET resume_attempt_at=?,notified_state=NULL WHERE session_id=?",
                        (now, sid),
                    )
                    changed = True
                    # Use the verified absolute binary, bypassing interactive aliases.
                    # One request sends command and Enter; persist state before sending.
                    self.herdr.request(
                        "pane.send_input",
                        {
                            "pane_id": row["pane_id"],
                            "text": command,
                            "keys": ["Enter"],
                        },
                    )
                elif row["state"] == "resuming" and now - row["changed_at"] > 90:
                    raise NotSafeToSuspend(
                        "Resume did not report the expected saved session"
                    )
                latest = self.store.one(
                    "SELECT * FROM suspensions WHERE session_id=?", (sid,)
                )
                if (
                    latest["state"] == "suspended"
                    and latest["notified_state"] != "suspended"
                ):
                    self.notify_safely(
                        latest,
                        f"Session suspended after the idle time limit. Reply here to resume it.\nManual resume: codex resume {sid}",
                    )
            except (
                OSError,
                RuntimeError,
                ValueError,
                KeyError,
                sqlite3.Error,
            ) as error:
                if row["state"] == "suspending":
                    LOG.warning("Cannot confirm suspension of %s: %s", sid, error)
                    if now - row["changed_at"] > 45:
                        self.state(sid, "resume_failed", now, str(error))
                else:
                    latest = self.store.one(
                        "SELECT * FROM suspensions WHERE session_id=?", (sid,)
                    )
                    if (
                        latest["state"] == "resuming"
                        and now - latest["changed_at"] <= 90
                    ):
                        # A lost response does not prove that command delivery failed.
                        LOG.warning("Resume result is uncertain for %s: %s", sid, error)
                        continue
                    self.state(sid, "resume_failed", now, str(error))
                    latest = self.store.one(
                        "SELECT * FROM suspensions WHERE session_id=?", (sid,)
                    )
                    if row["notified_state"] != "resume_failed":
                        self.notify_safely(
                            latest,
                            f"Resume is waiting: {error}. Your reply is saved. Check the pane or send another reply to retry.\nManual resume: codex resume {sid}",
                        )
        return changed

    def tick(self, agents: list[dict[str, Any]], now: float | None = None) -> bool:
        now = time.time() if now is None else now
        changed = self.reconcile(agents, now)
        suspended = 0
        counts: dict[str, int] = {}
        for agent in agents:
            counts[agent["session_id"]] = counts.get(agent["session_id"], 0) + 1
        self.idle_observed = {
            sid: seen for sid, seen in self.idle_observed.items() if sid in counts
        }
        for agent in agents:
            sid = agent["session_id"]
            if not supports(sid, "suspend"):
                continue
            if counts[sid] != 1:
                continue
            if agent["status"] not in {"idle", "done"}:
                self.idle_observed.pop(sid, None)
                continue
            observed = self.idle_observed.setdefault(sid, now)
            if now - observed < self.grace_seconds or self.managed(sid):
                continue
            previous = self.store.one(
                "SELECT last_wake FROM suspensions WHERE session_id=?", (sid,)
            )
            if previous and now - previous["last_wake"] < self.idle_seconds:
                continue
            try:
                recipe = self.prepare(agent, now)
                # Re-read timestamps and queued input at the last boundary before exit.
                if self.catalog.get(sid)["last_activity"] != recipe["last_activity"]:
                    continue
                with self.store.lock:
                    if self.pending(sid):
                        continue
                    self.store.write(
                        """
                        INSERT INTO suspensions
                            (session_id,pane_id,terminal_id,resume_args,executable,shell_summary,state,changed_at)
                        VALUES (?,?,?,?,?,?, 'suspending', ?)
                        ON CONFLICT(session_id) DO UPDATE SET pane_id=excluded.pane_id,
                            terminal_id=excluded.terminal_id,resume_args=excluded.resume_args,
                            executable=excluded.executable,shell_prompt=NULL,
                            shell_summary=excluded.shell_summary,summary_attempted=0,
                            resume_attempt_at=0,state='suspending',changed_at=excluded.changed_at,last_error=NULL,notified_state=NULL
                    """,
                        (
                            sid,
                            recipe["pane_id"],
                            recipe["terminal_id"],
                            json.dumps(recipe["args"]),
                            recipe["executable"],
                            recipe["shell_summary"],
                            now,
                        ),
                    )
                self.herdr.prompt(recipe["pane_id"], sid, "/quit")
                self.store.write(
                    "UPDATE sessions SET status='suspending' WHERE session_id=?", (sid,)
                )
                LOG.info(
                    "Requested idle suspension for %s in %s", sid, recipe["pane_id"]
                )
                changed = True
                suspended += 1
                if suspended >= 2:
                    break
            except (NotSafeToSuspend, OSError, ValueError, sqlite3.Error):
                continue
            except Exception:
                LOG.exception("Suspension result is uncertain for %s", sid)
        return changed
