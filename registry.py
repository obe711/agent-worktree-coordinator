"""
File ownership registry — tracks which agent owns which files/prefixes.

The registry is written to `.agent-lock.json` at the root of the repo so
agents can query it independently, and the coordinator can update it atomically.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from .models import Agent

log = logging.getLogger(__name__)


class OwnershipConflictError(Exception):
    """Raised when two agents are assigned overlapping file prefixes."""
    pass


class FileRegistry:
    """
    Manages exclusive file ownership across all agents.

    Rules
    -----
    - Each file path (or directory prefix) may be owned by at most one agent.
    - Agents may only commit changes to files they own.
    - The coordinator enforces this at task-assignment time; agents that
      attempt to touch unregistered files are paused and alerted.
    """

    def __init__(self, repo_path: Path, registry_filename: str = ".agent-lock.json"):
        self.repo_path = repo_path
        self.registry_path = repo_path / registry_filename
        self._registry: dict[str, str] = {}  # file_prefix -> agent_id

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_agent(self, agent: Agent) -> None:
        """
        Register an agent's file ownership. Raises OwnershipConflictError if
        any of its files overlap with an already-registered agent.
        """
        conflicts = self._detect_conflicts(agent)
        if conflicts:
            msg = (
                f"Agent '{agent.id}' conflicts with existing registrations "
                f"on: {conflicts}"
            )
            raise OwnershipConflictError(msg)

        for prefix in agent.owned_files:
            self._registry[prefix] = agent.id

        self._persist()
        log.info("Registered agent '%s' → %s", agent.id, agent.owned_files)

    def deregister_agent(self, agent_id: str) -> None:
        """Remove all file entries for an agent (called when it finishes)."""
        removed = [k for k, v in self._registry.items() if v == agent_id]
        for key in removed:
            del self._registry[key]
        self._persist()
        log.info("Deregistered agent '%s' (freed %d paths)", agent_id, len(removed))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def owner_of(self, filepath: str) -> Optional[str]:
        """Return the agent_id that owns `filepath`, or None if unregistered."""
        for prefix, agent_id in self._registry.items():
            if filepath.startswith(prefix):
                return agent_id
        return None

    def is_owned_by(self, filepath: str, agent_id: str) -> bool:
        return self.owner_of(filepath) == agent_id

    def find_overlapping_agents(self, file_list: list[str]) -> dict[str, list[str]]:
        """
        Given a list of files, return a mapping of {agent_id: [files]} for
        every agent that owns at least one of those files.
        Useful for pre-flight checks before assigning a new task.
        """
        owners: dict[str, list[str]] = {}
        for f in file_list:
            owner = self.owner_of(f)
            if owner:
                owners.setdefault(owner, []).append(f)
        return owners

    def all_agents(self) -> list[str]:
        """Return the set of agent IDs currently registered."""
        return list(set(self._registry.values()))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write the registry to disk atomically (write + rename)."""
        tmp = self.registry_path.with_suffix(".tmp")
        data = {
            "registry": self._registry,
            "version": 1,
        }
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.registry_path)

    def load(self) -> None:
        """Load the registry from disk (called on coordinator start)."""
        if not self.registry_path.exists():
            return
        data = json.loads(self.registry_path.read_text())
        self._registry = data.get("registry", {})
        log.info("Loaded registry: %d owned paths", len(self._registry))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_conflicts(self, agent: Agent) -> list[str]:
        """Return file prefixes in `agent.owned_files` already owned by someone else."""
        conflicts = []
        for new_prefix in agent.owned_files:
            for existing_prefix, existing_owner in self._registry.items():
                if existing_owner == agent.id:
                    continue
                # Overlap in either direction
                if new_prefix.startswith(existing_prefix) or existing_prefix.startswith(new_prefix):
                    conflicts.append(f"{new_prefix!r} overlaps with {existing_prefix!r} (owned by {existing_owner!r})")
        return conflicts
