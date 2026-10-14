"""Queue an on-demand runtime from any local terminal."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .agents import AgentConfig, load_config
from .bridge import Store, default_state_path
from .scheduler import Job


def initialize_queue(store: Store) -> None:
    store.write("""
        CREATE TABLE IF NOT EXISTS runtime_requests (
            request_id TEXT PRIMARY KEY,
            job_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            run_id TEXT
        )
    """)


def resolve_repo(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path.expanduser()), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return Path(result.stdout.strip()).resolve()


def choose_base(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "origin/main"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    return "origin/main" if result.returncode == 0 else "HEAD"


def runtime_status(store: Store, request_id: str) -> dict[str, Any]:
    request = store.one(
        "SELECT * FROM runtime_requests WHERE request_id=?", (request_id,)
    )
    if request is None:
        raise ValueError("Unknown runtime request")
    job = json.loads(request["job_json"])
    result = {
        "request_id": request_id,
        "state": request["state"],
        "name": Job(**job).display_name,
        "repo": job["repo"],
        "agent": asdict(Job(**job).agent_config()),
    }
    if request["run_id"]:
        run = store.one("SELECT * FROM job_runs WHERE run_id=?", (request["run_id"],))
        if run:
            for key in (
                "run_id",
                "state",
                "workspace_id",
                "tab_id",
                "pane_id",
                "session_id",
                "worktree_path",
                "branch",
                "error",
            ):
                result[key] = run[key]
            stored = store.one(
                "SELECT result_json FROM job_results WHERE run_id=?", (run["run_id"],)
            )
            if stored:
                result["result"] = json.loads(stored["result_json"])
            session = store.one(
                "SELECT thread_ts FROM sessions WHERE session_id=?",
                (run["session_id"],),
            )
            channel = store.one("SELECT value FROM settings WHERE key='channel'")
            if session and session["thread_ts"] and channel:
                url = store.thread_url(session["thread_ts"])
                if url:
                    result["slack_url"] = url
    return result


def queue_runtime(
    store: Store, job: Job, request_id: str | None = None
) -> dict[str, Any]:
    if job.mode != "runtime":
        raise ValueError("Queue only an on-demand runtime with this command")
    if job.agent is None and job.launcher is None:
        current = store.one("SELECT value FROM settings WHERE key='default_agent'")
        selected = (
            AgentConfig.parse(json.loads(current["value"]))
            if current
            else AgentConfig()
        )
        job = replace(job, agent=asdict(selected))
    job.agent_config()
    heartbeat = store.one("SELECT value FROM settings WHERE key='scheduler_scan_at'")
    if heartbeat is None or time.time() - float(heartbeat["value"]) > 120:
        raise ValueError(
            "The Shepherdr scheduler is not running. Start the monitor before requesting a runtime."
        )
    initialize_queue(store)
    request_id = str(uuid.UUID(request_id)) if request_id else str(uuid.uuid4())
    encoded = json.dumps(asdict(job), sort_keys=True)
    with store.lock, store.db:
        previous = store.db.execute(
            "SELECT job_json FROM runtime_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if previous and previous["job_json"] != encoded:
            raise ValueError("This request ID already belongs to a different task")
        store.db.execute(
            "INSERT OR IGNORE INTO runtime_requests (request_id,job_json,created_at) VALUES (?,?,?)",
            (request_id, encoded, time.time()),
        )
    return runtime_status(store, request_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shepherdr runtime", description=__doc__)
    parser.add_argument("--name", help="Short task name for the tab and worktree")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--base", help="Local Git reference; defaults to origin/main or HEAD"
    )
    parser.add_argument(
        "--launcher", choices=("codex",), help="Codex launcher override"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Override the monitor's agent config for this request",
    )
    parser.add_argument("--no-worktree", action="store_true")
    parser.add_argument("--agent-arg", action="append", default=[])
    prompts = parser.add_mutually_exclusive_group()
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file", type=Path)
    parser.add_argument(
        "--request-id",
        help="Reuse an ID to retry a queue request without a duplicate runtime",
    )
    parser.add_argument(
        "--status", metavar="REQUEST_ID", help="Read a queued runtime's status"
    )
    parser.add_argument("--state", type=Path, default=default_state_path())
    args = parser.parse_args(argv)
    if not args.state.is_file():
        parser.error("The monitor state file does not exist")
    store = Store(args.state)
    try:
        if args.status:
            initialize_queue(store)
            result = runtime_status(store, str(uuid.UUID(args.status)))
        else:
            prompt = args.prompt_file.read_text() if args.prompt_file else args.prompt
            if not prompt or not prompt.strip():
                parser.error("Supply --prompt or --prompt-file")
            name = args.name or "-".join(prompt.split()[:6])
            slug = re.sub(
                r"[^a-z0-9]+", "-", name.lower().replace("'", "").replace("’", "")
            ).strip("-")[:32]
            if not slug:
                parser.error("Supply a task name that contains letters or digits")
            repo = resolve_repo(args.repo)
            base = args.base or choose_base(repo)
            if not base.strip() or base.startswith("-"):
                parser.error("Use a valid Git reference for --base")
            job = Job(
                id="runtime-" + slug,
                schedule="* * * * *",
                timezone="UTC",
                repo=str(repo),
                prompt=prompt,
                enabled=True,
                worktree=not args.no_worktree,
                base=base,
                agent_args=tuple(args.agent_arg),
                max_runs=1,
                launcher=args.launcher,
                agent=asdict(load_config(args.config.expanduser()))
                if args.config
                else None,
                mode="runtime",
            )
            result = queue_runtime(store, job, args.request_id)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        parser.error(str(error))
    finally:
        store.db.close()
    return 0
