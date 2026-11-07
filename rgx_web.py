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
import html
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
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(body, ids))


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
    with ThreadPoolExecutor(max_workers=8) as pool:
        subpages = [d.get("html", "") for d in pool.map(load, encs)]
    games = {}   # doppelte raus; "enc" = Begegnung, zu der ein Einzelspiel gehoert (None = direkt auf der Seite)
    for enc, p in [(None, page)] + list(zip(encs, subpages)):
        for g in parse_cards(p):
            games.setdefault(g["sides"], dict(g, enc=enc))
    diag["karten"] = page.count("<rg-game-card") + sum(p.count("<rg-game-card") for p in subpages)
    diag["spiele"] = len(games)
    return list(games.values())


def absolute(src):
    return urllib.parse.urljoin(f"https://{HOST}/", html.unescape(src)) if src else ""


def load(url):
    """Seite laden, alle Spiele sammeln und die Spieler:innen aller Teams holen."""
    if urllib.parse.urlparse(url).hostname != HOST:
        raise ValueError(f"Bitte einen Link auf {HOST} einfuegen.")
    page = get(url)
    cache = {"diag": {"seite": len(page)}}
    games = collect_games(page, cache["diag"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda s: team_players(s, cache), {s for g in games for s in g["sides"]}))
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
.tabs{{display:flex;gap:4px;margin-bottom:1.2rem;border-bottom:2px solid #e3d9c4}}
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
const h=document.querySelector('.help'),v=h.querySelector('video'),show=()=>v.play().catch(()=>{{}}),hide=()=>{{v.pause();v.currentTime=0}};
h.addEventListener('mouseenter',show);h.addEventListener('focus',show);
h.addEventListener('mouseleave',()=>{{if(document.activeElement!==h)hide()}});h.addEventListener('blur',hide);
</script></body></html>"""


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
        for path, name in (("/", "RGX-Bilanz"), ("/team", "Teamperformance"))) + "</div>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/link_tour.mp4":
            return self.send_video()
        parsed = urllib.parse.urlparse(self.path)
        team_tab = parsed.path.rstrip("/") == "/team"
        q = urllib.parse.parse_qs(parsed.query)
        url = q.get("url", [""])[0].strip()
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
        data = PAGE.format(tabs=tabs("/team" if team_tab else "/", url), form=form, body=body).encode("utf-8")
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
