"""Installs and checks the agent skills a consuming repo pins in its `.agent-skills.lock`."""

__all__ = ["AgentSkillsError"]


class AgentSkillsError(Exception):
    """A failure reported to the user as `error: <message>` with exit code 1."""
