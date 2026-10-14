"""Start scheduled agents in new Herdr workspaces and optional worktrees."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from .agents import (
    DEFAULT_AGENT,
    AgentConfig,
    SessionHistories,
    empty_composer,
    session_key,
    shell_prompt_line,
)
from .bridge import SESSION_ID, HerdrError, message_text

LOG = logging.getLogger("shepherdr")
JOB_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
TERMINAL_STATES = ("done", "failed", "skipped_overlap", "skipped_refresh")


@dataclass(frozen=True)
class Job:
    id: str
    schedule: str
    timezone: str
    repo: str
    prompt: str = ""
    enabled: bool = False
    worktree: bool = True
    base: str = "HEAD"
    agent_args: tuple[str, ...] = ()
    max_runs: int | None = None
    launcher: str | None = None
    mode: str = "scheduled"
    action: str = "agent"
    discard_changes: bool = False
    agent: dict[str, Any] | None = None

    def agent_config(self, default: AgentConfig = DEFAULT_AGENT) -> AgentConfig:
        if self.agent is not None and self.launcher is not None:
            raise ValueError("Use agent or the legacy launcher, not both")
        if self.agent is not None:
            return AgentConfig.parse(self.agent)
        return AgentConfig("codex", (self.launcher,)) if self.launcher else default

    @property
    def display_name(self) -> str:
        return self.id.removeprefix("runtime-") if self.mode == "runtime" else self.id

    def next_run(self, now: float) -> float:
        start = datetime.fromtimestamp(now, ZoneInfo(self.timezone))
        return (
            croniter(self.schedule, start, max_years_between_matches=5)
            .get_next(datetime)
            .timestamp()
        )


def load_jobs(path: Path, default_agent: AgentConfig = DEFAULT_AGENT) -> list[Job]:
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or set(data) != {"jobs"}
        or not isinstance(data["jobs"], list)
    ):
        raise ValueError("The jobs file must contain a jobs array")
    jobs = []
    ids = set()
    fields = set(Job.__dataclass_fields__) - {"mode"}
    for index, item in enumerate(data["jobs"]):
        if not isinstance(item, dict) or set(item) - fields:
            raise ValueError(f"Job {index}: unknown fields")
        values = dict(item)
        action = values.get("action", "agent")
        if action not in {"agent", "refresh_checkout"}:
            raise ValueError(f"Job {index}: unknown action")
        required = (
            ("id", "schedule", "timezone", "repo", "prompt")
            if action == "agent"
            else ("id", "schedule", "timezone", "repo")
        )
        for field in required:
            if not isinstance(values.get(field), str) or not values[field].strip():
                raise ValueError(f"Job {index}: {field} must be a nonempty string")
        if not JOB_ID.fullmatch(values["id"]) or values["id"] in ids:
            raise ValueError(
                f"Job {index}: use a unique ID with lowercase letters, digits, and hyphens"
            )
        for field in ("enabled", "worktree", "discard_changes"):
            if field in values and not isinstance(values[field], bool):
                raise ValueError(f"Job {index}: {field} must be true or false")
        if action == "refresh_checkout":
            if values.get("worktree") is True:
                raise ValueError(
                    f"Job {index}: checkout refresh must use the primary checkout"
                )
            values["worktree"] = False
        if "max_runs" in values and (
            type(values["max_runs"]) is not int or values["max_runs"] < 1
        ):
            raise ValueError(f"Job {index}: max_runs must be a positive integer")
        if values.get("launcher") not in {None, "codex"}:
            raise ValueError(f"Job {index}: launcher must be codex")
        args = values.get("agent_args", [])
        if not isinstance(args, list) or not all(
            isinstance(arg, str) and "\0" not in arg for arg in args
        ):
            raise ValueError(f"Job {index}: agent_args must be a string array")
        if "base" in values and (
            not isinstance(values["base"], str) or not values["base"].strip()
        ):
            raise ValueError(f"Job {index}: base must be a nonempty Git reference")
        schedule = values["schedule"]
        if (
            len(schedule.split()) != 5
            or re.search(r"\b[HR]\b", schedule)
            or not croniter.is_valid(schedule)
        ):
            raise ValueError(f"Job {index}: use a valid five-field cron schedule")
        ZoneInfo(values["timezone"])
        repo = Path(values["repo"]).expanduser()
        if not repo.is_absolute():
            repo = path.parent / repo
        values["repo"] = str(repo.resolve())
        values["agent_args"] = tuple(args)
        job = Job(**values)
        selected = job.agent_config(default_agent)
        if job.launcher is None:
            values["agent"] = asdict(selected)
            job = Job(**values)
        job.next_run(time.time())
        if job.enabled and not repo.is_dir():
            raise ValueError(f"Job {index}: repository directory does not exist")
        jobs.append(job)
        ids.add(job.id)
    return jobs


class Scheduler:
    def __init__(
        self,
        bridge: Any,
        config: Path,
        worktree_root: Path,
        executor: Executor | None = None,
        default_agent: AgentConfig = DEFAULT_AGENT,
    ):
        self.bridge = bridge
        self.store = bridge.store
        self.herdr = bridge.herdr
        self.config = config
        self.worktree_root = worktree_root.expanduser().resolve()
        self.config_error: str | None = None
        self.default_agent = default_agent
        self.store.write(
            "INSERT OR REPLACE INTO settings VALUES ('default_agent',?)",
            (json.dumps(asdict(default_agent)),),
        )
        self.executor = (
            executor
            if executor is not None
            else ThreadPoolExecutor(max_workers=1, thread_name_prefix="shepherdr-jobs")
        )
        self.steps: dict[str, Future] = {}
        from .runtime import initialize_queue

        initialize_queue(self.store)
        with self.store.lock, self.store.db:
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS job_schedules (
                    job_id TEXT PRIMARY KEY,
                    schedule TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    next_due REAL
                );
                CREATE TABLE IF NOT EXISTS job_runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    scheduled_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    startup_at REAL,
                    workspace_id TEXT,
                    tab_id TEXT,
                    pane_id TEXT,
                    terminal_id TEXT,
                    session_id TEXT,
                    worktree_path TEXT,
                    branch TEXT,
                    prompt TEXT,
                    rollout_path TEXT,
                    rollout_offset INTEGER NOT NULL DEFAULT 0,
                    turn_id TEXT,
                    error TEXT,
                    UNIQUE(job_id, scheduled_at)
                );
                CREATE TABLE IF NOT EXISTS job_notices (
                    run_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    slack_ts TEXT NOT NULL,
                    PRIMARY KEY(run_id, state)
                );
                CREATE TABLE IF NOT EXISTS job_results (
                    run_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL
                );
            """)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)

    def accept_requests(self) -> None:
        with self.store.lock, self.store.db:
            for request in self.store.db.execute(
                "SELECT * FROM runtime_requests WHERE state='queued' ORDER BY created_at LIMIT 20"
            ).fetchall():
                job = Job(**json.loads(request["job_json"]))
                self.store.db.execute(
                    "INSERT OR IGNORE INTO job_runs (run_id,job_id,scheduled_at,state,job_json,created_at,updated_at) VALUES (?,?,?,'planned',?,?,?)",
                    (
                        request["request_id"],
                        job.id,
                        request["created_at"],
                        request["job_json"],
                        request["created_at"],
                        time.time(),
                    ),
                )
                self.store.db.execute(
                    "UPDATE runtime_requests SET state='accepted',run_id=? WHERE request_id=?",
                    (request["request_id"], request["request_id"]),
                )

    def notify(self) -> None:
        import html

        from .bridge import message_id

        for run in self.store.all(
            "SELECT * FROM job_runs WHERE state IN ('submitted','done','failed','uncertain','prompt_uncertain')"
        ):
            # Older scheduler-only installations did not post launch notices.
            job = Job(**json.loads(run["job_json"]))
            if job.mode != "runtime":
                continue
            state = "started" if run["state"] in {"submitted", "done"} else run["state"]
            if self.bridge.slack is None:
                # Without Slack the monitor logs every transition, so the log
                # records the real state instead of the single launch notice.
                if self.store.one(
                    "SELECT 1 FROM job_notices WHERE run_id=? AND state=?",
                    (run["run_id"], run["state"]),
                ):
                    continue
                LOG.info("Runtime %s: %s", job.display_name, run["state"])
                self.store.write(
                    "INSERT OR IGNORE INTO job_notices VALUES (?,?,?)",
                    (run["run_id"], run["state"], ""),
                )
                continue
            if self.store.one(
                "SELECT 1 FROM job_notices WHERE run_id=? AND state=?",
                (run["run_id"], state),
            ):
                continue
            session = self.store.one(
                "SELECT thread_ts,pane_id FROM sessions WHERE session_id=?",
                (run["session_id"],),
            )
            link = ""
            if state == "started":
                if (
                    not session
                    or not session["thread_ts"]
                    or session["pane_id"] != run["pane_id"]
                ):
                    continue
                url = self.store.thread_url(session["thread_ts"])
                if url:
                    link = f" · <{url}|Open thread>"
            text = (
                f"Shepherdr runtime {state} · *{html.escape(job.display_name)}*{link}"
            )
            ts = self.bridge.post_slack(
                text,
                thread_ts=None,
                client_msg_id=message_id(
                    self.bridge.channel,
                    run["session_id"] or run["run_id"],
                    "runtime-notice",
                    run["run_id"] + ":" + state,
                ),
                markdown=True,
            )
            self.store.write(
                "INSERT OR IGNORE INTO job_notices VALUES (?,?,?)",
                (run["run_id"], state, ts),
            )

    def update(self, run_id: str, **values: Any) -> None:
        values["updated_at"] = time.time()
        assignments = ",".join(f"{key}=?" for key in values)
        self.store.write(
            f"UPDATE job_runs SET {assignments} WHERE run_id=?",
            (*values.values(), run_id),
        )

    def schedule(self, jobs: list[Job], now: float) -> None:
        for job in jobs:
            row = self.store.one(
                "SELECT * FROM job_schedules WHERE job_id=?", (job.id,)
            )
            if row is None or (
                row["schedule"],
                row["timezone"],
                bool(row["enabled"]),
            ) != (job.schedule, job.timezone, job.enabled):
                self.store.write(
                    "INSERT OR REPLACE INTO job_schedules VALUES (?, ?, ?, ?, ?)",
                    (
                        job.id,
                        job.schedule,
                        job.timezone,
                        job.enabled,
                        job.next_run(now) if job.enabled else None,
                    ),
                )
                continue
            if not job.enabled or row["next_due"] is None or row["next_due"] > now:
                continue
            count = self.store.one(
                "SELECT count(*) AS n FROM job_runs WHERE job_id=? AND state!='skipped_overlap'",
                (job.id,),
            )["n"]
            if job.max_runs is not None and count >= job.max_runs:
                continue
            # After downtime, run only the latest due occurrence, not a backlog.
            local = datetime.fromtimestamp(now + 0.001, ZoneInfo(job.timezone))
            due = (
                croniter(job.schedule, local, max_years_between_matches=5)
                .get_prev(datetime)
                .timestamp()
            )
            run_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"shepherdr-job:{job.id}:{due}")
            )
            marks = ",".join("?" for _ in TERMINAL_STATES)
            active = self.store.one(
                f"SELECT 1 FROM job_runs WHERE job_id=? AND state NOT IN ({marks}) LIMIT 1",
                (job.id, *TERMINAL_STATES),
            )
            state = "skipped_overlap" if active else "planned"
            with self.store.lock, self.store.db:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO job_runs (run_id,job_id,scheduled_at,state,job_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (run_id, job.id, due, state, json.dumps(asdict(job)), now, now),
                )
                self.store.db.execute(
                    "UPDATE job_schedules SET next_due=? WHERE job_id=?",
                    (job.next_run(now), job.id),
                )
            LOG.info("Scheduled job %s: %s", job.id, state)
        for row in self.store.all("SELECT job_id FROM job_schedules WHERE enabled=1"):
            if row["job_id"] not in {job.id for job in jobs}:
                self.store.write(
                    "UPDATE job_schedules SET enabled=0,next_due=NULL WHERE job_id=?",
                    (row["job_id"],),
                )

    def observe(self, row: Any) -> None:
        sid = row["session_id"]
        if not sid:
            return
        path = (
            Path(row["rollout_path"])
            if row["rollout_path"]
            else self.bridge.rollouts.find_paths({sid}).get(sid)
        )
        if path is None or not path.is_file():
            return
        turn_id = row["turn_id"]
        state = row["state"]
        offset = row["rollout_offset"]
        answer = None
        for end, record in SessionHistories.records(path, offset):
            offset = end
            if record.get("type") != "response_item" or (
                record.get("metadata") or {}
            ).get("compaction_output"):
                continue
            payload = record.get("payload") or {}
            if payload.get("type") != "message":
                continue
            metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
            if (
                payload.get("role") == "user"
                and (message_text(payload) == row["prompt"].strip() or turn_id)
                and any(
                    isinstance(kind, str) and kind.startswith("user.")
                    for kind in metadata.get("content_item_kinds", [])
                )
            ):
                # A monitor retry or Slack follow-up can continue this job in
                # a new turn. Track it only after the job's own input is seen.
                turn_id = metadata.get("turn_id") or turn_id
                state = "submitted"
            if (
                turn_id
                and payload.get("role") == "assistant"
                and payload.get("phase") == "final_answer"
                and metadata.get("turn_id") == turn_id
            ):
                state = "done"
                answer = message_text(payload)
                break
        if answer:
            self.store.write(
                "INSERT OR REPLACE INTO job_results VALUES (?,?)",
                (row["run_id"], json.dumps({"answer": answer})),
            )
        self.update(
            row["run_id"],
            state=state,
            turn_id=turn_id,
            rollout_path=str(path),
            rollout_offset=offset,
        )

    def advance(self, row: Any, now: float) -> None:
        run_id = row["run_id"]
        job = Job(**json.loads(row["job_json"]))
        state = row["state"]
        selected = job.agent_config()
        if job.action == "refresh_checkout":
            from .checkout import refresh_checkout

            if state != "planned":
                raise ValueError(
                    "A checkout refresh has an uncertain result; inspect Git before retrying"
                )
            self.update(run_id, state="refreshing_checkout")
            result = refresh_checkout(
                Path(job.repo), discard_changes=job.discard_changes, herdr=self.herdr
            )
            self.store.write(
                "INSERT OR REPLACE INTO job_results VALUES (?,?)",
                (run_id, json.dumps(result)),
            )
            self.update(run_id, state=result["state"], error=result.get("reason"))
            LOG.info("Checkout refresh %s: %s", job.id, result)
            return
        if state == "planned":
            stamp = datetime.fromtimestamp(row["scheduled_at"], timezone.utc).strftime(
                "%Y%m%dT%H%MZ"
            )
            stamp += "-" + run_id[:8]
            label = job.display_name
            self.update(run_id, state="creating")
            if job.worktree:
                path = self.worktree_root / job.id / stamp
                path.parent.mkdir(parents=True, exist_ok=True)
                branch = f"shepherdr/{job.id}/{stamp}"
                self.update(run_id, worktree_path=str(path), branch=branch)
                result = self.herdr.request(
                    "worktree.create",
                    {
                        "cwd": job.repo,
                        "base": job.base,
                        "branch": branch,
                        "path": str(path),
                        "label": label,
                        "focus": False,
                    },
                    timeout=60,
                )
            else:
                result = self.herdr.request(
                    "workspace.create",
                    {"cwd": job.repo, "label": label, "focus": False},
                )
            pane = result["root_pane"]
            self.update(
                run_id,
                state="environment_ready",
                workspace_id=result["workspace"]["workspace_id"],
                tab_id=result["tab"]["tab_id"],
                pane_id=pane["pane_id"],
                terminal_id=pane["terminal_id"],
            )
            return
        if state == "creating":
            self.update(
                run_id,
                state="uncertain",
                error="Workspace creation has an uncertain result. Inspect Herdr before starting another run.",
            )
            return
        if state == "environment_ready":
            pane = self.herdr.request("pane.get", {"pane_id": row["pane_id"]})["pane"]
            process = self.herdr.request(
                "pane.process_info", {"pane_id": row["pane_id"]}
            )["process_info"]
            screen = self.herdr.request(
                "pane.read",
                {"pane_id": row["pane_id"], "source": "detection", "lines": 4},
            )["read"]["text"]
            if pane["terminal_id"] != row["terminal_id"] or pane.get("agent"):
                raise ValueError("The job pane is no longer its original shell")
            if (
                not isinstance(process.get("shell_pid"), int)
                or process["shell_pid"] <= 0
                or process.get("foreground_process_group_id")
                != process.get("shell_pid")
                or shell_prompt_line(screen) is None
            ):
                if now - row["created_at"] > 120:
                    raise ValueError("The job shell is not ready or contains a draft")
                return
            self.herdr.request(
                "tab.rename", {"tab_id": row["tab_id"], "label": job.display_name}
            )
            self.update(run_id, state="starting_agent", startup_at=now)
            command = selected.launch(job.agent_args)
            # A leading-glyph prompt theme cannot prove the line is empty, so
            # clear any typed text before the launch command is sent.
            self.herdr.request(
                "pane.send_keys", {"pane_id": row["pane_id"], "keys": ["ctrl+u"]}
            )
            self.herdr.request(
                "pane.send_input",
                {"pane_id": row["pane_id"], "text": command, "keys": ["enter"]},
            )
            return
        if state == "starting_agent":
            agent = self.herdr.request("agent.get", {"target": row["pane_id"]})["agent"]
            if agent.get("terminal_id") != row["terminal_id"]:
                raise ValueError("The job pane now has a different terminal")
            if agent.get("name") not in {None, f"job-{run_id[:8]}"}:
                raise ValueError("The job pane now has a different agent")
            identity = agent.get("agent_session") or {}
            sid = session_key(agent)
            if sid is not None:
                if row["session_id"] and row["session_id"] != sid:
                    raise ValueError("The job agent now has a different session")
                if not row["session_id"]:
                    self.update(run_id, session_id=sid)
            ready = agent.get("agent") == selected.integration and agent.get(
                "agent_status"
            ) in {
                "idle",
                "done",
            }
            if ready and not identity and selected.integration == "codex":
                screen = self.herdr.request(
                    "pane.read",
                    {"pane_id": row["pane_id"], "source": "detection", "lines": 4},
                )["read"]["text"]
                if empty_composer("codex", screen):
                    # Older Codex integrations do not report a new chat ID.
                    # Query only our new, empty agent; /status is not a turn.
                    self.update(run_id, state="identifying_agent")
                    self.herdr.request(
                        "agent.prompt", {"target": row["pane_id"], "text": "/status"}
                    )
                    return
            if (
                agent.get("agent") != selected.integration
                or agent.get("agent_status") not in {"idle", "done"}
                or sid is None
            ):
                if now - row["startup_at"] > 120:
                    raise ValueError(
                        f"The {selected.integration} agent did not become ready with a native session identity within two minutes. Check its Herdr integration."
                    )
                return
            if row["session_id"] and sid != row["session_id"]:
                raise ValueError("The job agent now has a different session")
            if agent.get("name") is None:
                self.herdr.request(
                    "agent.rename",
                    {"target": row["pane_id"], "name": f"job-{run_id[:8]}"},
                )
            screen = self.herdr.request(
                "pane.read",
                {"pane_id": row["pane_id"], "source": "detection", "lines": 30},
            )["read"]["text"]
            if not empty_composer(selected.integration, screen):
                if now - row["startup_at"] > 120:
                    raise ValueError(
                        "The job composer contains a draft or an unknown prompt"
                    )
                return
            label = (
                "Shepherdr runtime"
                if job.mode == "runtime"
                else "Scheduled Shepherdr job"
            )
            prompt = f"{label}: {job.display_name}\nTask:\n{job.prompt}"
            self.update(run_id, state="submitting", session_id=sid, prompt=prompt)
            try:
                self.herdr.prompt(row["pane_id"], sid, prompt)
            except HerdrError as error:
                if error.code in {"agent_not_ready", "agent_blocked"}:
                    self.update(run_id, state="starting_agent")
                    return
                raise
            self.update(run_id, state="submitted")
            LOG.info(
                "Started scheduled job %s in %s (session %s)",
                job.id,
                row["pane_id"],
                sid,
            )
            return
        if state == "identifying_agent":
            if selected.integration != "codex":
                raise ValueError(
                    "This integration cannot use Codex session identification"
                )
            agent = self.herdr.request("agent.get", {"target": row["pane_id"]})["agent"]
            if (
                agent.get("terminal_id") != row["terminal_id"]
                or agent.get("agent") != "codex"
                or agent.get("agent_status") not in {"idle", "done"}
            ):
                raise ValueError("The job agent changed during session identification")
            screen = self.herdr.request(
                "pane.read",
                {"pane_id": row["pane_id"], "source": "visible", "lines": 80},
            )["read"]["text"]
            match = re.search(
                r"^\s*Session:\s+([0-9a-f-]{36})\s*$", screen, re.MULTILINE
            )
            if not match or not empty_composer("codex", screen):
                if now - row["startup_at"] > 120:
                    raise ValueError("Codex did not report its session in /status")
                return
            sid = match[1].lower()
            current = self.herdr.request("agent.get", {"target": row["pane_id"]})[
                "agent"
            ]
            # Require the native title to agree with the fresh status output.
            title = (
                current.get("terminal_title_stripped")
                or current.get("terminal_title")
                or ""
            )
            if (
                current.get("terminal_id") != row["terminal_id"]
                or current.get("agent") != "codex"
                or current.get("agent_status") not in {"idle", "done"}
                or current.get("name") not in {None, f"job-{run_id[:8]}"}
                or not title.startswith(sid)
                or not SESSION_ID.fullmatch(sid)
            ):
                raise ValueError(
                    "The status session does not match the current job agent"
                )
            if row["session_id"] and row["session_id"] != sid:
                raise ValueError("The job session changed during identification")
            identity = current.get("agent_session") or {}
            if identity and identity.get("value", "").lower() != sid:
                raise ValueError("Herdr reports a different current job session")
            self.herdr.request(
                "pane.report_agent_session",
                {
                    "pane_id": row["pane_id"],
                    "source": "herdr:codex",
                    "agent": "codex",
                    "agent_session_id": sid,
                    "session_start_source": "startup",
                    "seq": time.time_ns(),
                },
            )
            self.update(run_id, state="starting_agent", session_id=sid)
            return
        if state in {"submitting", "submitted", "prompt_uncertain"}:
            if state == "submitting":
                self.update(
                    run_id,
                    state="prompt_uncertain",
                    error="Prompt delivery has an uncertain result; it will not be sent again.",
                )
            self.observe(
                self.store.one("SELECT * FROM job_runs WHERE run_id=?", (run_id,))
            )

    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.store.write(
            "INSERT OR REPLACE INTO settings VALUES ('scheduler_scan_at',?)",
            (str(time.time()),),
        )
        self.accept_requests()
        try:
            jobs = (
                load_jobs(self.config, self.default_agent)
                if self.config.is_file()
                else []
            )
        except (OSError, ValueError, KeyError) as error:
            if str(error) != self.config_error:
                LOG.error("Cannot load scheduled jobs: %s", error)
                self.config_error = str(error)
        else:
            self.config_error = None
            self.schedule(jobs, now)
        for run_id, future in list(self.steps.items()):
            if future.done():
                future.result()
                del self.steps[run_id]
        marks = ",".join("?" for _ in TERMINAL_STATES)
        rows = self.store.all(
            f"SELECT * FROM job_runs WHERE state NOT IN ({marks}) ORDER BY created_at",
            TERMINAL_STATES,
        )
        # Provisioning runs in a worker so a large checkout cannot block Slack.
        launched = False
        for row in rows:
            if row["run_id"] in self.steps:
                continue
            is_launch = row["state"] in {
                "planned",
                "environment_ready",
                "starting_agent",
            }
            if is_launch and launched:
                continue
            launched = launched or is_launch
            self.steps[row["run_id"]] = self.executor.submit(self.run_step, row, now)
        self.notify()

    def run_step(self, row: Any, now: float) -> None:
        try:
            self.advance(row, now)
        except Exception as error:
            current = self.store.one(
                "SELECT state,startup_at FROM job_runs WHERE run_id=?", (row["run_id"],)
            )
            state = current["state"]
            if (
                state == "starting_agent"
                and isinstance(error, (OSError, HerdrError))
                and now - (current["startup_at"] or now) <= 120
            ):
                # agent.start may finish after the socket disconnects.
                LOG.warning(
                    "Job %s is waiting for agent startup: %s", row["job_id"], error
                )
                return
            state = (
                "prompt_uncertain"
                if state == "submitting"
                else "uncertain"
                if state == "creating"
                else "failed"
            )
            self.update(row["run_id"], state=state, error=str(error))
            LOG.exception("Scheduled job %s: %s", row["job_id"], state)
