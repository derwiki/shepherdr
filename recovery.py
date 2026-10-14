"""Retry interrupted Codex turns after confirmed transient provider failures."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from shepherdr.agents import supports

if __package__:
    from .suspender import CodexCatalog, empty_composer
else:
    from suspender import CodexCatalog, empty_composer

LOG = logging.getLogger("shepherdr")
RETRY_PROMPT = "Try again. Continue the interrupted task from where it stopped."
CAPACITY_PROMPT = "Continue the interrupted task from where it stopped."
RECOVERY_PROMPTS = {RETRY_PROMPT, CAPACITY_PROMPT}
MODEL_CAPACITY = "model capacity"
SWITCH_TIMEOUT = 120
TAIL_BYTES = 2 * 1024 * 1024
PERMANENT = re.compile(
    r"insufficient[_ ]quota|exceeded.*quota|billing|credit balance|invalid[_ ]api[_ ]key|"
    r"authentication|unauthorized|permission denied|context[_ ]length|"
    r"tokens per day|requests per day|\b(?:TPD|RPD)\b|cancelled|canceled",
    re.IGNORECASE,
)
DURATION = re.compile(
    r"(\d+(?:\.\d+)?)\s*(milliseconds?|minutes?|seconds?|hours?|ms|s|m|h)(?![a-z])",
    re.IGNORECASE,
)


def transient_failure(error: dict[str, Any]) -> tuple[str, float] | None:
    """Read native error fields, never quoted Slack or assistant message text."""
    message = str(error.get("message", ""))
    info = error.get("codex_error_info") or error.get("code") or ""
    if isinstance(info, dict):
        info = " ".join(str(key) for key in info)
    text = f"{info} {message}"
    if PERMANENT.search(text):
        return None
    if re.search(r"selected model is at capacity", message, re.IGNORECASE):
        return MODEL_CAPACITY, 0
    size = re.search(
        r"Limit\s*:?\s*(\d+),\s*Used\s*:?\s*\d+,\s*Requested\s*:?\s*(\d+)",
        message,
        re.IGNORECASE,
    )
    if size and int(size[2]) > int(size[1]):
        return None
    if re.search(r"rate[_ ]limit|too many requests|\b429\b", text, re.IGNORECASE):
        kind = "rate limit"
    elif re.search(
        r"internal_server_error|server[_ ]overloaded|service unavailable|\b50[234]\b",
        text,
        re.IGNORECASE,
    ):
        kind = "temporary server error"
    elif re.search(
        r"response_stream_disconnected|http_connection_failed|connection reset|connection closed|timed out|timeout",
        text,
        re.IGNORECASE,
    ):
        kind = "connection failure"
    else:
        return None
    delay = 0.0
    hint = re.search(r"try again in\s+", message, re.IGNORECASE)
    if hint:
        remaining = message[hint.end() :]
        while duration := DURATION.match(remaining):
            number, unit = float(duration[1]), duration[2].lower()
            factor = (
                0.001
                if unit.startswith("mill") or unit == "ms"
                else 60
                if unit.startswith("m")
                else 3600
                if unit.startswith("h")
                else 1
            )
            delay += number * factor
            remaining = remaining[duration.end() :].lstrip()
    header = re.search(r"retry-after\s*[:=]\s*(\d+(?:\.\d+)?)", message, re.IGNORECASE)
    if header:
        delay = max(delay, float(header[1]))
    return (kind, delay) if math.isfinite(delay) else None


def menu_options(text: str, title: str) -> list[tuple[int, str, bool]]:
    headings = list(re.finditer(title, text, re.IGNORECASE | re.MULTILINE))
    if empty_composer(text) or not headings:
        return []
    if not re.fullmatch(r"enter .*esc back", text.rstrip().splitlines()[-1].strip()):
        return []
    return [
        (int(match[2]), match[3], bool(match[1]))
        for match in re.finditer(
            r"^\s*([›»])?\s*(\d+)\.\s+([^\n]+)$",
            text[headings[-1].end() :],
            re.MULTILINE,
        )
    ]


def select_fallback(
    options: list[tuple[int, str, bool]], model: str | None
) -> tuple[int, str] | None:
    if not model:
        return None
    for index, label, _ in options:
        if re.match(re.escape(model) + r"(?=\s|$)", label, re.IGNORECASE):
            return index, model.lower()
    return None


def selection_keys(
    options: list[tuple[int, str, bool]], index: int, *, session_only: bool = False
) -> list[str]:
    selected = [number for number, _, active in options if active]
    if len(selected) != 1:
        raise ValueError("The model picker selection is unknown")
    delta = index - selected[0]
    return ["down" if delta > 0 else "up"] * abs(delta) + [
        "s" if session_only else "enter"
    ]


def active_model(text: str) -> tuple[str, str] | None:
    matches = list(
        re.finditer(
            r"^\s*([a-z0-9][a-z0-9_.:/-]*)\s+"
            r"(none|minimal|low|medium|high|xhigh|max|ultra)\s*·",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
    )
    return (matches[-1][1].lower(), matches[-1][2].lower()) if matches else None


class Recovery:
    def __init__(
        self,
        bridge: Any,
        max_attempts: int = 3,
        episode_seconds: float = 3600,
        fallback_model: str | None = None,
    ):
        self.bridge = bridge
        self.store = bridge.store
        self.herdr = bridge.herdr
        self.max_attempts = max_attempts
        self.episode_seconds = episode_seconds
        self.fallback_model = fallback_model
        self.catalog = CodexCatalog(bridge.rollouts.home)
        self.store.write("""CREATE TABLE IF NOT EXISTS recoveries (
            session_id TEXT PRIMARY KEY, pane_id TEXT NOT NULL, terminal_id TEXT,
            rollout_path TEXT NOT NULL, offset INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'watching', error_key TEXT,
            kind TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            first_error REAL NOT NULL DEFAULT 0, due_at REAL NOT NULL DEFAULT 0,
            sent_at REAL NOT NULL DEFAULT 0, notice_key TEXT
        )""")
        self.store.write("""CREATE TABLE IF NOT EXISTS recovery_inputs (
            session_id TEXT NOT NULL, error_key TEXT NOT NULL, attempt INTEGER NOT NULL,
            sent_at REAL NOT NULL, input_id TEXT,
            PRIMARY KEY(session_id,error_key,attempt)
        )""")
        columns = {row[1] for row in self.store.all("PRAGMA table_info(recoveries)")}
        upgrading = "source_model" not in columns
        for name in (
            "source_model",
            "source_effort",
            "fallback_model",
            "switch_step",
            "recovery_error",
        ):
            if name not in columns:
                self.store.write(f"ALTER TABLE recoveries ADD COLUMN {name} TEXT")
        if upgrading:
            self.store.write(
                "UPDATE recoveries SET kind='legacy server error' WHERE state='pending' AND kind='temporary server error'"
            )

    def pending(self, sid: str) -> bool:
        row = self.store.one("SELECT state FROM recoveries WHERE session_id=?", (sid,))
        return bool(
            row
            and row["state"]
            in {"pending", "switching", "closing_picker", "sending", "submitted"}
        )

    def switching(self, sid: str) -> bool:
        row = self.store.one("SELECT state FROM recoveries WHERE session_id=?", (sid,))
        return bool(row and row["state"] in {"switching", "closing_picker"})

    def reset(self, sid: str, manual: bool = False) -> None:
        self.store.write(
            "UPDATE recoveries SET state='watching',error_key=NULL,kind=NULL,due_at=0,"
            "source_model=NULL,source_effort=NULL,fallback_model=NULL,switch_step=NULL,recovery_error=NULL"
            + (",attempts=0,first_error=0" if manual else "")
            + " WHERE session_id=?",
            (sid,),
        )

    def observe(self, sid: str, record: dict[str, Any], now: float) -> None:
        payload = record.get("payload") or {}
        event = payload.get("type") if record.get("type") == "event_msg" else None
        row = self.store.one("SELECT * FROM recoveries WHERE session_id=?", (sid,))
        if event == "user_message":
            auto = (
                payload.get("message") in RECOVERY_PROMPTS
                and row["sent_at"]
                and row["state"] in {"sending", "submitted", "watching"}
            )
            self.reset(sid, manual=not auto)
            return
        if record.get("type") == "response_item" and payload.get("role") == "user":
            meta = payload.get("internal_chat_message_metadata_passthrough") or {}
            if any(
                str(kind).startswith("user.")
                for kind in meta.get("content_item_kinds", [])
            ):
                self.reset(sid, manual=not self.monitor_input(sid, payload))
            return
        if event == "task_started":
            self.reset(sid)
            return
        if event == "turn_aborted" or (
            event == "task_complete" and not payload.get("error")
        ):
            self.reset(sid, manual=True)
            return
        if event == "task_complete":
            error = payload.get("error")
        elif event == "error" and not payload.get("will_retry"):
            error = payload
        else:
            return
        if not isinstance(error, dict):
            return
        failure = transient_failure(error)
        if failure is None:
            self.reset(sid, manual=True)
            return
        key = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        if row["error_key"] == key:
            return
        if self.store.one(
            "SELECT 1 FROM recovery_inputs WHERE session_id=? AND error_key=? LIMIT 1",
            (sid, key),
        ):
            # A rewritten rollout can replay an already handled failure.
            return
        kind, server_delay = failure
        if kind == MODEL_CAPACITY and not self.fallback_model:
            kind = "temporary server error"
        if kind == MODEL_CAPACITY:
            try:
                settings = self.catalog.get(sid)
                model = settings.get("model") or ""
                effort = settings.get("reasoning_effort") or ""
                if not model or any(character.isspace() for character in model):
                    raise ValueError("The selected model is unknown")
                model = model.lower()
                if model == self.fallback_model.lower():
                    raise ValueError("The selected model is already the fallback model")
                if effort not in {
                    "none",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                }:
                    raise ValueError("The saved reasoning effort is unknown")
            except (OSError, ValueError, sqlite3.Error) as error:
                self.store.write(
                    "UPDATE recoveries SET state='exhausted',error_key=?,kind=?,recovery_error=? WHERE session_id=?",
                    (key, kind, str(error), sid),
                )
                return
            self.store.write(
                "UPDATE recoveries SET source_model=?,source_effort=?,fallback_model=NULL,switch_step=NULL,recovery_error=NULL WHERE session_id=?",
                (model, "low" if effort in {"none", "minimal"} else effort, sid),
            )
        first = row["first_error"] or now
        delay = (
            0
            if kind == MODEL_CAPACITY
            else max(server_delay, min(10 * 2 ** row["attempts"], 300))
            + random.uniform(0.5, 2.5)
        )
        exhausted = (
            row["attempts"] >= self.max_attempts
            or now + delay - first >= self.episode_seconds
        )
        self.store.write(
            "UPDATE recoveries SET state=?,error_key=?,kind=?,first_error=?,due_at=? WHERE session_id=?",
            (
                "exhausted" if exhausted else "pending",
                key,
                kind,
                first,
                now + delay,
                sid,
            ),
        )

    def monitor_input(self, sid: str, payload: dict[str, Any]) -> bool:
        text = "\n".join(
            c.get("text", "")
            for c in payload.get("content", [])
            if c.get("type") == "input_text"
        ).strip()
        item_id = payload.get("id")
        if text not in RECOVERY_PROMPTS or not item_id:
            return False
        if self.store.one(
            "SELECT 1 FROM recovery_inputs WHERE session_id=? AND input_id=?",
            (sid, item_id),
        ):
            return True
        created = (payload.get("internal_chat_message_metadata_passthrough") or {}).get(
            "create_time"
        )
        for row in self.store.all(
            "SELECT * FROM recovery_inputs WHERE session_id=? AND input_id IS NULL ORDER BY sent_at",
            (sid,),
        ):
            if (
                isinstance(created, (int, float))
                and abs(created - row["sent_at"]) <= 300
            ):
                self.store.write(
                    "UPDATE recovery_inputs SET input_id=? WHERE session_id=? AND error_key=? AND attempt=?",
                    (item_id, sid, row["error_key"], row["attempt"]),
                )
                return True
        return False

    def scan(self, agent: dict[str, Any], now: float) -> Any:
        sid = agent["session_id"]
        session = self.store.one(
            "SELECT rollout_path FROM sessions WHERE session_id=?", (sid,)
        )
        if not session or not session["rollout_path"]:
            return None
        path = Path(session["rollout_path"])
        row = self.store.one("SELECT * FROM recoveries WHERE session_id=?", (sid,))
        if row is None:
            with path.open("rb") as stream:
                meta = json.loads(stream.readline(TAIL_BYTES))
            identity = meta.get("payload") or {}
            if meta.get("type") != "session_meta" or sid not in {
                identity.get("id"),
                identity.get("session_id"),
            }:
                raise ValueError(
                    "Saved rollout identity does not match the live session"
                )
            start = max(0, path.stat().st_size - TAIL_BYTES)
            if start:
                with path.open("rb") as stream:
                    stream.seek(start)
                    stream.readline()  # The tail can start within a JSON record.
                    start = stream.tell()
            self.store.write(
                "INSERT INTO recoveries(session_id,pane_id,terminal_id,rollout_path,offset) VALUES (?,?,?,?,?)",
                (sid, agent["pane_id"], agent.get("terminal_id"), str(path), start),
            )
            row = self.store.one("SELECT * FROM recoveries WHERE session_id=?", (sid,))
        if (
            row["pane_id"] != agent["pane_id"]
            or row["terminal_id"] != agent.get("terminal_id")
            or row["rollout_path"] != str(path)
            or path.stat().st_size < row["offset"]
        ):
            # A changed pane or rewritten history requires a fresh observation.
            self.reset(sid, manual=True)
            self.store.write("DELETE FROM recoveries WHERE session_id=?", (sid,))
            return None
        if row["kind"] == "legacy server error":
            with path.open("rb") as stream:
                start = max(0, row["offset"] - TAIL_BYTES)
                stream.seek(start)
                tail = stream.read(row["offset"] - start)
            for line in reversed(tail.splitlines()):
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                key = hashlib.sha256(
                    json.dumps(record, sort_keys=True).encode()
                ).hexdigest()
                if key == row["error_key"]:
                    self.store.write(
                        "UPDATE recoveries SET error_key=NULL WHERE session_id=?",
                        (sid,),
                    )
                    self.observe(sid, record, now)
                    break
            else:
                self.store.write(
                    "UPDATE recoveries SET state='exhausted',kind=NULL,recovery_error='The saved error could not be verified after upgrade' WHERE session_id=?",
                    (sid,),
                )
        started = time.monotonic()
        for offset, record in self.bridge.rollouts.records(path, row["offset"]):
            with self.store.lock, self.store.db:
                self.observe(sid, record, now)
                self.store.db.execute(
                    "UPDATE recoveries SET offset=? WHERE session_id=?", (offset, sid)
                )
            if time.monotonic() - started > 2:
                break
        return self.store.one("SELECT * FROM recoveries WHERE session_id=?", (sid,))

    def notice(self, row: Any) -> None:
        state = row["state"]
        if state == "closing_picker" and row["recovery_error"]:
            state = "exhausted"
        if state not in {"submitted", "sending", "exhausted"}:
            return
        key = f"{row['error_key']}:{state}:{row['attempts']}"
        if row["notice_key"] == key:
            return
        if state == "submitted":
            if row["kind"] == MODEL_CAPACITY:
                reason = "was at capacity"
                text = (
                    f"Monitor: {row['source_model']} {reason}. Switched this session "
                    f"to {row['fallback_model']} ({row['source_effort']}) and continued the interrupted task."
                )
            else:
                text = f"Monitor: retry {row['attempts']}/{self.max_attempts} sent after a {row['kind']}."
        elif state == "sending":
            text = "Monitor: retry delivery is uncertain. Check the pane before you send another turn."
        elif row["kind"] == MODEL_CAPACITY and row["recovery_error"]:
            text = f"Monitor: model-switch recovery stopped: {row['recovery_error']}. Check the pane to continue."
        else:
            text = "Monitor: automatic retries stopped after repeated failures or a long server delay. Check the pane and send a new turn to continue."
        session = self.store.one(
            "SELECT thread_ts FROM sessions WHERE session_id=?", (row["session_id"],)
        )
        if session and session["thread_ts"]:
            self.bridge.post_slack(
                text,
                thread_ts=session["thread_ts"],
                client_msg_id=self.bridge.lifecycle_message_id(
                    row["session_id"], f"recovery:{key}", 0
                ),
            )
            self.store.write(
                "UPDATE recoveries SET notice_key=? WHERE session_id=?",
                (key, row["session_id"]),
            )

    def user_pending(self, sid: str) -> bool:
        return bool(
            self.store.one(
                "SELECT 1 FROM inbound WHERE session_id=? AND (state IN ('pending','sending') OR (state='submitted' AND seen_in_rollout=0)) LIMIT 1",
                (sid,),
            )
        )

    def ready(self, agent: dict[str, Any], row: Any) -> bool:
        sid = row["session_id"]
        if agent["status"] not in {"idle", "done"} or self.user_pending(sid):
            return False
        if self.native_input_pending(sid):
            return False
        live = self.herdr.request("agent.get", {"target": row["pane_id"]})["agent"]
        identity = live.get("agent_session") or {}
        if (
            live.get("terminal_id") != row["terminal_id"]
            or live.get("agent") != "codex"
            or identity.get("kind") != "id"
            or identity.get("value") != sid
            or live.get("agent_status") not in {"idle", "done"}
        ):
            return False
        screen = self.herdr.request(
            "agent.read", {"target": row["pane_id"], "source": "detection", "lines": 30}
        )["read"]["text"]
        if row["kind"] == MODEL_CAPACITY:
            settings = active_model(screen)
            if row["switch_step"] == "verified":
                if settings != (row["fallback_model"], row["source_effort"]):
                    return False
            elif not settings or settings[0] != row["source_model"]:
                return False
        return (
            empty_composer(screen)
            and Path(row["rollout_path"]).stat().st_size == row["offset"]
        )

    def switch_model(self, row: Any, now: float) -> bool:
        sid = row["session_id"]
        live = self.herdr.request("agent.get", {"target": row["pane_id"]})["agent"]
        identity = live.get("agent_session") or {}
        if (
            live.get("terminal_id") != row["terminal_id"]
            or live.get("agent") != "codex"
            or identity.get("kind") != "id"
            or identity.get("value") != sid
        ):
            self.reset(sid, manual=True)
            return False
        if now >= row["due_at"]:
            raise ValueError("The model switch did not finish within two minutes")
        if live.get("agent_status") not in {"idle", "done"}:
            return False
        screen = self.herdr.request(
            "agent.read", {"target": row["pane_id"], "source": "detection", "lines": 40}
        )["read"]["text"]
        models = menu_options(screen, r"^\s*Select Model(?: and Effort)?\s*$")
        reasoning = menu_options(
            screen,
            r"^\s*Select Reasoning Level for "
            + re.escape(row["fallback_model"] or "")
            + r"\s*$",
        )
        advanced = (
            menu_options(screen, r"^\s*Advanced Reasoning\s*$")
            if row["switch_step"] in {"advanced", "verifying"}
            else []
        )
        if self.user_pending(sid) or self.native_input_pending(sid):
            self.store.write(
                "UPDATE recoveries SET state='closing_picker' WHERE session_id=?",
                (sid,),
            )
            return self.close_model_picker(row)
        if models:
            choice = select_fallback(models, self.fallback_model)
            if choice is None:
                all_models = next(
                    (
                        number
                        for number, label, _ in models
                        if label.startswith("All models")
                    ),
                    None,
                )
                if all_models is None:
                    raise ValueError(
                        "The configured fallback model is not available in the model picker"
                    )
                keys = selection_keys(models, all_models)
            else:
                number, model = choice
                self.store.write(
                    "UPDATE recoveries SET fallback_model=?,switch_step='model' WHERE session_id=?",
                    (model, sid),
                )
                keys = selection_keys(models, number)
        elif reasoning or advanced:
            options = reasoning or advanced
            label = (
                "Extra high"
                if row["source_effort"] == "xhigh"
                else row["source_effort"]
            )
            number = next(
                (
                    number
                    for number, name, _ in options
                    if re.match(re.escape(label) + r"(?=\s|$|\()", name, re.IGNORECASE)
                ),
                None,
            )
            if (
                number is None
                and reasoning
                and row["source_effort"] in {"max", "ultra"}
            ):
                number = next(
                    (n for n, name, _ in options if name.startswith("More reasoning")),
                    None,
                )
                if number is not None:
                    self.store.write(
                        "UPDATE recoveries SET switch_step='advanced' WHERE session_id=?",
                        (sid,),
                    )
                    keys = selection_keys(options, number)
                else:
                    raise ValueError(
                        "The saved reasoning effort is not available on the fallback model"
                    )
            elif number is not None and "s session" in screen:
                self.store.write(
                    "UPDATE recoveries SET switch_step='verifying' WHERE session_id=?",
                    (sid,),
                )
                keys = selection_keys(options, number, session_only=True)
            else:
                raise ValueError(
                    "The picker cannot apply the saved effort to this session only"
                )
        elif (
            row["switch_step"] == "verifying"
            and empty_composer(screen)
            and active_model(screen) == (row["fallback_model"], row["source_effort"])
        ):
            self.store.write(
                "UPDATE recoveries SET state='pending',switch_step='verified',due_at=? WHERE session_id=?",
                (now, sid),
            )
            return True
        else:
            return False
        current = self.herdr.request("agent.get", {"target": row["pane_id"]})["agent"]
        identity = current.get("agent_session") or {}
        if (
            current.get("terminal_id") != row["terminal_id"]
            or current.get("agent") != "codex"
            or identity.get("kind") != "id"
            or identity.get("value") != sid
            or current.get("agent_status") not in {"idle", "done"}
            or self.user_pending(sid)
            or self.native_input_pending(sid)
        ):
            return False
        self.herdr.request("agent.send_keys", {"target": row["pane_id"], "keys": keys})
        return True

    def close_model_picker(self, row: Any) -> bool:
        live = self.herdr.request("agent.get", {"target": row["pane_id"]})["agent"]
        identity = live.get("agent_session") or {}
        if (
            live.get("terminal_id") != row["terminal_id"]
            or live.get("agent") != "codex"
            or identity.get("kind") != "id"
            or identity.get("value") != row["session_id"]
        ):
            self.reset(row["session_id"], manual=True)
            return False
        if live.get("agent_status") not in {"idle", "done"}:
            return False
        screen = self.herdr.request(
            "agent.read", {"target": row["pane_id"], "source": "detection", "lines": 40}
        )["read"]["text"]
        if empty_composer(screen):
            if (
                row["recovery_error"]
                and not self.user_pending(row["session_id"])
                and not self.native_input_pending(row["session_id"])
            ):
                self.store.write(
                    "UPDATE recoveries SET state='exhausted' WHERE session_id=?",
                    (row["session_id"],),
                )
            else:
                self.reset(row["session_id"], manual=True)
            return True
        if (
            menu_options(screen, r"^\s*Select Model(?: and Effort)?\s*$")
            or menu_options(
                screen,
                r"^\s*Select Reasoning Level for "
                + re.escape(row["fallback_model"] or "")
                + r"\s*$",
            )
            or (
                row["switch_step"] in {"advanced", "verifying"}
                and menu_options(screen, r"^\s*Advanced Reasoning\s*$")
            )
        ):
            self.herdr.request(
                "agent.send_keys", {"target": row["pane_id"], "keys": ["esc"] * 3}
            )
            return True
        return False

    def native_input_pending(self, sid: str) -> bool:
        queue = self.bridge.rollouts.home / "queue_1.sqlite"
        if not queue.exists():
            return False
        with closing(
            sqlite3.connect(queue.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as db:
            return (
                db.execute(
                    "SELECT 1 FROM queued_items WHERE thread_id=? LIMIT 1", (sid,)
                ).fetchone()
                is not None
            )

    def tick(self, agents: list[dict[str, Any]], now: float | None = None) -> bool:
        now = time.time() if now is None else now
        changed = False
        counts = {
            a["session_id"]: sum(b["session_id"] == a["session_id"] for b in agents)
            for a in agents
        }
        for agent in agents:
            sid = agent["session_id"]
            if not supports(sid, "recover"):
                continue
            if counts[sid] != 1:
                continue
            try:
                row = self.scan(agent, now)
                if not row:
                    continue
                if self.user_pending(sid) and row["state"] == "pending":
                    self.reset(sid, manual=True)
                    continue
                if not changed and row["state"] in {"switching", "closing_picker"}:
                    try:
                        changed = (
                            self.close_model_picker(row)
                            if row["state"] == "closing_picker"
                            else self.switch_model(row, now)
                        )
                    except ValueError as error:
                        self.store.write(
                            "UPDATE recoveries SET state='closing_picker',recovery_error=? WHERE session_id=?",
                            (str(error), sid),
                        )
                        changed = self.close_model_picker(
                            self.store.one(
                                "SELECT * FROM recoveries WHERE session_id=?", (sid,)
                            )
                        )
                elif (
                    row["state"] == "pending"
                    and now - row["first_error"] >= self.episode_seconds
                ):
                    self.store.write(
                        "UPDATE recoveries SET state='exhausted' WHERE session_id=?",
                        (sid,),
                    )
                elif (
                    not changed
                    and row["state"] == "pending"
                    and now >= row["due_at"]
                    and self.ready(agent, row)
                ):
                    if (
                        row["kind"] == MODEL_CAPACITY
                        and row["switch_step"] != "verified"
                    ):
                        with self.store.lock, self.store.db:
                            if (
                                self.user_pending(sid)
                                or self.native_input_pending(sid)
                                or Path(row["rollout_path"]).stat().st_size
                                != row["offset"]
                            ):
                                continue
                            self.store.db.execute(
                                "UPDATE recoveries SET state='switching',switch_step='opening',due_at=? WHERE session_id=?",
                                (now + SWITCH_TIMEOUT, sid),
                            )
                        try:
                            self.herdr.prompt(row["pane_id"], sid, "/model")
                        except (OSError, RuntimeError, ValueError) as error:
                            if getattr(error, "code", None) in {
                                "agent_not_ready",
                                "agent_blocked",
                                "session_changed",
                            }:
                                self.store.write(
                                    "UPDATE recoveries SET state='pending',switch_step=NULL,due_at=? WHERE session_id=?",
                                    (now, sid),
                                )
                            else:
                                LOG.warning(
                                    "Model picker delivery is uncertain for %s: %s",
                                    sid,
                                    error,
                                )
                        changed = True
                        continue
                    with self.store.lock, self.store.db:
                        if (
                            self.user_pending(sid)
                            or self.native_input_pending(sid)
                            or Path(row["rollout_path"]).stat().st_size != row["offset"]
                        ):
                            continue
                        attempt = row["attempts"] + 1
                        self.store.db.execute(
                            "UPDATE recoveries SET state='sending',attempts=?,sent_at=? WHERE session_id=?",
                            (attempt, now, sid),
                        )
                        self.store.db.execute(
                            "INSERT INTO recovery_inputs(session_id,error_key,attempt,sent_at) VALUES (?,?,?,?)",
                            (sid, row["error_key"], attempt, now),
                        )
                    try:
                        self.herdr.prompt(
                            row["pane_id"],
                            sid,
                            CAPACITY_PROMPT
                            if row["kind"] == MODEL_CAPACITY
                            else RETRY_PROMPT,
                        )
                    except (OSError, RuntimeError, ValueError) as error:
                        if getattr(error, "code", None) in {
                            "agent_not_ready",
                            "agent_blocked",
                            "session_changed",
                        }:
                            self.store.write(
                                "UPDATE recoveries SET state='pending',attempts=?,sent_at=0 WHERE session_id=?",
                                (row["attempts"], sid),
                            )
                            self.store.write(
                                "DELETE FROM recovery_inputs WHERE session_id=? AND error_key=? AND attempt=?",
                                (sid, row["error_key"], attempt),
                            )
                        else:
                            LOG.warning(
                                "Retry delivery is uncertain for %s: %s", sid, error
                            )
                    else:
                        self.store.write(
                            "UPDATE recoveries SET state='submitted' WHERE session_id=?",
                            (sid,),
                        )
                        LOG.info(
                            "Sent automatic retry %d/%d to %s after %s",
                            attempt,
                            self.max_attempts,
                            sid,
                            row["kind"],
                        )
                    changed = True
                self.notice(
                    self.store.one(
                        "SELECT * FROM recoveries WHERE session_id=?", (sid,)
                    )
                )
            except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error):
                LOG.exception("Cannot check transient recovery for %s", sid)
        return changed
