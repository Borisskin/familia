"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any, Protocol

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.utils.helpers import build_assistant_message, current_time_str, detect_image_mime
from nanobot.utils.prompt_templates import render_template


class ContextExtension(Protocol):
    """Adds system-prompt sections without coupling nanobot to an integration."""

    def build_sections(self, *, actor: str | None, channel: str | None) -> list[str]:
        """Return additional system-prompt sections for the current turn."""
        ...

    def build_runtime_sections(
        self,
        *,
        actor: str | None,
        channel: str | None,
        chat_id: str | None,
    ) -> list[str]:
        """Return additional runtime-context sections for the current turn."""
        ...

    def format_actor_label(self, actor: str | None) -> str:
        """Return a user-visible actor label, or empty string for default formatting."""
        ...


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    # USER.md is no longer part of BOOTSTRAP because standalone nanobot
    # loads it explicitly below and integrations can provide actor-specific
    # profile sections through ContextExtension.
    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_RECENT_HISTORY = 50
    _RUNTIME_CONTEXT_END = "[/Runtime Context]"

    def __init__(
        self,
        workspace: Path,
        timezone: str | None = None,
        disabled_skills: list[str] | None = None,
        context_extensions: list[ContextExtension] | None = None,
    ):
        self.workspace = workspace
        self.timezone = timezone
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace, disabled_skills=set(disabled_skills) if disabled_skills else None)
        self.context_extensions = list(context_extensions or [])

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        actor: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(channel=channel)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        # Conversation-continuity rules: short/pronoun-only follow-ups
        # should be interpreted as continuations of the prior assistant
        # turn rather than fresh topics. Lives in code (not in
        # user-editable SOUL.md) so it doesn't clutter the persona file
        # the operator sees in admin.
        parts.append(render_template("agent/conversation_rules.md"))

        # Memory-scope defaults: family-by-default for private records.
        # Peers can read each other's private:* unless tagged 'secret'.
        # Also code-level, not user-editable.
        parts.append(render_template("agent/scope_defaults.md"))

        # User-facing memory model: enables the assistant to answer
        # questions about access/visibility honestly and consistently.
        # Without this, the LLM falls back on the dictionary meaning of
        # "private" and contradicts the actual ACL behaviour.
        parts.append(render_template("agent/memory_model.md"))

        # Grocery shopping via VkusVill official MCP. The MCP server
        # itself is wired in nanobot-config.json (deployment-time); this
        # snippet teaches the LLM the proper flow: search with
        # vvonly=0, confirm cart preview with user, hand off payment
        # via cart_link_create rather than attempting checkout.
        parts.append(render_template("agent/shopping_vkusvill.md"))

        # Standalone nanobot keeps the legacy single-tenant USER/MEMORY
        # file behavior. Actor-specific context belongs to extensions.
        if actor is None:
            user_block = self._build_user_block()
            if user_block:
                parts.append(user_block)

            memory_block = self._build_memory_block()
            if memory_block:
                parts.append(memory_block)

        for extension in self.context_extensions:
            parts.extend(section for section in extension.build_sections(actor=actor, channel=channel) if section)

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        entries = self.memory.read_unprocessed_history(since_cursor=self.memory.get_last_dream_cursor())
        if entries:
            capped = entries[-self._MAX_RECENT_HISTORY:]
            parts.append("# Recent History\n\n" + "\n".join(
                f"- [{e['timestamp']}] {e['content']}" for e in capped
            ))

        return "\n\n---\n\n".join(parts)

    # ---- legacy standalone block builders ----------------------------------

    def _build_user_block(self) -> str:
        """Legacy standalone USER profile."""
        legacy = self.workspace / "USER.md"
        if legacy.exists():
            content = legacy.read_text(encoding="utf-8")
            if content.strip() and not self._is_template_content(content, "USER.md"):
                return f"## USER.md\n\n{content}"
        return ""

    def _build_memory_block(self) -> str:
        """Legacy standalone long-term MEMORY."""
        legacy_text: str | None = None
        legacy_raw = self.memory.read_memory()
        if legacy_raw and not self._is_template_content(legacy_raw, "memory/MEMORY.md"):
            # In nanobot upstream get_memory_context strips the
            # template-marker block; replicate that behavior by
            # using its return value when non-empty.
            ctx = self.memory.get_memory_context()
            if ctx and ctx.strip():
                legacy_text = ctx

        if legacy_text:
            return f"# Memory\n\n{legacy_text}"
        return ""

    def _get_identity(self, channel: str | None = None) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return render_template(
            "agent/identity.md",
            workspace_path=workspace_path,
            runtime=runtime,
            platform_policy=render_template("agent/platform_policy.md", system=system),
            channel=channel or "",
        )

    @staticmethod
    def _build_runtime_context(
        channel: str | None, chat_id: str | None, timezone: str | None = None,
        session_summary: str | None = None, runtime_sections: list[str] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        lines = [f"Current Time: {current_time_str(timezone)}"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if session_summary:
            lines += ["", "[Resumed Session]", session_summary]
        for section in runtime_sections or []:
            if section:
                lines += ["", section]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines) + "\n" + ContextBuilder._RUNTIME_CONTEXT_END

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in value]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _is_template_content(content: str, template_path: str) -> bool:
        """Check if *content* is identical to the bundled template (user hasn't customized it)."""
        try:
            tpl = pkg_files("nanobot") / "templates" / template_path
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return False

    def _build_runtime_extension_sections(
        self,
        *,
        actor: str | None,
        channel: str | None,
        chat_id: str | None,
    ) -> list[str]:
        sections: list[str] = []
        for extension in self.context_extensions:
            build_runtime_sections = getattr(extension, "build_runtime_sections", None)
            if callable(build_runtime_sections):
                sections.extend(
                    section
                    for section in build_runtime_sections(
                        actor=actor,
                        channel=channel,
                        chat_id=chat_id,
                    )
                    if section
                )
        return sections

    def _format_actor_label(self, actor: str | None) -> str:
        if not actor:
            return ""
        for extension in self.context_extensions:
            format_actor_label = getattr(extension, "format_actor_label", None)
            if callable(format_actor_label):
                label = format_actor_label(actor)
                if label:
                    return label
        return actor

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        current_role: str = "user",
        session_summary: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_sections = self._build_runtime_extension_sections(
            actor=actor,
            channel=channel,
            chat_id=chat_id,
        )
        runtime_ctx = self._build_runtime_context(
            channel, chat_id, self.timezone,
            session_summary=session_summary, runtime_sections=runtime_sections,
        )
        if actor and current_role == "user" and current_message:
            label = self._format_actor_label(actor)
            current_message = f"[{label}]: {current_message}"
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content
        messages = [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names, channel=channel, actor=actor,
                ),
            },
            *history,
        ]
        if messages[-1].get("role") == current_role:
            last = dict(messages[-1])
            last["content"] = self._merge_message_content(last.get("content"), merged)
            messages[-1] = last
            return messages
        messages.append({"role": current_role, "content": merged})
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
                "_meta": {"path": str(p)},
            })

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: Any,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(build_assistant_message(
            content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        ))
        return messages
