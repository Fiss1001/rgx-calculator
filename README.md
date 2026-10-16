# RGX-Calculator (`rgx_web.py`)

Rechnet aus, wie viele RGX-Punkte eine Spieler:in an einem Turnier oder Ligaspieltag gewinnt oder verliert, noch bevor die Playerzone die offiziellen Werte einträgt.

## Benutzung
```
python rgx_web.py      # dann http://localhost:8000 öffnen
```
Es gibt zwei Felder:
- **Link:** ein Turnier-Spielplan (`…/tournaments/schedule/team/67787`) oder ein Liga-Spieltag (`…/schedule/mixed-…/80026?md=…`)
- **Name der Spieler:in:** z. B. `Finn Vissering`. Ein Teil des Namens reicht.

Gehostet (z. B. auf Render) nimmt das Skript den Port aus `$PORT`. Zusätzliche Pakete braucht es nicht.

## Was passiert
1. **Seite laden.** Es werden nur Links auf `playerzone.roundnetgermany.de` angenommen.
2. **Spiele sammeln.** Jede Spielkarte (`<rg-game-card>`) hat zwei Teams mit `load_team_info(…)` und die Punkte je Satz. Wer in einem Satz mehr Punkte hat, gewinnt ihn, und daraus ergibt sich der Satzstand (z. B. 2:1).
   - Im Turnier stehen die Spiele direkt auf der Seite.
   - In der Liga hat jede Begegnung Einzelspiele (Open 1, Open 2, Women, Mixed 1, Mixed 2). Das Skript lädt sie wie beim Aufklappen: `/ajax/get-subgames/<Begegnung>?group=<Gruppe>`.
3. **Spieler:innen holen.** Für jede Seite eines Spiels macht das Skript dieselbe Abfrage wie das Popup beim Klick auf den Teamnamen (`/tournament/get-team-info`) und bekommt die beiden Spieler:innen mit RGX.
4. **Eigene Spiele finden.** Gezählt werden nur die Spiele, in denen der eingetragene Name vorkommt. Dein RGX plus der deines Partners ergibt r1, die beiden Gegner:innen ergeben r2.
5. **Rechnen und anzeigen.** Es wird mit der Formel unten gerechnet. Angezeigt werden alle Spiele mit Partner, Gegner:innen, Ergebnis und Punkten sowie die Summe.

Alle Abfragen laufen parallel, damit auch ein ganzer Spieltag schnell geht.

## Formel
```
e1 = 1 / (1 + 10^((r2 − r1) / 550))      erwartetes Ergebnis
x1 = b · 50 · (p − e1)                    Punkte fürs Team
pro Spieler:in = round(x1) / 2           → immer 0,5er-Schritte
```
- `b` = 0,75 bei 1-Satz-Spielen, sonst 1
- `p` je Satzstand: 1:0, 2:0 und 3:0 = 1 · 3:1 = 0,75 · 2:1 = 0,67 · 3:2 = 0,6 · 1:1 und 2:2 = 0,5 · 2:3 = 0,4 · 1:2 = 0,33 · 1:3 = 0,25 · Niederlage zu null = 0

**Beispiel:** Team 1947 verliert 0:1 gegen 1935. Das ergibt e1 = 0,513 und x1 = 0,75 · 50 · (0 − 0,513) = −19,2, gerundet −19, also **−9,5** pro Spieler:in (wie offiziell).

## Wichtig
- Die Playerzone zeigt immer die **aktuellen** RGX. Am Spieltag selbst sind das die richtigen Werte, rückwirkend weicht es leicht ab.
- Ändert die Playerzone ihr HTML, kann das Auslesen kaputtgehen.

## Aufbau
| Teil | Aufgabe |
|---|---|
| `P_TABLE`, `delta` | Formel und Rundung |
| `get` | Abfragen an die Playerzone |
| `team_players` | Popup-Abfrage → Spieler:innen mit RGX |
| `parse_cards` | Spielkarten → Teams und Satzstand |
| `collect_games` | Turnierspiele bzw. alle Liga-Einzelspiele sammeln |
| `calculate` | Spiele der Spieler:in finden und rechnen |
| `render`, `PAGE`, `Handler` | Webseite |
