"""Server MCP multi-strumento per banco HP da laboratorio.

Strumenti gestiti (attivi):
  - HP 6632A — Power Supply (dialetto HP pre-SCPI)
    Riferimento: Operating Manual, Table 6-1 "Summary of Power Supply
    Instruction Set". NOTA IMPORTANTE: il 6632A NON ha query per i setpoint
    (no VSET?, no ISET?, no OUT?). Le sole query disponibili sono
    VOUT?, IOUT?, FAULT?, STS?, ASTS?, ERR?, TEST?, ID?, ROM?.

  - HP 5334B — Universal Counter (dialetto HP pre-SCPI)
    Riferimento: Operation & Programming Manual, Table 3-12 (Dec 1993).
    NOTA: ID senza '?', terminatori CR/LF, le misure si LEGGONO
    continuamente (no comando di trigger). 'IN' = Initialize (power-on),
    NON è un comando di lettura.

  - HP 3457A — Digital Multimeter 6.5 digit (dialetto HP pre-SCPI)
    Riferimento: HP 3457A Quick Reference.
    NOTA: ID? CON '?' (come il 6632A), header in MAIUSCOLO, terminatori
    CR/LF in ricezione, ';' separa comandi multipli. Pattern di misura
    usato qui: 'TARM AUTO; NRDGS 1,AUTO; TRIG HOLD' all'avvio, poi
    '<FUNCTION>;TRIG SGL' per ogni misura fresca.

  - HP 6060B — Electronic Load (SCPI)
    Carico elettronico DC 60V/60A/300W. Usa il dialetto SCPI moderno
    (*IDN?, MEAS:VOLT?, ecc.). Se non risponde a *IDN?, probabilmente è
    in 'Compatibility mode': sul pannello frontale, menu GPIB → LANG = SCPI.

Tutti sullo stesso bus GPIB via scheda Contec + KI-VISA.
Trasporto MCP: Streamable HTTP (porta 8000 per default).
"""
from __future__ import annotations

import os
import logging
import secrets
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Optional

import pyvisa
from mcp.server.fastmcp import FastMCP, Context

# ---------------------------------------------------------------------------
# Logging - console + file rotante
# ---------------------------------------------------------------------------
LOG_DIR = os.environ.get("MCP_LOG_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "server.log")

_log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_console = logging.StreamHandler()
_console.setFormatter(_log_fmt)
_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_log_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console, _file_handler])
log = logging.getLogger("mcp-gpib")
log.info("Logging su file: %s", LOG_FILE)

# ---------------------------------------------------------------------------
# Configurazione - tutto da variabili d'ambiente
# ---------------------------------------------------------------------------
GPIB_BOARD = os.environ.get("GPIB_BOARD", "0")

PSU_ADDR     = os.environ.get("PSU_ADDR",     "5")    # HP 6632A
COUNTER_ADDR = os.environ.get("COUNTER_ADDR", "3")    # HP 5334B
DMM_ADDR     = os.environ.get("DMM_ADDR",     "22")   # HP 3457A
LOAD_ADDR    = os.environ.get("LOAD_ADDR",    "2")    # HP 6060B

HTTP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("MCP_PORT", "8000"))

MCP_TOKEN = os.environ.get("MCP_TOKEN")  # None = autenticazione disabilitata


# ---------------------------------------------------------------------------
# Specifica di ogni strumento
# ---------------------------------------------------------------------------
@dataclass
class InstrumentSpec:
    """Configurazione di un singolo strumento sul bus."""
    name: str
    addr: str
    id_cmd: str              # comando di identificazione (manuale-specifico!)
    timeout_ms: int = 5000
    read_term: str = "\n"
    write_term: str = "\n"


SPECS = {
    # HP 6632A: comando ID? (con '?'), terminatori \n
    "psu":     InstrumentSpec(
        name="HP 6632A",
        addr=PSU_ADDR,
        id_cmd="ID?",
        timeout_ms=5000,
    ),
    # HP 5334B: comando ID (SENZA '?'), terminatori CR/LF (manuale sez. 3-333).
    # Timeout alto perché gate time lunghi possono richiedere >10s per una misura.
    "counter": InstrumentSpec(
        name="HP 5334B",
        addr=COUNTER_ADDR,
        id_cmd="ID",
        timeout_ms=15000,
        read_term="\r\n",
        write_term="\r\n",
    ),
    # HP 3457A: ID? CON '?' (come il 6632A), terminatori CR/LF.
    # Timeout sufficiente per integrazione NPLC=100 (~2s) e self-test.
    "dmm": InstrumentSpec(
        name="HP 3457A",
        addr=DMM_ADDR,
        id_cmd="ID?",
        timeout_ms=10000,
        read_term="\r\n",
        write_term="\n",
    ),
    # HP 6060B: SCPI standard, *IDN? per identificazione, terminatori \n.
    "load": InstrumentSpec(
        name="HP 6060B",
        addr=LOAD_ADDR,
        id_cmd="*IDN?",
        timeout_ms=5000,
    ),
}


# ---------------------------------------------------------------------------
# Lifespan: apre le sessioni VISA in modo tollerante agli errori
# ---------------------------------------------------------------------------
@dataclass
class LabContext:
    psu: Optional[pyvisa.resources.MessageBasedResource]
    counter: Optional[pyvisa.resources.MessageBasedResource]
    dmm: Optional[pyvisa.resources.MessageBasedResource]
    load: Optional[pyvisa.resources.MessageBasedResource]


def _open(rm: pyvisa.ResourceManager, spec: InstrumentSpec, max_attempts: int = 3):
    """Apre e configura una risorsa con retry (bug di state retention Contec)."""
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
            try:
                idn = inst.query(spec.id_cmd).strip()
                log.info("OK %s @ %s -> %s (tentativo %d)",
                         spec.name, resource, idn, attempt)
            except Exception as e:
                log.warning("%s @ %s aperto ma non risponde a '%s': %s",
                            spec.name, resource, spec.id_cmd, e)
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
    counter = _open(rm, SPECS["counter"])
    dmm     = _open(rm, SPECS["dmm"])
    load    = _open(rm, SPECS["load"])

    # Setup del DMM: predisponiamo il pattern di misura "FUNC;TRIG SGL".
    # Senza TRIG HOLD il DMM produce misure continue e rischiamo di leggere
    # dati stantii dal buffer; con TRIG HOLD una misura parte solo quando
    # mandiamo TRIG SGL (o '?'). OFORMAT ASCII garantisce risposte testuali.
    if dmm is not None:
        try:
            dmm.write("TARM AUTO;NRDGS 1,AUTO;TRIG HOLD;OFORMAT ASCII")
            log.info("DMM configurato per misure singole su trigger")
        except Exception as e:
            log.warning("Impossibile configurare il DMM all'avvio: %s", e)

    try:
        yield LabContext(psu=psu, counter=counter, dmm=dmm, load=load)
    finally:
        for inst in (psu, counter, dmm, load):
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
        rm.close()


# ---------------------------------------------------------------------------
# Bearer token middleware opzionale
# ---------------------------------------------------------------------------
class BearerAuthMiddleware:
    """ASGI middleware: se MCP_TOKEN è impostato, richiede header
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
# Helper interni
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


def _counter(ctx: Context):
    return _require(ctx.request_context.lifespan_context.counter, "HP 5334B (Counter)")


def _dmm(ctx: Context):
    return _require(ctx.request_context.lifespan_context.dmm, "HP 3457A (DMM)")


def _load(ctx: Context):
    return _require(ctx.request_context.lifespan_context.load, "HP 6060B (Load)")


# ===========================================================================
# TOOL GENERICI - introspezione e accesso raw per debug
# ===========================================================================
@mcp.tool()
def list_instruments(ctx: Context) -> dict:
    """Elenca gli strumenti configurati e il loro stato di connessione."""
    lc = ctx.request_context.lifespan_context
    return {
        "psu": {
            "model": SPECS["psu"].name,
            "address": f"GPIB{GPIB_BOARD}::{SPECS['psu'].addr}",
            "id_cmd": SPECS["psu"].id_cmd,
            "connected": lc.psu is not None,
        },
        "counter": {
            "model": SPECS["counter"].name,
            "address": f"GPIB{GPIB_BOARD}::{SPECS['counter'].addr}",
            "id_cmd": SPECS["counter"].id_cmd,
            "connected": lc.counter is not None,
        },
        "dmm": {
            "model": SPECS["dmm"].name,
            "address": f"GPIB{GPIB_BOARD}::{SPECS['dmm'].addr}",
            "id_cmd": SPECS["dmm"].id_cmd,
            "connected": lc.dmm is not None,
        },
        "load": {
            "model": SPECS["load"].name,
            "address": f"GPIB{GPIB_BOARD}::{SPECS['load'].addr}",
            "id_cmd": SPECS["load"].id_cmd,
            "connected": lc.load is not None,
        },
    }


@mcp.tool()
def raw_query(ctx: Context, target: str, command: str) -> str:
    """Invia un comando grezzo che attende una risposta.

    target: 'psu' | 'counter' | 'dmm' | 'load'
    command: stringa esatta da inviare.

    Nota: il 5334B usa comandi di query SENZA '?' (es. 'ID', 'TE', 'TC').
    Il 6632A e il 3457A usano comandi di query CON '?' (es. 'VOUT?', 'ID?', 'ERR?').
    Il 6060B è SCPI standard (es. '*IDN?', 'MEAS:VOLT?').
    """
    inst = {"psu": _psu, "counter": _counter, "dmm": _dmm, "load": _load}[target](ctx)
    return inst.query(command).strip()


@mcp.tool()
def raw_write(ctx: Context, target: str, command: str) -> str:
    """Invia un comando di scrittura grezzo (nessuna risposta attesa)."""
    inst = {"psu": _psu, "counter": _counter, "dmm": _dmm, "load": _load}[target](ctx)
    inst.write(command)
    return f"Inviato a {target}: {command}"


@mcp.tool()
def raw_read(ctx: Context, target: str) -> str:
    """Legge dal bus senza inviare alcun comando.

    Utile soprattutto sul 5334B che emette misure continuamente:
    dopo aver impostato la funzione (es. FN1) si legge direttamente.
    """
    inst = {"psu": _psu, "counter": _counter, "dmm": _dmm, "load": _load}[target](ctx)
    return inst.read().strip()


# ===========================================================================
# HP 6632A — Power Supply
# Riferimento: Operating Manual, Table 6-1.
# Range 6632A:
#   - Tensione: 0..20.475 V (risoluzione ~5 mV)
#   - Corrente: 0.02..5.1188 A (risoluzione ~1.25 mA, NON 0!)
#   - OVP:      0..22 V
# Formati di risposta:
#   - VOUT? -> SZD.DDD  (es. ' 5.000')
#   - IOUT? -> SD.DDDD  (es. ' 0.5000')
#   - ID?   -> 'HP6632A' (stringa)
#   - ERR?, STS?, FAULT?, ASTS?, TEST? -> ZZZZD (intero con padding spaces)
# ===========================================================================
@mcp.tool()
def psu_identify(ctx: Context) -> str:
    """Identificazione del PSU (ID?). Risponde 'HP6632A'."""
    return _psu(ctx).query("ID?").strip()


@mcp.tool()
def psu_set_voltage(ctx: Context, volts: float) -> str:
    """Imposta la tensione di uscita (VSET). Range 0-20.475 V.

    Il setpoint NON è leggibile dallo strumento (il 6632A non implementa
    VSET?). Usa psu_read_voltage per misurare la tensione effettiva."""
    if not 0.0 <= volts <= 20.475:
        raise ValueError("Tensione fuori range (0-20.475 V per il 6632A).")
    _psu(ctx).write(f"VSET {volts:.4f}")
    return f"PSU: VSET = {volts:.4f} V"


@mcp.tool()
def psu_set_current_limit(ctx: Context, amps: float) -> str:
    """Imposta il limite di corrente (ISET). Range 0.02-5.1188 A.

    ATTENZIONE: il 6632A non accetta 0 A esatti (minimo ~0.02 A).
    Se si chiede 0, lo strumento lo sostituisce silenziosamente con il
    minimo programmabile. Il setpoint NON è leggibile (no ISET?)."""
    if not 0.02 <= amps <= 5.1188:
        raise ValueError("Corrente fuori range (0.02-5.1188 A per il 6632A).")
    _psu(ctx).write(f"ISET {amps:.4f}")
    return f"PSU: ISET = {amps:.4f} A"


@mcp.tool()
def psu_set_overvoltage(ctx: Context, volts: float) -> str:
    """Imposta la soglia OVP (Over-Voltage Protection). Range 0-22 V.

    Se la tensione di uscita supera questa soglia, l'OVP scatta, manda in
    cortocircuito l'uscita (SCR crowbar) e disabilita l'alimentatore.
    Riarmare con psu_reset_protection dopo aver rimosso la causa."""
    if not 0.0 <= volts <= 22.0:
        raise ValueError("OVP fuori range (0-22 V per il 6632A).")
    _psu(ctx).write(f"OVSET {volts:.4f}")
    return f"PSU: OVSET = {volts:.4f} V"


@mcp.tool()
def psu_set_overcurrent_protect(ctx: Context, enabled: bool) -> str:
    """Abilita/disabilita la protezione di sovracorrente (OCP, 0/1).

    Quando abilitata, se l'uscita passa da CV a CC l'OCP scatta e
    disabilita l'uscita. Riarmare con psu_reset_protection."""
    _psu(ctx).write(f"OCP {1 if enabled else 0}")
    return f"PSU: OCP = {'ON' if enabled else 'OFF'}"


@mcp.tool()
def psu_output(ctx: Context, on: bool) -> str:
    """Accende (on=True, OUT 1) o spegne (on=False, OUT 0) l'uscita.

    Lo stato non è leggibile direttamente (no OUT?), ma può essere
    dedotto dal registro di stato (psu_status_register)."""
    _psu(ctx).write(f"OUT {1 if on else 0}")
    return f"PSU: OUT = {'ON' if on else 'OFF'}"


@mcp.tool()
def psu_read_voltage(ctx: Context) -> float:
    """Legge la tensione misurata sui morsetti (VOUT?). Volt."""
    return float(_psu(ctx).query("VOUT?").strip())


@mcp.tool()
def psu_read_current(ctx: Context) -> float:
    """Legge la corrente erogata (IOUT?). Ampere."""
    return float(_psu(ctx).query("IOUT?").strip())


@mcp.tool()
def psu_measurements(ctx: Context) -> dict:
    """Riassunto delle misure correnti del PSU.

    Sostituisce il vecchio psu_status: usa solo i query effettivamente
    supportati dal 6632A. I setpoint NON sono interrogabili."""
    inst = _psu(ctx)
    vout = float(inst.query("VOUT?").strip())
    iout = float(inst.query("IOUT?").strip())
    return {
        "vout_V": vout,
        "iout_A": iout,
        "power_W": vout * iout,
        "note": "il 6632A non permette la lettura di setpoint o stato OUT",
    }


# Mappa bit dello Status Register per il 6632A (manuale sez. 6-18)
_PSU_STATUS_BITS = {
    "CV":          0x001,
    "+CC":         0x002,
    "unregulated": 0x004,
    "OVP":         0x008,
    "OT":          0x010,
    "AC_fail":     0x020,
    "foldback":    0x040,
    "error":       0x080,
    "inhibited":   0x100,
    "-CC":         0x200,
    "OCP":         0x400,
    "fault":       0x800,
}


@mcp.tool()
def psu_status_register(ctx: Context) -> dict:
    """Legge il registro di stato del PSU (STS?) e ne decodifica i bit.

    Bit attivi tipici:
      - CV/+CC/-CC: modo operativo corrente
      - OVP/OCP: protezione scattata
      - OT, AC_fail: anomalie
      - foldback, inhibited, fault: altri stati di errore
    """
    raw = _psu(ctx).query("STS?").strip()
    try:
        value = int(raw)
    except ValueError:
        return {"raw": raw, "value": None, "decoded": {}}
    decoded = {k: bool(value & m) for k, m in _PSU_STATUS_BITS.items()}
    return {"raw": raw, "value": value, "decoded": decoded}


@mcp.tool()
def psu_fault_register(ctx: Context) -> dict:
    """Legge il registro fault del PSU (FAULT?).

    Indica quali bit di stato hanno causato un evento (es. OVP scattato).
    Si autoresetta dopo la lettura."""
    raw = _psu(ctx).query("FAULT?").strip()
    try:
        value = int(raw)
    except ValueError:
        return {"raw": raw, "value": None, "decoded": {}}
    decoded = {k: bool(value & m) for k, m in _PSU_STATUS_BITS.items()}
    return {"raw": raw, "value": value, "decoded": decoded}


# Codici di errore del 6632A (manuale sez. 6-22)
_PSU_ERROR_MESSAGES = {
    0:  "No error",
    1:  "EEPROM checksum error",
    2:  "EEPROM full",
    4:  "Invalid character",
    5:  "Invalid number",
    6:  "Invalid string",
    7:  "Buffer full",
    8:  "Numeric data not allowed",
    9:  "Data out of range",
    10: "Too many digits",
    11: "Header expected",
    12: "Unrecognized header",
    13: "EEPROM write failed",
    14: "Calibration error",
    15: "Cal switch prevents calibration",
    16: "Cal password incorrect",
    17: "Calibration not enabled",
    18: "Wrong type of value sent",
    19: "Wrong values for cal step",
}


@mcp.tool()
def psu_read_error(ctx: Context) -> dict:
    """Legge l'ultimo codice errore del PSU (ERR?) e lo decodifica.

    L'errore viene azzerato dopo la lettura."""
    raw = _psu(ctx).query("ERR?").strip()
    try:
        code = int(raw)
    except ValueError:
        return {"raw": raw, "code": None, "message": "risposta non parsabile"}
    return {
        "raw": raw,
        "code": code,
        "message": _PSU_ERROR_MESSAGES.get(code, f"Codice {code} (vedi manuale)"),
    }


@mcp.tool()
def psu_reset_protection(ctx: Context) -> str:
    """Resetta OVP/OCP dopo uno scatto di protezione (RST).

    Usare DOPO aver rimosso la causa (es. ridotto la tensione sotto OVP
    o aumentato il limite di corrente). Non è un reset generale."""
    _psu(ctx).write("RST")
    return "PSU: RST inviato (reset protezioni OVP/OCP)."


@mcp.tool()
def psu_reset_to_defaults(ctx: Context) -> str:
    """Reset completo del PSU ai valori di power-on (CLR).

    Questo è il vero 'reset' del 6632A: tutti i parametri tornano ai
    default di accensione (VSET=0, ISET=min, OUT=1, OCP=0, ecc.)."""
    inst = _psu(ctx)
    inst.clear()
    inst.write("CLR")
    return "PSU: CLR + Device Clear (reset ai default di power-on)."


@mcp.tool()
def psu_self_test(ctx: Context) -> dict:
    """Esegue il self-test del PSU (TEST?). 0 = OK."""
    raw = _psu(ctx).query("TEST?").strip()
    try:
        code = int(raw)
    except ValueError:
        return {"raw": raw, "passed": None}
    return {"raw": raw, "code": code, "passed": code == 0}


@mcp.tool()
def psu_display(ctx: Context, on: bool) -> str:
    """Accende o spegne il display frontale del PSU (DSP)."""
    _psu(ctx).write(f"DSP {1 if on else 0}")
    return f"PSU: DSP = {'ON' if on else 'OFF'}"


@mcp.tool()
def psu_ramp_voltage(
    ctx: Context,
    v_start: float,
    v_stop: float,
    v_step: float,
    dwell_s: float = 1.0,
    measure: bool = False,
) -> dict:
    """Esegue una rampa di tensione sul PSU con dwell time controllato.

    Tutta la rampa viene eseguita lato server in un singolo round-trip MCP:
    i time.sleep() avvengono in Python sul PC-LAB, quindi il timing è
    preciso e indipendente dalla latenza di rete client<->server.

    Gestisce sia rampe in salita (v_stop > v_start) che in discesa
    (v_stop < v_start). Se l'intervallo non è multiplo intero di v_step,
    l'ultimo punto viene aggiunto comunque per chiudere esattamente su v_stop.

    Args:
        v_start: tensione iniziale (V), range 0-20.475
        v_stop:  tensione finale (V), range 0-20.475
        v_step:  ampiezza dello step (V), > 0
        dwell_s: pausa tra uno step e il successivo (s), default 1.0.
                 Se 0, esegue alla massima velocità del bus GPIB.
        measure: se True, dopo ogni VSET legge VOUT?/IOUT? e li include
                 nei campioni (rallenta la rampa di ~30-50 ms/step).

    Returns:
        Dict con n_steps, v_start/v_stop effettivi, dwell_s, elapsed_s,
        e (se measure=True) lista samples con setpoint, misure, timestamp.

    Esempi:
        psu_ramp_voltage(0, 20, 1, dwell_s=1)
            -> 21 step di 1V con 1 secondo di pausa, ~20s totali
        psu_ramp_voltage(20, 0, 0.5, dwell_s=0.1, measure=True)
            -> discesa da 20 a 0 V con step 0.5V, dwell 100ms,
               registrando VOUT/IOUT a ogni step
        psu_ramp_voltage(0, 5, 0.1, dwell_s=0)
            -> rampa rapida 0->5V step 0.1V senza pause (test di slew rate)
    """
    import time
    if not 0.0 <= v_start <= 20.475:
        raise ValueError("v_start fuori range (0-20.475 V per il 6632A).")
    if not 0.0 <= v_stop <= 20.475:
        raise ValueError("v_stop fuori range (0-20.475 V per il 6632A).")
    if v_step <= 0:
        raise ValueError("v_step deve essere positivo.")
    if dwell_s < 0:
        raise ValueError("dwell_s non può essere negativo.")

    # Sanity check: evita rampe accidentali con migliaia di step
    n_intervals = int(round(abs(v_stop - v_start) / v_step))
    if n_intervals + 1 > 10000:
        raise ValueError(
            f"Rampa con {n_intervals + 1} step troppo lunga. "
            "Aumenta v_step o riduci il range."
        )

    inst = _psu(ctx)

    # Calcola la sequenza di setpoint, gestendo sia salite sia discese
    direction = 1.0 if v_stop >= v_start else -1.0
    voltages = [
        round(v_start + direction * i * v_step, 4)
        for i in range(n_intervals + 1)
    ]
    # Se la divisione non è esatta, includi comunque v_stop come ultimo punto
    if abs(voltages[-1] - v_stop) > v_step * 0.001:
        voltages.append(round(v_stop, 4))

    samples = []
    t0 = time.monotonic()
    for i, v in enumerate(voltages):
        inst.write(f"VSET {v:.4f}")
        if measure:
            vout = float(inst.query("VOUT?").strip())
            iout = float(inst.query("IOUT?").strip())
            samples.append({
                "vset_V": v,
                "vout_V": vout,
                "iout_A": iout,
                "t_s": round(time.monotonic() - t0, 3),
            })
        # Non sleepa dopo l'ultimo step (sarebbe attesa inutile)
        if i < len(voltages) - 1 and dwell_s > 0:
            time.sleep(dwell_s)
    elapsed = time.monotonic() - t0

    log.info(
        "Rampa completata: %d step da %.4f a %.4f V, dwell=%.3fs, totale=%.3fs",
        len(voltages), voltages[0], voltages[-1], dwell_s, elapsed,
    )

    return {
        "n_steps": len(voltages),
        "v_start": voltages[0],
        "v_stop": voltages[-1],
        "dwell_s": dwell_s,
        "elapsed_s": round(elapsed, 3),
        "measure": measure,
        "samples": samples if measure else None,
    }


@mcp.tool()
def psu_ramp_with_dmm(
    ctx: Context,
    v_start: float,
    v_stop: float,
    v_step: float,
    dwell_s: float = 1.0,
    dmm_max_volts: Optional[float] = None,
) -> dict:
    """Rampa di tensione sul PSU con misure parallele da PSU + DMM.

    Tutta la rampa viene eseguita lato server in un singolo round-trip MCP:
    i time.sleep() avvengono in Python sul PC-LAB, quindi il timing è preciso.

    Ad ogni step:
      1. imposta VSET sul PSU
      2. attende dwell_s secondi (stabilizzazione termica / elettrica)
      3. legge VOUT? e IOUT? dal PSU
      4. misura DCV con il DMM HP 3457A (più accurato, può essere
         collegato in 4-wire ai morsetti del carico per evitare la
         caduta sui cavi del PSU)

    Pattern utile per caratterizzazioni accurate dove si vuole il PSU come
    sorgente ma serve un voltmetro a parte per la misura di tensione.

    Args:
        v_start: tensione iniziale (V), range 0-20.475
        v_stop:  tensione finale (V), range 0-20.475
        v_step:  ampiezza dello step (V), > 0
        dwell_s: pausa per step (s), default 1.0
        dmm_max_volts: range DMM in V (None = autorange).
                       Per accelerare, fissarlo (es. 30 per range 0-30 V)
                       evita il tempo di autorange del DMM.

    Returns:
        Dict con n_steps, v_start/v_stop, dwell_s, elapsed_s, e lista
        samples (uno per step) con: vset_V, psu_vout_V, psu_iout_A,
        dmm_voltage_V, t_s (timestamp dall'inizio della rampa).
    """
    import time
    if not 0.0 <= v_start <= 20.475:
        raise ValueError("v_start fuori range (0-20.475 V per il 6632A).")
    if not 0.0 <= v_stop <= 20.475:
        raise ValueError("v_stop fuori range (0-20.475 V per il 6632A).")
    if v_step <= 0:
        raise ValueError("v_step deve essere positivo.")
    if dwell_s < 0:
        raise ValueError("dwell_s non può essere negativo.")
    if dmm_max_volts is not None and not 0 <= dmm_max_volts <= 300:
        raise ValueError("dmm_max_volts deve essere in [0, 300] V.")

    n_intervals = int(round(abs(v_stop - v_start) / v_step))
    if n_intervals + 1 > 10000:
        raise ValueError(
            f"Rampa con {n_intervals + 1} step troppo lunga. "
            "Aumenta v_step o riduci il range."
        )

    psu = _psu(ctx)
    dmm = _dmm(ctx)
    dmm_range = "AUTO" if dmm_max_volts is None else f"{dmm_max_volts}"

    direction = 1.0 if v_stop >= v_start else -1.0
    voltages = [
        round(v_start + direction * i * v_step, 4)
        for i in range(n_intervals + 1)
    ]
    if abs(voltages[-1] - v_stop) > v_step * 0.001:
        voltages.append(round(v_stop, 4))

    samples = []
    t0 = time.monotonic()
    for i, v in enumerate(voltages):
        psu.write(f"VSET {v:.4f}")
        if dwell_s > 0:
            time.sleep(dwell_s)
        # Dopo il dwell, misura tutto in sequenza
        psu_vout = float(psu.query("VOUT?").strip())
        psu_iout = float(psu.query("IOUT?").strip())
        dmm.write(f"DCV {dmm_range};TRIG SGL")
        dmm_v = float(dmm.read().strip())
        samples.append({
            "vset_V": v,
            "psu_vout_V": psu_vout,
            "psu_iout_A": psu_iout,
            "dmm_voltage_V": dmm_v,
            "t_s": round(time.monotonic() - t0, 3),
        })
    elapsed = time.monotonic() - t0

    log.info(
        "Rampa PSU+DMM completata: %d step da %.4f a %.4f V, dwell=%.1fs, totale=%.1fs",
        len(voltages), voltages[0], voltages[-1], dwell_s, elapsed,
    )

    return {
        "n_steps": len(voltages),
        "v_start": voltages[0],
        "v_stop": voltages[-1],
        "dwell_s": dwell_s,
        "elapsed_s": round(elapsed, 3),
        "samples": samples,
    }


# ===========================================================================
# HP 5334B — Universal Counter
# Riferimento: Operation & Programming Manual, Table 3-12 e sez. 3-333.
#
# DIALETTO HP PRE-SCPI con regole peculiari:
#   - 'ID' (SENZA '?') per identificazione
#   - Terminatori CR/LF (già configurati in SPECS)
#   - NESSUN comando di "leggi misura": il 5334B emette continuamente.
#     Dopo aver impostato la funzione, si esegue inst.read().
#     ATTENZIONE: 'IN' è "Initialize" (power-on), non "read"!
#
# FORMATO RISPOSTA (sez. 3-333):
#   <ALPHA><N spaces>±<digit>.<K digits>E±<2 digits>CR/LF
#   Es: 'F  1.234567E+06'  -> 1.234567 MHz
#       'V +0.523E+00'     -> 0.523 V
#   Carattere ALPHA iniziale indica il tipo di misura:
#     F=Frequenza, S=Tempo, V=Tensione, R=Ratio, T/t=Totalize,
#     A/B=Trigger Level A/B, H=Peaks
# ===========================================================================

# Mappa funzioni FN1..FN15 (Table 3-12) — CORRETTA rispetto al manuale.
COUNTER_FUNCTIONS = {
    "FREQ_A":             "FN1",   # Frequency A
    "FREQ_B":             "FN2",   # Frequency B
    "FREQ_C":             "FN3",   # Frequency C (canale alta freq, opz.)
    "PERIOD_A":           "FN4",   # Period A
    "TIME_INTERVAL_AB":   "FN5",   # Time Interval A -> B
    "TIME_INTERVAL_AB_D": "FN6",   # Time Interval A -> B con delay
    "RATIO_AB":           "FN7",   # Ratio A / B
    "TOTALIZE_STOP_A":    "FN8",   # Totalize Stop A
    "TOTALIZE_START_A":   "FN9",   # Totalize Start A
    "PULSE_WIDTH_A":      "FN10",  # Pulse Width A
    "RISE_FALL_TIME_A":   "FN11",  # Rise/Fall Time A
    "DVM":                "FN12",  # Voltage Mode (DVM integrato)
    "READ_TRIG_LEVELS":   "FN13",  # Read A and B Trigger Levels
    "READ_PEAKS_A":       "FN14",  # Read Channel A ± peaks
    "READ_PEAKS_B":       "FN15",  # Read Channel B ± peaks
}

# Unità inferite dal carattere ALPHA (sez. 3-335)
_COUNTER_UNITS = {
    "F": "Hz", "S": "s", "V": "V", "R": "",
    "T": "count", "t": "count",
    "A": "V", "B": "V", "H": "V",
}


def _parse_counter_reading(raw: str) -> dict:
    """Parsa l'output del 5334B in (alpha, value, unit_hint).

    Es: 'F  1.234567E+06' -> alpha='F', value=1234567.0, unit_hint='Hz'
    """
    raw = raw.strip()
    out = {"raw": raw, "alpha": None, "value": None, "unit_hint": None}
    if not raw:
        return out
    alpha = raw[0]
    rest = raw[1:].strip()
    out["alpha"] = alpha
    out["unit_hint"] = _COUNTER_UNITS.get(alpha)
    try:
        out["value"] = float(rest)
    except ValueError:
        out["value"] = None
    return out


@mcp.tool()
def counter_identify(ctx: Context) -> str:
    """Identificazione del contatore (ID, SENZA '?'). Risponde 'HP5334B'."""
    return _counter(ctx).query("ID").strip()


@mcp.tool()
def counter_set_function(ctx: Context, function: str) -> str:
    """Imposta la funzione di misura.

    function: una di FREQ_A, FREQ_B, FREQ_C, PERIOD_A,
              TIME_INTERVAL_AB, TIME_INTERVAL_AB_D, RATIO_AB,
              TOTALIZE_STOP_A, TOTALIZE_START_A, PULSE_WIDTH_A,
              RISE_FALL_TIME_A, DVM, READ_TRIG_LEVELS,
              READ_PEAKS_A, READ_PEAKS_B
    """
    f = function.upper()
    if f not in COUNTER_FUNCTIONS:
        raise ValueError(
            f"Funzione non valida. Disponibili: {sorted(COUNTER_FUNCTIONS)}"
        )
    _counter(ctx).write(COUNTER_FUNCTIONS[f])
    return f"Counter: funzione = {f} ({COUNTER_FUNCTIONS[f]})"


@mcp.tool()
def counter_set_gate_time(ctx: Context, seconds: float) -> str:
    """Imposta il gate time (GA<num>). Range 0.001-99.999 s.

    Gate time più lungo = più cifre significative, ma misura più lenta."""
    if not 0.001 <= seconds <= 99.999:
        raise ValueError("Gate time fuori range (0.001-99.999 s).")
    _counter(ctx).write(f"GA{seconds:.3f}")
    return f"Counter: gate time = {seconds:.3f} s"


@mcp.tool()
def counter_autotrigger(ctx: Context, on: bool = True) -> str:
    """Abilita (on=True, AU1) o disabilita (on=False, AU0) l'autotrigger."""
    cmd = "AU1" if on else "AU0"
    _counter(ctx).write(cmd)
    return f"Counter: autotrigger = {'ON' if on else 'OFF'} ({cmd})"


@mcp.tool()
def counter_read(ctx: Context) -> dict:
    """Legge la misura corrente dal contatore.

    Il 5334B emette misure continuamente: basta leggere dal bus, non serve
    inviare un comando di trigger. Restituisce {raw, alpha, value, unit_hint}.
    """
    raw = _counter(ctx).read().strip()
    return _parse_counter_reading(raw)


@mcp.tool()
def counter_measure_frequency(ctx: Context, channel: str = "A") -> dict:
    """Misura rapida di frequenza su canale A, B o C (Hz)."""
    ch = channel.upper()
    if ch not in ("A", "B", "C"):
        raise ValueError("channel deve essere 'A', 'B' o 'C'.")
    cmd = {"A": "FN1", "B": "FN2", "C": "FN3"}[ch]
    inst = _counter(ctx)
    inst.write(cmd)
    raw = inst.read().strip()
    return _parse_counter_reading(raw)


@mcp.tool()
def counter_measure_period(ctx: Context) -> dict:
    """Misura rapida di periodo sul canale A (s) — FN4."""
    inst = _counter(ctx)
    inst.write("FN4")
    raw = inst.read().strip()
    return _parse_counter_reading(raw)


@mcp.tool()
def counter_measure_time_interval(ctx: Context) -> dict:
    """Misura time interval A->B (s) — FN5."""
    inst = _counter(ctx)
    inst.write("FN5")
    raw = inst.read().strip()
    return _parse_counter_reading(raw)


@mcp.tool()
def counter_measure_ratio_ab(ctx: Context) -> dict:
    """Misura rapporto A/B — FN7 (adimensionale)."""
    inst = _counter(ctx)
    inst.write("FN7")
    raw = inst.read().strip()
    return _parse_counter_reading(raw)


@mcp.tool()
def counter_measure_dc_voltage(ctx: Context) -> dict:
    """DVM: misura tensione DC su canale A (V) — FN12.

    Il 5334B integra un DVM per misure DC di routine sul probe A."""
    inst = _counter(ctx)
    inst.write("FN12")
    raw = inst.read().strip()
    return _parse_counter_reading(raw)


@mcp.tool()
def counter_set_input_a_coupling(ctx: Context, ac: bool) -> str:
    """Accoppiamento del canale A: AC (True, AA1) o DC (False, AA0)."""
    _counter(ctx).write("AA1" if ac else "AA0")
    return f"Counter: input A coupling = {'AC' if ac else 'DC'}"


@mcp.tool()
def counter_set_input_a_impedance_50ohm(ctx: Context, fifty_ohm: bool) -> str:
    """Impedenza canale A: 50Ω (True, AZ1) o 1MΩ (False, AZ0)."""
    _counter(ctx).write("AZ1" if fifty_ohm else "AZ0")
    return f"Counter: input A impedance = {'50 Ohm' if fifty_ohm else '1 MOhm'}"


@mcp.tool()
def counter_set_input_a_attenuation_x10(ctx: Context, x10: bool) -> str:
    """Attenuazione canale A: x10 (True, AX1) o x1 (False, AX0)."""
    _counter(ctx).write("AX1" if x10 else "AX0")
    return f"Counter: input A attenuation = {'x10' if x10 else 'x1'}"


@mcp.tool()
def counter_set_input_a_slope(ctx: Context, negative: bool) -> str:
    """Slope di trigger canale A: positivo (False, AS0) o negativo (True, AS1)."""
    _counter(ctx).write("AS1" if negative else "AS0")
    return f"Counter: input A slope = {'negative' if negative else 'positive'}"


@mcp.tool()
def counter_set_input_b_coupling(ctx: Context, ac: bool) -> str:
    """Accoppiamento del canale B: AC (True, BA1) o DC (False, BA0)."""
    _counter(ctx).write("BA1" if ac else "BA0")
    return f"Counter: input B coupling = {'AC' if ac else 'DC'}"


@mcp.tool()
def counter_set_input_b_impedance_50ohm(ctx: Context, fifty_ohm: bool) -> str:
    """Impedenza canale B: 50Ω (True, BZ1) o 1MΩ (False, BZ0)."""
    _counter(ctx).write("BZ1" if fifty_ohm else "BZ0")
    return f"Counter: input B impedance = {'50 Ohm' if fifty_ohm else '1 MOhm'}"


@mcp.tool()
def counter_set_input_a_filter(ctx: Context, on: bool) -> str:
    """Filtro 100 kHz su canale A: ON (FI1) o OFF (FI0).

    Utile per pulire segnali a bassa frequenza dal rumore HF."""
    _counter(ctx).write("FI1" if on else "FI0")
    return f"Counter: input A 100 kHz filter = {'ON' if on else 'OFF'}"


@mcp.tool()
def counter_initialize(ctx: Context) -> str:
    """Inizializza il contatore allo stato di power-on (IN).

    Equivale a un riavvio software: tutti i parametri tornano ai default.
    NON usare per leggere una misura — quello è counter_read."""
    _counter(ctx).write("IN")
    return "Counter: IN inviato (initialize, stato power-on)."


@mcp.tool()
def counter_reset(ctx: Context) -> str:
    """Reset del contatore (RE + Device Clear)."""
    inst = _counter(ctx)
    inst.clear()
    inst.write("RE")
    return "Counter: RE + Device Clear."


@mcp.tool()
def counter_read_error(ctx: Context) -> str:
    """Legge il codice di errore del contatore (TE - Transmit Error)."""
    return _counter(ctx).query("TE").strip()


@mcp.tool()
def counter_transmit_calibration(ctx: Context) -> str:
    """Trasmette i dati di calibrazione del contatore (TC)."""
    return _counter(ctx).query("TC").strip()


# ===========================================================================
# HP 3457A — Digital Multimeter 6.5 digit
# Riferimento: HP 3457A Quick Reference.
#
# DIALETTO PRE-SCPI con regole specifiche:
#   - 'ID?' CON '?' (come 6632A, diverso da 5334B)
#   - Header SEMPRE in MAIUSCOLO, parametri liberi
#   - Terminatori in ricezione: CR, LF, o ';' (separatore comandi)
#   - Output in ASCII (14 char + CR/LF), parsabile con float()
#   - Default dopo power-on: FUNC=DCV, NPLC=10, RANGE=AUTO, TRIG=AUTO
#
# WORKFLOW DI MISURA scelto qui:
#   Nel lifespan abbiamo già impostato 'TARM AUTO; NRDGS 1,AUTO; TRIG HOLD'.
#   Quindi NESSUNA misura parte automaticamente. Per ogni misura mandiamo:
#       "<FUNCTION> <range>; TRIG SGL"
#   Il TRIG SGL triggera UNA misura singola e poi torna in HOLD: garantisce
#   una lettura "fresca" ogni volta, senza dati stantii nel buffer.
# ===========================================================================
# HP 6060B — Electronic Load (SCPI)
# Carico elettronico DC programmabile: 60 V / 60 A / 300 W.
# Tre modi operativi: Constant Current (CC), Constant Voltage (CV),
# Constant Resistance (CR).
# Se *IDN? non risponde, verificare che lo strumento sia in lingua SCPI
# (pannello frontale → menu GPIB → LANG = SCPI, salvare, riavviare).
# ===========================================================================
LOAD_MODES = {"CURR", "VOLT", "RES"}


@mcp.tool()
def load_identify(ctx: Context) -> str:
    """Identificazione del carico elettronico (*IDN?)."""
    return _load(ctx).query("*IDN?").strip()


@mcp.tool()
def load_set_mode(ctx: Context, mode: str) -> str:
    """Imposta il modo operativo del carico.

    mode: 'CURR' (corrente costante CC), 'VOLT' (tensione costante CV),
          'RES' (resistenza costante CR)
    """
    m = mode.upper()
    if m not in LOAD_MODES:
        raise ValueError(f"Modo non valido. Disponibili: {sorted(LOAD_MODES)}")
    _load(ctx).write(f"MODE {m}")
    return f"Load: MODE = {m}"


@mcp.tool()
def load_set_current(ctx: Context, amps: float) -> str:
    """Imposta il setpoint di corrente (modo CC). Range 0-60 A sul 6060B."""
    if not 0.0 <= amps <= 60.0:
        raise ValueError("Corrente fuori range (0-60 A per il 6060B).")
    _load(ctx).write(f"CURR {amps:.4f}")
    return f"Load: CURR = {amps:.4f} A"


@mcp.tool()
def load_set_voltage(ctx: Context, volts: float) -> str:
    """Imposta il setpoint di tensione (modo CV). Range 0-60 V sul 6060B."""
    if not 0.0 <= volts <= 60.0:
        raise ValueError("Tensione fuori range (0-60 V per il 6060B).")
    _load(ctx).write(f"VOLT {volts:.4f}")
    return f"Load: VOLT = {volts:.4f} V"


@mcp.tool()
def load_set_resistance(ctx: Context, ohms: float) -> str:
    """Imposta il setpoint di resistenza (modo CR). Range 0.033-10000 Ω."""
    if not 0.033 <= ohms <= 10000.0:
        raise ValueError("Resistenza fuori range (0.033-10000 Ω per il 6060B).")
    _load(ctx).write(f"RES {ohms:.4f}")
    return f"Load: RES = {ohms:.4f} Ω"


@mcp.tool()
def load_set_current_range(ctx: Context, high_range: bool) -> str:
    """Range corrente del carico.

    high_range=True  → 0-60 A (range alto, meno risoluzione)
    high_range=False → 0-6  A (range basso, miglior risoluzione)
    """
    val = "HIGH" if high_range else "LOW"
    _load(ctx).write(f"CURR:RANG {val}")
    return f"Load: CURR:RANG = {val}"


@mcp.tool()
def load_input(ctx: Context, on: bool) -> str:
    """Accende (on=True, INP ON) o spegne (on=False, INP OFF) l'ingresso."""
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
    """Riassume lo stato del carico: modo, stato ingresso, misure correnti."""
    inst = _load(ctx)
    mode = inst.query("MODE?").strip()
    inp = inst.query("INP?").strip()
    v = float(inst.query("MEAS:VOLT?").strip())
    i = float(inst.query("MEAS:CURR?").strip())
    p = float(inst.query("MEAS:POW?").strip())
    return {
        "mode": mode,
        "input": "ON" if inp.startswith(("1", "ON")) else "OFF",
        "voltage_V": v,
        "current_A": i,
        "power_W": p,
    }


@mcp.tool()
def load_reset(ctx: Context) -> str:
    """Reset del carico (*RST + *CLS + Device Clear)."""
    inst = _load(ctx)
    inst.clear()
    inst.write("*CLS")
    inst.write("*RST")
    return "Load resettato (*RST + *CLS + Device Clear)."


@mcp.tool()
def load_errors(ctx: Context, max_errors: int = 10) -> list[str]:
    """Legge la coda degli errori SCPI del carico (SYST:ERR?).

    Si ferma al primo '0,...' (no error) o dopo max_errors letture.
    """
    inst = _load(ctx)
    errors: list[str] = []
    for _ in range(max_errors):
        err = inst.query("SYST:ERR?").strip()
        errors.append(err)
        if err.startswith(("0,", "+0,")):
            break
    return errors


# ---------------------------------------------------------------------------
# Avvio del server
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if MCP_TOKEN:
        log.info("Autenticazione bearer token: ABILITATA")
        original_app = mcp.streamable_http_app()
        wrapped = BearerAuthMiddleware(original_app, MCP_TOKEN)
        import uvicorn
        uvicorn.run(wrapped, host=HTTP_HOST, port=HTTP_PORT)
    else:
        log.info("Autenticazione: DISABILITATA (nessun MCP_TOKEN)")
        mcp.run(transport="streamable-http")
