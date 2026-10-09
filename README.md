# iot-bot

A Telegram bot that controls home devices through Broadlink IR remotes (RM mini 3, RM4,
RM Pro) and Broadlink smart plugs (SP2, SP4). It runs 24/7 on a Raspberry Pi.

v2 rewrite (branch `v2`): python-telegram-bot 22 (async), broadlink 0.19, Python 3.13 via uv.
Design decisions and the roadmap (AC state engine, scheduling, prompts, telemetry) are in
[PLAN.md](PLAN.md); progress is in [TASKS.md](TASKS.md).

## What it does

- `/keyboard` opens a button remote: room > device > action
- `/on <device>`, `/off <device>`, `/d <device> <action>` (e.g. `/d bedroom_ac power on high`)
- `/status` shows uptime, the last action, every Broadlink device (online or offline) and config warnings
- `/list` shows rooms, devices and the actions each one has
- `/user` lists approved users with a Remove button; `/adduser <user id> <name>` approves someone
- `/ping` works for anyone and shows their Telegram user id (needed to approve them)

A device only gets the actions whose IR codes were captured in `commands.json`, so every button works.
Only approved Telegram user ids can use the bot; usernames are not checked.

## Files

| File               | Tracked | What it holds                                              |
|--------------------|---------|------------------------------------------------------------|
| `.env`             | no      | Bot token and paths (copy `.env.example`)                  |
| `devices.json`     | no      | Your rooms, RM remotes, devices and plugs (`devices.example.json`) |
| `users.json`       | no      | Approved users `{"<telegram id>": "<name>"}` (`users.example.json`) |
| `commands.json`    | yes     | Captured IR codes by device type / brand / model           |
| `logs/*.jsonl`     | no      | `device_events-YYYY-MM.jsonl` (every action), `audit-YYYY-MM.jsonl` (user changes) |
| `state/`           | no      | Future state files (schedules, AC state)                   |

JSON files are written atomically (temp file, fsync, rename) with a `.bak` of the previous good
version. If a file is damaged after a power cut, the bot loads the `.bak` and says so in `/status`.
If `users.json` and its backup are both unreadable, the bot refuses to start rather than lock everyone out.

## Setup

### 1. Bot token

Create a bot with @BotFather (`/newbot`). While the old bot is still running, use a **separate
test bot**: two programs polling one token fight over messages (409 Conflict).

### 2. Install (Raspberry Pi or any Linux box)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs uv
git clone https://github.com/joh90/iot iot-bot && cd iot-bot
git checkout v2
uv sync --no-dev                                    # downloads Python 3.13 + dependencies
cp .env.example .env                                # then put BOT_TOKEN in it
cp devices.example.json devices.json                # then edit
cp users.example.json users.json                    # then put your own id in it
uv run --no-dev iotbot --check                      # validates files, prints warnings, no network
uv run --no-dev iotbot
```

On a 32-bit OS older than glibc 2.31 (e.g. Ubuntu 16.04 armhf), `cryptography` (needed by broadlink)
has no prebuilt wheel and needs a Rust build. Use Docker instead (section 3b).

Your user id: start the bot, send it `/ping`, put the id in `users.json`, restart.
After that, add people from Telegram with `/adduser`.

### 3. Run as a service

See [deploy/iotbot.service](deploy/iotbot.service). It waits for network and NTP time sync
(the Pi has no hardware clock) and does not restart-loop on a config error.

### 3b. Docker on the Pi (old OS)

For a Pi whose OS is too old for the Python packages (this repo's Pi: Ubuntu 16.04 armhf, Docker 20.10.7).
The image brings Debian 13 + Python 3.13; the host only needs Docker.

```bash
git clone -b v2 https://github.com/joh90/iot iot-bot-v2 && cd iot-bot-v2
mkdir data && cp .env.example data/.env && chmod 600 data/.env   # then put BOT_TOKEN in it
cp ~/joh_devices.json data/devices.json && cp ~/joh_users.json data/users.json   # v1 files, same format
# on the PC (see below why not on the Pi):  deploy/docker.sh ship johnson@johrasp.lan
deploy/docker.sh discover    # finds every RM / plug, prints online/offline, no Telegram, no IR
deploy/docker.sh check       # validates files and the token format, no network (exit 3 = bad data)
deploy/docker.sh start       # runs check first; restarts on crash and at boot; logs capped at 3 x 10 MB
deploy/docker.sh logs
```

- `data/` holds `.env`, `devices.json`, `users.json`, `state/` and `logs/`. The container runs as
  uid 1001, so the folder must be writable by that uid (on this Pi that is the `johnson` user).
- `commands.json` is baked into the image (`COMMANDS_PATH=/app/commands.json`); rebuild after changing it.
- Build on the PC, not the Pi: Docker 20.10.7 cannot apply a seccomp profile to build steps
  ("does not support setting security options on build"), so `apt-get` and `uv` fail inside
  `docker build` on the Pi. `ship` cross-builds for linux/arm/v7 (PC needs `sudo dnf install
  qemu-user-static-arm`) and streams the image to the Pi with `docker save | ssh docker load`.
- `deploy/seccomp-clone3.json`: Docker 20.10.7's default seccomp profile blocks `clone3` with EPERM,
  so images with glibc 2.34+ fail with "can't start new thread". The profile is the stock v20.10.7
  profile plus `clone3 -> SCMP_ACT_TRACE`, which returns ENOSYS when no tracer is attached, so glibc
  falls back to `clone`. (`errnoRet` is not honoured by that Docker's runc.) Not needed on Docker 20.10.10+.
- Update: on the PC `deploy/docker.sh ship johnson@johrasp.lan`, then on the Pi `git fetch origin && git checkout origin/v2 && deploy/docker.sh restart`,
  then `docker image prune` now and then (each build keeps an `iotbot:<rev>` tag on the SD card).
- `stop` / `restart` delete the container's docker logs; the bot's own JSONL logs stay in `data/logs/`.
- Unlike the systemd unit, Docker has no `RestartPreventExitStatus` and no wait for NTP. `start` runs
  `check` first so a bad config never gets a restart loop. After a reboot the container may start before
  the clock is synced (the Pi has no RTC): Telegram TLS fails and is retried until NTP catches up, and
  the first few log timestamps can be wrong.

## devices.json

```json
{
  "bedroom": {
    "mac_address": "780f771abcde",
    "broadlink_type": "RMMINI",
    "ip_address": "192.168.1.50",
    "devices": [
      {"type": 1, "id": "bedroom_ac", "brand": "daikin", "model": "super-multi-nx"}
    ],
    "broadlink_devices": [
      {"id": "bedroom_lamp", "mac_address": "780f77116def", "broadlink_type": "SP2"}
    ]
  }
}
```

- Room names and device ids: letters, digits, `-` and `_`, up to 32 characters, unique across the file.
  A duplicate id is skipped with a warning (the first one wins).
- `mac_address`: with or without colons. Devices are found by MAC, so a new DHCP address is picked up
  automatically. `ip_address` is optional; set it if your router has a DHCP reservation, and the bot
  asks that address directly instead of broadcasting.
- `broadlink_type` for the RM is informational; any Broadlink remote that can send IR works.
- Device `type`: 1 aircon, 2 TV, 3 set-top box, 4 projector, 5 amplifier.
- `brand` + `model` pick the codes in `commands.json` (`commands["<type>"]["<brand>"]["<model>"]`).
- Plugs: `SP2` (power) or `SP4` (power + nightlight).

One bad entry only produces a warning; the rest of the room still loads.

## commands.json and learning codes

Each key is an action name (lowercase, digits, `_`), each value is the Broadlink packet as hex.
`power_on` and `power_off` are listed first; other actions follow file order.
Words in `/d` join with `_`: `/d bedroom_ac power on high` runs `power_on_high`.

To capture a code, find the RM's type, IP and MAC with `uv run python discovery`, then:

```bash
uv run python broadlink_cli --type <type> --host <ip> --mac <mac> --learn
```

The RM's light turns white; press the button on the original remote and paste the printed hex into
`commands.json`. Test with `--send <hex>`.

Known issue: `broadlink_cli --learn` was written for an older broadlink library. On 0.19,
`check_data()` raises instead of returning nothing while it waits, so `--learn` crashes after
about 2 seconds. Learning over Telegram (`/learn`) is on the deferred list in PLAN.md.

Aircon remotes send the whole state with every press (mode, temperature, fan, swing), so a captured
"power on" always means one specific setting. See the IR decode table in PLAN.md.

## Retries and safety

- One device failing never stops the bot from starting; `/status` shows which ones are offline.
- A failed send rediscovers the device by MAC and retries once, but only when a repeat is harmless:
  aircon frames (full state) are safe to resend, while toggles like TV power are never resent after an
  unclear reply, so the TV is not switched on and off again.

## Coming next

Aircon state engine (set any temperature/mode, not just captured presets), schedules and sleep timers,
reminders, usage stats. The LLM assistant (reached over an SSH tunnel to a PC) is still being planned.
See PLAN.md.

## Development

```bash
uv sync
uv run pytest -q
```

## License

MIT, see [LICENSE](LICENSE).
