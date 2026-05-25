<div align="right">

**[🇮🇹 Italiano](./guida_mcp_gpib_multistrumento_it.md) · 🇬🇧 English**

</div>

# Step-by-step guide: multi-instrument MCP server for an HP bench (GPIB via Contec + KI-VISA on Windows)

This guide takes you from a "freshly installed" Contec board to a working MCP server on **PC-LAB Windows** that exposes, in a single instance, the four instruments of the HP bench connected on the same GPIB bus. Every command is meant for **PowerShell**.

## 1. Solution architecture

```
[Claude Desktop on PC-CLIENT]
            |
            |   MCP protocol over Streamable HTTP (port 8000)
            v
   [PC-LAB Windows: Python MCP server]
            |
            |   pyvisa + KI-VISA
            v
   [Contec GPIB board PCI/USB]
            |
            |   IEEE-488 bus
            +---- HP 6632A   (PSU,     addr 5)
            +---- HP 6060B   (Load,    addr 2)
            +---- HP 5334B   (Counter, addr 3)
            +---- HP 3457A   (DMM,     addr 22)
```

The MCP server is a single Python process that keeps an open VISA session to each of the four instruments and exposes them through "speaking" MCP tools (`psu_set_voltage`, `load_measure_current`, `counter_measure_frequency`, `raw_query`, ...) instead of raw SCPI/HP commands.

Unlike the `tbs2204b/` server (which uses stdio), this server runs over **Streamable HTTP** on PC-LAB and is reached over the network by Claude Desktop installed on a different PC-CLIENT, because the Contec GPIB board physically lives on the lab PC.

## 2. Prerequisites

### Hardware

- PC-LAB Windows with a **Contec** GPIB board (PCI or USB) and the system drivers already installed (`CTSTGPIB.EXE` must see the board).
- The four instruments connected to the GPIB bus, with **distinct** addresses on their front panels.
- PC-CLIENT with Claude Desktop, on the same LAN as PC-LAB.

### Software on PC-LAB

- **Python 3.10 or later** (required by the MCP SDK).
- **KI-VISA** 64-bit (Keysight IO Libraries). **Do not** use `pyvisa-py`: it's pure Python and can't talk to Contec; pyvisa must use the system VISA.
- Python packages: `mcp[cli]`, `pyvisa`, `numpy<2`, `uvicorn`.

> `numpy<2` constraint: NumPy 2.x requires **X86_V2** CPU instructions (SSE4.x, POPCNT, ...) introduced by Intel Nehalem (2008). Older PC-LAB machines (Core 2, first-generation Atom, Pentium D) get a `RuntimeError` at import. NumPy 1.26 works everywhere. `pyvisa` imports numpy at module level even if we don't use it directly, so it's a mandatory dependency.

### PowerShell script execution permissions

To activate a virtualenv, PowerShell must be allowed to run `.ps1` scripts. Open PowerShell **as administrator** once and run:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

## 3. GPIB addresses of the instruments

Before continuing, verify and note each instrument's GPIB address from its front panel. Typical factory values and the keys/menus to change them:

| Instrument | Factory default | Key/menu | Address used in the lab |
|---|---|---|---|
| HP 6632A (PSU) | 5 | **Address** key | **5** |
| HP 6060B (Load) | 5 | **Address** menu under Local/Remote | **2** |
| HP 5334B (Counter) | 18 | **GP-IB Adrs** key | **3** |
| HP 3457A (DMM) | 22 | **ADDRESS** key | **22** |

The addresses must be **different** from one another. The ones in the right column are the server code's defaults (`hp-lab/server.py`); you can override them with environment variables at launch (see section 8).

> On the **HP 6060B**: if it doesn't respond to `*IDN?` in the section 5 test, it's probably in **Compatibility mode** instead of SCPI. On the front panel, GPIB menu → `LANG = SCPI`, save, restart the instrument.

## 4. Setting up the Python environment on PC-LAB

You have two equivalent options for the working folder:

- **A. You're using the lab repo.** Enter the `hp-lab/` subfolder: the `server.py` and `test_strumenti.py` files are already there.

  ```powershell
  cd "C:\Users\<your-user>\..\Server-MCP-strumentazione\hp-lab"
  ```

- **B. Start from scratch, no repo.** Create a working folder in your home directory, and later copy `server.py` and `test_strumenti.py` from the repo's folder.

  ```powershell
  cd $HOME
  mkdir mcp-gpib
  cd mcp-gpib
  ```

Create and activate a virtualenv:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

The prompt becomes `(.venv) PS C:\Users\...>`. Upgrade pip and install the dependencies:

```powershell
python -m pip install --upgrade pip
pip install "mcp[cli]" pyvisa "numpy<2" uvicorn
```

## 5. Connection test (before the MCP server)

Before launching the server, verify that every instrument responds. The repo already includes `test_strumenti.py` at the same level as `server.py`: it handles the Contec driver's **state-retention** bug (the first `open_resource` after a previous session sometimes fails with `INV_OBJECT`) by retrying with a freshly created Resource Manager.

By default the script tests **PSU, Load and Counter**. To include the DMM too, add an entry to the `INSTRUMENTS` dictionary:

```python
INSTRUMENTS = {
    "HP 6632A (PSU)":     {"addr": 5,  "id_cmd": "ID?"},
    "HP 6060B (Load)":    {"addr": 2,  "id_cmd": "*IDN?"},
    "HP 5334B (Counter)": {"addr": 3,  "id_cmd": "ID"},   # note: 'ID' WITHOUT '?'
    "HP 3457A (DMM)":     {"addr": 22, "id_cmd": "ID?"},  # 'ID?' WITH '?'
}
```

Adapt the addresses if they differ on your instruments. Run it:

```powershell
python .\test_strumenti.py
```

**Expected output:** four `OK -> ...` lines with the identification strings (`HP6632A`, `HEWLETT-PACKARD,6060B,...`, `HP5334B`, `HP3457A`).

If a single instrument fails while the others respond, check:

- the GPIB address on the panel (it might differ from the one in `INSTRUMENTS`);
- the GPIB cables and power (turned on);
- for the 6060B, that it's in **SCPI** language (see section 3).

> **Watch out for the identification commands**, they are not uniform:
> - HP 6632A → `ID?` (with `?`)
> - HP 3457A → `ID?` (with `?`)
> - HP 5334B → `ID` (**without** `?`, old pre-SCPI dialect)
> - HP 6060B → `*IDN?` (standard SCPI)

Once the four respond, you're ready for the server.

## 6. Useful commands for the four instruments

References: *HP 6632A Operating Manual* (Table 6-1), *HP 5334B Operation & Programming Manual* (Table 3-12 and §3-333), *HP 3457A Quick Reference*, *HP 6060B Programming Guide*.

### HP 6632A — Power Supply (pre-SCPI)

| Purpose | Command |
|---|---|
| Identification | `ID?` → `HP6632A` |
| Voltage setpoint | `VSET <V>` (range 0..20.475 V) |
| Current setpoint | `ISET <A>` (range 0.02..5.1188 A; **the minimum is NOT 0**) |
| OVP | `OVSET <V>` (0..22 V) |
| Output | `OUT 0` / `OUT 1` |
| Measurements | `VOUT?`, `IOUT?` |
| Status / errors | `STS?`, `FAULT?`, `ERR?`, `TEST?` |
| Reset protections | `RST` (re-arms OVP/OCP) |
| Full reset | `CLR` |

> **6632A limitation**: the setpoints are **not readable** (no `VSET?`, `ISET?`, `OUT?`). To know the real voltage/current you use `VOUT?`/`IOUT?`. The output state can only be inferred from the status register.

### HP 6060B — Electronic Load (SCPI)

| Purpose | Command |
|---|---|
| Identification | `*IDN?` |
| Operating mode | `MODE CURR` / `MODE VOLT` / `MODE RES` |
| Setpoint | `CURR <A>`, `VOLT <V>`, `RES <Ω>` |
| Current range | `CURR:RANG HIGH` (0..60 A) / `CURR:RANG LOW` (0..6 A) |
| Input | `INP ON` / `INP OFF` |
| Measurements | `MEAS:VOLT?`, `MEAS:CURR?`, `MEAS:POW?` |
| Errors | `SYST:ERR?` |
| Reset | `*RST`, `*CLS` |

### HP 5334B — Universal Counter (pre-SCPI)

**CR/LF** terminators. There is NO "read measurement" command: the 5334B emits **continuously**; after setting the function you do a `read()` on the bus.

| Purpose | Command |
|---|---|
| Identification | `ID` (**without** `?`) → `HP5334B` |
| Functions FN1..FN15 | `FN1`=Freq A, `FN2`=Freq B, `FN3`=Freq C, `FN4`=Period A, `FN5`=Time int. A→B, `FN7`=Ratio A/B, `FN10`=Pulse Width A, `FN11`=Rise/Fall Time A, `FN12`=DVM, `FN13`=Trig levels, `FN14/15`=Peaks A/B |
| Gate time | `GA<n>` (0.001..99.999 s) |
| Autotrigger | `AU1` / `AU0` |
| Channel A: coupling, impedance, attenuation, slope, filter | `AA0/1`, `AZ0/1`, `AX0/1`, `AS0/1`, `FI0/1` |
| Channel B: coupling, impedance | `BA0/1`, `BZ0/1` |
| Initialize (NOT a read!) | `IN` |
| Error / calibration | `TE`, `TC` |
| Reset | `RE` |

> Classic trap: `IN` means **Initialize** (power-on state), **not** "Input/read". To read a measurement you do `inst.read()` (or `query()` after setting the function, since the 5334B keeps emitting samples).

> Response format: `<ALPHA><spaces>±<digit>.<...>E±<2 digits>CR/LF`. The leading `ALPHA` character indicates the type: `F`=Frequency, `S`=Time, `V`=Voltage, `R`=Ratio, `T`/`t`=Totalize, `A`/`B`=Trigger Level, `H`=Peaks.

### HP 3457A — Digital Multimeter 6.5 digit (pre-SCPI)

Terminators: **CR/LF** on receive, `\n` on transmit. Headers always UPPERCASE, free-form parameters. `;` separates multiple commands on a single line.

| Purpose | Command |
|---|---|
| Identification | `ID?` (with `?`, like the 6632A) |
| Measurement functions | `DCV [range]`, `ACV`, `DCI`, `ACI`, `OHM`, `OHMF` (4-wire), `FREQ`, `PER` |
| Trigger | `TARM AUTO; NRDGS 1,AUTO; TRIG HOLD` then `TRIG SGL` for a single measurement |
| Output format | `OFORMAT ASCII` |
| Integration | `NPLC <n>` (1..100) |
| Self-test | `TEST` |

At startup the server applies `TARM AUTO; NRDGS 1,AUTO; TRIG HOLD; OFORMAT ASCII`: no measurement starts automatically; to get a fresh reading you do `"<FUNC>; TRIG SGL"` then `read()`.

## 7. Structure of the MCP server

The server uses **FastMCP** in Streamable HTTP mode. The code is in [`../server.py`](../server.py) (i.e. `hp-lab/server.py` relative to the repo root). **We don't reproduce the full source here** to keep the guide and the implementation from drifting out of sync: the file is the source of truth. Below we explain the architecture and the important points to know before using or modifying it.

### Configuration (environment variables)

```python
GPIB_BOARD   = os.environ.get("GPIB_BOARD",   "0")
PSU_ADDR     = os.environ.get("PSU_ADDR",     "5")    # HP 6632A
COUNTER_ADDR = os.environ.get("COUNTER_ADDR", "3")    # HP 5334B
DMM_ADDR     = os.environ.get("DMM_ADDR",     "22")   # HP 3457A
LOAD_ADDR    = os.environ.get("LOAD_ADDR",    "2")    # HP 6060B
HTTP_HOST    = os.environ.get("MCP_HOST",     "0.0.0.0")
HTTP_PORT    = int(os.environ.get("MCP_PORT", "8000"))
MCP_TOKEN    = os.environ.get("MCP_TOKEN")            # optional, bearer auth
MCP_LOG_DIR  = os.environ.get("MCP_LOG_DIR",  <server.py dir>)
```

The defaults are already those of the lab bench, so in production you typically don't need to set anything. `MCP_TOKEN` enables bearer authentication (see section 12).

### VISA sessions: tolerant opening in `lab_lifespan`

At process startup, `_open()` opens each instrument with **up to 3 attempts** (handling the Contec driver's state-retention bug). If one instrument doesn't respond, the server **starts anyway** with the other three: the tools for the missing one raise a `RuntimeError` with a diagnostic message only when actually called. Each instrument uses its own terminators and timeouts declared in `SPECS` (in particular the counter has a **15 s** timeout because long gate times can take seconds for a single measurement).

On the DMM, after opening, the sequence `TARM AUTO; NRDGS 1,AUTO; TRIG HOLD; OFORMAT ASCII` is applied so that every subsequent measurement is "fresh" and starts only on `TRIG SGL`.

### File logging

The server writes to the console **and** to `server.log` next to `server.py` (RotatingFileHandler, max 5 MB × 5 files). Useful for lab audit and for analyzing afterwards what the instrument sent. The path can be changed with `MCP_LOG_DIR`.

### Exposed tools

#### Generic / introspection

| Tool | What it does |
|---|---|
| `list_instruments` | Lists the 4 instruments, their GPIB address and `connected: bool`. |
| `raw_query(target, command)` | Raw query. `target` ∈ `psu`/`counter`/`dmm`/`load`. **The only way to query the DMM** (it has no dedicated tools). |
| `raw_write(target, command)` | Raw write. |
| `raw_read(target)` | A pure `read()` from the bus without sending commands (useful for the 5334B, which emits continuously). |

#### HP 6632A — PSU

| Tool | What it does |
|---|---|
| `psu_identify` | `ID?` → `HP6632A`. |
| `psu_set_voltage(volts)` | `VSET` (0..20.475 V). |
| `psu_set_current_limit(amps)` | `ISET` (0.02..5.1188 A). |
| `psu_set_overvoltage(volts)` | `OVSET` (0..22 V). |
| `psu_set_overcurrent_protect(enabled)` | `OCP 0/1`. |
| `psu_output(on)` | `OUT 0/1`. |
| `psu_read_voltage` / `psu_read_current` | `VOUT?` / `IOUT?` as a float. |
| `psu_measurements` | Summary: vout, iout, power. |
| `psu_status_register` / `psu_fault_register` | `STS?` / `FAULT?` decoded by bit (CV, +CC, -CC, OVP, OCP, OT, foldback, ...). |
| `psu_read_error` | `ERR?` with a human-readable message per code. |
| `psu_reset_protection` | `RST`: re-arms OVP/OCP after a trip. |
| `psu_reset_to_defaults` | `CLR` + Device Clear: full reset to the power-on state. |
| `psu_self_test` | `TEST?`. |
| `psu_display(on)` | Turns the front-panel display on/off. |
| `psu_ramp_voltage(v_start, v_stop, v_step, dwell_s, measure)` | Linear ramp (down-ramps too). Executed entirely server-side: the `time.sleep()` calls happen on PC-LAB, so the timing is precise and independent of MCP latency. If `measure=True`, it records VOUT/IOUT at each step. |
| `psu_ramp_with_dmm(v_start, v_stop, v_step, dwell_s, dmm_max_volts)` | As above, but at each step it also reads the 3457A DMM (more accurate; it can be connected 4-wire at the load terminals to avoid the PSU lead drop). |

#### HP 6060B — Load

| Tool | What it does |
|---|---|
| `load_identify` | `*IDN?`. |
| `load_set_mode(mode)` | `CURR`/`VOLT`/`RES`. |
| `load_set_current(amps)` | 0..60 A. |
| `load_set_voltage(volts)` | 0..60 V. |
| `load_set_resistance(ohms)` | 0.033..10000 Ω. |
| `load_set_current_range(high_range)` | HIGH=0..60 A, LOW=0..6 A (better resolution). |
| `load_input(on)` | `INP ON/OFF`. |
| `load_measure_voltage/current/power` | `MEAS:VOLT?`/`MEAS:CURR?`/`MEAS:POW?`. |
| `load_status` | Summarizes mode, input, V/I/P. |
| `load_reset` | `*RST` + `*CLS` + Device Clear. |
| `load_errors(max_errors=10)` | Drains the `SYST:ERR?` queue down to `0,"No error"`. |

#### HP 5334B — Counter

| Tool | What it does |
|---|---|
| `counter_identify` | `ID` (without `?`). |
| `counter_set_function(function)` | One of `FREQ_A`, `FREQ_B`, `FREQ_C`, `PERIOD_A`, `TIME_INTERVAL_AB`, `TIME_INTERVAL_AB_D`, `RATIO_AB`, `TOTALIZE_START_A`, `TOTALIZE_STOP_A`, `PULSE_WIDTH_A`, `RISE_FALL_TIME_A`, `DVM`, `READ_TRIG_LEVELS`, `READ_PEAKS_A`, `READ_PEAKS_B`. |
| `counter_set_gate_time(seconds)` | `GA` (0.001..99.999 s). |
| `counter_autotrigger(on)` | `AU1`/`AU0`. |
| `counter_read` | A pure `read()`, parsed into `{raw, alpha, value, unit_hint}`. |
| `counter_measure_frequency(channel)` | Helper for FN1/FN2/FN3 + read. |
| `counter_measure_period` / `counter_measure_time_interval` / `counter_measure_ratio_ab` / `counter_measure_dc_voltage` | Wrappers over the most common functions. |
| `counter_set_input_a_coupling/impedance_50ohm/attenuation_x10/slope/filter` | Channel A configuration. |
| `counter_set_input_b_coupling/impedance_50ohm` | Channel B configuration. |
| `counter_initialize` | `IN`: power-on state. |
| `counter_reset` | `RE` + Device Clear. |
| `counter_read_error` / `counter_transmit_calibration` | `TE` / `TC`. |

#### HP 3457A — DMM

The DMM **has no dedicated tools**: you query it via `raw_query("dmm", ...)` / `raw_write("dmm", ...)` / `raw_read("dmm")`. The standard pattern for a single measurement is:

```python
raw_write("dmm", "DCV 30;TRIG SGL")   # 30 V range, fires ONE measurement
raw_read("dmm")                       # reads the result
```

Replace `DCV` with `ACV`, `DCI`, `ACI`, `OHM`, `OHMF`, `FREQ`, `PER` for other measurements. The DMM is also used internally by `psu_ramp_with_dmm`.

### Optional authentication

If `MCP_TOKEN` is set, an ASGI middleware (`BearerAuthMiddleware`) requires `Authorization: Bearer <token>` on every HTTP request; without a valid header it responds 401.

### If you need to modify the server

Edit `hp-lab/server.py` directly in the repo: the guide does not contain a copy of it.

## 8. Starting the server on PC-LAB

From the `hp-lab` folder with the venv active:

```powershell
# Optional: only if you need values different from the defaults
$env:GPIB_BOARD   = "0"
$env:PSU_ADDR     = "5"
$env:LOAD_ADDR    = "2"
$env:COUNTER_ADDR = "3"
$env:DMM_ADDR     = "22"
$env:MCP_HOST     = "0.0.0.0"
$env:MCP_PORT     = "8000"
# Optional: enable bearer authentication
# $env:MCP_TOKEN  = "your-secret-token"

python .\server.py
```

At startup the logs should show something like:

```
... INFO mcp-gpib: File logging: C:\...\hp-lab\server.log
... INFO mcp-gpib: VISA in use: <KI-VISA ...>
... INFO mcp-gpib: OK HP 6632A @ GPIB0::5::INSTR  -> HP6632A
... INFO mcp-gpib: OK HP 5334B @ GPIB0::3::INSTR  -> HP5334B
... INFO mcp-gpib: OK HP 3457A @ GPIB0::22::INSTR -> HP3457A
... INFO mcp-gpib: OK HP 6060B @ GPIB0::2::INSTR  -> HEWLETT-PACKARD,6060B,...
... INFO mcp-gpib: DMM configured for single triggered measurements
... INFO mcp-gpib: Authentication: DISABLED (no MCP_TOKEN)
... INFO:     Uvicorn running on http://0.0.0.0:8000
```

If one instrument is offline you'll see `ERROR ... Could not open ...` for that one only: the server starts anyway with the rest, and the relevant tools raise an explanatory error when called.

To stop: `Ctrl+C`.

## 9. Firewall configuration on PC-LAB

Once, from PowerShell as administrator:

```powershell
New-NetFirewallRule -DisplayName "MCP GPIB Server" `
  -Direction Inbound -Protocol TCP -LocalPort 8000 `
  -Action Allow -Profile Private
```

Find and note PC-LAB's IP:

```powershell
ipconfig | Select-String "IPv4"
```

## 10. Network test from PC-CLIENT

Replace the IP with PC-LAB's:

```powershell
Test-NetConnection -ComputerName 192.168.1.50 -Port 8000
```

You should see `TcpTestSucceeded : True`.

## 11. Connecting to Claude Desktop on PC-CLIENT

Install Node.js LTS from <https://nodejs.org>. Verify:

```powershell
node --version
npx --version
```

Open the Claude Desktop configuration:

```powershell
notepad $env:APPDATA\Claude\claude_desktop_config.json
```

Insert (adapt the IP and, if enabled, the token):

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

If you enabled the bearer token, add it:

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
        "MCP_TOKEN": "your-secret-token"
      }
    }
  }
}
```

Save and **restart Claude Desktop completely** (including from the systray icon). In the tools bar you should see the `hp-lab` server's tools.

At this point you can ask Claude things like:

- "List the HP bench instruments and tell me which are connected."
- "Set the PSU to 5 V with a 100 mA limit, turn the output on, then measure the actual voltage and current."
- "Configure the load in constant-current mode at 50 mA, turn the input on, and tell me how much power it's dissipating."
- "Measure the frequency on channel A of the counter with a 1 s gate time."
- "Ramp the PSU voltage from 0 to 10 V in 0.5 V steps with a 200 ms dwell, recording at each step the voltage measured by the DMM on the 30 V range."
- "Read the DC voltage from the DMM (30 V range)." → Claude will use `raw_write("dmm", "DCV 30;TRIG SGL")` + `raw_read("dmm")`.

For debugging you can also open the MCP Inspector from PC-CLIENT:

```powershell
npx @modelcontextprotocol/inspector
```

Transport: **Streamable HTTP**, URL `http://192.168.1.50:8000/mcp`.

## 12. Security notes

Without `MCP_TOKEN`, anyone on the LAN who can reach port 8000 can command the instruments. On a closed lab LAN this is generally acceptable, but the PSU can deliver 100 W and the load can dissipate 300: an unauthorized connection can cause physical damage.

Options in order of effort:

**1. Bind only on the right interface.** If PC-LAB has multiple network cards:

```powershell
$env:MCP_HOST = "192.168.1.50"   # the instrument LAN card's IP
```

**2. Enable the bearer token.** Generate a random token and configure it on server and client:

```powershell
# On PC-LAB
$env:MCP_TOKEN = (-join ((48..57) + (97..122) | Get-Random -Count 32 | % {[char]$_}))
$env:MCP_TOKEN   # print it, you need it for Claude Desktop
```

Then configure Claude as shown in section 11 with the `env` block.

**3. Reverse proxy with TLS.** For use outside the lab LAN, put Caddy or nginx in front with a certificate (even self-signed) and remove `--allow-http`.

## 13. Automatic startup as a Windows service (NSSM)

Keeping a PowerShell window open on PC-LAB isn't practical. Register the server as a Windows service with **NSSM**:

1. Download NSSM 64-bit from <https://nssm.cc/download>.
2. Extract `nssm.exe` to `C:\Tools\`.
3. PowerShell as administrator:

   ```powershell
   C:\Tools\nssm.exe install MCPGpibServer
   ```

4. In the GUI configure (replace `YOUR_USER` and the actual path):

   - **Application Path**: `C:\Users\YOUR_USER\...\hp-lab\.venv\Scripts\python.exe`
   - **Startup directory**: `C:\Users\YOUR_USER\...\hp-lab`
   - **Arguments**: `server.py`

5. **Environment** tab (one variable per line):

   ```
   GPIB_BOARD=0
   PSU_ADDR=5
   LOAD_ADDR=2
   COUNTER_ADDR=3
   DMM_ADDR=22
   MCP_HOST=0.0.0.0
   MCP_PORT=8000
   ```

   (and `MCP_TOKEN=...` if you want to enable authentication)

6. **I/O** tab (optional, since the server already writes to `server.log` on its own):

   - **Output (stdout)**: `C:\Users\YOUR_USER\...\hp-lab\nssm-stdout.log`
   - **Error (stderr)**: `C:\Users\YOUR_USER\...\hp-lab\nssm-stderr.log`

7. **Install service**, then:

   ```powershell
   Start-Service MCPGpibServer
   Get-Service MCPGpibServer
   ```

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| All instruments `Could not open` at startup | KI-VISA doesn't see the Contec board | Re-run `CTSTGPIB.EXE`, double-check that KI-VISA is 64-bit |
| Only HP 6060B fails | Load in Compatibility mode instead of SCPI | Front panel → GPIB menu → `LANG = SCPI`, save, restart |
| `*IDN?` on the 6060B returns a strange string | Old firmware or non-SCPI language | Check with `raw_query("load", "*IDN?")` |
| Counter times out | Long gate time + low timeout | `SPECS["counter"].timeout_ms` is already 15000. Increase it if you measure very low-frequency signals |
| Counter always returns 0 or an empty string | You sent `IN` expecting a reading: `IN` is **Initialize** | Use `counter_read` (which does `read()`) or `raw_read("counter")` |
| The DMM always returns the same value | Forgot `TRIG SGL`: without it, you read the previous measurement from the buffer | Always send `<FUNC> [range]; TRIG SGL` before `raw_read("dmm")` |
| `psu_measurements` returns 0 V with the output ON | Sense open or no load connected | Normal with no load; connect a load and retest |
| `psu_set_current_limit(0)` is "ignored" | The 6632A has a ~0.02 A minimum and silently substitutes it | Use at least 0.02 A, or turn the output off with `psu_output(False)` |
| `load_set_current` "out of range" error for a valid value | You're using the LOW range (0..6 A) with a setpoint > 6 A | Call `load_set_current_range(high_range=True)` |
| Claude sees the tools but calls fail with 401 | Missing or wrong bearer token on the client side | Check `env.MCP_TOKEN` in `claude_desktop_config.json` |
| `load_errors` always returns `-113,"Undefined header"` | A sent command isn't recognized by the 6060B | Probably in Compatibility mode: see above |
| The first open fails with `INV_OBJECT` after closing another Python process | Known Contec driver state-retention bug | `_open()` retries automatically up to 3 times; if it persists, wait a few seconds and restart the server |
| `OVP tripped` and the PSU no longer delivers | OVP fired, the output is short-circuited via the SCR | Remove the cause, then `psu_reset_protection` (`RST`) |

## 15. Possible extensions

When the base works, consider adding:

- **Dedicated tools for the HP 3457A DMM** (`dmm_measure_dc_voltage`, `dmm_measure_resistance`, `dmm_set_nplc`, ...). The pattern is the same as `psu_*`: a `_dmm` helper + calls to `query`/`write`.
- **HP 33120A** (function generator, SCPI): analogous to the 6060B. Add a `genfun_*` family.
- **Composite tools** ("sequence tools"): a single tool that runs a complete sequence, e.g. "load with rising current and measure the breakdown voltage", to avoid multiple MCP round-trips. `psu_ramp_voltage` and `psu_ramp_with_dmm` are already examples of this pattern.
- **CSV/Parquet saving** of the ramps directly to PC-LAB's disk, returning only the path to the client.
- **Structured audit logging**: every command sent and response is already in `server.log`, but you can add a JSON logger alongside it for later analysis.

---

With this base you have a complete HP measurement bench (PSU + Load + Counter + DMM) controllable in natural language from Claude. From here on it's just a matter of adding tools as you need new operations on the instruments.
