"""Remote Claude behavior — same shape as remote_codex but for claude CLI.

The bridge serves the claude-remote browser site, manages a registry of
PCs running the claude-executor agent, and brokers commands (run.start /
run.input / run.interrupt / session.delete / fs.list / fs.mkdir) between
the browser and the agent.
"""

from .service import RemoteClaudeBehaviorService

__all__ = ["RemoteClaudeBehaviorService"]
