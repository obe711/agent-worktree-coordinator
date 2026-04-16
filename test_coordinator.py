"""
tests/test_coordinator.py
-------------------------
Unit tests for the DAG, registry, and coordinator logic.
No real git operations — worktree.py calls are mocked.
"""

from __future__ import annotations
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from git_agent_coordinator import (
    Agent,
    AgentStatus,
    CoordinatorConfig,
    DependencyDAG,
    FileRegistry,
    OwnershipConflictError,
    WorktreeCoordinator,
)
from git_agent_coordinator.dag import CyclicDependencyError
from git_agent_coordinator.models import ConflictSeverity, MergeCheckResult


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path):
    return tmp_path

@pytest.fixture
def four_agents(tmp_path):
    return [
        Agent(
            id="agent-A", branch="feature/auth",
            worktree_path=tmp_path / "ws-A",
            owned_files=["src/auth/", "tests/auth/"],
            depends_on=[],
        ),
        Agent(
            id="agent-B", branch="feature/billing",
            worktree_path=tmp_path / "ws-B",
            owned_files=["src/billing/", "tests/billing/"],
            depends_on=["agent-C"],
        ),
        Agent(
            id="agent-C", branch="feature/api",
            worktree_path=tmp_path / "ws-C",
            owned_files=["src/api/", "tests/api/"],
            depends_on=["agent-A"],
        ),
        Agent(
            id="agent-D", branch="feature/ui",
            worktree_path=tmp_path / "ws-D",
            owned_files=["src/ui/", "tests/ui/"],
            depends_on=["agent-B", "agent-C"],
        ),
    ]


# ------------------------------------------------------------------
# FileRegistry tests
# ------------------------------------------------------------------

class TestFileRegistry:
    def test_register_non_overlapping(self, tmp_repo):
        reg = FileRegistry(tmp_repo)
        a = Agent("a", "feature/a", tmp_repo/"ws-a", ["src/auth/"], [])
        b = Agent("b", "feature/b", tmp_repo/"ws-b", ["src/billing/"], [])
        reg.register_agent(a)
        reg.register_agent(b)
        assert reg.owner_of("src/auth/login.py") == "a"
        assert reg.owner_of("src/billing/invoice.py") == "b"
        assert reg.owner_of("src/other/thing.py") is None

    def test_register_overlap_raises(self, tmp_repo):
        reg = FileRegistry(tmp_repo)
        a = Agent("a", "feature/a", tmp_repo/"ws-a", ["src/shared/"], [])
        b = Agent("b", "feature/b", tmp_repo/"ws-b", ["src/shared/utils.py"], [])
        reg.register_agent(a)
        with pytest.raises(OwnershipConflictError):
            reg.register_agent(b)

    def test_deregister_frees_paths(self, tmp_repo):
        reg = FileRegistry(tmp_repo)
        a = Agent("a", "feature/a", tmp_repo/"ws-a", ["src/auth/"], [])
        reg.register_agent(a)
        reg.deregister_agent("a")
        assert reg.owner_of("src/auth/login.py") is None

    def test_persistence(self, tmp_repo):
        reg = FileRegistry(tmp_repo)
        a = Agent("a", "feature/a", tmp_repo/"ws-a", ["src/auth/"], [])
        reg.register_agent(a)
        # Reload from disk
        reg2 = FileRegistry(tmp_repo)
        reg2.load()
        assert reg2.owner_of("src/auth/login.py") == "a"

    def test_find_overlapping_agents(self, tmp_repo):
        reg = FileRegistry(tmp_repo)
        a = Agent("a", "feature/a", tmp_repo/"ws-a", ["src/auth/"], [])
        b = Agent("b", "feature/b", tmp_repo/"ws-b", ["src/billing/"], [])
        reg.register_agent(a)
        reg.register_agent(b)
        result = reg.find_overlapping_agents(["src/auth/login.py", "src/other/foo.py"])
        assert result == {"a": ["src/auth/login.py"]}


# ------------------------------------------------------------------
# DependencyDAG tests
# ------------------------------------------------------------------

class TestDependencyDAG:
    def test_merge_order_respects_deps(self, four_agents):
        dag = DependencyDAG(four_agents)
        order = [a.id for a in dag.merge_order()]
        # A before C, C before B, B and C before D
        assert order.index("agent-A") < order.index("agent-C")
        assert order.index("agent-C") < order.index("agent-B")
        assert order.index("agent-B") < order.index("agent-D")

    def test_no_deps_land_first(self, four_agents):
        dag = DependencyDAG(four_agents)
        order = dag.merge_order()
        assert order[0].id == "agent-A"

    def test_cyclic_dependency_raises(self, tmp_path):
        agents = [
            Agent("x", "feature/x", tmp_path/"wx", ["src/x/"], ["y"]),
            Agent("y", "feature/y", tmp_path/"wy", ["src/y/"], ["x"]),
        ]
        with pytest.raises(CyclicDependencyError):
            DependencyDAG(agents).merge_order()

    def test_prerequisites_met(self, four_agents):
        dag = DependencyDAG(four_agents)
        assert dag.prerequisites_met("agent-A", set()) is True
        assert dag.prerequisites_met("agent-C", set()) is False
        assert dag.prerequisites_met("agent-C", {"agent-A"}) is True

    def test_all_dependents(self, four_agents):
        dag = DependencyDAG(four_agents)
        # Everything eventually depends on agent-A
        all_deps = dag.all_dependents("agent-A")
        assert set(all_deps) == {"agent-C", "agent-B", "agent-D"}

    def test_direct_dependents(self, four_agents):
        dag = DependencyDAG(four_agents)
        assert dag.direct_dependents("agent-A") == ["agent-C"]
        # agent-B depends on agent-C, and agent-D also directly depends on agent-C
        assert dag.direct_dependents("agent-C") == ["agent-B", "agent-D"]


# ------------------------------------------------------------------
# Coordinator integration tests (git operations mocked)
# ------------------------------------------------------------------

class TestCoordinator:
    def _make_coordinator(self, tmp_path, agents):
        config = CoordinatorConfig(
            repo_path=tmp_path,
            main_branch="main",
            poll_interval_seconds=0.01,
            check_every_n_commits=5,
        )
        return WorktreeCoordinator(config, agents)

    @patch("git_agent_coordinator.coordinator.create_worktree")
    @patch("git_agent_coordinator.coordinator.current_sha", return_value="abc123")
    def test_setup_registers_all_agents(self, mock_sha, mock_create, tmp_path, four_agents):
        c = self._make_coordinator(tmp_path, four_agents)
        c.setup()
        assert all(a.status == AgentStatus.RUNNING for a in c.agents.values())
        assert set(c.registry.all_agents()) == {"agent-A", "agent-B", "agent-C", "agent-D"}

    @patch("git_agent_coordinator.coordinator.create_worktree")
    @patch("git_agent_coordinator.coordinator.current_sha", return_value="abc123")
    def test_setup_raises_on_file_overlap(self, mock_sha, mock_create, tmp_path):
        agents = [
            Agent("a", "feature/a", tmp_path/"ws-a", ["src/shared/"], []),
            Agent("b", "feature/b", tmp_path/"ws-b", ["src/shared/utils.py"], []),
        ]
        c = self._make_coordinator(tmp_path, agents)
        with pytest.raises(RuntimeError, match="overlaps"):
            c.setup()

    @patch("git_agent_coordinator.coordinator.create_worktree")
    @patch("git_agent_coordinator.coordinator.current_sha", return_value="abc123")
    def test_mark_agent_done_queues_for_merge(self, mock_sha, mock_create, tmp_path, four_agents):
        c = self._make_coordinator(tmp_path, four_agents)
        c.setup()
        c.mark_agent_done("agent-A")
        assert c.agents["agent-A"].status == AgentStatus.DONE

    @patch("git_agent_coordinator.coordinator.speculative_merge_check")
    @patch("git_agent_coordinator.coordinator.merge_branch", return_value=True)
    @patch("git_agent_coordinator.coordinator.rebase_onto", return_value=True)
    @patch("git_agent_coordinator.coordinator.commits_behind", return_value=0)
    @patch("git_agent_coordinator.coordinator.commit_count_since", return_value=0)
    @patch("git_agent_coordinator.coordinator.create_worktree")
    @patch("git_agent_coordinator.coordinator.current_sha", return_value="abc123")
    @patch("git_agent_coordinator.coordinator.remove_worktree")
    def test_topological_merge_order(
        self, mock_rm, mock_sha, mock_create,
        mock_count, mock_behind, mock_rebase, mock_merge, mock_check,
        tmp_path, four_agents
    ):
        mock_check.return_value = MergeCheckResult(
            base_ref="x", target_ref="y", severity=ConflictSeverity.NONE
        )
        c = self._make_coordinator(tmp_path, four_agents)
        c.setup()

        merge_order = []
        original_land = c._land_branch

        def tracking_land(agent):
            merge_order.append(agent.id)
            original_land(agent)

        c._land_branch = tracking_land

        # Mark all agents done
        for aid in ["agent-A", "agent-C", "agent-B", "agent-D"]:
            c.mark_agent_done(aid)

        # Run a few ticks manually
        for _ in range(10):
            c._attempt_pending_merges()

        # A must land before C, C before B, B before D
        assert merge_order.index("agent-A") < merge_order.index("agent-C")
        assert merge_order.index("agent-C") < merge_order.index("agent-B")
        assert merge_order.index("agent-B") < merge_order.index("agent-D")
