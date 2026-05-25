# Guida passo-passo: server MCP per oscilloscopio Tektronix TBS2204B via Ethernet (Windows 10 + PowerShell)

Questa guida ti porta dall'oscilloscopio "fresco di scatola" a un server MCP funzionante su **Windows 10**, usando **PowerShell** come shell. Tutti i comandi sono pensati per essere eseguiti in una finestra PowerShell standard (non serve una shell elevata, salvo dove indicato).

## 1. Architettura della soluzione

```
[Claude Desktop / client MCP]  <-- protocollo MCP -->  [Server MCP Python]
                                                              |
                                                       pyvisa + VISA
                                                              |
                                                    LAN / TCP-IP (LXI)
                                                              |
                                                  [Tektronix TBS2204B]
```

Il TBS2204B è un oscilloscopio LXI-compliant: parla **SCPI** (Standard Commands for Programmable Instruments) sopra TCP/IP. Il server MCP è un piccolo programma Python che:

1. Gestisce una sessione VISA verso lo strumento (apertura *lazy* alla prima invocazione, riconnessione automatica in caso di errore di rete).
2. Espone come *tool* MCP funzioni di alto livello (`identify`, `acquisition_state`, `set_acquisition`, `measure`, `get_waveform`, `reconnect`, `scpi_query`, `scpi_write`).
3. Riceve le richieste dal client MCP, traduce in SCPI, restituisce i risultati.

## 2. Prerequisiti

### Hardware e rete

- Oscilloscopio Tektronix TBS2204B con porta Ethernet posteriore.
- Cavo Ethernet tra oscilloscopio e rete (router/switch) raggiungibile dal PC Windows.
- PC Windows 10 con accesso amministratore (serve solo per installare Python e, se vuoi, NI-VISA).

### Software

- **Python 3.10 o superiore** (richiesto dall'SDK MCP).
- **Backend VISA**: la via più semplice è `pyvisa-py` (Python puro, niente installer pesanti). In alternativa puoi installare **NI-VISA** o **TekVISA**, che su Windows offrono migliore compatibilità con strumenti più datati e tool grafici utili (es. NI MAX).
- Pacchetti Python: `mcp[cli]`, `pyvisa`, `pyvisa-py`, `numpy`.

### Installazione di Python su Windows 10

Se non l'hai già installato:

1. Scarica l'installer da <https://www.python.org/downloads/windows/>.
2. **Importante**: nella prima schermata dell'installer spunta **"Add python.exe to PATH"** prima di cliccare *Install Now*.
3. Verifica in PowerShell:

   ```powershell
   python --version
   pip --version
   ```

   Se `python` non viene trovato, chiudi e riapri PowerShell (oppure riavvia Windows) per ricaricare il PATH.

### Permessi di esecuzione script PowerShell

Per attivare un virtualenv PowerShell deve poter eseguire script `.ps1`. Apri PowerShell **come amministratore** e una volta sola lancia:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Conferma con `S` (Sì). Da questo momento gli script firmati o creati in locale (come `Activate.ps1` del venv) gireranno senza errori.

## 3. Configurazione di rete dell'oscilloscopio

Sul TBS2204B:

1. Premi il tasto **Utility**.
2. Vai nel menu **I/O** → **Ethernet Network Settings**.
3. Imposta **DHCP ON** se hai un router che assegna gli indirizzi (consigliato per il primo collaudo), altrimenti configura manualmente IP, subnet mask e gateway.
4. Annota l'**indirizzo IP** che compare a schermo. In laboratorio Bioskin è configurato come **IP statico `192.168.0.75`**: useremo questo valore in tutti gli esempi della guida.
5. Da PowerShell verifica che lo strumento risponda al ping:

   ```powershell
   Test-Connection -ComputerName 192.168.0.75 -Count 4
   # oppure il classico
   ping 192.168.0.75
   ```

> Suggerimento: in produzione conviene assegnare un **IP statico** o una **DHCP reservation** sul router, così l'indirizzo non cambia tra una sessione e l'altra. Il TBS2204B di laboratorio è già configurato con IP statico.

### Firewall di Windows

Al primo collegamento Windows Defender potrebbe chiedere se autorizzare Python sulla rete. Spunta almeno **"Reti private"** e conferma. Se non vedi il prompt e i timeout persistono, controlla manualmente in *Windows Security → Firewall & network protection → Allow an app through firewall*.

## 4. Installazione dell'ambiente Python

Hai due opzioni equivalenti per la cartella di lavoro:

- **A. Stai usando il repo del laboratorio.** Entra nella sottocartella `tbs2204b/`: il file `server.py` è già lì.

  ```powershell
  cd "C:\Users\<tuo-utente>\..\Server-MCP-strumentazione\tbs2204b"
  ```

- **B. Parti da zero, senza repo.** Crea una cartella di lavoro nella tua home, e in seguito copierai `server.py` da quella del repo.

  ```powershell
  cd $HOME
  mkdir tbs2204b-mcp
  cd tbs2204b-mcp
  ```

Crea e attiva un virtualenv:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Il prompt diventa `(.venv) PS C:\Users\...>`. Aggiorna pip e installa le dipendenze:

```powershell
python -m pip install --upgrade pip
pip install "mcp[cli]" pyvisa pyvisa-py numpy
```

> Se preferisci NI-VISA o TekVISA, scaricali dal sito del rispettivo produttore e installali *prima* di lanciare lo script (richiede riavvio). `pyvisa` li userà automaticamente.

## 5. Test di connessione (prima del server MCP)

Prima di scrivere il server MCP, verifica che la comunicazione VISA funzioni. Crea il file `test_connessione.py` nella cartella di lavoro. Da PowerShell puoi farlo al volo con `notepad`:

```powershell
notepad test_connessione.py
```

Incolla questo contenuto e salva:

```python
import pyvisa

IP = "192.168.0.75"   # IP statico del TBS2204B in laboratorio
PORT = 4000           # porta SCPI raw socket del TBS2204B

# Trasporto SOCKET raw: funziona sia con pyvisa-py (Python puro) sia con
# NI-VISA / TekVISA, senza bisogno di suffissi di backend.
RESOURCE = f"TCPIP::{IP}::{PORT}::SOCKET"

rm = pyvisa.ResourceManager()
scope = rm.open_resource(RESOURCE)
scope.timeout = 10000           # ms
scope.read_termination = "\n"   # obbligatori in modalità SOCKET
scope.write_termination = "\n"

print("IDN:", scope.query("*IDN?"))
print("Acquisizione:", scope.query("ACQuire:STATE?"))

scope.close()
```

Esegui:

```powershell
python .\test_connessione.py
```

Se vedi una stringa tipo `TEKTRONIX,TBS2204B,...` la comunicazione funziona. Se ottieni un timeout, controlla IP, firewall, e che sul lato strumento l'interfaccia di rete sia abilitata.

> Nota: il TBS2204B espone SCPI sulla porta TCP 4000 (raw socket). Usiamo quella e non `TCPIP::IP::INSTR` perché `pyvisa-py` ha pieno supporto SOCKET senza richiedere NI-VISA, e il TBS2000 funziona affidabilmente su questo trasporto. Le due terminazioni di riga sono obbligatorie in modalità SOCKET: senza di esse `query` resta in attesa indefinita.

## 6. Comandi SCPI utili per il TBS2204B

Riferimento: *Tektronix TBS2000/TBS2000B Series Programmer Manual* (077-1149-xx). Estratto dei comandi più usati:

| Scopo | Comando SCPI |
|---|---|
| Identificazione strumento | `*IDN?` |
| Reset | `*RST` |
| Autoset | `AUTOSet EXECute` |
| Selezionare canale per waveform | `DATa:SOUrce CH1` |
| Formato dati waveform | `DATa:ENCdg RIBinary` |
| Larghezza byte per campione | `DATa:WIDth 1` (nativo ADC; vedi nota su `get_waveform` più sotto) |
| Range di campioni da scaricare | `DATa:STARt 1` / `DATa:STOP 100000` |
| Parametri di scala waveform | `WFMOutpre?` |
| Lettura della waveform | `CURVe?` |
| Misura automatica (es. ampiezza CH1) | `MEASUrement:IMMed:SOUrce CH1` + `MEASUrement:IMMed:TYPe AMPlitude` + `MEASUrement:IMMed:VALue?` |
| Stato acquisizione | `ACQuire:STATE?` |
| Avviare/fermare acquisizione | `ACQuire:STATE RUN` / `ACQuire:STATE STOP` |

Per ricostruire la waveform in volt/secondi servono i parametri `XINcr`, `XZEro`, `YMUlt`, `YOFf`, `YZEro`, ottenibili con `WFMOutpre:XINCR?`, ecc.

## 7. Struttura del server MCP

Il server usa **FastMCP**, l'API ad alto livello dell'SDK Python, e gira su `stdio`: sarà Claude Desktop ad avviarlo come sottoprocesso.

Il codice del server si trova nel file [`../server.py`](../server.py) (cioè `tbs2204b/server.py` rispetto alla radice del repo). **Non riportiamo qui l'intero sorgente** per evitare che guida e implementazione vadano fuori sincrono: il file è la fonte di verità. Di seguito spieghiamo l'architettura e i punti che è importante capire prima di usarlo o modificarlo.

### Configurazione (lette da variabili d'ambiente)

```python
SCOPE_IP   = os.environ.get("TBS2204B_IP",   "192.168.0.75")
SCOPE_PORT = int(os.environ.get("TBS2204B_PORT", "4000"))
RESOURCE   = f"TCPIP::{SCOPE_IP}::{SCOPE_PORT}::SOCKET"
```

I default sono già quelli del TBS2204B di laboratorio, quindi in produzione non è necessario impostare nulla; le variabili sono utili solo se vuoi puntare a un altro strumento o a una porta diversa.

### Gestione della sessione VISA: `ScopeConnection`

Il punto chiave del server è la classe `ScopeConnection`. A differenza dell'approccio "ingenuo" (`open_resource` nel lifespan, ricicla sempre la stessa sessione), questa classe:

1. **Apre la sessione in modo *lazy*** alla prima chiamata di un tool. Conseguenza: se all'avvio di Claude Desktop l'oscilloscopio è spento, il server MCP parte comunque e i tool falliranno con un messaggio chiaro solo quando vengono invocati.
2. **Riconnette automaticamente** in caso di `VisaIOError` o `OSError`: tipico scenario in cui il socket cache è morto perché lo strumento è stato spento e riacceso. La prima query fallisce, la sessione viene chiusa e riaperta, e la stessa operazione viene ritentata una volta. L'utente non si accorge di nulla.
3. Espone un metodo `call(fn)` che incapsula questa logica di retry: tutti i tool passano per esso (via la helper `_call(ctx, op)`).

Il timeout è impostato a **30 s** per dare margine a record d'onda lunghi via `CURVe?` su SOCKET.

### Tool esposti

| Tool | Cosa fa |
|---|---|
| `identify` | `*IDN?` — stringa identificativa dello strumento. |
| `acquisition_state` | Ritorna `RUN` o `STOP`. |
| `set_acquisition(run: bool)` | Avvia o ferma l'acquisizione. |
| `reconnect` | Forza chiusura+riapertura della sessione VISA. Normalmente non serve (il retry è automatico), utile per diagnostica o dopo aver cambiato IP. |
| `measure(channel, measurement)` | Misura automatica (`FREQ`, `AMPLITUDE`, `RMS`, `PK2PK`, ecc.). Whitelist in `ALLOWED_MEASUREMENTS`. |
| `get_waveform(channel, max_points=2000)` | Scarica la curva in binario, la converte in volt/secondi e — se necessario — la sottocampiona a `max_points` per non saturare il client. |
| `scpi_query(command)` | Query SCPI grezza (comandi che terminano con `?`). |
| `scpi_write(command)` | Comando SCPI di scrittura (senza `?`). |

### Nota su `get_waveform`: `DATa:WIDth 1`

Il TBS2000 ha un bug noto di byte order quando si usa `DATa:WIDth 2` (campioni a 16 bit). Per questo motivo `get_waveform` usa `DATa:WIDth 1` (nativo dell'ADC a 8 bit) con `datatype="b"` in `query_binary_values`: un byte per campione, niente ambiguità di endianness, e il trasferimento è anche più rapido. La precisione di 8 bit è quella reale dell'ADC dello strumento, quindi non si sta perdendo nulla rispetto al digitale acquisito.

### Se hai bisogno di modificare il server

Modifica direttamente `tbs2204b/server.py` nel repo: la guida non ne contiene una copia.

## 8. Test del server in locale

Prima di collegarlo a Claude, testalo con l'**MCP Inspector**. I default del server (`192.168.0.75:4000`) sono già quelli del laboratorio, quindi tipicamente non devi impostare nulla. Se vuoi puntare a un altro strumento o un'altra porta, esporta le variabili d'ambiente prima di lanciare:

```powershell
# Opzionale: solo se servono valori diversi dai default
$env:TBS2204B_IP   = "192.168.0.75"
$env:TBS2204B_PORT = "4000"

mcp dev ..\server.py
```

> Il comando va lanciato dalla cartella `tbs2204b/docs/` puntando al server con `..\server.py`, oppure direttamente da `tbs2204b/` con `mcp dev .\server.py`. Le variabili d'ambiente impostate con `$env:` valgono solo per la sessione PowerShell corrente. Se vuoi renderle permanenti per l'utente:
>
> ```powershell
> [Environment]::SetEnvironmentVariable("TBS2204B_IP", "192.168.0.75", "User")
> ```
>
> ma per l'uso con Claude Desktop conviene definirle direttamente nel `claude_desktop_config.json` (vedi sezione successiva).

L'Inspector apre un'interfaccia web (di solito su `http://localhost:5173`) dove vedi i tool elencati e puoi invocarli manualmente. Verifica che `identify` restituisca la stringa giusta e che `get_waveform` scarichi una curva sensata (con un segnale connesso a CH1).

Per uscire premi `Ctrl+C` nella finestra PowerShell.

## 9. Collegamento a Claude Desktop

Su Windows il file di configurazione di Claude Desktop si trova in `%APPDATA%\Claude\claude_desktop_config.json`. Da PowerShell aprilo con:

```powershell
notepad $env:APPDATA\Claude\claude_desktop_config.json
```

Se il file non esiste, Notepad chiederà se crearlo: rispondi sì.

Aggiungi (o estendi) la sezione `mcpServers` con i percorsi **assoluti** del tuo Python e del tuo `server.py`. Per ottenerli rapidamente:

```powershell
# Path assoluto del Python del venv
(Resolve-Path .\.venv\Scripts\python.exe).Path

# Path assoluto di server.py
(Resolve-Path .\server.py).Path
```

Esempio di configurazione (adatta i percorsi a quelli che ti escono dai comandi sopra):

```json
{
  "mcpServers": {
    "tbs2204b": {
      "command": "C:\\Users\\TUO_UTENTE\\tbs2204b-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\TUO_UTENTE\\tbs2204b-mcp\\server.py"],
      "env": {
        "TBS2204B_IP": "192.168.0.75",
        "TBS2204B_PORT": "4000"
      }
    }
  }
}
```

> Il blocco `env` è facoltativo: se ometti `TBS2204B_IP` e `TBS2204B_PORT` il server usa i default (`192.168.0.75` e `4000`), che sono già i valori del laboratorio.

> Attenzione: nel JSON i backslash di Windows vanno **raddoppiati** (`\\`). Salva il file e **riavvia Claude Desktop completamente** (chiudi anche dall'icona di systray, non solo la finestra). Nella barra dei tool dovresti vedere quelli del server `tbs2204b`.

A questo punto puoi chiedere a Claude cose come:

- "Identificami lo strumento e dimmi se sta acquisendo."
- "Misura la frequenza e l'ampiezza picco-picco su CH1."
- "Scarica la waveform di CH2 con 1000 punti e dimmi qual è il valore RMS calcolato."

## 10. Estensioni possibili

Quando la base funziona, valuta di aggiungere:

- **Screenshot** dello schermo dello strumento (`HARDCopy STARt` + lettura blocco binario PNG) e ritorno come `Image` MCP.
- **Trigger control**: tool dedicati per `TRIGger:A:LEVel`, `TRIGger:A:EDGE:SOUrce`, ecc.
- **Salvataggio CSV**: tool che invece di restituire la waveform inline la salva su disco e ritorna il path.
- **Cache**: per `WFMOutpre?` puoi leggere tutti i parametri con una singola query e fare il parsing locale, riducendo la latenza.

## 11. Troubleshooting rapido (Windows)

| Sintomo | Causa probabile | Soluzione |
|---|---|---|
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Execution policy restrittiva | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` (PowerShell admin) |
| `python` non riconosciuto | PATH non aggiornato dopo l'installazione | Riavvia PowerShell o reinstalla Python con la spunta "Add to PATH" |
| `VI_ERROR_TMO` al primo `*IDN?` | IP errato, porta sbagliata, firewall di Windows, scope non in rete | `Test-Connection 192.168.0.75`, controlla che la porta `4000` sia raggiungibile, verifica menu Utility → I/O. Ricorda che in modalità `SOCKET` mancano le terminazioni di riga se non impostate. |
| `pyvisa` non trova il backend | Manca sia NI-VISA sia `pyvisa-py` | `pip install pyvisa-py` (è già nei requisiti della sezione 4) |
| Il server parte ma il primo tool fallisce con `ConnectionError: Impossibile aprire la sessione VISA...` | Lo strumento è spento o IP/porta sbagliati. Il server è progettato per partire comunque e fallire solo all'invocazione. | Accendi lo strumento, oppure chiama il tool `reconnect` dopo aver corretto la configurazione. |
| Tool funzionava, poi inizia a dare timeout dopo che lo strumento è stato spento/riacceso | La cache della sessione punta a un socket morto | Nessuna azione: il server riapre la sessione automaticamente al tentativo successivo. Se persiste, chiama il tool `reconnect`. |
| Waveform "rumorosa" o tagliata | Acquisizione ferma o trigger non agganciato | Controlla `ACQuire:STATE?`, lancia `AUTOSet EXECute` |
| Valori delle misure tutti `9.9E37` | È il "not a number" SCPI: misura non valida (segnale assente) | Verifica probe e accoppiamento |
| Claude Desktop non vede i tool | Path errati o backslash non raddoppiati nel JSON | Usa `Resolve-Path`, raddoppia gli `\\`, riavvia Claude da systray |
| Errori di permessi su cartelle di sistema | Hai messo il progetto in `C:\Program Files\...` | Sposta il progetto in `C:\Users\TUO_UTENTE\...` |

---

Con questa base hai un MCP server pulito, estendibile e sufficientemente strutturato da gestire un TBS2204B in laboratorio da Windows 10. Da qui in avanti è solo questione di aggiungere tool man mano che ti servono nuove operazioni sullo strumento.
