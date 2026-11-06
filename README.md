# GEX SPX e NDX

Scatta le catene SPX e NDX dal CBOE e mostra profilo, livelli e struttura montata, in prezzi ES e NQ. Un solo servizio: sito e scheduler nello stesso processo, solo Python standard.

| Scatto (New York) | Di solito a Roma | Serve per |
|---|---|---|
| 02:30 | 8:30 | piano di giornata |
| 09:47 | 15:47 | primo dato dopo l'apertura; salva l'offset ES−SPX |
| 15:40 | 21:40 | gamma massimo verso la chiusura |
| 16:20 | 22:20 | barre a 1 minuto della sessione, per il test |

Solo dal lunedì al venerdì, per i due indici. Se uno scatto fallisce riprova ogni 3 minuti
per mezz'ora; le barre mancate si recuperano allo scatto del mattino dopo.
L'esito di ogni scatto è nella pagina, sotto "Scatti automatici". Nei giorni di borsa USA
chiusa gli scatti girano lo stesso e salvano la catena invariata.

## Storico

Ogni scatto finisce in due posti, nella stessa cartella dati:
- `*_SPX.json.gz`, `*_NDX.json.gz`, `*_barre.json.gz`: i dati grezzi, la fonte originale;
- `gex.db`: database SQLite con scatti, livelli e profilo per strike (0DTE e settimanale) e
  barre a 1 minuto. La vista `livelli_future` dà i livelli in prezzi ES e NQ.

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
3. Sul grafico apri la lista indicatori, sezione Personal, e aggiungi "GEX - Livelli".

Il progetto segue quello ufficiale: https://github.com/VolumetricaTrading/deepchart-indicators-api

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
