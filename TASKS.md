# iot-bot v2 tasks

Native Claude Code task tools were not available in the 2026-10-09 session, so tasks are tracked here.
Source of truth for decisions: `PLAN.md`. Build order: 1, 3, 2, 5, 6, 7 (4 = LLM, after planning).
Each numbered task gets a reviewer subagent before it is marked done.

Status: `[ ]` pending, `[~]` in progress, `[r]` built, in review, `[x]` done + reviewed

## Phase 1 -- Foundation (done 2026-10-09, awaiting user test with a test bot token)

| #    | Status | Task                                                                                                  |
|------|--------|-------------------------------------------------------------------------------------------------------|
| 1.1  | [x]    | Scaffold: uv + pyproject (py3.13, PTB 22.8, broadlink 0.19, python-dotenv), `.env` config (B16), gitignore real data + `*.example.json` (B23), pytest |
| 1.2  | [x]    | Storage: atomic JSON store (tmp + fsync file/dir + rename, `.bak`, load `.bak` on parse failure), monthly JSONL event log |
| 1.3  | [x]    | Device model: load devices.json + commands.json, pre-decoded bytes, data-driven feature allowlist (B2, B17), duplicate ids rejected (B19), per-device load errors never drop a room (B10) |
| 1.4  | [x]    | Broadlink hub: async wrapper, per-RM lock, discover by MAC, per-device try at startup, rediscover + retry on failed send (B9), any RM with `send_data` (B7), idempotent-only retry, SP2/SP4 plugs |
| 1.5  | [x]    | Service layer: `Result{ok,data,warnings,conflicts,error}`, DeviceService.run (actor + surface, device_events JSONL), UserService (auth by id B5, add/delete with rechecks B13) |
| 1.6  | [x]    | Telegram layer on PTB 22 async: /start /ping /status /list /keyboard /on /off /d /user /adduser, compact string callback data (B18), HTML escaping (B11), error handler (B15), UI nits (B26-28) |
| 1.7  | [x]    | Entry point + README rewrite for Pi/.env/tunnel (B22) + systemd unit example + local smoke run |

## Later phases (coarse; split into numbered subtasks when started)

| #   | Status | Task                                                                                       |
|-----|--------|--------------------------------------------------------------------------------------------|
| 3   | [ ]    | AC state engine: per-AC state, Daikin 280-bit encoder, golden tests vs captures, `/ac` + inline remote, 10-state hardware check |
| 2   | [ ]    | Scheduling per PLAN.md "Scheduling design" + adversarial review outcomes                    |
| 5   | [ ]    | TTL prompts (non-LLM): run-time nudge, pre-schedule heads-up, `/prefs`                      |
| 6   | [ ]    | Telemetry: `turns` JSONL, `/stats` p50/p95                                                  |
| 7   | [ ]    | Weekly recommendations from device_events, hours-run + kWh/SGD                             |
