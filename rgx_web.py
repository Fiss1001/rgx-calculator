#!/usr/bin/env python3
"""
RGX-Calculator: rechnet die RGX-Gewinne/-Verluste einer Spieler:in aus einem
Playerzone-Spielplan aus (Turnier-Teamspielplan oder Liga-Spieltag).

Starten:  python rgx_web.py   ->  http://localhost:8000
Hosting:  ist $PORT gesetzt (z. B. Render), wird dieser Port genutzt.

Formel (Roundnet Germany), r1/r2 = Team-RGX (Summe beider Spieler:innen):
    e1 = 1 / (1 + 10 ** ((r2 - r1) / 550))
    x1 = b * 50 * (p - e1)        b = 0.75 bei 1-Satz-Spielen, sonst 1
    pro Spieler:in: round(x1) / 2, Summe des Spieltags auf ganze Punkte gerundet
"""
import html
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "playerzone.roundnetgermany.de"
TOUR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "link_tour.mp4")  # Hilfe-Video neben dem Skript

# p aus Sicht von Team 1 je Satzstand
P_TABLE = {
    (1, 0): 1, (2, 0): 1, (3, 0): 1,
    (0, 1): 0, (0, 2): 0, (0, 3): 0,
    (1, 1): 0.5, (2, 2): 0.5,
    (2, 1): 0.67, (1, 2): 0.33,
    (3, 1): 0.75, (1, 3): 0.25,
    (3, 2): 0.6, (2, 3): 0.4,
}


def delta(r1, r2, own, opp):
    e1 = 1 / (1 + 10 ** ((r2 - r1) / 550))
    b = 0.75 if own + opp == 1 else 1
    x = b * 50 * (P_TABLE[(own, opp)] - e1)
    return (int(x + 0.5) if x >= 0 else -int(-x + 0.5)) / 2


# ---------------------------------------------------------------- Playerzone abfragen

def get(path, params=None):
    url = path if path.startswith("http") else f"https://{HOST}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def norm(name):
    return text(name).lower()


def team_players(args, cache):
    """Popup beim Klick auf einen Teamnamen: load_team_info(_, team, md_group, division, game)
    -> [(Name, RGX)] der beiden Spieler:innen."""
    if args not in cache:
        a = [x if x != "null" else "" for x in args] + [""] * 5
        try:
            raw = json.loads(get("/tournament/get-team-info", {
                "id": a[1], "md_group_id": a[2], "division_id": a[3], "game_id": a[4]}))["html"]
            # je Spielerkarte im Popup: Foto, Name, RGX, Vereinslogo, Verein
            players = []
            for card in raw.split('class="player-team-card')[1:]:
                name = re.search(r'class="player-name">(.*?)</div>', card, re.S)
                score = re.search(r'score="(\d+)"', card)
                img = re.search(r'class="player-img">\s*<img[^>]*src="([^"]+)"', card)
                logo = re.search(r'class="player-club">\s*<img[^>]*src="([^"]+)"', card)
                club = re.search(r'class="[^"]*club">(.*?)</div>', card, re.S)
                if name and score:
                    players.append((text(name.group(1)), int(score.group(1))))
                    cache.setdefault("info", {})[players[-1][0]] = {
                        "img": absolute(img.group(1)) if img else "",
                        "logo": absolute(logo.group(1)) if logo else "",
                        "club": text(club.group(1)) if club else ""}
            cache[args] = players
        except Exception:
            cache[args] = []
    return cache[args]


def parse_cards(page):
    """Spielkarten -> [{label, sides: [load_team_info-Argumente], sets: (a, b)}]."""
    games = []
    for card in re.findall(r"<rg-game-card(?:(?!<rg-game-card).)*?</rg-game-card>", page, re.S):
        if "show_subgames(" in card:
            continue
        sides = [tuple(x.strip() for x in m.split(","))
                 for m in re.findall(r"load_team_info\(([^)]*)\)", card)]
        a = b = 0
        for x, y in re.findall(r'<span class="score[^"]*">\s*(\d+)\s*</span>\s*<span class="score[^"]*">\s*(\d+)\s*</span>',
                               card):
            a, b = a + (int(x) > int(y)), b + (int(y) > int(x))
        label = re.search(r'slot="tags">(.*?)</div>', card, re.S)
        if len(sides) == 2 and (a, b) in P_TABLE:
            games.append({"label": text(label.group(1)) if label else "", "sides": tuple(sides), "sets": (a, b)})
    return games


def collect_games(page):
    """Turnier: Spiele stehen direkt auf der Seite. Liga: Einzelspiele jeder Begegnung nachladen."""
    group = re.search(r"group:\s*(\d+)", page)
    load = lambda enc: json.loads(get(f"/ajax/get-subgames/{enc}", {"group": group.group(1) if group else ""}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        subpages = [d.get("html", "") for d in pool.map(load, dict.fromkeys(re.findall(r"show_subgames\('(\d+)'\)", page)))]
    games = {g["sides"]: g for p in [page] + subpages for g in parse_cards(p)}   # doppelte raus
    return list(games.values())


def absolute(src):
    return urllib.parse.urljoin(f"https://{HOST}/", html.unescape(src)) if src else ""


def calculate(url, player):
    if urllib.parse.urlparse(url).hostname != HOST:
        raise ValueError(f"Bitte einen Link auf {HOST} einfuegen.")
    page = get(url)
    games = collect_games(page)
    cache, key = {}, norm(player)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda s: team_players(s, cache), {s for g in games for s in g["sides"]}))
    rows = []
    for g in games:
        sides = [team_players(s, cache) for s in g["sides"]]
        mine = next((i for i, ps in enumerate(sides) if any(key in norm(n) for n, _ in ps)), None)
        if mine is None:
            continue
        own, opp = sides[mine], sides[1 - mine]
        a, b = g["sets"] if mine == 0 else g["sets"][::-1]
        r1 = sum(r for _, r in own) if len(own) == 2 else None
        r2 = sum(r for _, r in opp) if len(opp) == 2 else None
        rows.append({"label": g["label"], "own": own, "opp": opp, "r1": r1, "r2": r2, "a": a, "b": b,
                     "delta": delta(r1, r2, a, b) if r1 and r2 else None,
                     "name": next(n for n, _ in own if key in norm(n))})
    # Spielerkarten: bei immer gleichem Partner beide, sonst nur die gesuchte Spieler:in
    cards = []
    if rows:
        me = rows[0]["name"]
        partners = {n for r in rows for n, _ in r["own"] if n != me}
        rgx = {n: v for r in rows for n, v in r["own"]}
        for n in [me] + (sorted(partners) if len(partners) == 1 else []):
            cards.append(dict(cache.get("info", {}).get(n, {}), name=n, rgx=rgx.get(n)))
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    return rows, text(title.group(1)) if title else "", cards


# ---------------------------------------------------------------- Ausgabe

def fmt(x, digits=1):
    v = f"{abs(x):.{digits}f}"
    return f"+ {v}" if x > 0 else f"-{v}" if x < 0 else v


def cls(x):
    return "pos" if x > 0 else "neg" if x < 0 else ""


def player_cards(cards):
    out = []
    for c in cards:
        src = lambda k: html.escape(c.get(k, ""), quote=True)
        photo = (f'<img class=photo src="{src("img")}" referrerpolicy=no-referrer alt="">' if c.get("img")
                 else '<div class=photo></div>')
        logo = f'<img class=clublogo src="{src("logo")}" referrerpolicy=no-referrer alt="">' if c.get("logo") else ""
        out.append(f'<div class=pcard>{photo}<div class=pinfo>{logo}<div class=pname>{html.escape(c["name"])}</div>'
                   f'<div class=pclub>{html.escape(c.get("club", ""))}</div>'
                   f'<span class=badge><b>RGX</b> {c["rgx"] or "?"}</span></div></div>')
    return f'<div class="pcards n{len(cards)}">{"".join(out)}</div>' if out else ""


def render(player, rows, title, cards=()):
    if not rows:
        return (f"<p class=err>Keine gespielten Spiele mit „{html.escape(player)}“ gefunden. "
                "Sind Aufstellungen und Ergebnisse schon eingetragen?</p>")
    total = sum(r["delta"] or 0 for r in rows)
    rounded = int(total + 0.5) if total >= 0 else -int(-total + 0.5)  # RGX wird nur in ganzen Punkten verbucht
    items = []
    for r in rows:
        res = "win" if r["a"] > r["b"] else "loss" if r["a"] < r["b"] else "draw"
        partner = " & ".join(n for n, _ in r["own"] if n != r["name"]) or "?"
        opp = " & ".join(n for n, _ in r["opp"]) or "?"
        d = (f"<span class='delta {cls(r['delta'])}'>{fmt(r['delta'])}</span>" if r["delta"] is not None
             else "<span class='delta muted'>?</span>")
        items.append(f"<div class='game {res}'><span><b>{html.escape(r['label'])}</b> mit {html.escape(partner)} "
                     f"<i>({r['r1'] or '?'})</i> vs {html.escape(opp)} <i>({r['r2'] or '?'})</i></span>"
                     f"<span class=res>{res} ({r['a']}:{r['b']})</span>{d}</div>")
    head = (f"Team: {cards[0]['name']} & {cards[1]['name']}" if len(cards) == 2
            else f"Spieler:in: {rows[0]['name']}")
    return (f"<div class=section><h3>{html.escape(head)}</h3>"
            f"<span class=muted>{html.escape(title)}</span></div>" + player_cards(cards) +
            f"<div class=section><h3>{len(rows)} Spiele</h3>"
            f"<span class='total {cls(total)}'>{fmt(rounded, 0)}"
            + (f" <small class=muted>({fmt(total)})</small>" if rounded != total else "") + "</span></div>"
            f"<div class=games>{''.join(items)}</div>")


PAGE = """<!doctype html><html lang=de><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>RGX-Calculator</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600&family=Barlow+Condensed:wght@600;700&display=swap" rel=stylesheet>
<style>
:root{{--bg:#fffcf5;--panel:#fdf3e1;--gold:#f6b83b;--red:#c0392b;--green:#2e9d3a;--muted:#8a8478}}
*{{box-sizing:border-box}} body{{margin:0;font-family:Barlow,system-ui,sans-serif;color:#1d1d1d;background:var(--bg)}}
nav{{background:#1f1f1f;color:#fff;padding:0 24px;height:64px;display:flex;align-items:center;
  font:700 1.6rem 'Barlow Condensed',sans-serif;text-transform:uppercase;letter-spacing:.03em}}
main{{max-width:1020px;margin:0 auto;padding:24px 16px 48px}}
form{{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.5rem}}
input{{padding:.6rem .7rem;border:1px solid #e3d9c4;border-radius:6px;font:inherit}}
.urlbox{{flex:1;min-width:240px;display:flex;gap:.4rem;align-items:center;position:relative}}
.urlbox input{{flex:1;min-width:0}}
.help{{width:1.8rem;height:1.8rem;flex:none;border-radius:50%;background:var(--gold);display:grid;place-items:center;
  font:700 1.1rem 'Barlow Condensed',sans-serif;cursor:help}}
.tip{{display:none;position:absolute;top:calc(100% + .5rem);left:0;right:0;z-index:5;background:#fff;padding:.6rem;
  border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.25);font-size:.95rem}}
.tip video{{display:block;width:100%;margin-top:.4rem;border-radius:4px}}
.help:hover .tip,.help:focus .tip,.help:focus-within .tip{{display:block}} input[name=player]{{width:14rem}}
button{{padding:.6rem 1.2rem;border:0;border-radius:6px;background:var(--gold);cursor:pointer;
  font:600 1.05rem 'Barlow Condensed',sans-serif;text-transform:uppercase}}
.section{{background:var(--panel);border-radius:12px;padding:16px 20px;margin-top:16px;display:flex;
  align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}}
.section h3{{margin:0;font:700 1.7rem 'Barlow Condensed',sans-serif;text-transform:uppercase}}
.total{{font:700 2rem 'Barlow Condensed',sans-serif}}
.pcards{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:16px}} .pcards.n1{{grid-template-columns:minmax(0,500px)}}
.pcard{{display:flex;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 3px 10px rgba(0,0,0,.12);min-height:170px}}
.photo{{width:38%;max-width:170px;object-fit:cover;flex-shrink:0;background:#ddd}}
.pinfo{{padding:14px 16px;display:flex;flex-direction:column;gap:.35rem;min-width:0}}
.clublogo{{height:44px;width:44px;object-fit:contain}}
.pname{{color:var(--red);font-weight:600;font-size:1.15rem;line-height:1.25}}
.pclub{{font:600 1rem 'Barlow Condensed',sans-serif;text-transform:uppercase;color:#555}}
.badge{{align-self:flex-start;background:#eee;border-radius:999px;padding:.2rem .65rem;font-weight:600}}
.badge b{{color:var(--red);font-size:.7rem;margin-right:.2rem}}
.games{{display:flex;flex-direction:column;gap:10px;margin-top:20px}}
.game{{display:grid;grid-template-columns:1fr auto 4.5rem;gap:1rem;align-items:center;background:#fffaf0;
  border:2px solid var(--gold);border-left-width:6px;border-radius:8px;padding:12px 16px}}
.game.win{{border-left-color:var(--green)}} .game.loss{{border-left-color:var(--red)}}
.game i{{font-style:normal;color:var(--muted)}}
.res{{color:var(--muted)}} .delta{{text-align:right;font-weight:600}}
.pos{{color:var(--green)}} .neg{{color:var(--red)}} .muted{{color:var(--muted)}}
.err{{background:#fdecea;padding:.8rem;border-radius:6px}}
@media (max-width:700px){{.pcards{{grid-template-columns:1fr}} .game{{grid-template-columns:1fr auto}} .res{{grid-column:1}} .delta{{grid-row:1/span 2;grid-column:2}}}}
</style></head><body>
<nav>RGX-Calculator</nav>
<main>
<form method=get action="/">
<div class=urlbox><input name=url placeholder="Playerzone-Link (Turnier-Spielplan oder Liga-Spieltag)" value="{url}" required>
<span class=help tabindex=0 aria-label="Wo finde ich den Link?">?<span class=tip>
<b>Wo finde ich den Link?</b><video src="/link_tour.mp4" muted loop playsinline preload=none></video></span></span></div>
<input name=player placeholder="Name der Spieler:in" value="{player}" required>
<button>Berechnen</button></form>
{body}
</main>
<script>
const h=document.querySelector('.help'),v=h.querySelector('video'),show=()=>v.play().catch(()=>{{}}),hide=()=>{{v.pause();v.currentTime=0}};
h.addEventListener('mouseenter',show);h.addEventListener('focus',show);
h.addEventListener('mouseleave',()=>{{if(document.activeElement!==h)hide()}});h.addEventListener('blur',hide);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/link_tour.mp4":
            return self.send_video()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        url, player = q.get("url", [""])[0].strip(), q.get("player", [""])[0].strip()
        body = ""
        if url and player:
            try:
                body = render(player, *calculate(url, player))
            except Exception as e:
                body = f"<p class=err>Fehler: {html.escape(str(e))}</p>"
        data = PAGE.format(url=html.escape(url, quote=True), player=html.escape(player, quote=True),
                           body=body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def send_video(self):
        # Range-Anfragen unterstuetzen, sonst spielt Safari/iOS das Video nicht ab
        try:
            with open(TOUR, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        start, end = 0, len(data) - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):
            start, end = (int(m[1]), int(m[2] or end)) if m[1] else (len(data) - int(m[2]), end)
            end = min(end, len(data) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data[start:end + 1])

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    print(f"RGX-Calculator laeuft auf http://localhost:{port}  (Beenden mit Strg+C)", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
