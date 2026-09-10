"""Slash command package kept for upstream compatibility.

Runtime command registration is intentionally not re-exported from this
package. Integrations must not wire ``CommandRouter`` into message
handling: slash-looking inbound text is ordinary user text, not a
privileged control plane.
"""

# from nanobot.command.builtin import register_builtin_commands
# from nanobot.command.router import CommandContext, CommandRouter

__all__: list[str] = []
