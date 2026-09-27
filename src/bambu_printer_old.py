import bambulabs_api as bbl
import datetime
import zipfile
import io
import time

from constants import *
from printer import Printer


class BambuPrinter(Printer):
    def __init__(self, name, printer_ip, access_code, serial, fake=False):
        super().__init__(name)
        self.fake = fake
        if self.fake:
            return
        self.printer = bbl.Printer(printer_ip, access_code, serial)
        self.printer.connect()

    def disconnect(self):
        self.printer.disconnect()

    def upload_and_start_print(self, print_sub):
        file_path = print_sub.print_attributes["file_path"]
        # Load into memory
        with open(file_path, 'rb') as f:
            zip_bytes = f.read()            
            zip_buffer = io.BytesIO(zip_bytes)
            result = self.printer.upload_file(zip_buffer, print_sub.print_attributes["file_name"])
            if "226" in result:
                print(f"Failed to upload file: {print_sub.print_attributes['file_name']}")
            else:
                self.printer.start_print(print_sub.print_attributes["file_name"], plate_number=1)
                print_sub.status = RUNNING

    def save_image(self):
        #self.printer.turn_light_on()
        time.sleep(0.5)
        image = self.printer.get_camera_image()
        time.sleep(0.5)
        #self.printer.turn_light_off()
        image.save(f"example_{self.name}.png")


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

        return f"Printer status: {status}\nLayers: {layer_num}/{total_layer_num}\nPercentage: {percentage}%\nBed temp: {bed_temperature} ºC\nNozzle temp: {nozzle_temperature} ºC\nRemaining time: {remaining_time}m\nFinish time: {finish_time_format}"
