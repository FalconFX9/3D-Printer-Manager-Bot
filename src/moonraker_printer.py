"""Printer driver for a Klipper/Moonraker printer (Sovol SV08), mirroring BambuPrinter's interface.

Moonraker is a plain HTTP API, so unlike the Bambu printers there is no persistent connection:
every call is a request to http://<ip>:7125. Requires:  pip install requests  (Pillow optional, for snapshots)
"""
import datetime
import io
import os
from urllib.parse import quote, urljoin

import requests

from src.constants import RUNNING
from src.printer import Printer

# Klipper print_stats.state -> the state names the bot uses (same vocabulary as BambuPrinter)
STATE_MAP = {
    "standby": "IDLE",
    "printing": "RUNNING",
    "paused": "PAUSE",
    "complete": "FINISH",
    "error": "FAILED",
    "cancelled": "FAILED",  # same as a Bambu print cancelled at the printer
}


class MoonrakerPrinter(Printer):
    def __init__(self, name, printer_ip, api_key=None, fake=False, port=7125,
                 filament_type=None, light_on_gcode=None, light_off_gcode=None,
                 webcam_name=None, timeout=10):
        """
        filament_type:   Klipper doesn't know what's loaded. Set this (e.g. "PLA") for the bot's filament
                         check, or use Spoolman (the active spool is then used instead).
        light_on_gcode / light_off_gcode:
                         G-code that switches the light, e.g. "SET_PIN PIN=caselight VALUE=1".
                         If omitted, a light is auto-detected from the printer's objects (best effort).
        webcam_name:     which Moonraker webcam to use for snapshots (default: the first enabled one).
        """
        super().__init__(name)
        self.name = name
        self.fake = fake
        self.printer_ip = printer_ip
        self.filament_type = filament_type
        self.webcam_name = webcam_name
        self.timeout = timeout
        self._light = (light_on_gcode, light_off_gcode) if light_on_gcode and light_off_gcode else None
        self._light_checked = self._light is not None
        self._objects = None
        self._spoolman_available = None
        if self.fake:
            return

        self.base_url = f"http://{printer_ip}:{port}"
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-Api-Key"] = api_key
        self.connect()

    # ---- HTTP helpers ----
    def _request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        resp = self.session.request(method, self.base_url + path, **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json()["error"]["message"]
            except Exception:
                detail = resp.text[:200]
            raise requests.HTTPError(f"{resp.status_code} {detail}", response=resp)
        return resp

    def _get(self, path):
        return self._request("GET", path).json()["result"]

    def _post(self, path, **kwargs):
        return self._request("POST", path, **kwargs).json().get("result")

    def _query(self, *objects):
        """Klipper object status, e.g. self._query('print_stats')['print_stats']['state']."""
        qs = "&".join(quote(o) for o in objects)
        return self._get(f"/printer/objects/query?{qs}")["status"]

    def _object_names(self):
        if self._objects is None:
            self._objects = self._get("/printer/objects/list")["objects"]
        return self._objects

    # ---- connection ----
    def connect(self):
        """Check that Moonraker answers. Never raises: the printer may simply be switched off for now."""
        if self.fake:
            return True
        try:
            info = self._get("/server/info")
            print(f"{self.name}: Moonraker {info.get('moonraker_version')}, Klippy {info.get('klippy_state')}")
            return True
        except Exception as e:
            print(f"{self.name}: can't reach Moonraker at {self.base_url} ({e}). Will retry on use.")
            return False

    def disconnect(self):
        if not self.fake:
            self.session.close()

    # ---- state / status ----
    @staticmethod
    def _state_from(status):
        webhooks = status.get("webhooks", {})
        if webhooks.get("state") not in (None, "ready"):
            return "ERROR"  # Klipper shut down / not ready: not in the bot's idle states
        return STATE_MAP.get(status["print_stats"].get("state"), "UNKNOWN")

    def get_state(self):
        """IDLE / RUNNING / PAUSE / FINISH / FAILED, or ERROR if Klipper itself is down."""
        if self.fake:
            return "IDLE"
        return self._state_from(self._query("print_stats", "webhooks"))

    def get_status_data(self):
        """Structured status for the Discord embed (same keys as BambuPrinter.get_status_data)."""
        if self.fake:
            return {"state": "RUNNING", "percentage": 10, "layer": 10, "total_layers": 100,
                    "bed_temp": 60, "nozzle_temp": 200, "remaining_minutes": 90}

        s = self._query("print_stats", "virtual_sdcard", "heater_bed", "extruder", "webhooks")
        state = self._state_from(s)
        stats = s["print_stats"]
        info = stats.get("info") or {}  # layer counts only exist if the slicer emits SET_PRINT_STATS_INFO
        progress = s["virtual_sdcard"].get("progress") or 0.0
        elapsed = stats.get("print_duration") or 0.0

        remaining = 0
        if state in ("RUNNING", "PAUSE") and progress > 0.01 and elapsed > 0:
            remaining = max(0, round((elapsed / progress - elapsed) / 60))

        return {
            "state": state,
            "percentage": round(progress * 100),
            "layer": info.get("current_layer") or 0,
            "total_layers": info.get("total_layer") or 0,
            "bed_temp": s["heater_bed"].get("temperature"),
            "nozzle_temp": s["extruder"].get("temperature"),
            "remaining_minutes": remaining,
        }

    def get_status(self):
        if self.fake:
            return f'''Printer status: RUNNING
            Layers: 10/100
            percentage: 10%
            Bed temp: 60 ºC
            Nozzle temp: 200 ºC
            Remaining time: 90m
            Finish time: {datetime.datetime.now() + datetime.timedelta(minutes=90)}'''

        d = self.get_status_data()
        remaining = d["remaining_minutes"]
        if remaining:
            finish = datetime.datetime.now() + datetime.timedelta(minutes=remaining)
            finish_time_format = finish.strftime("%Y-%m-%d %H:%M:%S")
        else:
            finish_time_format = "NA"

        def temp(value):
            return f"{value:.1f}" if isinstance(value, (int, float)) else "NA"

        return (
            f"Printer status: {d['state']}\n"
            f"Layers: {d['layer']}/{d['total_layers']}\n"
            f"Percentage: {d['percentage']}%\n"
            f"Bed temp: {temp(d['bed_temp'])} ºC\n"
            f"Nozzle temp: {temp(d['nozzle_temp'])} ºC\n"
            f"Remaining time: {remaining}m\n"
            f"Finish time: {finish_time_format}"
        )

    def get_errors(self):
        """Active error messages as strings."""
        if self.fake:
            return []
        s = self._query("print_stats", "webhooks")
        errors = []

        webhooks = s.get("webhooks", {})
        if webhooks.get("state") not in (None, "ready"):
            lines = (webhooks.get("state_message") or "").strip().splitlines()
            errors.append(f"Klipper {webhooks.get('state')}: {lines[0] if lines else 'no details'}"[:200])

        stats = s["print_stats"]
        if stats.get("state") == "error" and stats.get("message"):
            errors.append(f"Print error: {stats['message']}"[:200])
        return errors

    # ---- printing ----
    def upload_and_start_print(self, print_sub):
        if self.fake:
            print_sub.status = RUNNING
            return

        file_path = print_sub.print_attributes["file_path"]
        file_name = print_sub.print_attributes["file_name"]

        if not file_path.lower().endswith(".gcode"):
            print(f"Failed to upload file: {file_name} (Klipper needs a plain .gcode file)")
            return

        try:
            with open(file_path, "rb") as f:
                result = self._request("POST", "/server/files/upload",
                                       files={"file": (file_name, f)}, data={"root": "gcodes"},
                                       timeout=(5, 300)).json()
            remote_size = (result.get("item") or {}).get("size")
            if remote_size is not None and remote_size != os.path.getsize(file_path):
                print(f"Failed to upload file: {file_name} (size mismatch: {remote_size})")
                return
            self._post("/printer/print/start", json={"filename": file_name})
        except Exception as e:
            print(f"Failed to upload/start {file_name} on {self.name}: {e}")
            return  # status stays QUEUED, so the bot's retry logic takes over

        print_sub.status = RUNNING

    # ---- camera ----
    def _snapshot_url(self):
        try:
            webcams = self._get("/server/webcams/list")["webcams"]
        except Exception:
            webcams = []
        candidates = [w for w in webcams if w.get("enabled", True) and w.get("snapshot_url")]
        if self.webcam_name:
            candidates = [w for w in candidates if w.get("name") == self.webcam_name]
        url = candidates[0]["snapshot_url"] if candidates else "/webcam/?action=snapshot"
        # Relative webcam URLs are served by the web server (port 80), not by Moonraker (7125)
        return urljoin(f"http://{self.printer_ip}/", url)

    def save_image(self, out_path=None):
        """Save a webcam snapshot. Returns the path, or None if it couldn't be captured."""
        if self.fake:
            return None
        out_path = out_path or f"example_{self.name}.png"
        try:
            resp = self.session.get(self._snapshot_url(), timeout=self.timeout)
            resp.raise_for_status()
            try:
                from PIL import Image  # snapshots are usually JPEG; convert to match the file extension
                Image.open(io.BytesIO(resp.content)).save(out_path)
            except ImportError:
                with open(out_path, "wb") as f:
                    f.write(resp.content)
            print(f"Image saved: {out_path}")
            return out_path
        except Exception as e:
            print(f"Failed to capture frame from {self.name}: {e}")
            return None

    # ---- light ----
    def _light_gcodes(self):
        """(on, off) G-code for the light: configured, or auto-detected once from the printer's objects."""
        if not self._light_checked:
            self._light_checked = True
            try:
                self._light = self._detect_light()
            except Exception as e:
                print(f"{self.name}: light detection failed: {e}")
        return self._light

    def _detect_light(self):
        objects = self._object_names()
        for obj in objects:
            kind, _, name = obj.partition(" ")
            if kind == "output_pin" and any(k in name.lower() for k in ("light", "led", "lamp")):
                return f"SET_PIN PIN={name} VALUE=1", f"SET_PIN PIN={name} VALUE=0"
        for obj in objects:
            kind, _, name = obj.partition(" ")
            if kind in ("led", "neopixel", "dotstar") and name:
                return (f"SET_LED LED={name} RED=1 GREEN=1 BLUE=1 TRANSMIT=1",
                        f"SET_LED LED={name} RED=0 GREEN=0 BLUE=0 TRANSMIT=1")
        return None

    def _run_gcode(self, script):
        self._post("/printer/gcode/script", json={"script": script})

    def turn_light_on(self):
        if self.fake:
            return
        gcodes = self._light_gcodes()
        if gcodes:
            self._run_gcode(gcodes[0])
        else:
            print(f"{self.name}: no light found; pass light_on_gcode / light_off_gcode to MoonrakerPrinter")

    def turn_light_off(self):
        if self.fake:
            return
        gcodes = self._light_gcodes()
        if gcodes:
            self._run_gcode(gcodes[1])

    # ---- filament ----
    def _filament_missing(self):
        """True if a filament runout (switch) sensor reports no filament."""
        sensors = [o for o in self._object_names() if o.startswith("filament_switch_sensor ")]
        if not sensors:
            return False
        status = self._query(*sensors)
        return any(v.get("filament_detected") is False for v in status.values())

    def _spoolman_spool(self):
        """Active Spoolman spool as a filament dict, or None if Spoolman isn't set up / no active spool."""
        if self._spoolman_available is False:
            return None
        try:
            spool_id = self._get("/server/spoolman/spool_id").get("spool_id")
            if not spool_id:
                return None
            payload = self._post("/server/spoolman/proxy",
                                 json={"request_method": "GET", "path": f"/v1/spool/{spool_id}"})
            spool = payload.get("response", payload)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                self._spoolman_available = False  # Spoolman isn't configured in Moonraker
            return None
        except Exception:
            return None

        fil = spool.get("filament") or {}
        remaining, initial = spool.get("remaining_weight"), spool.get("initial_weight") or fil.get("weight")
        remain_pct = round(remaining / initial * 100) if remaining is not None and initial else None
        color = (fil.get("color_hex") or "")[:6]
        brand = " ".join(x for x in ((fil.get("vendor") or {}).get("name"), fil.get("name")) if x)
        return {
            "location": "Spool",
            "type": fil.get("material"),
            "brand": brand or None,
            "color": f"#{color.upper()}" if color else None,
            "remain_pct": remain_pct,
            "nozzle_temp": (None, fil.get("settings_extruder_temp")),
        }

    def get_filaments(self):
        """List of dicts, one per loaded spool (same shape as BambuPrinter.get_filaments)."""
        if self.fake:
            return [{"location": "Spool", "type": "PLA", "brand": "Generic PLA",
                     "color": "#FFFFFF", "remain_pct": 80}]

        if self._filament_missing():
            return []

        spool = self._spoolman_spool()
        if spool and spool.get("type"):
            return [spool]

        if self.filament_type:
            return [{"location": "Spool", "type": self.filament_type, "brand": None,
                     "color": None, "remain_pct": None, "nozzle_temp": (None, None)}]
        return []

    def get_filaments_text(self):
        items = self.get_filaments()
        if not items:
            return "No filament loaded"
        return "\n".join(
            f"{f['location']}: {f['type']}"
            + (f" ({f['brand']})" if f["brand"] else "")
            + (f", {f['color']}" if f["color"] else "")
            + (f", {f['remain_pct']}% left" if f["remain_pct"] not in (None, -1) else "")
            for f in items
        )