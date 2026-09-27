from bambu_printer import BambuPrinter


class PrinterManager:
    def __init__(self):
        self.printers = []

    def init_H2D(self, printer_ip, serial, access_code):
        bambu_printer = BambuPrinter("H2D", printer_ip, serial, access_code, fake=False)
        self.printers.append(bambu_printer)

    def init_A1mini(self, printer_ip, serial, access_code):
        bambu_printer = BambuPrinter("A1mini", printer_ip, serial, access_code, fake=False)
        self.printers.append(bambu_printer)

    def get_printer_by_name(self, name):
        for printer in self.printers:
            if printer.name == name:
                return printer
        return None

# if __name__ == "__main__":
    # print_manager = PrinterManager()
    # print_manager.init_H2D("192.168.0.100", "6ef79e8d", "0948AD540900932")
    # H2D = print_manager.get_printer_by_name("H2D")
    # import time
    # time.sleep(2)
    # print(H2D.get_status())
    # H2D.save_image()
    # H2D.disconnect()
    # print_manager.init_A1mini("192.168.0.101", "5d79485c", "0309CA452900808")
    # A1 = print_manager.get_printer_by_name("A1mini")
    # import time
    # time.sleep(5)
    # print(A1.get_status())
    # A1.save_image()
    # A1.disconnect()
