"""Focused contract for IDs assigned to newly-created principals."""

from __future__ import annotations

import json

import pytest

from familia import principals


def test_new_principal_id_is_trimmed_without_rewriting_underscores() -> None:
    assert principals.normalize_new_principal_id("  family_42  ") == "family_42"


@pytest.mark.parametrize(
    "raw_id",
    ["Family_42", "42_family", "family-member", "family member", "семья", "family.dot", "_"],
)
def test_new_principal_id_rejects_values_outside_lowercase_underscore_contract(
    raw_id: str,
) -> None:
    with pytest.raises(ValueError, match="start with a-z"):
        principals.normalize_new_principal_id(raw_id)


def test_new_principal_id_reports_collision_after_normalization() -> None:
    with pytest.raises(ValueError, match=r"family_42.*collides.*family_42"):
        principals.normalize_new_principal_id(
            "family_42",
            existing_ids={"family_42"},
        )


def test_only_actual_pair_namespace_collisions_are_reported() -> None:
    assert principals.ambiguous_pair_namespaces(["owner", "member_one"]) == {}
    assert principals.ambiguous_pair_namespaces(
        ["a", "a_b", "b_c", "c"]
    ) == {
        "a_b_c": (("a", "b_c"), ("a_b", "c")),
    }


def test_loading_registry_preserves_legacy_underscore_id(tmp_path) -> None:
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "principals": [
                    {
                        "id": "legacy_actor",
                        "display_name": "Legacy Actor",
                        "memx_key": "legacy-key",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    registry = principals.load_registry(path)

    assert registry.get("legacy_actor") is not None
    assert registry.get("legacy-actor") is None
