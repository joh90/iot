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
