#!/usr/bin/env python3
"""Scarico della catena SPX o NDX dal CBOE e profilo GEX "alla Serafini".

Uso (a mano, la mattina 7:30-8:30 e alle 15:47):

    python3 scarica_gex.py
    python3 scarica_gex.py --indice NDX              # Nasdaq, livelli in prezzi NQ
    python3 scarica_gex.py --alta                    # distanze alte (15 ES, 70 NQ)
    python3 scarica_gex.py --scadenza 2026-11-06     # settimanale
    python3 scarica_gex.py --offset 48               # offset ES-SPX imposto a mano
    python3 scarica_gex.py --file dati/2026-11-03_0803_SPX.json.gz   # rifà il calcolo da uno scarico salvato

Cosa fa:
1. scarica la catena SPX completa (ritardata 15 minuti) dal JSON pubblico del CBOE,
   lo stesso dato della pagina Quotes Dashboard da cui Serafini scarica il CSV;
2. salva il JSON originale in dati/ (compresso);
3. per la scadenza scelta (di default la 0DTE di oggi a New York) calcola per ogni strike
       netto = (OI call + Vol call) * Gamma call - (OI put + Vol put) * Gamma put
   sui 51 strike multipli di 5 attorno allo spot, e lo porta su ES sommando l'offset
   ES-SPX;
4. stampa il profilo e una proposta di livelli (flip, gamma massimo, C1-C3, P1-P3,
   break a +-10/15) e salva tutto in dati/ come CSV.

L'offset ES-SPX e' automatico. Il future ES quota il forward dell'S&P alla sua scadenza,
e il forward si legge dalle opzioni SPX della stessa scadenza trimestrale (terzo venerdi'
di mar/giu/set/dic) con la parita' put-call: F = strike + call - put all'ATM. Funziona
solo a mercato cash aperto (15:30-22:00), quando lo spot e' vivo: in quei casi lo
script lo calcola e lo salva in dati/offset.csv. Fuori orario (la mattina) usa l'ultimo
salvato in un giorno precedente, come fa Serafini ("il differenziale del giorno prima").
Quindi basta lanciarlo una volta in sessione (es. alle 15:47) perche' la mattina dopo
l'offset ci sia. --offset lo forza a mano.

I livelli C/P sono una proposta automatica (picchi locali): la scelta finale resta a te.
Solo libreria standard: basta python3.
"""
import argparse
import csv
import gzip
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_{}.json"
URL_BARRE = "https://cdn.cboe.com/api/global/delayed_quotes/charts/intraday/_{}.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
OPTION_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
NY = ZoneInfo("America/New_York")
ROMA = ZoneInfo("Europe/Rome")
DATI = Path(os.getenv("GEX_DATI") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
            or Path(__file__).resolve().parent / "dati")

# radice = 0DTE e settimanali (settlement in chiusura); mensile = scadenza del future.
# Il passo dell'NDX e' un adattamento nostro: i suoi strike sono irregolari (5, 10, 25), quindi
# il gamma si somma in fasce da 25 punti. Distanze: (bassa, alta volatilita').
INDICI = {
    "SPX": {"future": "ES", "radice": "SPXW", "mensile": "SPX", "passo": 5, "distanze": (10, 15)},
    "NDX": {"future": "NQ", "radice": "NDXP", "mensile": "NDX", "passo": 25, "distanze": (50, 70)},
}
N_STRIKE = 25          # 25 livelli sopra + 25 sotto + ATM = 51 (SPX +-125 punti, NDX +-625)
SOGLIA_PICCO = 0.20    # un picco conta se e' almeno il 20% della barra piu' lunga del suo lato


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def scarica(sym: str = "SPX") -> dict:
    return _get(URL.format(sym))


def scarica_barre(sym: str) -> dict:
    """Barre a 1 minuto dell'ultima sessione cash (orario di New York)."""
    return _get(URL_BARRE.format(sym))


def indice_di(payload: dict) -> str:
    return payload["data"]["symbol"].lstrip("^")


def salva_json(payload: dict, adesso: datetime, sym: str = "SPX") -> Path:
    DATI.mkdir(exist_ok=True)
    p = DATI / f"{adesso:%Y-%m-%d_%H%M}_{sym}.json.gz"
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(payload, f)
    tmp.replace(p)
    return p


def leggi_json(p: Path) -> dict:
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt") as f:
        return json.load(f)


def num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def opzioni(payload: dict) -> list[dict]:
    out = []
    for o in payload["data"]["options"]:
        m = OPTION_RE.match(o.get("option", ""))
        if not m:
            continue
        root, ymd, right, strike = m.groups()
        out.append({
            "root": root,
            "scadenza": datetime.strptime(ymd, "%y%m%d").date(),
            "tipo": right,
            "strike": int(strike) / 1000,
            "oi": num(o.get("open_interest")),
            "vol": num(o.get("volume")),
            "gamma": num(o.get("gamma")),
            "iv": num(o.get("iv")),
            "bid": num(o.get("bid")),
            "ask": num(o.get("ask")),
        })
    return out


def in_sessione(ts_ny: datetime) -> bool:
    m = ts_ny.hour * 60 + ts_ny.minute
    return ts_ny.weekday() < 5 and 9 * 60 + 30 <= m < 16 * 60


def scadenza_future(ops: list[dict], oggi: date, ind: dict) -> date | None:
    """Scadenza trimestrale delle opzioni mensili (terzo venerdi' di mar/giu/set/dic) del future
    in uso: si passa alla successiva dal giovedi' del roll di ES, 8 giorni prima della scadenza."""
    trim = sorted({o["scadenza"] for o in ops if o["root"] == ind["mensile"] and o["scadenza"].month % 3 == 0
                   and o["scadenza"].weekday() == 4 and 15 <= o["scadenza"].day <= 21})
    return next((d for d in trim if d > oggi + timedelta(days=8)), None)


def offset_parita(ops: list[dict], spot: float, scad: date, ind: dict) -> float | None:
    """Future - indice = forward implicito - spot, mediana dei 3 strike piu' vicini all'ATM."""
    call, put = {}, {}
    for o in ops:
        if o["root"] == ind["mensile"] and o["scadenza"] == scad and o["bid"] > 0 and o["ask"] > 0:
            (call if o["tipo"] == "C" else put)[o["strike"]] = (o["bid"] + o["ask"]) / 2
    ks = sorted(set(call) & set(put), key=lambda k: abs(call[k] - put[k]))[:3]
    if not ks:
        return None
    fwd = sorted(k + call[k] - put[k] for k in ks)[len(ks) // 2]
    return round((fwd - spot) * 4) / 4   # al tick di ES


def offset_salvato(prima_di: date, sym: str) -> tuple[date, float] | None:
    p = DATI / f"offset_{sym}.csv"
    if not p.exists():
        return None
    righe = [(date.fromisoformat(r["data"]), float(r["offset"])) for r in csv.DictReader(open(p))]
    righe = [r for r in righe if r[0] < prima_di]
    return max(righe) if righe else None


def salva_offset(giorno: date, valore: float, ts: datetime, sym: str) -> None:
    DATI.mkdir(exist_ok=True)
    p = DATI / f"offset_{sym}.csv"
    righe = list(csv.DictReader(open(p))) if p.exists() else []
    righe = [r for r in righe if r["data"] != giorno.isoformat()]   # tiene l'ultimo del giorno
    righe.append({"data": giorno.isoformat(), "offset": valore, "ora_ny": f"{ts:%H:%M}"})
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["data", "offset", "ora_ny"])
        w.writeheader()
        w.writerows(sorted(righe, key=lambda r: r["data"]))


def scadenza_default(scadenze: list[date], ora_ny: datetime) -> date:
    """La 0DTE di oggi a New York; dopo le 16:15 ET (o se oggi non c'e') la prossima."""
    oggi = ora_ny.date()
    if ora_ny.hour * 60 + ora_ny.minute >= 16 * 60 + 15:
        oggi += timedelta(days=1)
    return min(d for d in scadenze if d >= oggi)


def profilo(ops: list[dict], scad: date, spot: float, ind: dict) -> list[tuple[float, float, float, float]]:
    """[(strike, netto, gex call, gex put)] sui 51 livelli attorno allo spot."""
    passo = ind["passo"]
    atm = round(spot / passo) * passo
    lo, hi = atm - N_STRIKE * passo, atm + N_STRIKE * passo
    call, put = {}, {}
    for o in ops:
        k = round(o["strike"] / passo) * passo
        if o["root"] != ind["radice"] or o["scadenza"] != scad or k < lo or k > hi:
            continue
        g = (o["oi"] + o["vol"]) * o["gamma"]
        d = call if o["tipo"] == "C" else put
        d[k] = d.get(k, 0.0) + g
    strikes = sorted(set(call) | set(put))
    return [(k, call.get(k, 0.0) - put.get(k, 0.0), call.get(k, 0.0), put.get(k, 0.0)) for k in strikes]


def gamma_flip(prof, spot: float) -> float | None:
    """Strike dove il netto passa da negativo (sotto) a positivo; se ce n'e' piu' d'uno, il piu' vicino allo spot."""
    passaggi = [prof[i][0] for i in range(1, len(prof)) if prof[i - 1][1] < 0 <= prof[i][1]]
    return min(passaggi, key=lambda k: abs(k - spot)) if passaggi else None


def picchi(prof, segno: int) -> list[float]:
    """Massimi locali del lato call (segno=+1) o put (segno=-1), oltre la soglia."""
    vals = [segno * n for _, n, _, _ in prof]
    top = max([v for v in vals if v > 0], default=0)
    out = []
    for i, v in enumerate(vals):
        if v <= 0 or v < SOGLIA_PICCO * top:
            continue
        sx = vals[i - 1] if i > 0 else float("-inf")
        dx = vals[i + 1] if i < len(vals) - 1 else float("-inf")
        if v >= sx and v >= dx:
            out.append(prof[i][0])
    return out


class OffsetMancante(Exception):
    pass


def offset_es(ops: list[dict], spot: float, ts_ny: datetime, manuale: float | None,
              salva: bool, sym: str) -> tuple[float, str]:
    """Offset future-indice: a mano, oppure dalla parita' put-call in sessione, oppure l'ultimo
    salvato in un giorno precedente. salva=False per i ricalcoli su scarichi vecchi."""
    ind = INDICI[sym]
    scad_es = scadenza_future(ops, ts_ny.date(), ind)
    live = offset_parita(ops, spot, scad_es, ind) if scad_es and in_sessione(ts_ny) else None
    if live is not None and salva:
        salva_offset(ts_ny.date(), live, ts_ny, sym)
    if manuale is not None:
        return manuale, "a mano"
    if live is not None:
        return live, f"calcolato dalle {sym} {scad_es} (forward - spot)"
    # dopo l'apertura vale anche il valore di quel giorno: la sera la CBOE tiene la data della sessione
    aperto = ts_ny.hour * 60 + ts_ny.minute >= 9 * 60 + 30
    prec = offset_salvato(ts_ny.date() + timedelta(days=1 if aperto else 0), sym)
    if prec is None:
        raise OffsetMancante(f"offset {ind['future']}-{sym} non disponibile: fuori sessione e nessun valore salvato. "
                             "Serve uno scarico a mercato aperto (15:30-22:00) o un offset a mano.")
    return prec[1], f"salvato il {prec[0]}"


def livelli_base(prof, spot: float):
    """(flip, gamma massimo, [C1..C3], [P1..P3], put piu' forte), strike SPX."""
    flip = gamma_flip(prof, spot)
    gmax = max(prof, key=lambda r: r[1])[0]
    call_su = [k for k in picchi(prof, +1) if flip is None or k >= flip]
    put_giu = sorted([k for k in picchi(prof, -1) if flip is None or k < flip], reverse=True)
    pmax = min(prof, key=lambda r: r[1])[0]
    return flip, gmax, call_su[:3], put_giu[:3], pmax


def analizza(payload: dict, scadenza: date | None = None, offset: float | None = None,
             alta: bool = False, salva: bool = False) -> dict:
    """Tutto il calcolo di uno scarico: offset, profilo, livelli. Non stampa niente."""
    sym = indice_di(payload)
    ind = INDICI[sym]
    spot = num(payload["data"]["current_price"])
    ts_cboe = payload.get("timestamp", "")
    ts_ny = datetime.fromisoformat(ts_cboe).replace(tzinfo=timezone.utc).astimezone(NY)
    ops = opzioni(payload)
    off, nota_off = offset_es(ops, spot, ts_ny, offset, salva, sym)
    scadenze = sorted({o["scadenza"] for o in ops if o["root"] == ind["radice"]})
    scad = scadenza or scadenza_default(scadenze, ts_ny)
    prof = profilo(ops, scad, spot, ind)
    if not prof:
        raise ValueError(f"nessuna opzione per la scadenza {scad}")

    dist = ind["distanze"][1 if alta else 0]
    flip, gmax, c, p, pmax = livelli_base(prof, spot)
    es = lambda k: k + off
    regime = ("gamma positivo" if flip is not None and spot > flip + ind["passo"]
              else "gamma negativo" if flip is not None and spot < flip - ind["passo"] else "gamma flip")

    livelli = []
    if len(c) == 3: livelli += [(es(c[2]) + dist, "break long intraday (trend following) = C3+%d" % dist),
                                (es(c[2]), "tgt long = C3")]
    if len(c) >= 2: livelli += [(es(c[1]), "C2")]
    if c: livelli += [(es(c[0]) + dist, "break long = C1+%d" % dist), (es(c[0]), "C1")]
    if gmax not in c: livelli += [(es(gmax), "gamma massimo")]
    if flip is not None: livelli += [(es(flip), "gamma flip"), (es(flip) - dist, "break gamma negativo = flip-%d" % dist)]
    for i, k in enumerate(p):
        livelli += [(es(k), f"P{i + 1}"), (es(k) - dist, f"break gamma negativo = P{i + 1}-{dist}")]
    if pmax not in p: livelli += [(es(pmax), "put piu' forte")]
    livelli = sorted(set(livelli), key=lambda r: -r[0])

    return {"sym": sym, "ind": ind, "spot": spot, "ts_cboe": ts_cboe, "ts_ny": ts_ny, "ops": ops, "off": off, "nota_off": nota_off,
            "scadenze": scadenze, "scad": scad, "prof": prof, "dist": dist, "flip": flip, "gmax": gmax,
            "c": c, "p": p, "pmax": pmax, "regime": regime, "livelli": livelli}


def tipo_livello(nome: str) -> str:
    """Colore su Deepcharts, come nei piani di Serafini: L verde, S rosso, N flip e gamma massimo."""
    if nome.startswith(("gamma flip", "gamma massimo")):
        return "N"
    if nome.startswith("break long") or nome[:2] in ("P1", "P2", "P3") or nome.startswith("put"):
        return "L"
    return "S"


def riga_deepcharts(an: dict) -> str:
    """I livelli in una riga, da incollare nel parametro dell'indicatore GexLevels.cs."""
    ora = an["ts_ny"].astimezone(ROMA)
    testa = f"GEX {an['ind']['future']} {ora:%Y-%m-%d %H:%M}"
    uniti: dict[float, list[str]] = {}          # stesso prezzo: una linea, etichette unite
    for prezzo, nome in an["livelli"]:
        uniti.setdefault(prezzo, []).append(nome)
    righe = [f"{p:.2f};{' / '.join(nomi).replace(';', ',').replace('|', ',')};{tipo_livello(nomi[0])}"
             for p, nomi in uniti.items()]
    return "|".join([testa] + righe)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--indice", choices=INDICI, default="SPX", help="SPX (default) o NDX")
    ap.add_argument("--offset", type=float,
                    help="differenziale future-indice imposto a mano; senza, e' automatico (vedi sopra)")
    ap.add_argument("--alta", action="store_true",
                    help="alta volatilita' (range di ieri > 1,3%% o VIX in backwardation): distanze alte")
    ap.add_argument("--scadenza", type=date.fromisoformat, help="AAAA-MM-GG; default la 0DTE di oggi")
    ap.add_argument("--file", type=Path, help="ricalcola da uno scarico salvato invece di scaricare")
    a = ap.parse_args()

    adesso = datetime.now(timezone.utc)
    if a.file:
        payload = leggi_json(a.file)
        fonte = a.file
    else:
        payload = scarica(a.indice)
        fonte = salva_json(payload, adesso.astimezone(ROMA), a.indice)

    try:
        an = analizza(payload, a.scadenza, a.offset, a.alta, salva=not a.file)
    except (OffsetMancante, ValueError) as e:
        if not a.file:
            import storico
            storico.registra(fonte.name, payload)   # lo scatto va nello storico anche senza offset
        sys.exit(str(e))
    if not a.file:
        import storico
        storico.registra(fonte.name, payload)
    prof, off, scad = an["prof"], an["off"], an["scad"]
    flip, gmax, c, p = an["flip"], an["gmax"], an["c"], an["p"]
    es = lambda k: k + off

    fut, sym = an["ind"]["future"], an["sym"]
    print(f"CBOE {an['ts_cboe']} UTC  spot {sym} {an['spot']:.2f}  scadenza {scad}  distanze {an['dist']}")
    print(f"offset {fut}-{sym} {off:+g} ({an['nota_off']})")
    print(f"file: {fonte}\n")
    top = max(abs(n) for _, n, _, _ in prof) or 1
    for k, n, _, _ in reversed(prof):
        bar = ("#" * round(30 * n / top)).ljust(30) if n > 0 else ""
        neg = ("#" * round(30 * -n / top)).rjust(30) if n < 0 else " " * 30
        tag = []
        if k == flip: tag.append("FLIP")
        if k == gmax: tag.append("GAMMA MAX")
        if k in c: tag.append(f"C{c.index(k) + 1}")
        if k in p: tag.append(f"P{p.index(k) + 1}")
        print(f"{es(k):8.2f}  {n:9.2f}  {neg}|{bar}  {' '.join(tag)}")

    print(f"\nPrezzo rispetto al flip: {an['regime']} (calcolato sullo spot {sym}: ricontrollalo sul future)")
    print(f"\nLivelli proposti (prezzi {fut}):")
    for prezzo, nome in an["livelli"]:
        print(f"  {prezzo:8.2f}  {nome}")

    base = DATI / f"{adesso.astimezone(ROMA):%Y-%m-%d_%H%M}_{sym}_{scad}"
    DATI.mkdir(exist_ok=True)
    with open(f"{base}_profilo.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strike", f"prezzo_{fut.lower()}", "netto", "gex_call", "gex_put"])
        for k, n, gc, gp in prof:
            w.writerow([k, es(k), round(n, 4), round(gc, 4), round(gp, 4)])
    with open(f"{base}_livelli.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"prezzo_{fut.lower()}", "livello"])
        w.writerows([(round(pr, 2), nm) for pr, nm in an["livelli"]])
    print(f"\nsalvati: {base}_profilo.csv, {base}_livelli.csv")


if __name__ == "__main__":
    main()
