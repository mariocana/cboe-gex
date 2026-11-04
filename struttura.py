#!/usr/bin/env python3
"""La struttura "montata" dagli operatori secondo Serafini, e dove guadagna o perde.

Serafini (L1 149-153) legge il profilo GEX della 0DTE come una sola struttura:
    strangle venduto   : vende C3 e P3 (incassa il premio)
    vertical rialzista : compra C1, vende C2
    vertical ribassista: compra P1, vende P2
e mostra il P&L su ThinkOrSwim: dove la linea va sotto zero chi l'ha montata deve
coprirsi con i future (short sotto le put, long sopra le call).

Questo script la ricostruisce sui livelli proposti da scarica_gex.py (o su quelli che
gli passi), con i prezzi medi bid/ask dello scarico CBOE, e stampa/disegna:
  - P&L a scadenza e "adesso" (Black-Scholes con l'IV di ogni opzione)
  - i break-even, cioe' i prezzi ES dove scattano le coperture, da confrontare con
    il +-10/15 fisso di Serafini.

Uso:
    python3 struttura.py                       # ultimo scarico in dati/, livelli automatici
    python3 struttura.py --gambe C1=7598.5,C2=7608.5,C3=7628.5,P1=7578.5,P2=7568.5,P3=7548.5
    python3 struttura.py --file dati/2026-11-03_1605_SPX.json.gz --qta 10
    python3 struttura.py --indice NDX

Le gambe si danno in prezzi del future (come li vedi sul grafico); lo script toglie
l'offset e prende lo strike quotato piu' vicino. Il grafico SVG finisce in dati/.
"""
import argparse
import math
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

import scarica_gex as g

SEGNO = {"C1": +1, "C2": -1, "C3": -1, "P1": +1, "P2": -1, "P3": -1}   # +1 comprata, -1 venduta
ORDINE = ["P3", "P2", "P1", "C1", "C2", "C3"]


def ultimo_scarico(sym: str) -> Path:
    files = sorted(g.DATI.glob(f"*_{sym}.json.gz"))
    if not files:
        sys.exit("nessuno scarico in dati/: lancia prima scarica_gex.py")
    return files[-1]


def n_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(tipo: str, s: float, k: float, t: float, iv: float) -> float:
    """Prezzo Black-Scholes con tasso e dividendi a zero (per una 0DTE la differenza e' trascurabile)."""
    if t <= 0 or iv <= 0:
        return max(0.0, s - k) if tipo == "C" else max(0.0, k - s)
    d1 = (math.log(s / k) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    return s * n_cdf(d1) - k * n_cdf(d2) if tipo == "C" else k * n_cdf(-d2) - s * n_cdf(-d1)


def costruisci(an: dict, gambe_es: dict | None = None, qta: int = 10) -> dict:
    """La struttura sui livelli di g.analizza; gambe_es corregge singole gambe in prezzi ES."""
    off, scad, ops = an["off"], an["scad"], an["ops"]
    strike = {f"C{i + 1}": k for i, k in enumerate(an["c"])} | {f"P{i + 1}": k for i, k in enumerate(an["p"])}
    for nome, prezzo_es in (gambe_es or {}).items():
        if nome not in SEGNO:
            raise ValueError(f"gamba sconosciuta: {nome} (usa C1..C3, P1..P3)")
        strike[nome] = prezzo_es - off

    # quotazioni della scadenza, solo 0DTE/settimanali
    quote = {(o["tipo"], o["strike"]): o for o in ops
             if o["root"] == an["ind"]["radice"] and o["scadenza"] == scad and o["ask"] > 0}

    gambe, saltate = [], []
    for nome in ORDINE:
        if nome not in strike:
            continue
        tipo = nome[0]
        quotati = [q for t, q in quote if t == tipo]
        k = min(quotati, key=lambda q: abs(q - strike[nome]), default=None)   # lo strike quotato piu' vicino
        if k is None or abs(k - strike[nome]) > an["ind"]["passo"]:
            saltate.append(f"{nome}: nessuna quotazione per {tipo} {strike[nome]:g}")
            continue
        o = quote[(tipo, k)]
        gambe.append({"nome": nome, "tipo": tipo, "k": k, "q": SEGNO[nome] * qta,
                      "prezzo": (o["bid"] + o["ask"]) / 2, "iv": o["iv"]})
    if not gambe:
        raise ValueError("nessuna gamba quotata")

    scadenza_ny = datetime.combine(scad, time(16, 0), g.NY)
    t_anni = max(0.0, (scadenza_ny - an["ts_ny"]).total_seconds()) / (365 * 24 * 3600)
    credito = -sum(x["q"] * x["prezzo"] for x in gambe)   # >0 = incassato

    def pnl(s: float, t: float) -> float:
        valore = sum(x["q"] * bs(x["tipo"], s, x["k"], t, x["iv"]) for x in gambe)
        return valore + credito   # in punti indice (x100 = dollari SPX)

    ks = [x["k"] for x in gambe]
    passo = an["ind"]["passo"]          # griglia fine: 0,25 punti su SPX, 1,25 su NDX
    fine = passo / 20
    griglia = [i * fine for i in range(int((min(ks) - 12 * passo) / fine), int((max(ks) + 12 * passo) / fine) + 1)]
    scad_pnl = [pnl(s, 0) for s in griglia]
    ora_pnl = [pnl(s, t_anni) for s in griglia]

    pareggi = []
    for i in range(1, len(griglia)):
        y0, y1 = scad_pnl[i - 1], scad_pnl[i]
        if (y0 < 0) != (y1 < 0):
            pareggi.append(griglia[i - 1] + (griglia[i] - griglia[i - 1]) * (-y0) / (y1 - y0))

    titolo = f"Struttura {scad} - scarico {an['ts_cboe']} UTC - {qta} contratti per gamba"
    svg = disegna(griglia, scad_pnl, ora_pnl, gambe, pareggi, off, an["gmax"], an["spot"], titolo)
    return {"gambe": gambe, "saltate": saltate, "credito": credito, "griglia": griglia,
            "scad_pnl": scad_pnl, "ora_pnl": ora_pnl, "pareggi": pareggi, "svg": svg}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--indice", choices=g.INDICI, default="SPX", help="SPX (default) o NDX")
    ap.add_argument("--file", type=Path, help="scarico CBOE salvato; default l'ultimo in dati/")
    ap.add_argument("--offset", type=float, help="offset a mano; default automatico come scarica_gex.py")
    ap.add_argument("--scadenza", type=date.fromisoformat, help="AAAA-MM-GG; default la 0DTE")
    ap.add_argument("--gambe", help="es. C1=7598.5,C2=7608.5,P1=7578.5 in prezzi ES; quelle non date restano automatiche")
    ap.add_argument("--qta", type=int, default=10, help="contratti per gamba (default 10, come nelle slide)")
    a = ap.parse_args()

    fonte = a.file or ultimo_scarico(a.indice)
    gambe_es = {}
    for pezzo in (a.gambe.split(",") if a.gambe else []):
        nome, prezzo_es = pezzo.split("=")
        gambe_es[nome.strip().upper()] = float(prezzo_es)
    try:
        an = g.analizza(g.leggi_json(fonte), a.scadenza, a.offset)
        st = costruisci(an, gambe_es, a.qta)
    except (g.OffsetMancante, ValueError) as e:
        sys.exit(str(e))
    off, scad, gambe, credito = an["off"], an["scad"], st["gambe"], st["credito"]
    fut, sym, dist = an["ind"]["future"], an["sym"], an["ind"]["distanze"]

    print(f"CBOE {an['ts_cboe']} UTC  spot {sym} {an['spot']:.2f} ({fut} ~{an['spot'] + off:.2f})  scadenza {scad}")
    print(f"offset {fut}-{sym} {off:+g} ({an['nota_off']})   file: {fonte}\n")
    for r in st["saltate"]:
        print(f"  {r}, gamba saltata")
    print("Struttura (lato di chi l'ha montata: fondi e retail; i market maker hanno l'opposto)")
    for x in gambe:
        verbo = "compra" if x["q"] > 0 else "vende "
        print(f"  {x['nome']}  {verbo} {abs(x['q']):3d} {x['tipo']} {x['k']:7g}  ({fut} {x['k'] + off:8.2f})"
              f"  prezzo {x['prezzo']:6.2f}  IV {x['iv']:.1%}")
    print(f"  {'credito' if credito >= 0 else 'debito'} netto: {abs(credito):.2f} punti"
          f" (= ${abs(credito) * 100:,.0f} con moltiplicatore 100)\n")

    print(f"Break-even a scadenza (prezzi {fut}), dove chi ha montato la struttura comincia a perdere:")
    for b in st["pareggi"]:
        vicino = min(gambe, key=lambda x: abs(x["k"] - b))
        print(f"  {b + off:8.2f}   {b - vicino['k']:+6.1f} punti da {vicino['nome']} ({vicino['k'] + off:.2f})")
    print(f"  (Serafini usa +-{dist[0]} in bassa volatilita', +-{dist[1]} in alta)\n")

    print(f"{fut:>8}  {'scadenza':>9}  {'adesso':>9}")
    for i in range(0, len(st["griglia"]), 20):   # ogni 5 punti
        s = st["griglia"][i]
        tag = " ".join(x["nome"] for x in gambe if x["k"] == s)
        print(f"{s + off:8.2f}  {st['scad_pnl'][i]:9.1f}  {st['ora_pnl'][i]:9.1f}  {tag}")

    out = g.DATI / f"{fonte.name.split('.')[0]}_{scad}_struttura.svg"
    out.write_text(st["svg"])
    print(f"\ngrafico: {out}")


def disegna(xs, y_scad, y_ora, gambe, pareggi, off, gmax, spot, titolo) -> str:
    W, H, M = 1000, 520, 60
    x0, x1 = xs[0] + off, xs[-1] + off
    ymin, ymax = min(y_scad + y_ora), max(y_scad + y_ora)
    pad = (ymax - ymin) * 0.08 or 1
    ymin, ymax = ymin - pad, ymax + pad
    X = lambda v: M + (v - x0) / (x1 - x0) * (W - 2 * M)
    Y = lambda v: H - M - (v - ymin) / (ymax - ymin) * (H - 2 * M)
    linea = lambda ys: " ".join(f"{X(x + off):.1f},{Y(y):.1f}" for x, y in zip(xs, ys))
    el = [f'<rect width="{W}" height="{H}" fill="#14161a"/>',
          f'<text x="{M}" y="28" fill="#ddd" font-size="15">{titolo}</text>',
          f'<line x1="{M}" x2="{W - M}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="#666"/>']
    for i, x in enumerate(gambe):
        col = "#e05555" if x["q"] < 0 else "#4caf50"
        xx = X(x["k"] + off)
        el += [f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{M}" y2="{H - M}" stroke="{col}" stroke-dasharray="4 4"/>',
               f'<text x="{xx + 3:.1f}" y="{M + 12 + (i % 2) * 15}" fill="{col}" font-size="12">{x["nome"]} {x["k"] + off:g}</text>']
    for b in pareggi:
        xx = X(b + off)
        el += [f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{M}" y2="{H - M}" stroke="#f0c040"/>',
               f'<text x="{xx + 3:.1f}" y="{H - M - 6}" fill="#f0c040" font-size="12">{b + off:.1f}</text>']
    for v, nome in [(gmax + off, "gamma max"), (spot + off, "prezzo")]:
        if x0 <= v <= x1:
            el.append(f'<text x="{X(v):.1f}" y="{H - M + 34}" fill="#9ab" font-size="12" text-anchor="middle">{nome} {v:g}</text>')
    el += [f'<polyline points="{linea(y_scad)}" fill="none" stroke="#5aa9e6" stroke-width="2"/>',
           f'<polyline points="{linea(y_ora)}" fill="none" stroke="#c678dd" stroke-width="2"/>',
           f'<text x="{W - M - 230}" y="28" fill="#5aa9e6" font-size="13">a scadenza</text>',
           f'<text x="{W - M - 130}" y="28" fill="#c678dd" font-size="13">adesso</text>']
    tacca = 10 if x1 - x0 < 400 else 50
    for v in range(int(x0 // tacca * tacca), int(x1) + 1, tacca):
        if v >= x0:
            el.append(f'<text x="{X(v):.1f}" y="{H - M + 16}" fill="#888" font-size="11" text-anchor="middle">{v}</text>')
    for v in (ymin, 0, ymax):
        el.append(f'<text x="{M - 6}" y="{Y(v) + 4:.1f}" fill="#888" font-size="11" text-anchor="end">{v:.0f}</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" style="max-width:100%;height:auto" font-family="Helvetica, Arial">{"".join(el)}</svg>'


if __name__ == "__main__":
    main()
