import json

from iotbot.main import main


def test_check_mode(tmp_path, capsys):
    (tmp_path / ".env").write_text("BOT_TOKEN=1:x\n")
    (tmp_path / "devices.json").write_text(json.dumps({"r": {"devices": []}}))
    (tmp_path / "users.json").write_text(json.dumps({"1": "A"}))
    (tmp_path / "commands.json").write_text("{}")
    assert main(["--env-file", str(tmp_path / ".env"), "--check"]) == 0
    out = capsys.readouterr().out
    assert "rooms=1 devices=0 users=1" in out


def test_missing_token_exit_2(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    assert main(["--env-file", str(tmp_path / ".env"), "--check"]) == 2


def test_corrupt_users_refuses_start(tmp_path):
    (tmp_path / ".env").write_text("BOT_TOKEN=1:x\n")
    (tmp_path / "users.json").write_text("{broken")
    assert main(["--env-file", str(tmp_path / ".env"), "--check"]) == 3


def test_rejected_token_not_logged(tmp_path, monkeypatch, caplog):
    from telegram.error import InvalidToken

    import iotbot.bot.app as app_mod

    (tmp_path / ".env").write_text("BOT_TOKEN=123:supersecret\n")
    (tmp_path / "users.json").write_text('{"1": "A"}')

    class FakeApp:
        def run_polling(self, **kw):
            assert kw["bootstrap_retries"] == -1
            raise InvalidToken("The token `123:supersecret` was rejected by the server.")

    monkeypatch.setattr(app_mod, "build_application", lambda ctx: FakeApp())
    assert main(["--env-file", str(tmp_path / ".env")]) == 2
    assert "supersecret" not in caplog.text


def test_discover_needs_no_token_and_reports(tmp_path, monkeypatch, capsys):
    import iotbot.context as context_mod
    from iotbot.devices.hub import DeviceStatus

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text("BOT_TOKEN=\n")
    (tmp_path / "users.json").write_text('{"1": "A"}')
    real_build = context_mod.build_context

    def build(settings, transport=None):
        ctx = real_build(settings, transport)

        async def fake_start(expected):
            ctx.hub.status = {
                "aa": DeviceStatus("aa", "Bedroom RM", online=True, type="RMMINI", ip="10.0.0.2"),
                "bb": DeviceStatus("bb", "Study RM", error="not found on the network"),
            }

        ctx.hub.start = fake_start
        return ctx

    monkeypatch.setattr(context_mod, "build_context", build)
    assert main(["--env-file", str(tmp_path / ".env"), "--discover"]) == 1
    out = capsys.readouterr().out
    assert "Bedroom RM: aa online RMMINI 10.0.0.2" in out
    assert "Study RM: bb OFFLINE (not found on the network)" in out
    assert "1/2 devices online" in out


def test_discover_rejects_malformed_token(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text("BOT_TOKEN=nocolon\n")
    assert main(["--env-file", str(tmp_path / ".env"), "--discover"]) == 2


def test_discover_refuses_corrupt_users(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text("BOT_TOKEN=\n")
    (tmp_path / "users.json").write_text("{broken")
    assert main(["--env-file", str(tmp_path / ".env"), "--discover"]) == 3
