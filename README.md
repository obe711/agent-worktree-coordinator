# git_agent_coordinator

Efficiently manages a multi-agent system working with git worktrees.
Prevents merge conflicts through three phases of coordination.

## How it works

### Phase 1 — Partition (before agents start)
Each agent is assigned **exclusive file ownership**. No two concurrent
agents ever touch the same file or directory prefix. Overlaps are caught
at setup time and raise an error before any worktrees are created.

### Phase 2 — Monitor (while agents work)
A coordinator process polls every branch continuously:
- Runs `git merge-tree` (zero side effects) to detect conflicts early.
- Auto-rebases branches that drift too far behind `main`.
- Pauses lower-priority agents when an agent-vs-agent conflict is detected.

### Phase 3 — Merge (when agents finish)
Agents declare `depends_on` relationships. The coordinator builds a DAG
and merges PRs in **topological order**. After each merge, all remaining
branches are immediately rebased — preventing drift from compounding.

## Installation

```bash
pip install git+https://github.com/your-org/git-agent-coordinator
```

Or just copy the `git_agent_coordinator/` directory into your project.

## Quick start

```python
from git_agent_coordinator import WorktreeCoordinator, Agent, CoordinatorConfig
from pathlib import Path

config = CoordinatorConfig(
    repo_path=Path("/path/to/repo"),
    check_every_n_commits=10,   # run speculative checks every 10 commits
    max_drift_commits=20,       # auto-rebase if branch drifts this far
    squash_on_merge=True,       # squash to one commit before landing
)

agents = [
    Agent(
        id="agent-A",
        branch="feature/auth",
        worktree_path=Path("/path/to/ws-agent-A"),
        owned_files=["src/auth/", "tests/auth/"],
        depends_on=[],
    ),
    Agent(
        id="agent-B",
        branch="feature/api",
        worktree_path=Path("/path/to/ws-agent-B"),
        owned_files=["src/api/"],
        depends_on=["agent-A"],   # needs auth to land first
    ),
]

coordinator = WorktreeCoordinator(config, agents)
coordinator.setup()

# In your agent runner, call this when each agent finishes writing code:
# coordinator.mark_agent_done("agent-A")

coordinator.run()   # blocks until everything is merged
```

## Handling shared files

If two agents must both touch the same file (e.g. `package.json`), you
have two options:

**Option A — Serialize:** Don't run them in parallel. Add `depends_on`.

**Option B — Extract:** Create a prerequisite PR that only touches the
shared file, merge it first, then let both agents run in parallel on
their own files.

```python
# Option B: shared-deps lands first, others run in parallel
Agent(id="shared",  branch="chore/deps",    owned_files=["package.json"], depends_on=[]),
Agent(id="agent-A", branch="feature/auth",  owned_files=["src/auth/"],   depends_on=["shared"]),
Agent(id="agent-B", branch="feature/api",   owned_files=["src/api/"],    depends_on=["shared"]),
```

## Custom events

Override `CoordinatorEvents` to hook into lifecycle events:

```python
from git_agent_coordinator import CoordinatorEvents

class MyEvents(CoordinatorEvents):
    def on_conflict_detected(self, result, agents):
        slack.post(f"Conflict detected: {result.conflicting_files}")

    def on_agent_paused(self, agent, reason):
        # Send a SIGSTOP or HTTP call to pause the agent process
        agent_processes[agent.id].pause()

coordinator = WorktreeCoordinator(config, agents, events=MyEvents())
```

## Running tests

```bash
pip install pytest
pytest git_agent_coordinator/tests/ -v
```

## File structure

```
git_agent_coordinator/
  __init__.py        # public API
  models.py          # Agent, CoordinatorConfig, MergeCheckResult dataclasses
  registry.py        # FileRegistry — exclusive file ownership
  worktree.py        # git operations (create, rebase, merge, merge-tree)
  dag.py             # DependencyDAG + topological sort
  coordinator.py     # WorktreeCoordinator — the main loop
  example_usage.py   # full worked example with 4 agents
  tests/
    test_coordinator.py
```
