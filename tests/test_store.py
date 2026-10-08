import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from iotbot.store import JsonlLog, JsonStore, StoreError, read_json, write_json_atomic

SGT = ZoneInfo("Asia/Singapore")


def test_write_then_read(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"x": 1})
    assert read_json(p, dict) == ({"x": 1}, None)
    assert not (tmp_path / "a.json.tmp").exists()


def test_second_write_keeps_backup(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"v": 1})
    write_json_atomic(p, {"v": 2})
    assert json.loads((tmp_path / "a.json.bak").read_text()) == {"v": 1}
    assert json.loads(p.read_text()) == {"v": 2}


def test_corrupt_falls_back_to_bak(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"v": 1})
    write_json_atomic(p, {"v": 2})
    p.write_text('{"v": 3')  # torn write
    data, warning = read_json(p, dict)
    assert data == {"v": 1}
    assert "unreadable" in warning


def test_corrupt_file_never_overwrites_good_bak(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"v": 1})
    write_json_atomic(p, {"v": 2})  # bak = v1
    p.write_text("garbage")
    write_json_atomic(p, {"v": 3})  # must not back up the garbage
    assert json.loads((tmp_path / "a.json.bak").read_text()) == {"v": 1}


def test_missing_file_uses_default_or_bak(tmp_path):
    p = tmp_path / "a.json"
    assert read_json(p, lambda: {"d": 1}) == ({"d": 1}, None)
    (tmp_path / "a.json.bak").write_text('{"b": 1}')
    data, warning = read_json(p, dict)
    assert data == {"b": 1} and "restored" in warning


def test_both_corrupt_raises(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("x")
    (tmp_path / "a.json.bak").write_text("y")
    with pytest.raises(StoreError):
        read_json(p, dict)


def test_corrupt_no_backup_raises(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("x")
    with pytest.raises(StoreError):
        read_json(p, dict)


async def test_store_update_saves_and_returns(tmp_path):
    s = JsonStore(tmp_path / "s.json", dict)
    s.load()
    r = await s.update(lambda d: d.setdefault("n", 5))
    assert r == 5
    assert s.data == {"n": 5}
    assert json.loads((tmp_path / "s.json").read_text()) == {"n": 5}


async def test_store_update_failure_leaves_state(tmp_path):
    s = JsonStore(tmp_path / "s.json", lambda: {"n": 1})
    s.load()

    def boom(d):
        d["n"] = 99
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await s.update(boom)
    assert s.data == {"n": 1}
    assert not (tmp_path / "s.json").exists()


async def test_store_validate_rejects(tmp_path):
    def validate(d):
        if "x" in d:
            raise ValueError("x not allowed")

    s = JsonStore(tmp_path / "s.json", lambda: {"ok": 1}, validate=validate)
    s.load()
    await s.update(lambda d: d.__setitem__("y", 2))
    assert s.data == {"ok": 1, "y": 2}
    with pytest.raises(ValueError):
        await s.update(lambda d: d.__setitem__("x", 1))
    assert s.data == {"ok": 1, "y": 2}
    assert json.loads((tmp_path / "s.json").read_text()) == {"ok": 1, "y": 2}


async def test_store_serializes_concurrent_updates(tmp_path):
    import asyncio

    s = JsonStore(tmp_path / "s.json", lambda: {"n": 0})
    s.load()

    def inc(d):
        d["n"] += 1

    await asyncio.gather(*(s.update(inc) for _ in range(20)))
    assert s.data["n"] == 20
    assert json.loads((tmp_path / "s.json").read_text())["n"] == 20


def test_jsonl_monthly_split_and_read(tmp_path):
    log = JsonlLog(tmp_path, "device_events")
    log.append({"a": 1}, when=datetime(2026, 9, 30, 23, 59, tzinfo=SGT))
    log.append({"a": 2}, when=datetime(2026, 10, 1, 0, 1, tzinfo=SGT))
    assert [r["a"] for r in log.read_month(2026, 9)] == [1]
    assert [r["a"] for r in log.read_month(2026, 10)] == [2]
    assert list(log.read_month(2026, 11)) == []


def test_jsonl_month_uses_local_time(tmp_path):
    # 2026-09-30 17:00 UTC is 2026-10-01 01:00 SGT
    log = JsonlLog(tmp_path, "ev")
    log.append({"a": 1}, when=datetime(2026, 9, 30, 17, 0, tzinfo=ZoneInfo("UTC")))
    assert (tmp_path / "ev-2026-10.jsonl").exists()


def test_jsonl_skips_torn_line(tmp_path):
    log = JsonlLog(tmp_path, "ev")
    log.append({"a": 1}, when=datetime(2026, 10, 1, tzinfo=SGT))
    with open(tmp_path / "ev-2026-10.jsonl", "a") as f:
        f.write('{"a": 2')
    assert [r["a"] for r in log.read_month(2026, 10)] == [1]
