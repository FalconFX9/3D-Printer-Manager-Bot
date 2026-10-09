import os
import time

import discord

from src.discord_bot import PrintBot
from src.printer_manager import PrinterManager

# (name, PrinterManager init method, env var prefix)
PRINTER_SETUP = [
    ("H2D", "init_H2D", "H2D"),
    ("A1mini", "init_A1mini", "A1"),
    ("Sovol SV08", "init_Sovol", "SV08")
]


def env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing environment variable {name}")
    return value


def connect_printers(manager):
    for name, init_method, prefix in PRINTER_SETUP:
        ip = os.environ.get(f"{prefix}_IP")
        access_code = os.environ.get(f"{prefix}_ACCESS_CODE")
        serial = os.environ.get(f"{prefix}_SERIAL")
        if not (ip and access_code and serial):
            if "Sovol" in name and ip:
                # Sovol doesn't need an access code or serial, so allow it to connect with just the IP
                access_code = None
                serial = None
            else:
                print(f"Skipping {name}: {prefix}_IP / _ACCESS_CODE / _SERIAL not all set")
                continue
        try:
            # NOTE: positional order is (ip, access_code, serial)
            if init_method == "init_Sovol":
                getattr(manager, init_method)(ip)
            else:
                getattr(manager, init_method)(ip, access_code, serial)
            print(f"{name} connected")
        except Exception as e:
            print(f"Failed to connect to {name}: {e}")


if __name__ == "__main__":
    printer_manager = PrinterManager()
    connect_printers(printer_manager)
    time.sleep(5)  # let MQTT telemetry arrive before the bot starts querying

    intents = discord.Intents.default()
    intents.guilds = True
    intents.message_content = True

    PrintBot(printer_manager=printer_manager, intents=intents).run(env("BOT_TOKEN"))