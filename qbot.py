# Copyright (c) 2022 Andrea Giavatto
#
# Adjusted by dwc.motter with help of qwen3.8 in 2026

# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import discord
import asyncio
import json
import socket
import re
from itertools import zip_longest
from discord.ext import commands
import dns.resolver
import numpy as np
import os
import sys

dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ['1.1.1.1'] #cloudflare dns

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None, description='''A simple bot to query Quake 3 servers (protocol 68)''')

aliases = {
    'duel': 'doomwarriors.de:27960',
    'tdm': 'doomwarriors.de:27961',
    'ctf': 'doomwarriors.de:27962',
}
nameRegex = r'\^+[a-zA-Z0-9]'

def formatPlayerName(raw_name):
    """Turn a raw server player name into its display form.

    1. Strips Quake3 '^x' color codes (e.g. '^FFREEZE^ffreeze' -> 'FREEZEfreeze')
       and any surrounding double quotes.
    2. Collapses the common '<WORD><word>' double-name style down to just the
       first word -- but ONLY when both halves are the same word (one is a
       truncated/cased form of the other):
         'FREEZEfreeze' -> 'FREEZE'    'DALLIdall' -> 'DALLI'
       Names made of two *different* words are left untouched, e.g.
         'KACKspaten' -> 'KACKspaten'   (kack != spaten)
    """
    name = re.sub(nameRegex, '', raw_name).strip('"')
    m = re.match(r'^([A-Z]+)([a-z]+)$', name)
    if m:
        upper, lower = m.group(1), m.group(2)
        u, l = upper.lower(), lower.lower()
        if u == l:
            return upper
        # Same word, different length (e.g. DALLI / dall). Require a 3-char
        # minimum so unrelated short prefixes ('A'/'apple') never collapse.
        if min(len(u), len(l)) >= 3 and (u.startswith(l) or l.startswith(u)):
            return upper
    return name

# --- Server-info connection announcements -------------------------------------
# Every POLL_INTERVAL seconds the bot silently queries every server in `aliases`
# and, whenever a new player appears in a server's player list, posts a message
# to the configured text channel announcing the connection.
SERVER_INFO_CHANNEL_NAME = "server-info"
POLL_INTERVAL = 10

# Roles to tag in connection announcements (read from external file)
ANNOUNCE_ROLE_NAMES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'announce_role_names.txt')


def get_announce_role_names():
    """Read role names from the external config file (one per line).

    Returns an empty list if the file is missing or unreadable.
    """
    try:
        with open(ANNOUNCE_ROLE_NAMES_FILE, 'r', encoding='utf-8') as f:
            names = [line.strip() for line in f if line.strip()]
        return names
    except (FileNotFoundError, IOError) as exc:
        print(f"announce-roles: could not read {ANNOUNCE_ROLE_NAMES_FILE}: {exc}")
        return []

# Tracks the set of connected player names per alias from the previous poll.
# A player is reported as "connected" only when they appear here for the first
# time, so we keep the last seen state per server.
last_connected_players = {}

# Tracks the Score_Time cvar per alias from the previous poll. Used to detect
# when a new match starts (transition from "Waiting for Players" to a timer).
last_score_time = {}

# Tracks the last known serverInfo + playersList while a match was running,
# so we can post the final result even if scores reset on the next poll.
last_match_info = {}

# Stores the live "match in progress" message per alias so we can edit it
# in-place each poll cycle instead of posting a new message every minute.
live_match_messages = {}

# Handle to the background poller task, so it survives re-connections and is
# not garbage-collected. Populated once in on_ready().
server_info_task = None
# -----------------------------------------------------------------------------


def get_server_info_channel():
    """Return the ``#server-info`` text channel, or None if it cannot be found.

    Searches every guild the bot is a member of for a text channel with the
    configured name (see ``SERVER_INFO_CHANNEL_NAME``).
    """
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name == SERVER_INFO_CHANNEL_NAME:
                return channel
    return None


def get_role_mentions(guild):
    """Return a list of mention strings for the configured announcement roles."""
    role_names = get_announce_role_names()
    mentions = []
    for role_name in role_names:
        role = discord.utils.get(guild.roles, name=role_name)
        if role is not None:
            mentions.append(role.mention)
    return mentions


def _parse_alias_target(server):
    """Split an alias value like ``host:port`` into (host, port).

    Mirrors the logic used by the ``!q3`` command: port defaults to 27960 when
    not explicitly given.
    """
    host, sep, port_str = server.partition(":")
    port = int(port_str) if sep and port_str else 27960
    return host, port


async def _query_alias_status(alias, server):
    """Query one alias's server and return the full ``(serverInfo, playersList)`` tuple.

    Returns None when the server could not be reached or its host could not be
    resolved.
    """
    host, port = _parse_alias_target(server)
    if not isValidIp(host):
        resolved = await resolveHost(host)
        if len(resolved) == 0:
            return None
        host = resolved[0]

    status = await getServerStatus(host, port)
    if status is None:
        return None
    return status


def _is_match_running(score_time: str) -> bool:
    """Return True when Score_Time indicates an active match (MM:SS countdown).

    OSP sets Score_Time to a countdown like ``19:32`` while a match is in
    progress and to ``Waiting for Players`` (or similar non-timer text) when
    no match is active.
    """
    return bool(re.match(r'^\d+:\d+$', score_time.strip()))


async def server_info_poller():
    """Background loop that announces player connections and match starts to ``#server-info``.

    Runs forever (the bot is expected to live for a long time). Each cycle it
    queries every alias, compares the current player set against the previous
    cycle's, and posts one message per newly-connected player. It also watches
    the Score_Time cvar to detect when a new match starts (transition from a
    non-timer value to a MM:SS countdown) and posts a ``!q3``-style embed.
    The very first poll per server only records a baseline so players already
    online when the bot starts are not spammed as "connected" and an
    already-running match is not re-announced.
    """
    channel = get_server_info_channel()
    if channel is None:
        print(f"server-info: channel '{SERVER_INFO_CHANNEL_NAME}' not found yet; will keep retrying each poll")

    while True:
        # Re-locate the channel until found (e.g. it may not have been in the
        # guild cache on the very first cycle). Once found we keep using it.
        if channel is None:
            channel = get_server_info_channel()

        for alias, server in aliases.items():
            try:
                status = await _query_alias_status(alias, server)
                if status is None:
                    continue  # server offline / unresolvable this cycle

                serverInfo, playersList = status

                # --- Player connection announcements ---
                current_players = {formatPlayerName(p[2]) for p in playersList}
                previous_players = last_connected_players.get(alias)
                if previous_players is not None:  # skip the first (baseline) poll
                    new_players = current_players - previous_players
                    if new_players and channel is not None:
                        host, port = _parse_alias_target(server)
                        role_mentions = " ".join(get_role_mentions(channel.guild))
                        for name in sorted(new_players):
                            msg = f"**{name}** connected to **{alias}** ({host}:{port})"
                            if role_mentions:
                                msg += f" {role_mentions}"
                            await channel.send(msg)
                last_connected_players[alias] = current_players

                # --- Match status: post every cycle while a match is running ---
                current_score_time = serverInfo.get('score_time', '')
                previous_score_time = last_score_time.get(alias)
                match_running = _is_match_running(current_score_time)
                previous_running = (previous_score_time is not None
                                   and _is_match_running(previous_score_time))

                if match_running and channel is not None:
                    host, port = _parse_alias_target(server)
                    name = re.sub(nameRegex, '', serverInfo['sv_hostname'])
                    currentMap = serverInfo['mapname']
                    embed = discord.Embed(title=name, colour=0x3c9824, type='rich')
                    is_team_game = serverInfo['g_gametype'] in ('3', '4')
                    if is_team_game:
                        customiseEmbedForTeamGame(embed, serverInfo, playersList, currentMap)
                    else:
                        customiseEmbedForGenericGame(embed, serverInfo, playersList, currentMap)
                    # Add remaining time
                    embed.add_field(name="Time remaining", value=current_score_time, inline=True)
                    # First detection gets a "Match started" prefix; subsequent
                    # cycles edit the same message in-place.
                    is_new_match = not previous_running
                    prefix = f"**Match started** on **{alias}**!" if is_new_match else f"**{alias}** update"
                    embed.description = prefix
                    if alias in live_match_messages:
                        await live_match_messages[alias].edit(embed=embed)
                    else:
                        live_match_messages[alias] = await channel.send(embed=embed)
                    # Save state for potential "match ended" post
                    last_match_info[alias] = (serverInfo, playersList)

                elif previous_running and not match_running and channel is not None:
                    # Match just ended — post the final result using saved state
                    saved = last_match_info.get(alias)
                    if saved:
                        saved_info, saved_players = saved
                        host, port = _parse_alias_target(server)
                        name = re.sub(nameRegex, '', saved_info['sv_hostname'])
                        currentMap = saved_info['mapname']
                        embed = discord.Embed(title=name, colour=0x3c9824, type='rich')
                        is_team_game = saved_info['g_gametype'] in ('3', '4')
                        if is_team_game:
                            customiseEmbedForTeamGame(embed, saved_info, saved_players, currentMap)
                        else:
                            customiseEmbedForGenericGame(embed, saved_info, saved_players, currentMap)
                        # Determine winner for a friendly summary
                        if is_team_game:
                            red_score = saved_info.get('score_red', '0')
                            blue_score = saved_info.get('score_blue', '0')
                            red_n = int(red_score) if red_score.isdigit() else 0
                            blue_n = int(blue_score) if blue_score.isdigit() else 0
                            winner = "🔴 Red" if red_n > blue_n else ("🔵 Blue" if blue_n > red_n else "🤝 Draw")
                        else:
                            # Non-team game (duel): winner is the player with the highest score
                            best_name, best_score = '—', -1
                            for p in saved_players:
                                try:
                                    s = int(p[0])
                                except (ValueError, IndexError):
                                    continue
                                if s > best_score:
                                    best_score = s
                                    best_name = formatPlayerName(p[2])
                            winner = f"🏆 {best_name}" if best_score > 0 else "🤝 Draw"
                        await channel.send(f"**Match ended** on **{alias}** — {winner} wins!", embed=embed)
                    del last_match_info[alias]
                    live_match_messages.pop(alias, None)

                last_score_time[alias] = current_score_time

            except Exception as exc:  # keep the poller alive regardless of one server's errors
                print(f"server-info: error polling {alias}: {exc}")

        await asyncio.sleep(POLL_INTERVAL)


@bot.event
async def on_ready():
    print('Logged in as')
    print(bot.user.name)
    print(bot.user.id)
    print('------')
    # Start the connection-announcement poller exactly once, even if Discord
    # fires on_ready again after a reconnect.
    global server_info_task
    if server_info_task is None or server_info_task.done():
        server_info_task = asyncio.create_task(server_info_poller())


# --- Welcome new members -------------------------------------------------------

WELCOME_CHANNEL_NAME = 'welcome-area'
WELCOME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'welcome_message.txt')


@bot.event
async def on_member_join(member):
    """Post a welcome message in the 'welcome-area' channel when a new member joins."""
    channel = next((ch for ch in member.guild.text_channels if ch.name == WELCOME_CHANNEL_NAME), None)
    if channel is None:
        return

    try:
        with open(WELCOME_FILE, 'r', encoding='utf-8') as f:
            message = f.read().strip()
    except (FileNotFoundError, IOError):
        message = "Welcome {user} to the server!"
        print(f"welcome: {WELCOME_FILE} not found, using default message")

    message = message.replace('{user}', member.mention)
    await channel.send(message)
    print(f"welcome: greeted {member} in #{channel.name} of {member.guild.name}")

@bot.command()
async def alias(context):
    embed = discord.Embed(title='', description='', colour=0x3c9824, type='rich')
    sorted_keys = sorted(list(aliases.keys()), key=str.lower)
    names = []
    ips = []
    for key in sorted_keys:
        names.append(key)
        ips.append(aliases[key])
    embed.add_field(name='Alias', value='\n'.join(names), inline=True)
    embed.add_field(name='Hostname', value='\n'.join(ips), inline=True)
    await context.send(embed=embed)

@bot.command()
async def q3(context, argument: str):
    if argument == 'help':
        await context.send('Usage: !q3 <alias> or !q3 ip(:port) or !q3 hostname(:port), type !alias to see a list of known aliases')
        return
    
    queryServer = argument
    for key, value in aliases.items(): # check if alias first
        if key == argument:
            queryServer = value
            break

    # get ip and port
    query = queryServer.strip().split(":")
    ip = query[0]
    port = 27960
    if len(query) > 1:
        try:
            port = int(query[1])
        except ValueError:
            await context.send(f'Invalid port "{query[1]}" — usage: !q3 <alias> or !q3 ip(:port)')
            return
    if not isValidIp(ip):
        resolvedIps = await resolveHost(ip)
        if len(resolvedIps) == 0:
            await context.send("I couldn't resolve that domain")
            return
        else:
            ip = resolvedIps[0]

    status = await getServerStatus(ip, port)

    if status is None:
        await context.send(f'Could not reach that server at {ip}:{port} — it is offline or did not respond within 3 seconds.')
    else:
        serverInfo = status[0]
        playersList = status[1]

        try:
            name = re.sub(nameRegex, '', serverInfo['sv_hostname'])
            currentMap = serverInfo['mapname']

            embed = discord.Embed(title=name, description=ip+':'+str(port), colour=0x3c9824, type='rich')
            is_team_game = serverInfo['g_gametype'] == '3' or serverInfo['g_gametype'] == '4'
            if is_team_game:
                customiseEmbedForTeamGame(embed, serverInfo, playersList, currentMap)
            else:
                customiseEmbedForGenericGame(embed, serverInfo, playersList, currentMap)
            await context.send(embed=embed)
        except Exception as exc:  # malformed/unexpected server payload: report cleanly instead of a traceback
            print(f"q3: failed to build status for {ip}:{port}: {type(exc).__name__}: {exc}")
            await context.send(f'I could not read that server\'s status at {ip}:{port} — its response was malformed or missing expected fields.')

@bot.command()
async def statsall(context):
    """Fetch doomwarriors.de Quake3 stats and post aggregate totals to the channel."""
    import urllib.request

    url = "https://www.doomwarriors.de/quake3/stats.json?bots=true"

    def _fetch():
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception as e:
        await context.send(f"⚠️ Failed to fetch stats: {e}")
        return

    leaderboard = data.get("leaderboard", [])
    human_players = [p for p in leaderboard if not p.get("bot", False)]
    total_kills = sum(p.get("kills", 0) for p in leaderboard)
    total_damage = sum(p.get("damageGiven", 0) for p in leaderboard)
    total_gauntlet = sum(p.get("killsByWeapon", {}).get("Gauntlet", 0) for p in leaderboard)

    total_mg_shots = sum(p.get("weapons", {}).get("MachineGun", {}).get("shots", 0) for p in leaderboard)
    total_shotgun = sum(p.get("weapons", {}).get("Shotgun", {}).get("shots", 0) for p in leaderboard)
    total_rocket = sum(p.get("weapons", {}).get("R.Launcher", {}).get("shots", 0) for p in leaderboard)
    total_plasma = sum(p.get("weapons", {}).get("Plasmagun", {}).get("shots", 0) for p in leaderboard)
    total_shaft = sum(p.get("weapons", {}).get("LightningGun", {}).get("shots", 0) for p in leaderboard)
    total_grenade = sum(p.get("weapons", {}).get("G.Launcher", {}).get("shots", 0) for p in leaderboard)
    total_rail = sum(p.get("weapons", {}).get("Railgun", {}).get("shots", 0) for p in leaderboard)

    entries = [
        ("👥", "Unique Human players", len(human_players)),
        ("⚔️", "Total Kills", total_kills),
        ("🔥", "Total Damage given", total_damage),
        ("🗡️", "Gauntlet kills", total_gauntlet),
        ("🔫", "Machinegun shots", total_mg_shots),
        ("💥", "Shotgun shots", total_shotgun),
        ("🚀", "Rockets", total_rocket),
        ("🌀", "Plasma Shots", total_plasma),
        ("🌩️", "Shaft sparks", total_shaft),
        ("🟢", "Grenades", total_grenade),
        ("🎯", "Railgun slugs", total_rail),
    ]

    def _dw(s):
        w = 0
        for c in s:
            o = ord(c)
            if o == 0xFE0F:
                w += 0
            elif o > 0x1F000 or 0x2600 <= o <= 0x27BF:
                w += 2
            else:
                w += 1
        return w

    labels = [f"{i} {t}" for i, t, _ in entries]
    max_w = max(_dw(l) for l in labels) + 1
    def _fmt(v):
        return f"{v:,}".replace(",", ".")
    desc_lines = [f"{l}{' ' * (max_w - _dw(l))}{_fmt(v)}" for l, (_, _, v) in zip(labels, entries)]

    embed = discord.Embed(
        title="📊 Doomwarriors Quake3 Stats",
        url="https://www.doomwarriors.de/quake3/stats",
        color=discord.Color.dark_teal(),
        description="```\n" + "\n".join(desc_lines) + "\n```",
    )
    await context.send(embed=embed)

@bot.command()
async def help(context):
    """List available commands and how to use them."""
    embed = discord.Embed(
        title='Quake3 Bot — Commands',
        description='Query live Quake 3 servers and pull aggregate Doomwarriors stats.',
        colour=0x3c9824,
    )
    embed.add_field(
        name='!q3 <alias | host[:port] | ip[:port]>',
        value='Live server status — map, player count and scores. Use `!alias` to see known aliases.',
        inline=False,
    )
    embed.add_field(
        name='!alias',
        value='List all configured server aliases and their hostnames.',
        inline=False,
    )
    embed.add_field(
        name='!statsall',
        value='Post aggregate Quake3 stats from doomwarriors.de — players, kills, damage, per-weapon shots.',
        inline=False,
    )
    embed.set_footer(text='The bot also announces new connections and match results automatically in #server-info.')
    await context.send(embed=embed)

def customiseEmbedForTeamGame(embed, serverInfo, playersList, currentMap):
    if len(playersList) > 0:
        playersNumber = str(len(playersList)) + '/' + serverInfo['sv_maxclients']
        # Field 1: full-width, Map + Players combined
        embed.add_field(name='Map', value=f"{currentMap} ({playersNumber})", inline=False)

        if 'players_blue' in serverInfo and 'players_red' in serverInfo:
            players_in_blue_team = serverInfo['players_blue'].split()
            players_in_red_team = serverInfo['players_red'].split()
            team_red = []
            team_blue = []
            team_spec = []
            for index, player in enumerate(playersList):
                if str(index + 1) in players_in_blue_team:
                    team_blue.append(player)
                elif str(index + 1) in players_in_red_team:
                    team_red.append(player)
                else:
                    team_spec.append(player)
        
            # Field 2: full-width, Team Red above Team Blue (Red on the left half, Blue on the right half)
            embed.add_field(name='Teams', value=buildTeamsValue(team_red, serverInfo['score_red'], team_blue, serverInfo['score_blue']), inline=False)
            if len(team_spec) > 0:
                embed.add_field(name='Spectators', value=buildTeamValue(team_spec), inline=False)
        else: # all players in spec
            embed.add_field(name='Spectators', value=buildTeamValue(playersList), inline=False)

def customiseEmbedForGenericGame(embed, serverInfo, playersList, currentMap):
    if len(playersList) > 0:
        playersNumber = str(len(playersList)) + '/' + serverInfo['sv_maxclients']
        embed.add_field(name='Map', value=f"{currentMap} ({playersNumber})", inline=False)
        addPlayersToEmbed(embed, playersList, 'Player')

def addPlayersToEmbed(embed, playersList, teamName):
    sorted = sortPlayersByScore(playersList)

    embed.add_field(name=teamName, value='```\n' + '\n'.join(sorted[0]) + '```' , inline=True)
    embed.add_field(name='Score', value='```\n'  + '\n'.join(sorted[1]) + '```' , inline=True)

def _alignedTeamLines(playersList, name_width, score_width):
    """One line per player with the name left-aligned and the score right-aligned.

    Names are left-justified to name_width (never truncated) and scores
    right-justified to score_width, so every score's last digit lands in the same
    column. Callers pick name_width (at least the longest display name + a gap)
    and score_width (the widest score, and for teams the team total too) so
    players line up under a header. Players are sorted by score descending (ties
    keep insertion order). Returns ['—'] for an empty list so the caller can
    still emit a tidy placeholder.
    """
    sorted_players, sorted_scores = sortPlayersByScore(playersList)
    if not sorted_players:
        return ['—']
    return [name.ljust(name_width) + score.rjust(score_width)
            for name, score in zip(sorted_players, sorted_scores)]


def buildTeamValue(playersList):
    """Spectators field value: names left-aligned, scores right-aligned."""
    if not playersList:  # non-empty fallback so Discord never receives an empty field value
        return '—'
    names, scores = sortPlayersByScore(playersList)
    name_width = max(len(name) for name in names) + 1
    score_width = max(len(score) for score in scores)
    return '\n'.join(_alignedTeamLines(playersList, name_width, score_width))

def buildTeamsValue(team_red, score_red, team_blue, score_blue):
    # One full-width field with Team Red on the left and Team Blue on the right.
    # Each side: names left-aligned with just one space after the longest name,
    # and scores right-aligned to a column anchored to the team Score's digit
    # width (widened further if a player score has more digits). This keeps every
    # number's last digit under the team Score's last digit (a 6 sits under the 4
    # of 14) instead of trailing directly after the name. The red column is then
    # padded so the blue column starts at a fixed offset (monospace), keeping both
    # sides aligned.
    red_header = '[Team Red] Score: '
    blue_header = '[Team Blue] Score: '

    def side(team, score, header):
        # Anchor every player score to the team Score: each score is right-aligned
        # so its last digit lands in the same column as the team Score's last digit
        # (a 6 sits directly under the 7 of 77). Names are never truncated, so
        # [BOT]He-Man stays whole and its score lines up with the team total above.
        score = str(score)
        right_edge = len(header) + len(score) - 1
        names, scores = sortPlayersByScore(team)
        lines = [header + score]
        for name, sc in zip(names, scores):
            sc = str(sc)
            score_start = right_edge - len(sc) + 1
            if len(name) >= score_start:
                line = name + ' ' + sc
            else:
                line = name + ' ' * (score_start - len(name)) + sc
            lines.append(line)
        return lines

    red_lines = side(team_red, score_red, red_header)
    blue_lines = side(team_blue, score_blue, blue_header)
    width = max(len(line) for line in red_lines)
    rows = zip_longest(red_lines, blue_lines, fillvalue='')
    return '```\n' + '\n'.join(red.ljust(width) + ' ' + blue for red, blue in rows) + '\n```'

def sortPlayersByScore(playersList):
    sorted_players = []
    sorted_scores = []
    players = []
    scores = []
    if len(playersList) > 0:
        for p in playersList:
            try:
                score = int(p[0])
                p_name = formatPlayerName(p[2])
            except (ValueError, IndexError):
                continue
            players.append(p_name.strip("\""))
            scores.append(score)
    converted_scores = np.array(scores)
    indexes = np.argsort(converted_scores)
    reversed_indexes = indexes[::-1]
    for i in reversed_indexes:
        sorted_players.append(players[i])
        sorted_scores.append(str(scores[i]))
    return (sorted_players, sorted_scores)

def queryServerSync(ip: str, port: int):
    """Blocking Quake3 'getstatus' query. Always run inside a worker thread
    (see getServerStatus), never directly on the event loop."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)  # hard 3s limit so an offline/firewalled server can't hang us
    try:
        sock.connect((ip, port))
        getStatus = bytes([0xff, 0xff, 0xff, 0xff, 0x67, 0x65, 0x74, 0x73, 0x74, 0x61, 0x74, 0x75, 0x73, 0x0a])
        sock.send(getStatus)
        response = sock.recvfrom(2048) # response is (string, address) where address is (host, port) for AF_INET family
        underline_bytes = response[0]
        return parseResponse(underline_bytes.decode("latin_1"))
    finally:
        sock.close()  # release the fd on success or failure

async def getServerStatus(ip: str, port: int):
    # Offload the blocking socket I/O to a worker thread so the Discord
    # event loop stays responsive even while we wait for a reply.
    try:
        return await asyncio.to_thread(queryServerSync, ip, port)
    except (socket.timeout, OSError) as e:
        # socket.timeout is a subclass of OSError; both mean the server is
        # offline / unreachable / didn't answer within the 3s timeout.
        print(f"Query for {ip}:{port} failed: {e}")
        return None

def parseResponse(response: str):
    lines = response.split('\n')
    lines = lines[1:] # first element is 'ÿÿÿÿstatusResponse', followed by server cfg then optionally players
    serverVars = parseServerInfo(lines[0])
    players = []
    if len(lines) > 1: # there are players/bots
        players = parsePlayers(lines[1:])
    return (serverVars, players)

def parseServerInfo(serverData: str):
    data = serverData.split('\\')
    data = data[1:] # first element is empty as string starts with \\
    serverInfo = {}
    # if len(data) % 2 == 0: # must be even number now
    # stop before the last token so a trailing odd token can't raise IndexError
    for i in range(0, len(data) - 1, 2):
        key = data[i].lower()
        value = data[i+1]
        serverInfo[key] = value

    return serverInfo

def parsePlayers(playersList: [str]):
    players = []
    for p in playersList:
        if len(p) > 0:
            playerData = p.split() # score ping name
            players.append(playerData)
    return players

def isValidIp(ip: str):
   components = ip.strip().split(".")
   return len(components) == 4

async def resolveHost(domain: str):
    def _resolve():
        try:
            result = dns.resolver.resolve(domain, "A")
            return [str(item) for item in result]
        except dns.exception.DNSException as e:
            # Covers NXDOMAIN / NoAnswer / NoNameservers / Timeout, etc.
            # Returning [] lets the caller send "I couldn't resolve that domain".
            print(f"DNS resolution failed for {domain}: {type(e).__name__}: {e}")
            return []

    # dns.resolver.resolve is a blocking network call; run it in a worker
    # thread so the Discord event loop stays responsive while we wait.
    return await asyncio.to_thread(_resolve)

@bot.event
async def set_default_status():
    await bot.change_presence(game=discord.Game(name='noobs getting owned', type=3))

def acquire_single_instance_lock():
    """Guards against a second qbot instance running at the same time.

    Takes a non-blocking exclusive lock (flock) on a lock file next to this script.
    If it is already held by another live process, we print a helpful message
    (including that process's PID, when we can read it) and exit. The lock is
    released automatically by the kernel when this process exits or crashes, so a
    stale lock file never blocks a legitimate restart. Returns the file handle that
    must stay referenced for the lifetime of the process (dropping it would release
    the lock).
    """
    import fcntl
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.qbot.lock')
    # Open with 'a+' so we create the file if missing but do NOT truncate the
    # holder's PID — that would wipe exactly the info we need if someone is
    # already running. We only rewrite it after we win the lock below.
    fh = open(lock_path, 'a+')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Someone else already holds the lock. Read the PID it recorded (best
        # effort) so we can point the user at the right process to stop.
        holder = ''
        try:
            with open(lock_path) as existing:
                holder = existing.read().strip()
        except OSError:
            pass
        sys.exit(
            "Another qbot instance is already running"
            + (f" (PID {holder})." if holder else ".")
            + "\nNot starting a duplicate. Stop the other one first, e.g.:"
            + (f"\n    kill {holder}" if holder else "\n    pkill -f qbot.py")
        )
    # We now hold the lock, so it's safe to rewrite the file with our own PID
    # for the benefit of any future duplicate attempt.
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main():
    """Entry point: read the token, take the single-instance lock, and run the bot.

    Kept inside a function (and gated by the ``__main__`` check below) so that
    importing ``qbot`` for its helper functions (e.g. ``buildTeamsValue``) does
    NOT accidentally launch a live Discord bot as a side effect.
    """
    # The Discord bot token must be supplied via the DISCORD_TOKEN environment
    # variable. Keep it out of source control (see .env.example / .gitignore).
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit(
            "Error: DISCORD_TOKEN is not set. "
            "Set it before starting the bot, e.g. `export DISCORD_TOKEN=your-token` "
            "(or add it to a .env file / your shell profile)."
        )

    # Take the single-instance lock before connecting. This local stays referenced
    # for the lifetime of main() (which blocks in bot.run), so the OS keeps holding
    # the lock until the process exits.
    single_instance_lock = acquire_single_instance_lock()

    bot.run(token)


if __name__ == "__main__":
    main()
