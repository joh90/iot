# iot-bot v2 tasks

Native Claude Code task tools were not available in the 2026-10-09 session, so tasks are tracked here.
Source of truth for decisions: `PLAN.md`. Build order: 1, 3, 2, 5, 6, 7 (4 = LLM, after planning).
Each numbered task gets a reviewer subagent before it is marked done.

Status: `[ ]` pending, `[~]` in progress, `[r]` built, in review, `[x]` done + reviewed

## Phase 1 -- Foundation (done 2026-10-09, awaiting user test with a test bot token)

| #   | Status | Task                                                                                                                                                                                              |
|-----|--------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 1.1 | [x]    | Scaffold: uv + pyproject (py3.13, PTB 22.8, broadlink 0.19, python-dotenv), `.env` config (B16), gitignore real data + `*.example.json` (B23), pytest                                             |
| 1.2 | [x]    | Storage: atomic JSON store (tmp + fsync file/dir + rename, `.bak`, load `.bak` on parse failure), monthly JSONL event log                                                                         |
| 1.3 | [x]    | Device model: load devices.json + commands.json, pre-decoded bytes, data-driven feature allowlist (B2, B17), duplicate ids rejected (B19), per-device load errors never drop a room (B10)         |
| 1.4 | [x]    | Broadlink hub: async wrapper, per-RM lock, discover by MAC, per-device try at startup, rediscover + retry on failed send (B9), any RM with `send_data` (B7), idempotent-only retry, SP2/SP4 plugs |
| 1.5 | [x]    | Service layer: `Result{ok,data,warnings,conflicts,error}`, DeviceService.run (actor + surface, device_events JSONL), UserService (auth by id B5, add/delete with rechecks B13)                    |
| 1.6 | [x]    | Telegram layer on PTB 22 async: /start /ping /status /list /keyboard /on /off /d /user /adduser, compact string callback data (B18), HTML escaping (B11), error handler (B15), UI nits (B26-28)   |
| 1.7 | [x]    | Entry point + README rewrite for Pi/.env/tunnel (B22) + systemd unit example + local smoke run                                                                                                    |

## Phase D -- Deploy to the Pi in Docker (planned 2026-10-09; in-place upgrade on the real token)

Pi facts (D2): Pi 3 Model B, 1 GB RAM, 32-bit OS (glibc 2.23), Docker 20.10.7, libseccomp 2.5.1.
v1: `johiot.service` as root at master ad25a7f, token in the unit's ExecStart (visible in `ps`).

Why Docker: glibc 2.23 is too old for cryptography's armv7 wheel (needs 2.31) and Rust builds on 1 GB are not viable.
Docker 20.10.7 blocks clone3 with EPERM, so glibc 2.34+ images cannot start threads. Fix (proven on the Pi):
`deploy/seccomp-clone3.json` = moby v20.10.7 default profile + `clone3 -> SCMP_ACT_TRACE` (no tracer, so ENOSYS,
glibc falls back to clone). `errnoRet` is not honoured by this runc, hence TRACE.

| #   | Status | Task                                                                                                                                                                                                                                                                   |
|-----|--------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| D1  | [x]    | Key login works                                                                                                                                                                                                                                                        |
| D2  | [x]    | Read-only inspection (facts above)                                                                                                                                                                                                                                     |
| D2a | [x]    | Cleanup: old containers and files on the Pi (99 MB -> 15 GB free)                                                                                                                                                                                                      |
| D3  | [x]    | Runtime: Docker on the existing OS, custom seccomp profile (threads, clock, DNS, to_thread verified)                                                                                                                                                                   |
| D4  | [x]    | Docker packaging in repo: Dockerfile (python:3.13-slim, uv, gcc for cffi in a build stage, uid 1001), `deploy/seccomp-clone3.json`, run script, `--check` + hub-only discovery mode, log cap; reviewer                                                                 |
| D5  | [x]    | Push `v2` branch to GitHub (branch only, no merge to master)                                                                                                                                                                                                           |
| D6  | [x]    | Image cross-built on the PC (`deploy/docker.sh ship`, Pi Docker cannot build with the profile) and loaded on the Pi; `discover` in the container on the Pi: 2/2 RMs online in 10s. v2 not started (cutover deferred)                                                   |
| D7  | [x]    | Live 2026-10-12 14:04 on iotbot:8d23986 (johiot disabled by user, token copied from the v1 unit), then b4fed93 at 14:43 (2.11)                                                                                                                                         |
| D8  | n/a    | DROPPED 2026-10-09: user keeps the current token (it stays visible in `ps` and the v1 unit until cutover)                                                                                                                                                              |
| D8b | [x]    | 2026-10-12: user ran `sudo dpkg --configure -a` + reinstalled linux-firmware; all packages configured; boot files rebuilt (script/dtb same)                                                                                                                            |
| D9  | [x]    | 2026-10-12: Pi key ~/.ssh/iotbot_tunnel; PC authorized_keys restrict,port-forwarding,permitopen 8001,LAN only,command /bin/false (+ permitlisten localhost:1 on 2026-10-15: no -R); PC host key pinned; shell + port 22 refused                                        |
| D9b | [x]    | 2026-10-12: Pi ~/.ssh/config Host iotbot-tunnel; crontab @reboot + */5 watchdog, flock + autossh -M 0 -N; /health OK from Pi and container                                                                                                                             |
| D10 | [ ]    | Reboot test (user, when home): Pi boots on the rebuilt initrd, iotbot container comes back by itself, `/status` OK, tunnel back (curl 127.0.0.1:8001/health)                                                                                                           |

Follow-up from the 3.1 review (a parseable-but-invalid file could replace a good `.bak`): fixed in 2.0.

Rollback: stop the v2 container, re-enable `johiot` (v1 dir and data untouched). After D8, rollback also needs the
new token in the v1 unit.

## Phase 3 -- AC state engine (rescoped 2026-10-09, full auto, stop before cutover)

| #   | Status | Task                                                                                                                                                                                                                                                                                                            |
|-----|--------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 3.1 | [x]    | AC state model: frozen `AcState(power, mode=cool, temp 16-31, fan auto/1-4/quiet, vane auto/1-5/swing, powerful)`, strict validation (no bool/float/str-digit), `AcStateStore` cache in `state/ac_state.json` (damaged file -> start empty + warning); reviewed                                                 |
| 3.2 | [x]    | Mitsubishi 144-bit encoder + strict decoder (decoded state always re-encodes byte-exact) + Broadlink packet builder (capture-average timings, rounded ticks, frame twice, 0x0d05 end gap) + packet/pulse/frame parsers; reviewed                                                                                |
| 3.3 | [x]    | Golden tests: 6 captures byte-exact both ways, packet header, per-pulse timing vs the real remote (bits <= 100 us, header/gaps <= 160 us), 2 IRremoteESP8266 real-remote vectors. Not backed by a real capture: fan 2/4/quiet, vane 2-5, temps other than 22-24/26 (reachable only from Phase 2); reviewed      |
| 3.4 | [x]    | `AcService`: preset = decoded captured power_on; On = preset, Powerful = preset + powerful + fan auto, Off = last state powered off; per-AC lock across build/send/save; `DeviceService.send_ac_state` for Phase 2; `ac_state` in device_events; `AC_ENCODER=off` kill switch; `/status` aircon lines; reviewed |
| 3.5 | [x]    | Image `iotbot:8d23986` (= latest) shipped to the Pi, Pi clone at 8d23986, `discover` 2/2 online, encoder in the arm image gives the bedroom capture byte-exact. v2 not started, v1 untouched. STOPPED before D7                                                                                                 |

## Phase 2 -- Scheduling (full design, started 2026-10-10)

Design: PLAN.md "Scheduling design" + adversarial review outcomes. Backend first (2.1-2.5), UI after.
Ships (2.11) only after D7 and a few days of using the AC buttons.

| #    | Status | Task                                                                                                                                                                                                                                                                                                      |
|------|--------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 2.0  | [x]    | Store fix: `write_json_atomic` backs up the current file only if it parses AND passes `validate`; reviewed                                                                                                                                                                                                |
| 2.1  | [x]    | Schedule model + store: `schedules.json`, short ids, `rev`, limits (50 / 10 timers per device), AC action = AcState dict, typed steps on/off/adjust                                                                                                                                                       |
| 2.2  | [x]    | Time engine: weekly/once in Asia/Singapore, skip dates (fire-date semantics), paused_until, occurrence keys, pruning; DST-safe for any TZ_NAME, pause searched from its resume time; reviewed                                                                                                             |
| 2.3  | [x]    | Scheduler loop: own tick (sleep to next due, max 60s), never fire <= last fired, clock guard, startup missed detection, per-device action sequence, AC retry 3x/60s; NTP sync via adjtimex (verified on the Pi + container), adjust under the AC lock, no send without the started marker; reviewed twice |
| 2.4  | [x]    | Schedule service: plan/apply, list/get/update/delete, pause(filter, until)/resume, skip/unskip, `schedule_events` + revert, 7-day conflict simulation, agenda; replace only deletes clashes the user saw, edits rebuilt from the stored version, revert by change batch with the same checks; reviewed    |
| 2.5  | [x]    | Notifications: silent fire msg [Undo][Skip next][Pause] (10 min undo, superseded check), loud failure [Retry], edits-by-others, conflicts to both owners, broken device; Retry single-use + checks newer actions + never re-toggles, scheduler stops in post_stop; reviewed                               |
| 2.6  | [x]    | AC picker card (+/- temp, fan, vane, Powerful), reusable; reviewed                                                                                                                                                                                                                                        |
| 2.7  | [x]    | `/schedule` wizard: menu, steps, quick-pick + typed time, preview, conflict choice, "already passed tonight"; double taps safe, typed time waits for one message only; reviewed                                                                                                                           |
| 2.8  | [x]    | AC [Timer] fast path: Off in 30m/1h/2h/3h / At a time; per-device lock, clash re-checked inside the save; reviewed                                                                                                                                                                                        |
| 2.9  | [x]    | One-liner `/schedule add ...` + `/schedule tonight`; room names, in 2h, today/tomorrow; reviewed                                                                                                                                                                                                          |
| 2.10 | [x]    | User removal reassigns their schedules to the remover; orphans adopted on every removal, no rev bump; reviewed                                                                                                                                                                                            |
| 2.11 | [x]    | Shipped b4fed93 to the Pi 2026-10-12 14:43 (user chose not to wait). Next ship carries 6872a03 (wizard labels). Try untested AC states                                                                                                                                                                    |
| 2.12 | [x]    | Offline backlog gate (messages > 2 min old, taps queued at startup) + token scrubbed from logs; from the pre-cutover review of 8d23986                                                                                                                                                                    |
| 2.13 | [x]    | AC quick buttons -1C/+1C/Swing/Fan (last state, one change, never turns on, ends Powerful); AC screen shows last state sent; reviewed                                                                                                                                                                     |
| 2.14 | [x]    | Settings card on each AC: picker + [Send now] (may turn on, says so), back to the AC screen after sending; reviewed                                                                                                                                                                                       |

## Phase 6 -- Telemetry (planned 2026-10-12)

Design: PLAN.md "Phase 6 decisions". No push alerts, no /help notice.

| #   | Status | Task                                                                                                                                                                                               |
|-----|--------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 6.1 | [x]    | `iotbot/telemetry.py`: `Turn` in a contextvar, `TurnLog` -> monthly `turns-YYYY-MM.jsonl`, kept forever                                                                                            |
| 6.2 | [x]    | Application subclass wraps `process_update`: one line per update (crash + offline drop included), lag_s + total_ms                                                                                 |
| 6.3 | [x]    | Fill turns: intent from command/button, device service adds ir_ms + outcome; strangers `denied` (id, name, text); scheduler fires as surface `scheduler`; reviewed                                 |
| 6.4 | [x]    | `/stats [days]` (any approved user, default 7, max 365, in a thread): p50/p95 by surface, lag, % under 300 ms, failures by who, slowest 3 RM/plugs, strangers, offline drops, LLM health; reviewed |
| 6.5 | [~]    | Tests, README, reviewer per task; shipped 3347df5 -- user checks /stats (7 days, 1, 0, abc) on the live bot                                                                                        |
| 6.6 | [ ]    | Speed (after Phase 4): time-until-feedback metric, why one IR send takes ~570 ms, fewer Telegram round trips                                                                                       |

## Phase 4 -- LLM assistant (planned 2026-10-13)

Design: PLAN.md "Phase 4 decisions". Ships in 3 waves, each after a reviewer and the user's OK.

| #    | Status | Task                                                                                                                          |
|------|--------|-------------------------------------------------------------------------------------------------------------------------------|
| 4.0  | [x]    | Spike done 2026-10-13: 460-690 ms full turn from the Pi, 12/12 correct; see PLAN '4.0 spike results'                          |
| 4.1  | [x]    | LLM client: OpenAI-style tool calls, slot 1, health/busy check, 3 rounds / 20 s, strict tool-call validation, LLM_ENABLED     |
| 4.2  | [x]    | Text routing (private chats, wizard rule), per-person lock + rate limit, chat context, tool registry (open for 5/7)           |
| 4.3  | [x]    | Confirm cards + Undo button (code-drawn, stale check by seq/rev, short callback tokens)                                       |
| 4.4  | [x]    | Home tools: home state, AC set/adjust, idempotent features, timers + schedules via wizard Draft                               |
| 4.5  | [x]    | Telemetry + replay log (turn fields, llm JSONL 90 days), /stats LLM section; WAVE 1 ship                                      |
| 4.6  | [x]    | Memory store: facts, house facts, recipes (Save card), tidy on write, guardrails, /memory                                     |
| 4.7  | [x]    | Diary search (events + own messages, forgotten filter); WAVE 2 ship                                                           |
| 4.8  | [x]    | Outside tool + /outside (NEA, AQI as aqicn, bands, cache, throttle, stale data)                                               |
| 4.9  | [x]    | Holidays tool (MOM list, eves, long weekends), PH marks on dates and schedule cards; WAVE 3 ship                              |
| 4.10 | [x]    | Tests throughout (fake LLM server, broken replies, no IR), README, reviewer per task; 914 pass at 202b35e                     |
| 4.11 | [x]    | Live-test fixes: answer + card for mixed asks, already-paused refused, no ids, fresh state, 2 new schedules per card          |
| 4.12 | [~]    | Live-test fixes (202b35e): list before change, said-time guard, 'which aircon?' asked in code, plain cards; user re-test      |
| 4.13 | [x]    | Security leftovers (202b35e): ADMIN_IDS (empty = all equal), private chats only, notices/Undo per recipient, NEA prune        |

## Later phases (coarse; split into numbered subtasks when started)

| # | Status | Task                                                                                          |
|---|--------|-----------------------------------------------------------------------------------------------|
| 3 | [x]    | AC state engine (done 2026-10-09, see Phase 3 table above)                                    |
| 2 | [x]    | Scheduling (shipped 2026-10-12, see Phase 2 table above)                                      |
| 6 | [~]    | Telemetry: shipped 3347df5; user verifies 6.5; 6.6 speed after Phase 4                        |
| 4 | [~]    | LLM: planned 2026-10-13, building (see Phase 4 table above)                                   |
| 7 | [ ]    | Weekly report + hours-run + kWh/SGD; habit nudges ('turn it on?', live); daily brief (opt-in) |
| 5 | [ ]    | TTL prompts (non-LLM): run-time nudge, pre-schedule heads-up; prefs shown in /memory          |
