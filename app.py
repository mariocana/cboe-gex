#!/usr/bin/env python3
"""Sito GEX: scatta da solo la catena SPX agli orari del piano e mostra livelli e struttura.
In sessione la riscarica ogni 5 minuti per la pagina /live (come si spostano flip e gamma massimo).

Un solo processo: il server web e, in un thread, lo scheduler. Stanno insieme perche'
leggono e scrivono gli stessi file: su Railway un cron separato non vedrebbe il volume.

    python3 app.py                       # in locale su http://127.0.0.1:8000
    GEX_PASSWORD=... python3 app.py      # con password, ascolta su tutte le interfacce

Variabili: GEX_PASSWORD (su Railway serve o questa o GEX_PUBBLICO=1), GEX_PUBBLICO=1 (sito
aperto a tutti ma di sola lettura: senza password, niente "Scatta ora"), PORT, GEX_DATI (cartella dei dati; su
Railway basta montare un volume), GEX_SCHEDULER=0 per spegnere gli scatti automatici,
GEX_INDICI (indici degli scatti agli orari, default SPX; es. "SPX,NDX"), GEX_LIVE=0 per spegnere
il live ogni 5 minuti.
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
import urllib.parse
import urllib.request
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
AUTOMATICI = [s for s in os.getenv("GEX_INDICI", "SPX").split(",") if s in g.INDICI] or ["SPX"]
LIVE = [] if os.getenv("GEX_LIVE", "1") == "0" else ["SPX"]
LIVE_OGNI = 300                      # secondi fra due scatti live
LIVE_REFRESH = 60                    # secondi fra due ricaricamenti della pagina /live
STABILE_DA = 10 * 60                 # prima delle 10:00 NY il volume e' minimo e il flip salta
LIVE_ORARIO = (9 * 60 + 45, 16 * 60 + 15)   # minuti a New York: il dato CBOE e' indietro di 15
FINESTRA = timedelta(minutes=30)     # se uno scatto fallisce, riprova per mezz'ora
PAUSA_RIPROVA = 180                  # secondi fra due tentativi
NOME_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}_(SPX|NDX)\.json\.gz$")
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
PASSWORD = os.getenv("GEX_PASSWORD", "")
SU_RAILWAY = bool(os.getenv("RAILWAY_SERVICE_ID"))
PUBBLICO = os.getenv("GEX_PUBBLICO") == "1"
SOLA_LETTURA = PUBBLICO and not PASSWORD   # aperto a tutti: nessuno puo' far partire scarichi

blocco = threading.Lock()


# --- controllo dei feed -----------------------------------------------------------------
# Ogni scarico segna se il CBOE ha risposto. Dopo SOGLIA_ERRORI errori di fila (circa 5-10 minuti
# in sessione) c'e' un problema: banner rosso su tutte le pagine, /stato risponde 503 (per un
# monitor esterno come UptimeRobot) e, se GEX_TELEGRAM_TOKEN e' impostata, arriva un messaggio dal bot Telegram.
# /health resta sempre 200: lo usa Railway per riavviare il servizio, e riavviare non sistema un feed bloccato.

SOGLIA_ERRORI = 3
STALE_MIN = 20                       # in sessione, minuti senza nessun dato nuovo
RIAVVISO = 3600                      # secondi fra due notifiche dello stesso problema
TG_TOKEN = os.getenv("GEX_TELEGRAM_TOKEN", "")   # il token del bot, da @BotFather
TG_CHAT = os.getenv("GEX_TELEGRAM_CHAT", "")     # facoltativo: se manca si usa la prima chat che ha scritto al bot
blocco_feed = threading.Lock()


def leggi_feed() -> dict:
    p = g.DATI / "feed.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def scrivi_feed(f: dict) -> None:
    g.DATI.mkdir(parents=True, exist_ok=True)
    tmp = g.DATI / "feed.tmp"
    tmp.write_text(json.dumps(f, indent=1))
    tmp.replace(g.DATI / "feed.json")


def _tg(metodo: str, dati: dict) -> dict:
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{metodo}",
                                 data=urllib.parse.urlencode(dati).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def chat_telegram() -> str | None:
    """La chat a cui scrivere: quella impostata, oppure la prima che ha scritto al bot (poi resta quella)."""
    if TG_CHAT:
        return TG_CHAT
    f = leggi_feed()
    if f.get("telegram_chat"):
        return str(f["telegram_chat"])
    for u in _tg("getUpdates", {"limit": 50}).get("result", []):
        chat = (u.get("message") or {}).get("chat") or {}
        if chat.get("id"):
            with blocco_feed:
                f = leggi_feed()
                f["telegram_chat"] = chat["id"]
                scrivi_feed(f)
            return str(chat["id"])
    return None


def avvisa(testo: str) -> bool:
    """Messaggio dal bot Telegram, solo se GEX_TELEGRAM_TOKEN e' impostata. Il testo non ha dati personali."""
    if not TG_TOKEN:
        return False
    try:
        chat = chat_telegram()
        if not chat:
            print("telegram: nessuna chat. Scrivi un messaggio qualsiasi al bot, poi aspetta il prossimo avviso.", flush=True)
            return False
        _tg("sendMessage", {"chat_id": chat, "text": f"GEX SPX: {testo}"})
        return True
    except Exception as err:
        print(f"messaggio telegram non inviato: {str(err).replace(TG_TOKEN, '***')}", flush=True)
        return False


def segna_feed(quale: str, ok: bool, msg: str = "") -> None:
    ora = tempo.time()
    testo = None
    with blocco_feed:
        f = leggi_feed()
        v = f.get(quale, {})
        if ok:
            if v.get("fails", 0) >= SOGLIA_ERRORI:
                testo = f"Feed CBOE ({quale}) di nuovo attivo."
            v.update(ok_t=ora, fails=0, primo_errore=None, avvisato_t=None)
        else:
            v["fails"] = v.get("fails", 0) + 1
            v["primo_errore"] = v.get("primo_errore") or ora
            v["err_t"], v["errore"] = ora, msg[:200]
            if v["fails"] >= SOGLIA_ERRORI and ora - (v.get("avvisato_t") or 0) > RIAVVISO:
                v["avvisato_t"] = ora
                da = datetime.fromtimestamp(v["primo_errore"], g.ROMA)
                testo = f"Feed CBOE ({quale}) in errore dalle {da:%H:%M} a Roma: {v['errore']}"
        f[quale] = v
        scrivi_feed(f)
    if testo and not avvisa(testo) and not ok and TG_TOKEN:
        with blocco_feed:   # non inviato: al prossimo errore riprova, senza aspettare un'ora
            f = leggi_feed()
            f.setdefault(quale, {})["avvisato_t"] = None
            scrivi_feed(f)


def segna_nuovo_dato() -> None:
    with blocco_feed:
        f = leggi_feed()
        f.setdefault("live", {})["nuovo_t"] = tempo.time()
        scrivi_feed(f)


def problema_feed(adesso: datetime | None = None) -> str | None:
    """Il testo del problema attuale, o None se i feed vanno."""
    adesso = adesso or datetime.now(g.ROMA)
    f = leggi_feed()
    testi = []
    for quale in ("opzioni", "barre"):
        v = f.get(quale, {})
        if v.get("fails", 0) >= SOGLIA_ERRORI:
            da = datetime.fromtimestamp(v["primo_errore"], g.ROMA)
            buono = f" Ultimo dato buono alle {datetime.fromtimestamp(v['ok_t'], g.ROMA):%H:%M} del {datetime.fromtimestamp(v['ok_t'], g.ROMA):%d/%m}." \
                if v.get("ok_t") else ""
            testi.append(f"Feed CBOE ({quale}) in errore dalle {da:%H:%M} del {da:%d/%m} (Roma), {v['fails']} tentativi di fila: "
                         f"{v.get('errore', '')}.{buono}")
    ny = adesso.astimezone(g.NY)
    m = ny.hour * 60 + ny.minute
    nuovo = f.get("live", {}).get("nuovo_t")
    if (LIVE and not testi and nuovo and ny.weekday() < 5 and LIVE_ORARIO[0] + STALE_MIN <= m < LIVE_ORARIO[1]
            and tempo.time() - nuovo > STALE_MIN * 60):
        testi.append(f"Nessun dato live nuovo da {round((tempo.time() - nuovo) / 60)} minuti, anche se i feed rispondono: "
                     "il CBOE potrebbe non aggiornare, o lo scheduler essersi fermato.")
    return " ".join(testi) or None


g.ESITO = segna_feed


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
        an = None
        try:
            an = g.analizza(payload, salva=True)
        except (g.OffsetMancante, ValueError) as e:
            print(f"offset non aggiornato: {e}", flush=True)
        if an and sym in LIVE and g.in_sessione(an["ts_ny"]):
            try:
                storico.registra_live(an, payload)
            except Exception as e:
                print(f"live non aggiornato: {e}", flush=True)
        try:
            storico.registra(p.name, payload)
        except Exception as e:   # il JSON e' salvato: all'avvio successivo viene recuperato
            print(f"storico non aggiornato per {p.name}: {e}", flush=True)
        return p.name


def scatto_live(sym: str) -> str:
    """Scarica la catena e registra solo i numeri della 0DTE: il JSON grezzo non si salva."""
    with blocco:
        payload = g.scarica(sym)
        an = g.analizza(payload, salva=True)
        if not g.in_sessione(an["ts_ny"]):
            return f"dato CBOE fuori sessione ({an['ts_ny']:%Y-%m-%d %H:%M} NY)"
        nuovo = storico.registra_live(an, payload)
        if nuovo:
            segna_nuovo_dato()
        return f"{an['ts_ny']:%H:%M} NY {'registrato' if nuovo else 'gia presente, CBOE non aggiornata'}"


ultimo_live = {"t": 0.0}


def controlla_live(ora_ny: datetime) -> None:
    m = ora_ny.hour * 60 + ora_ny.minute
    if not LIVE or ora_ny.weekday() >= 5 or not LIVE_ORARIO[0] <= m < LIVE_ORARIO[1]:
        return
    if tempo.time() - ultimo_live["t"] < LIVE_OGNI - 20:
        return
    ultimo_live["t"] = tempo.time()
    for sym in LIVE:
        try:
            print(f"live {sym}: {scatto_live(sym)}", flush=True)
        except Exception as e:
            ultimo_live["t"] = tempo.time() - LIVE_OGNI + 60   # riprova fra un minuto
            print(f"live {sym}: ERRORE {e}", flush=True)


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
        for sym in AUTOMATICI:
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
        for controllo in (controlla_slot, controlla_live):
            try:
                controllo(datetime.now(g.NY))
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
tr.pre td{color:#6b737d}.scorri{overflow-x:auto}.avviso{border-color:var(--ko)}.nota{color:var(--tenue);font-size:12px;margin:8px 0 0}.ok{color:var(--ok)}.ko{color:var(--ko)}details summary{cursor:pointer;color:var(--tenue);font-size:13px;margin-top:10px}
a{color:#7fbdf0}textarea{width:100%;background:#111317;color:var(--testo);border:1px solid var(--bordo);border-radius:6px;padding:6px;font:12px/1.4 Menlo,monospace;margin:8px 0}svg{display:block}.grafico{overflow-x:auto}.grafico svg{min-width:480px}
"""


def e(x) -> str:
    return html.escape(str(x), quote=True)


def stato_html(ora_ny: datetime) -> str:
    stato = leggi_stato()
    righe = []
    for hhmm, nome in SLOT:
        inizio = ora_ny.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0, microsecond=0)
        for sym in AUTOMATICI:
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
    if any(stato.get(f"{ora_ny:%Y-%m-%d}_{hhmm}_{s}", {}).get("esito") == "errore" for hhmm, _ in SLOT for s in AUTOMATICI):
        avvisi.append("L'ultimo scatto automatico e' fallito: vedi il dettaglio qui sotto.")

    if not files:
        avvisi.append("Nessuno scarico ancora: aspetta il prossimo orario." if SOLA_LETTURA
                      else "Nessuno scarico ancora: usa \"Scatta ora\" o aspetta il prossimo orario.")
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
<div style="display:flex;gap:10px;align-items:center"><a href="/live">Live</a><a href="/storico">Storico</a>
{"" if SOLA_LETTURA else f'<form method="post" action="/scatta?i={sym}"><button type="submit">Scatta ora</button></form>'}</div></div>"""


def pagina_storico() -> str:
    def cella(v):
        return f'<td class="n">{v:.2f}</td>' if isinstance(v, float) else f"<td>{e('' if v is None else v)}</td>"
    righe = "".join("<tr>" + "".join(cella(v) for v in r) + "</tr>" for r in storico.ultimi())
    testa = "".join(f"<th>{t}</th>" for t in ["Indice", "Scatto (Roma)", "Scadenza", "Tipo", "Regime", "Offset", "Flip", "Gamma max",
                                              "C1", "C3", "P1", "P3"])
    corpo = f"""<section class="pan"><h2>Ultimi scatti (prezzi ES e NQ)</h2><div class="scorri"><table>{testa}{righe}</table></div>
<p class="nota"><a href="/storico.csv">Scarica tutti i livelli in CSV</a> &middot; <a href="/gex.db">Scarica il database completo</a></p></section>"""
    return html_pagina('<a href="/">Torna ai livelli</a>', [], "", corpo)


def html_pagina(filtri: str, avvisi: list[str], stato: str, corpo: str, sym: str = "",
                titolo: str = "GEX 0DTE", testa: str = "") -> str:
    problema = problema_feed()
    box = (f'<section class="pan avviso"><b>Problema con i dati CBOE.</b> {e(problema)}</section>' if problema else "")
    box += "".join(f'<section class="pan avviso">{e(a)}</section>' for a in avvisi)
    visibili = [s for s in g.INDICI if s in AUTOMATICI or s == sym or scarichi(s)]
    schede = " &middot; ".join(f"<b>{s}</b>" if s == sym else f'<a href="/?i={s}">{s}</a>' for s in visibili)
    return f"""<!doctype html><html lang="it"><head><meta charset="utf-8">{testa}
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(titolo)}</title><style>{CSS}</style>
<script>function copia(b){{const r=document.getElementById("riga");r.select();
const fine=()=>{{b.textContent="Copiato";setTimeout(()=>b.textContent="Copia per Deepcharts",1500)}};
if(navigator.clipboard)navigator.clipboard.writeText(r.value).then(fine,()=>{{document.execCommand("copy");fine()}});else{{document.execCommand("copy");fine()}}}}</script></head>
<body><main><h1>{e(titolo)}: {schede}</h1>{box}<section class="pan">{filtri}{stato}</section>{corpo}</main></body></html>"""


# --- live ------------------------------------------------------------------------------

SERIE = [("spot", "prezzo", "#5aa9e6"), ("flip", "flip", "#f0c040"),
         ("gamma_max", "gamma max", "#4caf50"), ("put_max", "put piu' forte", "#d76ad0")]


def ora_ny_di(r: dict) -> datetime:
    return datetime.fromisoformat(r["ts_utc"]).replace(tzinfo=timezone.utc).astimezone(g.NY)


def instabile(r: dict) -> bool:
    t = ora_ny_di(r)
    return t.hour * 60 + t.minute < STABILE_DA


def fut(r: dict, campo: str) -> float | None:
    """Valore in prezzi ES: strike SPX + offset dello stesso scatto."""
    v = r.get(campo)
    return None if v is None else v + (r["offset"] or 0)


def svg_timeline(righe: list[dict]) -> str:
    """Prezzo, flip, gamma massimo e put piu' forte durante la sessione; fascia = movimento atteso."""
    W, H, sx, dx, su, giu = 900, 380, 58, 150, 12, 30
    giorno = ora_ny_di(righe[0])
    t0 = giorno.replace(hour=9, minute=30, second=0, microsecond=0)
    t1 = giorno.replace(hour=16, minute=0, second=0, microsecond=0)
    X = lambda t: sx + max(0.0, min(1.0, (t - t0) / (t1 - t0))) * (W - sx - dx)
    rif = next((r for r in righe if not instabile(r)), righe[0])
    fascia = None
    if rif.get("straddle"):
        fascia = (fut(rif, "spot") - rif["straddle"], fut(rif, "spot") + rif["straddle"])
    tutti = [v for r in righe for c, _, _ in SERIE if (v := fut(r, c)) is not None] + list(fascia or [])
    lo, hi = min(tutti), max(tutti)
    pad = max((hi - lo) * 0.05, 2.0)
    lo, hi = lo - pad, hi + pad
    Y = lambda v: su + (hi - v) / (hi - lo) * (H - su - giu)
    el = []
    passo = next(p for p in (1, 2, 5, 10, 25, 50, 100, 250) if (hi - lo) / p <= 8)
    v = (int(lo // passo) + 1) * passo
    while v < hi:
        el.append(f'<line x1="{sx}" x2="{W - dx}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#2a2e35"/>'
                  f'<text x="{sx - 6}" y="{Y(v) + 4:.1f}" fill="#9aa3ad" font-size="11" text-anchor="end">{v:g}</text>')
        v += passo
    for h in range(10, 17):
        t = giorno.replace(hour=h, minute=0, second=0, microsecond=0)
        el.append(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{su}" y2="{H - giu}" stroke="#2a2e35"/>'
                  f'<text x="{X(t):.1f}" y="{H - 12}" fill="#9aa3ad" font-size="11" text-anchor="middle">{t.astimezone(g.ROMA):%H:%M}</text>')
    t_stabile = giorno.replace(hour=STABILE_DA // 60, minute=STABILE_DA % 60, second=0, microsecond=0)
    el.append(f'<rect x="{sx}" width="{X(t_stabile) - sx:.1f}" y="{su}" height="{H - su - giu}" fill="#9aa3ad" fill-opacity="0.16"/>'
              f'<text x="{(sx + X(t_stabile)) / 2:.1f}" y="{su + 12}" fill="#9aa3ad" font-size="10" text-anchor="middle">dati instabili</text>')
    if fascia:
        el.append(f'<rect x="{sx}" width="{W - dx - sx}" y="{Y(fascia[1]):.1f}" height="{Y(fascia[0]) - Y(fascia[1]):.1f}" '
                  f'fill="#ffffff" fill-opacity="0.05" stroke="#9aa3ad" stroke-dasharray="3 4"/>')
    etichette = []
    for campo, nome, col in SERIE:
        punti = [(X(ora_ny_di(r)), Y(v)) for r in righe if (v := fut(r, campo)) is not None]
        if not punti:
            continue
        if campo != "spot":   # un livello resta valido fino allo scatto dopo: linea a gradini
            gradini = []
            for (xa, ya), (xb, _) in zip(punti, punti[1:] + [(punti[-1][0], None)]):
                gradini += [(xa, ya), (xb, ya)]
            punti = gradini
        spessore = 2.2 if campo == "spot" else 1.6
        el.append(f'<polyline fill="none" stroke="{col}" stroke-width="{spessore}" points="'
                  + " ".join(f"{x:.1f},{y:.1f}" for x, y in punti) + '"/>')
        if len(punti) == 1:
            el.append(f'<circle cx="{punti[0][0]:.1f}" cy="{punti[0][1]:.1f}" r="3" fill="{col}"/>')
        etichette.append([punti[-1][1], f"{nome} {fut(righe[-1], campo) or 0:.2f}", col])
    etichette.sort()
    for i in range(1, len(etichette)):   # niente etichette sovrapposte
        etichette[i][0] = max(etichette[i][0], etichette[i - 1][0] + 14)
    x_et = X(ora_ny_di(righe[-1])) + 8
    for y, testo, col in etichette:
        el.append(f'<text x="{x_et:.1f}" y="{y + 4:.1f}" fill="{col}" font-size="12">{e(testo)}</text>')
    if fascia:
        el.append(f'<text x="{sx + 4}" y="{Y(fascia[1]) - 4:.1f}" fill="#9aa3ad" font-size="11">'
                  f'movimento atteso dal primo scatto: ±{rif["straddle"]:.1f}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" style="width:100%;height:auto" '
            f'font-family="Helvetica, Arial" role="img" aria-label="Prezzo, flip e gamma massimo durante la sessione">{"".join(el)}</svg>')


def svg_confronto(ora: dict, prima: dict) -> str:
    """Profilo 0DTE adesso (barre piene) e di prima (contorno), in prezzi ES."""
    off_o, off_p = ora["offset"] or 0, prima["offset"] or 0
    adesso = {k: n for k, n in ora["prof"]}
    prec = {k: n for k, n in prima["prof"]}
    strikes = sorted(set(adesso) | set(prec), reverse=True)
    if not strikes:
        return ""
    riga, sx, W = 13, 66, 660
    centro, meta = sx + 220, 200
    top = max(abs(v) for v in list(adesso.values()) + list(prec.values())) or 1
    H = riga * len(strikes) + 10
    el = [f'<line x1="{centro}" x2="{centro}" y1="0" y2="{H}" stroke="#555"/>']
    for i, k in enumerate(strikes):
        y = 5 + i * riga
        for n, piena in ((adesso.get(k), True), (prec.get(k), False)):
            if n is None:
                continue
            lung = abs(n) / top * meta
            x = centro if n >= 0 else centro - lung
            col = "#4caf50" if n >= 0 else "#d76ad0"
            stile = f'fill="{col}" fill-opacity="0.85"' if piena else 'fill="none" stroke="#e4e7eb" stroke-opacity="0.75"'
            el.append(f'<rect x="{x:.1f}" y="{y + 2}" width="{lung:.1f}" height="{riga - 4}" {stile}/>')
        el.append(f'<text x="{sx - 8}" y="{y + 10}" fill="#9aa3ad" font-size="10" text-anchor="end">{k + off_o:.2f}</text>')
        for campo, nome in (("flip", "FLIP"), ("gamma_max", "GAMMA MAX")):
            if ora.get(campo) == k:
                el.append(f'<text x="{centro + meta + 6}" y="{y + 10}" fill="#f0c040" font-size="10">{nome}</text>')
            if prima.get(campo) == k and ora.get(campo) != k:
                el.append(f'<text x="{centro + meta + 6}" y="{y + 10}" fill="#9aa3ad" font-size="10">{nome.lower()} prima</text>')
    k_alto, k_basso = strikes[0], strikes[-1]
    spot = ora["spot"]
    if k_basso <= spot <= k_alto and k_alto > k_basso:
        y = 5 + (k_alto - spot) / (k_alto - k_basso) * riga * (len(strikes) - 1) + riga / 2
        el.append(f'<line x1="{sx}" x2="{W}" y1="{y:.1f}" y2="{y:.1f}" stroke="#5aa9e6" stroke-dasharray="5 4"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" style="width:100%;height:auto" '
            f'font-family="Helvetica, Arial" role="img" aria-label="Profilo gamma adesso e di prima">{"".join(el)}</svg>')


def num_o_vuoto(v: float | None, decimali: int = 2) -> str:
    return "" if v is None else f"{v:.{decimali}f}"


def scatto_di_prima(righe: list[dict], minuti: int = 30) -> dict:
    limite = ora_ny_di(righe[-1]) - timedelta(minutes=minuti)
    prima = [r for r in righe if ora_ny_di(r) <= limite]
    return prima[-1] if prima else righe[0]


def novita(righe: list[dict]) -> list[str]:
    """Cosa e' cambiato: lato del flip rispetto allo scatto prima, flip e gamma max in 30 minuti."""
    righe = [r for r in righe if not instabile(r)]
    if not righe:
        return []
    ult = righe[-1]
    out = []
    if len(righe) >= 2 and righe[-2]["regime"] != ult["regime"]:
        out.append(f"Alle {ora_ny_di(ult).astimezone(g.ROMA):%H:%M} il prezzo e' passato in {ult['regime']} "
                   f"(prima {righe[-2]['regime']}).")
    prima = scatto_di_prima(righe)
    if prima is not ult:
        mins = round((ora_ny_di(ult) - ora_ny_di(prima)).total_seconds() / 60)
        for campo, nome in (("flip", "Flip"), ("gamma_max", "Gamma massimo")):
            a, b = fut(prima, campo), fut(ult, campo)
            if a is not None and b is not None and prima[campo] != ult[campo]:
                out.append(f"{nome} spostato da {a:.2f} a {b:.2f} in {mins} minuti.")
    rif = righe[0]
    if rif.get("straddle") and abs(ult["spot"] - rif["spot"]) > rif["straddle"]:
        out.append(f"Prezzo oltre il movimento atteso del primo scatto (±{rif['straddle']:.1f}): "
                   "giornata piu' ampia di quanto prezzavano le opzioni.")
    return out


def pagina_live(q: dict) -> str:
    ora_ny = datetime.now(g.NY)
    giorni = storico.live_giorni()
    m = ora_ny.hour * 60 + ora_ny.minute
    attivo = bool(LIVE) and ora_ny.weekday() < 5 and LIVE_ORARIO[0] <= m < LIVE_ORARIO[1]
    inizio = ora_ny.replace(hour=LIVE_ORARIO[0] // 60, minute=LIVE_ORARIO[0] % 60).astimezone(g.ROMA)
    fine = ora_ny.replace(hour=LIVE_ORARIO[1] // 60, minute=LIVE_ORARIO[1] % 60).astimezone(g.ROMA)
    spiega = (f"SPX 0DTE ogni 5 minuti dalle {inizio:%H:%M} alle {fine:%H:%M} a Roma, dati CBOE ritardati di 15 minuti. "
              "Serve a vedere come si spostano i livelli: quelli da mettere su Deepcharts restano gli scatti del mattino e delle 15:47.")
    if not LIVE:
        spiega = "Live spento (GEX_LIVE=0)."
    giorno = q.get("d", [giorni[0] if giorni else ""])[0]
    if giorno not in giorni:
        giorno = giorni[0] if giorni else ""
    opz = "".join(f'<option value="{d}"{" selected" if d == giorno else ""}>{GIORNI[date.fromisoformat(d).weekday()]} '
                  f'{date.fromisoformat(d):%d/%m/%Y}</option>' for d in giorni[:60])
    filtri = (f'<div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:space-between;align-items:end">'
              f'<form method="get" class="filtri"><label>Giorno<select name="d" onchange="this.form.submit()">{opz}</select></label>'
              f'<noscript><button type="submit">Mostra</button></noscript></form>'
              f'<div style="display:flex;gap:10px;align-items:center"><a href="/">Livelli</a><a href="/storico">Storico</a>'
              f'<a href="/replay">Replay</a><a href="/live.csv">CSV</a></div></div><p class="nota">{e(spiega)}</p>')
    oggi = giorno == f"{ora_ny:%Y-%m-%d}"
    auto = attivo and (oggi or not giorni)
    testa = ('<script>addEventListener("DOMContentLoaded",()=>{const c=document.getElementById("cd"),'
             'p=document.getElementById("pausa");if(!c)return;let n=%d;setInterval(()=>{if(p&&p.checked)return;'
             'n--;c.textContent=n;if(n<=0)location.reload()},1000)})</script>' % LIVE_REFRESH) if auto else ""
    adesso = datetime.now(g.ROMA)
    stato_ar = (f' &middot; aggiornamento automatico ogni {LIVE_REFRESH} s, prossimo tra <b id="cd">{LIVE_REFRESH}</b> s '
                '<label class="riga"><input type="checkbox" id="pausa"> pausa</label>' if auto
                else " &middot; aggiornamento automatico fermo (fuori orario o giorno passato)")
    filtri += f'<p class="nota" id="ar">Pagina aggiornata alle {adesso:%H:%M:%S} (Roma){stato_ar}</p>'
    righe = storico.live_del_giorno(giorno) if giorno else []
    if not righe:
        avviso = "Nessuno scatto live ancora: il primo arriva all'apertura di Wall Street." if LIVE else ""
        return html_pagina(filtri, [avviso] if avviso else [], "", "", "SPX", "GEX live", testa)

    ult = righe[-1]
    prima = scatto_di_prima([r for r in righe if not instabile(r)] or righe)
    t_ult = ora_ny_di(ult).astimezone(g.ROMA)
    em = f"±{ult['straddle']:.1f}" if ult.get("straddle") else "n.d."
    flip = fut(ult, "flip")
    corpo = [f"""<section class="pan dati">
<div><b>{fut(ult, 'spot'):.2f}</b><span>ES stimato (SPX {ult['spot']:.2f} + offset {ult['offset'] or 0:+g})</span></div>
<div><b>{e(ult['regime'])}</b><span>rispetto al flip</span></div>
<div><b>{'n.d.' if flip is None else f'{flip:.2f}'}</b><span>gamma flip</span></div>
<div><b>{fut(ult, 'gamma_max'):.2f}</b><span>gamma massimo</span></div>
<div><b>{em}</b><span>movimento atteso residuo (straddle ATM 0DTE)</span></div>
<div><b>{t_ult:%H:%M}</b><span>ultimo dato CBOE a Roma, {len(righe)} scatti</span></div>
</section>"""]
    cambi = novita(righe)
    if cambi:
        corpo.append('<section class="pan"><h2>Cosa e\' cambiato</h2><ul style="margin:0;padding-left:18px">'
                     + "".join(f"<li>{e(c)}</li>" for c in cambi) + "</ul></section>")
    corpo.append(f'<section class="pan"><h2>La sessione (ora di Roma, prezzi ES)</h2><div class="grafico">{svg_timeline(righe)}</div>'
                 '<p class="nota">Blu il prezzo, giallo il flip, verde il gamma massimo, viola la put piu\' forte. '
                 'I livelli sono a gradini: valgono fino allo scatto dopo.</p></section>')
    t_prima = ora_ny_di(prima).astimezone(g.ROMA)
    tab = "".join(
        f'<tr{" class=pre" if instabile(r) else ""}><td>{ora_ny_di(r).astimezone(g.ROMA):%H:%M}</td><td class="n">{fut(r, "spot"):.2f}</td><td>{e(r["regime"])}</td>'
        + "".join(f'<td class="n">{num_o_vuoto(fut(r, c))}</td>' for c in ("flip", "gamma_max", "put_max", "c1", "p1"))
        + f'<td class="n">{num_o_vuoto(r["straddle"], 1)}</td></tr>'
        for r in reversed(righe))
    corpo.append(f"""<div class="due">
<section class="pan"><h2>Profilo adesso ({t_ult:%H:%M}) e alle {t_prima:%H:%M}</h2><div class="grafico">{svg_confronto(ult, prima)}</div>
<p class="nota">Barre piene: adesso. Contorno bianco: {round((ora_ny_di(ult) - ora_ny_di(prima)).total_seconds() / 60)} minuti prima.</p></section>
<section class="pan"><h2>Scatti del giorno</h2><p class="nota" style="margin:0 0 8px">In grigio gli scatti prima delle 10:00 a New York (16:00 a Roma): volume minimo, il flip salta. Non vanno letti come segnali.</p><div class="scorri"><table>
<tr><th>Roma</th><th>ES</th><th>Regime</th><th>Flip</th><th>Gamma max</th><th>Put max</th><th>C1</th><th>P1</th><th>Straddle</th></tr>{tab}</table></div></section>
</div>""")
    return html_pagina(filtri, [], "", "".join(corpo), "SPX", "GEX live", testa)


# --- replay ----------------------------------------------------------------------------

REPLAY_JS = r"""
(function(){
const D=__DATI__, DR=__DR__, T0=__T0__;
const hm=s=>(+s.slice(0,2))*60+(+s.slice(3,5));
const fmt=m=>String(Math.floor(m/60)).padStart(2,"0")+":"+String(m%60).padStart(2,"0");
const live=D.live.map(r=>Object.assign({m:hm(r.ora_ny)},r));
const barre=D.barre.map(b=>({m:hm(b[0]),o:b[1],h:b[2],l:b[3],c:b[4]}));
const LV=[["flip","flip","#f0c040",0],["gamma_max","gamma max","#4caf50",0],["put_max","put piu' forte","#d76ad0",0],
 ["c1","C1","#7bc67e",1],["c2","C2","#7bc67e",1],["c3","C3","#7bc67e",1],["p1","P1","#e8837f",1],["p2","P2","#e8837f",1],["p3","P3","#e8837f",1]];
const A=570, B=960, PASSO=5, RITARDO=15;
const cv=document.getElementById("rp-cv"), ctx=cv.getContext("2d");
const sl=document.getElementById("rp-sl"), ora=document.getElementById("rp-ora");
const btnPlay=document.getElementById("rp-play"), vel=document.getElementById("rp-vel"), rit=document.getElementById("rp-rit");
let T=Math.max(A,Math.min(B-1,T0)), play=false, last=0;

function livello(t){const d=rit.checked?RITARDO:0;let r=null;for(const x of live){if(x.m+d<=t)r=x;else break}return r}
function barreFino(t){const o=[];for(const b of barre){if(b.m<=t)o.push(b);else break}return o}

function disegna(){
  const dpr=window.devicePixelRatio||1, W=cv.clientWidth, H=cv.clientHeight;
  if(cv.width!==Math.round(W*dpr)||cv.height!==Math.round(H*dpr)){cv.width=Math.round(W*dpr);cv.height=Math.round(H*dpr)}
  ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
  const t=Math.floor(T), bs=barreFino(t), r=livello(t);
  const off=(r?r.offset:(live[0]?live[0].offset:0))||0;
  const ml=8, mr=118, mt=10, mb=34, pw=W-ml-mr, ph=H-mt-mb;
  const X=m=>ml+(m-A)/(B-A)*pw;
  if(!bs.length){ctx.fillStyle="#9aa3ad";ctx.font="13px Helvetica";ctx.fillText("Nessuna barra a questo orario",ml+10,mt+20);pannello(null,r,off);return}
  let lo=Infinity,hi=-Infinity;for(const b of bs){lo=Math.min(lo,b.l+off);hi=Math.max(hi,b.h+off)}
  const px=bs[bs.length-1].c+off;
  if(hi-lo<40){const c=(hi+lo)/2;lo=c-20;hi=c+20}
  lo-=12;hi+=12;
  const lvs=[];
  if(r)for(const [k,n,c,tr] of LV){if(r[k]!=null)lvs.push({k,n,c,tr,p:r[k]+off})}
  for(const l of lvs){if(l.p>=lo-25&&l.p<=hi+25){lo=Math.min(lo,l.p-3);hi=Math.max(hi,l.p+3)}}
  const Y=p=>mt+(hi-p)/(hi-lo)*ph;
  ctx.font="11px Helvetica";ctx.textBaseline="middle";
  const passoG=(hi-lo)>150?50:(hi-lo)>60?20:10;
  ctx.strokeStyle="#262a31";ctx.fillStyle="#6b737d";ctx.lineWidth=1;ctx.setLineDash([]);
  for(let g=Math.ceil(lo/passoG)*passoG;g<=hi;g+=passoG){const y=Y(g);ctx.beginPath();ctx.moveTo(ml,y);ctx.lineTo(ml+pw,y);ctx.stroke()}
  ctx.textAlign="center";ctx.textBaseline="top";
  for(let m=A;m<=B;m+=30){const x=X(m);ctx.strokeStyle="#262a31";ctx.beginPath();ctx.moveTo(x,mt);ctx.lineTo(x,mt+ph);ctx.stroke();
    ctx.fillStyle="#9aa3ad";ctx.fillText(fmt(m+DR),x,mt+ph+4);ctx.fillStyle="#5d6570";ctx.fillText(fmt(m)+" NY",x,mt+ph+17)}
  const bw=Math.max(1,pw/(B-A)*0.8);
  for(const b of bs){const x=X(b.m)+bw/2, su=b.c>=b.o;ctx.strokeStyle=ctx.fillStyle=su?"#4caf50":"#e05555";
    ctx.beginPath();ctx.moveTo(x,Y(b.h+off));ctx.lineTo(x,Y(b.l+off));ctx.stroke();
    const y1=Y(Math.max(b.o,b.c)+off), y2=Y(Math.min(b.o,b.c)+off);ctx.fillRect(x-bw/2,y1,bw,Math.max(1,y2-y1))}
  // livelli: linee e etichette a destra
  const vis=lvs.filter(l=>l.p>=lo&&l.p<=hi).sort((a,b)=>b.p-a.p);
  let yp=-99;
  for(const l of vis){const y=Y(l.p);ctx.strokeStyle=l.c;ctx.lineWidth=l.tr?1:1.6;ctx.setLineDash(l.tr?[5,4]:[]);
    ctx.beginPath();ctx.moveTo(ml,y);ctx.lineTo(ml+pw,y);ctx.stroke();
    let yl=Math.max(y,yp+12);yp=yl;ctx.setLineDash([]);ctx.fillStyle=l.c;ctx.textAlign="left";ctx.textBaseline="middle";
    ctx.fillText(l.n+" "+l.p.toFixed(1),ml+pw+6,yl)}
  // prezzo
  const yP=Y(px);ctx.setLineDash([2,3]);ctx.strokeStyle="#5aa9e6";ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(ml,yP);ctx.lineTo(ml+pw,yP);ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle="#5aa9e6";ctx.fillRect(ml+pw+2,yP-8,mr-6,16);ctx.fillStyle="#0b1b2a";ctx.textAlign="left";ctx.fillText(px.toFixed(2),ml+pw+6,yP);
  // cursore del tempo
  const xt=X(t+1);ctx.strokeStyle="#3a414b";ctx.beginPath();ctx.moveTo(xt,mt);ctx.lineTo(xt,mt+ph);ctx.stroke();
  pannello(px,r,off,lvs);
}

function pannello(px,r,off,lvs){
  const t=Math.floor(T);
  ora.textContent=fmt(t+DR)+" Roma  ·  "+fmt(t)+" New York";
  sl.value=t;
  const el=id=>document.getElementById(id);
  if(px==null){return}
  let reg="n.d.";
  if(r&&r.flip!=null){const f=r.flip+off;reg=px>f+PASSO?"gamma positivo":px<f-PASSO?"gamma negativo":"gamma flip"}
  el("rp-px").textContent=px.toFixed(2);el("rp-reg").textContent=reg;
  el("rp-liv").textContent=r?(fmt(r.m+DR)+" Roma"+(r.m<600?" (instabile)":"")):"nessuno ancora";
  el("rp-off").textContent=(off>=0?"+":"")+off;
  const tb=el("rp-tb");
  if(!r){tb.innerHTML="<tr><td colspan=3 style='color:#9aa3ad'>Il primo livello arriva circa alle "+fmt(live[0]?live[0].m+(rit.checked?RITARDO:0)+DR:0)+" Roma.</td></tr>";return}
  let h="";
  const ord=lvs.slice().sort((a,b)=>b.p-a.p);
  let vicino=null,dm=1e9;for(const l of ord){const d=Math.abs(l.p-px);if(d<dm){dm=d;vicino=l}}
  for(const l of ord){const d=l.p-px;
    h+="<tr"+(l===vicino?" style='background:#232a33'":"")+"><td style='color:"+l.c+"'>"+l.n+"</td><td class='n'>"+l.p.toFixed(2)+"</td><td class='n'>"+(d>=0?"+":"")+d.toFixed(1)+"</td></tr>"}
  tb.innerHTML=h;
}

function vai(t){T=Math.max(A,Math.min(B-1,t));disegna()}
function tick(ts){
  if(play){const dt=(ts-last)/1000;T+=dt*(+vel.value)/60;if(T>=B-1){T=B-1;play=false;btnPlay.textContent="Play"}disegna()}
  last=ts;requestAnimationFrame(tick)}
btnPlay.onclick=()=>{play=!play;btnPlay.textContent=play?"Pausa":"Play";if(T>=B-1&&play)T=A};
sl.oninput=()=>{T=+sl.value;disegna()};
rit.onchange=disegna;
for(const [id,d] of [["rp-m5",-5],["rp-m1",-1],["rp-p1",1],["rp-p5",5]])document.getElementById(id).onclick=()=>{play=false;btnPlay.textContent="Play";vai(Math.floor(T)+d)};
addEventListener("keydown",e=>{if(e.target.tagName==="SELECT")return;
  if(e.key===" "){e.preventDefault();btnPlay.click()}else if(e.key==="ArrowRight"){vai(Math.floor(T)+(e.shiftKey?5:1))}else if(e.key==="ArrowLeft"){vai(Math.floor(T)-(e.shiftKey?5:1))}});
addEventListener("resize",disegna);
sl.min=A;sl.max=B-1;disegna();requestAnimationFrame(tick);
})();
"""


def pagina_replay(q: dict) -> str:
    giorni = storico.live_giorni()
    giorno = q.get("d", [giorni[0] if giorni else ""])[0]
    if giorno not in giorni:
        giorno = giorni[0] if giorni else ""
    opz = "".join(f'<option value="{d}"{" selected" if d == giorno else ""}>{GIORNI[date.fromisoformat(d).weekday()]} '
                  f'{date.fromisoformat(d):%d/%m/%Y}</option>' for d in giorni[:60])
    filtri = (f'<div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:space-between;align-items:end">'
              f'<form method="get" class="filtri"><label>Giorno<select name="d" onchange="this.form.submit()">{opz}</select></label>'
              f'<noscript><button type="submit">Mostra</button></noscript></form>'
              f'<div style="display:flex;gap:10px;align-items:center"><a href="/live">Live</a><a href="/">Livelli</a>'
              f'<a href="/storico">Storico</a></div></div>')
    if not giorno:
        return html_pagina(filtri, ["Nessuna giornata live salvata."], "", "", "SPX", "GEX replay")
    dati = storico.replay_giorno(giorno)
    if not dati["barre"] or not dati["live"]:
        return html_pagina(filtri, [f"Il {giorno} manca di barre a 1 minuto o di scatti live: non si puo' rivedere."],
                           "", "", "SPX", "GEX replay")
    mezzo = datetime.fromisoformat(giorno + "T12:00:00").replace(tzinfo=g.NY)
    diff = int((mezzo.astimezone(g.ROMA).replace(tzinfo=None) - mezzo.replace(tzinfo=None)).total_seconds() // 60)
    try:
        t0 = int(q.get("t", ["10:01"])[0][:2]) * 60 + int(q.get("t", ["10:01"])[0][3:5])
    except ValueError:
        t0 = 10 * 60 + 1
    js = (REPLAY_JS.replace("__DATI__", json.dumps(dati).replace("</", "<\\/"))
          .replace("__DR__", str(diff)).replace("__T0__", str(t0)))
    corpo = f"""<section class="pan">
<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px">
<button id="rp-play" class="primo">Play</button>
<button id="rp-m5" title="indietro 5 minuti">&minus;5</button><button id="rp-m1" title="indietro 1 minuto">&minus;1</button>
<button id="rp-p1" title="avanti 1 minuto">+1</button><button id="rp-p5" title="avanti 5 minuti">+5</button>
<label>Velocita'<select id="rp-vel"><option value="1">1x (1 minuto al minuto)</option><option value="5">5x</option>
<option value="10">10x</option><option value="30" selected>30x</option><option value="60">60x (1 minuto al secondo)</option>
<option value="120">120x</option></select></label>
<label class="riga"><input type="checkbox" id="rp-rit" checked> livelli in ritardo di 15 minuti, come nel live vero</label>
<b id="rp-ora" style="margin-left:auto"></b></div>
<input type="range" id="rp-sl" style="width:100%" min="570" max="959" step="1" value="601">
<canvas id="rp-cv" style="width:100%;height:480px;display:block;margin-top:8px"></canvas>
<p class="nota">Prezzo ES stimato = SPX a 1 minuto + offset dello scatto. Orari sull'asse: Roma, sotto New York.
I livelli sono a gradini: valgono fino al dato successivo. Tasti: spazio = play, frecce = 1 minuto (con maiusc 5).</p></section>
<div class="due"><section class="pan dati">
<div><b id="rp-px">-</b><span>ES stimato</span></div><div><b id="rp-reg">-</b><span>regime (prezzo contro flip)</span></div>
<div><b id="rp-liv">-</b><span>ora del dato CBOE dei livelli</span></div><div><b id="rp-off">-</b><span>offset ES&minus;SPX</span></div></section>
<section class="pan"><h2>Livelli e distanza dal prezzo</h2><div class="scorri"><table>
<tr><th>Livello</th><th>Prezzo ES</th><th>Distanza</th></tr><tbody id="rp-tb"></tbody></table></div>
<p class="nota">In evidenza il piu' vicino. Gli scatti prima delle 10:00 a New York sono instabili.</p></section></div>
<script>{js}</script>"""
    return html_pagina(filtri, [], "", corpo, "SPX", "GEX replay")


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
        if url.path == "/stato":
            problema = problema_feed()
            return self.rispondi(503 if problema else 200, problema or "ok", "text/plain; charset=utf-8")
        if not self.autorizzato():
            return
        try:
            if url.path == "/":
                self.rispondi(200, pagina(parse_qs(url.query, keep_blank_values=True)))
            elif url.path == "/storico":
                self.rispondi(200, pagina_storico())
            elif url.path == "/live":
                self.rispondi(200, pagina_live(parse_qs(url.query)))
            elif url.path == "/replay":
                self.rispondi(200, pagina_replay(parse_qs(url.query)))
            elif url.path == "/live.csv":
                self.rispondi(200, storico.csv_live(), "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="gex_live.csv"'})
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
        if SOLA_LETTURA:
            return self.rispondi(403, "Sito di sola lettura", "text/plain; charset=utf-8")
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
    if SU_RAILWAY and not PASSWORD and not PUBBLICO:
        sys.exit("GEX_PASSWORD non impostata: su Railway il sito non parte senza password. "
                 "Per un sito aperto di sola lettura imposta GEX_PUBBLICO=1.")
    host = "0.0.0.0" if PASSWORD or PUBBLICO else "127.0.0.1"
    porta = int(os.getenv("PORT", "8000"))
    fatti, falliti = storico.ricostruisci()   # recupera gli scarichi non ancora nello storico
    print(f"storico: {fatti} scarichi recuperati, {len(falliti)} non riusciti", flush=True)
    if os.getenv("GEX_SCHEDULER", "1") != "0":
        threading.Thread(target=scheduler, daemon=True).start()
    print(f"GEX su {host}:{porta}, dati in {g.DATI}", flush=True)
    ThreadingHTTPServer((host, porta), Gestore).serve_forever()


if __name__ == "__main__":
    main()
