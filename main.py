import os
import time

import discord

from src.discord_bot import PrintBot
from src.printer_manager import PrinterManager

# (name, PrinterManager init method, env var prefix)
PRINTER_SETUP = [
    ("H2D", "init_H2D", "H2D"),
    ("A1mini", "init_A1mini", "A1"),
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
            print(f"Skipping {name}: {prefix}_IP / _ACCESS_CODE / _SERIAL not all set")
            continue
        try:
            # NOTE: positional order is (ip, access_code, serial)
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