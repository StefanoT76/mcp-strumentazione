"""Test di connessione con retry — gestisce il bug di state retention Contec."""
import time
import pyvisa

GPIB_BOARD = 0
MAX_ATTEMPTS = 4
RETRY_DELAY_S = 1.0

INSTRUMENTS = {
    "HP 6632A (PSU)":     {"addr": 5,  "id_cmd": "ID?"},
    "HP 6060B (Load)":    {"addr": 6,  "id_cmd": "*IDN?"},
    "HP 5334B (Counter)": {"addr": 14, "id_cmd": "ID?"},
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
    """Ritorna un nuovo Resource Manager, chiudendo eventuali residui."""
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