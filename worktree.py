"""
Git worktree operations — thin wrapper around subprocess git commands.

All operations target a specific worktree path so they're safe to call
concurrently across multiple agent worktrees.
"""

from __future__ import annotations
import logging
import subprocess
from pathlib import Path
from typing import Optional

from .models import Agent, CoordinatorConfig, MergeCheckResult, ConflictSeverity

log = logging.getLogger(__name__)


class GitError(Exception):
    pass


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in `cwd`. Returns the CompletedProcess."""
    cmd = ["git"] + args
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True
    )
    if check and result.returncode not in (0, 1):
        raise GitError(
            f"git {' '.join(args)} failed (exit {result.returncode}):\n{result.stderr}"
        )
    return result


# ------------------------------------------------------------------
# Worktree lifecycle
# ------------------------------------------------------------------

def create_worktree(repo_path: Path, agent: Agent) -> None:
    """
    Create a new git worktree for `agent` at `agent.worktree_path`.
    Creates the branch if it does not exist yet.
    """
    worktree_path = agent.worktree_path
    branch = agent.branch

    # Check whether branch already exists
    result = _git(["branch", "--list", branch], cwd=repo_path, check=False)
    branch_exists = branch in result.stdout

    if branch_exists:
        _git(["worktree", "add", str(worktree_path), branch], cwd=repo_path)
    else:
        _git(["worktree", "add", "-b", branch, str(worktree_path)], cwd=repo_path)

    log.info("Created worktree '%s' → %s", agent.id, worktree_path)


def remove_worktree(repo_path: Path, agent: Agent) -> None:
    """Remove the worktree (but keep the branch for PR purposes)."""
    _git(["worktree", "remove", "--force", str(agent.worktree_path)], cwd=repo_path)
    log.info("Removed worktree '%s'", agent.id)


def list_worktrees(repo_path: Path) -> list[dict]:
    """Return a list of dicts describing all active worktrees."""
    result = _git(["worktree", "list", "--porcelain"], cwd=repo_path)
    worktrees = []
    current: dict = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                worktrees.append(current)
            current = {}
        elif line.startswith("worktree "):
            current["path"] = line.split(" ", 1)[1]
        elif line.startswith("HEAD "):
            current["HEAD"] = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1]
    if current:
        worktrees.append(current)
    return worktrees


# ------------------------------------------------------------------
# Branch inspection
# ------------------------------------------------------------------

def current_sha(worktree_path: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=worktree_path).stdout.strip()


def commits_behind(worktree_path: Path, upstream: str) -> int:
    """How many commits is this worktree's branch behind `upstream`?"""
    result = _git(
        ["rev-list", "--count", f"HEAD..{upstream}"],
        cwd=worktree_path
    )
    return int(result.stdout.strip() or "0")


def commit_count_since(worktree_path: Path, since_sha: str) -> int:
    """Count commits on HEAD since `since_sha`."""
    result = _git(
        ["rev-list", "--count", f"{since_sha}..HEAD"],
        cwd=worktree_path
    )
    return int(result.stdout.strip() or "0")


def changed_files(worktree_path: Path, base: str = "main") -> list[str]:
    """Files changed on this branch relative to `base`."""
    result = _git(
        ["diff", "--name-only", f"{base}...HEAD"],
        cwd=worktree_path
    )
    return [f for f in result.stdout.splitlines() if f]


# ------------------------------------------------------------------
# Speculative merge check (zero side effects)
# ------------------------------------------------------------------

def speculative_merge_check(
    repo_path: Path,
    branch_a: str,
    branch_b: str,
) -> MergeCheckResult:
    """
    Use `git merge-tree` to simulate merging branch_a into branch_b
    WITHOUT touching any working tree or index.

    Returns a MergeCheckResult describing any conflicts found.

    git merge-tree exits 0 on clean merge, 1 on conflict.
    With --write-tree it prints the resulting tree SHA (clean) or
    conflict markers (conflict).
    """
    # Find the merge base first
    base_result = _git(
        ["merge-base", branch_a, branch_b],
        cwd=repo_path
    )
    merge_base = base_result.stdout.strip()

    # Run the speculative merge
    result = _git(
        ["merge-tree", "--write-tree", "--no-messages", merge_base, branch_a, branch_b],
        cwd=repo_path,
        check=False,
    )

    if result.returncode == 0:
        return MergeCheckResult(
            base_ref=branch_a,
            target_ref=branch_b,
            severity=ConflictSeverity.NONE,
        )

    # Parse conflict output — each conflicting path is listed after "CONFLICT"
    conflicting_files = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("CONFLICT"):
            # Format: "CONFLICT (content): Merge conflict in path/to/file.py"
            if " in " in line:
                conflicting_files.append(line.split(" in ", 1)[1].strip())

    return MergeCheckResult(
        base_ref=branch_a,
        target_ref=branch_b,
        severity=ConflictSeverity.CONFLICT,
        conflicting_files=conflicting_files,
        raw_output=result.stdout,
    )


# ------------------------------------------------------------------
# Rebase & merge
# ------------------------------------------------------------------

def rebase_onto(worktree_path: Path, onto: str) -> bool:
    """
    Rebase this worktree's branch onto `onto` (e.g. "main").
    Returns True on success, False if conflicts arise (caller must handle).
    """
    result = _git(["rebase", onto], cwd=worktree_path, check=False)
    if result.returncode != 0:
        # Abort the rebase so the worktree is left clean
        _git(["rebase", "--abort"], cwd=worktree_path, check=False)
        log.warning(
            "Rebase onto %s failed in %s:\n%s",
            onto, worktree_path, result.stderr
        )
        return False
    log.info("Rebased %s onto %s", worktree_path, onto)
    return True


def squash_branch(repo_path: Path, branch: str, main_branch: str, message: str) -> bool:
    """
    Squash all commits on `branch` (since it diverged from `main_branch`)
    into a single commit with `message`.
    """
    base = _git(["merge-base", main_branch, branch], cwd=repo_path).stdout.strip()
    # Soft-reset to the merge base so all changes become staged
    result = _git(["reset", "--soft", base], cwd=repo_path, check=False)
    if result.returncode != 0:
        return False
    _git(["commit", "-m", message], cwd=repo_path)
    log.info("Squashed branch '%s' onto %s", branch, base[:8])
    return True


def merge_branch(
    repo_path: Path,
    branch: str,
    into: str,
    config: CoordinatorConfig,
    commit_message: Optional[str] = None,
) -> bool:
    """
    Merge `branch` into `into` (typically main).
    If config.squash_on_merge, squashes first.
    """
    # Ensure we're on the target branch
    _git(["checkout", into], cwd=repo_path)

    if config.squash_on_merge:
        msg = commit_message or f"feat: merge {branch}"
        result = _git(["merge", "--squash", branch], cwd=repo_path, check=False)
        if result.returncode != 0:
            return False
        _git(["commit", "-m", msg], cwd=repo_path)
    else:
        result = _git(
            ["merge", "--no-ff", branch, "-m", commit_message or f"Merge {branch}"],
            cwd=repo_path,
            check=False,
        )
        if result.returncode != 0:
            return False

    log.info("Merged '%s' into '%s'", branch, into)
    return True
