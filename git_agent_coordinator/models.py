"""
Core data models for the multi-agent git worktree coordinator.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class AgentStatus(Enum):
    IDLE      = "idle"
    RUNNING   = "running"
    PAUSED    = "paused"      # blocked on a conflict
    REBASING  = "rebasing"    # coordinator is rebasing its branch
    DONE      = "done"
    FAILED    = "failed"


class ConflictSeverity(Enum):
    NONE     = "none"
    DRIFT    = "drift"        # branch is N commits behind main — not yet a conflict
    CONFLICT = "conflict"     # git merge-tree detected an actual conflict


@dataclass
class Agent:
    """Represents one AI agent with its worktree and file ownership."""
    id: str                           # e.g. "agent-A"
    branch: str                       # e.g. "feature/auth"
    worktree_path: Path               # absolute path to this agent's worktree
    owned_files: list[str]            # file paths / prefixes this agent may edit
    depends_on: list[str] = field(default_factory=list)   # agent IDs that must land first
    status: AgentStatus = AgentStatus.IDLE
    commit_count: int = 0             # commits made since last rebase check
    last_rebase_sha: Optional[str] = None

    def owns(self, filepath: str) -> bool:
        """Return True if this agent is allowed to modify `filepath`."""
        return any(filepath.startswith(prefix) for prefix in self.owned_files)


@dataclass
class MergeCheckResult:
    """Result of a speculative git merge-tree check between two refs."""
    base_ref: str
    target_ref: str
    severity: ConflictSeverity
    conflicting_files: list[str] = field(default_factory=list)
    commits_behind: int = 0
    raw_output: str = ""


@dataclass
class CoordinatorConfig:
    """Tunable parameters for the coordinator loop."""
    repo_path: Path
    main_branch: str = "main"
    check_every_n_commits: int = 10   # run speculative checks after this many commits
    poll_interval_seconds: float = 15.0
    max_drift_commits: int = 20       # auto-rebase if branch is this many commits behind
    registry_file: str = ".agent-lock.json"
    auto_rebase: bool = True          # rebase branches automatically on drift/conflict
    squash_on_merge: bool = True      # squash each branch to one commit before landing
