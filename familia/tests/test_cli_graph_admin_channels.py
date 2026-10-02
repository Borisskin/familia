"""Channel administration through the CLI seam.

Every test runs ``graph_admin.main`` against a temporary ``config.json``
and checks what an operator would see: the saved section, the JSON reply
and the exit code. Networks, neonize and WhatsApp servers are replaced by
fakes at the outermost client; parameter handling stays real.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest

from familia.cli import graph_admin

KINDS = {"telegram", "vk", "discord", "slack", "matrix", "mattermost", "email", "whatsapp"}
SECRETS = (
    "token", "access_token", "app_token", "bot_token", "password",
    "imap_password", "smtp_password", "client_secret", "app_secret", "secret", "api_key",
)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("FAMILIA_CONFIG_FILE", str(path))
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(tmp_path / "audit.jsonl"))

    class Config:
        def write(self, channels: dict[str, Any]) -> None:
            path.write_text(json.dumps({"channels": channels}), encoding="utf-8")

        def section(self, kind: str) -> dict[str, Any]:
            return json.loads(path.read_text(encoding="utf-8"))["channels"][kind]

        def link_whatsapp(self) -> None:
            db = tmp_path / "whatsapp-auth" / "neonize.db"
            db.parent.mkdir(parents=True, exist_ok=True)
            db.write_bytes(b"session")

    return Config()


def run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = graph_admin.main(list(argv))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def add(capsys, kind: str, payload: dict[str, Any]) -> tuple[int, str]:
    rc, _, err = run(capsys, "channels", "add", kind, "--config", json.dumps(payload))
    return rc, err


def listed(capsys) -> dict[str, Any]:
    rc, out, _ = run(capsys, "channels", "list", "--json")
    assert rc == 0
    return json.loads(out)


def form_payload(listed_config: dict[str, Any], **typed: Any) -> dict[str, Any]:
    """What the admin form sends back: the listed config echoed plus the
    typed fields, never a redacted secret it had no field for."""
    payload = {k: v for k, v in listed_config.items() if k not in ("enabled", "allow_from")}
    payload.update(enabled=True, allow_from=["*"], **typed)
    return payload


# ---- kinds ------------------------------------------------------------------


def test_supported_kinds_match_the_field_contract(config, capsys):
    config.write({"dingtalk": {"enabled": True, "client_secret": "cn-secret-1"}})

    shown = listed(capsys)

    assert set(shown["supported_kinds"]) == KINDS
    rows = {r["name"]: r for r in shown["channels"]}
    assert rows["dingtalk"]["addable"] is False
    rc, out, _ = run(capsys, "channels", "deps", "status", "--json")
    assert rc == 0
    assert {row["kind"] for row in json.loads(out)["channels"]} == KINDS


def test_whatsapp_deps_need_python_magic(config, capsys, monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "magic" else real(name, *a))

    rc, out, _ = run(capsys, "channels", "deps", "status", "--json")

    row = {r["kind"]: r for r in json.loads(out)["channels"]}["whatsapp"]
    assert rc == 0
    assert row["installed"] is False
    assert "python-magic" in row["pip_spec"]


@pytest.mark.parametrize("argv", [
    ("channels", "add", "feishu", "--config", "{}"),
    ("channels", "test", "qq", "--config", "{}"),
    ("channels", "deps", "install", "wecom"),
])
def test_china_only_kinds_are_rejected(config, argv):
    with pytest.raises(SystemExit) as exc:
        graph_admin.main(list(argv))
    assert exc.value.code == 2


# ---- secrets ----------------------------------------------------------------


def test_list_redacts_every_secret_and_proxy_credentials(config, capsys):
    section = {key: f"value-of-{key}" for key in SECRETS}
    section.update(enabled=True, proxy="socks5://user:pw@proxy.example:1080")
    config.write({"slack": section})

    shown = listed(capsys)["channels"][0]["config"]

    for key in SECRETS:
        assert shown[key] == f"***{key[-4:]}", key
    assert shown["proxy"] == "socks5://***@proxy.example:1080"


def test_edit_keeps_secret_and_drops_cleared_field(config, capsys):
    config.write({"telegram": {"enabled": True, "token": "tg-secret-1234", "proxy": "http://p:1"}})
    shown = listed(capsys)["channels"][0]["config"]

    rc, err = add(capsys, "telegram", form_payload(shown, token="***1234", proxy=""))

    assert rc == 0, err
    assert config.section("telegram") == {"enabled": True, "allow_from": ["*"],
                                          "token": "tg-secret-1234"}


def test_redacted_secret_is_never_saved_even_without_a_stored_one(config, capsys):
    config.write({"telegram": {"enabled": True, "token": "tg-secret-1234"}})
    assert add(capsys, "telegram", {"token": "***9999"})[0] == 0
    assert config.section("telegram")["token"] == "tg-secret-1234"

    rc, err = add(capsys, "discord", {"token": "***9999"})
    assert rc == 2 and "requires" in err


@pytest.mark.parametrize("kind,payload", [
    ("telegram", {}),
    ("vk", {"group_id": "1"}),
    ("mattermost", {"token": "t"}),
    ("email", {"imap_host": "imap.x", "smtp_host": "smtp.x", "imap_username": "a@x"}),
])
def test_missing_required_fields_are_refused(config, capsys, kind, payload):
    rc, err = add(capsys, kind, payload)
    assert rc == 2
    assert "requires" in err


# ---- proxy masks ------------------------------------------------------------

PROXY = "socks5://user:pw@proxy.example:1080"


@pytest.mark.parametrize("sent,expected", [
    ("socks5://***@proxy.example:1080", PROXY),
    ("http://new.example:3128", "http://new.example:3128"),
    ("", None),
])
def test_proxy_mask_round_trip(config, capsys, sent, expected):
    config.write({"telegram": {"enabled": True, "token": "tg-secret-1234", "proxy": PROXY}})

    rc, err = add(capsys, "telegram", {"proxy": sent})

    assert rc == 0, err
    assert config.section("telegram").get("proxy") == expected


def test_proxy_host_changed_under_mask_is_refused(config, capsys):
    config.write({"telegram": {"enabled": True, "token": "tg-secret-1234", "proxy": PROXY}})

    rc, err = add(capsys, "telegram", {"proxy": "socks5://***@other.example:1080"})

    assert rc == 2
    assert "full proxy address" in err
    assert config.section("telegram")["proxy"] == PROXY


def test_connection_test_never_sees_masks(config, capsys, monkeypatch):
    config.write({"telegram": {"enabled": True, "token": "tg-secret-1234", "proxy": PROXY}})
    seen: list[str] = []

    class Reply:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"ok": True, "result": {"username": "bot", "id": 1}}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, **_: seen.append(url) or Reply())

    rc, out, _ = run(capsys, "channels", "test", "telegram", "--config",
                     json.dumps({"token": "***1234", "proxy": "socks5://***@proxy.example:1080"}))
    assert rc == 0 and json.loads(out)["ok"] is True
    assert seen == ["https://api.telegram.org/bottg-secret-1234/getMe"]

    rc, out, _ = run(capsys, "channels", "test", "telegram", "--config",
                     json.dumps({"proxy": "socks5://***@other.example:1080"}))
    assert json.loads(out)["ok"] is False


# ---- email ------------------------------------------------------------------

EMAIL_FORM = {
    "imap_host": "imap.mail.example", "imap_port": 993,
    "smtp_host": "smtp.mail.example", "smtp_port": 587,
    "imap_username": "owner@mail.example", "imap_password": "app-pass-1",
    "enabled": True, "allow_from": ["*"],
}


def test_email_form_saves_nanobot_keys(config, capsys):
    rc, err = add(capsys, "email", {**EMAIL_FORM, "smtp_port": 465})

    assert rc == 0, err
    saved = config.section("email")
    assert saved["smtp_username"] == saved["from_address"] == "owner@mail.example"
    assert saved["smtp_password"] == "app-pass-1"
    assert saved["consent_granted"] is True
    assert (saved["smtp_use_ssl"], saved["smtp_use_tls"]) == (True, False)


def test_email_derived_fields_follow_later_edits(config, capsys):
    assert add(capsys, "email", EMAIL_FORM)[0] == 0

    shown = listed(capsys)["channels"][0]["config"]
    assert add(capsys, "email", form_payload(shown, imap_password="app-pass-2"))[0] == 0
    assert config.section("email")["smtp_password"] == "app-pass-2"

    shown = listed(capsys)["channels"][0]["config"]
    assert add(capsys, "email", form_payload(shown, imap_username="new@mail.example",
                                             smtp_port=465))[0] == 0
    saved = config.section("email")
    assert saved["smtp_username"] == saved["from_address"] == "new@mail.example"
    assert (saved["smtp_use_ssl"], saved["smtp_use_tls"]) == (True, False)

    shown = listed(capsys)["channels"][0]["config"]
    assert add(capsys, "email", form_payload(shown, smtp_port=587))[0] == 0
    saved = config.section("email")
    assert (saved["smtp_use_ssl"], saved["smtp_use_tls"]) == (False, True)
    assert saved["smtp_password"] == "app-pass-2"


def test_email_hand_set_smtp_login_survives_edits(config, capsys):
    config.write({"email": {**EMAIL_FORM, "smtp_username": "relay@other.example",
                            "smtp_password": "relay-pass", "consent_granted": False}})

    shown = listed(capsys)["channels"][0]["config"]
    assert add(capsys, "email", form_payload(shown, imap_password="app-pass-2"))[0] == 0

    saved = config.section("email")
    assert saved["consent_granted"] is True
    assert saved["smtp_username"] == "relay@other.example"
    assert saved["smtp_password"] == "relay-pass"


def test_legacy_email_opens_filled_and_saves_without_retyping(config, capsys):
    legacy = {k: v for k, v in EMAIL_FORM.items() if not k.startswith("imap_user")
              and k != "imap_password"}
    config.write({"email": {**legacy, "user": "owner@mail.example", "password": "old-pass-9"}})

    shown = listed(capsys)["channels"][0]["config"]
    assert shown["imap_username"] == "owner@mail.example"
    assert shown["imap_password"] == "***ss-9"
    assert "user" not in shown and "password" not in shown

    rc, err = add(capsys, "email", form_payload(shown))

    assert rc == 0, err
    saved = config.section("email")
    assert "user" not in saved and "password" not in saved
    assert saved["imap_password"] == saved["smtp_password"] == "old-pass-9"


def test_legacy_matrix_renames_user(config, capsys):
    config.write({"matrix": {"enabled": True, "homeserver": "https://m.example",
                             "user": "@bot:m.example", "access_token": "mx-token-1"}})

    shown = listed(capsys)["channels"][0]["config"]
    rc, err = add(capsys, "matrix", form_payload(shown))

    assert rc == 0, err
    saved = config.section("matrix")
    assert saved["user_id"] == "@bot:m.example"
    assert "user" not in saved
    assert saved["access_token"] == "mx-token-1"


def test_email_connection_test_uses_merged_nanobot_keys(config, capsys, monkeypatch):
    config.write({"email": {"imap_host": "imap.mail.example", "user": "owner@mail.example",
                            "password": "old-pass-9", "enabled": True}})
    logins: list[tuple[str, str]] = []

    class Imap:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def login(self, user: str, pwd: str) -> None:
            logins.append((user, pwd))

    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", Imap)

    rc, _, _ = run(capsys, "channels", "test", "email", "--config",
                     json.dumps({"imap_host": "imap.mail.example",
                                 "imap_username": "owner@mail.example"}))

    assert rc == 0
    assert logins == [("owner@mail.example", "old-pass-9")]


# ---- mattermost -------------------------------------------------------------


def test_mattermost_saves_and_keeps_token(config, capsys):
    rc, err = add(capsys, "mattermost", {"server_url": "https://mm.example",
                                         "token": "mm-token-1", "team_id": ""})
    assert rc == 0, err
    assert config.section("mattermost") == {"server_url": "https://mm.example",
                                            "token": "mm-token-1", "enabled": True}

    shown = listed(capsys)["channels"][0]["config"]
    assert shown["token"] == "***en-1"
    assert add(capsys, "mattermost", form_payload(shown, team_id="team1"))[0] == 0
    assert config.section("mattermost")["token"] == "mm-token-1"


def test_mattermost_connection_test_refuses_missing_fields(config, capsys):
    rc, out, _ = run(capsys, "channels", "test", "mattermost", "--config",
                     json.dumps({"server_url": "https://mm.example"}))

    assert rc == 0
    assert json.loads(out) == {"ok": False, "message": "server_url or token missing",
                               "implemented": True}


# ---- whatsapp: saving -------------------------------------------------------


def test_whatsapp_is_not_saved_or_enabled_before_linking(config, capsys):
    rc, err = add(capsys, "whatsapp", {"enabled": True, "allow_from": ["*"]})
    assert rc == 2 and "not linked" in err

    config.write({"whatsapp": {"enabled": False}})
    rc, _, err = run(capsys, "channels", "enable", "whatsapp")
    assert rc == 2 and "not linked" in err


def test_whatsapp_linked_saves_only_admin_fields(config, capsys):
    config.link_whatsapp()

    rc, err = add(capsys, "whatsapp", {"enabled": True, "allow_from": ["*"], "proxy": PROXY})

    assert rc == 0, err
    assert config.section("whatsapp") == {"enabled": True, "allow_from": ["*"], "proxy": PROXY}


# ---- whatsapp: linking ------------------------------------------------------


class FakeLink:
    def __init__(self, reply: dict[str, Any] | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handle(self, action: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((action, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return dict(self.reply)


def connect(capsys, *argv: str, proxy: str | None = None) -> tuple[int, dict[str, Any]]:
    if proxy is not None:
        argv = (*argv, "--request-stdin")
    stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps({"proxy": proxy}))
    try:
        rc, out, err = run(capsys, "channels", "whatsapp-connect", *argv, "--json")
    finally:
        sys.stdin = stdin
    return rc, json.loads(out if rc == 0 else err)


def test_whatsapp_connect_reply_format(config, capsys, monkeypatch):
    link = FakeLink({"session_id": "s1", "status": "expired", "message": "expired"})
    monkeypatch.setattr(graph_admin, "_WHATSAPP_LINK", link)

    rc, reply = connect(capsys, "poll", "--session-id", "s1")

    assert rc == 0
    assert reply == {"schema_version": 1, "session_id": "s1", "status": "expired",
                     "message": "expired"}
    assert link.calls == [("poll", {"session_id": "s1", "force": False, "proxy": None})]


def test_whatsapp_connect_error_format(config, capsys, monkeypatch):
    monkeypatch.setattr(graph_admin, "_WHATSAPP_LINK", FakeLink(RuntimeError("boom")))

    rc, reply = connect(capsys, "cancel", "--session-id", "s1")

    assert rc == 1
    assert reply["schema_version"] == 1
    assert reply["code"] == "WHATSAPP_CONNECT"
    assert "boom" in reply["error"]


def test_whatsapp_connect_unmasks_stored_proxy(config, capsys, monkeypatch):
    config.write({"whatsapp": {"enabled": False, "proxy": PROXY}})
    link = FakeLink({"session_id": "", "status": "succeeded", "message": "ok"})
    monkeypatch.setattr(graph_admin, "_WHATSAPP_LINK", link)

    assert connect(capsys, "start", proxy="socks5://***@proxy.example:1080")[0] == 0
    assert connect(capsys, "start", proxy="")[0] == 0

    assert [c[1]["proxy"] for c in link.calls] == [PROXY, ""]


def test_whatsapp_relink_refused_while_channel_enabled(config, capsys, monkeypatch):
    config.link_whatsapp()
    config.write({"whatsapp": {"enabled": True}})
    link = FakeLink({"session_id": "s1", "status": "pending", "message": "wait"})
    monkeypatch.setattr(graph_admin, "_WHATSAPP_LINK", link)

    rc, reply = connect(capsys, "start", "--force")

    assert rc == 1
    assert reply["code"] == "WHATSAPP_CONNECT"
    assert link.calls == []


# ---- whatsapp: real adapter over a fake neonize ------------------------------


class FakeNeonizeClient:
    instances: ClassVar[list[FakeNeonizeClient]] = []

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.proxy_address: str | None = None
        self._qr: Any = None
        self._stopped = asyncio.Event()
        FakeNeonizeClient.instances.append(self)

    def qr(self, handler: Any) -> Any:
        self._qr = handler
        return handler

    def event(self, _kind: Any) -> Any:
        return lambda handler: handler

    async def connect(self, proxy: Any = None) -> asyncio.Task[None]:
        self.proxy_address = getattr(proxy, "proxy_address", None)

        async def native() -> None:
            await self._qr(self, b"2@raw-pairing-string")
            await self._stopped.wait()

        return asyncio.create_task(native())

    async def stop(self) -> None:
        self._stopped.set()


@pytest.fixture
def fake_neonize(monkeypatch: pytest.MonkeyPatch):
    from nanobot.channels.whatsapp import runtime

    FakeNeonizeClient.instances.clear()
    monkeypatch.setattr(runtime, "_NEONIZE_API", runtime._NeonizeAPI(
        NewAClient=FakeNeonizeClient, ConnectedEv=object, DisconnectedEv=object,
        MessageEv=object, PairStatusEv=object, build_jid=None,
        detect_mime=None, detect_buffer=None,
    ))
    binder = types.ModuleType("neonize._binder")

    class ProxySettings:
        def __init__(self, proxy_address: str) -> None:
            self.proxy_address = proxy_address

    binder.ProxySettings = ProxySettings  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "neonize", types.ModuleType("neonize"))
    monkeypatch.setitem(sys.modules, "neonize._binder", binder)
    monkeypatch.setattr(graph_admin, "_WHATSAPP_LINK", None)
    return FakeNeonizeClient.instances


def test_whatsapp_link_passes_unsaved_proxy_and_draws_qr(config, capsys, fake_neonize):
    pytest.importorskip("segno")
    config.write({"whatsapp": {"enabled": False, "proxy": PROXY}})

    rc, started = connect(capsys, "start", proxy="http://new:pw@proxy2.example:3128")

    assert rc == 0, started
    assert started["status"] == "pending"
    assert started["qr_svg"].startswith("data:image/svg+xml")
    assert "raw-pairing" not in json.dumps(started)
    assert fake_neonize[-1].proxy_address == "http://new:pw@proxy2.example:3128"
    assert config.section("whatsapp") == {"enabled": False, "proxy": PROXY}

    rc, cancelled = connect(capsys, "cancel", "--session-id", started["session_id"])
    assert rc == 0 and cancelled["status"] == "cancelled"

    rc, again = connect(capsys, "start", proxy="socks5://***@proxy.example:1080")
    assert rc == 0, again
    assert fake_neonize[-1].proxy_address == PROXY
    connect(capsys, "cancel", "--session-id", again["session_id"])


# ---- chat noise defaults ------------------------------------------------------


QUIET = {"sendProgress": False, "sendToolHints": False, "showCompactionNotices": False}


def _channels(config_path: Path) -> dict[str, Any]:
    return json.loads(config_path.read_text(encoding="utf-8"))["channels"]


def test_channel_add_writes_quiet_chat_defaults(config, capsys, tmp_path):
    config.write({})

    rc, err = add(capsys, "telegram", {"enabled": True, "token": "tg-secret-1234"})

    assert rc == 0, err
    channels = _channels(tmp_path / "config.json")
    assert {k: channels[k] for k in QUIET} == QUIET


def test_channel_add_keeps_explicit_user_choice(config, capsys, tmp_path):
    config.write({"sendToolHints": True})

    rc, err = add(capsys, "telegram", {"enabled": True, "token": "tg-secret-1234"})

    assert rc == 0, err
    channels = _channels(tmp_path / "config.json")
    assert channels["sendToolHints"] is True
    assert channels["sendProgress"] is False


def test_channel_add_keeps_explicit_snake_case_choice(config, capsys, tmp_path):
    config.write({"send_progress": True, "send_tool_hints": True, "show_compaction_notices": True})

    rc, err = add(capsys, "telegram", {"enabled": True, "token": "tg-secret-1234"})

    assert rc == 0, err
    channels = _channels(tmp_path / "config.json")
    assert not set(QUIET) & set(channels)
    assert channels["send_progress"] is channels["send_tool_hints"] is True
    assert channels["show_compaction_notices"] is True
