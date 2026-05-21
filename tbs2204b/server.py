"""Server MCP per oscilloscopio Tektronix TBS2204B."""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Callable, TypeVar

import numpy as np
import pyvisa
from mcp.server.fastmcp import FastMCP, Context

# ---------------------------------------------------------------------------
# Configurazione: IP e porta dello strumento da variabile d'ambiente
# ---------------------------------------------------------------------------
SCOPE_IP = os.environ.get("TBS2204B_IP", "192.168.0.75")
SCOPE_PORT = int(os.environ.get("TBS2204B_PORT", "4000"))
RESOURCE = f"TCPIP::{SCOPE_IP}::{SCOPE_PORT}::SOCKET"

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Gestione lazy della sessione VISA, con retry automatico su errore di rete
# ---------------------------------------------------------------------------
class ScopeConnection:
    """Wrapper che apre la sessione VISA al primo uso e la riusa fra chiamate.

    - Se l'oscilloscopio e' spento all'avvio di Claude Desktop il server MCP
      parte comunque: la connessione viene aperta alla prima invocazione di
      un tool, e un eventuale errore di rete non uccide il processo.
    - Se lo strumento viene spento e riacceso *dopo* che la sessione e' stata
      aperta, il metodo call() rileva la VisaIOError, invalida la cache e
      riapre la sessione automaticamente: il tool ricomincia a funzionare al
      primo tentativo successivo senza bisogno di chiamare reconnect a mano.
    """

    def __init__(self, resource: str) -> None:
        self.resource = resource
        self._rm: pyvisa.ResourceManager | None = None
        self._scope: pyvisa.resources.MessageBasedResource | None = None

    def get(self) -> pyvisa.resources.MessageBasedResource:
        if self._scope is not None:
            return self._scope
        if self._rm is None:
            self._rm = pyvisa.ResourceManager()
        try:
            scope = self._rm.open_resource(self.resource)
        except Exception as e:
            raise ConnectionError(
                f"Impossibile aprire la sessione VISA verso {self.resource}. "
                f"Verifica che l'oscilloscopio sia acceso e raggiungibile "
                f"(prova: Test-Connection {SCOPE_IP}, e che la porta {SCOPE_PORT} "
                f"sia aperta). Dettaglio: {e}"
            ) from e
        scope.timeout = 30000  # ms — più margine per record lunghi
        scope.read_termination = "\n"
        scope.write_termination = "\n"
        self._scope = scope
        return scope

    def close(self) -> None:
        if self._scope is not None:
            try:
                self._scope.close()
            except Exception:
                pass
            self._scope = None
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            self._rm = None

    def reconnect(self) -> pyvisa.resources.MessageBasedResource:
        self.close()
        return self.get()

    def call(self, fn: Callable[[pyvisa.resources.MessageBasedResource], T]) -> T:
        """Esegue fn(scope), riaprendo la sessione una volta su errore VISA.

        Tipico caso d'uso: lo strumento e' stato spento e riacceso, la cache
        contiene un socket morto, la prima query fallisce con VI_ERROR_TMO.
        Chiudiamo, riapriamo, riproviamo. Se anche il secondo tentativo
        fallisce vuol dire che lo strumento e' davvero irraggiungibile, e
        l'errore viene propagato al chiamante.
        """
        try:
            return fn(self.get())
        except (pyvisa.errors.VisaIOError, OSError) as e:
            print(
                f"[ScopeConnection] VisaIOError ({e}), invalido la cache e "
                f"riprovo una volta...",
                file=sys.stderr,
            )
            self.close()
            return fn(self.get())


@dataclass
class ScopeContext:
    conn: ScopeConnection


@asynccontextmanager
async def scope_lifespan(server: FastMCP) -> AsyncIterator[ScopeContext]:
    """Crea il wrapper all'avvio, senza aprire ancora la sessione VISA."""
    conn = ScopeConnection(RESOURCE)
    try:
        yield ScopeContext(conn=conn)
    finally:
        conn.close()


mcp = FastMCP("tbs2204b", lifespan=scope_lifespan)


def _call(ctx: Context, op: Callable[[pyvisa.resources.MessageBasedResource], T]) -> T:
    """Scorciatoia per eseguire un'operazione SCPI con retry automatico."""
    return ctx.request_context.lifespan_context.conn.call(op)


# ---------------------------------------------------------------------------
# Tool: identificazione, stato, gestione connessione
# ---------------------------------------------------------------------------
@mcp.tool()
def identify(ctx: Context) -> str:
    """Restituisce la stringa di identificazione dello strumento (*IDN?)."""
    return _call(ctx, lambda s: s.query("*IDN?").strip())


@mcp.tool()
def acquisition_state(ctx: Context) -> str:
    """Restituisce lo stato di acquisizione (RUN o STOP)."""
    state = _call(ctx, lambda s: s.query("ACQuire:STATE?").strip())
    return "RUN" if state == "1" else "STOP"


@mcp.tool()
def set_acquisition(ctx: Context, run: bool) -> str:
    """Avvia (run=True) o ferma (run=False) l'acquisizione."""
    cmd = f"ACQuire:STATE {'RUN' if run else 'STOP'}"
    _call(ctx, lambda s: s.write(cmd))
    return f"Acquisizione impostata a {'RUN' if run else 'STOP'}"


@mcp.tool()
def reconnect(ctx: Context) -> str:
    """Chiude e riapre la sessione VISA verso l'oscilloscopio.

    Normalmente non serve chiamarlo manualmente: il server riapre la
    connessione da solo al primo errore VISA. Usalo se vuoi forzare una
    riapertura (es. dopo aver cambiato cavo o IP) o per diagnostica.
    """
    conn = ctx.request_context.lifespan_context.conn
    conn.reconnect()
    return "Sessione VISA riaperta con successo."


# ---------------------------------------------------------------------------
# Tool: misure automatiche
# ---------------------------------------------------------------------------
ALLOWED_MEASUREMENTS = {
    "FREQ", "PERIOD", "AMPLITUDE", "MEAN", "RMS", "PK2PK",
    "MAXIMUM", "MINIMUM", "RISE", "FALL", "PWIDTH", "NWIDTH",
    "PDUTY", "NDUTY",
}


@mcp.tool()
def measure(ctx: Context, channel: int, measurement: str) -> dict:
    """Esegue una misura automatica.

    channel: numero del canale (1-4 per il TBS2204B)
    measurement: tipo di misura, es. 'FREQ', 'AMPLITUDE', 'RMS', 'PK2PK', ...
    """
    if channel not in (1, 2, 3, 4):
        raise ValueError("channel deve essere tra 1 e 4")
    measurement = measurement.upper()
    if measurement not in ALLOWED_MEASUREMENTS:
        raise ValueError(
            f"Misura '{measurement}' non supportata. "
            f"Disponibili: {sorted(ALLOWED_MEASUREMENTS)}"
        )

    def op(scope: pyvisa.resources.MessageBasedResource) -> dict:
        scope.write(f"MEASUrement:IMMed:SOUrce CH{channel}")
        scope.write(f"MEASUrement:IMMed:TYPe {measurement}")
        value = float(scope.query("MEASUrement:IMMed:VALue?"))
        units = scope.query("MEASUrement:IMMed:UNIts?").strip().strip('"')
        return {
            "channel": channel,
            "type": measurement,
            "value": value,
            "units": units,
        }

    return _call(ctx, op)


# ---------------------------------------------------------------------------
# Tool: download della forma d'onda
# ---------------------------------------------------------------------------
@mcp.tool()
def get_waveform(ctx: Context, channel: int, max_points: int = 2000) -> dict:
    """Scarica la waveform di un canale e restituisce tempo + tensione.

    Per evitare risposte enormi, di default vengono campionati al massimo
    2000 punti uniformemente distribuiti sul record.
    """
    if channel not in (1, 2, 3, 4):
        raise ValueError("channel deve essere tra 1 e 4")

    def op(scope: pyvisa.resources.MessageBasedResource) -> dict:
        # Configurazione del trasferimento
        scope.write(f"DATa:SOUrce CH{channel}")
        scope.write("DATa:ENCdg RIBinary")  # signed integer
        scope.write("DATa:WIDth 1")          # 1 byte: nativo ADC 8-bit, no endianness
        scope.write("DATa:STARt 1")
        record_len = int(scope.query("HORizontal:RECOrdlength?"))
        scope.write(f"DATa:STOP {record_len}")

        # Parametri di scala
        x_incr = float(scope.query("WFMOutpre:XINcr?"))
        x_zero = float(scope.query("WFMOutpre:XZEro?"))
        y_mult = float(scope.query("WFMOutpre:YMUlt?"))
        y_off = float(scope.query("WFMOutpre:YOFf?"))
        y_zero = float(scope.query("WFMOutpre:YZEro?"))

        # datatype 'b' = signed char (int8). WIDth 2 sul TBS2000 ha un bug di
        # byte order; WIDth 1 e' nativo dell'ADC e si trasferisce in 1 byte
        # per campione, scalando linearmente con la dimensione del record.
        raw = scope.query_binary_values(
            "CURVe?", datatype="b", container=np.array
        )

        # Conversione in unità reali
        voltage = (raw - y_off) * y_mult + y_zero
        time = x_zero + np.arange(len(raw)) * x_incr

        # Sottocampionamento se richiesto
        if max_points and len(raw) > max_points:
            idx = np.linspace(0, len(raw) - 1, max_points).astype(int)
            time = time[idx]
            voltage = voltage[idx]

        return {
            "channel": channel,
            "n_points": int(len(voltage)),
            "time_s": time.tolist(),
            "voltage_v": voltage.tolist(),
            "x_increment_s": x_incr,
            "y_multiplier_v": y_mult,
        }

    return _call(ctx, op)


# ---------------------------------------------------------------------------
# Tool: comando SCPI grezzo (utile per debug)
# ---------------------------------------------------------------------------
@mcp.tool()
def scpi_query(ctx: Context, command: str) -> str:
    """Invia un comando SCPI grezzo e restituisce la risposta.

    Usa solo comandi che terminano con '?' (query).
    """
    if not command.strip().endswith("?"):
        raise ValueError("Usa scpi_write per comandi senza '?'")
    return _call(ctx, lambda s: s.query(command).strip())


@mcp.tool()
def scpi_write(ctx: Context, command: str) -> str:
    """Invia un comando SCPI di scrittura (senza risposta)."""
    if "?" in command:
        raise ValueError("Usa scpi_query per comandi che ritornano valore")
    _call(ctx, lambda s: s.write(command))
    return f"Comando inviato: {command}"


if __name__ == "__main__":
    mcp.run()
