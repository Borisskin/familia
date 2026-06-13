import ast
from pathlib import Path


def test_cli_commands_do_not_import_concrete_familia_modules() -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "nanobot" / "nanobot" / "cli" / "commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("familia."):
                offenders.append(f"{node.lineno}:from {module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("familia."):
                    offenders.append(f"{node.lineno}:import {alias.name}")

    assert offenders == []
