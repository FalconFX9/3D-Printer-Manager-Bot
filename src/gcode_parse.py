import re
import xml.etree.ElementTree as ET


def to_seconds(line):
    m = re.search(r'(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?', line)
    if not m:
        return -1
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def parse_gcode(file_obj, file_path):
    attributes = {}
    attributes['file_path'] = file_path
    attributes['file_name'] = file_path.split('\\')[-1]
    attributes['target_printer'] = None
    attributes['print_time'] = None
    attributes['filament_used'] = {}
    filament_used_g = None
    for line in file_obj.readlines():
        if isinstance(line, bytes):
            line = line.decode('utf-8', errors='ignore')
        if "; printer_model = " in line:
            attributes['target_printer'] = line.split(" = ")[1].strip()
        if "; estimated printing time" in line:
            attributes['print_time'] = to_seconds(line.split(" = ")[1].strip())
        if "; filament used [g] = " in line:
            filament_used_g = float(line.split(" = ")[1])
        if "; filament_type = " in line:
            attributes['filament_used'][line.split(" = ")[1].strip()] = 0

    if filament_used_g is not None and attributes['filament_used']:
        fil_type = list(attributes['filament_used'].keys())[0]
        attributes['filament_used'][fil_type] = filament_used_g

    return attributes

def parse_xml(file_obj, file_path):
    attributes = {}
    attributes['print_time'] = None
    attributes['filament_used'] = None
    tree = ET.parse(file_obj)
    root = tree.getroot()

    # Get prediction (estimated print time in seconds) from metadata
    prediction = None
    for metadata in root.iter('metadata'):
        if metadata.get('key') == 'prediction':
            prediction = int(metadata.get('value'))
            break

    # Get filament info
    filaments = {}
    for filament in root.iter('filament'):
        filaments[filament.get('type')] = float(filament.get('used_g'))

    return {
        'print_time': prediction,
        'filament_used': filaments,
    }