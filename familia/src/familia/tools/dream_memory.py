"""Dream-only operation adapter for Familia's automatic memory ingestor."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


CONSOLIDATOR_ACTOR = "dream_consolidator"


@tool_parameters(
    tool_parameters_schema(
        kind=StringSchema(
            "Automatic operation kind: 'profile', 'memory', or 'delete'."
        ),
        fact_id=StringSchema(
            "Stable fact id required for 'memory' and 'delete'.",
            nullable=True,
        ),
        value=StringSchema(
            "Exact profile or atomic memory-fact value.",
            nullable=True,
        ),
        required=["kind"],
    )
    | {"additionalProperties": False}
)
class DreamMemorySetTool(Tool):
    """Pass one Dream operation to Familia's injected automatic writer."""

    def __init__(
        self,
        *,
        ingestor: Any | None = None,
        server_principal_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._ingestor = ingestor
        self._server_principal_getter = server_principal_getter

    @property
    def name(self) -> str:
        return "dream_memory_set"

    @property
    def description(self) -> str:
        return (
            "Submit a profile update, one atomic memory fact, or deletion of one "
            "exact fact. The private owner comes only from Familia's server context."
        )

    async def execute(
        self,
        kind: str,
        fact_id: str | None = None,
        value: str | None = None,
        **extra: Any,
    ) -> str:
        from familia.principals import get_current_actor

        current = get_current_actor()
        if current != CONSOLIDATOR_ACTOR:
            return (
                f"Error: dream_memory_set is only callable from the Dream "
                f"consolidator turn (current actor={current!r})"
            )
        if extra:
            return "Error: dream_memory_set received unsupported operation fields"
        if self._ingestor is None or not callable(getattr(self._ingestor, "ingest", None)):
            return "Error: Dream automatic memory ingestor is not configured"
        if not callable(self._server_principal_getter):
            return "Error: Dream trusted server principal is not configured"
        try:
            server_principal = self._server_principal_getter()
        except Exception as exc:
            return f"Error: Dream trusted server principal failed ({type(exc).__name__})"
        if not isinstance(server_principal, str) or not server_principal:
            return "Error: denied_invalid: Dream private owner is unavailable"

        operation: dict[str, Any] = {"kind": kind}
        if kind == "delete":
            if value is not None:
                return "Error: denied_invalid: delete operation does not accept value"
            operation["fact_id"] = fact_id
        else:
            operation["value"] = value
            if fact_id is not None:
                operation["fact_id"] = fact_id
        result = await self._ingestor.ingest(
            server_principal=server_principal,
            server_topic=None,
            operation=operation,
        )
        if isinstance(result, str) and result.startswith(
            ("committed:", "deleted:", "absent:")
        ):
            return result
        if isinstance(result, str) and result:
            return f"Error: {result}"
        return "Error: Dream automatic memory ingestor returned an invalid result"
