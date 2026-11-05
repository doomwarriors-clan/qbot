# qbot

Discord-Bot für die Doomwarriors-Clan-Server (doomwarriors.de): Zeigt den
Live-Status der Doomwarriors Quake-3-Server, liefert Gesamt-Statistiken zu Waffen und
meldet Matches live (Verbindungen, Match-Start/Pause/Ende) im Discord-Server.

**Führende Quelle (leading source):** dieses GitHub-Repo
(`doomwarriors-clan/qbot`). 

## Funktionen

### Befehle (Präfix `!`)

| Befehl | Beschreibung |
|---|---|
| `!q3` | Überblick über alle konfigurierten Server, qstat style (`alias \| Spieler/Max \| Map`), offline: `— \| offline` |
| `!q3 <alias>` | Live-Status eines Servers: Map, Spielerzahl, Punktestand (TDM: Teams + Scoreboard) |
| `!q3 <host:port>` bzw. `!q3 <ip:port>` | Live-Status eines beliebigen Servers |
| `!alias` | Liste aller konfigurierten Server-Aliase (s.u.) |
| `!statsall` | Aggregierte Stats von doomwarriors.de: Spieler, Kills, Schaden, Schusszahl pro Waffe … |
| `!help` | Diese Übersich |

### Automatische Meldungen

- **`#server-info`** — der Bot pollt alle konfigurierten Server alle 10 Sekunden:
  - **Verbindungsmeldung**: Erscheint ein neuer Spieler, wird er in `#server-info`
    angekündigt (mit Rollen-Mentions / Highlight aus `announce_role_names.txt`).
  - **Match in progress**: Live-Embed (Map, Stand, verbleibende Zeit), das bei jedem
    Poll in-place bearbeitet wird. Eine OSP-`timeout`-Pause wird als
    „**Match paused**“ angezeigt; die Uhr bleibt eingefroren.
  - **Match-Ende**: Endergebnis mit Sieger (bei Teamgames inkl. MVP).
- **`#welcome-area`** — neue Mitglieder werden automatisch begrüßt (Text aus
  `welcome_message.txt`, `{user}` wird durch den Mention ersetzt).

## Server-Konfiguration

Die Serverliste steht oben in `qbot.py` (Dict `aliases`):

```python
aliases = {
    'duel': 'doomwarriors.de:27960',
    'tdm':  'doomwarriors.de:27961',
    'ctf':  'doomwarriors.de:27962',
}
```
TODO: Server in Datei auslagern ...

Zur Konfiguration gehören außerdem:

- `SERVER_INFO_CHANNEL_NAME` / `WELCOME_CHANNEL_NAME` in `qbot.py` — die Namen der
  Discord-Kanäle für Ankündigungen bzw. Begrüßung.
- `announce_role_names.txt` — Rollen, die bei Verbindungsmeldungen mitgetaggt werden
  (eine Rollenname pro Zeile).
- `welcome_message.txt` — Begrüßungstext für neue Mitglieder.

## Installation & Betrieb

Der Bot kann als **systemd-User-Service** auf einem Server mit Internetzugang laufen. Die Unit
(`qbot.service`) erwartet das Bot-Verzeichnis unter `~/qbot` mit `.venv` und `.env`
dort.

### `.venv` (NICHT Teil des Repos)

Das Python-Virtualenv ist **nicht** Teil dieses Repos und wird einmalig auf der
Laufmaschine angelegt (der Bot nutzt Python 3.13; die Unit ruft
`.venv/bin/python3.13` direkt auf):

```bash
cd ~/qbot
python3.13 -m venv .venv
.venv/bin/pip install "discord.py~=2.7" dnspython numpy
```

Abhängigkeiten: `discord.py` (Discord-API), `dnspython` (Hostnamenauflösung, der Bot
nutzt Cloudflare-DNS 1.1.1.1), `numpy` (Zahlformatierung).

### `.env` (Secret, NICHT Teil des Repos)

Der Discord-Bot-Token lebt **nur** in `.env` (Modus 0600) — niemals im Repo,
nicht in `ps`/argv. Das Token wird von der Unit über `EnvironmentFile=` als
Umgebungsvariable injiziert. Vorlage: `.env.example`.

```bash
cp .env.example .env
chmod 600 .env
# DISCORD_TOKEN=<token aus der Discord-Entwicklerkonsole> eintragen
```

### systemd-User-Service

Die Unit-Datei liegt im Repo (`qbot.service`); Installation:

```bash
install -m 644 qbot.service ~/.config/systemd/user/qbot.service
systemctl --user daemon-reload
systemctl --user enable --now qbot
journalctl --user -u qbot -f          # Logs
```

Nützliche Befehle:

```bash
systemctl --user restart qbot         # Neustart (nach Updates)
systemctl --user status qbot          # Zustand
sudo loginctl enable-linger <user>    # einmalig: Bot überlebt Reboots ohne Login
```

> **Status (November 2026):** Der Clan-Server kann derzeit noch nicht direkt von
> GitHub klonen (Deploy Keys in der Org `doomwarriors-clan` deaktiviert).
> Bis auf Weiteres wird der Bot daher manuell aktualisiert: aktuelles `qbot.py`
> auf den Server kopieren, dann `systemctl --user restart qbot`.

## Dateien

| Datei | Beschreibung |
|---|---|
| `qbot.py` | Der Bot: Befehle, Poller (server-info), Welcome-Event |
| `qbot.service` | systemd-User-Unit (kanonische Kopie) |
| `.env.example` | Vorlage für den Token (das echte `.env` ist gitignored) |
| `announce_role_names.txt` | Rollen für Verbindungsmeldungen |
| `welcome_message.txt` | Begrüßungstext für neue Mitglieder |
| `debug_render.py` | Werftool: zeigt rohe TDM-Daten + Build-Teams-Ausgabe (nutzt den echten `qbot.py`-Code) |
