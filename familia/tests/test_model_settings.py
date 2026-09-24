"""Public model-management command scenarios."""

from __future__ import annotations

import json
from pathlib import Path


def test_agents_get_and_set_follow_active_named_profile(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from familia.cli import graph_admin

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "openai/direct-old",
                        "provider": "openai",
                        "modelPreset": "fast",
                        "fallback_models": ["safe"],
                        "unknownFamiliaField": {"keep": True},
                    }
                },
                "modelPresets": {
                    "fast": {
                        "model": "anthropic/fast",
                        "provider": "anthropic",
                        "max_tokens": 1234,
                        "context_window_tokens": 4321,
                        "temperature": 0.2,
                    },
                    "safe": {
                        "model": "openai/safe",
                        "provider": "openai",
                    },
                },
                "providers": {
                    "openai": {"api_key": "synthetic-key", "extra_body": {"keep": True}},
                    "anthropic": {"api_key": "synthetic-anthropic"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAMILIA_CONFIG_FILE", str(config_path))

    assert graph_admin.main(["agents", "get", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["main"]["model"] == "anthropic/fast"
    assert shown["main"]["provider"] == "anthropic"
    assert shown["main"]["source"] == "preset"
    assert shown["main"]["profile"] == "fast"
    assert shown["main"]["api_key"] == "***opic"
    assert shown["fallback"]["model"] == "openai/safe"

    assert graph_admin.main(
        [
            "agents",
            "set",
            "main",
            "--model",
            "openai/new",
            "--provider",
            "openai",
        ]
    ) == 0
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    defaults = raw["agents"]["defaults"]
    assert "modelPreset" not in defaults
    assert "model_preset" not in defaults
    assert defaults["model"] == "openai/new"
    assert defaults["max_tokens"] == 1234
    assert defaults["context_window_tokens"] == 4321
    assert defaults["temperature"] == 0.2
    assert defaults["fallback_models"] == ["safe"]
    assert defaults["unknownFamiliaField"] == {"keep": True}
    assert raw["modelPresets"]["fast"]["model"] == "anthropic/fast"
    assert raw["providers"]["openai"]["extra_body"] == {"keep": True}


def test_agents_get_uses_nanobot_provider_and_native_chain(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from familia.cli import graph_admin

    config_path = tmp_path / "config.json"
    original = {
        "agents": {
            "defaults": {
                "model": "deepseek-v4-pro",
                "provider": "auto",
                "fallback_models": [
                    {"model": "openai/first", "provider": "openai"},
                    {"model": "openai/second", "provider": "openai"},
                ],
            },
            "familia_fallback": {
                "model": "openai/legacy",
                "provider": "openai",
            },
        },
        "providers": {
            "novita": {"api_key": "novita-key"},
            "openai": {"api_key": "openai-key"},
        },
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("FAMILIA_CONFIG_FILE", str(config_path))

    assert graph_admin.main(["agents", "get", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["main"]["provider"] == "novita"
    assert shown["main"]["api_base"] == "https://api.novita.ai/openai"
    assert shown["fallback_source"] == "native"
    assert shown["fallback_chain"][1]["model"] == "openai/second"
    assert shown["legacy_fallback_key"] == "familia_fallback"
    assert shown["legacy_fallback"]["model"] == "openai/legacy"
    assert shown["legacy_fallback"]["source"] == "legacy"
    assert shown["legacy_fallback_conflict"] is True

    assert graph_admin.main(
        [
            "agents",
            "set",
            "main",
            "--model",
            "openai/new",
            "--provider",
            "openai",
        ]
    ) == 0
    after_main = json.loads(config_path.read_text(encoding="utf-8"))
    assert "familia_fallback" in after_main["agents"]
    assert after_main["agents"]["defaults"]["fallback_models"][1]["model"] == "openai/second"

    conflict = after_main
    conflict["providers"]["openai"]["extra_body"] = {"model": "openai/other"}
    config_path.write_text(json.dumps(conflict), encoding="utf-8")
    assert graph_admin.main(
        [
            "agents",
            "set",
            "main",
            "--model",
            "openai/newer",
            "--provider",
            "openai",
        ]
    ) == 2
    assert "extra_body.model" in capsys.readouterr().err
    assert json.loads(config_path.read_text(encoding="utf-8"))["agents"]["defaults"]["model"] == "openai/new"


def test_agents_get_preserves_codex_oauth_provider_fields(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from familia.cli import graph_admin

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "model": "openai-codex/gpt-5",
                        "provider": "auto",
                    }
                },
                "providers": {
                    "openai_codex": {
                        "proxy": "http://proxy",
                        "extra_body": {
                            "model": "openai-codex/gpt-5",
                            "keep": "oauth",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAMILIA_CONFIG_FILE", str(config_path))

    assert graph_admin.main(["agents", "get", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["main"]["provider"] == "openai_codex"
    assert shown["main"]["provider_model_override"] == "openai-codex/gpt-5"

    assert graph_admin.main(
        [
            "agents",
            "set",
            "main",
            "--model",
            "openai-codex/gpt-5",
            "--provider",
            "openai_codex",
        ]
    ) == 0
    assert json.loads(config_path.read_text(encoding="utf-8"))["providers"]["openai_codex"] == {
        "proxy": "http://proxy",
        "extra_body": {"model": "openai-codex/gpt-5", "keep": "oauth"},
    }
    capsys.readouterr()
