from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_PATH = (
    REPO_ROOT / "familia/src/familia/nanobot_extension/context.py"
)
MEMORY_PATH = REPO_ROOT / "familia/src/familia/tools/memory.py"

STALE_PEER_USER_ATTRIBUTES = frozenset(
    {
        "_build_peer_user_block",
        "_audit_peer_user",
        "_sanitize_untrusted_block",
        "_PEER_USER_MAX_BYTES",
        "_PEER_USER_TAG",
        "_PEER_USER_END",
    }
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _declared_names(nodes: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            names.add(node.target.id)
    return names


class RemovedMemoryDescriptionsTests(unittest.TestCase):
    def test_peer_user_contract_attributes_are_absent(self) -> None:
        tree = _parse(CONTEXT_PATH)
        context_classes = [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "FamiliaContextExtension"
        ]
        self.assertEqual(
            len(context_classes),
            1,
            "expected exactly one FamiliaContextExtension class declaration",
        )

        found = sorted(
            STALE_PEER_USER_ATTRIBUTES
            & _declared_names(context_classes[0].body)
        )
        self.assertEqual(
            found,
            [],
            "stale peer USER attributes still declared: "
            + ", ".join(found),
        )

    def test_tags_description_is_absent(self) -> None:
        tree = _parse(MEMORY_PATH)
        found = sorted({"_TAGS_DESC"} & _declared_names(tree.body))
        self.assertEqual(
            found,
            [],
            "stale memory module declarations still present: "
            + ", ".join(found),
        )


if __name__ == "__main__":
    unittest.main()
