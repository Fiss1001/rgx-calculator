#!/usr/bin/env python3
"""
RGX-Calculator: rechnet die RGX-Gewinne/-Verluste einer Spieler:in aus einem
Playerzone-Spielplan aus (Turnier-Teamspielplan oder Liga-Spieltag).

Starten:  python rgx_web.py   ->  http://localhost:8000
Hosting:  ist $PORT gesetzt (z. B. Render), wird dieser Port genutzt.

Formel (Roundnet Germany), r1/r2 = Team-RGX (Summe beider Spieler:innen):
    e1 = 1 / (1 + 10 ** ((r2 - r1) / 550))
    x1 = b * 50 * (p - e1)        b = 0.75 bei 1-Satz-Spielen, sonst 1
    pro Spieler:in: round(x1) / 2, Summe des Spieltags immer abgerundet
"""
import gzip
import html
import http.client
import threading
import time
import math
import json
import os
import re
import urllib.parse
from collections import Counter
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

POOL = ThreadPoolExecutor(max_workers=16)   # bleibt bestehen, damit die Verbindungen der Threads offen bleiben
_local = threading.local()   # pro Thread eine offene Verbindung (Keep-Alive spart den TLS-Aufbau je Abfrage)


def _conn(host, fresh=False):
    conns = _local.__dict__.setdefault("conns", {})
    if fresh or host not in conns:
        if host in conns:
            conns[host].close()
        conns[host] = http.client.HTTPSConnection(host, timeout=30)
    return conns[host]


def get(path, params=None, _redirects=3):
    url = path if path.startswith("http") else f"https://{HOST}{path}"
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    u = urllib.parse.urlsplit(url)
    target = (u.path or "/") + (f"?{u.query}" if u.query else "")
    headers = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest", "Accept-Encoding": "gzip"}
    for attempt in range(2):   # eine abgelaufene Keep-Alive-Verbindung einmal neu aufbauen
        conn = _conn(u.hostname, fresh=attempt > 0)
        try:
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            break
        except (http.client.HTTPException, OSError):
            if attempt:
                raise
    if resp.status in (301, 302, 303, 307, 308) and _redirects:
        return get(urllib.parse.urljoin(url, resp.getheader("Location", "")), None, _redirects - 1)
    if resp.status >= 400:
        raise OSError(f"HTTP Error {resp.status}: {resp.reason} ({target[:80]})")
    if resp.getheader("Content-Encoding", "") == "gzip":
        body = gzip.decompress(body)
    return body.decode("utf-8", errors="replace")


def text(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def norm(name):
    return text(name).lower()


def popup_html(resp):
    """Antwort von get-team-info: frueher JSON {html: ...}; zur Sicherheit auch reines HTML annehmen."""
    try:
        data = json.loads(resp)
    except ValueError:
        return resp
    if isinstance(data, dict):
        if isinstance(data.get("html"), str):
            return data["html"]
        return " ".join(v for v in data.values() if isinstance(v, str))
    return resp


def team_players(args, cache):
    """Popup beim Klick auf einen Teamnamen: load_team_info(_, team, md_group, division, game)
    -> [(Name, RGX)] der beiden Spieler:innen.
    Versteht das alte Popup (player-team-card, player-name ...) und das neue (rg-team-popup--player ...)."""
    if args not in cache:
        a = [x if x != "null" else "" for x in args] + [""] * 5
        try:
            resp = get("/tournament/get-team-info", {
                "id": a[1], "md_group_id": a[2], "division_id": a[3], "game_id": a[4]})
            raw = popup_html(resp)
            cache.setdefault("popup_sample", resp[:600])
            # je Spielerkarte im Popup: Foto, Name, RGX, Vereinslogo, Verein
            players = []
            for card in re.split(r'class="(?:player-team-card|rg-team-popup--player(?![-\w]))', raw)[1:]:
                name = (re.search(r'class="player-name">(.*?)</div>', card, re.S) or
                        re.search(r'class="[^"]*(?:--name|player-name)(?![-\w])[^"]*"[^>]*>(.*?)</(?:div|span|a|p|h\d)>',
                                  card, re.S))
                score = re.search(r'score="(\d+)"', card) or re.search(r'class="[^"]*(?:score|badge|rgx)[^"]*"[^>]*>\D{0,20}(\d{3,4})\b', card)
                img = (re.search(r'class="player-img">\s*<img[^>]*src="([^"]+)"', card) or
                       re.search(r'<img[^>]*class="[^"]*(?:avatar|photo|player-img)[^"]*"[^>]*src="([^"]+)"', card) or
                       re.search(r'class="[^"]*(?:avatar|photo|player-img)[^"]*"[^>]*>\s*<img[^>]*src="([^"]+)"', card))
                logo = (re.search(r'class="player-club">\s*<img[^>]*src="([^"]+)"', card) or
                        re.search(r'<img[^>]*class="[^"]*(?:club|logo)[^"]*"[^>]*src="([^"]+)"', card) or
                        re.search(r'class="[^"]*club[^"]*"[^>]*>\s*<img[^>]*src="([^"]+)"', card))
                club = next((c for c in map(text, re.findall(r'class="[^"]*club">(.*?)</div>', card, re.S) +
                                         re.findall(r'class="[^"]*club(?:-name)?"[^>]*>(.*?)</(?:div|span|a|p)>', card, re.S))
                             if c), "")
                if name and score and text(name.group(1)):
                    players.append((text(name.group(1)), int(score.group(1))))
                    cache.setdefault("info", {})[players[-1][0]] = {
                        "img": absolute(img.group(1)) if img else "",
                        "logo": absolute(logo.group(1)) if logo else "",
                        "club": club}
            cache[args] = players
        except Exception as e:
            cache.setdefault("errors", []).append(f"Team-Popup: {type(e).__name__}: {e}")
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


def md_group_bodies(page, diag=None):
    """Liga: Die Playerzone rendert nur die Spieltagsgruppe, auf der man landet (z. B. Gruppe A).
    Die anderen Gruppen sind leere Container mit data-md-group-id, deren Inhalt die Seite per
    /tournament/md-group-body/<id> nachlaedt. Das machen wir hier genauso."""
    ids = re.findall(r'data-md-group-id="?(\d+)', page)
    # zur Sicherheit alle Gruppen-Tabs (show_md_group(<id>)) laden; doppelte Spiele fliegen spaeter raus
    ids += [i for i in re.findall(r"show_md_group\((\d+)\)", page) if i != "0"]
    ids = list(dict.fromkeys(ids))
    def body(i):
        try:
            return json.loads(get(f"/tournament/md-group-body/{i}")).get("html", "")
        except Exception as e:
            if diag is not None:
                diag.setdefault("errors", []).append(f"Gruppe {i}: {type(e).__name__}: {e}")
            return ""
    if diag is not None:
        diag["gruppen"] = ids
    return list(POOL.map(body, ids))


def collect_games(page, diag=None):
    """Turnier: Spiele stehen direkt auf der Seite. Liga: Einzelspiele jeder Begegnung nachladen."""
    diag = {} if diag is None else diag
    page = "\n".join([page] + md_group_bodies(page, diag))
    group = re.search(r"group:\s*(\d+)", page)
    def load(enc):
        try:
            return json.loads(get(f"/ajax/get-subgames/{enc}", {"group": group.group(1) if group else ""}))
        except Exception as e:
            diag.setdefault("errors", []).append(f"Begegnung {enc}: {type(e).__name__}: {e}")
            return {}
    encs = list(dict.fromkeys(re.findall(r"show_subgames\('(\d+)'\)", page)))
    diag["begegnungen"] = len(encs)
    subpages = [d.get("html", "") for d in POOL.map(load, encs)]
    games = {}   # doppelte raus; "enc" = Begegnung, zu der ein Einzelspiel gehoert (None = direkt auf der Seite)
    for enc, p in [(None, page)] + list(zip(encs, subpages)):
        for g in parse_cards(p):
            games.setdefault(g["sides"], dict(g, enc=enc))
    diag["karten"] = page.count("<rg-game-card") + sum(p.count("<rg-game-card") for p in subpages)
    diag["spiele"] = len(games)
    return list(games.values())


def absolute(src):
    return urllib.parse.urljoin(f"https://{HOST}/", html.unescape(src)) if src else ""


CACHE_SECONDS = 300   # gleicher Link innerhalb von 5 Minuten (anderer Name, anderes Team) kommt aus dem Speicher
_loaded, _locks, _locks_lock = {}, {}, threading.Lock()


def load(url):
    """Seite laden, alle Spiele sammeln und die Spieler:innen aller Teams holen.
    Das Ergebnis bleibt CACHE_SECONDS im Speicher, damit Teamwechsel und weitere Namen sofort gehen."""
    if urllib.parse.urlparse(url).hostname != HOST:
        raise ValueError(f"Bitte einen Link auf {HOST} einfuegen.")
    key = url.split("#")[0]
    with _locks_lock:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:   # parallele Aufrufe desselben Links laden nur einmal
        hit = _loaded.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        result = _load(key)
        for k in [k for k, (t, _) in _loaded.items() if time.time() - t >= CACHE_SECONDS]:
            del _loaded[k]
        if result[0]:   # leere Ergebnisse (z. B. Fehler) nicht merken
            _loaded[key] = (time.time(), result)
        return result


def _load(url):
    page = get(url)
    cache = {"diag": {"seite": len(page)}}
    games = collect_games(page, cache["diag"])
    list(POOL.map(lambda s: team_players(s, cache), {s for g in games for s in g["sides"]}))
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    return games, cache, text(title.group(1)) if title else ""


def calculate(url, player):
    games, cache, title = load(url)
    key = norm(player)
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
    diag = dict(cache["diag"], teams=sum(1 for s in {s for g in games for s in g["sides"]} if team_players(s, cache)),
                namen=sorted(cache.get("info", {})), popup=cache.get("popup_sample", ""), errors=cache["diag"].get("errors", []) + cache.get("errors", []))
    return rows, title, cards, diag


def team_performance(url, team):
    """Alle Spiele eines Teams am Spieltag. team = Teil des Vereinsnamens oder Name einer Spieler:in.
    Pro Begegnung wird die eigene Seite ueber Verein/Name bestimmt; Einzelspiele ohne Treffer bekommen
    die Seite, auf der die schon gefundenen eigenen Spieler:innen stehen (sonst die uebliche Seite)."""
    games, cache, title = load(url)
    info = cache.get("info", {})
    club = lambda n: info.get(n, {}).get("club", "")
    clubs = Counter(c for g in games for s in g["sides"] for c in {club(n) for n, _ in team_players(s, cache)} if c)
    key = norm(team)
    hit = lambda ps: bool(key) and any(key in norm(n) or key in norm(club(n)) for n, _ in ps)
    rows = []
    for enc in dict.fromkeys(g["enc"] for g in games):
        gs = [(g, [team_players(s, cache) for s in g["sides"]]) for g in games if g["enc"] == enc]
        mine = {}
        for i, (g, sides) in enumerate(gs):
            h = [hit(ps) for ps in sides]
            if h[0] != h[1]:
                mine[i] = h.index(True)
        ours = {n for i, m in mine.items() for n, _ in gs[i][1][m]}
        usual = Counter(mine.values()).most_common(1)
        for i, (g, sides) in enumerate(gs):
            if i not in mine:
                overlap = [len(ours & {n for n, _ in ps}) for ps in sides]
                if overlap[0] != overlap[1]:
                    mine[i] = overlap.index(max(overlap))
                elif enc is not None and usual:
                    mine[i] = usual[0][0]
                else:
                    continue
            m = mine[i]
            own, opp = sides[m], sides[1 - m]
            a, b = g["sets"] if m == 0 else g["sets"][::-1]
            r1 = sum(r for _, r in own) if len(own) == 2 else None
            r2 = sum(r for _, r in opp) if len(opp) == 2 else None
            rows.append({"label": g["label"], "enc": enc, "own": own, "opp": opp, "a": a, "b": b,
                         "delta": delta(r1, r2, a, b) if r1 and r2 else None,
                         "opp_club": Counter(club(n) for n, _ in opp if club(n)).most_common(1)})
    return rows, title, info, clubs


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


def diagnose(d):
    """Kurzer Bericht, was beim Auslesen gefunden wurde (nur wenn kein Spiel passt)."""
    if not d:
        return ""
    e = d.get("errors", [])
    lines = [f"Seite geladen: {d.get('seite', 0)} Zeichen",
             f"Nachgeladene Gruppen: {', '.join(d.get('gruppen', [])) or 'keine'}",
             f"Begegnungen: {d.get('begegnungen', 0)}, Spielkarten: {d.get('karten', 0)}, "
             f"Spiele mit Ergebnis: {d.get('spiele', 0)}, Teams mit Spieler:innen: {d.get('teams', 0)}",
             f"Fehler: {len(e)}" + (f" (z. B. {e[0]})" if e else "")]
    names = d.get("namen", [])
    return ("<details class=note><summary>Diagnose</summary><p>" + "<br>".join(html.escape(l) for l in lines) +
            "</p>" + (f"<p><b>Gefundene Namen:</b> {html.escape(', '.join(names))}</p>" if names else "") +
            (f"<p><b>Popup-Antwort (Anfang):</b></p><pre style='white-space:pre-wrap;font-size:12px'>"
             f"{html.escape(d['popup'])}</pre>" if not d.get("teams") and d.get("popup") else "") +
            "</details>")


def render(player, rows, title, cards=(), diag=None):
    if not rows:
        return (f"<p class=err>Keine gespielten Spiele mit „{html.escape(player)}“ gefunden. "
                "Sind Aufstellungen und Ergebnisse schon eingetragen?</p>" + diagnose(diag))
    total = sum(r["delta"] or 0 for r in rows)
    rounded = math.floor(total)  # Playerzone verbucht ganze Punkte und rundet immer ab (+36,5 -> +36, -9,5 -> -10)
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
            f"<div class=games>{''.join(items)}</div>" + NOTE)


NOTE = ("<p class=note><b>Hinweis:</b> Die tatsächlichen Ergebnisse können um ±1 RGX abweichen, da in der "
        "Playerzone nicht vollständig dokumentiert ist, wann auf- oder abgerundet wird. Generell gibt es keine "
        "Garantie, dass die berechneten Werte stimmen. Alle Angaben ohne Gewähr.</p>")


def result(r):
    return "win" if r["a"] > r["b"] else "loss" if r["a"] < r["b"] else "draw"


def club_logos(info):
    """Verein -> Logo-URL (erstes Logo, das bei einer Spieler:in des Vereins steht)."""
    logos = {}
    for c in info.values():
        if c.get("club") and c.get("logo"):
            logos.setdefault(c["club"], c["logo"])
    return logos


def logo_img(logos, club, css="clogo"):
    src = logos.get(club, "")
    return f'<img class={css} src="{html.escape(src, quote=True)}" referrerpolicy=no-referrer alt="">' if src else ""


def team_chips(url, clubs, label, logos, active=""):
    if not clubs:
        return ""
    q = lambda c: html.escape("/team?" + urllib.parse.urlencode({"url": url, "team": c}), quote=True)
    return (f"<div class=chips><span class=muted>{label}</span>"
            + "".join(f"<a class='chip{' on' if c == active else ''}' href=\"{q(c)}\">{logo_img(logos, c)}{html.escape(c)}</a>"
                      for c, _ in clubs.most_common()) + "</div>")


def render_team(url, team, rows, title, info, clubs):
    logos = club_logos(info)
    if not team:
        return team_chips(url, clubs, "Team wählen:", logos) or "<p class=err>Keine Spiele mit Ergebnissen gefunden.</p>"
    if not rows:
        return (f"<p class=err>Keine gespielten Spiele für „{html.escape(team)}“ gefunden. Teil des Vereinsnamens "
                "oder Name einer Spieler:in eingeben.</p>" + team_chips(url, clubs, "Teams auf diesem Spieltag:", logos))
    encs = list(dict.fromkeys(r["enc"] for r in rows))
    opp = {}
    for e in encs:
        c = Counter(r["opp_club"][0][0] for r in rows if r["enc"] == e and r["opp_club"]).most_common(1)
        opp[e] = c[0][0] if c else "Gegner"
    club = lambda n: info.get(n, {}).get("club", "")
    own_club = Counter(club(n) for r in rows for n, _ in r["own"] if club(n)).most_common(1)
    name = own_club[0][0] if own_club else team
    players = {}
    for r in rows:
        for n, v in r["own"]:
            players.setdefault(n, {"rgx": v, "rows": []})["rows"].append(r)

    def tile(r, me):
        partner = " & ".join(n for n, _ in r["own"] if n != me) or "?"
        tip = f"{r['label']} · mit {partner} vs {' & '.join(n for n, _ in r['opp']) or '?'} · {r['a']}:{r['b']}"
        return (f"<span class='tile {result(r)}' title=\"{html.escape(tip, quote=True)}\">"
                f"<b>{r['a']}:{r['b']}</b><small>{html.escape(r['label'])}</small></span>")

    def bar(rs):
        w, l = sum(result(r) == "win" for r in rs), sum(result(r) == "loss" for r in rs)
        pct = round(100 * w / len(rs))
        return (f"<span class=wl><b class=pos>{w} S</b> · <b class=neg>{l} N</b></span>"
                f"<span class=bar title='{pct} % gewonnen'><i style='width:{pct}%'></i></span>")

    # gleich breite Spalten je Begegnung, damit die Kacheln aller Spieler:innen untereinander stehen
    width = {e: max(sum(r["enc"] == e for r in p["rows"]) for p in players.values()) * 62 for e in encs}
    cards = []
    for n, p in sorted(players.items(), key=lambda kv: -len(kv[1]["rows"])):
        c = info.get(n, {})
        photo = (f'<img class=tphoto src="{html.escape(c["img"], quote=True)}" referrerpolicy=no-referrer alt="">'
                 if c.get("img") else "<div class=tphoto></div>")
        groups = "".join(
            f"<div class=tgrp style='min-width:{width[e]}px'><small>vs {html.escape(opp[e])}</small><div class=tiles>"
            + ("".join(tile(r, n) for r in p["rows"] if r["enc"] == e) or "<span class='tile none'>–</span>")
            + "</div></div>" for e in encs)
        cards.append(f"<div class=tp>{photo}<div class=tpmain><div class=tphead><div><div class=pname>{html.escape(n)}</div>"
                     f"<div class=pclub>{html.escape(c.get('club', ''))}</div></div>"
                     f"<span class=badge><b>RGX</b> {p['rgx'] or '?'}</span></div>"
                     f"<div class=tpstrip>{groups}</div>"
                     f"<div class=tpfoot>{bar(p['rows'])}</div></div></div>")
    matches = []
    for e in encs:
        rs = [r for r in rows if r["enc"] == e]
        w, l = sum(result(r) == "win" for r in rs), sum(result(r) == "loss" for r in rs)
        res = "win" if w > l else "loss" if w < l else "draw"
        matches.append(f"<div class='match {res}'><small>vs</small> {logo_img(logos, opp[e])}<b>{html.escape(opp[e])}</b>"
                       f"<span class=mscore>{w}:{l}</span></div>")
    return (team_chips(url, clubs, "Team wechseln:", logos, name) +
            f"<div class=section><h3 class=teamh>{logo_img(logos, name, 'tlogo')}Team: {html.escape(name)}</h3>"
            f"<span class=muted>{html.escape(title)}</span></div>"
            f"<div class=section><h3>{len(rows)} Spiele</h3><div class=teamsum>{bar(rows)}</div></div>"
            f"<div class=matches>{''.join(matches)}</div>"
            f"<div class=tps>{''.join(cards)}</div>")


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
.note{{margin-top:20px;font-size:.85rem;color:var(--muted);line-height:1.4}}
.tabs{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:1.2rem;border-bottom:2px solid #e3d9c4}}
.tabs a{{padding:.55rem 1.1rem;color:#555;text-decoration:none;border-radius:8px 8px 0 0;margin-bottom:-2px;
  font:600 1.1rem 'Barlow Condensed',sans-serif;text-transform:uppercase;border-bottom:3px solid transparent}}
.tabs a.on{{color:#1d1d1d;background:var(--panel);border-bottom-color:var(--gold)}}
.chips{{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-top:16px}}
.chip{{background:#fff;border:2px solid var(--gold);border-radius:999px;padding:.3rem .85rem;color:#1d1d1d;text-decoration:none;font-weight:600}}
.chip{{display:inline-flex;align-items:center;gap:.4rem}} .chip:hover,.chip.on{{background:var(--gold)}}
.clogo{{width:24px;height:24px;object-fit:contain;flex:none}} .match .clogo{{align-self:center}}
.teamh{{display:flex;align-items:center;gap:.7rem}} .tlogo{{width:52px;height:52px;object-fit:contain;flex:none}}
.teamsum,.tpfoot{{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap}}
.teamsum .bar{{width:180px}} .wl{{white-space:nowrap}}
.bar{{display:inline-block;width:120px;height:10px;border-radius:5px;background:var(--red);overflow:hidden}}
.bar i{{display:block;height:100%;background:var(--green)}}
.matches{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-top:16px}}
.match{{display:flex;align-items:baseline;gap:.4rem;background:#fff;border-radius:8px;padding:10px 14px;border-left:6px solid var(--muted);
  box-shadow:0 2px 6px rgba(0,0,0,.08)}}
.match.win{{border-left-color:var(--green)}} .match.loss{{border-left-color:var(--red)}}
.match b{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.mscore{{margin-left:auto;font:700 1.4rem 'Barlow Condensed',sans-serif}}
.tps{{display:flex;flex-direction:column;gap:14px;margin-top:20px}}
.tp{{display:flex;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 3px 10px rgba(0,0,0,.12)}}
.tphoto{{width:120px;flex-shrink:0;object-fit:cover;background:#ddd}}
.tpmain{{flex:1;min-width:0;padding:12px 16px;display:flex;flex-direction:column;gap:.6rem}}
.tphead{{display:flex;justify-content:space-between;align-items:flex-start;gap:.5rem}}
.tpstrip{{display:flex;flex-wrap:wrap;gap:12px}}
.tgrp>small{{display:block;color:var(--muted);font-size:.75rem;margin-bottom:3px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tiles{{display:flex;gap:4px}}
.tile{{width:58px;height:44px;border-radius:6px;display:flex;flex-direction:column;align-items:center;justify-content:center;
  color:#fff;cursor:default;background:var(--muted)}}
.tile b{{font:700 1.05rem 'Barlow Condensed',sans-serif;line-height:1}}
.tile small{{font-size:.68rem;font-weight:600;max-width:54px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.tile.win{{background:var(--green)}} .tile.loss{{background:var(--red)}} .tile.none{{background:#eee;color:#aaa}}
@media (max-width:700px){{.tphoto{{width:72px;height:90px;align-self:flex-start;margin:12px 0 0 12px;border-radius:8px}} .tpmain{{padding:12px}} .pcards{{grid-template-columns:1fr}} .game{{grid-template-columns:1fr auto}} .res{{grid-column:1}} .delta{{grid-row:1/span 2;grid-column:2}}}}
</style></head><body>
<nav>RGX-Calculator</nav>
<main>
{tabs}
{form}
{body}
</main>
<script>
const h=document.querySelector('.help');if(h){{const v=h.querySelector('video'),show=()=>v.play().catch(()=>{{}}),hide=()=>{{v.pause();v.currentTime=0}};
h.addEventListener('mouseenter',show);h.addEventListener('focus',show);
h.addEventListener('mouseleave',()=>{{if(document.activeElement!==h)hide()}});h.addEventListener('blur',hide)}}
</script></body></html>"""


# Infoseite: Formel Schritt fuer Schritt, interaktiv (Tab "Wie funktioniert die RGX-Berechnung?")
INFO = r"""<style>
:root{--rx-bg:#fffcf5;--surface:#fff;--ink:#1d1d1d;--muted:#7a7468;--line:#e3d9c4;--own:#1f6fa8;--own-soft:#e3eef7;
--opp:#9a6b3e;--opp-soft:#f6ead9;--ball:#f6b83b;--win:#2e9d3a;--win-soft:#e3f3e5;--loss:#c0392b;--loss-soft:#fbe5e2;
--f-display:'Barlow Condensed','Arial Narrow',sans-serif;--f-body:Barlow,system-ui,sans-serif;--f-mono:ui-monospace,Menlo,Consolas,monospace}
.rx { font-family: var(--f-body); font-size: 17px; line-height: 1.55; }
.rx { max-width: 860px; display: flex; flex-direction: column; gap: 28px; }
.rx h1, .rx h2, .rx h3 { font-family: var(--f-display); font-weight: 700; line-height: 1.05; margin: 0; text-wrap: balance; letter-spacing: .01em; }
.rx h1 { font-size: clamp(40px, 7vw, 64px); font-weight: 800; text-transform: uppercase; }
.rx h2 { font-size: 30px; }.rx p { margin: 0; }
.lede { color: var(--muted); max-width: 62ch; font-size: 17px; }
.eyebrow { font-family: var(--f-mono); font-size: 12px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
.mono { font-family: var(--f-mono); }
.num { font-variant-numeric: tabular-nums; }

header { display: flex; flex-direction: column; gap: 12px; }
.ballmark { display: inline-flex; align-items: center; gap: 10px; }
.ballmark svg { flex: none; }

/* Rechner */
.calc { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 22px; display: flex; flex-direction: column; gap: 20px; }
.teams { display: grid; grid-template-columns: 1fr auto 1fr; gap: 14px; align-items: stretch; }
.team { border-radius: 10px; padding: 14px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.team.own { background: var(--own-soft); }
.team.opp { background: var(--opp-soft); }
.team h3 { font-size: 20px; text-transform: uppercase; }
.team.own h3 { color: var(--own); }
.team.opp h3 { color: var(--opp); }
.field { display: flex; flex-direction: column; gap: 3px; }
.field label { font-size: 13px; color: var(--muted); }
.field input {
  font-family: var(--f-mono); font-size: 18px; padding: 8px 10px; width: 100%;
  border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--ink);
}
.field input:focus-visible, .score:focus-visible { outline: 3px solid var(--ball); outline-offset: 1px; }
.rxsum { font-family: var(--f-mono); font-size: 14px; color: var(--muted); }
.rxsum b { color: var(--ink); font-size: 18px; }
.vs { align-self: center; font-family: var(--f-display); font-weight: 800; font-size: 26px; color: var(--muted); }

.scores { display: flex; flex-direction: column; gap: 10px; }
.scoregroups { display: flex; flex-wrap: wrap; gap: 14px 22px; }
.sgroup { display: flex; flex-direction: column; gap: 6px; }
.sgroup span { font-size: 12px; color: var(--muted); }
.sbtns { display: flex; gap: 6px; flex-wrap: wrap; }
.score {
  font-family: var(--f-mono); font-size: 15px; font-weight: 600; padding: 7px 11px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: var(--rx-bg); color: var(--ink);
}
.score.w { border-color: color-mix(in srgb, var(--win) 45%, var(--line)); }
.score.l { border-color: color-mix(in srgb, var(--loss) 45%, var(--line)); }
.score[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }

.rxresult { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; border-top: 1px dashed var(--line); padding-top: 18px; }
.rxresult .big { font-family: var(--f-display); font-weight: 800; font-size: 64px; line-height: 1; }
.rxresult .big.pos { color: var(--win); }
.rxresult .big.neg { color: var(--loss); }
.rxresult .big.zero { color: var(--muted); }
.rxresult p { color: var(--muted); max-width: 46ch; }

/* Schritte */
.steps { display: flex; flex-direction: column; gap: 0; }
.step { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 18px; padding-block: 22px; border-top: 1px solid var(--line); }
.step:first-child { border-top: 0; }
.stepno {
  width: 44px; height: 44px; border-radius: 50%; display: grid; place-items: center;
  background: var(--ball); color: #1d1d1d; font-family: var(--f-display); font-weight: 800; font-size: 24px;
}
.stepbody { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.stepbody > p { max-width: 64ch; }
.formula {
  font-family: var(--f-mono); font-size: 15px; background: var(--surface); border: 1px solid var(--line);
  border-radius: 8px; padding: 10px 14px; overflow-x: auto; white-space: nowrap;
}
.formula .v { font-weight: 600; }
.formula .o { color: var(--own); font-weight: 600; }
.formula .g { color: var(--opp); font-weight: 600; }
.formula .r { color: var(--muted); }
.chart { width: 100%; overflow-x: auto; }
.chart svg { display: block; width: 100%; height: auto; min-width: 560px; }
.chart text { font-family: var(--f-mono); font-size: 11px; fill: var(--muted); }
.chart .lbl { fill: var(--ink); font-weight: 600; font-size: 12px; }

/* Satzstand-Tabelle */
.ptable { display: grid; grid-template-columns: repeat(auto-fill, minmax(118px, 1fr)); gap: 6px; }
.pcell { border-radius: 8px; padding: 8px 10px; background: var(--surface); border: 1px solid var(--line); display: flex; flex-direction: column; gap: 2px; }
.pcell .s { font-family: var(--f-mono); font-weight: 600; font-size: 15px; }
.pcell .alt { font-family: var(--f-mono); font-size: 11px; color: var(--muted); }
.pcell .pv { font-family: var(--f-mono); font-size: 13px; color: var(--muted); }
.pcell.on { outline: 3px solid var(--ball); outline-offset: -1px; }

/* Rundung */
.round { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; font-family: var(--f-mono); font-size: 18px; }
.rxchip { padding: 6px 12px; border-radius: 999px; background: var(--surface); border: 1px solid var(--line); }
.rxchip.final { background: var(--ink); color: var(--surface); border-color: var(--ink); font-weight: 600; }
.arrow { color: var(--muted); }
.rxnote { font-size: 14px; color: var(--muted); max-width: 64ch; }

/* Faustregeln */
.rules { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.rule { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; display: flex; flex-direction: column; gap: 4px; }
.rule b { font-family: var(--f-display); font-size: 22px; font-weight: 700; }
.rule span { font-size: 14px; color: var(--muted); }
.rxlegend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13px; color: var(--muted); }
.rxlegend i { display: inline-block; width: 14px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 6px; }
footer { font-size: 13px; color: var(--muted); }

@media (max-width: 600px) {
  .teams { grid-template-columns: 1fr; }
  .vs { justify-self: center; }
  .step { grid-template-columns: minmax(0, 1fr); gap: 10px; }
  .calc { padding: 16px; }
  .rules { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: no-preference) {
  .chart circle.marker, .chart line.marker { transition: cx .25s, cy .25s, x1 .25s, x2 .25s; }
}
</style>


<div class="rx">
  <header>
    <div class="ballmark">
      <svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true"><circle cx="17" cy="17" r="15" fill="var(--ball)"/><path d="M5 12c8 3 16 3 24 0M5 22c8-3 16-3 24 0" stroke="#13222E" stroke-width="1.6" fill="none"/></svg>
      <span class="eyebrow">Roundnet Germany · RGX</span>
    </div>
    <h1>So entsteht dein RGX-Wert</h1>
    <p class="lede">Pro Spiel vergleicht die Formel, was von deinem Team <b>erwartet</b> wurde, mit dem, was ihr <b>erreicht</b> habt. Die Lücke dazwischen wird zu Punkten. Probier es unten aus: Alle Schritte rechnen live mit.</p>
  </header>

  <section class="calc" aria-label="Rechner">
    <div class="teams">
      <div class="team own">
        <h3>Dein Team</h3>
        <div class="field"><label for="a1">Du (RGX)</label><input id="a1" type="number" inputmode="numeric" value="980" step="10"></div>
        <div class="field"><label for="a2">Partner:in (RGX)</label><input id="a2" type="number" inputmode="numeric" value="1020" step="10"></div>
        <div class="rxsum">r1 = <b class="num" id="r1o">2000</b></div>
      </div>
      <div class="vs">vs</div>
      <div class="team opp">
        <h3>Gegner</h3>
        <div class="field"><label for="b1">Gegner:in 1 (RGX)</label><input id="b1" type="number" inputmode="numeric" value="1040" step="10"></div>
        <div class="field"><label for="b2">Gegner:in 2 (RGX)</label><input id="b2" type="number" inputmode="numeric" value="1060" step="10"></div>
        <div class="rxsum">r2 = <b class="num" id="r2o">2100</b></div>
      </div>
    </div>

    <div class="scores">
      <span class="eyebrow">Satzstand aus deiner Sicht</span>
      <div class="scoregroups" id="scoregroups"></div>
    </div>

    <div class="rxresult" aria-live="polite">
      <div class="big num" id="big">−7,5</div>
      <p id="bigtext">RGX-Punkte für dich in diesem Spiel.</p>
    </div>
  </section>

  <section aria-label="Rechenweg">
    <h2>Der Rechenweg in fünf Schritten</h2>
    <div class="steps">

      <div class="step">
        <div class="stepno">1</div>
        <div class="stepbody">
          <h3>Team-RGX addieren</h3>
          <p>Jedes Team bekommt einen gemeinsamen Wert: die Summe der RGX beider Spieler:innen. Wichtig ist am Ende nur der <b>Abstand</b> zwischen den beiden Teams.</p>
          <div class="formula" id="f1"></div>
        </div>
      </div>

      <div class="step">
        <div class="stepno">2</div>
        <div class="stepbody">
          <h3>Erwartung berechnen (e1)</h3>
          <p>Aus dem Abstand folgt, wie viel vom Spiel dein Team „holen sollte“, zwischen 0 und 1. Gleich starke Teams liegen bei 0,5. Bei 550 Punkten Vorsprung erwartet die Formel schon 0,91.</p>
          <div class="formula" id="f2"></div>
          <div class="chart" id="curve"></div>
        </div>
      </div>

      <div class="step">
        <div class="stepno">3</div>
        <div class="stepbody">
          <h3>Ergebnis in eine Zahl übersetzen (p)</h3>
          <p>Der Satzstand wird zu einem Wert zwischen 0 und 1. Ein klarer Sieg zählt 1, eine klare Niederlage 0. Knappe Spiele liegen dazwischen.</p>
          <div class="ptable" id="ptable"></div>
        </div>
      </div>

      <div class="step">
        <div class="stepno">4</div>
        <div class="stepbody">
          <h3>Lücke mal 50 (x1)</h3>
          <p>Die Differenz zwischen Erreicht (p) und Erwartet (e1) wird mit 50 multipliziert. Bei Spielen mit nur einem Satz zählt das Ganze nur zu 75&nbsp;% (b = 0,75). Das ist das Plus oder Minus für das <b>ganze Team</b>.</p>
          <div class="chart" id="gap"></div>
          <div class="formula" id="f4"></div>
        </div>
      </div>

      <div class="step">
        <div class="stepno">5</div>
        <div class="stepbody">
          <h3>Runden und teilen</h3>
          <p>Der Teamwert wird auf eine ganze Zahl gerundet (x,5 immer weg von der Null) und dann halbiert. Deshalb gibt es pro Spiel Werte in 0,5er-Schritten.</p>
          <div class="round" id="roundrow"></div>
          <p class="rxnote">Am Ende eines Turniers oder Spieltags werden alle Spiele addiert. Weil der RGX nur in ganzen Punkten verbucht wird, wird diese Summe <b>abgerundet</b>: aus +36,5 werden +36, aus −9,5 werden −10.</p>
        </div>
      </div>

    </div>
  </section>

  <section aria-label="Überblick" style="display:flex;flex-direction:column;gap:14px">
    <h2>Wie viel ist drin?</h2>
    <p class="lede">Punkte pro Spieler:in je nach RGX-Abstand, für Spiele über zwei oder drei Sätze. Der gelbe Strich ist dein Abstand von oben.</p>
    <div class="chart" id="overview"></div>
    <div class="rxlegend" id="rxlegend"></div>
    <div class="rules">
      <div class="rule"><b>Höchstens ±25</b><span>pro Spieler:in und Spiel. So viel gibt es nur für einen Sieg gegen ein haushoch überlegenes Team.</span></div>
      <div class="rule"><b>Gleich stark = ±12,5</b><span>bei gleichem Team-RGX bringt ein 2:0 genau +12,5, ein 0:2 genau −12,5.</span></div>
      <div class="rule"><b>Favorit verliert mehr</b><span>Je größer dein Vorsprung, desto weniger bringt ein Sieg und desto mehr kostet eine Niederlage.</span></div>
      <div class="rule"><b>1 Satz = 75 %</b><span>Spiele mit nur einem Satz zählen weniger, weil das Ergebnis zufälliger ist.</span></div>
    </div>
  </section>

  <footer>Formel wie in den anderen Tabs: e1 = 1 / (1 + 10^((r2 − r1) / 550)), x1 = b · 50 · (p − e1), pro Spieler:in round(x1) / 2. Gerechnet wird mit dem RGX vor dem Spiel.</footer>
</div>

<script>

const P = {"1:0":1,"2:0":1,"3:0":1,"3:1":.75,"2:1":.67,"3:2":.6,"1:1":.5,"2:2":.5,"2:3":.4,"1:2":.33,"1:3":.25,"0:1":0,"0:2":0,"0:3":0};
const GROUPS = [
  ["1 Satz", ["1:0","0:1"]],
  ["2 Sätze", ["2:0","1:1","0:2"]],
  ["3 Sätze", ["2:1","1:2"]],
  ["Best of 5", ["3:0","3:1","3:2","2:3","1:3","0:3"]],
];
let score = "0:1";
const $ = id => document.getElementById(id);
const fmt = (x, d = 1) => {
  const s = Math.abs(x).toFixed(d).replace(".", ",");
  return (x > 0 ? "+" : x < 0 ? "−" : "±") + s;
};
const fnum = (x, d = 3) => x.toFixed(d).replace(".", ",").replace("-", "−");
const roundHalfAway = x => x >= 0 ? Math.floor(x + .5) : -Math.floor(-x + .5);
const expected = d => 1 / (1 + Math.pow(10, -d / 550)); // d = r1 - r2
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// Satzstand-Buttons und p-Tabelle
const sg = $("scoregroups"), pt = $("ptable");
GROUPS.forEach(([name, list]) => {
  const g = document.createElement("div"); g.className = "sgroup";
  g.innerHTML = `<span>${name}</span><div class="sbtns"></div>`;
  list.forEach(s => {
    const [a, b] = s.split(":").map(Number);
    const btn = document.createElement("button");
    btn.className = "score " + (a > b ? "w" : a < b ? "l" : "");
    btn.textContent = s; btn.dataset.s = s; btn.type = "button";
    btn.addEventListener("click", () => { score = s; update(); });
    g.querySelector(".sbtns").appendChild(btn);
  });
  sg.appendChild(g);
});
["1:0","3:1","2:1","3:2","1:1","2:3","1:2","1:3","0:1"].forEach(s => {
  const alt = s === "1:0" ? "auch 2:0, 3:0" : s === "0:1" ? "auch 0:2, 0:3" : s === "1:1" ? "auch 2:2" : "";
  const c = document.createElement("div"); c.className = "pcell"; c.dataset.p = P[s];
  c.innerHTML = `<span class="s">${s}</span><span class="alt">${alt || "&nbsp;"}</span><span class="pv">p = ${String(P[s]).replace(".", ",")}</span>`;
  pt.appendChild(c);
});

const NS = "http://www.w3.org/2000/svg";
function svg(w, h) { const s = document.createElementNS(NS, "svg"); s.setAttribute("viewBox", `0 0 ${w} ${h}`); return s; }
function el(tag, attrs, parent) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); parent && parent.appendChild(e); return e; }
function text(t, attrs, parent) { const e = el("text", attrs, parent); e.textContent = t; return e; }

// Schritt 2: Erwartungskurve
function drawCurve(d, e) {
  const W = 760, H = 260, L = 46, R = 16, T = 16, B = 40;
  const xs = v => L + (v + 1000) / 2000 * (W - L - R);
  const ys = v => T + (1 - v) * (H - T - B);
  const s = svg(W, H);
  [0, .25, .5, .75, 1].forEach(v => {
    el("line", {x1: L, x2: W - R, y1: ys(v), y2: ys(v), stroke: css("--line"), "stroke-width": 1}, s);
    text(String(v).replace(".", ","), {x: L - 8, y: ys(v) + 4, "text-anchor": "end"}, s);
  });
  [-1000, -550, 0, 550, 1000].forEach(v => {
    text((v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v), {x: xs(v), y: H - B + 18, "text-anchor": "middle"}, s);
  });
  text("Abstand r1 − r2 (dein Team minus Gegner)", {x: (L + W - R) / 2, y: H - 4, "text-anchor": "middle"}, s);
  let path = "";
  for (let v = -1000; v <= 1000; v += 10) path += (v === -1000 ? "M" : "L") + xs(v).toFixed(1) + " " + ys(expected(v)).toFixed(1);
  el("path", {d: path, fill: "none", stroke: css("--own"), "stroke-width": 3}, s);
  const cd = Math.max(-1000, Math.min(1000, d));
  el("line", {x1: xs(cd), x2: xs(cd), y1: ys(0), y2: ys(e), stroke: css("--ball"), "stroke-width": 2, "stroke-dasharray": "4 3"}, s);
  el("line", {x1: L, x2: xs(cd), y1: ys(e), y2: ys(e), stroke: css("--ball"), "stroke-width": 2, "stroke-dasharray": "4 3"}, s);
  el("circle", {cx: xs(cd), cy: ys(e), r: 7, fill: css("--ball"), stroke: css("--ink"), "stroke-width": 2}, s);
  const right = xs(cd) < W - 200;
  text(`e1 = ${fnum(e)}`, {x: xs(cd) + (right ? 12 : -12), y: ys(e) - 10, "text-anchor": right ? "start" : "end", class: "lbl"}, s);
  $("curve").replaceChildren(s);
}

// Schritt 4: Lücke zwischen erwartet und erreicht
function drawGap(e, p, x1) {
  const W = 760, H = 120, L = 20, R = 20, Y = 62;
  const xs = v => L + v * (W - L - R);
  const s = svg(W, H);
  el("rect", {x: L, y: Y - 6, width: W - L - R, height: 12, rx: 6, fill: css("--line")}, s);
  const good = p >= e;
  el("rect", {x: xs(Math.min(e, p)), y: Y - 6, width: Math.max(2, Math.abs(xs(p) - xs(e))), height: 12, rx: 3, fill: good ? css("--win") : css("--loss")}, s);
  [0, .5, 1].forEach(v => text(String(v).replace(".", ","), {x: xs(v), y: Y + 30, "text-anchor": v === 0 ? "start" : v === 1 ? "end" : "middle"}, s));
  // Marker
  const mk = (v, col, label, up) => {
    el("line", {x1: xs(v), x2: xs(v), y1: Y - 14, y2: Y + 14, stroke: col, "stroke-width": 3}, s);
    const anchor = v < .12 ? "start" : v > .88 ? "end" : "middle";
    text(label, {x: xs(v), y: up ? Y - 22 : Y + 48, "text-anchor": anchor, class: "lbl", fill: col}, s);
  };
  const close = Math.abs(xs(p) - xs(e)) < 150;
  mk(e, css("--own"), `erwartet e1 = ${fnum(e, 2)}`, true);
  mk(p, css("--ink"), `erreicht p = ${fnum(p, 2)}`, !close ? true : false);
  $("gap").replaceChildren(s);
}

// Überblick: Punkte pro Spieler:in nach Abstand
const LINES = [
  ["2:0", "--win", ""], ["2:1", "--win", "6 4"], ["1:1", "--muted", ""], ["1:2", "--loss", "6 4"], ["0:2", "--loss", ""],
];
function drawOverview(d) {
  const W = 760, H = 300, L = 46, R = 46, T = 14, B = 40;
  const xs = v => L + (v + 800) / 1600 * (W - L - R);
  const ys = v => T + (25 - v) / 50 * (H - T - B);
  const s = svg(W, H);
  [-25, -12.5, 0, 12.5, 25].forEach(v => {
    el("line", {x1: L, x2: W - R, y1: ys(v), y2: ys(v), stroke: css("--line"), "stroke-width": v === 0 ? 1.6 : 1}, s);
    text(fmt(v, v % 1 ? 1 : 0).replace("±", ""), {x: L - 8, y: ys(v) + 4, "text-anchor": "end"}, s);
  });
  [-800, -400, 0, 400, 800].forEach(v => text((v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v), {x: xs(v), y: H - B + 18, "text-anchor": "middle"}, s));
  text("Abstand r1 − r2", {x: (L + W - R) / 2, y: H - 4, "text-anchor": "middle"}, s);
  const cd = Math.max(-800, Math.min(800, d));
  el("line", {x1: xs(cd), x2: xs(cd), y1: T, y2: H - B, stroke: css("--ball"), "stroke-width": 3}, s);
  LINES.forEach(([sc, col, dash]) => {
    let path = "";
    for (let v = -800; v <= 800; v += 10) {
      const val = 50 * (P[sc] - expected(v)) / 2;
      path += (v === -800 ? "M" : "L") + xs(v).toFixed(1) + " " + ys(val).toFixed(1);
    }
    el("path", {d: path, fill: "none", stroke: css(col), "stroke-width": 2.5, "stroke-dasharray": dash}, s);
    const endV = 50 * (P[sc] - expected(800)) / 2;
    text(sc, {x: W - R + 6, y: ys(endV) + 4, class: "lbl", fill: css(col)}, s);
  });
  $("overview").replaceChildren(s);
  $("rxlegend").innerHTML = LINES.map(([sc, col, dash]) =>
    `<span><i style="background:var(${col});${dash ? "background:repeating-linear-gradient(90deg,var(" + col + ") 0 5px,transparent 5px 8px)" : ""}"></i>${sc}</span>`).join("");
}

function update() {
  const v = id => Number($(id).value) || 0;
  const a1 = v("a1"), a2 = v("a2"), b1 = v("b1"), b2 = v("b2");
  const r1 = a1 + a2, r2 = b1 + b2, d = r1 - r2;
  const e = expected(d), p = P[score];
  const [sa, sb] = score.split(":").map(Number);
  const b = sa + sb === 1 ? .75 : 1;
  const x1 = b * 50 * (p - e);
  const rx = roundHalfAway(x1), per = rx / 2;

  $("r1o").textContent = r1; $("r2o").textContent = r2;
  document.querySelectorAll(".score").forEach(btn => btn.setAttribute("aria-pressed", btn.dataset.s === score));
  document.querySelectorAll(".pcell").forEach(c => c.classList.toggle("on", Number(c.dataset.p) === p));

  const big = $("big");
  big.textContent = fmt(per);
  big.className = "big num " + (per > 0 ? "pos" : per < 0 ? "neg" : "zero");
  const res = sa > sb ? "Sieg" : sa < sb ? "Niederlage" : "Unentschieden";
  $("bigtext").textContent = `RGX-Punkte für dich (und deine:n Partner:in) bei ${res} ${score}. Fürs ganze Team sind es ${fmt(rx, 0)}.`;

  $("f1").innerHTML = `r1 = <span class="o">${a1} + ${a2} = ${r1}</span> &nbsp;&nbsp; r2 = <span class="g">${b1} + ${b2} = ${r2}</span> &nbsp;&nbsp; <span class="r">Abstand</span> <span class="v">${d > 0 ? "+" : d < 0 ? "−" : ""}${Math.abs(d)}</span>`;
  $("f2").innerHTML = `e1 = 1 / (1 + 10^((<span class="g">${r2}</span> − <span class="o">${r1}</span>) / 550)) = <span class="v">${fnum(e)}</span>`;
  $("f4").innerHTML = `x1 = <span class="v">${String(b).replace(".", ",")}</span> · 50 · (<span class="v">${fnum(p, 2)}</span> − <span class="o">${fnum(e)}</span>) = <span class="v">${fnum(x1, 2)}</span> <span class="r">${b < 1 ? "← 1 Satz, daher 0,75" : "← mehrere Sätze, b = 1"}</span>`;
  $("roundrow").innerHTML = `<span class="rxchip">x1 = ${fnum(x1, 2)}</span><span class="arrow">runden →</span><span class="rxchip">${fmt(rx, 0)}</span><span class="arrow">÷ 2 →</span><span class="rxchip final">${fmt(per)} pro Person</span>`;

  drawCurve(d, e); drawGap(e, p, x1); drawOverview(d);
}
["a1","a2","b1","b2"].forEach(id => $(id).addEventListener("input", update));
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", update);
new MutationObserver(update).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
update();
</script>
"""

HELP = """<span class=help tabindex=0 aria-label="Wo finde ich den Link?">?<span class=tip>
<b>Wo finde ich den Link?</b><video src="/link_tour.mp4" muted loop playsinline preload=none></video></span></span>"""

FORM_RGX = """<form method=get action="/">
<div class=urlbox><input name=url placeholder="Playerzone-Link (Turnier-Spielplan oder Liga-Spieltag)" value="{url}" required>
{help}</div>
<input name=player placeholder="Name der Spieler:in" value="{player}" required>
<button>Berechnen</button></form>"""

FORM_TEAM = """<form method=get action="/team">
<div class=urlbox><input name=url placeholder="Playerzone-Link zum Liga-Spieltag" value="{url}" required>
{help}</div>
<input name=team placeholder="Team / Verein (optional)" value="{player}">
<button>Anzeigen</button></form>"""


def tabs(active, url):
    q = "?" + urllib.parse.urlencode({"url": url}) if url else ""
    return "<div class=tabs>" + "".join(
        f'<a href="{path}{html.escape(q, quote=True)}"{" class=on" if path == active else ""}>{name}</a>'
        for path, name in (("/", "RGX-Bilanz"), ("/team", "Teamperformance"),
                            ("/formel", "Wie funktioniert die RGX-Berechnung?"))) + "</div>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/link_tour.mp4":
            return self.send_video()
        parsed = urllib.parse.urlparse(self.path)
        team_tab = parsed.path.rstrip("/") == "/team"
        q = urllib.parse.parse_qs(parsed.query)
        url = q.get("url", [""])[0].strip()
        if parsed.path.rstrip("/") == "/formel":
            return self.send_page(PAGE.format(tabs=tabs("/formel", url), form="", body=INFO))
        player = q.get("team" if team_tab else "player", [""])[0].strip()
        body = ""
        try:
            if team_tab and url:
                body = render_team(url, player, *team_performance(url, player))
            elif url and player:
                body = render(player, *calculate(url, player))
        except Exception as e:
            body = f"<p class=err>Fehler: {html.escape(str(e))}</p>"
        form = (FORM_TEAM if team_tab else FORM_RGX).format(
            url=html.escape(url, quote=True), player=html.escape(player, quote=True), help=HELP)
        self.send_page(PAGE.format(tabs=tabs("/team" if team_tab else "/", url), form=form, body=body))

    def send_page(self, page):
        data = page.encode("utf-8")
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
