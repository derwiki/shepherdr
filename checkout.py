"""Refresh a primary checkout from origin/main without starting an agent."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path
from typing import Any


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=120,
        check=check,
    )


def busy_panes(repo: Path, herdr: Any) -> list[str]:
    result = []
    for agent in herdr.request("session.snapshot")["snapshot"]["agents"]:
        if not agent.get("agent"):
            continue
        for key in ("cwd", "foreground_cwd"):
            cwd = agent.get(key)
            if cwd and Path(cwd).resolve().is_relative_to(repo):
                result.append(agent["pane_id"])
                break
    return result


def refresh_checkout(
    repo: Path, *, discard_changes: bool, herdr: Any
) -> dict[str, Any]:
    repo = repo.resolve()
    if Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve() != repo:
        raise ValueError("Configure the repository root, not a subdirectory")
    gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    common = Path(git(repo, "rev-parse", "--git-common-dir").stdout.strip())
    common = common if common.is_absolute() else repo / common
    if gitdir.resolve() != common.resolve():
        raise ValueError("Checkout refresh is restricted to the primary worktree")
    with (gitdir / "shepherdr-refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "state": "skipped_refresh",
                "reason": "Another checkout refresh is running",
            }
        # Updating this remote-tracking ref also gives every linked worktree a
        # fresh local origin/main without changing their branches or files.
        git(
            repo,
            "fetch",
            "--no-tags",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        before = git(repo, "rev-parse", "HEAD").stdout.strip()
        target = git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        branch = git(
            repo, "symbolic-ref", "--short", "HEAD", check=False
        ).stdout.strip()
        result: dict[str, Any] = {
            "before": before,
            "target": target,
            "branch_before": branch,
            "discard_changes": discard_changes,
        }

        def skip(reason: str) -> dict[str, Any]:
            return {
                **result,
                "state": "skipped_refresh",
                "reason": reason,
                "after": before,
            }

        if any(
            (gitdir / marker).exists()
            for marker in (
                "index.lock",
                "MERGE_HEAD",
                "CHERRY_PICK_HEAD",
                "REVERT_HEAD",
                "rebase-apply",
                "rebase-merge",
                "sequencer",
            )
        ):
            return skip("A Git operation is in progress")
        if not discard_changes:
            if branch != "main":
                return skip("The checkout is on a task branch")
            if git(repo, "status", "--porcelain", "--untracked-files=no").stdout:
                return skip("The checkout has uncommitted tracked edits")
            if busy_panes(repo, herdr):
                return skip("An agent is using the checkout")
            if git(
                repo, "merge-base", "--is-ancestor", before, target, check=False
            ).returncode:
                return skip("Local main has commits not in origin/main")

        # Do not remove untracked or ignored paths that would become tracked.
        # git reset --hard can otherwise delete such obstacles.
        added = git(
            repo, "diff", "--name-only", "--diff-filter=A", "-z", before, target
        ).stdout.split("\0")
        tracked = set(git(repo, "ls-files", "-z").stdout.split("\0"))
        for name in filter(None, added):
            path = repo / name
            if os.path.lexists(path) and name not in tracked:
                return skip("An untracked path would be overwritten: " + name)
            for parent in path.parents:
                if parent == repo:
                    break
                relative = str(parent.relative_to(repo))
                if (
                    os.path.lexists(parent)
                    and (parent.is_symlink() or not parent.is_dir())
                    and relative not in tracked
                ):
                    return skip(
                        "An untracked parent path would be overwritten: " + relative
                    )
        if discard_changes:
            # Preserve saved commits before moving main or a detached HEAD.
            # Uncommitted tracked edits are discarded by explicit job policy.
            main = git(repo, "rev-parse", "--verify", "refs/heads/main", check=False)
            saved = main.stdout.strip() if main.returncode == 0 else before
            if git(
                repo, "merge-base", "--is-ancestor", saved, target, check=False
            ).returncode:
                ref = f"refs/shepherdr/refresh-main/{time.time_ns()}"
                git(repo, "update-ref", ref, saved)
                result["backup_ref"] = ref
            if (
                not branch
                and git(
                    repo, "merge-base", "--is-ancestor", before, target, check=False
                ).returncode
            ):
                ref = f"refs/shepherdr/refresh-detached/{time.time_ns()}"
                git(repo, "update-ref", ref, before)
                result["detached_backup_ref"] = ref
            if branch != "main":
                git(
                    repo,
                    "switch",
                    "--discard-changes",
                    "--force-create",
                    "main",
                    target,
                )
            git(repo, "reset", "--hard", target)
        else:
            git(repo, "reset", "--keep", target)
        after = git(repo, "rev-parse", "HEAD").stdout.strip()
        if (
            after != target
            or git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip() != "main"
        ):
            raise ValueError("Checkout verification failed after refresh")
        return {**result, "state": "done", "after": after}
