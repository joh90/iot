from pathlib import Path

import pytest

from iotbot.config import ConfigError, load_settings


def test_defaults_resolve_against_base(tmp_path):
    s = load_settings(environ={"BOT_TOKEN": "123:abc"}, base_dir=tmp_path)
    assert s.bot_token == "123:abc"
    assert s.devices_path == tmp_path / "devices.json"
    assert s.state_dir == tmp_path / "state"
    assert s.timezone == "Asia/Singapore"
    assert s.discover_timeout == 5.0


def test_absolute_paths_kept(tmp_path):
    s = load_settings(environ={"BOT_TOKEN": "1:x", "USERS_PATH": "/etc/u.json"}, base_dir=tmp_path)
    assert s.users_path == Path("/etc/u.json")


@pytest.mark.parametrize("token", ["", "   ", "nocolon"])
def test_bad_token_rejected(token):
    with pytest.raises(ConfigError):
        load_settings(environ={"BOT_TOKEN": token})


@pytest.mark.parametrize("value", ["soon", "nan", "inf", "0", "-1"])
def test_bad_timeout_rejected(value):
    with pytest.raises(ConfigError):
        load_settings(environ={"BOT_TOKEN": "1:x", "DISCOVER_TIMEOUT": value})


def test_env_file_loaded_relative_to_base_without_mutating_environ(tmp_path, monkeypatch):
    import os

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_NAME", raising=False)
    (tmp_path / ".env").write_text("BOT_TOKEN=42:secret\nBOT_NAME=Test\n")
    s = load_settings(base_dir=tmp_path)
    assert s.bot_token == "42:secret"
    assert s.bot_name == "Test"
    assert "BOT_TOKEN" not in os.environ


def test_real_env_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:real")
    (tmp_path / ".env").write_text("BOT_TOKEN=2:file\n")
    assert load_settings(base_dir=tmp_path).bot_token == "1:real"


def test_missing_token_names_env_path(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match=str(tmp_path)):
        load_settings(base_dir=tmp_path)


def test_bad_timezone_rejected():
    with pytest.raises(ConfigError, match="timezone"):
        load_settings(environ={"BOT_TOKEN": "1:x", "TZ_NAME": "Mars/Base"})
