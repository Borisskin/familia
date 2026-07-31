from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]

PROMPT_PATHS = (
    REPO_ROOT / "familia/src/familia/templates/agent/scope_defaults.md",
    REPO_ROOT / "familia/src/familia/templates/agent/memory_model.md",
)

OPERATIONS_PATHS = (
    REPO_ROOT / "docs/operations.md",
    REPO_ROOT / "docs/en/operations.md",
)

BUILD_FROM_SOURCE_PATHS = (
    REPO_ROOT / "docs/build-from-source.md",
    REPO_ROOT / "docs/en/build-from-source.md",
)

DOCUMENTATION_PATHS = tuple(
    REPO_ROOT / relative
    for relative in (
        "README.md",
        "README.en.md",
        "docs/architecture.md",
        "docs/en/architecture.md",
        "docs/policy.md",
        "docs/en/policy.md",
        "docs/security.md",
        "docs/en/security.md",
        "docs/operations.md",
        "docs/en/operations.md",
        "docs/build-from-source.md",
        "docs/en/build-from-source.md",
        "docs/quickstart.md",
        "docs/en/quickstart.md",
    )
)

RU_EN_CONTRACT_PAIRS = (
    (
        REPO_ROOT / "README.md",
        REPO_ROOT / "README.en.md",
    ),
    (
        REPO_ROOT / "docs/architecture.md",
        REPO_ROOT / "docs/en/architecture.md",
    ),
    (
        REPO_ROOT / "docs/policy.md",
        REPO_ROOT / "docs/en/policy.md",
    ),
    (
        REPO_ROOT / "docs/security.md",
        REPO_ROOT / "docs/en/security.md",
    ),
)

MIGRATION_COMMAND = "familia migrate hybrid-storage --apply --json"


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _fenced_code_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if current is None:
            if marker:
                fence = marker.group(1)
                fence_char = fence[0]
                fence_length = len(fence)
                current = []
            continue
        closing = re.match(
            rf"^\s{{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$",
            line,
        )
        if closing:
            blocks.append("\n".join(current))
            current = None
            fence_char = ""
            fence_length = 0
        else:
            current.append(line)
    return blocks


def _without_fenced_code(text: str) -> str:
    kept: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if not fence_char:
            if marker:
                fence = marker.group(1)
                fence_char = fence[0]
                fence_length = len(fence)
            else:
                kept.append(line)
            continue
        if re.match(
            rf"^\s{{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$",
            line,
        ):
            fence_char = ""
            fence_length = 0
    return "\n".join(kept)


def _inline_link_targets(text: str) -> list[str]:
    targets: list[str] = []
    cursor = 0
    while True:
        start = text.find("](", cursor)
        if start < 0:
            return targets
        index = start + 2
        depth = 1
        escaped = False
        while index < len(text) and depth:
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        cursor = index
        if depth:
            continue
        raw = text[start + 2 : index - 1].strip()
        if not raw:
            continue
        if raw.startswith("<"):
            end = raw.find(">")
            if end > 0:
                targets.append(raw[1:end])
            continue
        targets.append(raw.split(maxsplit=1)[0])


def _reference_link_targets(text: str) -> list[str]:
    targets: list[str] = []
    pattern = re.compile(
        r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        targets.append(match.group(1) or match.group(2))
    return targets


def _github_slug_base(heading: str) -> str:
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "").replace("~", "")
    result: list[str] = []
    for char in text.strip().lower():
        category = unicodedata.category(char)
        if char.isspace():
            result.append("-")
        elif char in {"-", "_"} or category[0] in {"L", "M", "N"}:
            result.append(char)
    return "".join(result)


def _github_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    text = _without_fenced_code(path.read_text(encoding="utf-8"))
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        base = _github_slug_base(match.group(1))
        candidate = base
        duplicate = 0
        while candidate in anchors:
            duplicate += 1
            candidate = f"{base}-{duplicate}"
        anchors.add(candidate)
    return anchors


def _resolve_with_exact_case(target: Path) -> tuple[Path, list[str], str | None]:
    normalized = target.resolve(strict=False)
    relative = normalized.relative_to(REPO_ROOT)
    current = REPO_ROOT
    mismatches: list[str] = []
    for component in relative.parts:
        if not current.is_dir():
            return current / component, mismatches, component
        children = list(current.iterdir())
        exact = next((child for child in children if child.name == component), None)
        if exact is not None:
            current = exact
            continue
        folded = [
            child
            for child in children
            if child.name.casefold() == component.casefold()
        ]
        if len(folded) == 1:
            mismatches.append(f"{component} != {folded[0].name}")
            current = folded[0]
            continue
        return current / component, mismatches, component
    return current, mismatches, None


def test_executable_memory_prompts_use_simplified_contract() -> None:
    issues: list[str] = []
    combined: list[str] = []
    forbidden_patterns = (
        (
            "shared/pair scope",
            re.compile(
                r"(?im)(?:"
                r"scope\s*=\s*['\"](?:shared|pair)"
                r"|^\s*-\s*\*\*`?(?:shared|pair)(?=[`:*])"
                r")"
            ),
        ),
        (
            "physical pair key",
            re.compile(r"(?i)\bpair(?::[<A-Za-z0-9_.>-]+)+"),
        ),
        (
            "underscore pair namespace",
            re.compile(r"(?i)\b(?:sorted\s+)?underscore\s+`?pair`?"),
        ),
    )
    for path in PROMPT_PATHS:
        text = path.read_text(encoding="utf-8")
        combined.append(text)
        for label, pattern in forbidden_patterns:
            if pattern.search(text):
                issues.append(f"{_relative(path)}: forbidden {label}")
        for literal in ("value:memory", "ArchiveResult"):
            if literal in text:
                issues.append(f"{_relative(path)}: forbidden {literal}")

    prompt_text = "\n".join(combined)
    required_patterns = (
        ("memory:<fact_id>", re.compile(r"memory:<fact_id>")),
        ("fact_id", re.compile(r"\bfact_id\b")),
        ("ts", re.compile(r"\bts\b")),
        ("topic_id", re.compile(r"\btopic_id\b")),
        ("delete", re.compile(r"\bdelete\b", re.IGNORECASE)),
    )
    for label, pattern in required_patterns:
        if not pattern.search(prompt_text):
            issues.append(f"executable prompts: missing {label}")

    assert not issues, "\n".join(issues)


def test_full_assembled_prompt_declares_catalog_server_revision_and_safe_retry(
    tmp_path: Path,
) -> None:
    from familia.nanobot_extension.context import FamiliaContextExtension
    from nanobot.agent.context import ContextBuilder

    prompt = ContextBuilder(
        tmp_path,
        context_extensions=[FamiliaContextExtension(tmp_path)],
    ).build_system_prompt(
        actor=None,
        channel="documentation-contract",
    )

    for literal in (
        "private:<principal>:value:user_profile",
        "private:<principal>:value:private_index",
        "memory:<fact_id>",
        "256",
    ):
        assert literal in prompt
    assert re.search(
        r"(?is)(?:server|memx).{0,120}(?:assigns|returns|sets).{0,80}`?ts`?",
        prompt,
    )
    assert re.search(
        r"(?is)(?:do not|never).{0,80}(?:send|supply|invent).{0,40}`?ts`?",
        prompt,
    )
    assert re.search(
        r"(?is)(?:failed|unconfirmed|retryable).{0,180}"
        r"(?:keep|preserve|retain).{0,80}(?:messages|source)",
        prompt,
    )
    for forbidden in (
        "family_legacy_untagged",
        "shared_family_relation",
        "pair_member",
        "ArchiveResult",
        "Peers' private keys",
        "Family members' shared keys",
    ):
        assert forbidden not in prompt


def test_policy_graph_examples_are_valid_typed_principal_graphs() -> None:
    from familia.acl.schema import Graph

    for path in (
        REPO_ROOT / "docs/policy.md",
        REPO_ROOT / "docs/en/policy.md",
    ):
        graph_examples = []
        for block in _fenced_code_blocks(path.read_text(encoding="utf-8")):
            try:
                value = json.loads(block)
            except ValueError:
                continue
            if isinstance(value, dict) and {"nodes", "edges"} <= value.keys():
                graph_examples.append(value)

        assert graph_examples, _relative(path)
        for graph in graph_examples:
            assert all(
                node.get("type") == "principal"
                for node in graph["nodes"]
            )
            assert Graph.from_dict(graph) is not None
        assert any(
            any(edge.get("rel") == "spouse_of" for edge in graph["edges"])
            for graph in graph_examples
        ), f"{_relative(path)}: missing principal relation example"


def test_ru_en_memory_contract_documents_keep_same_machine_facts() -> None:
    machine_literals = (
        "private:<principal>:value:user_profile",
        "private:<principal>:value:private_index",
        "private:<principal>:memory:<fact_id>",
        "memory:<fact_id>",
        "256",
        "catalog_full",
    )
    obsolete_literals = (
        "family_legacy_untagged",
        "shared_family_relation",
        "pair_member",
        "value:shared_index",
    )

    for ru_path, en_path in RU_EN_CONTRACT_PAIRS:
        ru = ru_path.read_text(encoding="utf-8")
        en = en_path.read_text(encoding="utf-8")
        for literal in machine_literals:
            assert (literal in ru) == (literal in en), (
                f"{literal}: {_relative(ru_path)} != {_relative(en_path)}"
            )
        for literal in obsolete_literals:
            assert literal not in ru, f"{_relative(ru_path)}: {literal}"
            assert literal not in en, f"{_relative(en_path)}: {literal}"

    combined_ru = "\n".join(
        ru.read_text(encoding="utf-8") for ru, _en in RU_EN_CONTRACT_PAIRS
    )
    combined_en = "\n".join(
        en.read_text(encoding="utf-8") for _ru, en in RU_EN_CONTRACT_PAIRS
    )
    for literal in machine_literals:
        assert literal in combined_ru
        assert literal in combined_en
    assert re.search(
        r"(?is)(?:ошиб|не подтвержд).{0,160}"
        r"(?:сообщен|исходн).{0,80}(?:сохран|остаю)",
        combined_ru,
    )
    assert re.search(
        r"(?is)(?:fail|unconfirm).{0,160}"
        r"(?:message|source).{0,80}(?:keep|preserv|retain|remain)",
        combined_en,
    )


def test_ru_en_docs_require_relation_and_topic_and_deny_guardian_shortcut() -> None:
    combined_ru = "\n\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            REPO_ROOT / "README.md",
            REPO_ROOT / "docs/architecture.md",
            REPO_ROOT / "docs/policy.md",
            REPO_ROOT / "docs/security.md",
        )
    )
    combined_en = "\n\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            REPO_ROOT / "README.en.md",
            REPO_ROOT / "docs/en/architecture.md",
            REPO_ROOT / "docs/en/policy.md",
            REPO_ROOT / "docs/en/security.md",
        )
    )

    paired_invariants = (
        (
            combined_ru,
            (
                r"(?is)spouse_of.{0,500}(?:топик|тем|topic_id)"
                r".{0,300}memory:<fact_id>",
                r"(?is)guardian_of.{0,500}(?:без|нет).{0,100}"
                r"(?:топик|тем).{0,160}(?:запрещ|не (?:да[её]т|получ))",
                r"(?is)(?:сервер|memx).{0,100}(?:созда|назнач|возвращ).{0,60}`?ts`?"
                r".{0,180}(?:модель|клиент).{0,80}не.{0,60}(?:переда|назнач|созда)",
                r"(?is)(?:ошиб|не подтвержд).{0,180}(?:сообщен|исходн)"
                r".{0,100}(?:сохран|остаю)",
            ),
        ),
        (
            combined_en,
            (
                r"(?is)spouse_of.{0,500}(?:topic|topic_id)"
                r".{0,300}memory:<fact_id>",
                r"(?is)guardian_of.{0,500}(?:without|no).{0,100}topic"
                r".{0,160}(?:deny|forbid|does not (?:grant|allow))",
                r"(?is)(?:server|memx).{0,100}(?:creates|assigns|returns|sets)"
                r".{0,60}`?ts`?.{0,180}(?:model|client)"
                r".{0,80}(?:does not|do not|must not|never).{0,60}(?:send|supply|assign|create)",
                r"(?is)(?:fail|unconfirm).{0,180}(?:message|source)"
                r".{0,100}(?:keep|preserv|retain|remain)",
            ),
        ),
    )
    for text, patterns in paired_invariants:
        for pattern in patterns:
            assert re.search(pattern, text), pattern


def test_operations_and_update_docs_use_current_migration_contract() -> None:
    issues: list[str] = []
    old_unique_status = re.compile(
        r"(?<![\w-])(?:"
        r"ready_with_warnings|success_with_warnings|needs_review"
        r")(?![\w-])",
        re.IGNORECASE,
    )
    old_machine_status = re.compile(
        r"(?:"
        r"(?:status|code)\s*(?:=|:)\s*[`'\"]?"
        r"(?:ready|success|fatal)(?![\w-])"
        r"|[`'\"](?:ready|success|fatal)[`'\"]"
        r")",
        re.IGNORECASE,
    )
    forbidden_commands = (
        ("manual tar czf", re.compile(r"\btar\s+-?czf\b", re.IGNORECASE)),
        ("manual tar xzf", re.compile(r"\btar\s+-?xzf\b", re.IGNORECASE)),
        (
            "manual docker run memx_data",
            re.compile(
                r"\bdocker\s+run\b[\s\S]*?\bmemx_data\b",
                re.IGNORECASE,
            ),
        ),
        (
            "manual docker compose down",
            re.compile(r"\bdocker\s+compose\s+down\b", re.IGNORECASE),
        ),
    )

    for path in OPERATIONS_PATHS:
        text = path.read_text(encoding="utf-8")
        for literal in ("familia-backup-manifest.json", MIGRATION_COMMAND):
            if literal not in text:
                issues.append(f"{_relative(path)}: missing {literal}")
        for status in ("complete", "partial", "failed"):
            if not re.search(
                rf"(?<![\w-]){re.escape(status)}(?![\w-])",
                text,
                re.IGNORECASE,
            ):
                issues.append(f"{_relative(path)}: missing {status}")
        if old_unique_status.search(text) or old_machine_status.search(text):
            issues.append(f"{_relative(path)}: contains an obsolete status")
        for block_number, fenced in enumerate(_fenced_code_blocks(text), start=1):
            for label, pattern in forbidden_commands:
                if pattern.search(fenced):
                    issues.append(
                        f"{_relative(path)}: fenced block {block_number} "
                        f"forbids {label}"
                    )

    for path in BUILD_FROM_SOURCE_PATHS:
        text = path.read_text(encoding="utf-8")
        for literal in ("SOURCE_VERSION", MIGRATION_COMMAND):
            if literal not in text:
                issues.append(f"{_relative(path)}: missing {literal}")

    assert not issues, "\n".join(issues)


def test_documentation_local_links_and_anchors_resolve() -> None:
    issues: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    for source in DOCUMENTATION_PATHS:
        if not source.is_file():
            issues.append(f"{_relative(source)}: documentation file is missing")
            continue
        text = _without_fenced_code(source.read_text(encoding="utf-8"))
        targets = _inline_link_targets(text) + _reference_link_targets(text)
        for raw_target in targets:
            parsed = urlsplit(raw_target)
            if parsed.scheme.lower() in {"http", "https", "mailto"}:
                continue
            relative_path = unquote(parsed.path)
            if relative_path.replace("\\", "/").rstrip("/").endswith(
                "releases/latest"
            ):
                continue
            target = source if not relative_path else source.parent / relative_path
            normalized = target.resolve(strict=False)
            try:
                normalized.relative_to(REPO_ROOT)
            except ValueError:
                continue
            resolved, case_mismatches, missing_component = _resolve_with_exact_case(
                target
            )
            if case_mismatches:
                issues.append(
                    f"{_relative(source)}: local target {raw_target} has wrong case "
                    f"({', '.join(case_mismatches)})"
                )
            if missing_component is not None or not resolved.exists():
                issues.append(
                    f"{_relative(source)}: missing local target {raw_target}"
                )
                continue
            fragment = unquote(parsed.fragment)
            if not fragment or resolved.suffix.lower() not in {"", ".md", ".markdown"}:
                continue
            if not resolved.is_file():
                issues.append(
                    f"{_relative(source)}: anchor target is not a file {raw_target}"
                )
                continue
            if resolved not in anchor_cache:
                anchor_cache[resolved] = _github_anchors(resolved)
            anchors = anchor_cache[resolved]
            if fragment not in anchors:
                issues.append(
                    f"{_relative(source)}: missing anchor #{fragment} "
                    f"in {_relative(resolved)}"
                )

    assert not issues, "\n".join(issues)
