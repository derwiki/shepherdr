"""Agent commands and native session integrations."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
# Agent labels Shepherdr routes. Codex reports a session UUID; the path
# integrations report an absolute session file.
INTEGRATIONS = ("codex", "omp", "pi")
PATH_INTEGRATIONS = ("omp", "pi")


@dataclass(frozen=True)
class AgentConfig:
    integration: str = "codex"
    command: tuple[str, ...] = ("codex",)

    @classmethod
    def parse(cls, value: Any) -> AgentConfig:
        if not isinstance(value, dict) or set(value) != {"integration", "command"}:
            raise ValueError("agent must contain integration and command")
        if (
            not isinstance(value["integration"], str)
            or value["integration"] not in INTEGRATIONS
        ):
            raise ValueError("Supported agent integrations are codex, omp, and pi")
        command = value["command"]
        if (
            not isinstance(command, (list, tuple))
            or not command
            or not all(isinstance(arg, str) and "\0" not in arg for arg in command)
            or not command[0].strip()
        ):
            raise ValueError("agent command must be a nonempty string array")
        return cls(value["integration"], tuple(command))

    def launch(self, extra_args: tuple[str, ...] = ()) -> str:
        command = [str(Path(self.command[0]).expanduser()), *self.command[1:]]
        args = list(extra_args)
        if self.integration == "codex" and "--no-daemon" not in command + args:
            # Codex hooks must use this terminal's environment.
            args.insert(0, "--no-daemon")
        return shlex.join([*command, *args])


DEFAULT_AGENT = AgentConfig()


def config_path() -> Path:
    return Path.home() / ".config/shepherdr/config.json"


def load_config(path: Path) -> AgentConfig:
    if not path.is_file():
        return AgentConfig()
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) - {"agent"}:
        raise ValueError("The config file supports only the agent field")
    return AgentConfig.parse(value["agent"]) if "agent" in value else AgentConfig()


def session_key(agent: dict[str, Any]) -> str | None:
    """Use only Herdr's native identity, namespaced per path-based agent."""
    identity = agent.get("agent_session") or {}
    kind = agent.get("agent")
    value = identity.get("value")
    if identity.get("agent") != kind or not isinstance(value, str):
        return None
    if kind == "codex" and identity.get("kind") == "id" and UUID.fullmatch(value):
        return value.lower()
    if (
        kind in PATH_INTEGRATIONS
        and identity.get("kind") == "path"
        and value.startswith("/")
        and "\0" not in value
        and Path(value).suffix == ".jsonl"
    ):
        # Pi and omp can report this path before the first turn creates the file.
        return kind + ":" + value
    return None


def integration_for(session_id: str) -> str:
    prefix = session_id.partition(":")[0]
    return prefix if prefix in PATH_INTEGRATIONS else "codex"


def supports(session_id: str, capability: str) -> bool:
    capabilities = {
        "codex": {"history", "prompt", "identify", "suspend", "resume", "recover"},
        "omp": {"history", "prompt"},
        "pi": {"history", "prompt"},
    }
    return capability in capabilities[integration_for(session_id)]


# Characters that end a shell prompt in common themes. A trailing glyph means
# nothing is typed after the prompt; themes such as robbyrussell put the glyph
# first instead, so a leading glyph marks the line as a prompt too. The caller
# clears any typed text before it sends input.
PROMPT_GLYPHS = "$%#>❯❱➜➤→λ»"


def shell_prompt_line(text: str) -> str | None:
    """Return the pane's last line when it sits at a shell prompt."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if last[-1] in PROMPT_GLYPHS or last.lstrip()[:1] in PROMPT_GLYPHS:
        return last
    return None


def empty_composer(integration: str, text: str) -> bool:
    if integration == "codex":
        from .suspender import empty_composer as codex_empty

        return codex_empty(text)
    if integration == "omp":
        # omp draws the draft left-aligned after the composer's bottom corner
        # and pushes a hint to the right edge, separated by a long run of
        # spaces. A dialog, a wrapped draft, or another editor does not end in
        # that corner, so it is never reported empty.
        lines = text.splitlines()
        if not lines or not lines[-1].startswith("╰─"):
            return False
        return not re.split(r" {10,}", lines[-1][2:])[0].strip()
    # The standard Pi editor has two plain horizontal borders. Accept only
    # an empty editor near the bottom; custom editors and menus stay untouched.
    lines = text.splitlines()
    borders = [i for i, line in enumerate(lines) if re.fullmatch(r"─{5,}\s*", line)]
    if len(borders) < 2:
        return False
    top, bottom = borders[-2:]
    return (
        bottom == top + 2
        and not lines[top + 1].strip()
        and lines[top].rstrip() == lines[bottom].rstrip()
        and len(lines) - bottom <= 4
    )


@dataclass
class PiIndex:
    offset: int = 0
    # Each entry carries its user turn, assistant reply, and reply end state.
    entries: dict[str, tuple[str | None, str | None, bool]] = field(
        default_factory=dict
    )
    children: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=64)
def pi_index(path: Path, device: int, inode: int, session_id: str) -> PiIndex:
    return PiIndex()


class SessionHistories:
    """Read native histories and supply the bridge's message record format."""

    def __init__(self, codex: Any):
        self.codex = codex
        # The existing Codex lifecycle implementations use this catalog path.
        self.home = codex.home

    def find_paths(self, session_ids: set[str]) -> dict[str, Path]:
        found = self.codex.find_paths(
            {sid for sid in session_ids if integration_for(sid) == "codex"}
        )
        for sid in session_ids:
            if integration_for(sid) in PATH_INTEGRATIONS:
                path = Path(sid.split(":", 1)[1])
                if path.is_file():
                    found[sid] = path
        return found

    @staticmethod
    def session_header(path: Path) -> dict[str, Any] | None:
        """Return a path-based session header, skipping records before it."""
        with path.open("rb") as stream:
            for _ in range(4):
                line = stream.readline(64 * 1024)
                if not line or not line.endswith(b"\n"):
                    # A Codex rollout, a partial record, or a huge first line.
                    return None
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, ValueError):
                    return None
                if isinstance(record, dict) and record.get("type") == "session":
                    return record
        return None

    @staticmethod
    def records(path: Path, offset: int) -> Iterator[tuple[int, dict[str, Any]]]:
        from .bridge import CodexRollouts

        header = SessionHistories.session_header(path)
        if header is None:
            yield from CodexRollouts.records(path, offset)
            return
        if header.get("version") != 3 or not UUID.fullmatch(header.get("id", "")):
            raise ValueError("Unsupported session header")
        stat = path.stat()
        if stat.st_size < offset:
            offset = 0
        if stat.st_size == offset:
            return
        index = pi_index(path.resolve(), stat.st_dev, stat.st_ino, header["id"])
        if stat.st_size < index.offset:
            index.entries.clear()
            index.children.clear()
            index.offset = 0
        # One namespace for every path-based history: the scheduler and the
        # bridge must derive the same turn IDs from the same file.
        prefix = "pi:" + header["id"] + ":"
        rebuilding = 0
        for end, record in CodexRollouts.records(path, min(offset, index.offset)):
            entry_id = record.get("id")
            turn, reply, closed = index.entries.get(
                record.get("parentId"), (None, None, False)
            )
            parent = record.get("parentId")
            if isinstance(parent, str) and isinstance(entry_id, str):
                child = index.children.setdefault(parent, entry_id)
                if child != entry_id:
                    # A sibling starts a different path through the Pi tree.
                    reply, closed = None, False
            message = record.get("message") or {}
            role = message.get("role") if record.get("type") == "message" else None
            content = message.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            final = (
                role == "assistant"
                and message.get("stopReason") == "stop"
                and not any(part.get("type") == "toolCall" for part in content)
            )
            if role == "user" and isinstance(entry_id, str):
                turn, reply, closed = prefix + entry_id, None, False
            elif role == "assistant" and isinstance(entry_id, str):
                if reply is None or closed:
                    reply = prefix + entry_id
                closed = final
            if isinstance(entry_id, str):
                index.entries[entry_id] = (turn, reply, closed)
            index.offset = max(index.offset, end)
            if end <= offset:
                rebuilding += 1
                if rebuilding % 1000 == 0:
                    # A restart can need old parent links. Give the bridge a
                    # budget boundary without changing its saved read position.
                    yield offset, {}
                continue
            normalized: dict[str, Any] = {}
            if role in {"user", "assistant"} and turn and isinstance(entry_id, str):
                normalized = {
                    "type": "response_item",
                    "timestamp": record.get("timestamp"),
                    "payload": {
                        "type": "message",
                        "id": prefix + entry_id,
                        "role": role,
                        "phase": "final_answer" if final else "commentary",
                        "content": [
                            {
                                "type": "input_text"
                                if role == "user"
                                else "output_text",
                                "text": part["text"],
                            }
                            for part in content
                            if part.get("type") == "text"
                            and isinstance(part.get("text"), str)
                        ],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": turn,
                            "mirror_turn_id": reply,
                            "create_time": message["timestamp"] / 1000
                            if isinstance(message.get("timestamp"), (int, float))
                            else None,
                            "content_item_kinds": ["user.text"]
                            if role == "user"
                            else [],
                        },
                    },
                }
            yield end, normalized
