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

| #    | Status | Task                                                                                                  |
|------|--------|-------------------------------------------------------------------------------------------------------|
| D1   | [x]    | Key login works as `johnson@johrasp`                                                                  |
| D2   | [x]    | Read-only inspection (facts above)                                                                    |
| D2a  | [x]    | Cleanup: Pi-hole container + image, `.part`, duplicate ASOT webm, ngrok, Youku cookies, pihole.sh (99 MB -> 15 GB free) |
| D3   | [x]    | Runtime: Docker on the existing OS, custom seccomp profile (threads, clock, DNS, to_thread verified)  |
| D4   | [ ]    | Docker packaging in repo: Dockerfile (python:3.13-slim, uv, gcc for cffi in a build stage, uid 1001), `deploy/seccomp-clone3.json`, run script, `--check` + hub-only discovery mode, log cap; reviewer |
| D5   | [ ]    | Push `v2` branch to GitHub (branch only, no merge to master)                                          |
| D6   | [ ]    | Install beside v1: clone to `~/iot-bot-v2`, data files copied into `~/iot-bot-v2/data`, `.env` mode 600, `docker build` on the Pi, `--check`, discovery of both RMs (no IR) while v1 keeps running |
| D7   | [ ]    | Cutover: stop + disable v1 (needs sudo), start v2, smoke test (/ping /status /keyboard + 1 IR the user picks) |
| D8   | [ ]    | Rotate the bot token in BotFather (old one is in `ps`, the v1 unit and a session log)                 |
| D9   | [ ]    | Pi -> LLM tunnel: Pi key restricted on this PC to forwarding 127.0.0.1:8001, autossh, curl /health    |

Rollback: stop the v2 container, re-enable `johiot` (v1 dir and data untouched). After D8, rollback also needs the
new token in the v1 unit.

## Later phases (coarse; split into numbered subtasks when started)

| #   | Status | Task                                                                                       |
|-----|--------|--------------------------------------------------------------------------------------------|
| 3   | [ ]    | AC state engine: per-AC state, Daikin 280-bit encoder, golden tests vs captures, `/ac` + inline remote, 10-state hardware check |
| 2   | [ ]    | Scheduling per PLAN.md "Scheduling design" + adversarial review outcomes                    |
| 5   | [ ]    | TTL prompts (non-LLM): run-time nudge, pre-schedule heads-up, `/prefs`                      |
| 6   | [ ]    | Telemetry: `turns` JSONL, `/stats` p50/p95                                                  |
| 7   | [ ]    | Weekly recommendations from device_events, hours-run + kWh/SGD                             |
