import asyncio
import datetime
import io
import time
from PIL import Image

from pybambu import BambuClient
from src.constants import *
from src.printer import Printer


class BambuPrinter(Printer):
    def __init__(self, name, printer_ip, access_code, serial, fake=False):
        super().__init__(name)
        self.name = name
        self.fake = fake
        self.printer = None
        if self.fake:
            return

        config = {
            "host": printer_ip,
            "access_code": access_code,
            "serial": serial,
            "local_mqtt": True,
            "enable_camera": True,
            "disable_ssl_verify": True,
            "device_type": "H2D",
        }

        self.printer = BambuClient(config)

    def _on_telemetry_update(self, *args, **kwargs):
        """Callback placeholder for pybambu telemetry events."""
        pass

    async def connect(self):
        if not self.fake and self.printer:
            # Connect MQTT asynchronously
            await self.printer.connect(self._on_telemetry_update)
            
            # publish() is a synchronous call, do NOT use await here
            if hasattr(self.printer, "publish"):
                self.printer.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

    async def disconnect(self):
        if not self.fake and self.printer:
            await self.printer.disconnect()

    async def upload_and_start_print(self, print_sub):
        if self.fake:
            print_sub.status = RUNNING
            return

        file_path = print_sub.print_attributes["file_path"]
        file_name = print_sub.print_attributes["file_name"]

        upload_success = await self.printer.ftp.upload_file(file_path, file_name)
        if not upload_success:
            print(f"Failed to upload file: {file_name}")
        else:
            await self.printer.start_print(file_name, plate_number=1)
            print_sub.status = RUNNING

    async def save_image(self):
        if self.fake:
            return

        await asyncio.sleep(0.5)
        image_bytes = await self.printer.camera.get_image()

        if image_bytes:
            image = Image.open(io.BytesIO(image_bytes))
            image.save(f"example_{self.name}.png")
            print(f"Image saved: example_{self.name}.png")
        else:
            print(f"Failed to capture frame from {self.name}")

    async def get_status(self):
        if self.fake:
            return f'''Printer status: RUNNING
            Layers: 10/100
            percentage: 10%
            Bed temp: 60 ºC
            Nozzle temp: 200 ºC
            Remaining time: 90m
            Finish time: {datetime.datetime.now() + datetime.timedelta(minutes=90)}'''

        dev = self.printer.get_device() if hasattr(self.printer, 'get_device') else self.printer.device

        # Safely extract attributes from dev or its sub-objects
        status = getattr(dev, 'gcode_state', None) or getattr(getattr(dev, 'print_job', None), 'gcode_state', 'UNKNOWN')
        percentage = getattr(dev, 'mc_percent', 0)
        layer_num = getattr(dev, 'layer_num', 0)
        total_layer_num = getattr(dev, 'total_layer_num', 0)

        # Temperature handling
        temp_obj = getattr(dev, 'temperature', None)
        bed_temp = getattr(temp_obj, 'bed_temperature', 0) if temp_obj else 0
        nozzle_temp = getattr(temp_obj, 'nozzle_temperature', 0) if temp_obj else 0

        remaining_time = getattr(dev, 'mc_remaining_time', None)

        if remaining_time is not None and str(remaining_time).isdigit():
            finish_time = datetime.datetime.now() + datetime.timedelta(
                minutes=int(remaining_time)
            )
            finish_time_format = finish_time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            finish_time_format = "NA"

        return (
            f"Printer status: {status}\n"
            f"Layers: {layer_num}/{total_layer_num}\n"
            f"Percentage: {percentage}%\n"
            f"Bed temp: {bed_temp} ºC\n"
            f"Nozzle temp: {nozzle_temp} ºC\n"
            f"Remaining time: {remaining_time}m\n"
            f"Finish time: {finish_time_format}"
        )