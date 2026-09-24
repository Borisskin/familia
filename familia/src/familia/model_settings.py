"""Nanobot model selection and persistence used by the Familia admin CLI."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class ModelSettingsError(ValueError):
    """A configuration conflict that must be reported before writing."""


_MODEL_PRESET_KEYS = ("modelPreset", "model_preset")
_FALLBACK_KEYS = ("fallbackModels", "fallback_models")
_GENERATION_FIELDS = (
    "max_tokens",
    "context_window_tokens",
    "temperature",
    "reasoning_effort",
)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _configured_value(mapping: dict[str, Any], keys: tuple[str, ...], label: str) -> Any:
    present = [(key, mapping[key]) for key in keys if key in mapping]
    if len(present) > 1 and present[0][1] != present[1][1]:
        raise ModelSettingsError(
            f"conflicting aliases for {label}: {present[0][0]} and {present[1][0]}"
        )
    return present[0][1] if present else None


def _remove_aliases(mapping: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        mapping.pop(key, None)


def _set_alias(mapping: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    existing = next((key for key in keys if key in mapping), keys[-1])
    _remove_aliases(mapping, keys)
    mapping[existing] = value


def _raw_provider_config(providers: dict[str, Any], provider: str) -> dict[str, Any]:
    wanted = provider.replace("-", "_").lower()
    for key, value in providers.items():
        if key.replace("-", "_").lower() == wanted and isinstance(value, dict):
            return value
    return {}


def _redact(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return "***" if len(value) <= 8 else f"***{value[-4:]}"


def _nanobot_config(raw: dict[str, Any]) -> Any:
    """Parse once through Nanobot so provider matching stays authoritative."""
    try:
        from nanobot.config.schema import Config

        return Config.model_validate(raw)
    except ModelSettingsError:
        raise
    except Exception as exc:  # pydantic's concrete error differs by Nanobot version
        raise ModelSettingsError(f"invalid model configuration: {exc}") from exc


def _provider_data(config: Any, value: dict[str, Any]) -> tuple[Any, str, Any, str | None]:
    """Return Nanobot's typed preset, provider name/config, and effective base URL."""
    try:
        from nanobot.config.schema import ModelPresetConfig

        preset = ModelPresetConfig.model_validate(value)
        provider = config.get_provider_name(preset.model, preset=preset) or preset.provider
        provider_cfg = config.get_provider(preset.model, preset=preset)
        api_base = config.get_api_base(preset.model, preset=preset)
    except Exception as exc:
        raise ModelSettingsError(f"provider resolution failed: {exc}") from exc
    return preset, provider or "auto", provider_cfg, api_base


def _slot(
    config: Any,
    value: dict[str, Any],
    *,
    source: str,
    profile: str | None = None,
) -> dict[str, Any]:
    preset, provider, provider_cfg, api_base = _provider_data(config, value)
    extra_body = getattr(provider_cfg, "extra_body", None)
    if not isinstance(extra_body, dict):
        extra_body = {}
    return {
        "model": preset.model,
        "provider": provider,
        "api_key": _redact(getattr(provider_cfg, "api_key", None)),
        "api_base": api_base,
        "context_window_tokens": preset.context_window_tokens,
        "max_tokens": preset.max_tokens,
        "temperature": preset.temperature,
        "reasoning_effort": preset.reasoning_effort,
        "source": source,
        "profile": profile,
        "provider_model_override": extra_body.get("model"),
    }


def _preset_map(raw: dict[str, Any]) -> dict[str, Any]:
    camel = raw.get("modelPresets")
    snake = raw.get("model_presets")
    if isinstance(camel, dict) and isinstance(snake, dict) and camel != snake:
        raise ModelSettingsError("conflicting aliases for model presets")
    return _mapping(camel if isinstance(camel, dict) else snake)


def _resolve_primary(
    raw: dict[str, Any],
    config: Any | None = None,
) -> tuple[dict[str, Any], str | None, str]:
    agents = _mapping(raw.get("agents"))
    defaults = _mapping(agents.get("defaults"))
    profile = _configured_value(defaults, _MODEL_PRESET_KEYS, "agents.defaults.model_preset")
    _preset_map(raw)  # detect a conflicting alias before Nanobot chooses one
    config = config or _nanobot_config(raw)
    try:
        selected = config.resolve_preset()
    except Exception as exc:
        raise ModelSettingsError(f"model preset {profile!r} is missing or invalid: {exc}") from exc
    return (
        selected.model_dump(mode="python"),
        str(profile) if profile and profile != "default" else None,
        "preset" if profile and profile != "default" else "defaults",
    )


def _legacy_fallback_value(raw: dict[str, Any]) -> dict[str, Any] | None:
    legacy = _mapping(raw.get("agents")).get("familia_fallback")
    return deepcopy(legacy) if isinstance(legacy, dict) and legacy else None


def _fallback_values(raw: dict[str, Any]) -> tuple[list[Any], str, str | None]:
    agents = _mapping(raw.get("agents"))
    defaults = _mapping(agents.get("defaults"))
    native = _configured_value(defaults, _FALLBACK_KEYS, "agents.defaults.fallback_models")
    legacy = _legacy_fallback_value(raw)
    if native is not None:
        if not isinstance(native, list):
            raise ModelSettingsError("agents.defaults.fallback_models must be an array")
        # Keep the old block visible when native settings take precedence.
        return list(native), "native", "familia_fallback" if legacy else None
    if legacy is not None:
        return [legacy], "legacy", "familia_fallback"
    return [], "none", None


def _resolve_fallback(
    value: object,
    *,
    primary: dict[str, Any],
    config: Any,
) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            preset = config.model_presets[value]
        except (KeyError, TypeError) as exc:
            raise ModelSettingsError(
                f"fallback model preset {value!r} is missing or invalid"
            ) from exc
        return preset.model_dump(mode="python")
    if isinstance(value, dict):
        resolved = deepcopy(primary)
        resolved.update(value)
        return resolved
    raise ModelSettingsError("fallback_models entries must be preset names or objects")


def read_model_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the effective primary and native fallback chain for display."""
    config = _nanobot_config(raw)
    primary, profile, source = _resolve_primary(raw, config)
    values, fallback_source, legacy_key = _fallback_values(raw)
    legacy_value = _legacy_fallback_value(raw)
    chain = [
        _slot(
            config,
            _resolve_fallback(value, primary=primary, config=config),
            source="native" if fallback_source == "native" else "legacy",
        )
        for value in values
    ]
    fallback = chain[0] if chain else None
    if fallback is not None:
        fallback["enabled"] = fallback_source == "native"
    legacy_fallback = None
    if legacy_value is not None:
        legacy_fallback = _slot(
            config,
            _resolve_fallback(legacy_value, primary=primary, config=config),
            source="legacy",
        )
        legacy_fallback["enabled"] = False
    legacy_conflict = bool(
        legacy_fallback
        and fallback_source == "native"
        and (
            not fallback
            or any(
                legacy_fallback.get(field) != fallback.get(field)
                for field in (
                    "model",
                    "provider",
                    "api_base",
                    "context_window_tokens",
                    "max_tokens",
                    "temperature",
                    "reasoning_effort",
                    "provider_model_override",
                )
            )
        )
    )
    primary_slot = _slot(
        config,
        primary,
        source=source,
        profile=profile,
    )
    return {
        "schema_version": 1,
        "main": primary_slot,
        "fallback": fallback,
        "fallback_chain": chain,
        "fallback_source": fallback_source,
        "fallback_enabled": fallback_source == "native" and bool(chain),
        "legacy_fallback_key": legacy_key,
        "legacy_fallback": legacy_fallback,
        "legacy_fallback_conflict": legacy_conflict,
    }


def resolve_slot(raw: dict[str, Any], slot: str) -> dict[str, Any] | None:
    """Resolve one slot for a live probe without exposing its secret."""
    config = _nanobot_config(raw)
    primary, _profile, _source = _resolve_primary(raw, config)
    if slot == "main":
        value = primary
    elif slot == "fallback":
        values, fallback_source, _legacy_key = _fallback_values(raw)
        # The legacy Familia key is displayed as pending migration, but
        # Nanobot only executes its native fallback list.
        if fallback_source != "native" or not values:
            return None
        value = _resolve_fallback(values[0], primary=primary, config=config)
    else:
        raise ModelSettingsError(f"unsupported agent slot {slot!r}")

    if not value.get("model"):
        return None
    preset, provider, provider_cfg, api_base = _provider_data(config, value)
    return {
        "model": preset.model,
        "provider": provider,
        "api_key": getattr(provider_cfg, "api_key", None) or "",
        "api_base": api_base or "",
    }


def _validate(raw: dict[str, Any]) -> None:
    """Validate the candidate with Nanobot's schema before replacing the file."""
    _nanobot_config(raw)


def _requested_provider(raw: dict[str, Any], model: str, provider: str | None) -> str:
    config = _nanobot_config(raw)
    try:
        from nanobot.config.schema import ModelPresetConfig

        requested = ModelPresetConfig(model=model, provider=provider or "auto")
        return config.get_provider_name(model, preset=requested) or requested.provider
    except Exception as exc:
        raise ModelSettingsError(f"provider resolution failed: {exc}") from exc


def _reject_provider_override(raw: dict[str, Any], model: str, provider: str) -> None:
    config = _nanobot_config(raw)
    try:
        from nanobot.config.schema import ModelPresetConfig

        requested = ModelPresetConfig(model=model, provider=provider)
        provider_cfg = config.get_provider(model, preset=requested)
    except Exception as exc:
        raise ModelSettingsError(f"provider resolution failed: {exc}") from exc
    override = getattr(provider_cfg, "extra_body", None)
    if isinstance(override, dict) and override.get("model") not in (None, "", model):
        raise ModelSettingsError(
            f"provider extra_body.model selects {override['model']!r}, not {model!r}"
        )


def apply_slot(
    raw: dict[str, Any],
    *,
    slot: str,
    model: str,
    provider: str | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
) -> dict[str, Any]:
    """Apply one admin slot and return the compatible public snapshot.

    The input mapping is only changed after all alias, override, and schema checks
    pass. Callers persist it atomically.
    """
    model = model.strip()
    if not model:
        raise ModelSettingsError("model is required")
    if slot not in {"main", "fallback"}:
        raise ModelSettingsError(f"unsupported agent slot {slot!r}")
    candidate = deepcopy(raw)
    agents = candidate.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise ModelSettingsError("agents must be an object")
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        raise ModelSettingsError("agents.defaults must be an object")
    _configured_value(defaults, _MODEL_PRESET_KEYS, "agents.defaults.model_preset")
    _preset_map(candidate)
    native = _configured_value(defaults, _FALLBACK_KEYS, "agents.defaults.fallback_models")
    if native is not None and not isinstance(native, list):
        raise ModelSettingsError("agents.defaults.fallback_models must be an array")
    provider_name = _requested_provider(candidate, model, (provider or "").strip() or None)
    _reject_provider_override(candidate, model, provider_name)

    if slot == "main":
        config = _nanobot_config(candidate)
        active, _profile, _source = _resolve_primary(candidate, config)
        for key in _GENERATION_FIELDS:
            if key in active:
                defaults[key] = active[key]
        defaults["model"] = model
        defaults["provider"] = provider_name
        _remove_aliases(defaults, _MODEL_PRESET_KEYS)
    else:
        chain = list(native or [])
        # The simple Familia control edits the first native fallback and leaves
        # every subsequent Nanobot candidate untouched.
        if chain:
            previous = chain[0]
            if isinstance(previous, dict):
                replacement = deepcopy(previous)
            elif isinstance(previous, str):
                config = _nanobot_config(candidate)
                primary, _profile, _source = _resolve_primary(candidate, config)
                previous = _resolve_fallback(
                    previous,
                    primary=primary,
                    config=config,
                )
                replacement = {
                    key: previous[key]
                    for key in _GENERATION_FIELDS
                    if key in previous
                }
            else:
                replacement = {}
            replacement.update(model=model, provider=provider_name)
            chain[0] = replacement
        else:
            chain.append({"model": model, "provider": provider_name})
        _set_alias(defaults, _FALLBACK_KEYS, chain)
        agents.pop("familia_fallback", None)

    providers = candidate.get("providers", {})
    if providers is None:
        providers = {}
    if not isinstance(providers, dict):
        raise ModelSettingsError("providers must be an object")
    config = _raw_provider_config(providers, provider_name)
    if api_key:
        config["api_key"] = api_key
    if api_base:
        config["api_base"] = api_base
    if api_key or api_base:
        if "providers" not in candidate or candidate["providers"] is None:
            candidate["providers"] = providers
        # Preserve the spelling already used by the input provider map.
        key = next(
            (name for name in providers if name.replace("-", "_").lower() == provider_name.replace("-", "_").lower()),
            provider_name,
        )
        providers[key] = config

    _validate(candidate)
    raw.clear()
    raw.update(candidate)
    return read_model_settings(raw)


def clear_fallback(raw: dict[str, Any]) -> dict[str, Any]:
    """Disable all legacy/native fallback entries without changing primary."""
    candidate = deepcopy(raw)
    agents = candidate.get("agents", {})
    if agents is None:
        agents = {}
    if not isinstance(agents, dict):
        raise ModelSettingsError("agents must be an object")
    defaults = agents.get("defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ModelSettingsError("agents.defaults must be an object")
    _remove_aliases(defaults, _FALLBACK_KEYS)
    agents["defaults"] = defaults
    agents.pop("familia_fallback", None)
    candidate["agents"] = agents
    _validate(candidate)
    raw.clear()
    raw.update(candidate)
    return read_model_settings(raw)
