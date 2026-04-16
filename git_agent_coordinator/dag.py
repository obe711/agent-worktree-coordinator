"""
Dependency DAG — builds a directed acyclic graph of agent dependencies
and produces a topological merge order.

Each agent can declare `depends_on: [agent_id, ...]` meaning
"my PR must land AFTER those agents' PRs have landed."
"""

from __future__ import annotations
from collections import defaultdict, deque
from typing import Generator

from .models import Agent


class CyclicDependencyError(Exception):
    pass


class DependencyDAG:
    """
    Directed Acyclic Graph of agent dependencies.

    Edges: A → B means "A must be merged before B"
    (i.e. B depends_on A).
    """

    def __init__(self, agents: list[Agent]):
        self.agents: dict[str, Agent] = {a.id: a for a in agents}
        # adjacency: prereq_id -> set of dependent_ids
        self._edges: dict[str, set[str]] = defaultdict(set)
        self._build(agents)

    def _build(self, agents: list[Agent]) -> None:
        for agent in agents:
            for prereq_id in agent.depends_on:
                if prereq_id not in self.agents:
                    raise ValueError(
                        f"Agent '{agent.id}' depends on unknown agent '{prereq_id}'"
                    )
                self._edges[prereq_id].add(agent.id)

    # ------------------------------------------------------------------
    # Topological sort (Kahn's algorithm)
    # ------------------------------------------------------------------

    def merge_order(self) -> list[Agent]:
        """
        Return agents in the order they should be merged into main.

        - Agents with no outstanding dependencies come first.
        - Within the same "tier" (agents that could be merged in parallel),
          order is stable (insertion order of the original agents list).
        - Raises CyclicDependencyError if there's a cycle.
        """
        in_degree = {aid: 0 for aid in self.agents}
        for prereq_id, dependents in self._edges.items():
            for dep in dependents:
                in_degree[dep] += 1

        # Start with all agents that have no prerequisites
        queue: deque[str] = deque(
            aid for aid, deg in in_degree.items() if deg == 0
        )
        order: list[Agent] = []

        while queue:
            aid = queue.popleft()
            order.append(self.agents[aid])
            for dependent_id in sorted(self._edges.get(aid, set())):
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        if len(order) != len(self.agents):
            cycle_nodes = [a for a, deg in in_degree.items() if deg > 0]
            raise CyclicDependencyError(
                f"Cyclic dependency detected among: {cycle_nodes}"
            )

        return order

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def direct_dependents(self, agent_id: str) -> list[str]:
        """Return agent IDs that directly depend on `agent_id`."""
        return sorted(self._edges.get(agent_id, set()))

    def all_dependents(self, agent_id: str) -> list[str]:
        """Return all transitive dependents of `agent_id` (BFS)."""
        visited: set[str] = set()
        queue: deque[str] = deque([agent_id])
        while queue:
            current = queue.popleft()
            for dep in self._edges.get(current, set()):
                if dep not in visited:
                    visited.add(dep)
                    queue.append(dep)
        visited.discard(agent_id)
        return sorted(visited)

    def prerequisites_met(self, agent_id: str, completed: set[str]) -> bool:
        """Return True if all agents that `agent_id` depends on are in `completed`."""
        agent = self.agents[agent_id]
        return all(prereq in completed for prereq in agent.depends_on)

    # ------------------------------------------------------------------
    # Debug / display
    # ------------------------------------------------------------------

    def describe(self) -> str:
        lines = ["Dependency DAG:"]
        for agent in self.merge_order():
            prereqs = agent.depends_on
            if prereqs:
                lines.append(f"  {agent.id} ({agent.branch})  ←  depends on {prereqs}")
            else:
                lines.append(f"  {agent.id} ({agent.branch})  ←  no deps (land first)")
        return "\n".join(lines)
