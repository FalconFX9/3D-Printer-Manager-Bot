from bambu_printer import BambuPrinter


class PrinterManager:
    def __init__(self):
        self.printers = []

    def init_H2D(self, printer_ip, serial, access_code):
        bambu_printer = BambuPrinter("H2D", printer_ip, serial, access_code, fake=True)
        self.printers.append(bambu_printer)

    def init_A1mini(self, printer_ip, serial, access_code):
        bambu_printer = BambuPrinter("A1mini", printer_ip, serial, access_code, fake=True)
        self.printers.append(bambu_printer)

    def get_printer_by_name(self, name):
        for printer in self.printers:
            if printer.name == name:
                return printer
        return None