# MCP Strumentazione — contesto per Claude Code

Questo repo contiene due server MCP per strumentazione di laboratorio:

- `tbs2204b/` — server stdio per oscilloscopio Tektronix TBS2204B via Ethernet (LXI).
  IP statico dello strumento in laboratorio: 192.168.0.75.
  Backend VISA: pyvisa-py (Python puro).

- `hp-lab/` — server streamable-http per banco HP (PSU 6632A, e-load 6060B,
  counter 5334B) via GPIB su scheda Contec. Va eseguito su PC-LAB (Windows)
  con KI-VISA installato. Vincolo: numpy<2 (CPU vecchie).

## Convenzioni

- Lingua di commit, README, commenti nel codice: italiano.
- Messaggi di commit: `area: verbo all'imperativo`. Esempi:
  `tbs2204b: aggiungi tool screenshot`, `hp-lab: timeout counter a 15s`,
  `docs: corretto indirizzo GPIB di default`.
- Branching: `main` è sempre deployabile. Lavori non triviali su branch
  `feature/...` o `fix/...`, poi PR.
- Tag delle release in semver: `v0.1.0`, `v0.2.0`, ...
- Nessun segreto in repo (token, PAT, password): tutto in `.env` locale
  (gitignored). Eventuali esempi vanno in `.env.example` con valori finti.

## Cosa NON fare

- Non installare `numpy>=2` nel server hp-lab.
- Non installare `pyvisa-py` nel server hp-lab (deve usare KI-VISA).
- Non committare file in `.venv/`, `server.log`, `*.csv` generati.
- Non chiudere bug o feature senza prima allineare con il documento di
  riferimento in `docs/`.