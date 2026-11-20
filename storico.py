#!/usr/bin/env python3
"""Storico degli scatti in SQLite (dati/gex.db), per le statistiche sui giorni passati.

La fonte resta il JSON grezzo di ogni scarico: il database si ricalcola da li'.

    python3 storico.py                  # aggiunge gli scarichi non ancora registrati
    python3 storico.py --tutto          # ricalcola tutto, es. dopo aver cambiato le regole

Tabelle:
    scatti   una riga per scarico: indice, orario, spot, offset future-indice
    livelli  una riga per scarico e scadenza (0DTE e settimanale): strike SPX di flip,
             gamma massimo, C1-C3, P1-P3
    profilo  il gamma netto strike per strike
    oi_strike  l'open interest grezzo (call e put) sugli stessi strike, per i livelli che non
             si spostano col prezzo
    barre    le barre a 1 minuto della sessione cash, per misurare cosa ha fatto il prezzo
    live     ogni 5 minuti in sessione (solo SPX, 0DTE): spot, offset, flip, gamma massimo,
             C/P e straddle ATM. Il JSON grezzo qui non si tiene (2 MB a scatto)
    live_profilo  il gamma netto 0DTE di ogni scatto live
    oi_future (vista)  l'open interest per strike in prezzi ES/NQ
    livelli_future (vista)  i livelli gia' in prezzi ES/NQ; vuoti finche' l'offset non c'e'
"""
import argparse
import csv
import gzip
import io
import json
import sqlite3
from datetime import timedelta

import scarica_gex as g

SCHEMA = """
CREATE TABLE IF NOT EXISTS scatti (
    file TEXT PRIMARY KEY, indice TEXT NOT NULL, ts_utc TEXT NOT NULL, data_ny TEXT NOT NULL, ora_roma TEXT NOT NULL,
    spot REAL NOT NULL, offset REAL, nota_offset TEXT, in_sessione INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS livelli (
    file TEXT NOT NULL REFERENCES scatti(file), scadenza TEXT NOT NULL, tipo TEXT NOT NULL,
    regime TEXT, flip REAL, gamma_max REAL, put_max REAL,
    c1 REAL, c2 REAL, c3 REAL, p1 REAL, p2 REAL, p3 REAL, netto_totale REAL,
    PRIMARY KEY (file, scadenza));
CREATE TABLE IF NOT EXISTS profilo (
    file TEXT NOT NULL, scadenza TEXT NOT NULL, strike REAL NOT NULL,
    netto REAL, gex_call REAL, gex_put REAL,
    PRIMARY KEY (file, scadenza, strike));
CREATE TABLE IF NOT EXISTS oi_strike (
    file TEXT NOT NULL, scadenza TEXT NOT NULL, strike REAL NOT NULL,
    oi_call REAL, oi_put REAL,
    PRIMARY KEY (file, scadenza, strike));
CREATE TABLE IF NOT EXISTS barre (
    indice TEXT NOT NULL, ts_ny TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
    PRIMARY KEY (indice, ts_ny));
CREATE TABLE IF NOT EXISTS live (
    ts_utc TEXT NOT NULL, indice TEXT NOT NULL, data_ny TEXT NOT NULL, ora_ny TEXT NOT NULL,
    spot REAL NOT NULL, offset REAL, scadenza TEXT NOT NULL, regime TEXT, flip REAL, gamma_max REAL, put_max REAL,
    c1 REAL, c2 REAL, c3 REAL, p1 REAL, p2 REAL, p3 REAL, netto_totale REAL, straddle REAL, iv30 REAL,
    PRIMARY KEY (indice, ts_utc));
CREATE TABLE IF NOT EXISTS live_profilo (
    indice TEXT NOT NULL, ts_utc TEXT NOT NULL, strike REAL NOT NULL, netto REAL,
    PRIMARY KEY (indice, ts_utc, strike));
CREATE VIEW IF NOT EXISTS livelli_future AS
SELECT s.indice, s.data_ny, s.ora_roma, s.ts_utc, s.in_sessione, s.spot, s.offset, l.scadenza, l.tipo, l.regime,
       l.flip + s.offset AS flip, l.gamma_max + s.offset AS gamma_max, l.put_max + s.offset AS put_max,
       l.c1 + s.offset AS c1, l.c2 + s.offset AS c2, l.c3 + s.offset AS c3,
       l.p1 + s.offset AS p1, l.p2 + s.offset AS p2, l.p3 + s.offset AS p3,
       l.netto_totale, s.file
FROM livelli l JOIN scatti s USING (file);
CREATE VIEW IF NOT EXISTS oi_future AS
SELECT s.indice, s.data_ny, s.ora_roma, s.in_sessione, o.scadenza, o.strike + s.offset AS prezzo_future,
       o.oi_call, o.oi_put, o.oi_call + o.oi_put AS oi_totale, s.file
FROM oi_strike o JOIN scatti s USING (file);
"""


def apri() -> sqlite3.Connection:
    g.DATI.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(g.DATI / "gex.db", timeout=30)
    db.executescript(SCHEMA)
    return db


def registra(nome: str, payload: dict) -> None:
    """Scrive uno scarico nel database; rifarlo sullo stesso file lo sovrascrive."""
    try:
        an = g.analizza(payload)
        offset, nota = an["off"], an["nota_off"]
    except g.OffsetMancante:
        an = g.analizza(payload, offset=0.0)   # i livelli restano in strike SPX, offset vuoto
        offset, nota = None, "non disponibile"

    # la settimanale e' il primo venerdi' dalla 0DTE in poi
    venerdi = next((d for d in an["scadenze"] if d >= an["scad"] and d.weekday() == 4
                    and d <= an["scad"] + timedelta(days=7)), None)
    viste = [("0dte", an)]
    if venerdi and venerdi != an["scad"]:
        viste.append(("settimanale", g.analizza(payload, venerdi, an["off"])))

    ora_roma = an["ts_ny"].astimezone(g.ROMA)
    db = apri()
    with db:
        for t in ("oi_strike", "profilo", "livelli", "scatti"):
            db.execute(f"DELETE FROM {t} WHERE file = ?", (nome,))
        db.execute("INSERT INTO scatti VALUES (?,?,?,?,?,?,?,?,?)",
                   (nome, an["sym"], an["ts_cboe"], an["ts_ny"].date().isoformat(), f"{ora_roma:%Y-%m-%d %H:%M}",
                    an["spot"], offset, nota, int(g.in_sessione(an["ts_ny"]))))
        for tipo, v in viste:
            c = (v["c"] + [None] * 3)[:3]
            p = (v["p"] + [None] * 3)[:3]
            db.execute("INSERT INTO livelli VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (nome, v["scad"].isoformat(), tipo, v["regime"], v["flip"], v["gmax"], v["pmax"],
                        *c, *p, sum(r[1] for r in v["prof"])))
            db.executemany("INSERT INTO profilo VALUES (?,?,?,?,?,?)",
                           [(nome, v["scad"].isoformat(), k, n, gc, gp) for k, n, gc, gp in v["prof"]])
            db.executemany("INSERT INTO oi_strike VALUES (?,?,?,?,?)",
                           [(nome, v["scad"].isoformat(), k, oc, op)
                            for k, oc, op in g.oi_strike(v["ops"], v["scad"], v["spot"], v["ind"])])
    db.close()


def registra_live(an: dict, payload: dict) -> bool:
    """Scatto live (0DTE). Torna False se quel dato CBOE c'era gia' (la CBOE non si e' aggiornata)."""
    c = (an["c"] + [None] * 3)[:3]
    p = (an["p"] + [None] * 3)[:3]
    straddle = g.straddle_atm(an["ops"], an["scad"], an["spot"], an["ind"])
    db = apri()
    try:
        with db:
            if db.execute("SELECT 1 FROM live WHERE indice = ? AND ts_utc = ?", (an["sym"], an["ts_cboe"])).fetchone():
                return False
            db.execute("INSERT INTO live VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (an["ts_cboe"], an["sym"], an["ts_ny"].date().isoformat(), f"{an['ts_ny']:%H:%M}",
                        an["spot"], an["off"], an["scad"].isoformat(), an["regime"], an["flip"], an["gmax"], an["pmax"],
                        *c, *p, sum(r[1] for r in an["prof"]), straddle, g.num(payload["data"].get("iv30"))))
            db.executemany("INSERT INTO live_profilo VALUES (?,?,?,?)",
                           [(an["sym"], an["ts_cboe"], k, n) for k, n, _, _ in an["prof"]])
        return True
    finally:
        db.close()


def live_giorni(sym: str = "SPX") -> list[str]:
    db = apri()
    righe = [r[0] for r in db.execute("SELECT DISTINCT data_ny FROM live WHERE indice = ? ORDER BY data_ny DESC", (sym,))]
    db.close()
    return righe


def live_del_giorno(giorno: str, sym: str = "SPX") -> list[dict]:
    """Gli scatti live di un giorno, in ordine di tempo, con i profili."""
    db = apri()
    db.row_factory = sqlite3.Row
    righe = [dict(r) for r in db.execute("SELECT * FROM live WHERE indice = ? AND data_ny = ? ORDER BY ts_utc",
                                         (sym, giorno))]
    prof: dict[str, list] = {}
    for r in db.execute("""SELECT p.ts_utc, p.strike, p.netto FROM live_profilo p JOIN live l USING (indice, ts_utc)
                           WHERE l.indice = ? AND l.data_ny = ? ORDER BY p.strike""", (sym, giorno)):
        prof.setdefault(r[0], []).append((r[1], r[2]))
    db.close()
    for r in righe:
        r["prof"] = prof.get(r["ts_utc"], [])
    return righe


def replay_giorno(giorno: str, sym: str = "SPX") -> dict:
    """Dati per rivedere una giornata: scatti live (livelli in strike, con offset) e barre a 1 minuto."""
    db = apri()
    db.row_factory = sqlite3.Row
    live = [dict(r) for r in db.execute(
        """SELECT ts_utc, ora_ny, spot, offset, regime, flip, gamma_max, put_max, c1, c2, c3, p1, p2, p3,
                  netto_totale, straddle FROM live WHERE indice = ? AND data_ny = ? ORDER BY ts_utc""", (sym, giorno))]
    barre = [list(r) for r in db.execute(
        """SELECT substr(ts_ny, 12, 5), open, high, low, close FROM barre
           WHERE indice = ? AND substr(ts_ny, 1, 10) = ? ORDER BY ts_ny""", (sym, giorno))]
    db.close()
    return {"giorno": giorno, "live": live, "barre": barre}


def csv_live() -> str:
    db = apri()
    cur = db.execute("""SELECT indice, data_ny, ora_ny, ts_utc, spot, offset, scadenza, regime,
                               flip + offset AS flip, gamma_max + offset AS gamma_max, put_max + offset AS put_max,
                               c1 + offset AS c1, c2 + offset AS c2, c3 + offset AS c3,
                               p1 + offset AS p1, p2 + offset AS p2, p3 + offset AS p3, netto_totale, straddle, iv30
                        FROM live ORDER BY ts_utc""")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([d[0] for d in cur.description])
    w.writerows(cur)
    db.close()
    return out.getvalue()


def registra_barre(sym: str, payload: dict) -> None:
    righe = [(sym, b["datetime"], b["price"]["open"], b["price"]["high"], b["price"]["low"], b["price"]["close"])
             for b in payload["data"]]
    db = apri()
    with db:
        db.executemany("INSERT OR REPLACE INTO barre VALUES (?,?,?,?,?,?)", righe)
    db.close()


def salva_barre(sym: str) -> tuple[str, int]:
    """Scarica le barre dell'ultima sessione, le salva grezze e nel database."""
    payload = g.scarica_barre(sym)
    barre = payload["data"]
    if not barre:
        raise ValueError(f"nessuna barra per {sym}")
    giorno = barre[-1]["datetime"][:10]
    g.DATI.mkdir(parents=True, exist_ok=True)
    p = g.DATI / f"{giorno}_{sym}_barre.json.gz"
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(payload, f)
    tmp.replace(p)
    registra_barre(sym, payload)
    return giorno, len(barre)


def ricostruisci(tutto: bool = False) -> tuple[int, list[str]]:
    """Registra i file mancanti (o tutti). Torna quanti ne ha fatti e quelli falliti."""
    db = apri()
    gia = {r[0] for r in db.execute("SELECT file FROM scatti")}
    gia_barre = set(db.execute("SELECT DISTINCT indice, substr(ts_ny, 1, 10) FROM barre"))
    db.close()
    fatti, falliti = 0, []
    for sym in g.INDICI:
        for p in sorted(g.DATI.glob(f"*_{sym}.json.gz")):
            if p.name in gia and not tutto:
                continue
            try:
                registra(p.name, g.leggi_json(p))
                fatti += 1
            except Exception as e:
                falliti.append(f"{p.name}: {e}")
        for p in sorted(g.DATI.glob(f"*_{sym}_barre.json.gz")):
            if (sym, p.name[:10]) in gia_barre and not tutto:
                continue
            try:
                registra_barre(sym, g.leggi_json(p))
                fatti += 1
            except Exception as e:
                falliti.append(f"{p.name}: {e}")
    return fatti, falliti


def ultimi(n: int = 60) -> list[tuple]:
    db = apri()
    righe = db.execute("""SELECT indice, ora_roma, scadenza, tipo, regime, offset, flip, gamma_max,
                                 c1, c3, p1, p3
                          FROM livelli_future ORDER BY ts_utc DESC, indice, tipo LIMIT ?""", (n,)).fetchall()
    db.close()
    return righe


def csv_livelli() -> str:
    db = apri()
    cur = db.execute("SELECT * FROM livelli_future ORDER BY ts_utc, indice, tipo")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([d[0] for d in cur.description])
    w.writerows(cur)
    db.close()
    return out.getvalue()


def copia_db() -> bytes:
    db = apri()
    dati = db.serialize()
    db.close()
    return dati


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tutto", action="store_true", help="ricalcola anche gli scarichi gia' registrati")
    a = ap.parse_args()
    fatti, falliti = ricostruisci(a.tutto)
    print(f"registrati {fatti} scarichi in {g.DATI / 'gex.db'}")
    for r in falliti:
        print(f"  non riuscito: {r}")


if __name__ == "__main__":
    main()
