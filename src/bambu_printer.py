import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|timeout;5000000"

import bambulabs_api as bbl
import cv2
import datetime
import io
import time
import json

from src.constants import *
from src.printer import Printer
from src.bambu_ftps import upload_file as ftps_upload


class BambuPrinter(Printer):
    def __init__(self, name, printer_ip, access_code, serial, fake=False):
        super().__init__(name)
        self.name = name
        self.fake = fake
        self.printer_ip = printer_ip
        self.access_code = access_code
        self.serial = serial
        if self.fake:
            return
        self.printer = bbl.Printer(printer_ip, access_code, serial)
        self.printer = bbl.Printer(printer_ip, access_code, serial)
        if self._is_h2d() and hasattr(self.printer, "mqtt_start"):
            # H2D camera is RTSPS (handled via OpenCV), so skip the library's camera client
            self.printer.mqtt_start()
        else:
            self.printer.connect()

    def disconnect(self):
        if self.fake:
            return
        if self._is_h2d() and hasattr(self.printer, "mqtt_stop"):
            self.printer.mqtt_stop()
        else:
            self.printer.disconnect()

    def upload_and_start_print(self, print_sub):
        if self.fake:
            print_sub.status = RUNNING
            return

        file_path = print_sub.print_attributes["file_path"]
        file_name = print_sub.print_attributes["file_name"]

        if not ftps_upload(self.printer_ip, self.access_code, file_path, file_name):
            print(f"Failed to upload file: {file_name}")
            return  # status stays QUEUED, so the bot's retry logic takes over

        self.printer.start_print(file_name, plate_number=1)
        print_sub.status = RUNNING

    def _is_h2d(self):
        return self.name.upper().startswith("H2D")

    def _grab_frame_rtsp(self):
        """H2D: grab a single frame from the RTSPS stream on port 322."""
        url = f"rtsps://bblp:{self.access_code}@{self.printer_ip}:322/streaming/live/1"
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        try:
            if not cap.isOpened():
                return None
            frame = None
            # Skip a few frames: the first ones after connect are often
            # incomplete or grey until a keyframe arrives
            for _ in range(5):
                ok, f = cap.read()
                if ok:
                    frame = f
            return frame
        finally:
            cap.release()

    def _send_led(self, node, mode):
        payload = {
            "system": {
                "sequence_id": "0",
                "command": "ledctrl",
                "led_node": node,
                "led_mode": mode,  # "on" or "off"
                "led_on_time": 500,
                "led_off_time": 500,
                "loop_times": 0,
                "interval_time": 0,
            }
        }
        client = self.printer.mqtt_client
        # Attribute names vary between library versions, so try the likely ones
        paho = getattr(client, "_client", None) or getattr(client, "client", None)
        paho.publish(f"device/{self.serial}/request", json.dumps(payload))

    H2D_LIGHT_NODES = ["chamber_light", "chamber_light2", "work_light"]  # add e.g. "chamber_light2" if step 1 lists it

    def turn_light_on(self):
        if self.fake:
            return
        if self._is_h2d():
            for node in self.H2D_LIGHT_NODES:
                self._send_led(node, "on")
        else:
            self.printer.turn_light_on()

    def turn_light_off(self):
        if self.fake:
            return
        if self._is_h2d():
            for node in self.H2D_LIGHT_NODES:
                self._send_led(node, "off")
        else:
            self.printer.turn_light_off()

    def save_image(self, out_path=None):
        if self.fake:
            return None
        out_path = out_path or f"example_{self.name}.png"

        if self._is_h2d():
            frame = self._grab_frame_rtsp()
            if frame is not None and cv2.imwrite(out_path, frame):
                print(f"Image saved: {out_path}")
                return out_path
            print(f"Failed to capture frame from {self.name}")
            return None

        # Other printers: bambulabs_api's built-in camera support
        try:
            time.sleep(0.5)
            image = self.printer.get_camera_image()
            time.sleep(0.5)
            image.save(out_path)
            return out_path
        except Exception as e:
            print(f"Failed to capture frame from {self.name}: {e}")
            return None

    def get_errors(self):
        """Active error codes as strings, e.g. ['Print error 0300_4001', 'HMS_0300_0100_0001_0001']."""
        if self.fake:
            return []
        report = self.printer.mqtt_dump().get("print", {})
        errors = []

        try:
            pe = int(report.get("print_error") or 0)
        except (TypeError, ValueError):
            pe = 0
        if pe:
            errors.append(f"Print error {pe >> 16:04X}_{pe & 0xFFFF:04X}")

        for item in report.get("hms") or []:
            try:
                attr, code = int(item.get("attr", 0)), int(item.get("code", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            errors.append(f"HMS_{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}")
        return errors

    def get_filaments(self):
        """Return a list of dicts, one per externally loaded spool."""
        if self.fake:
            return [{"location": "External spool", "type": "PLA", "brand": "Bambu PLA Basic",
                    "color": "#FFFFFF", "remain_pct": 80}]

        report = self.printer.mqtt_dump().get("print", {})

        # H2D reports one external spool per nozzle in "vir_slot";
        # the A1 mini reports a single "vt_tray"
        external = report.get("vir_slot") or ([report["vt_tray"]] if "vt_tray" in report else [])

        filaments = []
        for tray in external:
            ftype = tray.get("tray_type")
            if not ftype:  # nothing loaded
                continue
            color = (tray.get("tray_color") or "")[:6]  # RRGGBBAA -> RRGGBB
            filaments.append({
                "location": f"External spool {tray.get('id', '')}".strip(),
                "type": ftype,
                "brand": tray.get("tray_sub_brands") or None,
                "color": f"#{color}" if color else None,
                "remain_pct": tray.get("remain"),
                "nozzle_temp": (tray.get("nozzle_temp_min"), tray.get("nozzle_temp_max")),
            })
        return filaments

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

    def get_status(self):
        if self.fake:
            return f'''Printer status: RUNNING
            Layers: 10/100
            percentage: 10%
            Bed temp: 60 ºC
            Nozzle temp: 200 ºC
            Remaining time: 90m
            Finish time: {datetime.datetime.now() + datetime.timedelta(minutes=90)}'''
        status = self.printer.get_state()
        percentage = self.printer.get_percentage()
        layer_num = self.printer.current_layer_num()
        total_layer_num = self.printer.total_layer_num()
        bed_temperature = self.printer.get_bed_temperature()
        nozzle_temperature = self.printer.get_nozzle_temperature()
        remaining_time = self.printer.get_time()
        if remaining_time is not None:
            finish_time = datetime.datetime.now() + datetime.timedelta(
                minutes=int(remaining_time))
            finish_time_format = finish_time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            finish_time_format = "NA"

        return (
            f"Printer status: {status}\n"
            f"Layers: {layer_num}/{total_layer_num}\n"
            f"Percentage: {percentage}%\n"
            f"Bed temp: {bed_temperature} ºC\n"
            f"Nozzle temp: {nozzle_temperature} ºC\n"
            f"Remaining time: {remaining_time}m\n"
            f"Finish time: {finish_time_format}"
        )