# RGX-Calculator

Eine kleine Webseite, die aus dem Playerzone-Spielplan deines Teams ausrechnet, wie viele RGX-Punkte du bei einem Turnier gewinnst oder verlierst. Das geht schon am Turniertag, bevor die Playerzone die offiziellen Werte einträgt.

---

## Funktion

1. Du fügst den Link zu deinem Team-Spielplan ein
2. Das Skript lädt diese Seite und liest dein Team, die Spieler:innen und alle Spiele mit Satzergebnissen aus.
3. Für jeden Gegner fragt es dieselben Daten ab, die das Popup beim Klick auf den Teamnamen zeigt, und erhält so dessen Team-RGX.
4. Mit der offiziellen RGX-Formel rechnet es für jedes Spiel das Plus/Minus pro Spieler:in aus und zählt alles zusammen.
5. Das Ergebnis wird im Playerzone-Look angezeigt.

Es werden keine zusätzlichen Pakete gebraucht, nur Python.

---

## Starten

**Lokal**
```
python rgx_web.py            # http://localhost:8000
python rgx_web.py 8001       # anderer Port
python rgx_web.py --test     # Selbsttest der Rechnung
```

**Gehostet (Render)**

- https://rgx-calculator.onrender.com
---

## Ablauf im Detail

### 1. Link prüfen und Seite laden
- Es werden nur Links auf `playerzone.roundnetgermany.de` angenommen. Damit ruft die Seite keine fremden Adressen ab.
- Die Seite wird **vom Python-Server** geladen und nicht vom Browser. Der Browser dürfte fremde Seiten wegen CORS gar nicht auslesen.

### 2. HTML in Text umwandeln (`PageReader`)
Das HTML wird in einfache Textzeilen zerlegt:
- RGX-Badges (`<rg-score-badge score="989">`) werden zur Zeile `(989)`.
- Bei jedem Teamnamen-Link wird aus `onclick="load_team_info(45631, 67787, …, 470737)"` die **Team-ID** (67787) und die **Spiel-ID** (470737) gemerkt.

### 3. Eigenes Team auslesen (`parse_team`, `parse_players`)
- Im Block **„Team:“** stehen der Teamname und die RGX beider Spieler:innen, z. B. 989 + 945 = **1934**.
- Für die Anzeige werden zusätzlich Profilbild, Name, Verein und Vereinslogo aus den Spielerkarten geholt.

### 4. Spiele auslesen (`parse_schedule`)
Unter **„Schedule“** steht jedes Spiel so:
```
Gr. A #4             ← Runde
Team 1               ← Team 1
Team 2               ← Team 2
9  13                ← Satz 1 (Team 1 : Team 2)
13 11                ← Satz 2
```
- Die Punkte werden paarweise gelesen, und wer mehr Punkte hat, gewinnt den Satz. Im Beispiel steht es 1:1.
- Spiele ohne Ergebnis (noch nicht gespielt) werden übersprungen.
- Danach wird jedes Spiel aus **deiner** Sicht gedreht, dein Team steht also immer vorne. Gibst du im Feld „Teamname“ etwas ein, zählen nur Spiele mit diesem Namen.

### 5. Gegner-RGX holen (`team_rgx_via_api`)
Die Gegner-RGX stehen nicht auf der Seite. Sie werden erst geladen, wenn man auf einen Teamnamen klickt. Das Skript macht genau diese Abfrage selbst:
```
GET /tournament/get-team-info?id=<Team-ID>&game_id=<Spiel-ID>
```
Die Antwort enthält das Popup-HTML mit beiden Spieler-Badges, und deren Summe ist der Team-RGX des Gegners. Klappt das bei einem Gegner nicht, steht bei diesem Spiel „?“ und es fehlt in der Summe.

### 6. Rechnen (`delta_player`)
Siehe nächster Abschnitt.

### 7. Anzeigen (`render`)
Turniername, der Team-Block mit Spielerkarten, die Gesamtbilanz und darunter jedes Spiel als Karte mit Logo, Gegner (RGX), Ergebnis und Punkten. Der Streifen links ist grün bei Sieg, rot bei Niederlage und gelb bei Unentschieden.

---

## Die Formel

Für jedes Spiel, aus Sicht deines Teams (Team 1):

```
e1 = 1 / (1 + 10^((r2 − r1) / 550))      erwartetes Ergebnis
x1 = b · 50 · (p − e1)                    Punkte fürs ganze Team
pro Spieler:in = round(x1) / 2
```

| Zeichen | Bedeutung |
|---|---|
| `r1`, `r2` | Team-RGX (Summe beider Spieler:innen) von dir und vom Gegner |
| `d = 550` | Skalierung: wie stark ein RGX-Unterschied die Erwartung verschiebt |
| `k = 50` | maximale Punkte pro Spiel fürs Team |
| `b` | 0,75 bei 1-Satz-Spielen, sonst 1 |
| `p` | tatsächliches Ergebnis (siehe Tabelle) |

**Ergebnis `p` je Satzstand**

| Stand | p | Stand | p |
|---|---|---|---|
| 1:0, 2:0, 3:0 | 1 | 0:1, 0:2, 0:3 | 0 |
| 3:1 | 0,75 | 1:3 | 0,25 |
| 2:1 | 0,67 | 1:2 | 0,33 |
| 3:2 | 0,6 | 2:3 | 0,4 |
| 1:1, 2:2 | 0,5 | | |

**Runden:** Die Playerzone rundet den Teamwert auf eine ganze Zahl und halbiert ihn dann. Deshalb kommen immer 0,5er-Schritte heraus.

**Beispiel:** Ihr (1947) verliert 0:1 gegen Team 2 (1935).
- e1 = 1 / (1 + 10^(−12/550)) = **0,513**
- x1 = 0,75 · 50 · (0 − 0,513) = **−19,2**, gerundet −19
- pro Spieler:in: **−9,5** ✔ 

---

## Wichtig zu wissen

- **Aktuelle RGX:** Playerzone zeigt immer die *aktuellen* Werte. Am Turniertag sind das die Werte vor dem Turnier, also genau richtig. Für ältere Turniere weicht das Ergebnis etwas ab
- **Seitenstruktur:** Ändert die Playerzone ihr HTML, kann das Auslesen kaputtgehen. Dann zeigt die Seite den gelesenen Text an, damit man sieht, woran es hängt.
- **Öffentlich:** Gehostet kann jede:r mit der Adresse die Seite nutzen. Es werden aber nur öffentliche Playerzone-Daten angezeigt, es gibt keinen Login und es wird nichts gespeichert.

---

## Aufbau der Datei

| Teil | Aufgabe |
|---|---|
| `P_TABLE`, `expected`, `delta_player` | Formel und Rundung |
| `PageReader`, `read_page` | HTML → Textzeilen + Team-/Spiel-IDs |
| `fetch` | Playerzone-Seite laden (nur diese Domain) |
| `parse_team`, `parse_players` | eigenes Team, Spieler-RGX, Fotos, Vereine |
| `parse_schedule` | Spiele und Satzergebnisse |
| `tournament_title` | „Coconut Cup (Intermediate Open)“ |
| `team_rgx_via_api` | Gegner-RGX über die Popup-Abfrage |
| `evaluate` | Spiele aus eigener Sicht + RGX zusammenführen |
| `render`, `PAGE` | Darstellung im Playerzone-Look |
| `compute`, `Handler` | Webserver: Formular → Ergebnis |
| `selftest` | prüft die Rechnung gegen die offiziellen Coconut-Cup-Werte |
