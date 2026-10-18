"""Phase 4.9: Singapore public holidays (built-in list + recorded MOM replies, never online)."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from iotbot.llm.assistant import Assistant
from iotbot.llm.client import LlmClient
from iotbot.llm.tools import ArgError, parse_args, schema_problems
from iotbot.llm.world import HolidayTools
from iotbot.outside import holidays as mod
from iotbot.outside.holidays import Holidays
from iotbot.result import Actor
from tests.test_ac_service import make_ctx

SGT = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=SGT).timestamp()
ALICE = Actor(111, "Alice", "llm")
D2026 = "d_149b61ad0a22f61c09dc80f2df5bbec8"
D2028 = "d_" + "2" * 32
DALL = "d_" + "a" * 32                     # like MOM's combined 2020-2027 dataset


class Mom:
    """data.gov.sg: the collection lists datasets; each dataset is one year."""

    def __init__(self, years=None):
        self.years = years or {D2026: [{"_id": 1, "date": "2026-01-01", "day": "Thursday", "holiday": "New Year’s Day"},
                                       {"_id": 2, "date": "2026-11-09", "day": "Monday", "holiday": "Polling Day"}],
                               D2028: [{"_id": 1, "date": "2028-01-01", "day": "Saturday", "holiday": "New Year's Day"},
                                       {"_id": 2, "date": "bad", "holiday": "x"}]}
        self.calls: list[str] = []
        self.down = False
        self.fail: set[str] = set()
        self.page = 100

    def __call__(self, request):
        if self.down:
            raise httpx.ConnectError("down")
        if "collections" in request.url.path:
            self.calls.append("collection")
            return httpx.Response(200, json={"data": {"collectionMetadata": {"childDatasets": [*self.years, "junk"]}}})
        ds = request.url.params["resource_id"]
        self.calls.append(ds)
        if ds in self.fail:
            return httpx.Response(429)
        rows = self.years[ds]
        off = int(request.url.params.get("offset", 0))
        return httpx.Response(200, json={"success": True, "result": {"records": rows[off:off + self.page],
                                                                     "total": len(rows)}})


def hol(tmp_path, mom=None, clock=None):
    h = Holidays(tmp_path / "holidays.json", "Asia/Singapore",
                 http=httpx.AsyncClient(transport=httpx.MockTransport(mom or Mom())), clock=clock or (lambda: NOW))
    assert h.load() is None
    return h


@pytest.fixture(autouse=True)
def no_gap(monkeypatch):
    monkeypatch.setattr(mod, "CALL_GAP_S", 0)


def test_built_in_list_labels_holidays_observed_days_and_eves(tmp_path):
    h = hol(tmp_path)
    assert h.label(date(2026, 11, 9)) == "PH: Deepavali (Observed)"
    assert h.label(date(2026, 1, 1)) == "PH: New Year's Day"                    # straight quote
    assert h.label(date(2026, 2, 16)) == "eve: Chinese New Year's Eve, not a public holiday"
    assert h.label(date(2026, 12, 24)) == "eve: Christmas Eve, not a public holiday"
    assert h.label(date(2026, 12, 31)) == "eve: New Year's Eve, not a public holiday"
    assert h.label(date(2026, 10, 15)) is None
    assert h.day(date(2026, 10, 17)) == "Sat 17 Oct 2026: not a public holiday (a weekend)"
    assert "do not have MOM's holiday list for 2030" in h.day(date(2030, 1, 1))
    assert h.label(date(2030, 1, 1)) == "PH list for 2030 not known yet"


def test_next_long_weekends_and_year(tmp_path):
    h = hol(tmp_path)
    assert h.upcoming(date(2026, 10, 15), 2) == ["Sun 08 Nov 2026: Deepavali", "Mon 09 Nov 2026: Deepavali (Observed)"]
    lw = h.long_weekends(date(2026, 10, 15), date(2027, 3, 1))
    assert lw[0] == "Sat 07 Nov to Mon 09 Nov 2026 (3 days): Deepavali, Deepavali (Observed)"
    assert any(x.startswith("Fri 25 Dec to Sun 27 Dec 2026") for x in lw)
    assert any(x.startswith("Sat 06 Feb to Mon 08 Feb 2027 (3 days): Chinese New Year") for x in lw)
    assert len(h.year(2027)) == 12 and "MOM publishes" in h.year(2029)[0]


async def test_refresh_reads_new_years_saves_and_skips_past_years(tmp_path):
    mom = Mom()
    h = hol(tmp_path, mom)
    assert await h.refresh() is True                                   # never fetched: stale
    assert h.label(date(2026, 11, 9)) == "PH: Polling Day"            # the live list replaces 2026
    assert h.label(date(2026, 11, 8)) is None
    assert h.label(date(2028, 1, 1)) == "PH: New Year's Day" and h.label(date(2027, 1, 1))  # 2027 kept from fallback
    saved = json.loads((tmp_path / "holidays.json").read_text())
    assert sorted(saved["sets"]) == [D2026, D2028]
    assert await h.refresh() is False                                  # fresh: no calls
    assert mom.calls == ["collection", D2026, D2028]
    h2 = hol(tmp_path, mom)                                            # a restart reads the saved list
    assert h2.label(date(2026, 11, 9)) == "PH: Polling Day" and not h2.stale()


async def test_refresh_failure_keeps_the_old_list(tmp_path):
    mom = Mom()
    mom.down = True
    h = hol(tmp_path, mom)
    assert await h.refresh() is False and h.label(date(2026, 11, 9)) == "PH: Deepavali (Observed)"
    assert not (tmp_path / "holidays.json").exists()


def test_damaged_saved_file_falls_back(tmp_path):
    (tmp_path / "holidays.json").write_text("{not json")
    h = Holidays(tmp_path / "holidays.json", "Asia/Singapore", clock=lambda: NOW)
    assert "unreadable" in h.load() and h.label(date(2026, 12, 25)) == "PH: Christmas Day"


async def test_tool(tmp_path):
    tool = HolidayTools(hol(tmp_path)).tools(ALICE)[0]
    assert schema_problems(tool.parameters) == [] and not tool.write
    out = await tool.run(parse_args(tool, json.dumps({"what": "day", "date": "2026-11-09"})))
    assert out["lines"] == ["Mon 09 Nov 2026: PH: Deepavali (Observed)"] and out["today"] == "Thu 15 Oct 2026"
    out = await tool.run({"what": "next"})
    assert out["lines"][0] == "Sun 08 Nov 2026: Deepavali" and out["known_until"] == "25 Dec 2027"
    out = await tool.run({"what": "long_weekends", "year": 2027})
    assert out["lines"][0].startswith("Fri 01 Jan to Sun 03 Jan 2027")
    with pytest.raises(ArgError):
        await tool.run({"what": "day", "date": "2026-02-30"})


def test_stamp_marks_holidays_and_eves(tmp_path):
    h = hol(tmp_path)
    clock = lambda: datetime(2026, 11, 6, 20, 0, tzinfo=SGT).timestamp()
    a = Assistant(LlmClient("http://llm", 1), list, "Asia/Singapore", clock=clock, day_label=h.label)
    stamp = a.stamp(ALICE, "hi")
    assert "Sun 08 Nov (PH: Deepavali), Mon 09 Nov (PH: Deepavali (Observed))" in stamp
    assert stamp.startswith("[From Alice, Fri 06 Nov 2026 20:00; next days:")


def test_context_has_holidays_and_the_tool(tmp_path):
    ctx = make_ctx(tmp_path)
    assert ctx.holidays.label(date(2026, 12, 25)) == "PH: Christmas Day"
    assert "holidays" in [t.name for t in ctx.assistant.tools_for(ALICE)]



async def test_combined_dataset_never_overrides_a_single_year_and_is_paged(tmp_path):
    combined = [{"date": "2026-11-09", "holiday": "Old Polling Day"}, {"date": "2027-01-01", "holiday": "New Year's Day"},
                {"date": "2027-05-20", "holiday": "Vesak Day"}, {"date": "2025-01-01", "holiday": "New Year's Day"}]
    mom = Mom({DALL: combined, D2026: [{"date": "2026-01-01", "holiday": "New Year's Day"}]})
    mom.page = 2                                                       # two pages for the combined one
    h = hol(tmp_path, mom)
    assert await h.refresh() is True
    assert h.label(date(2026, 11, 9)) is None                          # the 2026 dataset wins for 2026
    assert h.label(date(2027, 5, 20)) == "PH: Vesak Day" and h.label(date(2025, 1, 1)) == "PH: New Year's Day"
    assert mom.calls.count(DALL) == 2


async def test_one_failing_dataset_keeps_the_others(tmp_path):
    mom = Mom()
    mom.fail = {D2028}
    h = hol(tmp_path, mom)
    assert await h.refresh() is True and h.label(date(2026, 11, 9)) == "PH: Polling Day"
    assert h.stale()                                                   # tried again on the next daily check
    mom.fail = set()
    assert await h.refresh() is True and h.label(date(2028, 1, 1)) == "PH: New Year's Day" and not h.stale()


def test_long_weekend_under_way_and_unknown_years(tmp_path):
    h = hol(tmp_path)
    assert h.long_weekends(date(2026, 11, 8), date(2026, 11, 20))[0].startswith("Sat 07 Nov to Mon 09 Nov 2026")
    assert h.long_weekends(date(2028, 1, 1), date(2028, 12, 31)) == ["I do not have MOM's holiday list for 2028 yet"]
    assert h.long_weekends(date(2027, 12, 1), date(2028, 6, 1))[-1] == "(MOM's list only goes to the end of 2027)"


def test_damaged_sets_change_nothing(tmp_path):
    (tmp_path / "holidays.json").write_text(json.dumps({"fetched_at": NOW, "sets": [1, 2]}))
    h = Holidays(tmp_path / "holidays.json", "Asia/Singapore", clock=lambda: NOW)
    assert "unreadable" in h.load() and h.stale() and h.label(date(2026, 12, 25)) == "PH: Christmas Day"
