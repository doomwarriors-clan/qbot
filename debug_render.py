"""Throwaway: print raw TDM data + exact current buildTeamsValue output.

Uses the REAL code in qbot.py so the numbers/strings match what Discord shows.
"""
import sys
sys.path.insert(0, '/home/hkuehnemund/Documents/qbot')
import qbot

status = qbot.queryServerSync('doomwarriors.de', 27961)
serverInfo, playersList = status

print("=== RAW playersList (score, ping, name) ===")
for i, p in enumerate(playersList):
    print(f"  idx {i+1}: score={p[0]!r} ping={p[1]!r} name={p[2]!r}")

print("score_red  =", repr(serverInfo.get('score_red')))
print("score_blue =", repr(serverInfo.get('score_blue')))
print("g_gametype =", repr(serverInfo.get('g_gametype')))
print("players_blue =", repr(serverInfo.get('players_blue')))
print("players_red  =", repr(serverInfo.get('players_red')))

players_in_blue_team = serverInfo.get('players_blue', '').split()
players_in_red_team = serverInfo.get('players_red', '').split()
team_red, team_blue, team_spec = [], [], []
for index, player in enumerate(playersList):
    if str(index + 1) in players_in_blue_team:
        team_blue.append(player)
    elif str(index + 1) in players_in_red_team:
        team_red.append(player)
    else:
        team_spec.append(player)

print("=== team_red (raw tuples) ===")
for p in team_red:
    print("   ", p)
print("=== team_blue (raw tuples) ===")
for p in team_blue:
    print("   ", p)

print("=== CURRENT buildTeamsValue output ===")
out = qbot.buildTeamsValue(team_red, serverInfo.get('score_red', '0'),
                           team_blue, serverInfo.get('score_blue', '0'))
print(out)
