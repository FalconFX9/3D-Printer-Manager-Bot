import asyncio
import os

import discord

from src.constants import QUEUED, RUNNING, COMPLETE, REMOVED
from src.print_queue_manager import PrintQueueManager
from src.print_utils import (
    IDLE_STATES,
    check_ready,
    fmt_duration,
    printer_state,
    resolve_printer_name,
)

DOWNLOAD_FOLDER = os.environ.get("DOWNLOAD_FOLDER", os.path.join(os.getcwd(), ".downloads"))
PRINT_SUBMISSION_CHANNEL = "forum-bot-testing"   # forum channel people post prints in
STATUS_CHANNEL = "3d-printer-status"             # channel holding one status message per printer
STATUS_INTERVAL = 60                             # seconds between status message updates
DISPATCH_INTERVAL = 30                           # seconds between retries of queued prints
MAX_START_ATTEMPTS = 3


class PrintBot(discord.Client):
    def __init__(self, printer_manager, queue_manager=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.printer_manager = printer_manager
        self.queue = queue_manager or PrintQueueManager()
        self.dispatch_lock = asyncio.Lock()
        self._started = False
        self.notices = {}    # id(print_sub) -> last notice key (avoids spamming the thread)
        self.attempts = {}   # id(print_sub) -> start attempts

    # ---- startup / shutdown ----
    async def setup_hook(self):
        self.status_task = self.loop.create_task(self.update_printers_status())
        self.dispatch_task = self.loop.create_task(self.dispatch_loop())

    async def on_ready(self):
        print(f"Logged on as {self.user}!")
        if self._started:  # on_ready can fire again after a reconnect
            return
        self._started = True
        for printer in self.printer_manager.printers:
            await self.update_status_message(printer.name, "Bot is starting up...")

    async def close(self):
        for printer in self.printer_manager.printers:
            try:
                await asyncio.to_thread(printer.disconnect)
            except Exception as e:
                print(f"Error disconnecting {printer.name}: {e}")
        await super().close()

    # ---- discord helpers ----
    def get_status_channel(self):
        for channel in self.get_all_channels():
            if channel.name == STATUS_CHANNEL:
                return channel

    async def get_status_message(self, printer_name):
        status_channel = self.get_status_channel()
        if status_channel is None:
            return None
        async for message in status_channel.history(limit=100):
            if message.author == self.user and message.content.startswith(f"**{printer_name}**"):
                return message
        return None

    async def update_status_message(self, printer_name, status_text):
        status_channel = self.get_status_channel()
        if status_channel is None:
            print("Status channel not found.")
            return
        message = await self.get_status_message(printer_name)
        content = f"**{printer_name}**\n{status_text}"
        if message:
            await message.edit(content=content)
        else:
            await status_channel.send(content)

    async def say(self, sub, text):
        try:
            await sub.discord_thread.send(text)
        except Exception as e:
            print(f"Could not post to thread: {e}")

    async def say_once(self, sub, key, text):
        if self.notices.get(id(sub)) == key:
            return
        self.notices[id(sub)] = key
        await self.say(sub, text)

    def find_sub(self, thread):
        return next((s for s in self.queue.prints if s.discord_thread == thread), None)

    # ---- receiving files ----
    async def on_message(self, message):
        channel = message.channel
        if (
            channel.type != discord.ChannelType.public_thread
            or channel.parent is None
            or channel.parent.name != PRINT_SUBMISSION_CHANNEL
        ):
            return
        if message.author == self.user:
            return

        for attachment in message.attachments:
            await self.handle_attachment(channel, attachment)

    async def handle_attachment(self, thread, attachment):
        name = os.path.basename(attachment.filename)
        if not name.lower().endswith(".gcode.3mf"):
            await thread.send(f"`{name}` ignored: please attach a sliced `.gcode.3mf` (Bambu Studio export).")
            return

        existing = self.find_sub(thread)
        if existing and existing.status in (RUNNING, COMPLETE):
            await thread.send("This thread's print has already been started, so I can't replace it. Open a new post.")
            return

        os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
        # Prefix with the thread id so identical file names from different threads don't collide
        file_path = os.path.join(DOWNLOAD_FOLDER, f"{thread.id}_{name}")
        await attachment.save(file_path)
        print(f"Downloaded attachment to: {file_path}")

        try:
            await asyncio.to_thread(self.queue.add_print, thread, file_path)
        except Exception as e:
            print(f"Failed to parse {file_path}: {e}")
            await thread.send(f"Couldn't read `{name}`: {e}")
            return

        sub = self.find_sub(thread)
        sub.status = QUEUED
        self.notices.pop(id(sub), None)
        self.attempts.pop(id(sub), None)

        attrs = sub.print_attributes or {}
        target = resolve_printer_name(attrs.get("target_printer")) or "unknown"
        filaments = ", ".join(f"{t} {g:g} g" for t, g in (attrs.get("filament_used") or {}).items()) or "unknown"
        await thread.send(
            f"Added to queue: `{name}`\n"
            f"Target printer: {target} (from `{attrs.get('target_printer')}`)\n"
            f"Estimated time: {fmt_duration(attrs.get('print_time'))}\n"
            f"Filament: {filaments}"
        )
        await self.dispatch_queue()

    # ---- dispatching ----
    async def dispatch_queue(self):
        async with self.dispatch_lock:
            queued = [s for s in self.queue.prints if s.status == QUEUED and s.print_attributes]
            queued.sort(key=lambda s: s.submission_time or 0)
            for sub in queued:
                try:
                    await self.try_dispatch(sub)
                except Exception as e:
                    print(f"Dispatch error: {e}")

    async def try_dispatch(self, sub):
        attrs = sub.print_attributes
        model = attrs.get("target_printer")
        printer_name = resolve_printer_name(model)

        if printer_name is None:
            sub.status = REMOVED
            await self.say(sub, f"I can't tell which printer this is for (printer_model = `{model}`). "
                                "Re-slice for the H2D or A1 mini and post it again.")
            return

        printer = self.printer_manager.get_printer_by_name(printer_name)
        if printer is None:
            await self.say_once(sub, "offline", f"{printer_name} isn't connected right now. I'll keep retrying.")
            return

        try:
            ready, key, msg = await asyncio.to_thread(check_ready, printer, attrs)
        except Exception as e:
            print(f"Readiness check failed for {printer_name}: {e}")
            await self.say_once(sub, "check-error", f"Couldn't read {printer_name}'s state yet. I'll retry.")
            return

        if not ready:
            await self.say_once(sub, key, msg)
            return

        await self.start_print(sub, printer)

    async def start_print(self, sub, printer):
        n = self.attempts.get(id(sub), 0) + 1
        self.attempts[id(sub)] = n
        file_name = sub.print_attributes["file_name"]
        await self.say(sub, f"Uploading `{file_name}` to {printer.name}...")

        try:
            await asyncio.to_thread(printer.upload_and_start_print, sub)
        except Exception as e:
            print(f"Upload/start error on {printer.name}: {e}")

        if sub.status != RUNNING:
            if n >= MAX_START_ATTEMPTS:
                sub.status = REMOVED
                await self.say(sub, f"Failed to start the print on {printer.name} after {n} attempts. Giving up.")
            else:
                await self.say(sub, f"Upload/start failed on {printer.name} (attempt {n}/{MAX_START_ATTEMPTS}). Will retry.")
            return

        await self.say(sub, f"Print started on {printer.name}.")

        # Hold the dispatch lock until the printer leaves its idle state,
        # so the next queued job can't be sent before telemetry catches up.
        for _ in range(30):
            await asyncio.sleep(2)
            if await asyncio.to_thread(printer_state, printer) not in IDLE_STATES:
                break

    async def dispatch_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.dispatch_queue()
            except Exception as e:
                print(f"Dispatch loop error: {e}")
            await asyncio.sleep(DISPATCH_INTERVAL)

    # ---- status messages ----
    async def update_printers_status(self):
        await self.wait_until_ready()
        while not self.is_closed():
            for printer in self.printer_manager.printers:
                try:
                    status = await asyncio.to_thread(printer.get_status)
                    filaments = await asyncio.to_thread(printer.get_filaments_text)
                    text = f"{status}\n\n**Loaded filament**\n{filaments}"
                except Exception as e:
                    text = f"Printer unreachable: {e}"
                try:
                    await self.update_status_message(printer.name, text)
                except Exception as e:
                    print(f"Status update failed for {printer.name}: {e}")
            await asyncio.sleep(STATUS_INTERVAL)
