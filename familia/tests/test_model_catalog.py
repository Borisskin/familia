from __future__ import annotations

import json
from types import SimpleNamespace

from familia.cli import model_catalog


def _spec(name: str, **kwargs):
    values = dict(
        name=name,
        keywords=(),
        env_key=f"{name.upper()}_KEY",
        display_name=name.title(),
        model_catalog="auto",
        default_api_base="https://example.test/v1",
        is_direct=False,
        is_oauth=False,
        is_transcription_only=False,
        settings_alias_for="",
        strip_model_prefix=False,
        strip_model_prefixes=(),
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_list_providers_uses_registry_without_network(monkeypatch):
    monkeypatch.setattr(model_catalog, "_provider_specs", lambda: (_spec("demo"),))
    monkeypatch.setattr(
        model_catalog,
        "_transcription_specs",
        lambda: (SimpleNamespace(name="demo-stt", default_model="speech-1"),),
    )
    monkeypatch.setattr(model_catalog.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))

    result = model_catalog.list_providers({"providers": {}}, "chat")

    assert [item.key for item in result] == ["demo"]
    assert result[0].catalog_mode == "remote"


def test_catalog_cache_identity_purpose_and_stale_error(monkeypatch):
    monkeypatch.setattr(model_catalog, "_provider_specs", lambda: (_spec("demo"),))
    cache = {"version": 2, "entries": {}}
    now = [1_000]
    calls = []

    def receiver(base, key):
        calls.append((base, key))
        return [
            {"id": "chat-1", "purpose": "chat", "label": "Chat one"},
            {"id": "speech-1", "purpose": "speech"},
            {"id": "mystery-1"},
        ]

    config = {"providers": {"demo": {"api_key": "super-secret", "api_base": "https://example.test/v1"}}}
    first = model_catalog.load_catalog(
        config,
        {"kind": "chat", "provider": "demo", "current_model": "manual-model"},
        now_ms=lambda: now[0],
        cache=cache,
        receiver=receiver,
    )

    assert first.status == "available"
    assert first.source == "remote"
    assert [item.id for item in first.models] == ["demo/chat-1", "demo/mystery-1", "manual-model"]
    assert first.models[-1].current is True
    assert calls == [("https://example.test/v1", "super-secret")]
    assert "super-secret" not in json.dumps(cache)

    now[0] += 1_000
    cached = model_catalog.load_catalog(
        config,
        {"kind": "chat", "provider": "demo"},
        now_ms=lambda: now[0],
        cache=cache,
        receiver=lambda *_: (_ for _ in ()).throw(AssertionError("refresh")),
    )
    assert cached.source == "cache"

    now[0] += model_catalog.FRESH_TTL_MS
    failed = model_catalog.load_catalog(
        config,
        {"kind": "chat", "provider": "demo", "refresh": True},
        now_ms=lambda: now[0],
        cache=cache,
        receiver=lambda *_: (_ for _ in ()).throw(RuntimeError("secret failure")),
    )
    assert failed.status == "error"
    assert failed.source == "stale"
    assert "secret" not in (failed.message or "")


def test_catalog_rejects_cross_origin_pagination(monkeypatch):
    monkeypatch.setattr(model_catalog, "_provider_specs", lambda: (_spec("demo"),))
    seen = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "chat-1", "purpose": "chat"}], "next": "https://evil.test/models"}

    monkeypatch.setattr(model_catalog.httpx, "get", lambda url, **kwargs: (seen.append(url) or Response()))
    result = model_catalog.load_catalog(
        {"providers": {"demo": {"api_key": "key", "api_base": "https://example.test/v1"}}},
        {"kind": "chat", "provider": "demo", "refresh": True},
        now_ms=lambda: 1_000,
        cache={"version": 2, "entries": {}},
    )

    assert result.status == "rejected"
    assert result.source == "none"
    assert seen == ["https://example.test/v1/models"]


def test_catalog_applies_provider_prefix_rules(monkeypatch):
    monkeypatch.setattr(
        model_catalog,
        "_provider_specs",
        lambda: (_spec("gateway", strip_model_prefix=True, strip_model_prefixes=("route",)),),
    )
    result = model_catalog.load_catalog(
        {"providers": {"gateway": {"api_key": "key", "api_base": "https://example.test/v1"}}},
        {"kind": "chat", "provider": "gateway", "refresh": True},
        now_ms=lambda: 1_000,
        cache={"version": 2, "entries": {}},
        receiver=lambda *_: [
            {"id": "vendor/model", "purpose": "chat"},
            {"id": "route/model", "purpose": "chat"},
        ],
    )

    assert [item.id for item in result.models] == ["vendor/model", "route/model"]


def test_catalog_ignores_non_object_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "models_cache.json"
    cache_path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("FAMILIA_MODELS_CACHE", str(cache_path))
    monkeypatch.setattr(model_catalog, "_provider_specs", lambda: (_spec("demo"),))

    result = model_catalog.load_catalog(
        {"providers": {"demo": {"api_key": "key", "api_base": "https://example.test/v1"}}},
        {"kind": "chat", "provider": "demo", "refresh": True},
        now_ms=lambda: 1_000,
        receiver=lambda *_: [{"id": "chat-1", "purpose": "chat"}],
    )

    assert result.status == "available"
    assert [item.id for item in result.models] == ["demo/chat-1"]


def test_configured_dynamic_provider_loads_compatible_catalog(monkeypatch):
    monkeypatch.setattr(model_catalog, "_provider_specs", lambda: ())
    config = {
        "providers": {
            "tenant-chat": {
                "api_key": "tenant-secret",
                "api_base": "https://tenant.example/v1",
            }
        }
    }

    providers = model_catalog.list_providers(config, "chat")
    assert providers[0].key == "tenant-chat"
    assert providers[0].catalog_mode == "remote"
    assert providers[0].configured is True

    seen = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "tenant-model", "purpose": "chat"}]}

    monkeypatch.setattr(
        model_catalog.httpx,
        "get",
        lambda url, **_kwargs: (seen.append(url) or Response()),
    )

    result = model_catalog.load_catalog(
        config,
        {"kind": "chat", "provider": "tenant-chat", "refresh": True},
        now_ms=lambda: 1_000,
        cache={"version": 2, "entries": {}},
    )

    assert result.status == "available"
    assert [item.id for item in result.models] == ["tenant-model"]
    assert seen == ["https://tenant.example/v1/models"]


def test_oauth_catalog_uses_familia_cache_and_force_refresh(monkeypatch):
    monkeypatch.setattr(
        model_catalog,
        "_provider_specs",
        lambda: (
            _spec(
                "openai_codex",
                model_catalog="hybrid",
                is_oauth=True,
                default_api_base="https://chatgpt.example/backend-api",
            ),
        ),
    )
    calls = []

    def fake_catalog(_proxy=None):
        calls.append("fetch")
        return SimpleNamespace(
            source="remote",
            fetched_at=1,
            message=None,
            models=(SimpleNamespace(id="openai-codex/remote", label="Remote"),),
        )

    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_openai_codex_model_catalog",
        fake_catalog,
    )
    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.invalidate_openai_codex_model_catalog",
        lambda: None,
    )
    cache = {"version": 2, "entries": {}}
    config = {"providers": {"openai_codex": {}}}
    now = [1_000]

    first = model_catalog.load_catalog(
        config,
        {"kind": "chat", "provider": "openai_codex"},
        now_ms=lambda: now[0],
        cache=cache,
    )
    assert first.source == "remote"
    now[0] += 1_000
    cached = model_catalog.load_catalog(
        config,
        {"kind": "chat", "provider": "openai_codex"},
        now_ms=lambda: now[0],
        cache=cache,
    )
    assert cached.source == "cache"
    refreshed = model_catalog.load_catalog(
        config,
        {"kind": "chat", "provider": "openai_codex", "refresh": True},
        now_ms=lambda: now[0],
        cache=cache,
    )
    assert refreshed.source == "remote"
    assert calls == ["fetch", "fetch"]


def test_oauth_builtin_fallback_is_not_exposed(monkeypatch):
    monkeypatch.setattr(
        model_catalog,
        "_provider_specs",
        lambda: (
            _spec(
                "openai_codex",
                model_catalog="hybrid",
                is_oauth=True,
                default_api_base="https://chatgpt.example/backend-api",
            ),
        ),
    )
    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_openai_codex_model_catalog",
        lambda _proxy=None: SimpleNamespace(
            source="fallback",
            fetched_at=1,
            message="fallback",
            models=(SimpleNamespace(id="openai-codex/builtin", label="Builtin"),),
        ),
    )
    result = model_catalog.load_catalog(
        {"providers": {"openai_codex": {}}},
        {"kind": "chat", "provider": "openai_codex", "refresh": True},
        now_ms=lambda: 1_000,
        cache={"version": 2, "entries": {}},
    )
    assert result.status == "unsupported"
    assert result.models == ()


def test_missing_main_and_fallback_providers_remain_legacy(monkeypatch):
    monkeypatch.setattr(model_catalog, "_provider_specs", lambda: (_spec("demo"),))
    result = model_catalog.list_providers(
        {
            "agents": {
                "defaults": {"provider": "demo", "model": "demo/main"},
                "familia_fallback": {"provider": "vanished", "model": "vanished/fallback"},
            },
            "providers": {
                "tenant": {"api_base": "https://tenant.example/v1"},
            },
        },
        "chat",
    )
    by_key = {item.key: item for item in result}
    assert by_key["vanished"].legacy is True
    assert by_key["tenant"].legacy is False
