"""Familia context extension for nanobot prompts."""

from __future__ import annotations

from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Any

from nanobot.agent.context import ContextBuilder


class FamiliaContextExtension:
    """Build actor-specific prompt sections for familia-backed nanobot turns."""

    # Sentinel returned by ``_principal_client`` when familia exists but
    # actor-specific memory access cannot be constructed. This is distinct
    # from ``None``: ``None`` means there is no actor, while this sentinel
    # means we must fail closed and avoid leaking single-tenant files.
    _CLIENT_FAILED = object()
    # Cap projected atomic names per foreign owner. Catalog order is oldest
    # first, so projection walks it backwards and keeps the newest names.
    _PEER_INDEX_MAX_KEYS_PER_PEER = 40
    _SYSTEM_TEMPLATE_FILES = (
        "agent/scope_defaults.md",
        "agent/memory_model.md",
        "agent/shopping_vkusvill.md",
    )

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)

    def build_sections(self, *, actor: str | None, channel: str | None) -> list[str]:
        """Return familia system-prompt sections for the current actor."""
        del channel
        peer_client = self._principal_client(actor)
        graph_snapshot = self._load_graph_snapshot(peer_client)
        # Order matters. Product policy templates come first, then own
        # USER/MEMORY, the atomic catalog, and authorized foreign names.
        sections = [
            *self._build_system_template_sections(),
            self._build_user_block(actor),
            self._build_memory_block(actor),
            self._build_key_index_block(
                actor,
                suffix="value:private_index",
                heading="Private keys you've written",
                scope_label="private",
            ),
            self._build_peer_memory_projection_block(
                actor,
                client=peer_client,
                graphs=graph_snapshot,
            ),
        ]
        return [section for section in sections if section]

    def _build_system_template_sections(self) -> list[str]:
        """Return familia-owned prompt policy sections.

        Memory-scope defaults and the user-facing memory model describe the
        family graph ACL. VkusVill shopping flow is product-specific MCP
        behavior. These sections live here so nanobot core stays neutral.
        """
        sections: list[str] = []
        for template in self._SYSTEM_TEMPLATE_FILES:
            text = self._render_template(template)
            if text:
                sections.append(text)
        return sections

    @staticmethod
    def _render_template(name: str) -> str:
        template = pkg_files("familia") / "templates" / name
        if not template.is_file():
            return ""
        return template.read_text(encoding="utf-8").rstrip()

    def build_runtime_sections(
        self,
        *,
        actor: str | None,
        channel: str | None,
        chat_id: str | None,
    ) -> list[str]:
        """Return familia runtime-context sections for the current actor."""
        del channel, chat_id
        if not actor:
            return []
        try:
            from familia import bootstrap as fb
        except ImportError:
            return []
        try:
            # ACL vocabulary is runtime context, not system prompt proper:
            # graph etags can change between turns, so this must be rebuilt
            # per call instead of being hidden inside a cache-friendly system
            # section.
            acl_block = fb.build_vocabulary_for(actor) or ""
        except Exception:  # noqa: BLE001
            return []
        return [acl_block] if acl_block else []

    def format_actor_label(self, actor: str | None) -> str:
        """Return the display name for a principal id."""
        if not actor:
            return ""
        try:
            from familia.principals import actor_display
        except ImportError:
            return actor
        try:
            return actor_display(actor) or actor
        except Exception:  # noqa: BLE001
            return actor

    def _principal_client(self, actor: str | None) -> Any:
        """Return a PrincipalMemoryClient for actor-specific memX access.

        Return values:
        - PrincipalMemoryClient: success, ready to read/write memX.
        - None: no actor for this turn.
        - _CLIENT_FAILED: actor exists but registry/key/client setup failed.

        The last case is intentionally fail-closed. Falling back to legacy
        workspace USER/MEMORY for a known actor would leak the owner's
        single-tenant files into another principal's prompt.
        """
        if not actor:
            return None
        try:
            from familia.acl.principal_memory import PrincipalMemoryClient
            from familia.principals import get_registry
        except ImportError:
            return self._CLIENT_FAILED
        try:
            principal = get_registry().get(actor)
        except Exception:  # noqa: BLE001
            return self._CLIENT_FAILED
        if principal is None or not principal.memx_key:
            return self._CLIENT_FAILED
        try:
            return PrincipalMemoryClient(actor, principal.memx_key)
        except Exception:  # noqa: BLE001
            return self._CLIENT_FAILED

    def _load_graph_snapshot(
        self,
        client: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Return one fail-closed raw graph snapshot for this build."""
        if client is None or client is self._CLIENT_FAILED:
            return None
        try:
            return client._load_graph_snapshot()
        except Exception:  # noqa: BLE001
            return None

    def _build_user_block(self, actor: str | None) -> str:
        """Own USER profile from ``private:<actor>:value:user_profile``.

        Missing content returns an empty block. Familia actors must not fall
        back to workspace/USER.md because that file belongs to standalone
        nanobot and may describe a different person.
        """
        client = self._principal_client(actor)
        if client is None or client is self._CLIENT_FAILED:
            return ""
        text = client.get("value:user_profile")
        if text and text.strip():
            return f"## USER (you, {actor})\n\n{text}"
        return ""

    def _build_memory_block(self, actor: str | None) -> str:
        """Own long-term memory from ``private:<actor>:value:memory``.

        Actor-specific memory is loaded only from memX. If the actor client is
        unavailable, the safe result is no block, not legacy file fallback.
        """
        client = self._principal_client(actor)
        if client is None or client is self._CLIENT_FAILED:
            return ""
        memx_text = client.get("value:memory")
        if memx_text:
            memx_text = memx_text.strip()
            if memx_text:
                return f"# Memory\n\n{memx_text}"
        return ""

    def _build_key_index_block(
        self,
        actor: str | None,
        *,
        suffix: str,
        heading: str,
        scope_label: str,
    ) -> str:
        """Render strict atomic names from the current actor's catalog."""
        if suffix != "value:private_index" or scope_label != "private":
            return ""
        client = self._principal_client(actor)
        if client is None or client is self._CLIENT_FAILED:
            return ""
        raw = client.get(suffix)
        if raw is None:
            return ""
        try:
            from familia.acl.principal_memory import (
                _decode_atomic_memory_catalog,
            )
        except ImportError:
            return ""
        entries = _decode_atomic_memory_catalog(raw)
        if entries is None:
            return ""
        names = [name for name, _tags in reversed(entries)]
        if not names:
            return ""
        bullet_list = "\n".join(f"- {name}" for name in names)
        return (
            f"# {heading}\n\n"
            "Atomic private memory names stored in your catalog. "
            "To read one, call "
            f"``memory_get`` with ``scope='{scope_label}'`` and the "
            "bare key name. Newest first.\n\n"
            f"{bullet_list}"
        )

    def _build_peer_memory_projection_block(
        self,
        actor: str | None,
        *,
        client: Any,
        graphs: tuple[dict[str, Any], dict[str, Any]] | None,
    ) -> str:
        """Render only authorized foreign atomic names, never values."""
        if (
            client is None
            or client is self._CLIENT_FAILED
            or graphs is None
        ):
            return ""
        try:
            from familia.principals import get_registry
        except ImportError:
            return ""

        try:
            registry = get_registry()
            principal_ids = sorted(registry.ids)
        except Exception:  # noqa: BLE001
            return ""

        sections: list[str] = []
        for pid in principal_ids:
            if pid == actor:
                continue
            try:
                names = client.project_other_memory_names(
                    pid,
                    graphs=graphs,
                    limit=self._PEER_INDEX_MAX_KEYS_PER_PEER,
                )
                peer_principal = registry.get(pid)
            except Exception:  # noqa: BLE001
                return ""
            if not names:
                continue
            display = (
                peer_principal.display_name
                if peer_principal and peer_principal.display_name
                else pid
            )
            bullets = "\n".join(f"- {name}" for name in names)
            sections.append(f"## {pid} ({display})\n{bullets}")

        if not sections:
            return ""

        intro = (
            "Authorized atomic memory names from other principals. Read one "
            "with ``memory_get(scope='private', actor='<their_id>', "
            "key='<memory:name>')``. Values and raw catalogs are not projected."
        )
        return "# Family memory facts\n\n" + intro + "\n\n" + "\n\n".join(sections)


class FamiliaContextBuilder(ContextBuilder):
    """Nanobot context builder that never reads shared USER/MEMORY/history.

    The target builder owns message assembly and skills; this product-owned
    subclass replaces only system-prompt construction.  The active actor is
    taken from the turn ContextVar, which the adapter binds before prompting.
    """

    def __init__(
        self,
        workspace: str | Path,
        timezone: str | None = None,
        disabled_skills: list[str] | None = None,
    ) -> None:
        self._disabled_skills = set(disabled_skills or ())
        super().__init__(
            Path(workspace),
            timezone=timezone,
            disabled_skills=disabled_skills,
        )
        self._extension = FamiliaContextExtension(self.workspace)

    def _load_shared_system_files(self, workspace: Path) -> str:
        """Read only project instructions/personality, never USER.md."""
        from nanobot.utils.helpers import load_bundled_template

        parts: list[str] = []
        for filename, root in (("AGENTS.md", workspace), ("SOUL.md", self.workspace)):
            path = root / filename
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            if filename == "SOUL.md" and self._is_template_content(
                content,
                "legacy/SOUL.md",
            ):
                content = load_bundled_template("SOUL.md") or content
            if not content.strip():
                continue
            parts.append(f"## {filename}\n\n{content}")
        return "\n\n".join(parts)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        channel: str | None = None,
        session_summary: str | None = None,
        workspace: Path | None = None,
        include_memory_recent_history: bool = True,
        session_key: str | None = None,
        unified_session: bool = False,
    ) -> str:
        # Familia archives only to memX; target session summaries are derived
        # from file-backed history and are never a prompt source here.
        del include_memory_recent_history, session_key, unified_session, session_summary
        from nanobot.utils.prompt_templates import render_template

        from familia.principals import get_current_actor

        root = workspace or self.workspace
        actor = get_current_actor()
        parts = [self._get_identity(channel=channel, workspace=root)]
        shared = self._load_shared_system_files(root)
        if shared:
            parts.append(shared)

        # Familia templates and actor-owned memX sections are the only profile
        # and memory source in adapter mode.
        parts.extend(self._extension.build_sections(actor=actor, channel=channel))
        parts.append(render_template("agent/tool_contract.md"))

        # ``self.skills`` is created at loop construction for the central
        # workspace.  Rebuild this read-only loader from the server-bound
        # actor root so workspace-level skills cannot cross principals; the
        # loader still exposes packaged skills as public capabilities.
        from nanobot.agent.skills import SkillsLoader

        skills = SkillsLoader(root, disabled_skills=self._disabled_skills)
        always_skills = skills.get_always_skills()
        if always_skills:
            always_content = skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")
        skills_summary = skills.build_skills_summary(exclude=set(always_skills))
        if skills_summary:
            parts.append(render_template("agent/skills_section.md", skills_summary=skills_summary))

        return "\n\n---\n\n".join(part for part in parts if part)

    def build_messages(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Delegate message assembly while retaining the safe prompt override."""
        from familia.principals import get_current_actor

        actor = get_current_actor()
        history = kwargs.get("history")
        if isinstance(history, list):
            filtered: list[dict[str, Any]] = []
            for message in history:
                if not isinstance(message, dict):
                    continue
                metadata = message.get("metadata")
                tagged_actor = message.get("actor")
                if isinstance(metadata, dict):
                    tagged_actor = metadata.get("actor", tagged_actor)
                if tagged_actor not in (None, actor):
                    continue
                filtered.append(message)
            kwargs["history"] = filtered
        return super().build_messages(*args, **kwargs)
