#!/usr/bin/env python3
"""Sito GEX: scatta da solo le catene SPX e NDX agli orari del piano e mostra livelli e struttura.

Un solo processo: il server web e, in un thread, lo scheduler. Stanno insieme perche'
leggono e scrivono gli stessi file: su Railway un cron separato non vedrebbe il volume.

    python3 app.py                       # in locale su http://127.0.0.1:8000
    GEX_PASSWORD=... python3 app.py      # con password, ascolta su tutte le interfacce

Variabili: GEX_PASSWORD (obbligatoria su Railway), PORT, GEX_DATI (cartella dei dati; su
Railway basta montare un volume), GEX_SCHEDULER=0 per spegnere gli scatti automatici.
"""
import base64
import hmac
import html
import json
import os
import re
import sys
import threading
import time as tempo
import traceback
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import scarica_gex as g
import storico
import struttura as st

# Orari di New York, cosi' i cambi di ora legale non richiedono modifiche.
SLOT = [("0230", "mattina, piano di giornata (8:30 a Roma)"),
        ("0947", "primo dato dopo l'apertura (15:47 a Roma)"),
        ("1540", "prima della chiusura (21:40 a Roma)"),
        ("1620", "barre a 1 minuto della sessione (22:20 a Roma)")]
FINESTRA = timedelta(minutes=30)     # se uno scatto fallisce, riprova per mezz'ora
PAUSA_RIPROVA = 180                  # secondi fra due tentativi
NOME_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_(SPX|NDX)\.json\.gz$")
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
PASSWORD = os.getenv("GEX_PASSWORD", "")
SU_RAILWAY = bool(os.getenv("RAILWAY_SERVICE_ID"))

blocco = threading.Lock()


# --- scatti ---------------------------------------------------------------------------

def leggi_stato() -> dict:
    p = g.DATI / "stato.json"
    return json.loads(p.read_text()) if p.exists() else {}


def scrivi_stato(chiave: str, voce: dict) -> None:
    g.DATI.mkdir(parents=True, exist_ok=True)
    stato = leggi_stato()
    stato[chiave] = voce
    tmp = g.DATI / "stato.tmp"
    tmp.write_text(json.dumps(dict(sorted(stato.items())[-60:]), indent=1))
    tmp.replace(g.DATI / "stato.json")


def scatta(sym: str) -> str:
    """Scarica e salva la catena; a mercato aperto aggiorna anche l'offset."""
    with blocco:
        g.DATI.mkdir(parents=True, exist_ok=True)
        payload = g.scarica(sym)
        p = g.salva_json(payload, datetime.now(g.ROMA), sym)
        try:
            g.analizza(payload, salva=True)
        except (g.OffsetMancante, ValueError) as e:
            print(f"offset non aggiornato: {e}", flush=True)
        try:
            storico.registra(p.name, payload)
        except Exception as e:   # il JSON e' salvato: all'avvio successivo viene recuperato
            print(f"storico non aggiornato per {p.name}: {e}", flush=True)
        return p.name


def barre_del_giorno(sym: str, ora_ny: datetime) -> str:
    with blocco:
        giorno, n = storico.salva_barre(sym)
    if giorno != f"{ora_ny:%Y-%m-%d}":
        return f"nessuna sessione oggi (ultime barre {giorno})"   # festivita' USA
    return f"{n} barre del {giorno}"


def esegui(hhmm: str, sym: str, ora_ny: datetime) -> str:
    if hhmm == "1620":
        return barre_del_giorno(sym, ora_ny)
    nome = scatta(sym)
    if hhmm == "0230":   # recupera le barre di ieri se la sera lo scatto e' saltato
        try:
            with blocco:
                storico.salva_barre(sym)
        except Exception as e:
            print(f"barre {sym} non recuperate: {e}", flush=True)
    return nome


def controlla_slot(ora_ny: datetime) -> None:
    if ora_ny.weekday() >= 5:
        return
    for hhmm, _ in SLOT:
        inizio = ora_ny.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
        if not inizio <= ora_ny < inizio + FINESTRA:
            continue
        for sym in g.INDICI:
            chiave = f"{ora_ny:%Y-%m-%d}_{hhmm}_{sym}"
            voce = leggi_stato().get(chiave, {})
            if voce.get("esito") == "ok" or tempo.time() - voce.get("t", 0) < PAUSA_RIPROVA:
                continue
            tentativi = voce.get("tentativi", 0) + 1
            try:
                nome = esegui(hhmm, sym, ora_ny)
                scrivi_stato(chiave, {"esito": "ok", "file": nome, "t": tempo.time(), "tentativi": tentativi})
                print(f"slot {chiave}: {nome}", flush=True)
            except Exception as e:
                scrivi_stato(chiave, {"esito": "errore", "errore": str(e)[:200], "t": tempo.time(), "tentativi": tentativi})
                print(f"slot {chiave}: ERRORE {e}", flush=True)


def scheduler() -> None:
    while True:
        try:
            controlla_slot(datetime.now(g.NY))
        except Exception:
            traceback.print_exc()
        tempo.sleep(30)


def prossimo_slot(ora_ny: datetime) -> datetime:
    for giorni in range(8):
        d = ora_ny + timedelta(days=giorni)
        if d.weekday() >= 5:
            continue
        for hhmm, _ in SLOT:
            t = d.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
            if t > ora_ny:
                return t
    raise RuntimeError("nessuno slot")


def scarichi(sym: str) -> list[str]:
    if not g.DATI.exists():
        return []
    return sorted((p.name for p in g.DATI.glob(f"*_{sym}.json.gz") if NOME_FILE.match(p.name)), reverse=True)


# --- grafica --------------------------------------------------------------------------

def svg_profilo(an: dict) -> str:
    prof, off = an["prof"], an["off"]
    tag = {}
    for k, nome in [(an["flip"], "FLIP"), (an["gmax"], "GAMMA MAX")]:
        if k is not None:
            tag.setdefault(k, []).append(nome)
    for i, k in enumerate(an["c"]):
        tag.setdefault(k, []).append(f"C{i + 1}")
    for i, k in enumerate(an["p"]):
        tag.setdefault(k, []).append(f"P{i + 1}")

    riga, sx, W = 16, 66, 660
    centro, meta = sx + 240, 230
    top = max(abs(r[1]) for r in prof) or 1
    H = riga * len(prof) + 12
    el = [f'<line x1="{centro}" x2="{centro}" y1="0" y2="{H}" stroke="#555"/>']
    righe = list(reversed(prof))
    for i, (k, n, _, _) in enumerate(righe):
        y = 6 + i * riga
        lung = abs(n) / top * meta
        col = "#4caf50" if n >= 0 else "#d76ad0"
        x = centro if n >= 0 else centro - lung
        el.append(f'<rect x="{x:.1f}" y="{y + 2}" width="{lung:.1f}" height="{riga - 5}" fill="{col}"/>')
        el.append(f'<text x="{sx - 8}" y="{y + 12}" fill="#9aa3ad" font-size="11" text-anchor="end">{k + off:.2f}</text>')
        if k in tag:
            el.append(f'<text x="{centro + meta + 8}" y="{y + 12}" fill="#f0c040" font-size="11">{" ".join(tag[k])}</text>')
    prezzo = an["spot"]
    k_alto, k_basso = righe[0][0], righe[-1][0]
    if k_basso <= prezzo <= k_alto:
        y = 6 + (k_alto - prezzo) / (k_alto - k_basso) * riga * (len(righe) - 1) + riga / 2
        el.append(f'<line x1="{sx}" x2="{W}" y1="{y:.1f}" y2="{y:.1f}" stroke="#5aa9e6" stroke-dasharray="5 4"/>')
        el.append(f'<text x="{W - 4}" y="{y - 4:.1f}" fill="#5aa9e6" font-size="11" text-anchor="end">{"prezzo" if g.in_sessione(an["ts_ny"]) else "chiusura"} {prezzo + off:.2f}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" style="width:100%;height:auto" '
            f'font-family="Helvetica, Arial" role="img" aria-label="Profilo del gamma netto per strike">{"".join(el)}</svg>')


CSS = """
:root{color-scheme:dark;--bg:#14161a;--pan:#1c1f25;--bordo:#2e333b;--testo:#e4e7eb;--tenue:#9aa3ad;--ok:#4caf50;--ko:#e05555;--giallo:#f0c040}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--testo);font:15px/1.45 Helvetica,Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 12px}h2{font-size:15px;margin:0 0 10px}
.pan{background:var(--pan);border:1px solid var(--bordo);border-radius:8px;padding:14px;margin-bottom:14px;min-width:0}
.dati{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.dati b{display:block;font-size:17px}.dati span{color:var(--tenue);font-size:12px}
.due{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:14px;align-items:start}@media(max-width:820px){.due{grid-template-columns:minmax(0,1fr)}}
form.filtri{display:flex;flex-wrap:wrap;gap:10px;align-items:end}label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--tenue)}label.riga{flex-direction:row;align-items:center;gap:6px;padding-bottom:7px}
select,input[type=number],button{background:#111317;color:var(--testo);border:1px solid var(--bordo);border-radius:6px;padding:6px 8px;font:inherit}input[type=number]{width:96px}button{cursor:pointer}button.primo{background:#2b5d8a;border-color:#2b5d8a}
table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:3px 6px;border-bottom:1px solid var(--bordo);text-align:left;vertical-align:top}th{color:var(--tenue);font-weight:normal;font-size:12px}td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scorri{overflow-x:auto}.avviso{border-color:var(--ko)}.nota{color:var(--tenue);font-size:12px;margin:8px 0 0}.ok{color:var(--ok)}.ko{color:var(--ko)}details summary{cursor:pointer;color:var(--tenue);font-size:13px;margin-top:10px}
a{color:#7fbdf0}textarea{width:100%;background:#111317;color:var(--testo);border:1px solid var(--bordo);border-radius:6px;padding:6px;font:12px/1.4 Menlo,monospace;margin:8px 0}svg{display:block}.grafico{overflow-x:auto}.grafico svg{min-width:480px}
"""


def e(x) -> str:
    return html.escape(str(x), quote=True)


def stato_html(ora_ny: datetime) -> str:
    stato = leggi_stato()
    righe = []
    for hhmm, nome in SLOT:
        inizio = ora_ny.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
        for sym in g.INDICI:
            v = stato.get(f"{ora_ny:%Y-%m-%d}_{hhmm}_{sym}")
            if ora_ny.weekday() >= 5:
                esito = '<span>weekend</span>'
            elif v and v["esito"] == "ok":
                esito = f'<span class="ok">ok</span> {e(v["file"])}'
            elif v:
                esito = f'<span class="ko">errore</span> {e(v.get("errore", ""))} ({v.get("tentativi")} tentativi)'
            elif ora_ny >= inizio + FINESTRA:
                esito = '<span class="ko">saltato</span>'
            else:
                esito = "in attesa"
            righe.append(f"<tr><td>{e(nome)}</td><td>{sym}</td><td>{esito}</td></tr>")
    prossimo = prossimo_slot(ora_ny).astimezone(g.ROMA)
    return (f'<details><summary>Scatti automatici di oggi, prossimo {prossimo:%d/%m %H:%M} a Roma</summary>'
            f'<div class="scorri"><table>{"".join(righe)}</table></div></details>')


def float_o_none(q: dict, nome: str) -> float | None:
    try:
        return float(q.get(nome, [""])[0].replace(",", "."))
    except ValueError:
        return None


def pagina(q: dict) -> str:
    ora_ny = datetime.now(g.NY)
    sym = q.get("i", ["SPX"])[0]
    if sym not in g.INDICI:
        sym = "SPX"
    fut = g.INDICI[sym]["future"]
    files = scarichi(sym)
    corpo, avvisi = [], []
    if SU_RAILWAY and not (os.getenv("GEX_DATI") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")):
        avvisi.append("Nessun volume montato: a ogni deploy gli scarichi vanno persi.")
    stato = leggi_stato()
    if any(stato.get(f"{ora_ny:%Y-%m-%d}_{hhmm}_{s}", {}).get("esito") == "errore" for hhmm, _ in SLOT for s in g.INDICI):
        avvisi.append("L'ultimo scatto automatico e' fallito: vedi il dettaglio qui sotto.")

    if not files:
        avvisi.append("Nessuno scarico ancora: usa \"Scatta ora\" o aspetta il prossimo orario.")
        return html_pagina(barra(None, q, [], sym), avvisi, stato_html(ora_ny), "", sym)

    f = q.get("f", [files[0]])[0]
    if f not in files:
        f = files[0]
    try:
        scad = date.fromisoformat(q.get("scad", [""])[0])
    except ValueError:
        scad = None
    offset = float_o_none(q, "offset")
    alta = q.get("alta", [""])[0] == "1"
    payload = g.leggi_json(g.DATI / f)
    senza_offset = False
    try:
        try:
            an = g.analizza(payload, scad, offset, alta)
        except g.OffsetMancante as err:   # i dati ci sono: si mostrano in strike dell'indice
            an = g.analizza(payload, scad, 0.0, alta)
            an["nota_off"] = "mancante"
            senza_offset, fut = True, sym
            avvisi.append(f"{err} Finche' manca, i prezzi qui sotto sono strike {sym}, non del future.")
    except ValueError as err:
        avvisi.append(str(err))
        return html_pagina(barra(None, q, files, sym, f), avvisi, stato_html(ora_ny), "", sym)

    gambe_es = {n: v for n in st.ORDINE if (v := float_o_none(q, n.lower())) is not None}
    vivo = g.in_sessione(an["ts_ny"])
    nota_es = f"{fut} stimato ({sym} {an['spot']:.2f})" if vivo else f"ultima chiusura {sym} {an['spot']:.2f} + offset, non il prezzo attuale"
    nota_regime = f"sullo spot {sym}, ricontrolla sul future" if vivo else "sulla chiusura: confronta il flip con il future adesso"
    corpo.append(f"""<section class="pan dati">
<div><b>{an['spot'] + an['off']:.2f}</b><span>{e(nota_es)}</span></div>
<div><b>{e(an['regime'])}</b><span>{e(nota_regime)}</span></div>
<div><b>{an['off']:+g}</b><span>offset {fut}-{sym}, {e(an['nota_off'])}</span></div>
<div><b>{an['dist']} punti</b><span>distanza dei break</span></div>
<div><b>{an['scad']:%d/%m/%Y}</b><span>scadenza</span></div>
<div><b>{an['ts_ny'].astimezone(g.ROMA):%d/%m %H:%M}</b><span>dato CBOE a Roma, ritardo 15 min</span></div>
</section>""")
    liv = "".join(f'<tr><td class="n">{p:.2f}</td><td>{e(n)}</td></tr>' for p, n in an["livelli"])
    riga = "" if senza_offset else f"""<details><summary>Riga per Deepcharts</summary><textarea id="riga" readonly rows="4" aria-label="Riga per Deepcharts">{e(g.riga_deepcharts(an))}</textarea>
<button type="button" onclick="copia(this)">Copia per Deepcharts</button></details>"""
    corpo.append(f"""<div class="due">
<section class="pan"><h2>Profilo del gamma netto</h2><div class="grafico">{svg_profilo(an)}</div></section>
<section class="pan"><h2>Livelli proposti ({fut})</h2><table>{liv}</table>
<p class="nota">C e P sono picchi scelti in automatico: la scelta finale resta tua.</p>
{riga}</section></div>""")

    try:
        s = st.costruisci(an, gambe_es)
    except ValueError as err:
        avvisi.append(f"Struttura non calcolabile: {err}")
    else:
        righe = "".join(
            f'<tr><td>{x["nome"]}</td><td>{"compra" if x["q"] > 0 else "vende"} {x["tipo"]}</td>'
            f'<td class="n">{x["k"]:g}</td><td class="n">{x["k"] + an["off"]:.2f}</td>'
            f'<td class="n">{x["prezzo"]:.2f}</td><td class="n">{x["iv"]:.1%}</td></tr>' for x in s["gambe"])
        be = "".join(
            f'<tr><td class="n">{b + an["off"]:.2f}</td><td class="n">{b - v["k"]:+.1f}</td><td>da {v["nome"]}</td></tr>'
            for b in s["pareggi"] for v in [min(s["gambe"], key=lambda x: abs(x["k"] - b))])
        campi = "".join(
            f'<label>{n}<input type="number" step="0.25" name="{n.lower()}" value="{e(q.get(n.lower(), [""])[0])}"></label>'
            for n in st.ORDINE)
        nascosti = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in
                           {"i": sym, "f": f, "scad": an["scad"], "alta": "1" if alta else "",
                            "offset": q.get("offset", [""])[0]}.items())
        tipo = "credito" if s["credito"] >= 0 else "debito"
        nota_ndx = " (sul Nasdaq \"forse 70\", dal report del 19/10)" if sym == "NDX" else ""
        saltate = f'<p class="nota">{e("; ".join(s["saltate"]))}</p>' if s["saltate"] else ""
        corpo.append(f"""<section class="pan"><h2>Struttura montata: strangle + 2 vertical</h2>
<div class="grafico">{s['svg']}</div>
<div class="due" style="margin-top:12px"><div class="scorri"><table>
<tr><th>Gamba</th><th>Lato</th><th>Strike {sym}</th><th>{fut}</th><th>Prezzo</th><th>IV</th></tr>{righe}</table>
<p class="nota">{tipo} netto {abs(s['credito']):.2f} punti su 10 contratti per gamba. Lato di chi l'ha montata; i market maker hanno l'opposto. Quantita' e lato sono l'ipotesi di Serafini, non un dato.</p>{saltate}</div>
<div class="scorri"><table><tr><th>Break-even {fut}</th><th>Distanza</th><th></th></tr>{be}</table>
<p class="nota">Serafini usa {an['ind']['distanze'][0]} punti in bassa volatilita', {an['ind']['distanze'][1]} in alta{nota_ndx}.</p></div></div>
<details><summary>Correggi le gambe (prezzi {fut})</summary><form method="get" class="filtri" style="margin-top:8px">{nascosti}{campi}<button type="submit">Ricalcola</button></form></details></section>""")

    return html_pagina(barra(an, q, files, sym, f), avvisi, stato_html(ora_ny), "".join(corpo), sym)


def barra(an: dict | None, q: dict, files: list[str], sym: str, f: str = "") -> str:
    opz = "".join(f'<option value="{e(n)}"{" selected" if n == f else ""}>{e(n[:10])} {e(n[11:13])}:{e(n[13:15])}</option>'
                  for n in files[:60])
    scad = ""
    if an:
        future = [d for d in an["scadenze"] if d >= an["ts_ny"].date()][:15]
        s_opz = "".join(f'<option value="{d}"{" selected" if d == an["scad"] else ""}>{GIORNI[d.weekday()]} {d:%d/%m/%Y}</option>' for d in future)
        scad = f'<label>Scadenza<select name="scad">{s_opz}</select></label>'
    alta = " checked" if q.get("alta", [""])[0] == "1" else ""
    punti = g.INDICI[sym]["distanze"][1]
    return f"""<div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:space-between;align-items:end">
<form method="get" class="filtri"><input type="hidden" name="i" value="{sym}"><label>Scarico (ora di Roma)<select name="f">{opz}</select></label>{scad}
<label>Offset a mano<input type="number" step="0.25" name="offset" value="{e(q.get('offset', [''])[0])}"></label>
<label class="riga"><input type="checkbox" name="alta" value="1"{alta}>Alta volatilita' ({punti} punti)</label>
<button class="primo" type="submit">Aggiorna</button></form>
<div style="display:flex;gap:10px;align-items:center"><a href="/storico">Storico</a>
<form method="post" action="/scatta?i={sym}"><button type="submit">Scatta ora</button></form></div></div>"""


def pagina_storico() -> str:
    def cella(v):
        return f'<td class="n">{v:.2f}</td>' if isinstance(v, float) else f"<td>{e('' if v is None else v)}</td>"
    righe = "".join("<tr>" + "".join(cella(v) for v in r) + "</tr>" for r in storico.ultimi())
    testa = "".join(f"<th>{t}</th>" for t in ["Indice", "Scatto (Roma)", "Scadenza", "Tipo", "Regime", "Offset", "Flip", "Gamma max",
                                              "C1", "C3", "P1", "P3"])
    corpo = f"""<section class="pan"><h2>Ultimi scatti (prezzi ES e NQ)</h2><div class="scorri"><table>{testa}{righe}</table></div>
<p class="nota"><a href="/storico.csv">Scarica tutti i livelli in CSV</a> &middot; <a href="/gex.db">Scarica il database completo</a></p></section>"""
    return html_pagina('<a href="/">Torna ai livelli</a>', [], "", corpo)


def html_pagina(filtri: str, avvisi: list[str], stato: str, corpo: str, sym: str = "") -> str:
    box = "".join(f'<section class="pan avviso">{e(a)}</section>' for a in avvisi)
    schede = " &middot; ".join(f"<b>{s}</b>" if s == sym else f'<a href="/?i={s}">{s}</a>' for s in g.INDICI)
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>GEX 0DTE</title><style>{CSS}</style>
<script>function copia(b){{const r=document.getElementById("riga");r.select();
const fine=()=>{{b.textContent="Copiato";setTimeout(()=>b.textContent="Copia per Deepcharts",1500)}};
if(navigator.clipboard)navigator.clipboard.writeText(r.value).then(fine,()=>{{document.execCommand("copy");fine()}});else{{document.execCommand("copy");fine()}}}}</script></head>
<body><main><h1>GEX 0DTE: {schede}</h1>{box}<section class="pan">{filtri}{stato}</section>{corpo}</main></body></html>"""


# --- server ---------------------------------------------------------------------------

class Gestore(BaseHTTPRequestHandler):
    server_version = "gex"
    sys_version = ""
    ultimo_manuale: dict = {}

    def rispondi(self, codice: int, testo: str | bytes, tipo: str = "text/html; charset=utf-8", extra: dict | None = None) -> None:
        dati = testo if isinstance(testo, bytes) else testo.encode()
        self.send_response(codice)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dati)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(dati)

    def autorizzato(self) -> bool:
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
            except ValueError:
                pw = ""
            if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                return True
        self.rispondi(401, "Serve la password", "text/plain; charset=utf-8", {"WWW-Authenticate": 'Basic realm="GEX"'})
        return False

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/health":
            return self.rispondi(200, "ok", "text/plain")
        if not self.autorizzato():
            return
        try:
            if url.path == "/":
                self.rispondi(200, pagina(parse_qs(url.query, keep_blank_values=True)))
            elif url.path == "/storico":
                self.rispondi(200, pagina_storico())
            elif url.path == "/storico.csv":
                self.rispondi(200, storico.csv_livelli(), "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="gex_livelli.csv"'})
            elif url.path == "/gex.db":
                self.rispondi(200, storico.copia_db(), "application/vnd.sqlite3",
                              {"Content-Disposition": 'attachment; filename="gex.db"'})
            else:
                self.rispondi(404, "Pagina non trovata", "text/plain; charset=utf-8")
        except Exception:
            traceback.print_exc()
            self.rispondi(500, "Errore interno: vedi i log", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if not self.autorizzato():
            return
        url = urlsplit(self.path)
        if url.path != "/scatta":
            return self.rispondi(404, "Pagina non trovata", "text/plain; charset=utf-8")
        sym = parse_qs(url.query).get("i", ["SPX"])[0]
        if sym not in g.INDICI:
            return self.rispondi(404, "Indice sconosciuto", "text/plain; charset=utf-8")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return self.rispondi(403, "Richiesta non ammessa", "text/plain; charset=utf-8")
        if tempo.time() - Gestore.ultimo_manuale.get(sym, 0) < 60:
            return self.rispondi(303, "", extra={"Location": "/?" + urlencode({"i": sym})})
        Gestore.ultimo_manuale[sym] = tempo.time()
        try:
            nome = scatta(sym)
            print(f"scatto manuale: {nome}", flush=True)
            self.rispondi(303, "", extra={"Location": "/?" + urlencode({"i": sym, "f": nome})})
        except Exception as err:
            traceback.print_exc()
            self.rispondi(502, f"Scarico CBOE non riuscito: {err}", "text/plain; charset=utf-8")

    def log_message(self, formato, *args) -> None:
        if "/health" not in str(args[0] if args else ""):
            super().log_message(formato, *args)


def main() -> None:
    if SU_RAILWAY and not PASSWORD:
        sys.exit("GEX_PASSWORD non impostata: su Railway il sito non parte senza password.")
    host = "0.0.0.0" if PASSWORD else "127.0.0.1"
    porta = int(os.getenv("PORT", "8000"))
    fatti, falliti = storico.ricostruisci()   # recupera gli scarichi non ancora nello storico
    print(f"storico: {fatti} scarichi recuperati, {len(falliti)} non riusciti", flush=True)
    if os.getenv("GEX_SCHEDULER", "1") != "0":
        threading.Thread(target=scheduler, daemon=True).start()
    print(f"GEX su {host}:{porta}, dati in {g.DATI}", flush=True)
    ThreadingHTTPServer((host, porta), Gestore).serve_forever()


if __name__ == "__main__":
    main()
