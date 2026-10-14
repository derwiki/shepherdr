#!/usr/bin/env python3
"""Shepherdr mirrors supported Herdr sessions to Slack and routes replies back.

`--slack-mode none` skips Slack entirely: the monitor only runs scheduled and
runtime jobs, and those are followed through `shepherdr runtime --status`.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from shepherdr.agents import (
    INTEGRATIONS,
    PATH_INTEGRATIONS,
    SessionHistories,
    config_path,
    empty_composer,
    integration_for,
    load_config,
    session_key,
    supports,
)

LOG = logging.getLogger("shepherdr")
SESSION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
STATE_DB = re.compile(r"state_\d+\.sqlite")
MAX_TEXT = 35_000
SAFE_SLACK_BYTES = 3_000


class HerdrError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class HerdrClient:
    """Use the caller's Herdr socket, including with an older running server."""

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.unidentified_panes: set[str] = set()
        self.conflicting_sessions: dict[str, tuple[str, ...]] = {}

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 10
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        payload = {"id": request_id, "method": method, "params": params or {}}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(self.socket_path))
            client.sendall((json.dumps(payload) + "\n").encode())
            with client.makefile("rb") as stream:
                line = stream.readline(8 * 1024 * 1024)
        if not line.endswith(b"\n"):
            raise HerdrError("bad_response", "Herdr returned an incomplete response")
        response = json.loads(line)
        if response.get("error"):
            error = response["error"]
            raise HerdrError(error.get("code", "unknown"), error.get("message", ""))
        if response.get("id") != request_id:
            raise HerdrError("bad_response", "Herdr returned a different request ID")
        result = response.get("result")
        if not isinstance(result, dict):
            raise HerdrError("bad_response", "Herdr returned no result")
        return result

    def agents(self) -> list[dict[str, str]]:
        snapshot = self.request("session.snapshot")["snapshot"]
        workspaces = {
            row["workspace_id"]: row["label"] for row in snapshot["workspaces"]
        }
        tabs = {row["tab_id"]: row["label"] for row in snapshot["tabs"]}
        result = []
        unidentified = set()
        for row in snapshot["agents"]:
            if row.get("agent") not in INTEGRATIONS:
                continue
            session_id = session_key(row)
            if session_id is None:
                unidentified.add(row["pane_id"])
                continue
            result.append(
                {
                    "session_id": session_id,
                    "agent": row["agent"],
                    "pane_id": row["pane_id"],
                    "workspace": workspaces.get(
                        row["workspace_id"], row["workspace_id"]
                    ),
                    "tab": tabs.get(row["tab_id"], row["tab_id"]),
                    "status": row.get("agent_status", "unknown"),
                    "terminal_id": row.get("terminal_id"),
                    "cwd": row.get("cwd"),
                    "focused": row.get("focused", False),
                }
            )
        session_panes: dict[str, list[str]] = {}
        identity_keys: dict[str, str] = {}
        for agent in result:
            sid = agent["session_id"]
            prefix, _, path = sid.partition(":")
            canonical = (
                prefix + ":" + str(Path(path).resolve())
                if prefix in PATH_INTEGRATIONS
                else sid
            )
            identity_keys[sid] = canonical
            session_panes.setdefault(canonical, []).append(agent["pane_id"])
        conflicts = {
            sid: tuple(sorted(session_panes[key]))
            for sid, key in identity_keys.items()
            if len(session_panes[key]) > 1
        }
        if conflicts != self.conflicting_sessions:
            if conflicts:
                LOG.warning(
                    "Cannot route Slack replies to sessions reported by multiple panes: %s",
                    conflicts,
                )
            self.conflicting_sessions = conflicts
        # A stale integration can assign another agent's ID to this pane.
        # Do not choose a destination based on snapshot iteration order.
        result = [agent for agent in result if agent["session_id"] not in conflicts]
        if unidentified != self.unidentified_panes:
            if unidentified:
                LOG.warning(
                    "Cannot mirror panes without a native session identity: %s. "
                    "Check the Herdr agent integration.",
                    ", ".join(sorted(unidentified)),
                )
            elif self.unidentified_panes:
                LOG.info("All supported panes now have native session identities")
            self.unidentified_panes = unidentified
        return result

    def prompt(self, pane_id: str, session_id: str, text: str) -> None:
        # Check the exact session immediately before sending to an opaque pane ID.
        agent = self.request("agent.get", {"target": pane_id})["agent"]
        if session_key(agent) != session_id:
            raise HerdrError(
                "session_changed", "The pane has a different agent session"
            )
        if not supports(session_id, "prompt"):
            raise HerdrError(
                "unsupported_action", "This integration cannot send a turn"
            )
        if agent.get("agent_status") not in {"idle", "done"}:
            raise HerdrError("agent_not_ready", "The agent is not ready for a new turn")
        integration = integration_for(session_id)
        if integration in PATH_INTEGRATIONS:
            screen = self.request(
                "pane.read", {"pane_id": pane_id, "source": "detection", "lines": 30}
            )["read"]["text"]
            if not empty_composer(integration, screen):
                raise HerdrError(
                    "agent_not_ready", f"The {integration} editor is not empty"
                )
        # Check duplicate native identities at the delivery boundary, including
        # wrappers that resume an existing session in a second pane.
        current = next(
            (a for a in self.agents() if a["session_id"] == session_id), None
        )
        if (
            current is None
            or current["pane_id"] != pane_id
            or current.get("terminal_id") != agent.get("terminal_id")
        ):
            raise HerdrError(
                "session_changed",
                "The pane session changed or has a duplicate identity",
            )
        if current["status"] not in {"idle", "done"}:
            raise HerdrError("agent_not_ready", "The agent is not ready for a new turn")
        self.request("agent.prompt", {"target": pane_id, "text": text})


class CodexRollouts:
    def __init__(self, home: Path):
        self.home = home

    def find_paths(self, session_ids: set[str]) -> dict[str, Path]:
        found: dict[str, Path] = {}
        databases = sorted(
            (
                path
                for path in self.home.glob("state_*.sqlite")
                if STATE_DB.fullmatch(path.name)
            ),
            key=lambda path: int(path.stem.split("_")[-1]),
            reverse=True,
        )
        for database in databases:
            missing = sorted(session_ids - found.keys())
            if not missing:
                break
            try:
                with closing(
                    sqlite3.connect(
                        database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
                    )
                ) as db:
                    columns = {
                        row[1] for row in db.execute("PRAGMA table_info(threads)")
                    }
                    if not {"id", "rollout_path"} <= columns:
                        continue
                    for start in range(0, len(missing), 200):
                        batch = missing[start : start + 200]
                        marks = ",".join("?" for _ in batch)
                        for session_id, filename in db.execute(
                            f"SELECT id, rollout_path FROM threads WHERE id IN ({marks})",
                            batch,
                        ):
                            if filename and Path(filename).is_file():
                                found[session_id.lower()] = Path(filename)
            except (OSError, sqlite3.Error) as error:
                LOG.warning("Cannot read Codex state %s: %s", database.name, error)
        for session_id in session_ids - found.keys():
            # This is an exact-ID fallback for sessions not yet indexed in SQLite.
            for folder in ("sessions", "archived_sessions"):
                matches = (self.home / folder).glob(f"*/*/*/*{session_id}.jsonl")
                path = next(matches, None)
                if path:
                    found[session_id] = path
                    break
        return found

    @staticmethod
    def records(path: Path, offset: int) -> Iterator[tuple[int, dict[str, Any]]]:
        with path.open("rb") as stream:
            limit = path.stat().st_size
            if limit < offset:
                offset = 0
            stream.seek(offset)
            while stream.tell() < limit:
                line = stream.readline(limit - stream.tell())
                if not line or not line.endswith(b"\n"):
                    break
                end = stream.tell()
                try:
                    yield end, json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    LOG.warning(
                        "Skip invalid Codex record in %s at byte %d", path.name, offset
                    )
                    yield end, {}
                offset = end


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    pane_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    tab TEXT NOT NULL,
                    status TEXT NOT NULL,
                    thread_ts TEXT,
                    root_text TEXT,
                    rollout_path TEXT,
                    offset INTEGER NOT NULL DEFAULT 0,
                    last_seen_slack_ts TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS sessions_thread_ts ON sessions(thread_ts);
                CREATE TABLE IF NOT EXISTS assistant_turns (
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    slack_ts TEXT NOT NULL,
                    text TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    PRIMARY KEY (session_id, turn_id)
                );
                CREATE TABLE IF NOT EXISTS activity_notices (
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    slack_ts TEXT NOT NULL,
                    PRIMARY KEY (session_id, turn_id)
                );
                CREATE TABLE IF NOT EXISTS user_items (
                    item_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    slack_ts TEXT
                );
                CREATE TABLE IF NOT EXISTS ignored_slack_messages (
                    slack_ts TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS inbound (
                    slack_ts TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    sent_at REAL,
                    seen_in_rollout INTEGER NOT NULL DEFAULT 0,
                    acked INTEGER NOT NULL DEFAULT 0
                );
            """)

    def bind(
        self, channel: str, user_id: str, *, workspace_url: str | None = None
    ) -> None:
        """Do not reuse one state file for a different Slack destination or sender."""
        with self.lock, self.db:
            for key, value in (("channel", channel), ("user_id", user_id)):
                previous = self.db.execute(
                    "SELECT value FROM settings WHERE key=?", (key,)
                ).fetchone()
                if previous and previous["value"] != value:
                    raise ValueError(
                        f"State file is already bound to a different Slack {key}"
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, value)
                )
            if workspace_url:
                self.db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('workspace_url', ?)",
                    (workspace_url.rstrip("/"),),
                )

    def thread_url(self, thread_ts: str) -> str | None:
        workspace = self.one("SELECT value FROM settings WHERE key='workspace_url'")
        channel = self.one("SELECT value FROM settings WHERE key='channel'")
        if not workspace or not channel:
            return None
        return (
            f"{workspace['value']}/archives/{channel['value']}/"
            f"p{thread_ts.replace('.', '')}"
        )

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self.lock, self.db:
            self.db.execute(sql, params)

    def discover(
        self, agent: dict[str, str], rollout_path: Path | None, backfill: bool
    ) -> None:
        session_id = agent["session_id"]
        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT rollout_path FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing is None:
                offset = (
                    0
                    if backfill or rollout_path is None
                    else rollout_path.stat().st_size
                )
                self.db.execute(
                    """
                    INSERT INTO sessions
                    (session_id, pane_id, workspace, tab, status, rollout_path, offset)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        session_id,
                        agent["pane_id"],
                        agent["workspace"],
                        agent["tab"],
                        agent["status"],
                        str(rollout_path) if rollout_path else None,
                        offset,
                    ),
                )
            else:
                new_path = str(rollout_path) if rollout_path else None
                self.db.execute(
                    """
                    UPDATE sessions SET pane_id=?, workspace=?, tab=?, status=?,
                        rollout_path=COALESCE(rollout_path, ?),
                        offset=CASE WHEN rollout_path IS NULL AND ? IS NOT NULL THEN 0 ELSE offset END
                    WHERE session_id=?
                """,
                    (
                        agent["pane_id"],
                        agent["workspace"],
                        agent["tab"],
                        agent["status"],
                        new_path,
                        new_path,
                        session_id,
                    ),
                )

    def add_inbound(self, session_id: str, ts: str, text: str) -> None:
        with self.lock, self.db:
            self.db.execute(
                """
                INSERT OR IGNORE INTO inbound (slack_ts, session_id, text, created_at)
                VALUES (?, ?, ?, ?)
            """,
                (ts, session_id, text, time.time()),
            )

    def match_inbound(
        self, session_id: str, text: str, created_at: float | None
    ) -> bool:
        text = text.strip()
        with self.lock, self.db:
            rows = self.db.execute(
                """
                SELECT slack_ts, sent_at, text FROM inbound
                WHERE session_id=? AND state IN ('sending', 'submitted')
                    AND seen_in_rollout=0 ORDER BY created_at
            """,
                (session_id,),
            ).fetchall()
            for row in rows:
                if row["text"].strip() != text:
                    continue
                if (
                    created_at is None
                    or row["sent_at"] is None
                    or abs(created_at - row["sent_at"]) < 300
                ):
                    self.db.execute(
                        """
                        UPDATE inbound SET state='submitted', seen_in_rollout=1
                        WHERE slack_ts=?
                    """,
                        (row["slack_ts"],),
                    )
                    return True
        return False


def message_text(payload: dict[str, Any]) -> str:
    parts = []
    for item in payload.get("content", []):
        if item.get("type") in {"input_text", "output_text"} and isinstance(
            item.get("text"), str
        ):
            parts.append(item["text"])
    return "\n".join(parts).strip()


def limit_text(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + "\n[Message shortened for Slack]"


def slack_parts(text: str) -> list[str]:
    text = limit_text(text)
    if len(text.encode("utf-8")) <= SAFE_SLACK_BYTES:
        return [text]
    label, separator, body = text.partition("\n")
    if not separator:
        label, body = "Message", text
    label = label.rstrip(":")[:80]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for character in body:
        width = len(character.encode("utf-8"))
        if current and size + width > SAFE_SLACK_BYTES - 200:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += width
    if current:
        chunks.append("".join(current))
    return [
        f"{label} (part {index}/{len(chunks)}):\n{chunk}"
        for index, chunk in enumerate(chunks, 1)
    ]


def message_id(channel: str, session_id: str, kind: str, key: str) -> str:
    # This namespace is part of the persisted Slack protocol. Keep it across
    # project renames so retries do not produce duplicate messages.
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"shepherdr:{channel}:{session_id}:{kind}:{key}")
    )


class SlackApi:
    def __init__(self, client: Any, channel: str):
        self.client = client
        self.channel = channel
        self.last_post = 0.0

    def post(
        self,
        text: str,
        *,
        thread_ts: str | None,
        client_msg_id: str,
        on_post: Callable[[str], None] | None = None,
        markdown: bool = False,
    ) -> str:
        last_ts = ""
        parts = slack_parts(text)
        for index, part in enumerate(parts, 1):
            # Slack generally permits one posted message per second in a channel.
            wait = 1.1 - (time.monotonic() - self.last_post)
            if wait > 0:
                time.sleep(wait)
            base_id = (
                client_msg_id
                if len(parts) == 1
                else str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{client_msg_id}:part:{index}")
                )
            )
            for attempt in range(5):
                post_id = (
                    base_id
                    if attempt == 0
                    else str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{base_id}:repost:{attempt}")
                    )
                )
                try:
                    response = self.client.chat_postMessage(
                        channel=self.channel,
                        text=part,
                        thread_ts=thread_ts,
                        client_msg_id=post_id,
                        mrkdwn=markdown,
                        parse="none",
                        unfurl_links=False,
                        unfurl_media=False,
                    )
                    break
                except Exception as error:
                    if (
                        self.error_code(error) != "duplicate_message_not_found"
                        or attempt == 4
                    ):
                        raise
            self.last_post = time.monotonic()
            last_ts = response["ts"]
            if on_post is not None:
                on_post(last_ts)
        return last_ts

    @staticmethod
    def error_code(error: Exception) -> str | None:
        response = getattr(error, "response", None)
        return getattr(error, "code", None) or (
            response.get("error") if response is not None else None
        )

    def update(self, ts: str, text: str) -> bool:
        if len(limit_text(text).encode("utf-8")) > SAFE_SLACK_BYTES:
            return False
        try:
            self.client.chat_update(
                channel=self.channel,
                ts=ts,
                text=limit_text(text),
                mrkdwn=False,
                parse="none",
            )
        except Exception as error:
            if self.error_code(error) in {"edit_window_closed", "msg_too_long"}:
                return False
            raise
        return True

    def react(self, ts: str) -> None:
        try:
            self.client.reactions_add(
                channel=self.channel, timestamp=ts, name="white_check_mark"
            )
        except Exception as error:
            # A previous successful reaction can be retried after a DB failure.
            if "already_reacted" not in str(error):
                raise

    def replies(self, thread_ts: str, oldest: str) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            kwargs = {
                "channel": self.channel,
                "ts": thread_ts,
                "oldest": oldest,
                "limit": 200,
            }
            if cursor:
                kwargs["cursor"] = cursor
            response = self.client.conversations_replies(**kwargs)
            yield from response.get("messages", [])
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break


class Bridge:
    def __init__(
        self,
        herdr: HerdrClient,
        rollouts: CodexRollouts,
        slack: SlackApi | None,
        store: Store,
        channel: str | None,
        user_id: str | None,
        backfill: bool = False,
        search: Any = None,
        activity_notices: bool = False,
    ):
        self.herdr = herdr
        self.rollouts = rollouts
        self.slack = slack
        self.store = store
        self.channel = channel
        self.user_id = user_id
        if slack is not None:
            self.store.bind(channel, user_id)
        self.backfill = backfill
        self.bootstrap_complete = bool(self.store.one("SELECT 1 FROM sessions LIMIT 1"))
        self.search = search
        self.activity_notices = activity_notices
        self.suspender = None
        self.recovery = None
        self.scheduler = None
        self.rollout_index = 0
        if search is not None:
            self.store.write(
                "INSERT OR IGNORE INTO settings VALUES ('client_scan_at', ?)",
                (str(time.time()),),
            )
        self.last_reconcile = 0.0
        self.reconcile_index = 0

    def accept_slack_event(self, event: dict[str, Any]) -> bool:
        if event.get("type") != "message" or event.get("subtype") is not None:
            return False
        if event.get("channel") != self.channel or event.get("user") != self.user_id:
            return False
        ts, thread_ts, text = event.get("ts"), event.get("thread_ts"), event.get("text")
        if not (isinstance(ts, str) and isinstance(thread_ts, str) and ts != thread_ts):
            return False
        if not isinstance(text, str) or not text.strip():
            return False
        if self.search is not None and text.startswith(
            ("Agent:\n", "Agent (working):\n", "Agent (part ", "Local user:\n")
        ):
            return False
        if self.store.one("SELECT 1 FROM assistant_turns WHERE slack_ts=?", (ts,)):
            return False
        if self.store.one("SELECT 1 FROM user_items WHERE slack_ts=?", (ts,)):
            return False
        if self.store.one(
            "SELECT 1 FROM ignored_slack_messages WHERE slack_ts=?", (ts,)
        ):
            return False
        session = self.store.one(
            """
            SELECT session_id FROM sessions WHERE thread_ts=? AND status != 'ended'
        """,
            (thread_ts,),
        )
        if session is None:
            return False
        self.store.add_inbound(session["session_id"], ts, text)
        return True

    def post_slack(
        self,
        text: str,
        *,
        thread_ts: str | None,
        client_msg_id: str,
        markdown: bool = False,
    ) -> str:
        if self.slack is None:
            raise RuntimeError("This monitor runs without Slack")
        return self.slack.post(
            text,
            thread_ts=thread_ts,
            client_msg_id=client_msg_id,
            markdown=markdown,
            on_post=lambda ts: self.store.write(
                "INSERT OR IGNORE INTO ignored_slack_messages VALUES (?)", (ts,)
            ),
        )

    def lifecycle_message_id(
        self, session_id: str, state: str, changed_at: float
    ) -> str:
        return message_id(
            self.channel, session_id, "lifecycle", f"{state}:{changed_at}"
        )

    def ensure_activity_notice(
        self, session: sqlite3.Row, turn_id: str, record: dict[str, Any]
    ) -> None:
        if not self.activity_notices:
            return
        if self.store.one(
            "SELECT 1 FROM activity_notices WHERE session_id=? AND turn_id=?",
            (session["session_id"], turn_id),
        ):
            return
        try:
            completed_at = datetime.fromisoformat(
                str(record["timestamp"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            completed_at = datetime.now(timezone.utc)
        epoch = int(completed_at.timestamp())
        local = completed_at.astimezone()
        fallback = f"{local:%b} {local.day}, {local.year} at {local:%I:%M %p %Z}"
        thread_url = self.store.thread_url(session["thread_ts"])
        workspace = html.escape(session["workspace"][:60])
        tab = html.escape(session["tab"][:80])
        notice = (
            f"Agent replied · *{workspace} / {tab}* · "
            f"Last updated: <!date^{epoch}^{{date_short_pretty}} at {{time}}|{fallback}>"
        )
        if thread_url:
            notice += f" · <{thread_url}|Open thread>"
        slack_ts = self.post_slack(
            notice,
            thread_ts=None,
            client_msg_id=message_id(
                self.channel, session["session_id"], "activity", turn_id
            ),
            markdown=True,
        )
        self.store.write(
            "INSERT INTO activity_notices VALUES (?, ?, ?)",
            (session["session_id"], turn_id, slack_ts),
        )

    @staticmethod
    def root_text(session: sqlite3.Row) -> str:
        integration = integration_for(session["session_id"])
        identity = (
            session["session_id"].split(":", 1)[1]
            if integration in PATH_INTEGRATIONS
            else session["session_id"]
        )
        label = {"codex": "Codex", "omp": "omp", "pi": "Pi"}[integration]
        return (
            f"{label} session: {identity}\n"
            f"Workspace: {session['workspace']}\n"
            f"Tab: {session['tab']}\n"
            f"Pane: {session['pane_id']}\n"
            f"Status: {session['status']}"
        )

    def update_root(self, session: sqlite3.Row, text: str) -> None:
        if self.slack.update(session["thread_ts"], text) is False:
            self.post_slack(
                "Session updated:\n" + text,
                thread_ts=session["thread_ts"],
                client_msg_id=message_id(
                    self.channel, session["session_id"], "root-update", text
                ),
            )
        self.store.write(
            "UPDATE sessions SET root_text=? WHERE session_id=?",
            (text, session["session_id"]),
        )

    def sync_roots(self, agents: list[dict[str, str]]) -> None:
        active_ids = {agent["session_id"] for agent in agents}
        missing_paths = set()
        for session_id in active_ids:
            row = self.store.one(
                "SELECT rollout_path FROM sessions WHERE session_id=?", (session_id,)
            )
            if row is None or row["rollout_path"] is None:
                missing_paths.add(session_id)
        paths = self.rollouts.find_paths(missing_paths)
        for agent in agents:
            sid = agent["session_id"]
            try:
                self.store.discover(
                    agent, paths.get(sid), self.backfill or self.bootstrap_complete
                )
                row = self.store.one(
                    "SELECT * FROM sessions WHERE session_id=?", (sid,)
                )
                root_text = self.root_text(row)
                if row["thread_ts"] is None:
                    ts = self.post_slack(
                        root_text,
                        thread_ts=None,
                        client_msg_id=message_id(self.channel, sid, "root", sid),
                    )
                    self.store.write(
                        """
                        UPDATE sessions SET thread_ts=?, root_text=?, last_seen_slack_ts=?
                        WHERE session_id=?
                    """,
                        (ts, root_text, ts, sid),
                    )
                    LOG.info("Created Slack thread for %s in %s", sid, agent["pane_id"])
                elif row["root_text"] != root_text:
                    self.update_root(row, root_text)
            except Exception:
                LOG.exception("Cannot sync session root %s", sid)
        for row in self.store.all("SELECT * FROM sessions WHERE thread_ts IS NOT NULL"):
            if row["session_id"] not in active_ids:
                managed = self.suspender is not None and self.suspender.managed(
                    row["session_id"]
                )
                if not managed:
                    self.store.write(
                        "UPDATE sessions SET status='ended' WHERE session_id=?",
                        (row["session_id"],),
                    )
                updated = self.store.one(
                    "SELECT * FROM sessions WHERE session_id=?", (row["session_id"],)
                )
                try:
                    text = self.root_text(updated)
                    if text == row["root_text"]:
                        continue
                    self.update_root(updated, text)
                except Exception:
                    LOG.exception("Cannot mark session ended %s", row["session_id"])
        self.bootstrap_complete = True

    def mirror_record(self, session: sqlite3.Row, record: dict[str, Any]) -> None:
        if record.get("type") != "response_item":
            return
        # Codex can store an internal summary as a final answer for the same
        # turn as the real reply. Do not let it finalize the mirrored turn.
        if (record.get("metadata") or {}).get("compaction_output"):
            return
        payload = record.get("payload") or {}
        if payload.get("type") != "message":
            return
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            return
        text = message_text(payload)
        if not text:
            return
        metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
        if role == "user":
            kinds = metadata.get("content_item_kinds") or []
            if not any(
                isinstance(kind, str) and kind.startswith("user.") for kind in kinds
            ):
                return
            item_id = payload.get("id")
            if not item_id or self.store.one(
                "SELECT 1 FROM user_items WHERE item_id=?", (item_id,)
            ):
                return
            created_at = metadata.get("create_time")
            if self.store.match_inbound(session["session_id"], text, created_at):
                self.store.write(
                    "INSERT INTO user_items (item_id, session_id) VALUES (?, ?)",
                    (item_id, session["session_id"]),
                )
                return
            label = (
                "Monitor"
                if self.recovery is not None
                and self.recovery.monitor_input(session["session_id"], payload)
                else "Local user"
            )
            slack_ts = self.post_slack(
                label + ":\n" + text,
                thread_ts=session["thread_ts"],
                client_msg_id=message_id(
                    self.channel, session["session_id"], "user", item_id
                ),
            )
            self.store.write(
                "INSERT INTO user_items VALUES (?, ?, ?)",
                (item_id, session["session_id"], slack_ts),
            )
            return
        phase = payload.get("phase")
        if phase not in {"commentary", "final_answer"}:
            return
        turn_id = (
            metadata.get("mirror_turn_id")
            or metadata.get("turn_id")
            or payload.get("id")
        )
        if not turn_id:
            return
        previous = self.store.one(
            """
            SELECT * FROM assistant_turns WHERE session_id=? AND turn_id=?
        """,
            (session["session_id"], turn_id),
        )
        if previous and previous["phase"] == "final_answer":
            self.ensure_activity_notice(session, turn_id, record)
            return
        prefix = "Agent:\n" if phase == "final_answer" else "Agent (working):\n"
        rendered = limit_text(prefix + text)
        if previous is None:
            slack_ts = self.post_slack(
                rendered,
                thread_ts=session["thread_ts"],
                client_msg_id=message_id(
                    self.channel, session["session_id"], "assistant", turn_id
                ),
            )
            self.store.write(
                """
                INSERT INTO assistant_turns VALUES (?, ?, ?, ?, ?)
            """,
                (session["session_id"], turn_id, slack_ts, rendered, phase),
            )
        elif previous["text"] != rendered or previous["phase"] != phase:
            slack_ts = previous["slack_ts"]
            if (
                len(previous["text"].encode("utf-8")) > SAFE_SLACK_BYTES
                or self.slack.update(slack_ts, rendered) is False
            ):
                self.store.write(
                    "INSERT OR IGNORE INTO ignored_slack_messages VALUES (?)",
                    (slack_ts,),
                )
                slack_ts = self.post_slack(
                    rendered,
                    thread_ts=session["thread_ts"],
                    client_msg_id=message_id(
                        self.channel,
                        session["session_id"],
                        "assistant-continuation",
                        payload.get("id") or turn_id,
                    ),
                )
            self.store.write(
                """
                UPDATE assistant_turns SET slack_ts=?, text=?, phase=?
                WHERE session_id=? AND turn_id=?
            """,
                (slack_ts, rendered, phase, session["session_id"], turn_id),
            )
        if phase == "final_answer":
            self.ensure_activity_notice(session, turn_id, record)

    def sync_rollouts(self, active_ids: set[str], budget_seconds: float = 10) -> None:
        sessions = self.store.all("SELECT * FROM sessions WHERE thread_ts IS NOT NULL")
        started = time.monotonic()
        for step in range(len(sessions)):
            index = (self.rollout_index + step) % len(sessions)
            session = sessions[index]
            path_string = session["rollout_path"]
            if not path_string or not Path(path_string).is_file():
                continue
            path = Path(path_string)
            if (
                session["session_id"] not in active_ids
                and session["status"] == "ended"
                and path.stat().st_size <= session["offset"]
            ):
                continue
            try:
                for offset, record in self.rollouts.records(path, session["offset"]):
                    self.mirror_record(session, record)
                    self.store.write(
                        "UPDATE sessions SET offset=? WHERE session_id=?",
                        (offset, session["session_id"]),
                    )
                    if time.monotonic() - started >= budget_seconds:
                        # Large or growing histories must not block Slack input.
                        self.rollout_index = (index + 1) % len(sessions)
                        return
            except Exception:
                LOG.exception(
                    "Cannot sync session history for %s", session["session_id"]
                )

    def send_pending(self, agents: list[dict[str, str]]) -> None:
        ready = {a["session_id"]: a for a in agents if a["status"] in {"idle", "done"}}
        sent_this_cycle: set[str] = set()
        for incoming in self.store.all(
            "SELECT * FROM inbound WHERE state='pending' ORDER BY created_at, slack_ts"
        ):
            sid = incoming["session_id"]
            if sid not in ready or sid in sent_this_cycle:
                continue
            if self.recovery is not None and self.recovery.switching(sid):
                continue
            inflight = self.store.one(
                """
                SELECT 1 FROM inbound WHERE session_id=? AND state IN ('sending', 'submitted')
                    AND seen_in_rollout=0 LIMIT 1
            """,
                (sid,),
            )
            if inflight:
                continue
            self.store.write(
                "UPDATE inbound SET state='sending', sent_at=? WHERE slack_ts=?",
                (time.time(), incoming["slack_ts"]),
            )
            try:
                self.herdr.prompt(ready[sid]["pane_id"], sid, incoming["text"])
            except HerdrError as error:
                if error.code in {
                    "session_changed",
                    "agent_not_ready",
                    "agent_blocked",
                }:
                    self.store.write(
                        "UPDATE inbound SET state='pending', sent_at=NULL WHERE slack_ts=?",
                        (incoming["slack_ts"],),
                    )
                else:
                    # A socket failure may happen after Herdr accepted the prompt.
                    # Keep 'sending' until a matching Codex user turn proves delivery.
                    LOG.warning("Prompt for %s has uncertain result: %s", sid, error)
                continue
            except Exception:
                LOG.exception("Prompt for %s has uncertain result", sid)
                continue
            self.store.write(
                "UPDATE inbound SET state='submitted' WHERE slack_ts=?",
                (incoming["slack_ts"],),
            )
            sent_this_cycle.add(sid)
            LOG.info("Submitted Slack message %s to %s", incoming["slack_ts"], sid)

    def ack_submitted(self) -> None:
        for row in self.store.all(
            "SELECT slack_ts FROM inbound WHERE state='submitted' AND acked=0"
        ):
            try:
                self.slack.react(row["slack_ts"])
                self.store.write(
                    "UPDATE inbound SET acked=1 WHERE slack_ts=?", (row["slack_ts"],)
                )
            except Exception:
                LOG.exception("Cannot acknowledge Slack message %s", row["slack_ts"])

    def reconcile_one_thread(self, interval: float) -> None:
        if time.monotonic() - self.last_reconcile < interval:
            return
        sessions = self.store.all("""
            SELECT session_id, thread_ts, last_seen_slack_ts FROM sessions
            WHERE thread_ts IS NOT NULL AND status != 'ended' ORDER BY thread_ts
        """)
        if not sessions:
            return
        session = sessions[self.reconcile_index % len(sessions)]
        self.reconcile_index += 1
        self.last_reconcile = time.monotonic()
        try:
            newest = session["last_seen_slack_ts"] or session["thread_ts"]
            for message in self.slack.replies(session["thread_ts"], newest):
                ts = message.get("ts", "")
                if ts and Decimal(ts) > Decimal(newest):
                    newest = ts
                self.accept_slack_event({**message, "channel": self.channel})
            if newest != (session["last_seen_slack_ts"] or session["thread_ts"]):
                self.store.write(
                    "UPDATE sessions SET last_seen_slack_ts=? WHERE session_id=?",
                    (newest, session["session_id"]),
                )
        except Exception:
            LOG.exception("Cannot reconcile Slack thread for %s", session["session_id"])

    def poll_client_messages(self) -> None:
        if self.search is None:
            return
        row = self.store.one("SELECT value FROM settings WHERE key='client_scan_at'")
        started = time.time()
        try:
            for event in self.search.since(float(row["value"])):
                self.accept_slack_event(event)
            self.store.write(
                "UPDATE settings SET value=? WHERE key='client_scan_at'",
                (str(started),),
            )
        except Exception:
            LOG.exception("Cannot search Slack for new thread replies")

    def tick(self, reconcile_interval: float = 5) -> None:
        agents = self.herdr.agents()
        if self.slack is None:
            # Without Slack there is no thread to mirror, no remote reply to
            # deliver, and no idle session to resume from Slack, so only Codex
            # error recovery and the scheduler act.
            if self.recovery is not None:
                self.recovery.tick(agents)
            if self.scheduler is not None:
                self.scheduler.tick()
            return
        self.sync_roots(agents)
        self.sync_rollouts({agent["session_id"] for agent in agents})
        self.poll_client_messages()
        self.reconcile_one_thread(reconcile_interval)
        if self.recovery is not None and self.recovery.tick(agents):
            agents = self.herdr.agents()
        if self.suspender is not None and self.suspender.tick(agents):
            agents = self.herdr.agents()
            self.sync_roots(agents)
        self.send_pending(agents)
        self.ack_submitted()
        if self.scheduler is not None:
            self.scheduler.tick()


def check_local(herdr: HerdrClient, rollouts: CodexRollouts) -> None:
    snapshot = herdr.request("session.snapshot")["snapshot"]
    panes = {
        kind: sum(row.get("agent") == kind for row in snapshot["agents"])
        for kind in INTEGRATIONS
    }
    agents = herdr.agents()
    paths = rollouts.find_paths({agent["session_id"] for agent in agents})
    print(
        json.dumps(
            {
                "codex_panes": panes["codex"],
                "omp_panes": panes["omp"],
                "pi_panes": panes["pi"],
                "identified_sessions": len(agents),
                "unidentified_panes": len(herdr.unidentified_panes),
                "unidentified_pane_ids": sorted(herdr.unidentified_panes),
                "conflicting_sessions": herdr.conflicting_sessions,
                "rollout_files": len(paths),
                "missing_rollouts": sorted(
                    {agent["session_id"] for agent in agents} - paths.keys()
                ),
                "capabilities": {
                    kind: {
                        cap: supports(
                            f"{kind}:/session.jsonl"
                            if kind in PATH_INTEGRATIONS
                            else kind,
                            cap,
                        )
                        for cap in ("history", "prompt", "suspend", "resume", "recover")
                    }
                    for kind in INTEGRATIONS
                },
            },
            indent=2,
        )
    )


def default_state_path() -> Path:
    return Path.home() / ".local/state/shepherdr/bridge.sqlite"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shepherdr", description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check Herdr and native histories without Slack",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Mirror old turns for newly found sessions",
    )
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument(
        "--config",
        type=Path,
        default=config_path(),
        help="Agent config (read at monitor startup)",
    )
    parser.add_argument("--reconcile-seconds", type=float, default=5)
    parser.add_argument(
        "--jobs",
        type=Path,
        default=Path.home() / ".config/shepherdr/jobs.json",
        help="Cron jobs configuration (reloaded on each poll)",
    )
    parser.add_argument(
        "--check-jobs",
        action="store_true",
        help="Validate cron jobs without starting agents",
    )
    parser.add_argument("--no-jobs", action="store_true", help="Disable scheduled jobs")
    parser.add_argument(
        "--job-runs",
        action="store_true",
        help="Show recent scheduled runs without starting agents",
    )
    parser.add_argument(
        "--worktree-root",
        type=Path,
        default=Path.home() / ".local/state/shepherdr/worktrees",
        help="Directory for scheduled job worktrees",
    )
    parser.add_argument(
        "--no-auto-retry",
        action="store_true",
        help="Disable recovery after transient Codex errors",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=3,
        help="Maximum automatic attempts for one interrupted task",
    )
    parser.add_argument(
        "--fallback-model",
        help="Optional Codex model to select when the current model is at capacity",
    )
    parser.add_argument(
        "--suspend-idle-days",
        type=float,
        default=1,
        help="Suspend saved, idle Codex agents after this many days; 0 disables suspension",
    )
    parser.add_argument(
        "--no-activity-notices",
        action="store_true",
        help="Do not post completed-turn notices in the main channel",
    )
    parser.add_argument(
        "--slack-mode",
        choices=("bot", "client", "none"),
        default="bot",
        help="Slack transport; 'none' runs runtime and scheduled jobs without Slack",
    )
    parser.add_argument(
        "--client-credentials",
        type=Path,
        default=Path.home() / ".config/shepherdr/slack-client.json",
    )
    parser.add_argument(
        "--socket", type=Path, default=os.environ.get("HERDR_SOCKET_PATH")
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=default_state_path(),
    )
    parser.add_argument("--channel", default=os.environ.get("SLACK_CHANNEL_ID"))
    parser.add_argument("--user", default=os.environ.get("SLACK_USER_ID"))
    args = parser.parse_args(argv)
    try:
        default_agent = load_config(args.config.expanduser())
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.job_runs:
        rows = []
        if args.state.is_file():
            with closing(
                sqlite3.connect(args.state.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                db.row_factory = sqlite3.Row
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='job_runs'"
                ).fetchone():
                    rows = [
                        dict(row)
                        for row in db.execute(
                            "SELECT run_id,job_id,scheduled_at,state,workspace_id,tab_id,pane_id,session_id,worktree_path,branch,error FROM job_runs ORDER BY scheduled_at DESC LIMIT 30"
                        )
                    ]
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='job_results'"
                    ).fetchone():
                        for row in rows:
                            result = db.execute(
                                "SELECT result_json FROM job_results WHERE run_id=?",
                                (row["run_id"],),
                            ).fetchone()
                            if result:
                                row["result"] = json.loads(result["result_json"])
        print(json.dumps(rows, indent=2))
        return 0
    if args.check_jobs:
        if __package__:
            from .scheduler import load_jobs
        else:
            from shepherdr.scheduler import load_jobs
        try:
            jobs = load_jobs(args.jobs.expanduser(), default_agent)
        except (OSError, ValueError, KeyError) as error:
            parser.error(str(error))
        print(
            json.dumps(
                [
                    {
                        "id": job.id,
                        "enabled": job.enabled,
                        "schedule": job.schedule,
                        "timezone": job.timezone,
                        "repo": job.repo,
                        "worktree": job.worktree,
                        "action": job.action,
                        "agent": job.agent,
                        "discard_changes": job.discard_changes,
                        "next_run": datetime.fromtimestamp(
                            job.next_run(time.time()), ZoneInfo(job.timezone)
                        ).isoformat(),
                    }
                    for job in jobs
                ],
                indent=2,
            )
        )
        return 0
    if os.environ.get("HERDR_ENV") != "1" or not args.socket:
        parser.error("Run inside Herdr with HERDR_ENV=1 and HERDR_SOCKET_PATH set")
    if args.poll_seconds <= 0 or args.reconcile_seconds <= 0:
        parser.error("Polling intervals must be positive")
    if args.suspend_idle_days < 0:
        parser.error("The suspension interval cannot be negative")
    if args.retry_attempts < 1 or args.retry_attempts > 10:
        parser.error("The automatic retry limit must be between 1 and 10")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    herdr = HerdrClient(args.socket)
    rollouts = SessionHistories(CodexRollouts(args.codex_home.expanduser()))
    if args.check:
        check_local(herdr, rollouts)
        return 0
    if args.slack_mode != "none" and not (args.channel and args.user):
        parser.error(
            "Set SLACK_CHANNEL_ID and SLACK_USER_ID, or use --channel and --user"
        )
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state.parent / (args.state.name + ".lock")).open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another bridge is already using this state file")
        store = Store(args.state)
        socket_client = None
        search = None
        slack = None
        if args.slack_mode == "none":
            LOG.info("Slack is disabled; runtime and scheduled jobs only")
        else:
            if args.slack_mode == "client":
                if __package__:
                    from .client_slack import ClientMessageSearch, ClientWebClient
                else:
                    from client_slack import ClientMessageSearch, ClientWebClient
                if not args.client_credentials.is_file():
                    parser.error("Slack client credentials file is missing")
                web = ClientWebClient(args.client_credentials)
                identity = web.auth_test()
                if identity.get("user_id") != args.user:
                    parser.error(
                        "The Slack client credentials belong to a different user"
                    )
                search = ClientMessageSearch(web, args.channel)
            else:
                if not (
                    os.environ.get("SLACK_BOT_TOKEN")
                    and os.environ.get("SLACK_APP_TOKEN")
                ):
                    parser.error("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN for bot mode")
                try:
                    from slack_sdk import WebClient
                    from slack_sdk.http_retry.builtin_handlers import (
                        RateLimitErrorRetryHandler,
                    )
                    from slack_sdk.socket_mode import SocketModeClient
                    from slack_sdk.socket_mode.response import SocketModeResponse
                except ImportError as error:
                    parser.error(
                        f"Install Shepherdr dependencies with uv sync: {error}"
                    )
                web = WebClient(
                    token=os.environ["SLACK_BOT_TOKEN"],
                    retry_handlers=[RateLimitErrorRetryHandler(max_retry_count=3)],
                )
                identity = web.auth_test()
            store.bind(args.channel, args.user, workspace_url=identity.get("url"))
            slack = SlackApi(web, args.channel)
        bridge = Bridge(
            herdr,
            rollouts,
            slack,
            store,
            args.channel,
            args.user,
            args.backfill,
            search,
            activity_notices=not args.no_activity_notices,
        )
        if slack is not None:
            # Suspended sessions resume from a Slack reply, so suspension needs
            # a Slack monitor.
            if __package__:
                from .suspender import Suspender
            else:
                from suspender import Suspender
            bridge.suspender = Suspender(
                bridge,
                idle_seconds=args.suspend_idle_days * 86400
                if args.suspend_idle_days
                else float("inf"),
            )
        if not args.no_auto_retry:
            if __package__:
                from .recovery import Recovery
            else:
                from recovery import Recovery
            bridge.recovery = Recovery(
                bridge,
                max_attempts=args.retry_attempts,
                fallback_model=args.fallback_model,
            )
        if not args.no_jobs:
            if __package__:
                from .scheduler import Scheduler
            else:
                from shepherdr.scheduler import Scheduler
            bridge.scheduler = Scheduler(
                bridge,
                args.jobs.expanduser(),
                args.worktree_root.expanduser(),
                default_agent=default_agent,
            )
        if args.slack_mode == "bot":
            socket_client = SocketModeClient(
                app_token=os.environ["SLACK_APP_TOKEN"], web_client=web
            )

            def on_slack_request(client: Any, request: Any) -> None:
                if request.type != "events_api":
                    return
                try:
                    bridge.accept_slack_event(request.payload.get("event") or {})
                except Exception:
                    LOG.exception("Cannot store Slack event; Slack can retry it")
                    return
                client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=request.envelope_id)
                )

            socket_client.socket_mode_request_listeners.append(on_slack_request)
            socket_client.connect()
        if slack is None:
            LOG.info("Shepherdr started without Slack")
        else:
            LOG.info("Shepherdr started for channel %s", args.channel)
        try:
            while True:
                started = time.monotonic()
                try:
                    bridge.tick(args.reconcile_seconds)
                except Exception:
                    LOG.exception("Bridge poll failed")
                time.sleep(max(0, args.poll_seconds - (time.monotonic() - started)))
        except KeyboardInterrupt:
            LOG.info("Shepherdr stopped")
        finally:
            if bridge.scheduler is not None:
                bridge.scheduler.close()
            if socket_client is not None:
                socket_client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
