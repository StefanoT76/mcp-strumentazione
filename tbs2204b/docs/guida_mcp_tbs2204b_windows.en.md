<div align="right">

**[🇮🇹 Italiano](./guida_mcp_tbs2204b_windows_it.md) · 🇬🇧 English**

</div>

# Step-by-step guide: MCP server for the Tektronix TBS2204B oscilloscope over Ethernet (Windows 10 + PowerShell)

This guide takes you from an oscilloscope "fresh out of the box" to a working MCP server on **Windows 10**, using **PowerShell** as the shell. Every command is meant to be run in a standard PowerShell window (no elevated shell needed, except where noted).

## 1. Solution architecture

```
[Claude Desktop / MCP client]  <-- MCP protocol -->  [Python MCP server]
                                                              |
                                                       pyvisa + VISA
                                                              |
                                                    LAN / TCP-IP (LXI)
                                                              |
                                                  [Tektronix TBS2204B]
```

The TBS2204B is an LXI-compliant oscilloscope: it speaks **SCPI** (Standard Commands for Programmable Instruments) over TCP/IP. The MCP server is a small Python program that:

1. Manages a VISA session to the instrument (*lazy* open on the first invocation, automatic reconnect on a network error).
2. Exposes high-level functions as MCP *tools* (`identify`, `acquisition_state`, `set_acquisition`, `measure`, `get_waveform`, `reconnect`, `scpi_query`, `scpi_write`).
3. Receives requests from the MCP client, translates them into SCPI, and returns the results.

## 2. Prerequisites

### Hardware and network

- Tektronix TBS2204B oscilloscope with a rear Ethernet port.
- Ethernet cable between the oscilloscope and a network (router/switch) reachable from the Windows PC.
- Windows 10 PC with administrator access (needed only to install Python and, optionally, NI-VISA).

### Software

- **Python 3.10 or later** (required by the MCP SDK).
- **VISA backend**: the simplest path is `pyvisa-py` (pure Python, no heavy installers). Alternatively you can install **NI-VISA** or **TekVISA**, which on Windows offer better compatibility with older instruments and useful GUI tools (e.g. NI MAX).
- Python packages: `mcp[cli]`, `pyvisa`, `pyvisa-py`, `numpy`.

### Installing Python on Windows 10

If you don't already have it:

1. Download the installer from <https://www.python.org/downloads/windows/>.
2. **Important**: on the first installer screen tick **"Add python.exe to PATH"** before clicking *Install Now*.
3. Verify in PowerShell:

   ```powershell
   python --version
   pip --version
   ```

   If `python` is not found, close and reopen PowerShell (or reboot Windows) to reload the PATH.

### PowerShell script execution permissions

To activate a virtualenv, PowerShell must be allowed to run `.ps1` scripts. Open PowerShell **as administrator** and run this once:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Confirm with `Y` (Yes). From now on signed or locally created scripts (such as the venv's `Activate.ps1`) will run without errors.

## 3. Network configuration of the oscilloscope

On the TBS2204B:

1. Press the **Utility** button.
2. Go to the **I/O** menu → **Ethernet Network Settings**.
3. Set **DHCP ON** if you have a router that assigns addresses (recommended for the first bring-up), otherwise configure IP, subnet mask and gateway manually.
4. Note the **IP address** shown on screen. In the Bioskin lab it is configured as a **static IP `192.168.0.75`**: we'll use this value in all the examples in this guide.
5. From PowerShell, verify the instrument responds to ping:

   ```powershell
   Test-Connection -ComputerName 192.168.0.75 -Count 4
   # or the classic
   ping 192.168.0.75
   ```

> Tip: in production it's worth assigning a **static IP** or a **DHCP reservation** on the router, so the address doesn't change between sessions. The lab TBS2204B is already configured with a static IP.

### Windows Firewall

On the first connection Windows Defender may ask whether to allow Python on the network. Tick at least **"Private networks"** and confirm. If you don't see the prompt and timeouts persist, check manually under *Windows Security → Firewall & network protection → Allow an app through firewall*.

## 4. Setting up the Python environment

You have two equivalent options for the working folder:

- **A. You're using the lab repo.** Enter the `tbs2204b/` subfolder: the `server.py` file is already there.

  ```powershell
  cd "C:\Users\<your-user>\..\Server-MCP-strumentazione\tbs2204b"
  ```

- **B. Start from scratch, no repo.** Create a working folder in your home directory, and later copy `server.py` from the repo's folder.

  ```powershell
  cd $HOME
  mkdir tbs2204b-mcp
  cd tbs2204b-mcp
  ```

Create and activate a virtualenv:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

The prompt becomes `(.venv) PS C:\Users\...>`. Upgrade pip and install the dependencies:

```powershell
python -m pip install --upgrade pip
pip install "mcp[cli]" pyvisa pyvisa-py numpy
```

> If you prefer NI-VISA or TekVISA, download them from the respective vendor's site and install them *before* launching the script (a reboot is required). `pyvisa` will use them automatically.

## 5. Connection test (before the MCP server)

Before writing the MCP server, verify that VISA communication works. Create the file `test_connessione.py` in the working folder. From PowerShell you can do it on the fly with `notepad`:

```powershell
notepad test_connessione.py
```

Paste this content and save:

```python
import pyvisa

IP = "192.168.0.75"   # the TBS2204B's static IP in the lab
PORT = 4000           # the TBS2204B's raw-socket SCPI port

# Raw SOCKET transport: works with both pyvisa-py (pure Python) and
# NI-VISA / TekVISA, with no need for backend suffixes.
RESOURCE = f"TCPIP::{IP}::{PORT}::SOCKET"

rm = pyvisa.ResourceManager()
scope = rm.open_resource(RESOURCE)
scope.timeout = 10000           # ms
scope.read_termination = "\n"   # mandatory in SOCKET mode
scope.write_termination = "\n"

print("IDN:", scope.query("*IDN?"))
print("Acquisition:", scope.query("ACQuire:STATE?"))

scope.close()
```

Run it:

```powershell
python .\test_connessione.py
```

If you see a string like `TEKTRONIX,TBS2204B,...` communication works. If you get a timeout, check the IP, the firewall, and that the network interface is enabled on the instrument side.

> Note: the TBS2204B exposes SCPI on TCP port 4000 (raw socket). We use that instead of `TCPIP::IP::INSTR` because `pyvisa-py` has full SOCKET support without requiring NI-VISA, and the TBS2000 works reliably over this transport. The two line terminations are mandatory in SOCKET mode: without them `query` waits indefinitely.

## 6. Useful SCPI commands for the TBS2204B

Reference: *Tektronix TBS2000/TBS2000B Series Programmer Manual* (077-1149-xx). A selection of the most-used commands:

| Purpose | SCPI command |
|---|---|
| Instrument identification | `*IDN?` |
| Reset | `*RST` |
| Autoset | `AUTOSet EXECute` |
| Select channel for the waveform | `DATa:SOUrce CH1` |
| Waveform data format | `DATa:ENCdg RIBinary` |
| Byte width per sample | `DATa:WIDth 1` (native ADC; see the `get_waveform` note below) |
| Range of samples to download | `DATa:STARt 1` / `DATa:STOP 100000` |
| Waveform scaling parameters | `WFMOutpre?` |
| Read the waveform | `CURVe?` |
| Automatic measurement (e.g. CH1 amplitude) | `MEASUrement:IMMed:SOUrce CH1` + `MEASUrement:IMMed:TYPe AMPlitude` + `MEASUrement:IMMed:VALue?` |
| Acquisition state | `ACQuire:STATE?` |
| Start/stop acquisition | `ACQuire:STATE RUN` / `ACQuire:STATE STOP` |

To reconstruct the waveform in volts/seconds you need the parameters `XINcr`, `XZEro`, `YMUlt`, `YOFf`, `YZEro`, obtained with `WFMOutpre:XINCR?`, etc.

## 7. Structure of the MCP server

The server uses **FastMCP**, the high-level API of the Python SDK, and runs over `stdio`: Claude Desktop will launch it as a subprocess.

The server code is in the file [`../server.py`](../server.py) (i.e. `tbs2204b/server.py` relative to the repo root). **We don't reproduce the full source here** to keep the guide and the implementation from drifting out of sync: the file is the source of truth. Below we explain the architecture and the points that are important to understand before using or modifying it.

### Configuration (read from environment variables)

```python
SCOPE_IP   = os.environ.get("TBS2204B_IP",   "192.168.0.75")
SCOPE_PORT = int(os.environ.get("TBS2204B_PORT", "4000"))
RESOURCE   = f"TCPIP::{SCOPE_IP}::{SCOPE_PORT}::SOCKET"
```

The defaults are already those of the lab TBS2204B, so in production you don't need to set anything; the variables are useful only if you want to point at a different instrument or a different port.

### VISA session management: `ScopeConnection`

The key part of the server is the `ScopeConnection` class. Unlike the "naive" approach (`open_resource` in the lifespan, always reusing the same session), this class:

1. **Opens the session *lazily*** on the first tool call. As a result: if the oscilloscope is off when Claude Desktop starts, the MCP server starts anyway and the tools will fail with a clear message only when they are invoked.
2. **Reconnects automatically** on a `VisaIOError` or `OSError`: the typical scenario in which the cached socket is dead because the instrument was powered off and on again. The first query fails, the session is closed and reopened, and the same operation is retried once. The user notices nothing.
3. Exposes a `call(fn)` method that wraps this retry logic: every tool goes through it (via the `_call(ctx, op)` helper).

The timeout is set to **30 s** to allow headroom for long waveform records over `CURVe?` on SOCKET.

### Exposed tools

| Tool | What it does |
|---|---|
| `identify` | `*IDN?` — the instrument identification string. |
| `acquisition_state` | Returns `RUN` or `STOP`. |
| `set_acquisition(run: bool)` | Starts or stops the acquisition. |
| `reconnect` | Forces a close+reopen of the VISA session. Normally not needed (the retry is automatic); useful for diagnostics or after changing the IP. |
| `measure(channel, measurement)` | Automatic measurement (`FREQ`, `AMPLITUDE`, `RMS`, `PK2PK`, etc.). Whitelisted in `ALLOWED_MEASUREMENTS`. |
| `get_waveform(channel, max_points=2000)` | Downloads the curve in binary, converts it to volts/seconds and — if necessary — subsamples it to `max_points` so as not to overwhelm the client. |
| `scpi_query(command)` | Raw SCPI query (commands ending with `?`). |
| `scpi_write(command)` | Raw SCPI write command (without `?`). |

### Note on `get_waveform`: `DATa:WIDth 1`

The TBS2000 has a known byte-order bug when using `DATa:WIDth 2` (16-bit samples). For this reason `get_waveform` uses `DATa:WIDth 1` (the ADC's native 8 bits) with `datatype="b"` in `query_binary_values`: one byte per sample, no endianness ambiguity, and the transfer is faster too. The 8-bit precision is the instrument's real ADC precision, so nothing is lost compared to the acquired digital data.

### If you need to modify the server

Edit `tbs2204b/server.py` directly in the repo: the guide does not contain a copy of it.

## 8. Testing the server locally

Before connecting it to Claude, test it with the **MCP Inspector**. The server defaults (`192.168.0.75:4000`) are already those of the lab, so you typically don't need to set anything. If you want to point at a different instrument or port, export the environment variables before launching:

```powershell
# Optional: only if you need values different from the defaults
$env:TBS2204B_IP   = "192.168.0.75"
$env:TBS2204B_PORT = "4000"

mcp dev ..\server.py
```

> The command should be run from the `tbs2204b/docs/` folder pointing at the server with `..\server.py`, or directly from `tbs2204b/` with `mcp dev .\server.py`. Environment variables set with `$env:` only apply to the current PowerShell session. To make them permanent for the user:
>
> ```powershell
> [Environment]::SetEnvironmentVariable("TBS2204B_IP", "192.168.0.75", "User")
> ```
>
> but for use with Claude Desktop it's better to define them directly in `claude_desktop_config.json` (see the next section).

The Inspector opens a web interface (usually at `http://localhost:5173`) where you see the tools listed and can invoke them manually. Check that `identify` returns the right string and that `get_waveform` downloads a sensible curve (with a signal connected to CH1).

To exit, press `Ctrl+C` in the PowerShell window.

## 9. Connecting to Claude Desktop

On Windows, the Claude Desktop configuration file is at `%APPDATA%\Claude\claude_desktop_config.json`. Open it from PowerShell with:

```powershell
notepad $env:APPDATA\Claude\claude_desktop_config.json
```

If the file doesn't exist, Notepad will ask whether to create it: answer yes.

Add (or extend) the `mcpServers` section with the **absolute** paths to your Python and your `server.py`. To get them quickly:

```powershell
# Absolute path to the venv's Python
(Resolve-Path .\.venv\Scripts\python.exe).Path

# Absolute path to server.py
(Resolve-Path .\server.py).Path
```

Example configuration (adapt the paths to what the commands above print):

```json
{
  "mcpServers": {
    "tbs2204b": {
      "command": "C:\\Users\\YOUR_USER\\tbs2204b-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\YOUR_USER\\tbs2204b-mcp\\server.py"],
      "env": {
        "TBS2204B_IP": "192.168.0.75",
        "TBS2204B_PORT": "4000"
      }
    }
  }
}
```

> The `env` block is optional: if you omit `TBS2204B_IP` and `TBS2204B_PORT` the server uses the defaults (`192.168.0.75` and `4000`), which are already the lab values.

> Watch out: in JSON, Windows backslashes must be **doubled** (`\\`). Save the file and **restart Claude Desktop completely** (quit it from the systray icon too, not just the window). In the tools bar you should see the `tbs2204b` server's tools.

At this point you can ask Claude things like:

- "Identify the instrument and tell me whether it's acquiring."
- "Measure the frequency and peak-to-peak amplitude on CH1."
- "Download the CH2 waveform with 1000 points and tell me the computed RMS value."

## 10. Possible extensions

Once the base works, consider adding:

- **Screenshot** of the instrument's screen (`HARDCopy STARt` + reading the binary PNG block) and returning it as an MCP `Image`.
- **Trigger control**: dedicated tools for `TRIGger:A:LEVel`, `TRIGger:A:EDGE:SOUrce`, etc.
- **CSV save**: a tool that, instead of returning the waveform inline, saves it to disk and returns the path.
- **Cache**: for `WFMOutpre?` you can read all parameters in a single query and parse them locally, reducing latency.

## 11. Quick troubleshooting (Windows)

| Symptom | Likely cause | Fix |
|---|---|---|
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Restrictive execution policy | `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` (admin PowerShell) |
| `python` not recognized | PATH not updated after install | Restart PowerShell or reinstall Python with "Add to PATH" ticked |
| `VI_ERROR_TMO` on the first `*IDN?` | Wrong IP, wrong port, Windows firewall, scope not on the network | `Test-Connection 192.168.0.75`, check that port `4000` is reachable, verify the Utility → I/O menu. Remember that in `SOCKET` mode the line terminations are missing unless set. |
| `pyvisa` can't find the backend | Missing both NI-VISA and `pyvisa-py` | `pip install pyvisa-py` (it's already in the section 4 requirements) |
| The server starts but the first tool fails with `ConnectionError: Could not open the VISA session...` | The instrument is off or the IP/port are wrong. The server is designed to start anyway and fail only on invocation. | Turn the instrument on, or call the `reconnect` tool after fixing the configuration. |
| A tool was working, then starts timing out after the instrument was powered off/on | The cached session points to a dead socket | No action needed: the server reopens the session automatically on the next attempt. If it persists, call the `reconnect` tool. |
| "Noisy" or clipped waveform | Acquisition stopped or trigger not locked | Check `ACQuire:STATE?`, run `AUTOSet EXECute` |
| All measurement values are `9.9E37` | It's the SCPI "not a number": invalid measurement (no signal) | Check probe and coupling |
| Claude Desktop doesn't see the tools | Wrong paths or un-doubled backslashes in the JSON | Use `Resolve-Path`, double the `\\`, restart Claude from the systray |
| Permission errors on system folders | You put the project under `C:\Program Files\...` | Move the project to `C:\Users\YOUR_USER\...` |

---

With this base you have a clean, extensible MCP server, structured well enough to manage a TBS2204B in the lab from Windows 10. From here on it's just a matter of adding tools as you need new operations on the instrument.
