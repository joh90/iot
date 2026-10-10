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

Pi facts (D2): `johrasp.lan` 192.168.86.48, Pi 3 Model B, 1 GB RAM, Ubuntu 16.04 armhf (32-bit), glibc 2.23,
kernel 4.4 (2018), Docker 20.10.7 (last apt build for xenial), libseccomp 2.5.1, user `johnson` (uid 1001,
docker group, sudo needs a password). v1: `johiot.service` as root, `~/iot` at master ad25a7f, data in
`~/joh_devices.json` + `~/joh_users.json`, token in the unit's ExecStart (visible in `ps`). Disk: 15 GB free
after removing the stopped Pi-hole container + junk (`~/etc-pihole`, `~/etc-dnsmasq.d` kept: root-owned).

Why Docker: glibc 2.23 is too old for cryptography's armv7 wheel (needs 2.31) and Rust builds on 1 GB are not viable.
Docker 20.10.7 blocks clone3 with EPERM, so glibc 2.34+ images cannot start threads. Fix (proven on the Pi):
`deploy/seccomp-clone3.json` = moby v20.10.7 default profile + `clone3 -> SCMP_ACT_TRACE` (no tracer, so ENOSYS,
glibc falls back to clone). `errnoRet` is not honoured by this runc, hence TRACE.

| #   | Status | Task                                                                                                                                                                                                                                                         |
|-----|--------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| D1  | [x]    | Key login works as `johnson@johrasp`                                                                                                                                                                                                                         |
| D2  | [x]    | Read-only inspection (facts above)                                                                                                                                                                                                                           |
| D2a | [x]    | Cleanup: Pi-hole container + image, `.part`, duplicate ASOT webm, ngrok, Youku cookies, pihole.sh (99 MB -> 15 GB free)                                                                                                                                      |
| D3  | [x]    | Runtime: Docker on the existing OS, custom seccomp profile (threads, clock, DNS, to_thread verified)                                                                                                                                                         |
| D4  | [x]    | Docker packaging in repo: Dockerfile (python:3.13-slim, uv, gcc for cffi in a build stage, uid 1001), `deploy/seccomp-clone3.json`, run script, `--check` + hub-only discovery mode, log cap; reviewer                                                       |
| D5  | [x]    | Push `v2` branch to GitHub (branch only, no merge to master)                                                                                                                                                                                                 |
| D6  | [x]    | Image cross-built on the PC (`deploy/docker.sh ship`, Pi Docker cannot build with the profile) and loaded on the Pi; `discover` in the container on the Pi: 2/2 RMs online in 10s. v2 not started (cutover deferred)                                         |
| D7  | [ ]    | NEXT (decided 2026-10-10, before Phase 2 ships): cutover (user runs `sudo systemctl disable --now johiot` + puts the token in `data/.env`), `deploy/docker.sh start`, smoke test; then a few days of using the AC buttons                                    |
| D8  | n/a    | DROPPED 2026-10-09: user keeps the current token (it stays visible in `ps` and the v1 unit until cutover)                                                                                                                                                    |
| D9  | [ ]    | Pi -> LLM tunnel on the host (not a container): `sudo apt-get install autossh` (user; 1.4e-2 is in the Pi's apt lists), new ed25519 key on the Pi restricted here to 127.0.0.1:8001 forwarding, started by `@reboot` cron (decided 2026-10-10), curl /health |

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

| #    | Status | Task                                                                                                                                                                    |
|------|--------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 2.0  | [x]    | Store fix: `write_json_atomic` backs up the current file only if it parses AND passes `validate`; reviewed                                                              |
| 2.1  | [x]    | Schedule model + store: `schedules.json`, short ids, `rev`, limits (50 / 10 timers per device), AC action = AcState dict, typed steps on/off/adjust                     |
| 2.2  | [ ]    | Time engine: weekly/once in Asia/Singapore, skip dates (fire-date semantics), paused_until, occurrence keys, pruning                                                    |
| 2.3  | [ ]    | Scheduler loop: own tick (sleep to next due, max 60s), never fire <= last fired, clock guard, startup missed detection, per-device action sequence, AC retry 3x/60s     |
| 2.4  | [ ]    | Schedule service: plan/apply, list/get/update/delete, pause(filter, until)/resume, skip/unskip, `schedule_events` + revert, 7-day conflict simulation, agenda           |
| 2.5  | [ ]    | Notifications: silent fire msg [Undo][Skip next][Pause] (10 min undo, superseded check), loud failure [Retry], edits-by-others, conflicts to both owners, broken device |
| 2.6  | [ ]    | AC picker card (+/- temp, fan, vane, Powerful), reusable                                                                                                                |
| 2.7  | [ ]    | `/schedule` wizard: menu, steps, quick-pick + typed time, preview, conflict choice, "already passed tonight"                                                            |
| 2.8  | [ ]    | AC [Timer] fast path: Off in 30m/1h/2h/3h / At a time                                                                                                                   |
| 2.9  | [ ]    | One-liner `/schedule add ...` + `/schedule tonight`                                                                                                                     |
| 2.10 | [ ]    | User removal reassigns their schedules to the remover                                                                                                                   |
| 2.11 | [ ]    | Docs + ship to the Pi (after D7 + AC use)                                                                                                                               |

## Later phases (coarse; split into numbered subtasks when started)

| # | Status | Task                                                                   |
|---|--------|------------------------------------------------------------------------|
| 3 | [x]    | AC state engine (done 2026-10-09, see Phase 3 table above)             |
| 2 | [~]    | Scheduling (see Phase 2 table above)                                   |
| 5 | [ ]    | TTL prompts (non-LLM): run-time nudge, pre-schedule heads-up, `/prefs` |
| 6 | [ ]    | Telemetry: `turns` JSONL, `/stats` p50/p95                             |
| 7 | [ ]    | Weekly recommendations from device_events, hours-run + kWh/SGD         |
