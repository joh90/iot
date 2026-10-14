# iot-bot

A Telegram bot that controls home devices through Broadlink IR remotes (RM mini 3, RM4,
RM Pro) and Broadlink smart plugs (SP2, SP4). It runs 24/7 on a Raspberry Pi.

v2 rewrite (branch `v2`): python-telegram-bot 22 (async), broadlink 0.19, Python 3.13 via uv.
Design decisions and the roadmap (AC state engine, scheduling, prompts, telemetry) are in
[PLAN.md](PLAN.md); progress is in [TASKS.md](TASKS.md).

## What it does

- `/keyboard` opens a button remote: room > device > action
- `/on <device>`, `/off <device>`, `/d <device> <action>` (e.g. `/d bedroom_ac power on high`)
- `/status` shows uptime, the last action, the last state sent to each aircon, every Broadlink device (online or offline) and config warnings
- `/list` shows rooms, devices and the actions each one has
- `/user` lists approved users with a Remove button; `/adduser <user id> <name>` approves someone
- `/ping` works for anyone and shows their Telegram user id (needed to approve them)
- `/stats [days]` shows how fast the bot answers (p50/p95 per surface, share under 300 ms), the slowest
  remotes, failed sends and who or what sent them, strangers, chat assistant use, and whether the LLM PC
  answers (default 7 days)
- Plain messages in a private chat go to the chat assistant (see below), e.g. "a bit cooler in the
  bedroom", "off the office aircon at 7am weekdays", "is the bedroom aircon on?"

A device only gets the actions whose IR codes were captured in `commands.json`, so every button works.
Only approved Telegram user ids can use the bot; usernames are not checked.

## Files

| File            | Tracked | What it holds                                                                                                                                                                                              |
|-----------------|---------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `.env`          | no      | Bot token and paths (copy `.env.example`)                                                                                                                                                                  |
| `devices.json`  | no      | Your rooms, RM remotes, devices and plugs (`devices.example.json`)                                                                                                                                         |
| `users.json`    | no      | Approved users `{"<telegram id>": "<name>"}` (`users.example.json`)                                                                                                                                        |
| `commands.json` | yes     | Captured IR codes by device type / brand / model                                                                                                                                                           |
| `logs/*.jsonl`  | no      | `device_events-YYYY-MM.jsonl` (every send), `turns-YYYY-MM.jsonl` (every message, tap and schedule fire, with its text and timings), `audit-YYYY-MM.jsonl` (user changes), `schedule_events-YYYY-MM.jsonl` |
| `state/`        | no      | `ac_state.json` (last state sent per aircon); later schedules                                                                                                                                              |

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
cp ~/devices.json data/devices.json && cp ~/users.json data/users.json   # v1 files, same format
# on the PC (see below why not on the Pi):  deploy/docker.sh ship user@pi-host
deploy/docker.sh discover    # finds every RM / plug, prints online/offline, no Telegram, no IR
deploy/docker.sh check       # validates files and the token format, no network (exit 3 = bad data)
deploy/docker.sh start       # runs check first; restarts on crash and at boot; logs capped at 3 x 10 MB
deploy/docker.sh logs
```

- `data/` holds `.env`, `devices.json`, `users.json`, `state/` and `logs/`. The container runs as
  uid 1001, so the folder must be writable by that uid (on this Pi that is the `pi` user).
- `commands.json` is baked into the image (`COMMANDS_PATH=/app/commands.json`); rebuild after changing it.
- Build on the PC, not the Pi: Docker 20.10.7 cannot apply a seccomp profile to build steps
  ("does not support setting security options on build"), so `apt-get` and `uv` fail inside
  `docker build` on the Pi. `ship` cross-builds for linux/arm/v7 (PC needs `sudo dnf install
  qemu-user-static-arm`) and streams the image to the Pi with `docker save | ssh docker load`.
- `deploy/seccomp-clone3.json`: Docker 20.10.7's default seccomp profile blocks `clone3` with EPERM,
  so images with glibc 2.34+ fail with "can't start new thread". The profile is the stock v20.10.7
  profile plus `clone3 -> SCMP_ACT_TRACE`, which returns ENOSYS when no tracer is attached, so glibc
  falls back to `clone`. (`errnoRet` is not honoured by that Docker's runc.) Not needed on Docker 20.10.10+.
- Update: on the PC `deploy/docker.sh ship user@pi-host`, then on the Pi `git fetch origin && git checkout origin/v2 && deploy/docker.sh restart`,
  then `docker image prune` now and then (each build keeps an `iotbot:<rev>` tag on the SD card).
- `stop` / `restart` delete the container's docker logs; the bot's own JSONL logs stay in `data/logs/`.
- Unlike the systemd unit, Docker has no `RestartPreventExitStatus` and no wait for NTP. `start` runs
  `check` first so a bad config never gets a restart loop. After a reboot the container may start before
  the clock is synced (the Pi has no RTC): Telegram TLS fails and is retried until NTP catches up, and
  the first few log timestamps can be wrong.

### 4. Tunnel to the LLM PC (for the chat assistant)

The LLM runs on another machine (llama-server on its `127.0.0.1:8001`). The Pi reaches it with an SSH
tunnel, so the server never listens on the network. The chat assistant and `/stats` use it.

```bash
# on the Pi
ssh-keygen -t ed25519 -N "" -C iotbot-tunnel@pi -f ~/.ssh/iotbot_tunnel
ssh-keyscan -t ed25519 <pc-host> >> ~/.ssh/known_hosts    # check the fingerprint against the PC first
```

On the PC, add the key to `~/.ssh/authorized_keys` so it can only open that one tunnel (no shell, no
other ports, LAN only):

```
restrict,port-forwarding,permitopen="127.0.0.1:8001",from="192.168.1.*",command="/bin/false" ssh-ed25519 AAAA... iotbot-tunnel@pi
```

On the Pi, `~/.ssh/config`:

```
Host iotbot-tunnel
  HostName <pc-host>
  User <pc-user>
  IdentityFile ~/.ssh/iotbot_tunnel
  IdentitiesOnly yes
  BatchMode yes
  StrictHostKeyChecking yes
  ServerAliveInterval 30
  ServerAliveCountMax 3
  ExitOnForwardFailure yes
  LocalForward 127.0.0.1:8001 127.0.0.1:8001
```

and `crontab -e` (`sudo apt install autossh` first). `flock` keeps it to one tunnel; the 5-minute line
restarts it if autossh dies:

```
@reboot AUTOSSH_GATETIME=0 flock -n $HOME/.iotbot-tunnel.lock autossh -M 0 -N iotbot-tunnel >/dev/null 2>&1
*/5 * * * * AUTOSSH_GATETIME=0 flock -n $HOME/.iotbot-tunnel.lock autossh -M 0 -N iotbot-tunnel >/dev/null 2>&1
```

Check: `curl 127.0.0.1:8001/health` on the Pi. The container uses the host network, so it sees the same port.

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

## Aircon state (Mitsubishi)

Mitsubishi Electric aircons (144-bit protocol) are driven by the bot's own encoder instead of the raw
captures. The captured `power_on` is decoded and becomes the room's preset:

- **On** sends the preset (e.g. bedroom cool 22C fan 3 vane auto)
- **Powerful** sends the preset with powerful on and fan auto, as the remote does
- **Off** sends the last state the bot sent, with power off
- **-1C / +1C / Swing / Fan** change one thing in the last state sent and send it in full. They never
  switch an aircon on: if it is off (or the bot has sent nothing yet) they say "Press On first". Each
  tap ends Powerful. Swing off goes back to the room's vane; Fan steps auto, 1-4, quiet.
- **Settings** opens a card ([-] temp [+], fan, vane, Powerful) and **Send now** sends that state,
  switching the aircon on if needed. The aircon's screen shows the last state sent.

Supported: cool mode, 16-31C, fan auto/1-4/quiet, vane auto/1-5/swing, powerful. The last state sent
is kept in `state/ac_state.json` and shown in `/status`; it is what the bot asked for, so it goes stale
if someone uses the physical remote. Each send is logged with its `ac_state` in `device_events`.

If an aircon ignores the bot's frames, set `AC_ENCODER=off` in `.env` and restart: every aircon goes
back to sending its captured codes. Daikin aircons, and any capture the encoder cannot read, always
use the captured codes.

## Schedules and timers

`/schedule` opens a menu: **+ New**, **My schedules**, **Tonight**. New walks through device, action,
repeat on days or just once, time, days, then shows a preview with the next runs before anything is saved.

- **Aircon on** uses a picker card: [-] temp [+], fan, vane, Powerful. It starts from the room preset.
- **Change temp** only changes the temperature, and only if the bot last turned that aircon on; it never
  switches on an aircon someone turned off.
- **Timer** on an aircon's buttons: off in 30m / 1h / 2h / 3h, or at a time. Two taps, with Undo.
- One line instead of buttons:

  ```
  /schedule add bedroom on 23:00 sun-thu cool 22 fan 3 name=Bedtime
  /schedule add bedroom off in 2h
  /schedule add office set 25 14:00 daily
  /schedule tonight      /schedule list      /schedule help
  ```

When a schedule runs, its creator gets a silent message with **Undo** (10 minutes, only if nothing newer
was sent), **Skip next** and **Pause**. Failures and missed runs make a sound; failures offer **Retry**.
Two schedules for the same device within 5 minutes of each other count as a clash: you choose Keep both
or Replace. Every change is logged in `logs/schedule_events-*.jsonl` and can be undone from its message.

Rules worth knowing:

- If the bot was not running at a scheduled time, that run is skipped and reported as missed. Nothing is
  sent late.
- A run in progress is marked before sending, so a restart never sends it twice.
- After boot the Pi has no clock until NTP syncs, so schedules wait for NTP before running (shown in
  `/status`).
- Weekly schedules are capped at 50, timers at 10 per device. Removing a user hands their schedules to
  whoever removed them.
- Schedules live in `state/schedules.json`. A damaged entry is listed as broken and never runs; a
  damaged file is moved aside (`schedules.json.broken-<time>`) and the bot starts with none.

## Retries and safety

- One device failing never stops the bot from starting; `/status` shows which ones are offline.
- Taps and commands sent while the bot was offline are not run when it comes back (Telegram keeps
  them for a day): the sender is told to send again. Messages older than 2 minutes count as offline.
- The bot token is scrubbed from the logs.
- A failed send rediscovers the device by MAC and retries once, but only when a repeat is harmless:
  aircon frames (full state) are safe to resend, while toggles like TV power are never resent after an
  unclear reply, so the TV is not switched on and off again.

## Chat assistant

Approved users can type to the bot in a private chat. A local model on the PC (whatever llama-server
has loaded, thinking off) reads the message and picks tools; code does everything else.

- It handles the managed aircons and their schedules: change settings ("a bit cooler" = 1C below what
  the bot last sent), timers, repeating schedules, edit / pause / resume / delete, and questions such
  as "is the bedroom aircon on?" or "what's scheduled tonight?". Anything else gets a polite no.
- Nothing changes until you tap. Every action shows a card drawn by code, "Do this?" with each line as
  before -> after, and [Do it] [Cancel] for 5 minutes. A newer request replaces your older card. If the
  device or schedule changed since the card was drawn, Do it refuses and you ask again.
- After Do it the card shows each line as ok or FAILED, with [Retry failed] and [Undo] for 10 minutes.
  Undo re-sends the earlier aircon state (or restores the schedule), and refuses a line if something
  newer happened to it.
- What it says about an aircon is what the bot last sent; the physical remote is invisible to it.
- It can look things up in the bot's own logs: "who turned on the bedroom aircon last night?", "when
  did the office timer change?", "what did I ask you about the office?". It sees every aircon change
  and schedule change (who, when, ok or failed) and only your own past messages, never anyone else's.
  Messages that mention a note you told it to forget are hidden (best effort; the usage log on the
  Pi still has the old messages, as it keeps everything).
- It remembers your last 3 messages and its replies for 10 minutes, so "make it 23 and swing" works
  as a follow-up.
- Limits: one message at a time per person, 20 per 10 minutes. If the PC is off or busy you get a
  one-line reply and can use `/keyboard` as usual.
- While the wizard waits for "Type a time", a time you type goes to the wizard; anything else goes to
  the assistant.
- `LLM_ENABLED=off` in `.env` switches it off. `LLM_SLOT` (default 1) is the llama-server slot it
  uses, so slot 0 stays free for the PC owner.

Memory (`/memory` lists it, with a Forget button per item):

- Notes: tell it something lasting ("the big room is the bedroom", "I sleep at 23 with fan quiet")
  and it asks "Remember this?" [Remember] [No]. It decides whether a note is about the house (everyone's
  assistant uses it; anyone can forget it; `/memory` shows who added it) or just you (private). Notes
  are data for the assistant, never rules: text that tries to tell it how to behave is refused.
- Recipes: spell out the steps ("save good night: bedroom 25 fan quiet, office off") and tap Save.
  Saying "good night" later draws the usual card with every step (steps already done are skipped).
- Limits: 30 notes per person, 30 house notes, 10 recipes per person, 200 characters per note. Stored
  in `state/memory.json`; a damaged file is moved aside and memory starts empty.

Outside (`/outside [area]`, or ask in chat: "how's the haze?", "will it rain in Jurong?"):

- Live NEA data from data.gov.sg (no API key): 2-hour, 24-hour and 4-day forecasts, temperature,
  rain, humidity, wind, lightning within 10 km in the last 30 minutes, UV, heat stress, PSI and PM2.5.
- Home is the configured area, with air readings for the east and central regions. Name any of NEA's 47 areas
  (or a region: "PSI in the west") for somewhere else.
- AQI is worked out the way aqicn.org shows it: the US EPA AQI (pre-2024 table) on NEA's latest 1-hour
  PM2.5, or NEA's PM10 / ozone / SO2 / CO sub-index if higher. PSI is NEA's official 24-hour index.
  Bands and the advice line come from code (NEA's advice for healthy people), never from the model.
- Every line says when NEA measured it. Replies are cached 10 minutes and at most 5 calls go to NEA per
  10 seconds. If NEA is down, the last good data is shown with its age, up to 6 hours old.
- `/outside` works without the PC (no LLM). Outside conditions only come up when you ask.

Holidays ("is next Monday a PH?", "next long weekend?", "holidays next year"):

- MOM's public holiday list (with "Observed" Mondays and days such as polling day) from data.gov.sg,
  refreshed monthly in the background and saved in `state/holidays.json`; a built-in 2026-2027 list is the
  fallback. Christmas Eve, New Year's Eve and CNY eve are known as eves, not holidays.
- Long weekends (3+ days off with a holiday) are worked out in code.
- Schedules still run on holidays. A schedule or timer card says when it will run on one, and the
  assistant sees holidays marked on the dates of the coming week.

Every chat turn that reached the model has its full requests and replies in
`logs/llm/llm-YYYY-MM-DD.jsonl` (kept 90 days), to replay against a new model or prompt later. Group chats and strangers never reach the model.

## Usage log

`turns-YYYY-MM.jsonl` has one line per message, button tap or schedule fire: who (strangers too, with
their Telegram name), the raw text or button data, the outcome, time inside the bot (`total_ms`), time
spent sending IR (`ir_ms`) and, for messages, the delay from Telegram (`lag_s`, whole seconds). Lines
link to `device_events` by `request_id`. Nothing is deleted: the raw text is kept on purpose, as real
phrasing to test the LLM assistant against later. The files stay on the Pi and are never committed.

## Coming next

Weekly usage patterns and reminders ("turn on the bedroom aircon?" at your usual time). See PLAN.md.

## Development

```bash
uv sync
uv run pytest -q
```

## License

MIT, see [LICENSE](LICENSE).
