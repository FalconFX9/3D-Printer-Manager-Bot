def send_print(print_sub):
    if print_sub.attributes["file_path"].endswith('.gcode'):
        # OrcaSlicer file, send to Sovol
        pass
    else:
        # BBL file, send to appropriate printer
        pass