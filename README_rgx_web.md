# RGX-Calculator (`rgx_web.py`)

Eine kleine Webseite, die ausrechnet, wie viele RGX-Punkte eine Spieler:in an einem Turnier oder Ligaspieltag gewinnt oder verliert. Das geht schon am selben Tag, bevor die Playerzone die offiziellen Werte einträgt.

## Aufrufen
Der RGX-Calculator läuft online unter:

**https://rgx-calculator.onrender.com/**

Einfach öffnen, Felder ausfüllen, fertig. Nach längerer Pause kann der erste Aufruf etwas dauern, weil der kostenlose Render-Server erst aufwacht.

### Lokal starten (optional)
```
python rgx_web.py
```
Danach **http://localhost:8000** öffnen. Es braucht nur Python 3, keine zusätzlichen Pakete. Auf Render liest das Skript den Port aus der Umgebungsvariable `PORT`.

## Eingabe
Die Seite hat zwei Felder:

| Feld | Inhalt | Beispiel |
|---|---|---|
| **Link** | Turnier-Spielplan eines Teams oder ein Liga-Spieltag aus der Playerzone | `https://playerzone.roundnetgermany.de/tournaments/schedule/team/<id>` |
| **Spieler:in** | Name der Person, für die gerechnet wird. Ein Teil des Namens reicht. | `Max Mustermann` |

Ob es ein Turnier oder ein Ligaspieltag ist, erkennt das Skript selbst.

## Was passiert
1. **Seite laden.** Es werden nur Links auf `playerzone.roundnetgermany.de` angenommen.
2. **Spiele sammeln.** Aus jeder Spielkarte werden die beiden Teams und die Punkte je Satz gelesen. Daraus ergibt sich der Satzstand (z. B. 2:1).
   - **Turnier:** Die Spiele stehen direkt auf der Seite.
   - **Liga:** Jede Begegnung enthält Einzelspiele (Open, Women, Mixed …). Das Skript lädt sie wie beim Aufklappen auf der Seite.
3. **Spieler:innen holen.** Für jedes Team wird dieselbe Abfrage gemacht wie beim Klick auf den Teamnamen (Popup). So kommen Namen, aktueller RGX, Profilbild und Verein.
4. **Eigene Spiele finden.** Gezählt werden nur Spiele, in denen der eingetragene Name vorkommt. Wechselt der Partner (typisch in der Liga), ist das kein Problem.
5. **Rechnen und anzeigen.**

Alle Abfragen laufen parallel, damit auch ein ganzer Spieltag schnell geht.

## Anzeige
- **Kopfzeile:** „RGX-Calculator“
- **Team-Karten:** Foto, Name, RGX und Verein.
  - Immer derselbe Partner: beide Karten (`Team: A & B`)
  - Wechselnde Partner: nur die Karte der eingetragenen Spieler:in
- **Summe:** Anzahl der Spiele und Gesamt-Plus/Minus
- **Spielliste:** je Spiel Kategorie, Partner, Gegner:innen, Satzstand und Punkte

## Formel
```
r1 = RGX eigene Spieler:in + RGX Partner
r2 = RGX beider Gegner:innen

e1 = 1 / (1 + 10^((r2 − r1) / 550))      erwartetes Ergebnis
x1 = b · 50 · (p − e1)                    Punkte fürs Team
pro Spieler:in = round(x1) / 2           immer in 0,5er-Schritten
```
- `b` = 0,75 bei Spielen mit nur einem Satz, sonst 1
- `p` je Satzstand:

| Satzstand | p |
|---|---|
| 1:0, 2:0, 3:0 | 1 |
| 3:1 | 0,75 |
| 2:1 | 0,67 |
| 3:2 | 0,6 |
| 1:1, 2:2 | 0,5 |
| 2:3 | 0,4 |
| 1:2 | 0,33 |
| 1:3 | 0,25 |
| 0:1, 0:2, 0:3 | 0 |

Gerundet wird kaufmännisch (,5 immer weg von der Null).

**Beispiel:** Ein Team mit 2000 verliert ein 1-Satz-Spiel 0:1 gegen ein Team mit 2100.
- e1 = 1 / (1 + 10^(100/550)) ≈ 0,397
- x1 = 0,75 · 50 · (0 − 0,397) ≈ −14,9, gerundet −15
- pro Spieler:in: **−7,5**

## Wichtig
- Die Playerzone zeigt immer den **aktuellen** RGX. Am Turnier- oder Spieltag selbst stimmen die Werte daher genau, rückwirkend weichen sie leicht ab.
- Ändert die Playerzone ihr HTML, kann das Auslesen kaputtgehen.

## Aufbau des Skripts
| Teil | Aufgabe |
|---|---|
| `P_TABLE`, `delta` | Formel und Rundung |
| `get` | Abfragen an die Playerzone |
| `team_players` | Popup-Abfrage: Spieler:innen mit RGX, Foto und Verein |
| `parse_cards` | Spielkarten auslesen: Teams und Satzstand |
| `collect_games` | Alle Spiele sammeln (Turnier direkt, Liga über die Einzelspiele) |
| `calculate` | Spiele der Spieler:in finden und rechnen |
| `player_cards`, `render` | Ergebnis als HTML aufbauen |
| `PAGE`, `Handler` | Eingabeseite und kleiner Webserver |
