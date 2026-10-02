"""Discord-independent print logic: printer selection, filament and readiness checks."""
import re

# Printer states in which a new job may be sent
IDLE_STATES = {"IDLE", "FINISH", "FAILED"}


def resolve_printer_name(model):
    """Map the gcode's `printer_model` (e.g. 'Bambu Lab A1 mini') to a PrinterManager name."""
    m = re.sub(r"[^a-z0-9]", "", (model or "").lower())
    if "h2d" in m:
        return "H2D"
    if "a1mini" in m:
        return "A1mini"
    return None


def required_filament_types(attrs):
    """Filament types the sliced file needs, e.g. {'PLA'}."""
    used = attrs.get("filament_used") or {}
    keys = [k for k, grams in used.items() if grams and grams > 0] or list(used.keys())
    types = set()
    for key in keys:
        for t in re.split(r"[;,]", str(key)):
            if t.strip():
                types.add(t.strip().upper())
    return types


def printer_state(printer):
    """Current gcode state as an upper-case string ('IDLE', 'RUNNING', ...)."""
    if printer.fake:
        return "IDLE"
    state = printer.printer.get_state()
    return str(getattr(state, "name", state)).split(".")[-1].upper()


def check_ready(printer, attrs):
    """Blocking. Returns (ready, notice_key, message). The bed-clear check lives in the bot."""
    state = printer_state(printer)
    if state not in IDLE_STATES:
        return False, "busy", f"{printer.name} is busy (state: {state}). Waiting for it to be free."

    required = required_filament_types(attrs)
    loaded = {str(f["type"]).strip().upper() for f in printer.get_filaments() if f.get("type")}
    missing = required - loaded
    if missing:
        key = f"filament:{sorted(missing)}:{sorted(loaded)}"
        msg = (
            f"Filament mismatch on {printer.name}. This print needs "
            f"{', '.join(sorted(required))}, but the printer reports "
            f"{', '.join(sorted(loaded)) or 'nothing loaded'}. "
            "Load the right filament (and set its type on the printer) and I'll start it automatically."
        )
        return False, key, msg

    return True, "", ""


def fmt_duration(seconds):
    if not seconds or seconds < 0:
        return "unknown"
    h, rem = divmod(int(seconds), 3600)
    return f"{h}h {rem // 60}m" if h else f"{rem // 60}m"