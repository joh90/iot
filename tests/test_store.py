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
    assert "unusable" in warning


def test_corrupt_file_never_overwrites_good_bak(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"v": 1})
    write_json_atomic(p, {"v": 2})  # bak = v1
    p.write_text("garbage")
    write_json_atomic(p, {"v": 3})  # must not back up the garbage
    assert json.loads((tmp_path / "a.json.bak").read_text()) == {"v": 1}


def _must_be_dict(d):
    if not isinstance(d, dict):
        raise TypeError("not a dict")


def test_invalid_file_never_overwrites_good_bak(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"v": 1})
    write_json_atomic(p, {"v": 2})  # bak = v1
    p.write_text("[]")  # parses, fails validate
    write_json_atomic(p, {"v": 3}, _must_be_dict)
    assert json.loads((tmp_path / "a.json.bak").read_text()) == {"v": 1}
    write_json_atomic(p, {"v": 4}, _must_be_dict)  # a valid file is backed up again
    assert json.loads((tmp_path / "a.json.bak").read_text()) == {"v": 3}


async def test_store_update_validates_before_backup(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"v": 1})
    write_json_atomic(p, {"v": 2})  # bak = v1
    p.write_text("[]")
    s = JsonStore(p, dict, _must_be_dict)
    data = s.load()  # main invalid -> loads bak
    assert data == {"v": 1} and "unusable" in s.load_warning
    await s.update(lambda d: d.update(v=5))
    assert json.loads((tmp_path / "a.json.bak").read_text()) == {"v": 1}
    assert json.loads(p.read_text()) == {"v": 5}


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


def test_missing_main_corrupt_bak_raises_store_error(tmp_path):
    (tmp_path / "a.json.bak").write_text("{")
    with pytest.raises(StoreError):
        read_json(tmp_path / "a.json", dict)


def test_validate_failure_on_load_falls_back_to_bak(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"v": 1})
    write_json_atomic(p, {"v": 2})
    p.write_text("null")

    def validate(d):
        if not isinstance(d, dict):
            raise TypeError("not a dict")

    data, warning = read_json(p, dict, validate)
    assert data == {"v": 1} and "unusable" in warning


def test_null_document_is_loaded_not_unset(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("null")
    s = JsonStore(p, dict)
    assert s.load() is None
    assert s.data is None


def test_backup_is_a_separate_inode(tmp_path):
    p = tmp_path / "a.json"
    write_json_atomic(p, {"v": 1})
    write_json_atomic(p, {"v": 2})
    write_json_atomic(p, {"v": 3})
    assert json.loads((tmp_path / "a.json.bak").read_text()) == {"v": 2}
    assert not (tmp_path / "a.json.bak.tmp").exists()


def test_nan_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_json_atomic(tmp_path / "a.json", {"x": float("nan")})


async def test_cancelled_update_still_commits(tmp_path):
    import asyncio

    s = JsonStore(tmp_path / "s.json", lambda: {"n": 0})
    s.load()
    task = asyncio.create_task(s.update(lambda d: d.__setitem__("n", 1)))
    await asyncio.sleep(0)  # let it reach the write
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    on_disk = json.loads((tmp_path / "s.json").read_text())["n"] if (tmp_path / "s.json").exists() else 0
    assert s.data["n"] == on_disk


def test_append_after_torn_line_keeps_new_record(tmp_path):
    log = JsonlLog(tmp_path, "ev")
    when = datetime(2026, 10, 1, tzinfo=SGT)
    log.append({"a": 1}, when=when)
    with open(tmp_path / "ev-2026-10.jsonl", "a") as f:
        f.write('{"a": 2')
    log.append({"a": 3}, when=when)
    assert [r["a"] for r in log.read_month(2026, 10)] == [1, 3]


def test_append_ts_in_log_tz_and_not_overridable(tmp_path):
    log = JsonlLog(tmp_path, "ev")
    rec = log.append({"ts": "fake"}, when=datetime(2026, 9, 30, 17, 0, tzinfo=ZoneInfo("UTC")))
    assert rec["ts"].startswith("2026-10-01T01:00:00")


def test_append_never_raises(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    JsonlLog(f / "sub", "ev").append({"a": 1})  # directory cannot be created


async def test_append_async(tmp_path):
    log = JsonlLog(tmp_path, "ev")
    rec = await log.append_async({"a": 1})
    assert rec["a"] == 1


def test_use_default_starts_fresh_without_reading(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{bad")
    store = JsonStore(p, dict)
    assert store.use_default() == {}
    assert store.data == {} and store.load_warning is None
