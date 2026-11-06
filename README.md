# qbot

Discord bot for the doomwarriors clan servers (doomwarriors.de): shows the
live status of the doomwarriors Quake 3 servers, provides overall weapon
statistics and reports matches live (connects, match start/pause/end) on the
Discord server.

## Features

### Commands (prefix `!`)

| Command | Description |
|---|---|
| `!q3` | Overview of all configured servers, qstat style (`alias \| players/max \| map`), offline: `— \| offline` |
| `!q3 <alias>` | Live status of one server: map, player count, score (TDM: teams + scoreboard) |
| `!q3 <host:port>` or `!q3 <ip:port>` | Live status of any server |
| `!alias` | List of all configured server aliases (see below) |
| `!statsall` | Aggregated stats from doomwarriors.de: players, kills, damage, shots per weapon … |
| `!help` | This overview |

### Automatic messages

- **`#server-info`** — the bot polls all configured servers every 10 seconds:
  - **Connect message**: when a new player appears, they are announced in
    `#server-info` (with role mentions / highlight from
    `announce_role_names.txt`).
  - **Match in progress**: live embed (map, score, time left), edited in place
    on every poll. An OSP `timeout` pause is shown as "**Match paused**"; the
    clock stays frozen.
  - **Match end**: final result with the winner (for team games including the
    MVP).
- **`#welcome-area`** — new members are greeted automatically (text from
  `welcome_message.txt`, `{user}` is replaced by the mention).

## Server configuration

The server list is at the top of `qbot.py` (dict `aliases`):

```python
aliases = {
    'duel': 'doomwarriors.de:27960',
    'tdm':  'doomwarriors.de:27961',
    'ctf':  'doomwarriors.de:27962',
}
```

TODO: move the servers into a file.

TODO 2: a single config for all settings (highlight, servers, welcome text,
etc.).

The configuration also includes:

- `SERVER_INFO_CHANNEL_NAME` / `WELCOME_CHANNEL_NAME` in `qbot.py` — the names
  of the Discord channels for announcements and greetings.
- `announce_role_names.txt` — roles tagged in connect messages (one role name
  per line).
- `welcome_message.txt` — greeting text for new members.

## Installation & operation

The bot can run as a **systemd user service** on any server with internet
access. The unit (`qbot.service`) expects the bot directory at `~/qbot` with
`.venv` and `.env` in it.

The repo is public, so the server can clone and update it over HTTPS without
any key:

```bash
git clone https://github.com/doomwarriors-clan/qbot.git ~/qbot
cd ~/qbot && git pull                 # later updates
```

### `.venv` (NOT part of the repo)

The Python virtualenv is **not** part of this repo and is created once on the
machine that runs the bot (the bot uses Python 3.13; the unit calls
`.venv/bin/python3.13` directly):

```bash
cd ~/qbot
python3.13 -m venv .venv
.venv/bin/pip install "discord.py~=2.7" dnspython numpy
```

Dependencies: `discord.py` (Discord API), `dnspython` (hostname resolution, the
bot uses Cloudflare DNS 1.1.1.1), `numpy` (number formatting).

### `.env` (secret, NOT part of the repo)

The Discord bot token lives **only** in `.env` (mode 0600) — never in the repo,
not in `ps`/argv. The unit injects the token as an environment variable via
`EnvironmentFile=`. Template: `.env.example`.

```bash
cp .env.example .env
chmod 600 .env
# set DISCORD_TOKEN=<token from the Discord developer portal>
```

### systemd user service

The unit file is in the repo (`qbot.service`); to install:

```bash
install -m 644 qbot.service ~/.config/systemd/user/qbot.service
systemctl --user daemon-reload
systemctl --user enable --now qbot
journalctl --user -u qbot -f          # logs
```

Useful commands:

```bash
systemctl --user restart qbot         # restart (after updates)
systemctl --user status qbot          # status
sudo loginctl enable-linger <user>    # once: the bot survives reboots without a login
```

## Files

| File | Description |
|---|---|
| `qbot.py` | The bot: commands, poller (server-info), welcome event |
| `qbot.service` | systemd user unit (canonical copy) |
| `.env.example` | Template for the token (the real `.env` is git-ignored) |
| `announce_role_names.txt` | Roles for connect messages |
| `welcome_message.txt` | Greeting text for new members |
| `debug_render.py` | Debug tool: shows raw TDM data + build-teams output (uses the real `qbot.py` code) |
