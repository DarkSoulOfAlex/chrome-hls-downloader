# chrome-hls-downloader

Scarica il video HLS che stai guardando nel browser, anche quando il sito protegge lo
stream con token che scadono e controlli sugli header. Poi verifica ogni frame del
risultato.

[English](README.md)

```
chrome-hls-dl -o film.mp4
```

Si apre una finestra del browser. Vai sulla pagina, premi play e lascia andare il
video. Il programma trova lo stream, scarica i segmenti in parallelo con i token e gli
header del browser, li unisce in un MP4 e controlla che ogni frame video e audio si
decodifichi correttamente.

## Perché

Molti player scaricano i segmenti con URL tipo `seg-12.ts?t=…&e=…`, dove il token scade
dopo un paio di minuti, e il CDN rifiuta le richieste senza gli header `Origin`,
`Referer` o `Sec-Fetch-*` del browser. Se copi un URL in un downloader, funziona per un
minuto e poi dà `403`.

Questo programma non indovina niente. Osserva il browser tramite il Chrome DevTools
Protocol e ripete le richieste **esattamente come le manda il browser**. Quando il
player cambia token, il download passa a quello nuovo, e dopo un `403` aspetta il
successivo.

## Funzioni

- **Nessun codice specifico per un sito.** Playlist, token, cookie e header vengono
  letti dalle richieste vere del browser, in ogni frame: anche dagli iframe di altri
  siti che girano in un processo separato, e dai worker.
- **Token che cambiano.** Passa al token nuovo appena il player lo usa. Con
  `401/403/404/410` si mette in pausa finché il player non ne fornisce un altro. Se il
  token risulta legato al singolo segmento, ricarica la playlist.
- **Riprende da dove era rimasto.** I segmenti restano in `<output>.parts/`: basta
  rilanciare lo stesso comando.
- **Controllo di completezza.** Ogni segmento viene confrontato con il suo
  `Content-Length`, e i segmenti TS devono essere formati da pacchetti interi da 188
  byte con il byte di sincronismo corretto.
- **Verifica frame per frame** (serve ffmpeg), descritta qui sotto.

## Verifica

Parte da sola dopo il download, oppure a mano con `chrome-hls-dl verify FILE`:

1. **Autotest.** Una copia dei primi 30 secondi viene rovinata apposta, e deve dare più
   segnalazioni di ffmpeg dell'originale. Se ffmpeg sul tuo PC non rivelasse i difetti,
   la verifica si ferma invece di dare un falso "OK".
2. **Decodifica completa** di tutti i frame video e audio, a livello `-v warning`,
   perché ffmpeg segnala i frame rovinati come avvisi, non come errori. Ogni
   segnalazione viene annotata con il minuto.
3. **Controlli:**
   - frame decodificati uguali ai frame nel file;
   - nessun buco nella timeline video e in quella audio;
   - audio e video partono e finiscono insieme.
4. **Silenzio finale.** Alcune sorgenti aggiungono in fondo silenzio digitale con solo
   audio. Se c'è, viene segnalato come imbottitura e non come difetto, e viene creata
   una copia `*.trimmed.mp4` senza quella coda.

## Installazione

Servono Python ≥ 3.10, un browser basato su Chromium e, per l'MP4 e la verifica,
[ffmpeg](https://ffmpeg.org/) nel `PATH`.

```
pip install git+https://github.com/DarkSoulOfAlex/chrome-hls-downloader
```

Su Windows ffmpeg si installa con `winget install -e --id Gyan.FFmpeg`.

## Uso

```
chrome-hls-dl [-o OUTPUT] [opzioni]
chrome-hls-dl verify FILE [--no-trim]
```

| opzione | |
|---|---|
| `-o, --output` | file di uscita (default `video.mp4`) |
| `-j, --parallel` | download in parallelo (default 6). Di più non vuol dire più veloce: molti server limitano per IP |
| `--browser` | eseguibile del browser (default: Chrome, poi Edge, Brave, Chromium, Vivaldi) |
| `--port` | porta di debug (default 9222) |
| `--profile` | cartella del profilo del browser (separato da quello di tutti i giorni) |
| `--url` | pagina da aprire nel browser |
| `--workdir` | cartella dei segmenti (default `<output>.parts`) |
| `--wait` | rinuncia se nessun video parte entro N secondi |
| `--token-wait` | quanto aspettare un token nuovo (default 600 s) |
| `--no-verify`, `--no-trim`, `--keep-segments` | |

Consigli:

- **Tieni il video in riproduzione** e non ridurre la finestra a icona. Il player
  prende token nuovi solo mentre va, e i browser rallentano le pagine nascoste e mute.
- **Se il video stava già andando** quando hai avviato il programma, la playlist
  potrebbe essere stata caricata prima. Ricarica la pagina (F5) e premi play.
- **Il browser usa un profilo separato.** Le versioni recenti di Chrome non permettono
  il debug remoto sul profilo principale, quindi il tuo browser di tutti i giorni non
  viene toccato. Se il sito richiede il login, accedi nella nuova finestra.

## Browser supportati

Chrome, Edge, Brave, Chromium, Vivaldi, Opera: qualsiasi browser basato su Chromium.
Firefox e Safari no, perché non offrono il Chrome DevTools Protocol.

## Limiti

- **Solo HLS:** niente DASH né MP4 progressivi.
- **Non supportati:** stream cifrati (`EXT-X-KEY` AES-128 / SAMPLE-AES), DRM
  (Widevine, FairPlay, PlayReady) e playlist con byte-range. Il programma non aggira
  cifratura o DRM.
- **Live:** vengono scaricati solo i segmenti presenti nella playlist nel momento in
  cui è stata letta.
- **Qualità:** la copia è identica bit per bit a ciò che il sito trasmette. Gli
  artefatti di compressione della sorgente (fiamme ed esplosioni a quadretti, …)
  restano.

## Note legali

Usa questo programma solo per contenuti tuoi, di pubblico dominio o che hai il permesso
di scaricare. Sei responsabile del rispetto del diritto d'autore e dei termini di
servizio dei siti.

## Licenza

MIT
