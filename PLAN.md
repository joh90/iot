# iot-bot v2 plan

Planned 2026-10-08/09 (SGT). Build starts on the 5h rate-window reset (~02:00 SGT 2026-10-09,
exact time in `~/.cache/claude-rate-limits`). Work on a branch; the old
bot on the Pi keeps running untouched until the branch is proven.

## Decisions

| Topic         | Decision                                                                                                                                             |
|---------------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| Host          | Bot runs on the Raspberry Pi 24/7. LLM stays on the Fedora PC                                                                                        |
| Pi -> LLM     | `autossh -N -L 8001:127.0.0.1:8001 fedora` as a systemd unit on the Pi; bot calls 127.0.0.1:8001                                                     |
| LLM backend   | Local llama-server qwen36-35b-a3b + deterministic fast path                                                                                          |
| LLM build     | NOT YET. Needs a deeper planning session first (see "LLM: open planning items")                                                                      |
| Approach      | Restructure on PTB 22 (async), keep device/room/IR model + IR data. Minimal bug-fixing                                                               |
| Storage       | NO SQLite. JSON state files (atomic tmp+rename, saved on every change) + JSONL logs (monthly)                                                        |
| Roles         | None (B4 skipped). All approved users are equal                                                                                                      |
| Python        | uv-managed Python 3.13 on the Pi (system Python is likely 3.6; PTB 22 needs >= 3.10)                                                                 |
| Hardware      | Office + joh-bedroom: RM mini 3 (RMMINI) + Mitsubishi Electric fn18ve (since 2023-06; Daikin only in the old `.bk`)                                  |
| TTL prompts   | Run-time nudge + pre-schedule heads-up + (later, with LLM) confirm-every-LLM-action expiry                                                           |
| Tracking      | Numbered native tasks, reviewer subagent after each task                                                                                             |
| Users         | Me + household (2-4). Messages attribute actions ("X set bedroom to 24C")                                                                            |
| First run     | 2026-10-09 02:02 run builds Phase 1 ONLY, then stops for user test                                                                                   |
| Service layer | All surfaces (slash, buttons, scheduler, later LLM) call one typed service API. Results carry structured warnings/conflicts; never silently resolved |

## Bug triage (from code review + Fable review)

Fix:

| #    | Bug                                                                                     | Fix                                                                                                 |
|------|-----------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| B1   | PTB 13 imports `imghdr` (gone in 3.13); cryptography 3.2 has no wheel                   | Rewrite: PTB 22 + broadlink 0.19                                                                    |
| B2   | `/d` + keyboard call any device attribute (`__delattr__`, `fire_action`)                | Allowlist: feature must be in device interface                                                      |
| B5   | Auth requires username match; renamed / no-username users locked out                    | Auth on user id only; name is a label                                                               |
| B7   | Only `RMMINI` sends; other RMs silently drop                                            | `hasattr(rm, "send_data")`                                                                          |
| B9   | One `auth()` failure kills startup; no rediscovery after DHCP change                    | Per-device try, discover by MAC, retry-on-fail rediscover; optional DHCP reservation + IP in config |
| B10  | `logger` undefined in `iot/devices/broadlink/__init__.py`; drops whole room             | Add logger, `.upper()` once                                                                         |
| B11  | Legacy Markdown crashes on `_` in user text / device ids                                | Plain text or MarkdownV2 escaping                                                                   |
| B13  | Delete user: no self-delete recheck, KeyError on double tap                             | Recheck + `.get`                                                                                    |
| B15  | Error handler logs full Update (PII), never answers callback spinner                    | Log `update_id`, answer query                                                                       |
| B16  | Token on CLI                                                                            | `.env`                                                                                              |
| B17  | Interface vs commands.json key drift; keyboard shows buttons with no codes              | Fix keys; build keyboard from available codes                                                       |
| B19  | Duplicate device ids across rooms overwrite                                             | Reject at load                                                                                      |
| B22  | README drift                                                                            | Rewrite for Pi + tunnel + .env setup                                                                |
| B23  | `users.json` / `devices.json` tracked in a public repo                                  | Gitignore + `*.example.json`                                                                        |
| B26+ | Literal `*` in popups, `query.message` None, same-name users, "not modified" double tap | Small fixes                                                                                         |

Free with rewrite / AC engine: B8 (blocking sleeps), B14 (adduser fallthrough), B18 (callback_data
64B -> compact string callback data, NOT `arbitrary_callback_data`; see adversarial #2), B20 (thread safety), B24/B25 (bogus "toggle" / temp captures).

Not fixing: B3 (set-top box data), B4 (roles), B6 as SQLite (handled instead by atomic JSON save on
change), B12 (/on /off silent), B21 (broadlink_cli / discovery scripts).

## Phases

Build order (decided 2026-10-09): 1, 3, 2, 5, 6, 7; 4 (LLM) after its planning session. The Phase 3
hardware check was dropped; Phase 2 ships only after the D7 cutover and a few days of using the AC
buttons, and untested AC states (fan 2/4/quiet, vane 2-5) get tried through the picker before
schedules rely on them (2026-10-10).

| Phase | Scope                                                                                                                                                                                                                                                                                                                                                           |
|-------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1     | Foundation: uv + pyproject (py3.13), PTB 22.8, broadlink 0.19, `.env`, JSON state store (atomic), auth by id, Broadlink layer (per-RM lock, pre-decoded bytes, reconnect), device_events JSONL from day 1, pytest, chosen B-fixes. Typed service layer (`device.send(device, action)` returning Result with warnings) so Phase 2 + LLM plug in without rewrites |
| 2     | Scheduling: see "Scheduling design" below                                                                                                                                                                                                                                                                                                                       |
| 3     | AC state engine, rescoped 2026-10-09: Mitsubishi 144-bit encoder (cool only; temp, fan, up/down vane + swing, powerful), per-AC state, golden tests vs the 6 captures, On/Off/Powerful buttons send encoded frames. No `/ac` UI (state picker comes with Phase 2), no hardware check (test by use after cutover)                                                |
| 5     | TTL prompts (non-LLM parts): run-time nudge [Off][Keep][Snooze], pre-schedule heads-up [OK][Skip tonight], `/prefs` (TTL, threshold, default-on-timeout, quiet hours)                                                                                                                                                                                           |
| 6     | Telemetry: `turns` JSONL (user, surface, raw, intent, outcome, parse/ir/total ms), owner `/stats` p50/p95                                                                                                                                                                                                                                                       |
| 7     | Weekly recommendations from device_events (plain stats), hours-run + kWh/SGD (SP tariff 0.2972/kWh)                                                                                                                                                                                                                                                             |
| 4     | LLM layer -- after deeper planning                                                                                                                                                                                                                                                                                                                              |
| --    | Deferred ideas: presence (phone on Wi-Fi), weather (NEA), scenes, `/learn` via Telegram, voice notes                                                                                                                                                                                                                                                            |

## Scheduling design (Phase 2, agreed 2026-10-09)

Use cases: daily/weekly bedtime + wake, sleep timers (one-shot), pre-cool (weekly "on" + Phase 5
heads-up [OK][Skip today]), pause, skip. Timezone fixed Asia/Singapore.

| Topic        | Decision                                                                                                                                                                                                                                                                                                           |
|--------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Action       | Per device: managed AC -> `{"kind":"state","state":AcState.to_dict()}` (exact, Phase 3 encoder); non-AC or unmanaged AC -> `{"kind":"capture","key":...}`                                                                                                                                                          |
| Send policy  | Always send full state, even if last-sent state matches (IR is one-way; corrects drift)                                                                                                                                                                                                                            |
| Recurrence   | `when.kind` = `weekly` (time + days) or `once` (ISO datetime). Weekday/weekend = separate entries                                                                                                                                                                                                                  |
| Pause        | Indefinite (`enabled:false`) or until date (`paused_until`, auto-resumes)                                                                                                                                                                                                                                          |
| Skip         | "Skip next" button + specific dates (`skip_dates`, past ones pruned). List shows "(Fri skipped)"                                                                                                                                                                                                                   |
| Timers       | Multiple one-shots per device allowed; deleted after firing (kept in events log)                                                                                                                                                                                                                                   |
| Conflicts    | Same device within +/-5 min, or on right after off -> service returns conflict; UI asks [Keep both][Replace][Cancel]; LLM later asks in chat                                                                                                                                                                       |
| Missed fire  | Bot down at fire time -> skip + AUDIBLE "missed" notice (user kept this after adversarial review)                                                                                                                                                                                                                  |
| Step intent  | Steps typed `on(state)` / `off` / `adjust(temp=..)`; adjust skipped if bot-known state is off. Manual changes never suspend schedules                                                                                                                                                                              |
| Ownership    | Shared: anyone sees/edits any schedule. Fire msg + edits-by-others notify the creator                                                                                                                                                                                                                              |
| Fire msg     | "Bedtime: bedroom AC on, cool 22C fan4" [Undo][Skip next][Pause]; ALWAYS silent. Only failures, missed, conflicts are audible. Undo (10 min) resends previous state, only if this fire is still the device's latest action, else "superseded by X"; expiry checked on tap                                          |
| Failure      | Rediscover + retry 3x over 60s, then loud msg to creator with [Retry], ignores quiet hours                                                                                                                                                                                                                         |
| Create UX    | Tap-through wizard, one message edited in place (same pattern as /keyboard and /user): device > action > AC picker > weekly/once > time > days > preview [Save][Back][Cancel]. Plus one-line slash `/schedule add bedroom on 23:00 sun-thu cool 22`. `/schedule` menu: [+ New][My schedules][Tonight] (2026-10-10) |
| AC picker    | One card: [-] temp [+], fan row (auto 1-4 quiet), vane row (auto 1-5 swing), Powerful toggle, [Done][Reset to preset]; starts from the room preset (2026-10-10)                                                                                                                                                    |
| Time entry   | Quick picks (21:30-00:00, 07:00, 07:30) + [Type a time]: next text from that user is parsed (23:00, 11pm, 2330) (2026-10-10)                                                                                                                                                                                       |
| Timer button | [Timer] on each AC keyboard: Off in 30m/1h/2h/3h, [At a time...] -> time step; result message has [Undo] (2026-10-10)                                                                                                                                                                                              |
| Drafts       | Wizard draft kept in memory keyed by chat + message, 15 min TTL; after a restart the next tap says "draft expired, start again" (callback data is only 64 bytes) (2026-10-10)                                                                                                                                      |
| IDs          | Short (`s7f3a` weekly, `t91c2` timer) so users, buttons, and the LLM can reference them                                                                                                                                                                                                                            |
| Future       | `only_if` reserved (null) for presence conditions                                                                                                                                                                                                                                                                  |

`schedules.json` shape:

```json
{
  "version": 1,
  "schedules": {
    "s7f3a": {
      "id": "s7f3a", "label": "Bedtime", "device": "bedroom_ac",
      "action": {"kind": "state", "state": {"power": true, "mode": "cool", "temp": 22, "fan": 4, "vane": "swing", "powerful": false}},
      "when": {"kind": "weekly", "time": "23:00", "days": ["sun", "mon", "tue", "wed", "thu"]},
      "enabled": true, "paused_until": null, "skip_dates": [], "only_if": null,
      "created_by": 123456, "created_at": "...", "updated_by": 123456, "updated_at": "...",
      "last_fired": {"at": "...", "result": "ok"}
    }
  }
}
```

### Adversarial review outcomes (2026-10-09, all adopted unless noted)

| #  | Risk                                                         | Fix                                                                                                                                                                       |
|----|--------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1  | Pi has no RTC; stale clock at boot                           | In code (bot runs in Docker; Ubuntu 16.04 has no systemd-time-wait-sync): won't fire until clock >= last saved timestamp. Pi runs `ntp`                                   |
| 2  | `arbitrary_callback_data` is in-memory; reboot kills buttons | Compact string callback data `sch:skip:s7f3a:r12` (< 64 B). REPLACES the B18 plan                                                                                         |
| 3  | Full-state frame resurrects an AC someone turned off         | Typed steps (see Step intent)                                                                                                                                             |
| 4  | Retrying a toggle capture can double-toggle                  | Only idempotent AC state retried; toggles retried only on connect error, never on timeout                                                                                 |
| 5  | Stale retry lands after a newer action                       | Per-device action sequence; newer action cancels older pending retries                                                                                                    |
| 7  | Notification fatigue -> muted bot -> missed failures         | Fire msgs always silent                                                                                                                                                   |
| 9  | Old bot + v2 on one token -> 409 Conflict                    | Superseded: no test bot; v1 is stopped at cutover (D7) before v2 starts                                                                                                   |
| 10 | Clock jumps double-fire / drift with per-job JobQueue        | Own tick loop (sleep until next due, max 60s); occurrence key `s7f3a@2026-10-09T23:00`; never fire <= last fired. Same path does startup missed detection. NO `run_daily` |
| 11 | Stale cards / LLM read-then-write races                      | `rev` per schedule; writes carry rev; mismatch -> show current version                                                                                                    |
| 12 | Midnight ambiguity in skip / pause                           | Skip stores fire date, rendered "Fri night (Sat 01:00)"; `paused_until` = resume datetime, always echoed back                                                             |
| 13 | Edit past tonight's time                                     | No silent catch-up: "22:30 already passed tonight [Run now][From tomorrow]"                                                                                               |
| 14 | Creator removed -> orphan schedules                          | Reassign to the remover, listed in the delete confirm                                                                                                                     |
| 15 | Cross-person conflicts warn only the creator                 | Also notify the other schedule's owner                                                                                                                                    |
| 16 | Conflict heuristic too weak                                  | Simulate each device's next 7 days: clashes, redundant steps, "on but never off". Powers `/schedule tonight` agenda                                                       |
| 17 | Device renamed/removed                                       | Mark schedule broken, notify creator, never crash                                                                                                                         |
| 18 | SD power loss                                                | fsync file + dir, keep `.bak`, load `.bak` on parse failure + loud alert                                                                                                  |
| 19 | LLM two-phase writes                                         | `schedule.plan(...)` -> normalized + next 3 runs + conflicts + plan_id bound to rev; `schedule.apply(plan_id)`. Wizard uses same preview card                             |
| 20 | Undo for edits                                               | `schedule_events` JSONL (actor, surface slash/button/llm/scheduler, before/after); `schedule.revert(event_id)`                                                            |
| 21 | LLM guardrails                                               | Labels unique per device, length cap + escaping; max 50 schedules / 10 timers per device                                                                                  |
| 22 | Vacation                                                     | Bulk `pause(filter=all|device, until)`                                                                                                                                    |

Deferred ideas: routines (named relative step sequences, e.g. Sleep = on 22 now, 24 at +3h, off at +7h;
user deferred 2026-10-09), Daikin in-frame off-timer as Pi-dead backup (needs capture to verify),
SG public-holiday skip flag.

Service API (shared by slash, buttons, scheduler, later LLM tools):
`schedule.plan / apply / list / get / update / delete / pause(until?, filter?) / resume / skip(date|next) / unskip / revert(event_id) / agenda(range)`,
each returning `Result{ok, data, warnings[], conflicts[]}`; writes carry `rev`.

## Phase 3 decisions (2026-10-09)

| Topic          | Decision                                                                                                                                                   |
|----------------|------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Target         | Mitsubishi Electric 144-bit, both rooms (live `joh_devices.json`). Daikin codes kept, no encoder                                                           |
| Controls       | Cool mode only; temp 16-31; fan auto/1-4/quiet (protocol has 4 speeds + quiet, corrected from 1-5); up/down vane auto/1-5/swing; powerful. No dry/fan-only |
| "On"           | Fixed room preset = the captured frame's state (bedroom 22C fan 3 vane auto, office 23C fan 3 vane 1)                                                      |
| UI             | None new. Existing On/Off/Powerful buttons go through the encoder. State picker arrives with Phase 2. `/status` shows the last state sent per AC           |
| Buttons        | On = preset (decoded from the captured power_on); Powerful = preset + powerful + fan auto (as captured); Off = last sent state, power and powerful off     |
| Kill switch    | `AC_ENCODER=off` in `.env` + restart: ACs send captured codes again                                                                                        |
| Hardware check | Skipped. Golden tests gate the build; user tests by use after cutover and reports odd behaviour                                                            |
| Run            | Full auto through Phase 3 incl. shipping the image to the Pi; stop before cutover (D7)                                                                     |
| Cutover        | After Phase 3                                                                                                                                              |

Decoded Mitsubishi captures (18 bytes, sent twice, checksum = sum(b0..b16) & 0xFF):

```
bedroom on (fn18ve-22):  23 cb 26 01 00 20 18 06 36 43 00 00 00 00 00 00 00 cc
bedroom off:             23 cb 26 01 00 00 18 06 36 43 00 00 00 00 00 00 00 ac
bedroom powerful:        23 cb 26 01 00 20 18 06 36 40 00 00 00 00 00 08 00 d1
office on (fn18ve-23):   23 cb 26 01 00 20 18 07 36 4b 00 00 00 00 00 00 00 d5
```
b5 power (20 on), b6 mode (18 cool), b7 temp-16, b8 36 (cool + wide vane centre), b9 fan bits 0-2 /
vane bits 3-5 / 0x40 flag, b15 powerful (08). Uncaptured bit meanings from IRremoteESP8266 `ir_Mitsubishi.cpp`.

## Aircon IR decode (commands.json)

All captures are full-state frames (AC remotes send the whole state every press); checksums = sum & 0xFF.

| Remote                   | Protocol                          | Notes                                                                                                                      |
|--------------------------|-----------------------------------|----------------------------------------------------------------------------------------------------------------------------|
| daikin super-multi-nx    | Daikin 280-bit, 3 frames 8+8+19 B | F3 b5 = mode<<4 \| power (cool 3, dry 2, fan 6), b6 = temp*2, b8 = fan<<4 \| swing(0xF), b13 = powerful. F2 = remote clock |
| daikin arc433a22         | Daikin 280-bit, 2 frames 8+19 B   | on/off only, cool 26C                                                                                                      |
| daikin brc4c152          | Daikin176, 7+15 B                 | `temp_up` == `temp_down` byte-identical (both 27C)                                                                         |
| mitsubishi fn18ve-22/-23 | Mitsubishi Electric 144-bit x2    | "-22"/"-23" are 22C/23C presets of the same remote                                                                         |

Decoded bedroom captures: power_on = cool 21C fan4 swing on; power_on_high = cool 22C fan5 swing off;
power_on_low = cool 22C fan2 swing on; power_on_dry = dry auto; powerful = cool 22C + powerful;
toggle_swing = cool 21C swing OFF (absolute); toggle_fan = FAN mode 25C + powerful.
Encoder reference: IRremoteESP8266 `ir_Daikin.cpp`; packing via broadlink 0.19's exposed IR conversion functions.

## Library notes

- PTB 22.8 (2026-06, Bot API 10.0): `sendMessageDraft` streaming (9.3/9.5), button `style` colors (9.4),
  `date_time` entity (9.5), private-chat topics (9.3/9.4), JobQueue, `arbitrary_callback_data`,
  reactions, `concurrent_updates`, `AIORateLimiter`.
- broadlink 0.19.0 (2024-04, latest): exposed IR/RF conversion functions, `ip_address` direct setup,
  RM4 ids. 0.18 renamed `device.device` -> `Device`.

## Speed

Phase 1 removes the main costs: sequential PTB 13 dispatcher + `time.sleep`, 5s broadcast discovery,
hex->bytes on every send. Add reaction ack, edit-in-place keyboards. Measure with Phase 6 timings.
Target: < 300 ms slash -> IR send.

## LLM: open planning items (deeper session before any build)

Draft design L1-L12 discussed: fast-path parser first, typed tools with enum devices, two-phase writes
with code-rendered confirm cards expiring after TTL, static system prompt for prompt-cache hits, 6-turn
memory with 10-min idle reset, structured time output validated in code, max 3 rounds / 20s / no thinking,
cached `/health` probe (1s) so a dead tunnel fails fast, 1 slot max on the shared llama-server, streaming
via `sendMessageDraft`, ~60-utterance English + Singlish eval set, confirm-outcome telemetry.
Still to decide: fast-path grammar scope, exact tool list, memory, Singlish coverage, evals, tunnel
failure UX, voice notes.

## Pending before build

- Pi facts (user to paste): `uname -m; cat /etc/os-release; python3 --version; ldd --version | head -1; df -h /; ps aux | grep main.py`
- Branch name + whether to push: DECIDED 2026-10-09 -- push the `v2` branch (no merge) at deploy step D4
- Test bot token: DROPPED 2026-10-09 -- user chose to test by upgrading the real bot in place (Phase D in TASKS.md)
- Tunnel: DECIDED 2026-10-09 -- set up at cutover (D7), not with Phase 4; Pi key restricted to port 8001 forwarding
