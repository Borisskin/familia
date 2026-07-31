import ast
from pathlib import Path


def _is_familia_import(module: str) -> bool:
    return module == "familia" or module.startswith("familia.")


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


def test_nanobot_core_does_not_import_familia_package() -> None:
    root = Path(__file__).resolve().parents[3]
    core_root = root / "nanobot" / "nanobot"

    offenders: list[str] = []
    for path in sorted(core_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if _is_familia_import(module):
                    names = ", ".join(alias.name for alias in node.names)
                    offenders.append(f"{rel}:{node.lineno}:from {module} import {names}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_familia_import(alias.name):
                        offenders.append(f"{rel}:{node.lineno}:import {alias.name}")

    assert offenders == []


def test_product_prompt_templates_are_not_owned_by_nanobot_core() -> None:
    root = Path(__file__).resolve().parents[3]
    context_path = root / "nanobot" / "nanobot" / "agent" / "context.py"
    context_source = context_path.read_text(encoding="utf-8")
    product_templates = [
        "agent/scope_defaults.md",
        "agent/memory_model.md",
        "agent/shopping_vkusvill.md",
    ]

    offenders = [
        f"{context_path.relative_to(root).as_posix()}:render_template({template!r})"
        for template in product_templates
        if template in context_source
    ]
    for template in product_templates:
        template_path = root / "nanobot" / "nanobot" / "templates" / template
        if template_path.exists():
            offenders.append(template_path.relative_to(root).as_posix())

    assert offenders == []


def test_nanobot_tests_do_not_import_familia_package() -> None:
    root = Path(__file__).resolve().parents[3]
    tests_root = root / "nanobot" / "tests"

    offenders: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if _is_familia_import(module):
                    offenders.append(f"{rel}:{node.lineno}:from {module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_familia_import(alias.name):
                        offenders.append(f"{rel}:{node.lineno}:import {alias.name}")

    assert offenders == []


def test_dream_and_heartbeat_do_not_import_concrete_familia_modules() -> None:
    root = Path(__file__).resolve().parents[3]
    paths = [
        root / "nanobot" / "nanobot" / "agent" / "memory.py",
        root / "nanobot" / "nanobot" / "heartbeat" / "service.py",
    ]

    offenders: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if _is_familia_import(module):
                    offenders.append(f"{rel}:{node.lineno}:from {module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_familia_import(alias.name):
                        offenders.append(f"{rel}:{node.lineno}:import {alias.name}")

    assert offenders == []
