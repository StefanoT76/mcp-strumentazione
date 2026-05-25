# Guida passo-passo: server MCP multi-strumento per banco HP (GPIB via Contec + KI-VISA su Windows)

Questa guida ti porta dalla scheda Contec "fresca di installazione" a un server MCP funzionante su **PC-LAB Windows**, che espone in un'unica istanza i quattro strumenti del banco HP collegati sullo stesso bus GPIB. Tutti i comandi sono pensati per **PowerShell**.

## 1. Architettura della soluzione

```
[Claude Desktop su PC-CLIENT]
            |
            |   protocollo MCP via Streamable HTTP (porta 8000)
            v
   [PC-LAB Windows: server MCP Python]
            |
            |   pyvisa + KI-VISA
            v
   [scheda GPIB Contec PCI/USB]
            |
            |   bus IEEE-488
            +---- HP 6632A   (PSU,     addr 5)
            +---- HP 6060B   (Load,    addr 2)
            +---- HP 5334B   (Counter, addr 3)
            +---- HP 3457A   (DMM,     addr 22)
```

Il server MCP è un singolo processo Python che mantiene una sessione VISA aperta su ognuno dei quattro strumenti e li espone con tool MCP "parlanti" (`psu_set_voltage`, `load_measure_current`, `counter_measure_frequency`, `raw_query`, ...) invece di comandi SCPI/HP grezzi.

A differenza del server `tbs2204b/` (che usa stdio), questo server gira in **Streamable HTTP** su PC-LAB e viene raggiunto via rete da Claude Desktop installato su un PC-CLIENT diverso, perché la scheda GPIB Contec vive fisicamente sul PC del laboratorio.

## 2. Prerequisiti

### Hardware

- PC-LAB Windows con scheda GPIB **Contec** (PCI o USB) e driver di sistema già installati (`CTSTGPIB.EXE` deve vedere la scheda).
- I quattro strumenti collegati al bus GPIB, con indirizzi **distinti** sul pannello frontale.
- PC-CLIENT con Claude Desktop, sulla stessa LAN di PC-LAB.

### Software su PC-LAB

- **Python 3.10 o superiore** (richiesto dall'SDK MCP).
- **KI-VISA** 64 bit (Keysight IO Libraries). **Non** usare `pyvisa-py`: è Python puro e non parla con Contec; pyvisa deve usare la VISA di sistema.
- Pacchetti Python: `mcp[cli]`, `pyvisa`, `numpy<2`, `uvicorn`.

> Vincolo `numpy<2`: NumPy 2.x richiede istruzioni CPU **X86_V2** (SSE4.x, POPCNT, ...) introdotte da Intel Nehalem (2008). PC-LAB più vecchi (Core 2, Atom di prima generazione, Pentium D) ricevono un `RuntimeError` all'import. NumPy 1.26 funziona ovunque. `pyvisa` importa numpy a livello modulo anche se non lo usiamo direttamente, quindi è una dipendenza obbligata.

### Permessi di esecuzione script PowerShell

Per attivare un virtualenv PowerShell deve poter eseguire script `.ps1`. Apri PowerShell **come amministratore** una volta sola e lancia:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

## 3. Indirizzi GPIB degli strumenti

Prima di proseguire, verifica e annota l'indirizzo GPIB di ogni strumento dal suo pannello frontale. Valori tipici da fabbrica e tasti/menu per cambiarli:

| Strumento | Default di fabbrica | Tasto/menu | Indirizzo usato in laboratorio |
|---|---|---|---|
| HP 6632A (PSU) | 5 | tasto **Address** | **5** |
| HP 6060B (Load) | 5 | menu **Address** sotto Local/Remote | **2** |
| HP 5334B (Counter) | 18 | tasto **GP-IB Adrs** | **3** |
| HP 3457A (DMM) | 22 | tasto **ADDRESS** | **22** |

Gli indirizzi devono essere **diversi** tra loro. Quelli della colonna di destra sono i default del codice del server (`hp-lab/server.py`); puoi sovrascriverli con variabili d'ambiente al lancio (vedi sezione 8).

> Sul **HP 6060B**: se nel test della sezione 5 non risponde a `*IDN?`, probabilmente è in **Compatibility mode** invece che SCPI. Sul pannello frontale, menu GPIB → `LANG = SCPI`, salva, riavvia lo strumento.

## 4. Installazione dell'ambiente Python su PC-LAB

Hai due opzioni equivalenti per la cartella di lavoro:

- **A. Stai usando il repo del laboratorio.** Entra nella sottocartella `hp-lab/`: i file `server.py` e `test_strumenti.py` sono già lì.

  ```powershell
  cd "C:\Users\<tuo-utente>\..\Server-MCP-strumentazione\hp-lab"
  ```

- **B. Parti da zero, senza repo.** Crea una cartella di lavoro nella tua home, e in seguito copierai `server.py` e `test_strumenti.py` da quella del repo.

  ```powershell
  cd $HOME
  mkdir mcp-gpib
  cd mcp-gpib
  ```

Crea e attiva un virtualenv:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Il prompt diventa `(.venv) PS C:\Users\...>`. Aggiorna pip e installa le dipendenze:

```powershell
python -m pip install --upgrade pip
pip install "mcp[cli]" pyvisa "numpy<2" uvicorn
```

## 5. Test di connessione (prima del server MCP)

Prima di lanciare il server, verifica che ogni strumento risponda. Nel repo c'è già `test_strumenti.py` allo stesso livello di `server.py`: gestisce il bug di **state retention** del driver Contec (la prima `open_resource` dopo una sessione precedente a volte fallisce con `INV_OBJECT`) ritentando con un Resource Manager appena creato.

Lo script di default testa **PSU, Load e Counter**. Per includere anche il DMM, aggiungi una voce al dizionario `INSTRUMENTS`:

```python
INSTRUMENTS = {
    "HP 6632A (PSU)":     {"addr": 5,  "id_cmd": "ID?"},
    "HP 6060B (Load)":    {"addr": 2,  "id_cmd": "*IDN?"},
    "HP 5334B (Counter)": {"addr": 3,  "id_cmd": "ID"},   # nota: 'ID' SENZA '?'
    "HP 3457A (DMM)":     {"addr": 22, "id_cmd": "ID?"},  # 'ID?' CON '?'
}
```

Adatta gli indirizzi se sui tuoi strumenti sono diversi. Esegui:

```powershell
python .\test_strumenti.py
```

**Output atteso:** quattro righe `OK -> ...` con le stringhe di identificazione (`HP6632A`, `HEWLETT-PACKARD,6060B,...`, `HP5334B`, `HP3457A`).

Se un singolo strumento fallisce ma gli altri rispondono, controlla:

- indirizzo GPIB sul pannello (potrebbe essere diverso da quello in `INSTRUMENTS`);
- cavi GPIB e alimentazione (acceso);
- per il 6060B, che sia in lingua **SCPI** (vedi sezione 3).

> **Attenzione ai comandi di identificazione**, non sono uniformi:
> - HP 6632A → `ID?` (con `?`)
> - HP 3457A → `ID?` (con `?`)
> - HP 5334B → `ID` (**senza** `?`, dialetto pre-SCPI antico)
> - HP 6060B → `*IDN?` (SCPI standard)

Quando i quattro rispondono, sei pronto per il server.

## 6. Comandi utili per i quattro strumenti

Riferimenti: *HP 6632A Operating Manual* (Table 6-1), *HP 5334B Operation & Programming Manual* (Table 3-12 e sez. 3-333), *HP 3457A Quick Reference*, *HP 6060B Programming Guide*.

### HP 6632A — Power Supply (pre-SCPI)

| Scopo | Comando |
|---|---|
| Identificazione | `ID?` → `HP6632A` |
| Setpoint tensione | `VSET <V>` (range 0..20.475 V) |
| Setpoint corrente | `ISET <A>` (range 0.02..5.1188 A; **minimo NON è 0**) |
| OVP | `OVSET <V>` (0..22 V) |
| Uscita | `OUT 0` / `OUT 1` |
| Misure | `VOUT?`, `IOUT?` |
| Stato / errori | `STS?`, `FAULT?`, `ERR?`, `TEST?` |
| Reset protezioni | `RST` (riarma OVP/OCP) |
| Reset completo | `CLR` |

> **Limite del 6632A**: i setpoint **non sono leggibili** (nessun `VSET?`, `ISET?`, `OUT?`). Per sapere la tensione/corrente reale si usano `VOUT?`/`IOUT?`. Lo stato dell'uscita si deduce solo dal registro di stato.

### HP 6060B — Electronic Load (SCPI)

| Scopo | Comando |
|---|---|
| Identificazione | `*IDN?` |
| Modo operativo | `MODE CURR` / `MODE VOLT` / `MODE RES` |
| Setpoint | `CURR <A>`, `VOLT <V>`, `RES <Ω>` |
| Range corrente | `CURR:RANG HIGH` (0..60 A) / `CURR:RANG LOW` (0..6 A) |
| Ingresso | `INP ON` / `INP OFF` |
| Misure | `MEAS:VOLT?`, `MEAS:CURR?`, `MEAS:POW?` |
| Errori | `SYST:ERR?` |
| Reset | `*RST`, `*CLS` |

### HP 5334B — Universal Counter (pre-SCPI)

Terminatori **CR/LF**. NON c'è un comando "leggi misura": il 5334B emette **continuamente**, dopo aver impostato la funzione si fa `read()` sul bus.

| Scopo | Comando |
|---|---|
| Identificazione | `ID` (**senza** `?`) → `HP5334B` |
| Funzioni FN1..FN15 | `FN1`=Freq A, `FN2`=Freq B, `FN3`=Freq C, `FN4`=Period A, `FN5`=Time int. A→B, `FN7`=Ratio A/B, `FN10`=Pulse Width A, `FN11`=Rise/Fall Time A, `FN12`=DVM, `FN13`=Trig levels, `FN14/15`=Peaks A/B |
| Gate time | `GA<n>` (0.001..99.999 s) |
| Autotrigger | `AU1` / `AU0` |
| Canale A: coupling, impedenza, attenuazione, slope, filtro | `AA0/1`, `AZ0/1`, `AX0/1`, `AS0/1`, `FI0/1` |
| Canale B: coupling, impedenza | `BA0/1`, `BZ0/1` |
| Initialize (NON è una lettura!) | `IN` |
| Errore / calibrazione | `TE`, `TC` |
| Reset | `RE` |

> Trappola classica: `IN` significa **Initialize** (stato power-on), **non** "Input/read". Per leggere una misura si fa `inst.read()` (o `query()` dopo aver impostato la funzione, dato che il 5334B continua a emettere campioni).

> Formato risposta: `<ALPHA><spazi>±<digit>.<...>E±<2 digits>CR/LF`. Il carattere `ALPHA` iniziale indica il tipo: `F`=Frequenza, `S`=Tempo, `V`=Tensione, `R`=Ratio, `T`/`t`=Totalize, `A`/`B`=Trigger Level, `H`=Peaks.

### HP 3457A — Digital Multimeter 6.5 digit (pre-SCPI)

Terminatori: **CR/LF** in ricezione, `\n` in trasmissione. Header sempre in MAIUSCOLO, parametri liberi. `;` separa comandi multipli su una sola linea.

| Scopo | Comando |
|---|---|
| Identificazione | `ID?` (con `?`, come 6632A) |
| Funzioni di misura | `DCV [range]`, `ACV`, `DCI`, `ACI`, `OHM`, `OHMF` (4 fili), `FREQ`, `PER` |
| Trigger | `TARM AUTO; NRDGS 1,AUTO; TRIG HOLD` poi `TRIG SGL` per misura singola |
| Formato output | `OFORMAT ASCII` |
| Integrazione | `NPLC <n>` (1..100) |
| Self-test | `TEST` |

Il server applica all'avvio `TARM AUTO; NRDGS 1,AUTO; TRIG HOLD; OFORMAT ASCII`: nessuna misura parte automaticamente; per ottenere una lettura fresca si fa `"<FUNC>; TRIG SGL"` poi `read()`.

## 7. Struttura del server MCP

Il server usa **FastMCP** in modalità Streamable HTTP. Il codice si trova in [`../server.py`](../server.py) (cioè `hp-lab/server.py` rispetto alla radice del repo). **Non riportiamo qui l'intero sorgente** per evitare che guida e implementazione vadano fuori sincrono: il file è la fonte di verità. Di seguito spieghiamo l'architettura e i punti importanti da conoscere prima di usarlo o modificarlo.

### Configurazione (variabili d'ambiente)

```python
GPIB_BOARD   = os.environ.get("GPIB_BOARD",   "0")
PSU_ADDR     = os.environ.get("PSU_ADDR",     "5")    # HP 6632A
COUNTER_ADDR = os.environ.get("COUNTER_ADDR", "3")    # HP 5334B
DMM_ADDR     = os.environ.get("DMM_ADDR",     "22")   # HP 3457A
LOAD_ADDR    = os.environ.get("LOAD_ADDR",    "2")    # HP 6060B
HTTP_HOST    = os.environ.get("MCP_HOST",     "0.0.0.0")
HTTP_PORT    = int(os.environ.get("MCP_PORT", "8000"))
MCP_TOKEN    = os.environ.get("MCP_TOKEN")            # opzionale, bearer auth
MCP_LOG_DIR  = os.environ.get("MCP_LOG_DIR",  <dir di server.py>)
```

I default sono già quelli del banco di laboratorio, quindi in produzione tipicamente non è necessario impostare nulla. `MCP_TOKEN` abilita l'autenticazione bearer (vedi sezione 12).

### Sessioni VISA: apertura tollerante in `lab_lifespan`

All'avvio del processo, `_open()` apre ogni strumento con **fino a 3 tentativi** (gestisce il bug di state retention del driver Contec). Se uno strumento non risponde, il server **parte ugualmente** con gli altri tre: i tool relativi al mancante alzano `RuntimeError` con messaggio diagnostico solo quando vengono effettivamente chiamati. Ogni strumento usa i propri terminatori e timeout dichiarati in `SPECS` (in particolare il counter ha timeout **15 s** perché gate time lunghi possono richiedere secondi per una singola misura).

Sul DMM, dopo l'apertura, viene applicata la sequenza `TARM AUTO; NRDGS 1,AUTO; TRIG HOLD; OFORMAT ASCII` così che ogni misura successiva sia "fresca" e parta solo su `TRIG SGL`.

### Logging su file

Il server scrive su console **e** su `server.log` accanto a `server.py` (RotatingFileHandler, max 5 MB × 5 file). Utile per audit di laboratorio e per analizzare a posteriori cosa ha mandato lo strumento. Il path si può cambiare con `MCP_LOG_DIR`.

### Tool esposti

#### Generici / introspezione

| Tool | Cosa fa |
|---|---|
| `list_instruments` | Elenca i 4 strumenti, indirizzo GPIB e `connected: bool`. |
| `raw_query(target, command)` | Query grezza. `target` ∈ `psu`/`counter`/`dmm`/`load`. **Unico modo per interrogare il DMM** (non ha tool dedicati). |
| `raw_write(target, command)` | Scrittura grezza. |
| `raw_read(target)` | `read()` puro dal bus senza inviare comandi (utile per il 5334B che emette in continuo). |

#### HP 6632A — PSU

| Tool | Cosa fa |
|---|---|
| `psu_identify` | `ID?` → `HP6632A`. |
| `psu_set_voltage(volts)` | `VSET` (0..20.475 V). |
| `psu_set_current_limit(amps)` | `ISET` (0.02..5.1188 A). |
| `psu_set_overvoltage(volts)` | `OVSET` (0..22 V). |
| `psu_set_overcurrent_protect(enabled)` | `OCP 0/1`. |
| `psu_output(on)` | `OUT 0/1`. |
| `psu_read_voltage` / `psu_read_current` | `VOUT?` / `IOUT?` come float. |
| `psu_measurements` | Riassunto: vout, iout, potenza. |
| `psu_status_register` / `psu_fault_register` | `STS?` / `FAULT?` decodificati per bit (CV, +CC, -CC, OVP, OCP, OT, foldback, ...). |
| `psu_read_error` | `ERR?` con messaggio leggibile per codice. |
| `psu_reset_protection` | `RST`: riarma OVP/OCP dopo intervento. |
| `psu_reset_to_defaults` | `CLR` + Device Clear: reset completo allo stato power-on. |
| `psu_self_test` | `TEST?`. |
| `psu_display(on)` | Accende/spegne il display frontale. |
| `psu_ramp_voltage(v_start, v_stop, v_step, dwell_s, measure)` | Rampa lineare (anche in discesa). Eseguita interamente lato server: i `time.sleep()` avvengono su PC-LAB, quindi il timing è preciso e indipendente dalla latenza MCP. Se `measure=True`, registra VOUT/IOUT ad ogni step. |
| `psu_ramp_with_dmm(v_start, v_stop, v_step, dwell_s, dmm_max_volts)` | Come sopra, ma ad ogni step legge anche il DMM 3457A (più accurato; può essere collegato in 4-wire ai morsetti del carico per evitare caduta sui cavi PSU). |

#### HP 6060B — Load

| Tool | Cosa fa |
|---|---|
| `load_identify` | `*IDN?`. |
| `load_set_mode(mode)` | `CURR`/`VOLT`/`RES`. |
| `load_set_current(amps)` | 0..60 A. |
| `load_set_voltage(volts)` | 0..60 V. |
| `load_set_resistance(ohms)` | 0.033..10000 Ω. |
| `load_set_current_range(high_range)` | HIGH=0..60 A, LOW=0..6 A (miglior risoluzione). |
| `load_input(on)` | `INP ON/OFF`. |
| `load_measure_voltage/current/power` | `MEAS:VOLT?`/`MEAS:CURR?`/`MEAS:POW?`. |
| `load_status` | Riassume modo, ingresso, V/I/P. |
| `load_reset` | `*RST` + `*CLS` + Device Clear. |
| `load_errors(max_errors=10)` | Drena la coda `SYST:ERR?` fino al `0,"No error"`. |

#### HP 5334B — Counter

| Tool | Cosa fa |
|---|---|
| `counter_identify` | `ID` (senza `?`). |
| `counter_set_function(function)` | Tra `FREQ_A`, `FREQ_B`, `FREQ_C`, `PERIOD_A`, `TIME_INTERVAL_AB`, `TIME_INTERVAL_AB_D`, `RATIO_AB`, `TOTALIZE_START_A`, `TOTALIZE_STOP_A`, `PULSE_WIDTH_A`, `RISE_FALL_TIME_A`, `DVM`, `READ_TRIG_LEVELS`, `READ_PEAKS_A`, `READ_PEAKS_B`. |
| `counter_set_gate_time(seconds)` | `GA` (0.001..99.999 s). |
| `counter_autotrigger(on)` | `AU1`/`AU0`. |
| `counter_read` | `read()` puro, parsato in `{raw, alpha, value, unit_hint}`. |
| `counter_measure_frequency(channel)` | Helper per FN1/FN2/FN3 + lettura. |
| `counter_measure_period` / `counter_measure_time_interval` / `counter_measure_ratio_ab` / `counter_measure_dc_voltage` | Wrapper sulle funzioni più comuni. |
| `counter_set_input_a_coupling/impedance_50ohm/attenuation_x10/slope/filter` | Configurazione canale A. |
| `counter_set_input_b_coupling/impedance_50ohm` | Configurazione canale B. |
| `counter_initialize` | `IN`: stato power-on. |
| `counter_reset` | `RE` + Device Clear. |
| `counter_read_error` / `counter_transmit_calibration` | `TE` / `TC`. |

#### HP 3457A — DMM

Il DMM **non ha tool dedicati**: si interroga via `raw_query("dmm", ...)` / `raw_write("dmm", ...)` / `raw_read("dmm")`. Il pattern standard per una misura singola è:

```python
raw_write("dmm", "DCV 30;TRIG SGL")   # range 30 V, scatta UNA misura
raw_read("dmm")                       # legge il risultato
```

Sostituisci `DCV` con `ACV`, `DCI`, `ACI`, `OHM`, `OHMF`, `FREQ`, `PER` per altre misure. Il DMM è anche usato internamente da `psu_ramp_with_dmm`.

### Autenticazione opzionale

Se `MCP_TOKEN` è impostata, un middleware ASGI (`BearerAuthMiddleware`) richiede `Authorization: Bearer <token>` su ogni richiesta HTTP; senza header valido risponde 401.

### Se hai bisogno di modificare il server

Modifica direttamente `hp-lab/server.py` nel repo: la guida non ne contiene una copia.

## 8. Avvio del server su PC-LAB

Dalla cartella `hp-lab` con il venv attivo:

```powershell
# Opzionali: solo se servono valori diversi dai default
$env:GPIB_BOARD   = "0"
$env:PSU_ADDR     = "5"
$env:LOAD_ADDR    = "2"
$env:COUNTER_ADDR = "3"
$env:DMM_ADDR     = "22"
$env:MCP_HOST     = "0.0.0.0"
$env:MCP_PORT     = "8000"
# Opzionale: abilita autenticazione bearer
# $env:MCP_TOKEN  = "il-tuo-token-segreto"

python .\server.py
```

All'avvio nei log dovresti vedere qualcosa come:

```
... INFO mcp-gpib: Logging su file: C:\...\hp-lab\server.log
... INFO mcp-gpib: VISA in uso: <KI-VISA ...>
... INFO mcp-gpib: OK HP 6632A @ GPIB0::5::INSTR  -> HP6632A
... INFO mcp-gpib: OK HP 5334B @ GPIB0::3::INSTR  -> HP5334B
... INFO mcp-gpib: OK HP 3457A @ GPIB0::22::INSTR -> HP3457A
... INFO mcp-gpib: OK HP 6060B @ GPIB0::2::INSTR  -> HEWLETT-PACKARD,6060B,...
... INFO mcp-gpib: DMM configurato per misure singole su trigger
... INFO mcp-gpib: Autenticazione: DISABILITATA (nessun MCP_TOKEN)
... INFO:     Uvicorn running on http://0.0.0.0:8000
```

Se uno strumento è offline vedrai `ERROR ... Impossibile aprire ...` solo per quello: il server parte ugualmente con i restanti, e i tool relativi alzano errore esplicativo quando chiamati.

Per fermare: `Ctrl+C`.

## 9. Configurazione del firewall su PC-LAB

Una sola volta, da PowerShell come amministratore:

```powershell
New-NetFirewallRule -DisplayName "MCP GPIB Server" `
  -Direction Inbound -Protocol TCP -LocalPort 8000 `
  -Action Allow -Profile Private
```

Trova e annota l'IP di PC-LAB:

```powershell
ipconfig | Select-String "IPv4"
```

## 10. Test di rete da PC-CLIENT

Sostituisci l'IP con quello di PC-LAB:

```powershell
Test-NetConnection -ComputerName 192.168.1.50 -Port 8000
```

Devi vedere `TcpTestSucceeded : True`.

## 11. Collegamento a Claude Desktop su PC-CLIENT

Installa Node.js LTS da <https://nodejs.org>. Verifica:

```powershell
node --version
npx --version
```

Apri la configurazione di Claude Desktop:

```powershell
notepad $env:APPDATA\Claude\claude_desktop_config.json
```

Inserisci (adatta IP e, se abilitato, token):

```json
{
  "mcpServers": {
    "hp-lab": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://192.168.1.50:8000/mcp",
        "--allow-http",
        "--transport", "http-only"
      ]
    }
  }
}
```

Se hai abilitato il bearer token, aggiungilo:

```json
{
  "mcpServers": {
    "hp-lab": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://192.168.1.50:8000/mcp",
        "--allow-http",
        "--transport", "http-only",
        "--header", "Authorization: Bearer ${MCP_TOKEN}"
      ],
      "env": {
        "MCP_TOKEN": "il-tuo-token-segreto"
      }
    }
  }
}
```

Salva e **riavvia Claude Desktop completamente** (anche dall'icona della systray). Nella barra dei tool dovresti vedere quelli del server `hp-lab`.

A questo punto puoi chiedere a Claude cose come:

- "Elenca gli strumenti del banco HP e dimmi quali sono connessi."
- "Imposta il PSU a 5 V con limite 100 mA, accendi l'uscita, poi misura tensione e corrente effettive."
- "Configura il carico in modo corrente costante a 50 mA, accendi l'ingresso, e dimmi quanta potenza sta dissipando."
- "Misura la frequenza sul canale A del contatore con gate time 1 s."
- "Fai una rampa di tensione del PSU da 0 a 10 V con passi da 0.5 V e dwell 200 ms, registrando ad ogni step la tensione misurata col DMM in range 30 V."
- "Leggi la tensione DC dal DMM (range 30 V)." → Claude userà `raw_write("dmm", "DCV 30;TRIG SGL")` + `raw_read("dmm")`.

Per debug puoi anche aprire l'MCP Inspector da PC-CLIENT:

```powershell
npx @modelcontextprotocol/inspector
```

Trasporto: **Streamable HTTP**, URL `http://192.168.1.50:8000/mcp`.

## 12. Note di sicurezza

Senza `MCP_TOKEN`, chiunque sulla LAN che raggiunga la porta 8000 può comandare gli strumenti. In LAN di laboratorio chiusa è generalmente accettabile, ma il PSU può erogare 100 W e il carico ne può dissipare 300: una connessione non autorizzata può fare danni fisici.

Opzioni in ordine di sforzo:

**1. Bind solo sull'interfaccia giusta.** Se PC-LAB ha più schede di rete:

```powershell
$env:MCP_HOST = "192.168.1.50"   # IP della scheda LAN strumenti
```

**2. Attiva il bearer token.** Genera un token casuale e configuralo su server e client:

```powershell
# Su PC-LAB
$env:MCP_TOKEN = (-join ((48..57) + (97..122) | Get-Random -Count 32 | % {[char]$_}))
$env:MCP_TOKEN   # stampalo, ti serve per Claude Desktop
```

Poi configura Claude come mostrato nella sezione 11 col blocco `env`.

**3. Reverse proxy con TLS.** Per uso fuori dalla LAN di laboratorio, metti davanti Caddy o nginx con certificato (anche self-signed) e togli `--allow-http`.

## 13. Avvio automatico come servizio Windows (NSSM)

Tenere aperta una finestra PowerShell sul PC-LAB non è pratico. Registra il server come servizio Windows con **NSSM**:

1. Scarica NSSM 64 bit da <https://nssm.cc/download>.
2. Estrai `nssm.exe` in `C:\Tools\`.
3. PowerShell come amministratore:

   ```powershell
   C:\Tools\nssm.exe install MCPGpibServer
   ```

4. Nella GUI configura (sostituisci `TUO_UTENTE` e il path effettivo):

   - **Application Path**: `C:\Users\TUO_UTENTE\...\hp-lab\.venv\Scripts\python.exe`
   - **Startup directory**: `C:\Users\TUO_UTENTE\...\hp-lab`
   - **Arguments**: `server.py`

5. Tab **Environment** (una variabile per riga):

   ```
   GPIB_BOARD=0
   PSU_ADDR=5
   LOAD_ADDR=2
   COUNTER_ADDR=3
   DMM_ADDR=22
   MCP_HOST=0.0.0.0
   MCP_PORT=8000
   ```

   (e `MCP_TOKEN=...` se vuoi attivare l'autenticazione)

6. Tab **I/O** (opzionale, dato che il server scrive già su `server.log` per conto suo):

   - **Output (stdout)**: `C:\Users\TUO_UTENTE\...\hp-lab\nssm-stdout.log`
   - **Error (stderr)**: `C:\Users\TUO_UTENTE\...\hp-lab\nssm-stderr.log`

7. **Install service**, poi:

   ```powershell
   Start-Service MCPGpibServer
   Get-Service MCPGpibServer
   ```

## 14. Troubleshooting

| Sintomo | Causa probabile | Soluzione |
|---|---|---|
| Tutti gli strumenti `Impossibile aprire` all'avvio | KI-VISA non vede la scheda Contec | Riesegui `CTSTGPIB.EXE`, ricontrolla che KI-VISA sia 64 bit |
| Solo HP 6060B fallisce | Carico in Compatibility mode invece di SCPI | Pannello frontale → menu GPIB → `LANG = SCPI`, salva, riavvia |
| `*IDN?` su 6060B ritorna stringa strana | Firmware vecchio o lingua non SCPI | Verifica con `raw_query("load", "*IDN?")` |
| Counter va in timeout | Gate time lungo + timeout basso | `SPECS["counter"].timeout_ms` è già a 15000. Aumenta se misuri segnali a bassissima frequenza |
| Counter ritorna sempre 0 o stringa vuota | Hai inviato `IN` aspettandoti una lettura: `IN` è **Initialize** | Usa `counter_read` (che fa `read()`) o `raw_read("counter")` |
| DMM ritorna sempre lo stesso valore | Dimenticato `TRIG SGL`: senza, leggi dal buffer la misura precedente | Manda sempre `<FUNC> [range]; TRIG SGL` prima del `raw_read("dmm")` |
| `psu_measurements` ritorna 0 V con uscita ON | Sense aperto o nessun carico collegato | Normale a vuoto; collega un carico e ritesta |
| `psu_set_current_limit(0)` viene "ignorato" | Il 6632A ha minimo ~0.02 A, lo sostituisce silenziosamente | Usa almeno 0.02 A, oppure spegni l'uscita con `psu_output(False)` |
| `load_set_current` errore "fuori range" per valore valido | Stai usando range LOW (0..6 A) con setpoint > 6 A | Chiama `load_set_current_range(high_range=True)` |
| Claude vede i tool ma le chiamate falliscono con 401 | Bearer token mancante o errato lato client | Verifica `env.MCP_TOKEN` in `claude_desktop_config.json` |
| `load_errors` ritorna sempre `-113,"Undefined header"` | Comando inviato non riconosciuto dal 6060B | Probabilmente è in Compatibility mode: vedi sopra |
| Prima apertura fallisce con `INV_OBJECT` dopo aver chiuso un altro processo Python | Bug noto di state retention del driver Contec | `_open()` ritenta automaticamente fino a 3 volte; se persiste, attendi qualche secondo e riavvia il server |
| `OVP scattato` e PSU non eroga più | OVP è intervenuto, l'uscita è in cortocircuito tramite SCR | Rimuovi la causa, poi `psu_reset_protection` (`RST`) |

## 15. Estensioni possibili

Quando la base funziona, valuta di aggiungere:

- **Tool dedicati per il DMM HP 3457A** (`dmm_measure_dc_voltage`, `dmm_measure_resistance`, `dmm_set_nplc`, ...). Lo schema è quello di `psu_*`: un helper `_dmm` + chiamate a `query`/`write`.
- **HP 33120A** (generatore di funzioni, SCPI): analogo al 6060B. Aggiungi una famiglia `genfun_*`.
- **Tool composti** ("sequence tool"): un singolo tool che fa una sequenza completa, es. "carica con corrente crescente e misura tensione di breakdown", per evitare round-trip MCP multipli. `psu_ramp_voltage` e `psu_ramp_with_dmm` sono già esempi di questo pattern.
- **Salvataggio CSV/Parquet** delle rampe direttamente su disco di PC-LAB, restituendo solo il path al client.
- **Audit logging strutturato**: tutti i comandi inviati e le risposte sono già su `server.log`, ma puoi affiancare un logger JSON per analisi a posteriori.

---

Con questa base hai un banco di misura HP completo (PSU + Load + Counter + DMM) controllabile in linguaggio naturale da Claude. Da qui in avanti è solo questione di aggiungere tool man mano che servono nuove operazioni sugli strumenti.
