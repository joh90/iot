"""Read-only tools about the world outside the flat: weather and air (4.8), holidays (4.9).

Answers are lines worded in code (bands, advice, "as of" times); the model only relays
them. Asked about outside only: never added to aircon replies (PLAN "Outside").
"""

from __future__ import annotations

from typing import Any

from iotbot.llm.tools import Tool
from iotbot.outside.weather import AREAS, HOME, PARTS, REGIONS, SUMMARY, Outside
from iotbot.result import Actor

PART_HELP = ("now = temperature and rain now; forecast_2h; forecast_24h = today by period; outlook_4day; "
             "air = AQI, PSI, PM2.5 and NEA's advice; rain; lightning (10 km, last 30 min); uv; heat = heat "
             "stress (WBGT); wind; humidity")


class OutsideTools:
    def __init__(self, outside: Outside):
        self.outside = outside

    def tools(self, actor: Actor) -> list[Tool]:
        return [Tool(
            "outside", f"Weather and air quality in Singapore from NEA, worded for you to relay as given. "
                       f"Home is {HOME}; give area only if the person names another place (pick the nearest "
                       f"of these areas and say which one you used). Only for questions about outside.",
            {"type": "object", "properties": {
                "area": {"type": "string", "enum": list(AREAS)},
                "region": {"type": "string", "enum": list(REGIONS),
                           "description": "only when the person names a region (\"in the west\", \"central\")"},
                "parts": {"type": "array", "minItems": 1, "maxItems": 5, "description": PART_HELP,
                          "items": {"type": "string", "enum": list(PARTS)}}}},
            run=self._run)]

    async def _run(self, args: dict[str, Any]) -> dict[str, Any]:
        parts = list(dict.fromkeys(args.get("parts") or SUMMARY))
        out = await self.outside.report(args.get("area"), parts, args.get("region"))
        out["note"] = "Relay these lines; never add numbers, bands or advice they do not give."
        return out
