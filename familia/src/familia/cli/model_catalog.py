"""Model/provider catalogs shared by the Familia CLI and admin client.

The module deliberately keeps provider-specific knowledge behind two small
operations.  ``nanobot`` remains the source of truth; this module only turns
its metadata and a selected remote response into a stable JSON shape.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, MutableMapping, Sequence
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

CatalogKind = Literal["chat", "transcription"]
MAX_MODELS = 10_000
CACHE_VERSION = 2
FRESH_TTL_MS = 60 * 60 * 1000


class _CatalogRejected(ValueError):
    """The target or pagination chain violated the catalog safety boundary."""


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    kind: CatalogKind
    auth_kind: str
    catalog_mode: str
    configured: bool
    supports_model_choice: bool
    default_model: str | None = None
    legacy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderList:
    providers: tuple[Provider, ...] = ()
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "providers": [p.to_dict() for p in self.providers],
        }

    def __iter__(self):
        return iter(self.providers)

    def __len__(self) -> int:
        return len(self.providers)

    def __getitem__(self, index: int) -> Provider:
        return self.providers[index]


@dataclass(frozen=True)
class Model:
    id: str
    wire_id: str
    label: str
    purpose: str
    current: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogRequest:
    kind: CatalogKind
    provider: str
    refresh: bool = False
    api_key: str | None = None
    api_base: str | None = None
    current_model: str | None = None

    @classmethod
    def from_value(cls, value: "CatalogRequest | Mapping[str, Any]") -> "CatalogRequest":
        if isinstance(value, cls):
            return value
        return cls(
            kind=value.get("kind", "chat"),
            provider=str(value.get("provider", "")),
            refresh=bool(value.get("refresh", False)),
            api_key=value.get("api_key"),
            api_base=value.get("api_base"),
            current_model=value.get("current_model"),
        )


@dataclass(frozen=True)
class CatalogSnapshot:
    provider: str
    kind: CatalogKind
    status: str
    source: str
    fetched_at_ms: int
    stale: bool
    message: str | None
    models: tuple[Model, ...] = ()
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "kind": self.kind,
            "status": self.status,
            "source": self.source,
            "fetched_at_ms": self.fetched_at_ms,
            "stale": self.stale,
            "message": self.message,
            "models": [m.to_dict() for m in self.models],
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class _ProviderMeta:
    key: str
    label: str
    auth_kind: str = "api_key"
    catalog_mode: str = "remote"
    default_model: str | None = None
    supports_model_choice: bool = True
    is_oauth: bool = False
    is_transcription_only: bool = False
    is_direct: bool = False
    default_api_base: str = ""
    strip_model_prefix: bool = False
    strip_model_prefixes: tuple[str, ...] = ()
    settings_alias_for: str = ""


def _provider_specs() -> tuple[Any, ...]:
    try:
        from nanobot.providers.registry import PROVIDERS

        return tuple(PROVIDERS)
    except Exception:  # pragma: no cover - only used by a missing optional host
        return ()


def _transcription_specs() -> tuple[Any, ...]:
    try:
        from nanobot.audio.transcription_registry import TRANSCRIPTION_PROVIDERS

        return tuple(TRANSCRIPTION_PROVIDERS)
    except Exception:  # pragma: no cover - only used by a missing optional host
        return ()


def _config_mapping(config: Any) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        return config
    dump = getattr(config, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="python")
        except TypeError:
            return dump()
    return {}


def _config_section(config: Any, name: str) -> Mapping[str, Any]:
    raw = _config_mapping(config).get(name, {})
    if isinstance(raw, Mapping):
        return raw
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        try:
            value = dump(mode="python")
        except TypeError:
            value = dump()
        return value if isinstance(value, Mapping) else {}
    return {}


def _value(section: Any, *names: str, default: Any = None) -> Any:
    if isinstance(section, Mapping):
        for name in names:
            if name in section and section[name] is not None:
                return section[name]
        return default
    for name in names:
        value = getattr(section, name, None)
        if value is not None:
            return value
    return default


def _provider_config(config: Any, key: str) -> Mapping[str, Any]:
    providers = _config_section(config, "providers")
    value = providers.get(key, {}) if isinstance(providers, Mapping) else {}
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            value = dump(mode="python")
        except TypeError:
            value = dump()
    return value if isinstance(value, Mapping) else {}


def _configured(config: Any, key: str) -> bool:
    section = _provider_config(config, key)
    return bool(_value(section, "api_key", "apiKey") or _value(section, "api_base", "apiBase"))


def _meta_from_spec(spec: Any, *, kind: CatalogKind) -> _ProviderMeta:
    if kind == "transcription":
        return _ProviderMeta(
            key=str(getattr(spec, "name", "")),
            label=str(getattr(spec, "name", "")).replace("_", " ").title(),
            auth_kind="api_key",
            catalog_mode="remote",
            default_model=getattr(spec, "default_model", None),
            supports_model_choice=str(getattr(spec, "name", "")) != "yandex",
        )
    oauth = bool(getattr(spec, "is_oauth", False))
    mode = str(getattr(spec, "model_catalog", "auto") or "auto")
    if mode == "auto":
        mode = "remote" if getattr(spec, "default_api_base", "") or getattr(spec, "is_direct", False) else "unsupported"
    return _ProviderMeta(
        key=str(getattr(spec, "name", "")),
        label=str(getattr(spec, "label", "") or getattr(spec, "display_name", "") or getattr(spec, "name", "")),
        auth_kind="oauth" if oauth else ("none" if getattr(spec, "is_direct", False) and not getattr(spec, "env_key", "") else "api_key"),
        catalog_mode="oauth" if mode == "hybrid" else mode,
        supports_model_choice=True,
        is_oauth=oauth,
        is_transcription_only=bool(getattr(spec, "is_transcription_only", False)),
        is_direct=bool(getattr(spec, "is_direct", False)),
        default_api_base=str(getattr(spec, "default_api_base", "") or ""),
        strip_model_prefix=bool(getattr(spec, "strip_model_prefix", False)),
        strip_model_prefixes=tuple(getattr(spec, "strip_model_prefixes", ()) or ()),
        settings_alias_for=str(getattr(spec, "settings_alias_for", "") or ""),
    )


def _canonical_specs(kind: CatalogKind) -> dict[str, tuple[Any, _ProviderMeta]]:
    result: dict[str, tuple[Any, _ProviderMeta]] = {}
    specs = _transcription_specs() if kind == "transcription" else _provider_specs()
    for spec in specs:
        meta = _meta_from_spec(spec, kind=kind)
        if not meta.key or (kind == "chat" and meta.is_transcription_only):
            continue
        canonical = meta.settings_alias_for or meta.key
        if canonical not in result:
            result[canonical] = (spec, meta if canonical == meta.key else _ProviderMeta(**{**meta.__dict__, "key": canonical}))
    return result


def _canonical_key(kind: CatalogKind, value: str) -> str:
    """Resolve a registry alias without maintaining a second provider list."""
    if not value:
        return value
    for spec in (_transcription_specs() if kind == "transcription" else _provider_specs()):
        meta = _meta_from_spec(spec, kind=kind)
        if meta.key == value:
            return meta.settings_alias_for or meta.key
    return value


def _provider_config_for(config: Any, kind: CatalogKind, key: str) -> Mapping[str, Any]:
    section = _provider_config(config, key)
    if section:
        return section
    specs = _transcription_specs() if kind == "transcription" else _provider_specs()
    for spec in specs:
        meta = _meta_from_spec(spec, kind=kind)
        if meta.settings_alias_for == key:
            alias_section = _provider_config(config, meta.key)
            if alias_section:
                return alias_section
    return section


def _dynamic_chat_pair(name: str) -> tuple[Any, _ProviderMeta] | None:
    try:
        from nanobot.providers.registry import create_dynamic_spec

        spec = create_dynamic_spec(name)
    except Exception:
        return None
    return spec, _meta_from_spec(spec, kind="chat")


def _configured_provider_names(config: Any) -> set[str]:
    providers = _config_section(config, "providers")
    return {str(k) for k, v in providers.items() if isinstance(v, Mapping) and (_value(v, "api_key", "apiKey") or _value(v, "api_base", "apiBase"))}


def _agent_slot_values(config: Any) -> tuple[tuple[str, str], ...]:
    agents = _config_section(config, "agents")
    values: list[tuple[str, str]] = []
    for slot in ("defaults", "familia_fallback"):
        section = _value(agents, slot, default={})
        provider = str(_value(section, "provider", default="") or "")
        model = str(_value(section, "model", default="") or "")
        if not provider:
            provider = _provider_for_model(model)
        if provider or model:
            values.append((provider, model))
    return tuple(values)


def _current_values(config: Any, kind: CatalogKind) -> tuple[str, str]:
    raw = _config_mapping(config)
    if kind == "transcription":
        transcription = _config_section(config, "transcription")
        channels = _config_section(config, "channels")
        provider = str(_value(transcription, "provider", "transcription_provider", "transcriptionProvider", default="") or "")
        if not provider:
            provider = str(_value(channels, "transcriptionProvider", "transcription_provider", default="") or "")
        model = str(_value(transcription, "model", default="") or "")
        return provider, model
    slots = _agent_slot_values(config)
    return slots[0] if slots else ("", "")


def _provider_for_model(model: str) -> str:
    value = (model or "").lower()
    for spec in _provider_specs():
        name = str(getattr(spec, "name", ""))
        if name and (value.startswith(name.lower() + "/") or name.lower() in value):
            return name
        for keyword in getattr(spec, "keywords", ()) or ():
            if str(keyword).lower() in value:
                return name
    return ""


def list_providers(config: Any, kind: CatalogKind) -> ProviderList:
    """List providers from nanobot registries without network access."""
    if kind not in ("chat", "transcription"):
        raise ValueError("kind must be chat or transcription")
    entries = _canonical_specs(kind)
    configured = _configured_provider_names(config)
    configured_canonical = {_canonical_key(kind, name) for name in configured}
    saved_canonical = {
        _canonical_key(kind, provider)
        for provider, _ in _agent_slot_values(config)
        if provider
    }
    for name in configured:
        if name in entries:
            continue
        if kind != "chat":
            continue
        pair = _dynamic_chat_pair(name)
        if pair is not None:
            entries[name] = pair
        else:
            entries[name] = (None, _ProviderMeta(key=name, label=name.replace("_", " ").title()))
    for saved in saved_canonical:
        if saved and saved not in entries:
            entries[saved] = (
                None,
                _ProviderMeta(
                    key=saved,
                    label=saved,
                    catalog_mode="unsupported",
                    supports_model_choice=saved.casefold() != "yandex",
                ),
            )

    result: list[Provider] = []
    for key, (_, meta) in entries.items():
        result.append(
            Provider(
                key=key,
                label=meta.label,
                kind=kind,
                auth_kind=meta.auth_kind,
                catalog_mode=meta.catalog_mode,
                configured=key in configured_canonical or _configured(config, key),
                supports_model_choice=meta.supports_model_choice,
                default_model=meta.default_model,
                legacy=key in saved_canonical and key not in configured_canonical and key not in _canonical_specs(kind),
            )
        )
    result.sort(key=lambda item: (item.legacy, item.label.casefold(), item.key))
    return ProviderList(tuple(result))


def _cache_path(config: Any) -> Path:
    explicit = os.environ.get("FAMILIA_MODELS_CACHE")
    if explicit:
        return Path(explicit)
    source = getattr(config, "_source_path", None)
    if source:
        return Path(source).expanduser().resolve().parent / "models_cache.json"
    path = os.environ.get("FAMILIA_CONFIG_FILE") or os.environ.get("NANOBOT_CONFIG")
    if path:
        return Path(path).expanduser().resolve().parent / "models_cache.json"
    return Path.home() / ".nanobot" / "models_cache.json"


def _load_cache(path: Path) -> MutableMapping[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, Mapping)
            or data.get("version") != CACHE_VERSION
            or not isinstance(data.get("entries"), dict)
        ):
            return {"version": CACHE_VERSION, "entries": {}}
        return data
    except (OSError, ValueError, TypeError):
        return {"version": CACHE_VERSION, "entries": {}}


def _save_cache(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _secret_fingerprint(secret: str | None) -> str:
    if not secret:
        return "oauth" if secret is None else "none"
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _cache_key(kind: CatalogKind, provider: str, base: str, api_key: str | None) -> str:
    # The key contains no credential material; the digest is deliberately one-way.
    return json.dumps([kind, provider, base.strip().rstrip("/"), _secret_fingerprint(api_key)], ensure_ascii=False, separators=(",", ":"))


def _safe_message(exc: BaseException) -> str:
    return f"Каталог недоступен ({type(exc).__name__})"


def _normalize_id(meta: _ProviderMeta, wire_id: str) -> str:
    value = wire_id.strip()
    if not value:
        return ""
    prefixes = {
        meta.key.casefold(),
        meta.key.replace("_", "-").casefold(),
        *(prefix.casefold() for prefix in meta.strip_model_prefixes),
    }
    if "/" in value and (
        meta.strip_model_prefix or value.split("/", 1)[0].casefold() in prefixes
    ):
        return value
    if meta.is_direct or meta.key in {"custom", "azure_openai", "bedrock"}:
        return value
    return f"{meta.key}/{value}"


def _purpose(row: Mapping[str, Any]) -> str:
    explicit = row.get("purpose") or row.get("modality") or row.get("type")
    if isinstance(explicit, str):
        lower = explicit.casefold()
        if any(x in lower for x in ("speech", "audio", "transcri", "asr", "voice")):
            return "transcription"
        if any(x in lower for x in ("chat", "text", "language", "completion")):
            return "chat"
        if any(x in lower for x in ("image", "vision", "embedding", "vector", "rerank")):
            return "other"
    modalities: list[str] = []
    for key in ("modalities", "input_modalities", "output_modalities", "capabilities"):
        value = row.get(key)
        if isinstance(value, str):
            modalities.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
            modalities.extend(str(v) for v in value)
        elif isinstance(value, Mapping):
            modalities.extend(str(k) for k, enabled in value.items() if enabled)
    lower = " ".join(modalities).casefold()
    if any(x in lower for x in ("speech", "audio", "transcri", "asr", "voice")):
        return "transcription"
    if any(x in lower for x in ("image", "embedding", "vector", "rerank", "vision")):
        return "other"
    if any(x in lower for x in ("text", "chat", "completion")):
        return "chat"
    return "unknown"


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row if isinstance(row, Mapping) else {"id": str(row)} for row in payload]
    if not isinstance(payload, Mapping):
        return []
    for key in ("data", "models", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row if isinstance(row, Mapping) else {"id": str(row)} for row in value]
    return []


def _extract_models(meta: _ProviderMeta, rows: Iterable[Mapping[str, Any]], kind: CatalogKind, current: str | None) -> tuple[Model, ...]:
    result: list[Model] = []
    seen: set[str] = set()
    for row in rows:
        wire = str(row.get("id") or row.get("name") or "").strip()
        if not wire or wire in seen:
            continue
        seen.add(wire)
        purpose = _purpose(row)
        if kind == "transcription" and purpose != "transcription":
            continue
        if kind == "chat" and purpose in {"other", "transcription"}:
            continue
        stored = _normalize_id(meta, wire)
        result.append(Model(stored, wire, str(row.get("label") or row.get("display_name") or stored), purpose, stored == (current or "") or wire == (current or "")))
        if len(result) >= MAX_MODELS:
            break
    if current and not any(item.current for item in result) and len(result) < MAX_MODELS:
        # Keep a manually configured value visible without pretending it came
        # from the provider's successful response.
        result.append(Model(current, current, current, "unknown", True))
    return tuple(result)


def _remote_rows(base: str, key: str | None) -> list[Mapping[str, Any]]:
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    base_url = f"{base.rstrip('/')}/models"
    origin = urlsplit(base_url)
    rows: list[Mapping[str, Any]] = []
    next_url: str | None = base_url
    seen_urls: set[str] = set()
    while next_url and next_url not in seen_urls and len(rows) < MAX_MODELS:
        seen_urls.add(next_url)
        target = urlsplit(next_url)
        if target.scheme not in {"http", "https"} or (target.netloc and target.netloc != origin.netloc):
            raise _CatalogRejected("catalog pagination redirected to another host")
        response = httpx.get(next_url, headers=headers, timeout=10.0, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        page = _rows(payload)
        rows.extend(page[: MAX_MODELS - len(rows)])
        candidate = None
        if isinstance(payload, Mapping):
            candidate = payload.get("next")
            if not candidate and isinstance(payload.get("links"), Mapping):
                candidate = payload["links"].get("next")
            if not candidate and isinstance(payload.get("pagination"), Mapping):
                candidate = payload["pagination"].get("next")
        next_url = urljoin(next_url, str(candidate)) if candidate else None
    return rows


def _snapshot(request: CatalogRequest, *, status: str, source: str, models: Sequence[Model] = (), fetched_at_ms: int = 0, stale: bool = False, message: str | None = None) -> CatalogSnapshot:
    return CatalogSnapshot(request.provider, request.kind, status, source, fetched_at_ms, stale, message, tuple(models))


def _cached_models(entry: Mapping[str, Any] | None) -> tuple[Model, ...]:
    if not isinstance(entry, Mapping):
        return ()
    models: list[Model] = []
    for item in entry.get("models", ()):
        if not isinstance(item, Mapping):
            continue
        try:
            model_id = str(item.get("id", ""))
            if not model_id:
                continue
            models.append(Model(
                id=model_id,
                wire_id=str(item.get("wire_id", model_id)),
                label=str(item.get("label", model_id)),
                purpose=str(item.get("purpose", "unknown")),
                current=bool(item.get("current", False)),
            ))
        except (TypeError, ValueError):
            continue
    return tuple(models[:MAX_MODELS])


def load_catalog(
    config: Any,
    request: CatalogRequest | Mapping[str, Any],
    *,
    now_ms: Callable[[], int] | None = None,
    cache: MutableMapping[str, Any] | None = None,
    receiver: Callable[..., Sequence[Mapping[str, Any]]] | None = None,
) -> CatalogSnapshot:
    """Load one provider catalog, retaining stale data on refresh failure."""
    req = CatalogRequest.from_value(request)
    if req.kind not in ("chat", "transcription"):
        raise ValueError("kind must be chat or transcription")
    canonical_provider = _canonical_key(req.kind, req.provider)
    if canonical_provider != req.provider:
        req = CatalogRequest(
            req.kind,
            canonical_provider,
            req.refresh,
            req.api_key,
            req.api_base,
            req.current_model,
        )
    now = now_ms or (lambda: int(time.time() * 1000))
    if req.current_model is None:
        configured_provider, configured_model = _current_values(config, req.kind)
        if _canonical_key(req.kind, configured_provider) == req.provider:
            req = CatalogRequest(
                req.kind,
                req.provider,
                req.refresh,
                req.api_key,
                req.api_base,
                configured_model or None,
            )
    entries = _canonical_specs(req.kind)
    pair = entries.get(req.provider)
    if pair is None and req.kind == "chat" and _configured(config, req.provider):
        pair = _dynamic_chat_pair(req.provider)
    if pair is None:
        return _snapshot(req, status="unsupported", source="none", message="Каталог для поставщика не поддерживается")
    spec, meta = pair
    section = _provider_config_for(config, req.kind, req.provider)
    api_key = req.api_key if req.api_key is not None else _value(section, "api_key", "apiKey")
    api_base = (req.api_base if req.api_base is not None else _value(section, "api_base", "apiBase")) or meta.default_api_base
    api_key = str(api_key) if api_key else None
    api_base = str(api_base or "")
    if not api_base:
        if req.kind == "transcription" and meta.default_model:
            models = (Model(_normalize_id(meta, meta.default_model), meta.default_model, meta.default_model, "transcription", meta.default_model == req.current_model),)
            return _snapshot(req, status="available", source="none", models=models, message="Используется модель по умолчанию nanobot")
        return _snapshot(req, status="unsupported", source="none", message="Каталог для поставщика не поддерживается")
    if not api_key and meta.auth_kind == "api_key" and not meta.is_direct:
        return _snapshot(req, status="not_configured", source="none", message="Поставщик не настроен")

    path = _cache_path(config)
    data = cache if cache is not None else _load_cache(path)
    entries_data = data.setdefault("entries", {})
    key = _cache_key(req.kind, req.provider, api_base, api_key)
    old = entries_data.get(key) if isinstance(entries_data, Mapping) else None
    current_now = now()
    if isinstance(old, Mapping) and not req.refresh:
        age = max(0, current_now - int(old.get("fetched_at_ms") or 0))
        if age < FRESH_TTL_MS:
            models = _cached_models(old)
            return _snapshot(req, status="available", source="cache", models=models, fetched_at_ms=int(old.get("fetched_at_ms") or 0), stale=False)

    oauth_source = "remote"
    oauth_message: str | None = None
    oauth_rows: list[Mapping[str, Any]] | None = None
    if req.kind == "chat" and meta.catalog_mode == "oauth":
        try:
            from nanobot.providers.oauth_model_catalog import (
                get_oauth_model_catalog,
                invalidate_oauth_model_catalog,
            )

            if req.refresh:
                invalidate_oauth_model_catalog(req.provider)
            oauth = get_oauth_model_catalog(req.provider, proxy=_value(section, "proxy"))
            if getattr(oauth, "source", "") == "fallback":
                if isinstance(old, Mapping):
                    models = _cached_models(old)
                    return _snapshot(req, status="error", source="stale", models=models, fetched_at_ms=int(old.get("fetched_at_ms") or 0), stale=True, message="Каталог OAuth недоступен; используется устаревший список")
                return _snapshot(req, status="unsupported", source="none", message="Каталог OAuth недоступен; введите модель вручную")
            oauth_source = "stale" if str(getattr(oauth, "source", "")) == "stale" else "remote"
            oauth_message = getattr(oauth, "message", None)
            oauth_rows = [
                {"id": item.id, "label": getattr(item, "label", ""), "purpose": "chat"}
                for item in oauth.models
            ]
        except Exception as exc:
            logger.warning("OAuth model catalog failed: type={}", type(exc).__name__)
            if isinstance(old, Mapping):
                models = _cached_models(old)
                return _snapshot(req, status="error", source="stale", models=models, fetched_at_ms=int(old.get("fetched_at_ms") or 0), stale=True, message=_safe_message(exc))
            return _snapshot(req, status="error", source="none", message=_safe_message(exc))

    try:
        raw_rows = oauth_rows if oauth_rows is not None else list((receiver or _remote_rows)(api_base, api_key))
        models = _extract_models(meta, raw_rows, req.kind, req.current_model)
        if not models and req.kind == "transcription" and meta.default_model:
            models = (Model(_normalize_id(meta, meta.default_model), meta.default_model, meta.default_model, "transcription", meta.default_model == req.current_model),)
        fetched = current_now
        entries_data[key] = {"fetched_at_ms": fetched, "models": [m.to_dict() for m in models]}
        data["version"] = CACHE_VERSION
        if cache is None:
            _save_cache(path, data)
        return _snapshot(req, status="available", source=oauth_source, models=models, fetched_at_ms=fetched, stale=oauth_source == "stale", message=oauth_message)
    except _CatalogRejected as exc:
        logger.warning("Model catalog rejected: provider={} kind={} type={}", req.provider, req.kind, type(exc).__name__)
        if isinstance(old, Mapping):
            models = _cached_models(old)
            return _snapshot(req, status="rejected", source="stale", models=models, fetched_at_ms=int(old.get("fetched_at_ms") or 0), stale=True, message=_safe_message(exc))
        return _snapshot(req, status="rejected", source="none", message=_safe_message(exc))
    except Exception as exc:
        logger.warning("Model catalog refresh failed: provider={} kind={} type={}", req.provider, req.kind, type(exc).__name__)
        if isinstance(old, Mapping):
            models = _cached_models(old)
            return _snapshot(req, status="error", source="stale", models=models, fetched_at_ms=int(old.get("fetched_at_ms") or 0), stale=True, message=_safe_message(exc))
        return _snapshot(req, status="error", source="none", message=_safe_message(exc))


def provider_key_for_model(model: str) -> str:
    """Resolve a model to the nanobot registry without a Familia list."""
    return _provider_for_model(model)
