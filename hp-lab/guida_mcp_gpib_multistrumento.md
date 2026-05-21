# Guida MCP GPIB multi-strumento — dalla sezione 6

Questa è la continuazione della guida `guida_mcp_gpib_contec_windows.md`, riscritta dalla sezione 6 per gestire **più strumenti contemporaneamente** sulla stessa scheda Contec. La configurazione descritta supporta:

- **HP 6632A** — alimentatore DC programmabile (pre-SCPI, dialetto HP)
- **HP 6060B** — carico elettronico DC (SCPI)
- **HP 5334B** — contatore universale (pre-SCPI, dialetto HP)

Tutti e tre vivono sullo stesso bus GPIB, ognuno al proprio indirizzo. Un singolo `server.py` ne gestisce le sessioni VISA in parallelo ed espone tool MCP "parlanti" (`psu_set_voltage`, `load_measure_current`, `counter_measure_frequency`...) invece di SCPI grezzi.

Le sezioni 1-5 della guida precedente restano valide: installazione di Python, configurazione di PowerShell, installazione di KI-VISA. Riprendi da qui.

---

## 6. Creazione del progetto Python su PC-LAB

In PowerShell:

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

> Nota 1: **non installare** `pyvisa-py`. Quel backend è Python puro e non sa parlare con Contec; pyvisa deve usare la VISA di sistema (KI-VISA).
>
> Nota 2: il vincolo `numpy<2` serve perché NumPy 2.x richiede istruzioni CPU **X86_V2** (SSE4.x, POPCNT, ecc.) introdotte da Intel Nehalem (2008). PC-LAB più vecchi (Core 2, Atom prime generazioni, Pentium D) non le hanno e ricevono un `RuntimeError` all'import. NumPy 1.26 funziona ovunque. `pyvisa` importa numpy a livello modulo anche se non lo usiamo direttamente, quindi è una dipendenza obbligata.

### Indirizzi GPIB degli strumenti

Prima di proseguire, verifica e annota l'**indirizzo GPIB** di ogni strumento dal pannello frontale di ciascuno. Valori tipici da fabbrica:

| Strumento | Indirizzo di default | Tasto/menu per cambiarlo |
|---|---|---|
| HP 6632A | 5 | tasto **Address** |
| HP 6060B | 5 | menu **Address** sotto Local/Remote |
| HP 5334B | 18 | tasto **GP-IB Adrs** |

Imposta indirizzi **diversi** tra loro. In questa guida useremo:

- HP 6632A → `5`
- HP 6060B → `6`
- HP 5334B → `14`

Sostituisci ovunque con i tuoi se differiscono.

---

## 7. Test di connessione multi-strumento

Crea uno script di verifica veloce per controllare che tutti e tre rispondano. Da PowerShell:

```powershell
notepad test_strumenti.py
```

Incolla questo contenuto (versione robusta con retry, per gestire il bug di state retention del driver Contec quando si lanciano processi in rapida successione):

```python
"""Verifica che HP 6632A, HP 6060B e HP 5334B siano raggiungibili sul bus.
Versione con retry: il driver Contec a volte rifiuta la prima apertura
dopo un processo precedente. Ritentiamo con RM fresco se necessario."""
import time
import pyvisa

GPIB_BOARD = 0
MAX_ATTEMPTS = 4
RETRY_DELAY_S = 1.0

INSTRUMENTS = {
    "HP 6632A (PSU)":     {"addr": 5,  "id_cmd": "ID?"},
    "HP 6060B (Load)":    {"addr": 6,  "id_cmd": "*IDN?"},
    "HP 5334B (Counter)": {"addr": 14, "id_cmd": "ID"},
}


def try_open(rm, addr, id_cmd):
    """Apre, identifica e chiude. Ritorna (ok, message)."""
    inst = None
    try:
        inst = rm.open_resource(f"GPIB{GPIB_BOARD}::{addr}::INSTR")
        inst.timeout = 5000
        inst.read_termination = "\n"
        inst.write_termination = "\n"
        inst.clear()
        idn = inst.query(id_cmd).strip()
        return True, idn
    except Exception as e:
        return False, str(e)
    finally:
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass


def fresh_rm():
    return pyvisa.ResourceManager()


rm = fresh_rm()
print("VISA library:", rm.visalib)
print()

for name, cfg in INSTRUMENTS.items():
    print(f"--- {name} a GPIB::{cfg['addr']} ---")
    success = False
    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ok, msg = try_open(rm, cfg["addr"], cfg["id_cmd"])
        if ok:
            print(f"  OK (tentativo {attempt}) -> {msg}")
            success = True
            break
        last_err = msg
        print(f"  tentativo {attempt} fallito: {msg}")
        # Se è il bug di state retention, ricrea l'RM
        if "INV_OBJECT" in msg:
            try:
                rm.close()
            except Exception:
                pass
            time.sleep(RETRY_DELAY_S)
            rm = fresh_rm()
        else:
            time.sleep(RETRY_DELAY_S)
    if not success:
        print(f"  FALLITO dopo {MAX_ATTEMPTS} tentativi: {last_err}")
    print()

try:
    rm.close()
except Exception:
    pass
```

Esegui:

```powershell
python .\test_strumenti.py
```

**Output atteso:** tre righe `OK -> ...` con le stringhe di identificazione. Se un singolo strumento fallisce ma gli altri rispondono, controlla:

- indirizzo GPIB sul pannello (potrebbe essere diverso da quello in `INSTRUMENTS`)
- cavi GPIB
- alimentazione dello strumento (acceso)

**Caso particolare HP 6060B:** se non risponde a `*IDN?` ma sembra acceso e cablato bene, potrebbe essere in **Compatibility mode** invece che SCPI. Sul pannello frontale entra nel menu di configurazione GPIB e seleziona `LANG = SCPI`, salva, riavvia lo strumento.

Quando i tre rispondono, sei pronto per il server multi-strumento.

---

## 8. Il file `server.py` multi-strumento

Crea `server.py` nella cartella di lavoro:

```powershell
notepad server.py
```

Incolla questo contenuto. Le sezioni sono commentate, leggilo dall'alto per capire la struttura.

```python
"""Server MCP multi-strumento per banco HP da laboratorio.

Strumenti gestiti:
  - HP 6632A — Power Supply (pre-SCPI, dialetto HP)
  - HP 6060B — Electronic Load (SCPI)
  - HP 5334B — Universal Counter (pre-SCPI, dialetto HP)

Tutti sullo stesso bus GPIB via scheda Contec + KI-VISA.
Trasporto MCP: Streamable HTTP (porta 8000 per default).
"""
from __future__ import annotations

import os
import logging
import secrets
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional

import pyvisa
from mcp.server.fastmcp import FastMCP, Context

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mcp-gpib")

# ---------------------------------------------------------------------------
# Configurazione - tutto da variabili d'ambiente
# ---------------------------------------------------------------------------
GPIB_BOARD = os.environ.get("GPIB_BOARD", "0")

PSU_ADDR     = os.environ.get("PSU_ADDR",     "5")    # HP 6632A
LOAD_ADDR    = os.environ.get("LOAD_ADDR",    "6")    # HP 6060B
COUNTER_ADDR = os.environ.get("COUNTER_ADDR", "14")   # HP 5334B

HTTP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("MCP_PORT", "8000"))

# Token opzionale: se impostato, i client devono passare
# `Authorization: Bearer <token>` come header HTTP.
MCP_TOKEN = os.environ.get("MCP_TOKEN")  # None = autenticazione disabilitata


# ---------------------------------------------------------------------------
# Definizione strumenti
# ---------------------------------------------------------------------------
@dataclass
class InstrumentSpec:
    """Configurazione per un singolo strumento sul bus."""
    name: str
    addr: str
    language: str            # "scpi" | "hp_pre"
    timeout_ms: int = 5000
    read_term: str = "\n"
    write_term: str = "\n"


SPECS = {
    "psu":     InstrumentSpec("HP 6632A",  PSU_ADDR,     "hp_pre", 5000),
    "load":    InstrumentSpec("HP 6060B",  LOAD_ADDR,    "scpi",   5000),
    # Counter timeout più alto: con gate lunghi una misura può richiedere >1 s
    "counter": InstrumentSpec("HP 5334B",  COUNTER_ADDR, "hp_pre", 15000),
}


# ---------------------------------------------------------------------------
# Contesto di lifespan: apre tutte le sessioni VISA tolleranti agli errori
# ---------------------------------------------------------------------------
@dataclass
class LabContext:
    """Sessioni VISA per strumento. Un valore può essere None se la
    connessione iniziale è fallita: i tool relativi alzeranno errore."""
    psu: Optional[pyvisa.resources.MessageBasedResource]
    load: Optional[pyvisa.resources.MessageBasedResource]
    counter: Optional[pyvisa.resources.MessageBasedResource]


def _open(rm: pyvisa.ResourceManager, spec: InstrumentSpec, max_attempts: int = 3):
    """Apre e configura una singola risorsa, con retry per il bug di state
    retention del driver Contec. Ritorna None se tutti i tentativi falliscono."""
    import time
    resource = f"GPIB{GPIB_BOARD}::{spec.addr}::INSTR"
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            inst = rm.open_resource(resource)
            inst.timeout = spec.timeout_ms
            inst.read_termination = spec.read_term
            inst.write_termination = spec.write_term
            inst.clear()
            # Identificazione di sanità
            id_cmd = "*IDN?" if spec.language == "scpi" else "ID?"
            try:
                idn = inst.query(id_cmd).strip()
                log.info("OK %s @ %s -> %s (tentativo %d)",
                         spec.name, resource, idn, attempt)
            except Exception as e:
                log.warning("%s @ %s aperto ma non risponde a %s: %s",
                            spec.name, resource, id_cmd, e)
            return inst
        except Exception as e:
            last_err = e
            log.warning("Tentativo %d/%d fallito per %s @ %s: %s",
                        attempt, max_attempts, spec.name, resource, e)
            if attempt < max_attempts:
                time.sleep(1.0)
    log.error("Impossibile aprire %s @ %s dopo %d tentativi: %s",
              spec.name, resource, max_attempts, last_err)
    return None


@asynccontextmanager
async def lab_lifespan(server: FastMCP) -> AsyncIterator[LabContext]:
    rm = pyvisa.ResourceManager()
    log.info("VISA in uso: %s", rm.visalib)
    psu     = _open(rm, SPECS["psu"])
    load    = _open(rm, SPECS["load"])
    counter = _open(rm, SPECS["counter"])
    try:
        yield LabContext(psu=psu, load=load, counter=counter)
    finally:
        for inst in (psu, load, counter):
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
        rm.close()


# ---------------------------------------------------------------------------
# Middleware opzionale di autenticazione (bearer token)
# ---------------------------------------------------------------------------
class BearerAuthMiddleware:
    """ASGI middleware: se MCP_TOKEN è impostato, richiede
    `Authorization: Bearer <token>` su ogni richiesta HTTP."""
    def __init__(self, app, token: Optional[str]):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if self.token and scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            expected = f"Bearer {self.token}"
            if not secrets.compare_digest(auth, expected):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Istanza FastMCP
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "hp-lab",
    lifespan=lab_lifespan,
    host=HTTP_HOST,
    port=HTTP_PORT,
)


# ---------------------------------------------------------------------------
# Helper: estrazione strumenti dal Context con messaggi utili
# ---------------------------------------------------------------------------
def _require(inst, name: str):
    if inst is None:
        raise RuntimeError(
            f"{name} non disponibile. Verifica indirizzo GPIB, "
            "cavo e alimentazione. Vedi i log del server all'avvio."
        )
    return inst


def _psu(ctx: Context):
    return _require(ctx.request_context.lifespan_context.psu, "HP 6632A (PSU)")


def _load(ctx: Context):
    return _require(ctx.request_context.lifespan_context.load, "HP 6060B (Load)")


def _counter(ctx: Context):
    return _require(ctx.request_context.lifespan_context.counter, "HP 5334B (Counter)")


# ===========================================================================
# TOOL GENERICI - introspezione e SCPI raw per debug
# ===========================================================================
@mcp.tool()
def list_instruments(ctx: Context) -> dict:
    """Elenca gli strumenti configurati e il loro stato di connessione."""
    lc = ctx.request_context.lifespan_context
    out = {}
    for key, inst in (("psu", lc.psu), ("load", lc.load), ("counter", lc.counter)):
        spec = SPECS[key]
        out[key] = {
            "model": spec.name,
            "address": f"GPIB{GPIB_BOARD}::{spec.addr}",
            "language": spec.language,
            "connected": inst is not None,
        }
    return out


@mcp.tool()
def raw_query(ctx: Context, target: str, command: str) -> str:
    """Invia una query SCPI/HP grezza a uno strumento e ritorna la risposta.

    target: 'psu' | 'load' | 'counter'
    command: stringa esatta da inviare, deve terminare con '?'
    """
    if not command.strip().endswith("?"):
        raise ValueError("Le query devono terminare con '?'. Usa raw_write per i comandi.")
    inst = {"psu": _psu, "load": _load, "counter": _counter}[target](ctx)
    return inst.query(command).strip()


@mcp.tool()
def raw_write(ctx: Context, target: str, command: str) -> str:
    """Invia un comando di scrittura grezzo a uno strumento.

    target: 'psu' | 'load' | 'counter'
    command: stringa esatta da inviare (senza '?')
    """
    if "?" in command:
        raise ValueError("I comandi di scrittura non devono contenere '?'. Usa raw_query.")
    inst = {"psu": _psu, "load": _load, "counter": _counter}[target](ctx)
    inst.write(command)
    return f"Inviato a {target}: {command}"


# ===========================================================================
# HP 6632A — Power Supply (pre-SCPI, dialetto HP)
# ===========================================================================
@mcp.tool()
def psu_identify(ctx: Context) -> str:
    """Identificazione del power supply (ID?)."""
    return _psu(ctx).query("ID?").strip()


@mcp.tool()
def psu_set_voltage(ctx: Context, volts: float) -> str:
    """Imposta la tensione di uscita del PSU. Range 0-20 V per il 6632A."""
    if not 0.0 <= volts <= 20.0:
        raise ValueError("Tensione fuori range (0-20 V per il 6632A).")
    _psu(ctx).write(f"VSET {volts:.4f}")
    return f"PSU: VSET = {volts:.4f} V"


@mcp.tool()
def psu_set_current_limit(ctx: Context, amps: float) -> str:
    """Imposta il limite di corrente del PSU. Range 0-5 A per il 6632A."""
    if not 0.0 <= amps <= 5.0:
        raise ValueError("Corrente fuori range (0-5 A per il 6632A).")
    _psu(ctx).write(f"ISET {amps:.4f}")
    return f"PSU: ISET = {amps:.4f} A"


@mcp.tool()
def psu_output(ctx: Context, on: bool) -> str:
    """Accende (on=True) o spegne (on=False) l'uscita del PSU."""
    _psu(ctx).write(f"OUT {1 if on else 0}")
    return f"PSU: OUT = {'ON' if on else 'OFF'}"


@mcp.tool()
def psu_read_voltage(ctx: Context) -> float:
    """Legge la tensione effettivamente erogata dal PSU (in V)."""
    return float(_psu(ctx).query("VOUT?").strip())


@mcp.tool()
def psu_read_current(ctx: Context) -> float:
    """Legge la corrente effettivamente erogata dal PSU (in A)."""
    return float(_psu(ctx).query("IOUT?").strip())


@mcp.tool()
def psu_status(ctx: Context) -> dict:
    """Riassume lo stato del PSU: setpoint, misure, stato uscita."""
    inst = _psu(ctx)
    vset = float(inst.query("VSET?").strip())
    iset = float(inst.query("ISET?").strip())
    vout = float(inst.query("VOUT?").strip())
    iout = float(inst.query("IOUT?").strip())
    out_state = inst.query("OUT?").strip()
    return {
        "vset_V": vset, "iset_A": iset,
        "vout_V": vout, "iout_A": iout,
        "power_W": vout * iout,
        "output": "ON" if out_state.startswith("1") else "OFF",
    }


@mcp.tool()
def psu_reset(ctx: Context) -> str:
    """Reset del PSU (CLR)."""
    inst = _psu(ctx)
    inst.clear()
    inst.write("CLR")
    return "PSU resettato (CLR + Device Clear)."


# ===========================================================================
# HP 6060B — Electronic Load (SCPI)
# ===========================================================================
LOAD_MODES = {"CURR", "VOLT", "RES"}


@mcp.tool()
def load_identify(ctx: Context) -> str:
    """Identificazione del carico elettronico (*IDN?)."""
    return _load(ctx).query("*IDN?").strip()


@mcp.tool()
def load_set_mode(ctx: Context, mode: str) -> str:
    """Imposta il modo operativo del carico.

    mode: 'CURR' (corrente costante), 'VOLT' (tensione costante),
          'RES' (resistenza costante)
    """
    m = mode.upper()
    if m not in LOAD_MODES:
        raise ValueError(f"Modo non valido. Disponibili: {sorted(LOAD_MODES)}")
    _load(ctx).write(f"MODE {m}")
    return f"Load: MODE = {m}"


@mcp.tool()
def load_set_current(ctx: Context, amps: float) -> str:
    """Imposta il setpoint di corrente (in modo CC). Range 0-60 A sul 6060B."""
    if not 0.0 <= amps <= 60.0:
        raise ValueError("Corrente fuori range (0-60 A per il 6060B).")
    _load(ctx).write(f"CURR {amps:.4f}")
    return f"Load: CURR = {amps:.4f} A"


@mcp.tool()
def load_set_voltage(ctx: Context, volts: float) -> str:
    """Imposta il setpoint di tensione (in modo CV). Range 0-60 V sul 6060B."""
    if not 0.0 <= volts <= 60.0:
        raise ValueError("Tensione fuori range (0-60 V per il 6060B).")
    _load(ctx).write(f"VOLT {volts:.4f}")
    return f"Load: VOLT = {volts:.4f} V"


@mcp.tool()
def load_set_resistance(ctx: Context, ohms: float) -> str:
    """Imposta il setpoint di resistenza (in modo CR). Range 0.033-10000 Ohm."""
    if not 0.033 <= ohms <= 10000.0:
        raise ValueError("Resistenza fuori range (0.033-10000 Ohm per il 6060B).")
    _load(ctx).write(f"RES {ohms:.4f}")
    return f"Load: RES = {ohms:.4f} Ohm"


@mcp.tool()
def load_set_current_range(ctx: Context, high_range: bool) -> str:
    """Seleziona il range di corrente del carico.
    high_range=True -> 0-60 A; high_range=False -> 0-6 A (miglior risoluzione).
    """
    val = "HIGH" if high_range else "LOW"
    _load(ctx).write(f"CURR:RANG {val}")
    return f"Load: CURR:RANG = {val}"


@mcp.tool()
def load_input(ctx: Context, on: bool) -> str:
    """Accende (on=True) o spegne (on=False) l'ingresso del carico."""
    _load(ctx).write(f"INP {'ON' if on else 'OFF'}")
    return f"Load: INP = {'ON' if on else 'OFF'}"


@mcp.tool()
def load_measure_voltage(ctx: Context) -> float:
    """Misura la tensione ai morsetti del carico (V)."""
    return float(_load(ctx).query("MEAS:VOLT?").strip())


@mcp.tool()
def load_measure_current(ctx: Context) -> float:
    """Misura la corrente assorbita dal carico (A)."""
    return float(_load(ctx).query("MEAS:CURR?").strip())


@mcp.tool()
def load_measure_power(ctx: Context) -> float:
    """Misura la potenza dissipata dal carico (W)."""
    return float(_load(ctx).query("MEAS:POW?").strip())


@mcp.tool()
def load_status(ctx: Context) -> dict:
    """Riassume lo stato del carico: modo, setpoint, misure."""
    inst = _load(ctx)
    mode = inst.query("MODE?").strip()
    inp = inst.query("INP?").strip()
    v = float(inst.query("MEAS:VOLT?").strip())
    i = float(inst.query("MEAS:CURR?").strip())
    p = float(inst.query("MEAS:POW?").strip())
    return {
        "mode": mode,
        "input": "ON" if inp.startswith(("1", "ON")) else "OFF",
        "voltage_V": v, "current_A": i, "power_W": p,
    }


@mcp.tool()
def load_reset(ctx: Context) -> str:
    """Reset del carico (*RST + *CLS)."""
    inst = _load(ctx)
    inst.clear()
    inst.write("*CLS")
    inst.write("*RST")
    return "Load resettato (*RST + *CLS + Device Clear)."


@mcp.tool()
def load_errors(ctx: Context, max_errors: int = 10) -> list[str]:
    """Legge la coda degli errori SCPI del carico."""
    inst = _load(ctx)
    errors: list[str] = []
    for _ in range(max_errors):
        err = inst.query("SYST:ERR?").strip()
        errors.append(err)
        if err.startswith(("0,", "+0,")):
            break
    return errors


# ===========================================================================
# HP 5334B — Universal Counter (pre-SCPI, dialetto HP)
# ===========================================================================
COUNTER_FUNCTIONS = {
    "FREQ_A":     "FN1",   # Frequency Channel A
    "FREQ_B":     "FN2",   # Frequency Channel B (richiede opzione)
    "PERIOD_A":   "FN3",   # Period A
    "TIME_AB":    "FN4",   # Time Interval A -> B
    "RATIO_AB":   "FN5",   # Ratio A / B
    "TOTALIZE_A": "FN6",   # Totalize A
    "DC_VOLTS_A": "FN8",   # Tensione DC su canale A (DVM)
}


@mcp.tool()
def counter_identify(ctx: Context) -> str:
    """Identificazione del contatore (ID?)."""
    return _counter(ctx).query("ID?").strip()


@mcp.tool()
def counter_set_function(ctx: Context, function: str) -> str:
    """Imposta la funzione di misura del contatore.

    function: 'FREQ_A', 'FREQ_B', 'PERIOD_A', 'TIME_AB',
              'RATIO_AB', 'TOTALIZE_A', 'DC_VOLTS_A'
    """
    f = function.upper()
    if f not in COUNTER_FUNCTIONS:
        raise ValueError(f"Funzione non valida. Disponibili: {sorted(COUNTER_FUNCTIONS)}")
    _counter(ctx).write(COUNTER_FUNCTIONS[f])
    return f"Counter: funzione = {f} ({COUNTER_FUNCTIONS[f]})"


@mcp.tool()
def counter_autotrigger(ctx: Context) -> str:
    """Attiva l'autotrigger sui canali (regola automaticamente le soglie)."""
    _counter(ctx).write("AU")
    return "Counter: autotrigger attivato (AU)."


@mcp.tool()
def counter_read(ctx: Context) -> dict:
    """Legge il valore corrente di misura del contatore.

    Restituisce sia il valore numerico parsato che la stringa raw,
    perché alcuni modi (es. totalize) non sono numeri puri.
    """
    inst = _counter(ctx)
    # IN inizia/legge una misura singola sul 5334B
    raw = inst.query("IN").strip()
    try:
        # Le risposte numeriche del 5334B sono in notazione scientifica:
        # es. " F  1.23456789E+06" (la 'F' è l'indicatore di funzione)
        # Prendiamo l'ultima parola e tentiamo il parsing.
        token = raw.split()[-1]
        value = float(token)
    except (ValueError, IndexError):
        value = None
    return {"raw": raw, "value": value}


@mcp.tool()
def counter_measure_frequency(ctx: Context, channel: str = "A") -> float:
    """Misura rapida di frequenza su canale A o B (Hz)."""
    if channel.upper() not in ("A", "B"):
        raise ValueError("channel deve essere 'A' o 'B'.")
    inst = _counter(ctx)
    inst.write("FN1" if channel.upper() == "A" else "FN2")
    raw = inst.query("IN").strip()
    token = raw.split()[-1]
    return float(token)


@mcp.tool()
def counter_measure_period(ctx: Context) -> float:
    """Misura rapida di periodo sul canale A (s)."""
    inst = _counter(ctx)
    inst.write("FN3")
    raw = inst.query("IN").strip()
    token = raw.split()[-1]
    return float(token)


@mcp.tool()
def counter_reset(ctx: Context) -> str:
    """Reset del contatore."""
    inst = _counter(ctx)
    inst.clear()
    inst.write("RE")
    return "Counter resettato (RE + Device Clear)."


# ---------------------------------------------------------------------------
# Avvio: applica eventuale auth middleware e lancia il server HTTP
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if MCP_TOKEN:
        log.info("Autenticazione bearer token: ABILITATA")
        # FastMCP espone l'app ASGI: incapsuliamo l'app con il middleware
        original_app = mcp.streamable_http_app()
        wrapped = BearerAuthMiddleware(original_app, MCP_TOKEN)
        import uvicorn
        uvicorn.run(wrapped, host=HTTP_HOST, port=HTTP_PORT)
    else:
        log.info("Autenticazione: DISABILITATA (nessun MCP_TOKEN)")
        mcp.run(transport="streamable-http")
```

Salva.

### Riepilogo della struttura

- **Configurazione da env vars**: indirizzi GPIB, porta HTTP, token opzionale.
- **`_open()`**: apre ogni strumento in modo tollerante: se uno non risponde, il server parte comunque e gli altri funzionano. Lo strumento mancante alza un errore esplicativo solo se chiamato.
- **`SPECS`**: descrive caratteristiche di ogni strumento (timeout, terminatori, linguaggio). Il counter ha timeout più lungo perché con gate time lunghi una misura può richiedere secondi.
- **Tool per strumento**: `psu_*`, `load_*`, `counter_*`. Ogni famiglia è autonoma e usa il linguaggio corretto.
- **Tool generici**: `list_instruments`, `raw_query`, `raw_write` per introspezione e debug.
- **Autenticazione opzionale** via `MCP_TOKEN`: se impostato, il server richiede header `Authorization: Bearer <token>` su ogni richiesta.

---

## 9. Avvio del server su PC-LAB

Dalla cartella `mcp-gpib` con il venv attivo:

```powershell
$env:GPIB_BOARD   = "0"
$env:PSU_ADDR     = "5"
$env:LOAD_ADDR    = "2"
$env:COUNTER_ADDR = "3"
$env:MCP_HOST     = "0.0.0.0"
$env:MCP_PORT     = "8000"
# (opzionale) abilita autenticazione bearer:
# $env:MCP_TOKEN  = "il-tuo-token-segreto"

python .\server.py
```

All'avvio dovresti vedere nei log:

```
... INFO mcp-gpib: VISA in uso: <KI-VISA ...>
... INFO mcp-gpib: OK HP 6632A @ GPIB0::5::INSTR -> HP6632A
... INFO mcp-gpib: OK HP 6060B @ GPIB0::6::INSTR -> HEWLETT-PACKARD,6060B,...
... INFO mcp-gpib: OK HP 5334B @ GPIB0::14::INSTR -> HP5334B
... INFO mcp-gpib: Autenticazione: DISABILITATA (nessun MCP_TOKEN)
... INFO:     Uvicorn running on http://0.0.0.0:8000
```

Se uno strumento è offline vedrai `ERROR ... Impossibile aprire ...` per quello soltanto: il server parte ugualmente con i restanti, e i tool relativi alzeranno errore esplicativo quando chiamati.

Per fermare: `Ctrl+C`.

---

## 10. Configurazione del firewall su PC-LAB

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

---

## 11. Test di rete da PC-CLIENT

```powershell
Test-NetConnection -ComputerName 192.168.0.20 -Port 8000
```

Devi vedere `TcpTestSucceeded : True`.

---

## 12. Setup di PC-CLIENT: Node.js + mcp-remote

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

Salva e **riavvia Claude Desktop completamente** (anche dall'icona della systray).

---

## 13. Verifica end-to-end

In Claude Desktop, prova prompt come:

> "Elenca gli strumenti del banco HP e dimmi quali sono connessi."

Claude userà `list_instruments` e ti darà lo stato di tutti e tre.

> "Imposta il PSU a 5V con limite 100 mA, accendi l'uscita, poi misura tensione e corrente effettive."

Sequenza attesa: `psu_set_voltage(5.0)` → `psu_set_current_limit(0.1)` → `psu_output(True)` → `psu_read_voltage()` + `psu_read_current()`.

> "Configura il carico in modo corrente costante a 50 mA, accendi l'ingresso, e dimmi quanta potenza sta dissipando."

> "Misura la frequenza sul canale A del contatore."

Per debug più avanzato puoi anche usare l'MCP Inspector da PC-CLIENT:

```powershell
npx @modelcontextprotocol/inspector
```

Trasporto: **Streamable HTTP**, URL `http://192.168.1.50:8000/mcp`.

---

## 14. Avvio automatico come servizio Windows (NSSM)

Tenere aperta una finestra PowerShell sul PC-LAB non è pratico. Registra il server come servizio Windows con **NSSM**:

1. Scarica NSSM 64 bit da <https://nssm.cc/download>.
2. Estrai `nssm.exe` in `C:\Tools\`.
3. PowerShell come amministratore:

   ```powershell
   C:\Tools\nssm.exe install MCPGpibServer
   ```

4. Nella GUI configura (sostituisci `TUO_UTENTE`):

   - **Application Path**: `C:\Users\TUO_UTENTE\mcp-gpib\.venv\Scripts\python.exe`
   - **Startup directory**: `C:\Users\TUO_UTENTE\mcp-gpib`
   - **Arguments**: `server.py`

5. Tab **Environment**:

   ```
   GPIB_BOARD=0
   PSU_ADDR=5
   LOAD_ADDR=6
   COUNTER_ADDR=14
   MCP_HOST=0.0.0.0
   MCP_PORT=8000
   ```

   (e `MCP_TOKEN=...` se vuoi attivare l'autenticazione)

6. Tab **I/O**:

   - **Output (stdout)**: `C:\Users\TUO_UTENTE\mcp-gpib\server.log`
   - **Error (stderr)**: `C:\Users\TUO_UTENTE\mcp-gpib\server.err.log`

7. **Install service**, poi:

   ```powershell
   Start-Service MCPGpibServer
   Get-Service MCPGpibServer
   ```

---

## 15. Note di sicurezza

Senza `MCP_TOKEN`, chiunque sulla LAN che raggiunga la porta 8000 può comandare gli strumenti. In LAN di laboratorio chiusa è generalmente accettabile, ma considera che il PSU può erogare 100 W e il carico può dissiparne 300: una connessione non autorizzata può fare danni fisici.

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

Poi configura Claude come mostrato nella sezione 12 col blocco `env`.

**3. Reverse proxy con TLS.** Per uso fuori dalla LAN di laboratorio, metti davanti Caddy o nginx con certificato (anche self-signed) e togli `--allow-http`.

---

## 16. Troubleshooting

| Sintomo | Causa probabile | Soluzione |
|---|---|---|
| Tutti gli strumenti `Impossibile aprire` all'avvio | KI-VISA non vede la scheda Contec | Riesegui `CTSTGPIB.EXE`, ricontrolla che KI-VISA sia 64 bit |
| Solo HP 6060B fallisce | Carico in Compatibility mode invece di SCPI | Sul pannello frontale del 6060B, menu GPIB → `LANG = SCPI` |
| `*IDN?` su 6060B ritorna stringa strana | Firmware molto vecchio o lingua non SCPI | Verifica con `raw_query("load", "*IDN?")` |
| 5334B in timeout | Gate time lungo + timeout basso | `SPECS["counter"].timeout_ms` già a 15000 nel codice. Aumenta se misuri segnali a bassa frequenza |
| `psu_status` ritorna valori a zero ma output ON | Sense aperto o load assente | Normale a vuoto; collega un carico e ritesta |
| `load_set_current` errore "fuori range" mentre invece il valore è OK | Stai usando range LOW (0-6 A) per > 6 A | Chiama `load_set_current_range(high_range=True)` |
| Claude vede tool ma `raw_query` fallisce con `Unauthorized` | Bearer token mancante o errato lato client | Verifica `env.MCP_TOKEN` in `claude_desktop_config.json` |
| `load_errors` ritorna sempre `-113,"Undefined header"` | Comando inviato non riconosciuto dal 6060B | Probabilmente è in Compatibility mode: vedi sopra |
| Counter `IN` ritorna stringa che non riesco a parsare | Funzione che ritorna dati strutturati (totalize, time interval) | Usa `raw_query("counter", "IN")` e lavora sulla stringa |

---

## 17. Estensioni successive

Quando tutto gira:

- **Aggiungere HP 33120A** (generatore di funzioni, SCPI): è banale, simile al 6060B. Aggiungi una sezione `genfun_*` con tool tipo `genfun_set_frequency`, `genfun_set_amplitude`, `genfun_output`.
- **Aggiungere HP 3457A** (DMM, pre-SCPI): simile al 6632A. Tool tipo `dmm_measure_dc_voltage`, con flusso "imposta funzione → trigger → leggi".
- **Aggiungere Racal-Dana 5002**: dopo aver recuperato il manuale. Probabilmente serve `read_bytes()` invece di `query()` per le sue risposte a lunghezza fissa.
- **Tool composti** ("sequence tool"): un singolo tool che fa una sequenza completa, es. "carica con corrente crescente e misura tensione di breakdown" — utile per evitare round-trip MCP multipli.
- **Logging strutturato** su file: aggiungi un `FileHandler` al logger per tracciare tutti i comandi inviati e le risposte (audit di laboratorio).

Quando questa base funziona, hai un banco di misura HP completo controllabile in linguaggio naturale da Claude. Buon laboratorio!
