# GEX SPX

Scatta la catena SPX dal CBOE e mostra profilo, livelli e struttura montata, in prezzi ES. Un solo servizio: sito e scheduler nello stesso processo, solo Python standard. Il codice gestisce anche NDX/NQ: gli scatti automatici sono spenti, si riaccendono con `GEX_INDICI=SPX,NDX`.

| Scatto (New York) | Di solito a Roma | Serve per |
|---|---|---|
| 02:30 | 8:30 | piano di giornata |
| 09:47 | 15:47 | primo dato dopo l'apertura; salva l'offset ES−SPX |
| 15:40 | 21:40 | gamma massimo verso la chiusura |
| 16:20 | 22:20 | barre a 1 minuto della sessione, per il test |

Solo dal lunedì al venerdì. Se uno scatto fallisce riprova ogni 3 minuti
per mezz'ora; le barre mancate si recuperano allo scatto del mattino dopo.
L'esito di ogni scatto è nella pagina, sotto "Scatti automatici". Nei giorni di borsa USA
chiusa gli scatti girano lo stesso e salvano la catena invariata.

## Live ogni 5 minuti

In sessione (9:45–16:15 a New York, di solito 15:45–22:15 a Roma) la catena SPX si riscarica
ogni 5 minuti. La pagina `/live` mostra:
- prezzo, flip, gamma massimo e put più forte durante la giornata, in prezzi ES;
- la fascia del movimento atteso: lo straddle ATM 0DTE del primo scatto attorno al prezzo di allora;
- il profilo adesso contro quello di 30 minuti prima;
- cosa è cambiato: il prezzo che passa dall'altra parte del flip, flip o gamma massimo che si spostano.

Il dato CBOE resta ritardato di 15 minuti. Il live serve a vedere come si muovono i livelli;
quelli da mettere su Deepcharts restano gli scatti del mattino e delle 15:47.
Del live si tengono solo i numeri (tabelle `live` e `live_profilo`, CSV su `/live.csv`), non il
JSON grezzo: sarebbero 2 MB ogni 5 minuti. `GEX_LIVE=0` lo spegne.

## Replay

La pagina `/replay` rivede una giornata già salvata (dall'11/11 in poi), minuto per minuto: candele ES
(SPX a 1 minuto più l'offset) e livelli del dato live, con play a 1x-120x, cursore, tasti ±1/±5 minuti
e, di default, i livelli in ritardo di 15 minuti come nel live vero. Accanto al grafico, regime e
distanza dal prezzo di ogni livello. Si apre su un orario con `?d=2026-11-18&t=10:26` (ora di New York).
Serve il giorno intero: scatti live e barre a 1 minuto, queste ultime salvate alle 22:20 Roma.
In locale: `GEX_DATI=cartella-con-gex.db GEX_LIVE=0 python3 app.py`, poi `http://127.0.0.1:8000/replay`.

## Storico

Ogni scatto finisce in due posti, nella stessa cartella dati:
- `*_SPX.json.gz`, `*_NDX.json.gz`, `*_barre.json.gz`: i dati grezzi, la fonte originale;
- `gex.db`: database SQLite con scatti, livelli e profilo per strike (0DTE e settimanale) e
  barre a 1 minuto. La vista `livelli_future` dà i livelli in prezzi ES e NQ.

La tabella `oi_strike` (e la vista `oi_future`, in prezzi ES) tiene l'open interest grezzo di call
e put sugli stessi 51 strike: serve a provare dei livelli che non si spostano col prezzo (call e
put wall), da confrontare con i livelli gamma. Dopo il deploy, `python3 storico.py --tutto`
la riempie anche per gli scatti già salvati sul volume.

Il database si ricalcola dai JSON con `python3 storico.py --tutto`, per esempio se cambiano
le regole dei livelli. Dal sito, la pagina Storico mostra gli ultimi scatti e permette di
scaricare il CSV e il database completo. Il volume Railway non è un backup: ogni tanto
scarica `gex.db` sul Mac.

## Deepcharts

Deepchart non ammette file o rete negli indicatori, quindi i livelli si incollano. Sul sito,
sotto i livelli, "Riga per Deepcharts" dà il testo da copiare; l'indicatore
`deepcharts/GexLevels.cs` lo disegna. Si fa alle 8:30 e dopo le 15:47.

Una volta sola, sul PC con Deepchart:
1. Installa il .NET 10 SDK e chiedi al supporto Deepchart il developer id.
2. Copia su quel PC la cartella `deepcharts` e, da lì, compila con il tuo id:

   ```bash
   dotnet build GexIndicators.csproj -p:DeepchartDevId=IL-TUO-ID
   ```

   La DLL finisce da sola in `Documents\Deepchart\Indicators`. Se Deepchart non è in
   `C:\Program Files\Volumetrica Trading\Deepchart\`, aggiungi `-p:DeepchartDir="D:\percorso"`.
   Se Windows gira su Parallels, Documenti di Windows è quello del Mac: la cartella vera è
   `C:\Mac\Home\Documents\Deepchart\Indicators`, mentre il build di default scrive in
   `C:\Users\<nome>\Documents`, che Deepchart non legge. Aggiungi
   `-p:OutputPath="C:\Mac\Home\Documents\Deepchart\Indicators\"` e riavvia Deepchart.
3. Sul grafico apri la lista indicatori, sezione Personal, e aggiungi "GEX - Livelli".

Il progetto segue quello ufficiale: https://github.com/VolumetricaTrading/deepchart-indicators-api

Due modi, nelle impostazioni dell'indicatore (sezione Profilo):
- predefinito: una linea per livello, con la sua etichetta;
- "Modo profilo": una barra orizzontale per strike (verde gamma netto positivo, rosso negativo),
  come il grafico del sito, con le etichette dei livelli accanto. Larghezza e spessore si
  regolano; se le barre si toccano o sono troppo sottili, cambia lo spessore.
La riga del sito contiene già il profilo (segmenti `prezzo;valore;B`), quindi vale per entrambi.

Ogni volta: incolla la riga nel parametro "Livelli". Se l'indicatore viene rifiutato, il log di
Deepchart indica la riga e il tipo non ammesso.

## In locale

```bash
python3 app.py
```

Poi apri http://127.0.0.1:8000. Senza `GEX_PASSWORD` ascolta solo su questo Mac.
Gli script da terminale `scarica_gex.py` e `struttura.py` funzionano ancora.

## Su Railway

1. Installa la CLI e collegati al tuo account:

   ```bash
   brew install railway
   ```

   ```bash
   railway login
   ```

2. Dalla cartella `gex_cboe`, crea il progetto e carica il codice:

   ```bash
   railway init
   ```

   ```bash
   railway up
   ```

3. Nel dashboard del servizio:
   - **Variables**: aggiungi `GEX_PASSWORD`. Senza, il servizio non parte.
     In alternativa `GEX_PUBBLICO=1` (e niente password): il sito è aperto a tutti ma di sola
     lettura, il pulsante "Scatta ora" sparisce e gli scatti li fa solo lo scheduler. Chiunque
     conosca l'indirizzo vede livelli, live e storico e può scaricare `gex.db`.
   - **Volume**: collegane uno con mount path `/data`. Senza, ogni deploy cancella gli
     scarichi, e la pagina lo segnala. Il percorso viene letto da solo.
   - **Settings → Networking**: genera il dominio pubblico.
4. Apri il sito: il browser chiede utente e password. L'utente può essere qualsiasi,
   conta la password. Premi **Scatta ora** per provare subito.

Il primo offset ES−SPX arriva con il primo scarico a mercato aperto. Prima di allora la
pagina chiede di inserirlo a mano.

Ogni scarico pesa circa 1,8 MB: con due indici e tre scatti al giorno sono circa 2,5 GB
l'anno, cioè meno di 500 MB per i due mesi di test (`../docs/test_gex_2mesi.md`).

Prima di fidarti dell'offset NQ−NDX controllalo sul grafico: è verificato solo su SPX.
