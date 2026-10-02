import re
import time
import zipfile
from src.print_sub import PrintSubmission
from src.gcode_parse import parse_gcode, parse_xml


class PrintQueueManager:
    def __init__(self):
        self.prints = []

    def add_empty_print(self, thread):
        new_print = PrintSubmission()
        new_print.discord_thread = thread
        self.prints.append(new_print)
        print(f"Added empty print to queue for thread: {thread.name}")

    def add_print(self, thread, file_path):
        if not any(print_sub.discord_thread == thread for print_sub in self.prints):
            new_print = PrintSubmission()
            new_print.discord_thread = thread
            new_print.submission_time = time.time()
            new_print.print_attributes = self.read_gcode(file_path)
            self.prints.append(new_print)
            print(f"Added print to queue: {new_print.print_attributes['file_name']}")
        else:
            print_sub = next(print_sub for print_sub in self.prints if print_sub.discord_thread == thread)
            print_sub.submission_time = time.time()
            print_sub.print_attributes = self.read_gcode(file_path)
            print(f"Modified print in queue: {print_sub.print_attributes['file_name']}")

    def remove_print(self, print_sub):
        if print_sub in self.prints:
            self.prints.remove(print_sub)
            print(f"Removed print from queue: {print_sub}")
        else:
            print(f"Print not found in queue: {print_sub}")

    @staticmethod
    def read_gcode(file_path):
        if file_path.endswith('.gcode'):
            with open(file_path, 'r') as f:
                return parse_gcode(f, file_path)
                
        elif file_path.endswith('.gcode.3mf'):
            attributes = {}
            # Stupid bambu lab export files are zip file with 3mf extension, gcode is in subdir
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                for file in zip_ref.namelist():
                    if file.endswith('.gcode'):
                        with zip_ref.open(file) as f:
                            attributes.update(parse_gcode(f, file_path))

                    if file.endswith("slice_info.config"):
                        with zip_ref.open(file) as f:
                            attributes.update(parse_xml(f, file_path))

            return attributes