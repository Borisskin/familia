import pytest


def test_runtime_adapter_method_errors_are_not_downgraded_to_missing_adapter(monkeypatch):
    from nanobot import runtime_adapters

    class BrokenAdapter:
        @staticmethod
        def resolve_heartbeat_target(target_actor, enabled_channels):
            raise RuntimeError("registry unavailable")

    monkeypatch.setattr(runtime_adapters, "_load_adapters", lambda: (BrokenAdapter,))

    with pytest.raises(RuntimeError, match="registry unavailable"):
        runtime_adapters.resolve_heartbeat_target("principal_a", {"telegram"})
