#!/usr/bin/env python3
"""
Lokale Webseite fuer die RGX-Bilanz eines Turniers, noch bevor die Playerzone
die Punkte eintraegt.

Starten:   python rgx_web.py        (anderer Port: python rgx_web.py 8080)
Dann http://localhost:8000 oeffnen und den Link zum Spielplan des eigenen Teams
einfuegen, z. B. https://playerzone.roundnetgermany.de/tournaments/schedule/team/67787

Ablauf:
  1. Spielplan-Seite laden: eigenes Team mit den RGX beider Spieler:innen,
     darunter alle Spiele mit Satzergebnissen.
  2. Fuer jeden Gegner dieselbe Abfrage machen wie das Popup beim Klick auf den
     Teamnamen (/tournament/get-team-info) und die RGX beider Spieler:innen addieren.
  3. Formel (Roundnet Germany):
       e1 = 1 / (1 + 10 ** ((r2 - r1) / 550))
       x1 = b * 50 * (p - e1)        b = 0.75 bei 1-Satz-Spielen, sonst 1
       pro Spieler:in: round(x1) / 2

Die RGX-Werte sind die aktuell angezeigten. Am Turniertag sind das die Werte
vor dem Turnier; rueckwirkend fuer aeltere Turniere weichen sie ab.

Keine zusaetzlichen Pakete noetig. Selbsttest: python rgx_web.py --test
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
ALLOWED_HOST = "playerzone.roundnetgermany.de"

# ---------------------------------------------------------------- Formel

P_TABLE = {
    (1, 0): 1, (2, 0): 1, (3, 0): 1,
    (0, 1): 0, (0, 2): 0, (0, 3): 0,
    (1, 1): 0.5, (2, 2): 0.5,
    (2, 1): 0.67, (1, 2): 0.33,
    (3, 1): 0.75, (1, 3): 0.25,
    (3, 2): 0.6, (2, 3): 0.4,
}


def expected(r1, r2):
    return 1 / (1 + 10 ** ((r2 - r1) / 550))


def delta_player(r1, r2, own, opp):
    b = 0.75 if own + opp == 1 else 1
    x = b * 50 * (P_TABLE[(own, opp)] - expected(r1, r2))
    rounded = int(x + 0.5) if x >= 0 else -int(-x + 0.5)
    return rounded / 2


# ---------------------------------------------------------------- Seite lesen

class PageReader(HTMLParser):
    """HTML -> Textzeilen plus Links (Text -> URL).
    RGX-Badges <rg-score-badge score="989"> werden zu einer Zeile "(989)"."""
    BLOCK = {"div", "p", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "header", "table", "td", "th", "a", "span", "button", "option"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0
        self.links = []          # (Linktext, href)
        self.a_stack = []        # offene <a>: (href, Startindex in parts)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a":
            m = re.search(r"load_team_info\((\d+),\s*(\d+)(?:[^)]*?,\s*(\d+)\s*)?\)", attrs.get("onclick") or "")
            ref = f"team-info:{m.group(2)}:{m.group(3) or ''}" if m else ""
            self.a_stack.append((ref or attrs.get("href") or "", len(self.parts)))
        score = (attrs.get("score") or "").strip()
        if tag.endswith("score-badge") and re.fullmatch(r"\d{3,5}", score):
            self.parts.append(f"\n({score})\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        if tag == "a" and self.a_stack:
            href, start = self.a_stack.pop()
            text = " ".join("".join(self.parts[start:]).split())
            if href and text:
                self.links.append((text, href))
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def read_page(raw):
    p = PageReader()
    p.feed(raw)
    lines = [" ".join(html.unescape(l).split()) for l in "".join(p.parts).splitlines()]
    return [l for l in lines if l], p.links


def fetch(url):
    u = urllib.parse.urlparse(url.strip())
    if u.scheme not in ("http", "https") or u.hostname != ALLOWED_HOST:
        raise ValueError(f"Bitte einen Link auf {ALLOWED_HOST} einfuegen.")
    req = urllib.request.Request(url.strip(), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


RGX_LINE = re.compile(r"^\((\d{3,5})\)$")
NUM_LINE = re.compile(r"^\d{1,2}$")
GAMES_END = {"Enter result", "Ergebnis eintragen"}


def parse_team(lines):
    """Block "Team:" -> (Teamname, [RGX der Spieler:innen])."""
    for i, l in enumerate(lines):
        if l in ("Team:", "Team"):
            name = lines[i + 1] if i + 1 < len(lines) else ""
            rgx = []
            for l2 in lines[i + 2:]:
                if l2 in ("Schedule", "Spielplan") or len(rgx) == 2:
                    break
                m = RGX_LINE.match(l2)
                if m:
                    rgx.append(int(m.group(1)))
            return name, rgx
    return None, []


def parse_schedule(lines):
    """Spiele nach der Ueberschrift "Schedule":
    Rundenname, Team 1, Team 2, dann Punkte je Satz paarweise (Team 1, Team 2).
    Rueckgabe: Liste (runde, team1, team2, saetze1, saetze2)."""
    try:
        start = max(i for i, l in enumerate(lines) if l in ("Schedule", "Spielplan")) + 1
    except ValueError:
        return []
    games, words, nums = [], [], []

    def flush():
        if len(words) >= 2 and nums and len(nums) % 2 == 0:
            a = b = 0
            for x, y in zip(nums[0::2], nums[1::2]):
                a += x > y
                b += y > x
            if (a, b) in P_TABLE:
                label = words[-3] if len(words) >= 3 else ""
                games.append((label, words[-2], words[-1], a, b))

    for l in lines[start:]:
        if l in GAMES_END:
            break
        if NUM_LINE.match(l):
            nums.append(int(l))
            continue
        if nums:
            flush()
            words, nums = [], []
        words.append(l)
    flush()
    return games


def tournament_title(lines):
    """"Coconut Cup (Intermediate Open)" aus Seitentitel und Kopfbereich."""
    division = lines[0].rsplit(" - ", 1)[1] if lines and " - " in lines[0] else ""
    name = ""
    for i, l in enumerate(lines):
        if l == "Change site" and i + 1 < len(lines) and lines[i + 1] not in ("European Roundnet Association",):
            name = lines[i + 1]
    return f"{name} ({division})" if name and division else name or division


def team_rgx_via_api(tteam_id, game_id):
    """Dieselbe Abfrage, die das Popup beim Klick auf einen Teamnamen macht."""
    q = urllib.parse.urlencode({"id": tteam_id, "md_group_id": "", "division_id": "", "game_id": game_id})
    req = urllib.request.Request(f"https://{ALLOWED_HOST}/tournament/get-team-info?{q}",
                                 headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    lines, _ = read_page(data.get("html", ""))
    vals = [int(m.group(1)) for m in map(RGX_LINE.match, lines) if m]
    return sum(vals[:2]) if len(vals) >= 2 else None


# ---------------------------------------------------------------- HTML-Ausgabe

def fmt(x, digits=1):
    """Wie in der Playerzone: "+ 9.5", "-7.0", "-33"."""
    v = f"{abs(x):.{digits}f}"
    return f"+ {v}" if x > 0 else f"-{v}" if x < 0 else v


def sign_cls(x):
    return "pos" if x > 0 else "neg" if x < 0 else ""


def strip_tags(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ").split())


def parse_players(raw, base):
    """Spielerkarten im TEAM-Block: Foto, Name, Verein, Vereinslogo, RGX."""
    players = []
    for chunk in raw.split('class="player-card')[1:3]:
        img = re.search(r'class="player-profile-img"[^>]*src="([^"]+)"', chunk)
        logo = re.search(r'class="info-player-logo"[^>]*src="([^"]+)"', chunk)
        name = re.search(r'class="info-player-name">(.*?)</div>', chunk, re.S)
        club = re.search(r'class="info-player-club">(.*?)</div>', chunk, re.S)
        score = re.search(r'score="(\d+)"', chunk)
        players.append({
            "img": urllib.parse.urljoin(base, html.unescape(img.group(1))) if img else "",
            "logo": urllib.parse.urljoin(base, html.unescape(logo.group(1))) if logo else "",
            "name": strip_tags(name.group(1)) if name else "",
            "club": strip_tags(club.group(1)) if club else "",
            "rgx": int(score.group(1)) if score else None,
        })
    return players


def team_logos(raw, base):
    """Teamname -> Logo-URLs aus den Spielkarten."""
    logos = {}
    for m in re.finditer(r'onclick="load_team_info\([^"]*"\s*>(.*?)</a>', raw, re.S):
        name = re.search(r'class="name">(.*?)</span>', m.group(1), re.S)
        if name:
            srcs = re.findall(r'<img[^>]*src="([^"]+)"', m.group(1))
            logos.setdefault(strip_tags(name.group(1)).lower(),
                             [urllib.parse.urljoin(base, html.unescape(x)) for x in srcs][:2])
    return logos


def logo_html(urls):
    return "".join(f'<img class=tlogo src="{html.escape(u, quote=True)}" referrerpolicy=no-referrer alt="">'
                   for u in urls)


def render_players(players):
    cards = []
    for p in players:
        photo = (f'<img class=photo src="{html.escape(p["img"], quote=True)}" referrerpolicy=no-referrer alt="">'
                 if p["img"] else '<div class="photo empty"></div>')
        club_logo = (f'<img class=clublogo src="{html.escape(p["logo"], quote=True)}" referrerpolicy=no-referrer alt="">'
                     if p["logo"] else "")
        rgx = f'<span class=badge><b>RGX</b> {p["rgx"]}</span>' if p["rgx"] is not None else ""
        cards.append(f'<div class=pcard>{photo}<div class=pinfo>{club_logo}'
                     f'<div class=pname>{html.escape(p["name"])}</div>'
                     f'<div class=pclub>{html.escape(p["club"])}</div>{rgx}</div></div>')
    return f'<div class=pcards>{"".join(cards)}</div>' if cards else ""


def render(own_name, games, title="", players=(), logos=None):
    logos = logos or {}
    rows, total, missing = [], 0.0, 0
    for _, r1, opp, r2, a, b in games:
        res = "win" if a > b else "loss" if a < b else "draw"
        lg = logo_html(logos.get(opp.lower(), []))
        if r2 is None:
            missing += 1
            rows.append(f"<div class='game {res}'><span class=opp>{lg}{html.escape(opp)} <i>(?)</i></span>"
                        f"<span class=res>{res} ({a}:{b})</span><span class='delta muted'>?</span></div>")
            continue
        dp = delta_player(r1, r2, a, b)
        total += dp
        rows.append(f"<div class='game {res}'><span class=opp>{lg}{html.escape(opp)} <i>({r2})</i></span>"
                    f"<span class=res>{res} ({a}:{b})</span>"
                    f"<span class='delta {sign_cls(dp)}'>{fmt(dp)}</span></div>")
    rgx = games[0][1]
    tot = fmt(total, 0 if total == int(total) else 1)
    own_logo = logo_html(logos.get(own_name.lower(), []))
    note = (f"<p class=muted>Bei {missing} Spiel(en) konnte der Gegner-RGX nicht geladen werden; "
            "sie fehlen in der Summe.</p>" if missing else "")
    return (f"<div class=hero><h2>{html.escape(title or 'Turnier')}</h2></div>"
            f"<div class=section><h3>Team: {own_logo} {html.escape(own_name)}</h3>"
            f"<div class=teamrgx>Team-RGX <b>{rgx}</b></div></div>"
            + render_players(players) +
            f"<div class=section><h3>RGX-Bilanz</h3><div class=sum>"
            f"<span>{len(games)} Spiele</span><span class='total {sign_cls(total)}'>{tot}</span></div></div>"
            f"<div class=games>{''.join(rows)}</div>{note}")


PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>RGX-Bilanz</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@500;600;700&display=swap" rel=stylesheet>
<style>
:root{{--bg:#fffcf5;--panel:#fdf3e1;--gold:#f6b83b;--gold-d:#e09f1f;--ink:#1d1d1d;--red:#c0392b;--green:#2e9d3a;--muted:#8a8478}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:Barlow,system-ui,sans-serif;color:var(--ink);background:var(--bg)}}
nav{{background:#1f1f1f;color:#fff;display:flex;align-items:center;gap:1.2rem;padding:0 24px;height:64px}}
nav .logo{{font:700 1.7rem 'Barlow Condensed',sans-serif;letter-spacing:.02em}} nav .logo span{{color:var(--gold)}}
nav .tab{{background:var(--gold);color:var(--ink);font:600 1.05rem 'Barlow Condensed',sans-serif;text-transform:uppercase;
  padding:.45rem 1rem;border-radius:4px;letter-spacing:.03em}}
main{{max-width:1020px;margin:0 auto;padding:24px 16px 48px}}
form{{display:flex;gap:.5rem;flex-wrap:wrap}}
input{{padding:.6rem .7rem;border:1px solid #e3d9c4;border-radius:6px;font:inherit;background:#fff}}
input[name=url]{{flex:1;min-width:240px}} input[name=team]{{width:12rem}}
button{{padding:.6rem 1.2rem;border:0;border-radius:6px;background:var(--gold);color:var(--ink);cursor:pointer;
  font:600 1.05rem 'Barlow Condensed',sans-serif;text-transform:uppercase;letter-spacing:.03em}}
button:hover{{background:var(--gold-d)}}
.hint{{color:var(--muted);font-size:.9rem;margin:.5rem 0 1.5rem}}
.hero{{background:var(--panel);border-radius:12px;padding:18px;text-align:center}}
.hero h2{{margin:0;font:700 1.9rem 'Barlow Condensed',sans-serif;text-transform:uppercase}}
.section{{background:var(--panel);border-radius:12px;padding:16px 20px;margin-top:22px;display:flex;
  align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}}
.section h3{{margin:0;font:700 1.7rem 'Barlow Condensed',sans-serif;text-transform:uppercase;display:flex;align-items:center;gap:.5rem}}
.section .tlogo{{height:28px}}
.teamrgx{{font:600 1.1rem 'Barlow Condensed',sans-serif;text-transform:uppercase}}
.teamrgx b{{background:var(--gold);padding:.25rem .6rem;border-radius:6px;margin-left:.3rem}}
.pcards{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px}}
.pcard{{display:flex;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 3px 10px rgba(0,0,0,.12);min-height:180px}}
.photo{{width:38%;max-width:170px;object-fit:cover;flex-shrink:0;background:#ddd}}
.pinfo{{padding:14px 16px;display:flex;flex-direction:column;gap:.35rem;min-width:0}}
.clublogo{{height:48px;width:48px;object-fit:contain}}
.pname{{color:var(--red);font-weight:600;font-size:1.15rem;line-height:1.25}}
.pclub{{font:500 1rem 'Barlow Condensed',sans-serif;text-transform:uppercase;color:#555}}
.badge{{align-self:flex-start;background:#eee;border-radius:999px;padding:.2rem .65rem;font-weight:600;font-size:.95rem}}
.badge b{{color:var(--red);font-size:.7rem;margin-right:.2rem}}
.sum{{display:flex;align-items:baseline;gap:1rem;font:600 1.1rem 'Barlow Condensed',sans-serif;text-transform:uppercase}}
.sum .total{{font-size:2rem}}
.games{{display:flex;flex-direction:column;gap:10px;margin-top:20px}}
.game{{display:grid;grid-template-columns:1fr auto 4.5rem;gap:1rem;align-items:center;background:#fffaf0;
  border:2px solid var(--gold);border-left-width:6px;border-radius:8px;padding:12px 16px}}
.game.win{{border-left-color:var(--green)}} .game.loss{{border-left-color:var(--red)}}
.opp{{display:flex;align-items:center;gap:.45rem;font-weight:500;min-width:0}} .opp i{{font-style:normal;color:var(--muted)}}
.opp .tlogo{{height:24px;width:24px;object-fit:contain}}
.res{{color:var(--muted);text-align:right}} .delta{{text-align:right;font-weight:600}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}} .muted{{color:var(--muted)}}
.err{{background:#fdecea;padding:.8rem;border-radius:6px}}
pre{{white-space:pre-wrap;background:#fff;padding:.8rem;max-height:300px;overflow:auto;font-size:.8rem}}
@media (max-width:700px){{.pcards{{grid-template-columns:1fr}} nav .tab{{display:none}}
  .game{{grid-template-columns:1fr auto;}} .game .res{{grid-column:1;text-align:left;font-size:.9rem}} .game .delta{{grid-row:1 / span 2;grid-column:2}}}}
</style></head><body>
<nav><span class=logo>PZ<span>.</span></span><span class=tab>RGX-Bilanz</span></nav>
<main>
<form method=get action="/">
<input name=url placeholder="Playerzone-Link zu deinen Spielen" value="{url}" required>
<input name=team placeholder="Teamname (optional)" value="{team}">
<button>Berechnen</button></form>
<p class=hint>Teamname nur noetig, wenn die Seite auch Spiele anderer Teams zeigt. Ein Teil des Namens reicht.</p>
{body}
</main></body></html>"""


def evaluate(lines, own_rgx_parts, ident, opp_rgx):
    """opp_rgx: Funktion Teamname -> Team-RGX oder None.
    Rueckgabe: (Teamname, Team-RGX, Spiele als (_, r1, gegner, r2, a, b))."""
    own_name, rgx_parts = parse_team(lines)
    own_rgx = sum(own_rgx_parts or rgx_parts)
    key = (ident or own_name or "").lower()
    is_own = lambda n: key and key in n.lower()
    out = []
    for _, t1, t2, a, b in parse_schedule(lines):
        if is_own(t1) and not is_own(t2):
            opp = t2
        elif is_own(t2) and not is_own(t1):
            opp, a, b = t1, b, a
        else:
            continue
        out.append((own_name, own_rgx, opp, opp_rgx(opp), a, b))
    return own_name, own_rgx, out


def compute(url, ident):
    raw = fetch(url)
    lines, links = read_page(raw)
    own_name, rgx_parts = parse_team(lines)
    if len(rgx_parts) != 2:
        return error("Eigenes Team mit den beiden RGX-Werten nicht gefunden.", lines)
    # Teamname -> (tteam_id, game_id) aus den onclick="load_team_info(...)"-Links
    team_ids = {}
    for text, ref in links:
        if ref.startswith("team-info:"):
            team_ids.setdefault(text.lower(), ref.split(":")[1:])
    _, _, raw_games = evaluate(lines, rgx_parts, ident, lambda n: None)
    known = {}
    for name in dict.fromkeys(g[2] for g in raw_games):
        if name.lower() in team_ids:
            try:
                known[name] = team_rgx_via_api(*team_ids[name.lower()])
            except Exception:
                known[name] = None
    opp_rgx = known.get
    # eigenes Team ebenfalls ueber das Popup (kann den Stand zum Spielzeitpunkt liefern)
    if own_name and own_name.lower() in team_ids:
        try:
            own_api = team_rgx_via_api(*team_ids[own_name.lower()])
            if own_api:
                rgx_parts = [own_api]
        except Exception:
            pass
    own_name, own_rgx, games = evaluate(lines, rgx_parts, ident, opp_rgx)
    if not games:
        return error("Keine gespielten Spiele erkannt.", lines)
    return render(own_name, games, tournament_title(lines), parse_players(raw, url), team_logos(raw, url))


def error(msg, lines):
    return (f"<p class=err>{html.escape(msg)} Bitte die Seite speichern (Strg+S) und Claude schicken.</p>"
            "<details><summary>Gelesener Seitentext</summary>"
            f"<pre>{html.escape(chr(10).join(lines)[:8000])}</pre></details>")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        url = q.get("url", [""])[0]
        team = q.get("team", [""])[0].strip()
        body = ""
        if url:
            try:
                body = compute(url, team)
            except Exception as e:
                body = f"<p class=err>Fehler: {html.escape(str(e))}</p>"
        data = PAGE.format(url=html.escape(url, quote=True), team=html.escape(team, quote=True),
                           body=body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- Selbsttest

SAMPLE = """Schedule Moin meets Grüezi - Intermediate Open
Change site
European Roundnet Association
Swiss Roundnet
Change site
Coconut Cup
Info
Team:
Moin meets Grüezi
Finn Vissering
Flensburger Flundern
(989)
Maximilian Solleder
Roundnet Club Luzern
(945)
Schedule
P11-12
Moin meets Grüezi
Schlechte Sets
8
21
P9-12 #1
Setz aufs Netz
Moin meets Grüezi
21
14
P9-16 #2
Cutastrophal Banal
Moin meets Grüezi
6
21
AF #4
No mames
Moin meets Grüezi
17
9
17
11
Gr. A #4
Cutastrophal Banal
Moin meets Grüezi
9
13
13
11
Gr. A #3
Moin meets Grüezi
Jonas &amp; Julian
9
13
6
13
Gr. A #2
Moin meets Grüezi
Serve low, fly high
7
13
5
13
Gr. A #1
No mames
Moin meets Grüezi
13
4
13
6
P5-8
Moin meets Grüezi
Noch offen
Enter result
1. Set
:
"""


def selftest():
    official = {"Schlechte Sets": (1935, -9.5), "Setz aufs Netz": (2108, -6.5),
                "Cutastrophal Banal": (1960, None), "No mames": (2176, -7.0),
                "Jonas & Julian": (2136, -8.0), "Serve low, fly high": (2278, -5.0)}
    lines = [" ".join(html.unescape(l).split()) for l in SAMPLE.splitlines() if l.strip()]
    ok = tournament_title(lines) == "Coconut Cup (Intermediate Open)"
    ok &= parse_team(lines) == ("Moin meets Grüezi", [989, 945])
    # mit den offiziellen Team-RGX vom Turniertag (1947 statt heute 1934)
    name, rgx, games = evaluate(lines, [1947, 0], "", lambda n: official[n][0])
    got = [(g[2], g[4], g[5], delta_player(g[1], g[3], g[4], g[5])) for g in games]
    expect = [("Schlechte Sets", 0, 1, -9.5), ("Setz aufs Netz", 0, 1, -6.5),
              ("Cutastrophal Banal", 1, 0, 9.5), ("No mames", 0, 2, -7.0),
              ("Cutastrophal Banal", 1, 1, 0.5), ("Jonas & Julian", 0, 2, -8.0),
              ("Serve low, fly high", 0, 2, -5.0), ("No mames", 0, 2, -7.0)]
    ok &= got == expect and sum(g[3] for g in got) == -33
    if not ok:
        print(got)
    print("Selbsttest", "bestanden" if ok else "FEHLGESCHLAGEN")
    return ok


def main():
    if "--test" in sys.argv:
        sys.exit(0 if selftest() else 1)
    # Hosting (z. B. Render): Port kommt aus $PORT, dann auf allen Adressen lauschen
    if os.environ.get("PORT"):
        port, host = int(os.environ["PORT"]), "0.0.0.0"
    else:
        port, host = (int(sys.argv[1]) if len(sys.argv) > 1 else PORT), "127.0.0.1"
    print(f"RGX-Bilanz laeuft auf http://localhost:{port}  (Beenden mit Strg+C)", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
