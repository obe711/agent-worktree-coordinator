"""
WorktreeCoordinator — the central process that manages all agents,
runs speculative merge checks, triggers rebases, and orchestrates
the topological merge sequence.

Usage
-----
    from git_agent_coordinator import WorktreeCoordinator, Agent, CoordinatorConfig
    from pathlib import Path

    config = CoordinatorConfig(repo_path=Path("/path/to/repo"))

    agents = [
        Agent(
            id="agent-A",
            branch="feature/auth",
            worktree_path=Path("/path/to/ws-agent-A"),
            owned_files=["src/auth/", "src/middleware/", "tests/auth.test.ts"],
            depends_on=[],
        ),
        Agent(
            id="agent-B",
            branch="feature/api",
            worktree_path=Path("/path/to/ws-agent-B"),
            owned_files=["src/api/routes/", "src/api/schema.ts"],
            depends_on=["agent-A"],   # needs auth to be merged first
        ),
    ]

    coordinator = WorktreeCoordinator(config, agents)
    coordinator.setup()           # create worktrees, write registry
    coordinator.run()             # start the monitoring loop (blocking)
"""

from __future__ import annotations
import logging
import time
from pathlib import Path
from typing import Callable, Optional

from .dag import DependencyDAG
from .models import Agent, AgentStatus, CoordinatorConfig, ConflictSeverity, MergeCheckResult
from .registry import FileRegistry, OwnershipConflictError
from .worktree import (
    GitError,
    commit_count_since,
    commits_behind,
    create_worktree,
    current_sha,
    merge_branch,
    rebase_onto,
    remove_worktree,
    speculative_merge_check,
)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Event hooks (optional callbacks the caller can attach)
# ------------------------------------------------------------------

class CoordinatorEvents:
    """
    Override any of these methods in a subclass (or replace them
    as callables) to hook into coordinator lifecycle events —
    e.g. to send Slack notifications or pause an agent process.
    """
    def on_conflict_detected(self, result: MergeCheckResult, agents: list[Agent]) -> None:
        log.warning(
            "[CONFLICT] %s ↔ %s — files: %s",
            result.base_ref, result.target_ref, result.conflicting_files
        )

    def on_agent_paused(self, agent: Agent, reason: str) -> None:
        log.warning("[PAUSED] Agent '%s': %s", agent.id, reason)

    def on_agent_resumed(self, agent: Agent) -> None:
        log.info("[RESUMED] Agent '%s'", agent.id)

    def on_rebase_complete(self, agent: Agent) -> None:
        log.info("[REBASED] Agent '%s' is up to date", agent.id)

    def on_rebase_failed(self, agent: Agent) -> None:
        log.error("[REBASE FAILED] Agent '%s' needs manual intervention", agent.id)

    def on_merge_complete(self, agent: Agent) -> None:
        log.info("[MERGED] Agent '%s' → %s", agent.id, "main")

    def on_merge_cascade(self, rebased_agents: list[Agent]) -> None:
        log.info("[CASCADE] Rebased %d branches after merge", len(rebased_agents))


# ------------------------------------------------------------------
# Coordinator
# ------------------------------------------------------------------

class WorktreeCoordinator:
    """
    Manages the full lifecycle of a multi-agent git worktree session.

    Responsibilities
    ----------------
    1. Setup      — create worktrees, register file ownership
    2. Monitor    — poll branches for drift and speculative conflicts
    3. Rebase     — keep branches close to main automatically
    4. Merge      — land PRs in topological dependency order
    5. Teardown   — clean up worktrees when agents finish
    """

    def __init__(
        self,
        config: CoordinatorConfig,
        agents: list[Agent],
        events: Optional[CoordinatorEvents] = None,
    ):
        self.config = config
        self.agents: dict[str, Agent] = {a.id: a for a in agents}
        self.events = events or CoordinatorEvents()

        self.registry = FileRegistry(config.repo_path, config.registry_file)
        self.dag = DependencyDAG(agents)
        self._merged: set[str] = set()   # agent IDs that have been merged into main
        self._running = False

    # ------------------------------------------------------------------
    # Phase 1 — Setup
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """
        Create all worktrees and register file ownership.
        Call this once before starting the monitoring loop.
        """
        self.registry.load()

        for agent in self.agents.values():
            # Validate ownership before touching the filesystem
            try:
                self.registry.register_agent(agent)
            except OwnershipConflictError as e:
                raise RuntimeError(
                    f"Cannot set up agent '{agent.id}': {e}"
                ) from e

            if not agent.worktree_path.exists():
                create_worktree(self.config.repo_path, agent)
                agent.last_rebase_sha = current_sha(agent.worktree_path)

            agent.status = AgentStatus.RUNNING

        log.info(
            "Setup complete. %d agents registered.\n%s",
            len(self.agents), self.dag.describe()
        )

    # ------------------------------------------------------------------
    # Phase 2 — Monitoring loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Start the coordinator monitoring loop (blocking).
        Runs until all agents are in DONE or FAILED state.
        """
        self._running = True
        log.info("Coordinator started (poll every %.0fs)", self.config.poll_interval_seconds)

        while self._running and not self._all_done():
            try:
                self._tick()
            except Exception as exc:
                log.exception("Unexpected error in coordinator tick: %s", exc)

            time.sleep(self.config.poll_interval_seconds)

        log.info("All agents finished. Coordinator exiting.")
        self._teardown()

    def stop(self) -> None:
        """Request a graceful stop of the monitoring loop."""
        self._running = False

    def _tick(self) -> None:
        """
        One iteration of the monitoring loop:
        1. Check each running agent for commit count → trigger speculative checks
        2. Check each running agent for drift → trigger rebases
        3. Check for newly completable merges in topological order
        """
        for agent in self._active_agents():
            self._check_agent(agent)

        self._attempt_pending_merges()

    def _check_agent(self, agent: Agent) -> None:
        """Run drift + speculative conflict checks for a single agent."""
        if not agent.worktree_path.exists():
            return

        # -- Drift check --------------------------------------------------
        behind = commits_behind(agent.worktree_path, self.config.main_branch)

        if behind > self.config.max_drift_commits and self.config.auto_rebase:
            log.info(
                "Agent '%s' is %d commits behind — triggering auto-rebase",
                agent.id, behind
            )
            self._rebase_agent(agent)
            return  # rebase handles everything; re-check next tick

        # -- Commit-count trigger for speculative checks -------------------
        if agent.last_rebase_sha:
            new_commits = commit_count_since(agent.worktree_path, agent.last_rebase_sha)
            agent.commit_count = new_commits
        else:
            agent.commit_count += 1  # rough fallback

        if agent.commit_count >= self.config.check_every_n_commits:
            self._run_speculative_checks(agent)
            agent.commit_count = 0
            agent.last_rebase_sha = current_sha(agent.worktree_path)

    # ------------------------------------------------------------------
    # Speculative merge checking
    # ------------------------------------------------------------------

    def _run_speculative_checks(self, trigger_agent: Agent) -> None:
        """
        Run git merge-tree between trigger_agent's branch and:
          a) main
          b) every other active agent's branch

        Conflicts are reported but not auto-resolved (that requires
        agent-level decision making); instead the lower-priority branch
        is paused and its owner notified.
        """
        log.debug("Running speculative checks for '%s'", trigger_agent.id)

        # Check against main
        result = speculative_merge_check(
            self.config.repo_path,
            trigger_agent.branch,
            self.config.main_branch,
        )
        if result.severity == ConflictSeverity.CONFLICT:
            self.events.on_conflict_detected(result, [trigger_agent])
            self._handle_conflict_with_main(trigger_agent, result)

        # Cross-check against every other active agent
        for other in self._active_agents():
            if other.id == trigger_agent.id:
                continue
            result = speculative_merge_check(
                self.config.repo_path,
                trigger_agent.branch,
                other.branch,
            )
            if result.severity == ConflictSeverity.CONFLICT:
                self.events.on_conflict_detected(result, [trigger_agent, other])
                self._handle_agent_vs_agent_conflict(trigger_agent, other, result)

    def _handle_conflict_with_main(
        self, agent: Agent, result: MergeCheckResult
    ) -> None:
        """
        An agent's branch conflicts with main. This usually means main has
        moved (another PR landed) and the agent's changes now clash.
        Strategy: pause the agent, rebase, then resume.
        """
        self.events.on_agent_paused(
            agent, f"Conflict with main on files: {result.conflicting_files}"
        )
        agent.status = AgentStatus.PAUSED
        success = self._rebase_agent(agent)
        if success:
            agent.status = AgentStatus.RUNNING
            self.events.on_agent_resumed(agent)

    def _handle_agent_vs_agent_conflict(
        self, agent_a: Agent, agent_b: Agent, result: MergeCheckResult
    ) -> None:
        """
        Two concurrent agents are about to conflict with each other.
        Resolution: pause the agent whose branch is NEWER (landed later),
        let the earlier-branching agent finish, then rebase the paused one.

        Heuristic: the agent with more commits behind main started later
        and is the likely 'modifier' — pause that one.
        """
        behind_a = commits_behind(agent_a.worktree_path, self.config.main_branch)
        behind_b = commits_behind(agent_b.worktree_path, self.config.main_branch)

        # The agent more behind main is the newer branch — pause it
        to_pause = agent_a if behind_a >= behind_b else agent_b
        to_continue = agent_b if to_pause is agent_a else agent_a

        log.info(
            "Pausing '%s' (newer branch) — will rebase after '%s' lands",
            to_pause.id, to_continue.id
        )
        self.events.on_agent_paused(
            to_pause,
            f"Cross-agent conflict with '{to_continue.id}' on: {result.conflicting_files}"
        )
        to_pause.status = AgentStatus.PAUSED

    # ------------------------------------------------------------------
    # Rebase
    # ------------------------------------------------------------------

    def _rebase_agent(self, agent: Agent) -> bool:
        """
        Rebase `agent`'s worktree onto main. Updates agent state accordingly.
        Returns True on success.
        """
        agent.status = AgentStatus.REBASING
        success = rebase_onto(agent.worktree_path, self.config.main_branch)

        if success:
            agent.last_rebase_sha = current_sha(agent.worktree_path)
            agent.commit_count = 0
            self.events.on_rebase_complete(agent)
            agent.status = AgentStatus.RUNNING
        else:
            agent.status = AgentStatus.PAUSED
            self.events.on_rebase_failed(agent)

        return success

    def _cascade_rebase(self, just_merged_agent: Agent) -> None:
        """
        After `just_merged_agent` lands on main, rebase ALL remaining
        running/paused agents so they stay current.

        Paused agents that were blocked by the just-merged agent get
        resumed if their conflict files were only with that agent.
        """
        rebased: list[Agent] = []

        for agent in self.agents.values():
            if agent.id == just_merged_agent.id:
                continue
            if agent.status in (AgentStatus.DONE, AgentStatus.FAILED):
                continue

            success = self._rebase_agent(agent)
            if success:
                rebased.append(agent)
            # Paused agents that rebase successfully are un-paused
            if success and agent.status == AgentStatus.PAUSED:
                agent.status = AgentStatus.RUNNING
                self.events.on_agent_resumed(agent)

        if rebased:
            self.events.on_merge_cascade(rebased)

    # ------------------------------------------------------------------
    # Phase 3 — Topological merge
    # ------------------------------------------------------------------

    def _attempt_pending_merges(self) -> None:
        """
        Walk the topological merge order. For each agent whose:
          - dependencies are all merged, AND
          - status is DONE (agent has signalled it's finished writing code)
        — land its PR into main, then trigger a rebase cascade.
        """
        for agent in self.dag.merge_order():
            if agent.id in self._merged:
                continue
            if agent.status != AgentStatus.DONE:
                continue
            if not self.dag.prerequisites_met(agent.id, self._merged):
                log.debug(
                    "Agent '%s' is done but waiting on prerequisites: %s",
                    agent.id,
                    [p for p in agent.depends_on if p not in self._merged]
                )
                continue

            # All clear — land this branch
            self._land_branch(agent)

    def _land_branch(self, agent: Agent) -> None:
        """
        Final pre-merge check, then merge into main, then cascade.
        """
        # One last speculative check before merging
        final_check = speculative_merge_check(
            self.config.repo_path,
            agent.branch,
            self.config.main_branch,
        )
        if final_check.severity == ConflictSeverity.CONFLICT:
            log.warning(
                "Pre-merge check FAILED for '%s' — conflicts: %s. Rebasing first.",
                agent.id, final_check.conflicting_files
            )
            success = self._rebase_agent(agent)
            if not success:
                log.error("Cannot land '%s' — rebase failed. Skipping.", agent.id)
                agent.status = AgentStatus.FAILED
                return

        # Merge
        commit_msg = f"feat({agent.branch.split('/')[-1]}): merge agent {agent.id}"
        success = merge_branch(
            self.config.repo_path,
            agent.branch,
            self.config.main_branch,
            self.config,
            commit_message=commit_msg,
        )

        if success:
            self._merged.add(agent.id)
            self.events.on_merge_complete(agent)
            self.registry.deregister_agent(agent.id)
            # Cascade rebase onto all remaining branches immediately
            self._cascade_rebase(agent)
        else:
            log.error("Merge of '%s' failed — marking as FAILED", agent.id)
            agent.status = AgentStatus.FAILED

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _teardown(self) -> None:
        """Remove all worktrees after a successful run."""
        for agent in self.agents.values():
            if agent.worktree_path.exists():
                try:
                    remove_worktree(self.config.repo_path, agent)
                except GitError as e:
                    log.warning("Could not remove worktree for '%s': %s", agent.id, e)

        # Remove the lock file
        lock = self.config.repo_path / self.config.registry_file
        if lock.exists():
            lock.unlink()
        log.info("Teardown complete.")

    # ------------------------------------------------------------------
    # Agent status helpers (called externally by agent processes)
    # ------------------------------------------------------------------

    def mark_agent_done(self, agent_id: str) -> None:
        """
        Called by an agent (or its wrapper) when it has finished writing code.
        The coordinator will then schedule its PR for merging.
        """
        agent = self.agents.get(agent_id)
        if agent:
            agent.status = AgentStatus.DONE
            log.info("Agent '%s' marked as done — queued for merge", agent_id)

    def mark_agent_failed(self, agent_id: str, reason: str = "") -> None:
        agent = self.agents.get(agent_id)
        if agent:
            agent.status = AgentStatus.FAILED
            log.error("Agent '%s' failed: %s", agent_id, reason)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _active_agents(self) -> list[Agent]:
        return [
            a for a in self.agents.values()
            if a.status in (AgentStatus.RUNNING, AgentStatus.REBASING)
        ]

    def _all_done(self) -> bool:
        return all(
            a.status in (AgentStatus.DONE, AgentStatus.FAILED)
            or a.id in self._merged
            for a in self.agents.values()
        )
