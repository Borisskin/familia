"""Agent core module.

The public names remain importable from :mod:`nanobot.agent`, but their
implementations load lazily.  Eagerly importing the loop here creates a cycle
when ``nanobot.bus.queue`` imports an agent type while channel discovery is
still importing the bus package.
"""

from importlib import import_module
from typing import Any


_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentHook": ("nanobot.agent.hook", "AgentHook"),
    "AgentHookContext": ("nanobot.agent.hook", "AgentHookContext"),
    "AgentRunHookContext": ("nanobot.agent.hook", "AgentRunHookContext"),
    "AgentTurnHookContext": ("nanobot.agent.hook", "AgentTurnHookContext"),
    "AgentTurnHookFactory": ("nanobot.agent.hook", "AgentTurnHookFactory"),
    "CompositeHook": ("nanobot.agent.hook", "CompositeHook"),
    "AgentLoop": ("nanobot.agent.loop", "AgentLoop"),
    "ContextBuilder": ("nanobot.agent.context", "ContextBuilder"),
    "MemoryStore": ("nanobot.agent.memory", "MemoryStore"),
    "SkillsLoader": ("nanobot.agent.skills", "SkillsLoader"),
    "SubagentManager": ("nanobot.agent.subagent", "SubagentManager"),
}

__all__ = [
    "AgentHook",
    "AgentHookContext",
    "AgentRunHookContext",
    "AgentTurnHookContext",
    "AgentTurnHookFactory",
    "AgentLoop",
    "CompositeHook",
    "ContextBuilder",
    "MemoryStore",
    "SkillsLoader",
    "SubagentManager",
]


def __getattr__(name: str) -> Any:
    """Resolve one compatibility export without importing the whole agent."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
