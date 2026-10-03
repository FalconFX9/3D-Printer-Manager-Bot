import asyncio
import io
import os
import re
import time

import discord

from src import queue_store
from src.constants import QUEUED, RUNNING, COMPLETE, REMOVED, FAILED
from src.print_queue_manager import PrintQueueManager
from src.embeds import (
    display_name,
    print_info_embed,
    queue_embed,
    read_plate_preview,
    status_embed,
)
from src.print_sub import PrintSubmission
from src.print_utils import (
    IDLE_STATES,
    check_ready,
    fmt_duration,
    printer_state,
    resolve_printer_name,
)

DOWNLOAD_FOLDER = os.environ.get("DOWNLOAD_FOLDER", os.path.join(queue_store.ROOT, ".downloads"))
PRINT_SUBMISSION_CHANNEL = "forum-bot-testing"   # forum channel people post prints in
STATUS_CHANNEL = "3d-printer-status"             # channel holding one status message per printer
STATUS_INTERVAL = 60                             # seconds between status message updates
DISPATCH_INTERVAL = 30                           # seconds between retries of queued prints
MAX_START_ATTEMPTS = 3
KEEP_FINISHED_SECONDS = 14 * 24 * 3600           # drop non-queued entries older than this on restore
DONE_WORDS = {"done", "finished"}                # typed in a thread: print finished, bed is clear
CANCEL_WORDS = {"cancel"}                        # typed in a thread: remove a queued print
RETRY_WORDS = {"retry"}                          # typed in a thread: try a failed print again
SUBMIT_ROLES = ("3D Printer", "Exec")            # roles allowed to submit prints (case-insensitive)
MONITOR_INTERVAL = 15                            # seconds between printer state checks (pause/fail alerts)
ALERT_STATES = {"PAUSE", "FAILED", "FINISH"}
PHOTO_FOLDER = os.path.join(queue_store.ROOT, ".photos")   # temp storage for finished-print photos
LIGHT_SETTLE_SECONDS = 1.5                       # let the light come on / exposure settle before the photo
PHOTO_TIMEOUT = 45                               # give up on a photo after this many seconds


def is_done_message(text):
    return (text or "").strip().lower().strip(".!?, ") in DONE_WORDS


def is_cancel_message(text):
    return (text or "").strip().lower().strip(".!?, ") in CANCEL_WORDS


def is_retry_message(text):
    return (text or "").strip().lower().strip(".!?, ") in RETRY_WORDS


def can_submit(author):
    """True if the message author has one of the SUBMIT_ROLES (needs a guild Member with roles)."""
    allowed = {r.lower() for r in SUBMIT_ROLES}
    return any(role.name.strip().lower() in allowed for role in getattr(author, "roles", []))


def capture_photo_blocking(printer, path):
    """Blocking: light on, photo to `path`, light off. Returns the path, or None if no file was written."""
    if os.path.exists(path):
        os.remove(path)
    try:
        printer.turn_light_on()
    except Exception as e:
        print(f"Could not turn light on for {printer.name}: {e}")
    try:
        time.sleep(LIGHT_SETTLE_SECONDS)
        printer.save_image(path)
    finally:
        try:
            printer.turn_light_off()
        except Exception as e:
            print(f"Could not turn light off for {printer.name}: {e}")
    return path if os.path.exists(path) else None


def describe_notice(key):
    """Short label for why a queued print is waiting, from its last notice key."""
    if not key:
        return "waiting"
    if key == "busy":
        return "printer busy"
    if key.startswith("bed:"):
        return "waiting for bed to be cleared"
    if key.startswith("filament:"):
        return "filament mismatch"
    if key == "offline":
        return "printer offline"
    return "waiting"


class PrintBot(discord.Client):
    def __init__(self, printer_manager, queue_manager=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.printer_manager = printer_manager
        self.queue = queue_manager or PrintQueueManager()
        self.dispatch_lock = asyncio.Lock()
        self.queue_restored = asyncio.Event()
        self._started = False
        self.notices = {}      # id(print_sub) -> last notice key (avoids spamming the thread)
        self.attempts = {}     # id(print_sub) -> start attempts
        self.starting = set()  # id(print_sub) of prints currently being uploaded/started
        self.bed_blocker = {}  # printer name -> thread id whose print is (or may be) on the bed
        self.last_state = {}   # printer name -> last seen gcode state (for pause/failure alerts)
        self.queue_msg_lock = asyncio.Lock()
        self._queue_text = None  # last text posted to the queue message (skip identical edits)

    # ---- startup / shutdown ----
    async def setup_hook(self):
        self.status_task = self.loop.create_task(self.update_printers_status())
        self.dispatch_task = self.loop.create_task(self.dispatch_loop())
        self.monitor_task = self.loop.create_task(self.monitor_printers())

    async def on_ready(self):
        print(f"Logged on as {self.user}!")
        if self._started:  # on_ready can fire again after a reconnect
            return
        self._started = True

        try:
            await self.restore_queue()
        except Exception as e:
            print(f"Queue restore failed: {e}")
        finally:
            self.queue_restored.set()

        for printer in self.printer_manager.printers:
            await self.update_status_message(printer.name, "Bot is starting up...")
        await self.refresh_queue_message()

    async def close(self):
        self.save_queue()
        for printer in self.printer_manager.printers:
            try:
                await asyncio.to_thread(printer.disconnect)
            except Exception as e:
                print(f"Error disconnecting {printer.name}: {e}")
        await super().close()

    # ---- queue persistence ----
    def save_queue(self):
        records = []
        for s in self.queue.prints:
            if not s.print_attributes or s.discord_thread is None:
                continue
            records.append({
                "thread_id": s.discord_thread.id,
                "submission_time": s.submission_time,
                "status": s.status,
                "print_attributes": s.print_attributes,
                "notice": self.notices.get(id(s)),
                "attempts": self.attempts.get(id(s), 0),
                "starting": id(s) in self.starting,
            })
        try:
            queue_store.save({"prints": records, "bed_blocker": dict(self.bed_blocker)})
        except Exception as e:
            print(f"Could not save queue: {e}")

    async def restore_queue(self):
        data = await asyncio.to_thread(queue_store.load)
        now = time.time()
        restored = 0
        restored_thread_ids = set()

        for rec in data["prints"]:
            status = rec.get("status", QUEUED)
            submitted = rec.get("submission_time")
            if status != QUEUED and submitted and now - submitted > KEEP_FINISHED_SECONDS:
                continue  # old finished/removed entry, let it go

            thread_id = rec["thread_id"]
            try:
                thread = self.get_channel(thread_id) or await self.fetch_channel(thread_id)
            except Exception as e:
                print(f"Skipping saved print: thread {thread_id} unavailable ({e})")
                continue

            sub = PrintSubmission()
            sub.discord_thread = thread
            sub.submission_time = submitted
            sub.print_attributes = rec.get("print_attributes")
            sub.status = status
            if rec.get("notice") is not None:
                self.notices[id(sub)] = rec["notice"]
            self.attempts[id(sub)] = rec.get("attempts", 0)
            self.queue.prints.append(sub)
            restored += 1
            restored_thread_ids.add(thread_id)

            if rec.get("starting") and sub.status == QUEUED:
                # Bot died mid-upload: the printer may or may not have started it.
                # Don't risk a duplicate print; the bed stays marked occupied.
                sub.status = REMOVED
                await self.say(sub, "The bot restarted while this print was being sent to the printer. "
                                    "Check the printer; if it didn't start, post the file again. "
                                    "The bed stays marked as occupied until you reply `done` here.")
            elif sub.status == QUEUED and not os.path.exists(sub.print_attributes.get("file_path", "")):
                sub.status = REMOVED
                await self.say(sub, "The downloaded file is missing after the bot restarted. Please post it again.")

        # Keep only bed blockers whose thread still exists, otherwise nobody could clear them
        self.bed_blocker = {
            name: int(tid) for name, tid in data["bed_blocker"].items() if int(tid) in restored_thread_ids
        }

        print(f"Restored {restored} print(s) from {queue_store.QUEUE_FILE}")
        self.save_queue()

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
            if message.author != self.user:
                continue
            if message.content.startswith(f"**{printer_name}**"):  # older plain-text version
                return message
            if message.embeds and message.embeds[0].title == printer_name:
                return message
        return None

    async def update_status_message(self, printer_name, status_text=None, embed=None):
        """Create or edit the bot's message for `printer_name` (an embed, or plain text)."""
        status_channel = self.get_status_channel()
        if status_channel is None:
            print("Status channel not found.")
            return
        message = await self.get_status_message(printer_name)
        content = f"**{printer_name}**\n{status_text}" if status_text else None
        if message:
            await message.edit(content=content, embed=embed)
        else:
            await status_channel.send(content=content, embed=embed)

    async def say(self, sub, text, file_path=None):
        try:
            if file_path:
                try:
                    await sub.discord_thread.send(text, file=discord.File(file_path))
                    return
                except Exception as e:
                    print(f"Could not attach photo, sending text only: {e}")
            await sub.discord_thread.send(text)
        except Exception as e:
            print(f"Could not post to thread: {e}")

    async def capture_photo(self, printer, sub):
        """Photo of the printer's camera view, or None if it couldn't be captured in time."""
        os.makedirs(PHOTO_FOLDER, exist_ok=True)
        path = os.path.join(PHOTO_FOLDER, f"{sub.discord_thread.id}_finished.png")
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(capture_photo_blocking, printer, path), timeout=PHOTO_TIMEOUT
            )
        except Exception as e:
            print(f"Could not capture photo from {printer.name}: {e!r}")
            return None

    async def say_once(self, sub, key, text):
        if self.notices.get(id(sub)) == key:
            return
        self.notices[id(sub)] = key
        await self.say(sub, text)

    def find_sub(self, thread):
        return next((s for s in self.queue.prints if s.discord_thread == thread), None)

    # ---- receiving files / commands ----
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

        if message.attachments:
            for attachment in message.attachments:
                await self.handle_attachment(channel, attachment, message.author)
        elif is_done_message(message.content):
            await self.handle_done(channel)
        elif is_cancel_message(message.content):
            await self.handle_cancel(channel)
        elif is_retry_message(message.content):
            await self.handle_retry(channel)

    async def handle_done(self, thread):
        """User typed 'done'/'finished': mark the print complete and the bed clear."""
        sub = self.find_sub(thread)
        was_running = sub is not None and sub.status == RUNNING
        cleared = [name for name, tid in self.bed_blocker.items() if tid == thread.id]

        for name in cleared:
            del self.bed_blocker[name]
        if was_running:
            sub.status = COMPLETE

        if cleared or was_running:
            self.save_queue()
            head = "Marked as done." if was_running else "Bed marked clear."
            where = f" The bed on {', '.join(cleared)} is now clear." if cleared else ""
            if sub is not None and sub.status == QUEUED:  # a retry is waiting on the bed
                tail = " Your retry will start once the printer is idle."
            elif sub is not None and sub.status == FAILED:
                tail = (" The next queued print will start once the printer is idle."
                        " You can still reply `retry` to queue this one again.")
            else:
                tail = " The next queued print will start once the printer is idle."
            await thread.send(f"{head}{where}{tail}")
            await self.dispatch_queue()
        elif sub is not None and sub.status == QUEUED:
            await thread.send("This print hasn't started yet, so there's nothing to mark as done.")
        elif sub is not None and sub.status == COMPLETE:
            await thread.send("This print is already marked as done.")
        elif sub is not None and sub.status == FAILED:
            await thread.send("The bed is already marked clear. Reply `retry` to try this print again.")

    async def handle_retry(self, thread):
        """User typed 'retry': put a failed print back in the queue, keeping its original place."""
        sub = self.find_sub(thread)
        if sub is None or not sub.print_attributes:
            return

        if sub.status != FAILED:
            notes = {
                QUEUED: "This print is still in the queue, so there's nothing to retry.",
                RUNNING: "This print is still running.",
                COMPLETE: "This print finished successfully. Post the file again to print it again.",
                REMOVED: "This print was removed from the queue. Post the file again to queue it.",
            }
            await thread.send(notes.get(sub.status, "Nothing to retry here."))
            return

        if not os.path.exists(sub.print_attributes.get("file_path", "")):
            sub.status = REMOVED
            self.save_queue()
            await thread.send("The downloaded file is gone, so I can't retry. Please post it again.")
            await self.refresh_queue_message()
            return

        printer_name = resolve_printer_name(sub.print_attributes.get("target_printer"))
        released = False
        if printer_name and self.bed_blocker.get(printer_name) == thread.id:
            del self.bed_blocker[printer_name]  # replying "retry" confirms the bed is clear
            released = True

        sub.status = QUEUED  # submission_time is unchanged, so it keeps its place in the queue
        self.attempts.pop(id(sub), None)
        self.notices.pop(id(sub), None)
        self.save_queue()

        note = " Treating this as confirmation that the bed is clear." if released else ""
        await thread.send(f"Retrying `{display_name(sub.print_attributes)}`. It keeps its place in the queue "
                          f"and will start as soon as {printer_name or 'the printer'} is ready.{note}")
        await self.dispatch_queue()

    async def handle_cancel(self, thread):
        """User typed 'cancel': remove this thread's print from the queue if it hasn't started."""
        sub = self.find_sub(thread)
        if sub is None or not sub.print_attributes:
            return

        if id(sub) in self.starting:
            await thread.send("This print is being sent to the printer right now, so it can't be canceled here.")
        elif sub.status in (QUEUED, FAILED):
            sub.status = REMOVED
            self.notices.pop(id(sub), None)
            self.attempts.pop(id(sub), None)
            try:
                os.remove(sub.print_attributes.get("file_path", ""))
            except OSError:
                pass
            self.save_queue()
            await thread.send("Canceled. The print was removed from the queue. Post a new file to queue it again.")
            await self.refresh_queue_message()
        elif sub.status == RUNNING:
            await thread.send("This print has already started, so I can't cancel it from here. "
                              "Stop it on the printer, then reply `done` once the bed is clear.")
        elif sub.status == COMPLETE:
            await thread.send("This print is already finished, so there's nothing to cancel.")
        else:
            await thread.send("This print is no longer in the queue.")

    async def handle_attachment(self, thread, attachment, author=None):
        name = os.path.basename(attachment.filename)
        if not name.lower().endswith(".gcode.3mf"):
            await thread.send(f"`{name}` ignored: please attach a sliced `.gcode.3mf` (Bambu Studio export).")
            return

        if not can_submit(author):
            roles = " or ".join(f"`{r}`" for r in SUBMIT_ROLES)
            await thread.send(f"Sorry, only members with the {roles} role can submit prints.")
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
        self.save_queue()

        attrs = sub.print_attributes or {}
        target = resolve_printer_name(attrs.get("target_printer"))
        ahead = sorted(
            (s for s in self.queue.prints
             if s.status == QUEUED and s.print_attributes
             and resolve_printer_name(s.print_attributes.get("target_printer")) == target),
            key=lambda s: s.submission_time or 0,
        )
        position = ahead.index(sub) + 1 if target and sub in ahead else None
        preview = await asyncio.to_thread(read_plate_preview, file_path)
        embed = print_info_embed(attrs, target, position, has_preview=preview is not None)
        try:
            if preview is not None:
                await thread.send(embed=embed, file=discord.File(io.BytesIO(preview), filename="preview.png"))
            else:
                await thread.send(embed=embed)
        except Exception as e:
            print(f"Could not send print info embed: {e}")
            await thread.send(f"Added to queue: `{name}` (printer: {target or 'unknown'}). "
                              "Reply `cancel` to remove it.")
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
            self.save_queue()
        await self.refresh_queue_message()

    async def try_dispatch(self, sub):
        if sub.status != QUEUED:  # e.g. canceled since the queue snapshot was taken
            return
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

        blocker = self.bed_blocker.get(printer_name)
        if blocker is not None:
            await self.say_once(
                sub, f"bed:{blocker}",
                f"Waiting for the previous print on {printer_name} to finish and its bed to be cleared. "
                f"Once it's removed, reply `done` in <#{blocker}>."
            )
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
        if sub.status != QUEUED:  # canceled while readiness was being checked
            return
        n = self.attempts.get(id(sub), 0) + 1
        self.attempts[id(sub)] = n
        file_name = sub.print_attributes["file_name"]

        # Claim the print and occupy the bed before any await (so a cancel can't slip in), and
        # persist an "in flight" marker: if the bot dies mid-upload, the restored queue won't
        # blindly re-send a print the printer may already have started.
        self.starting.add(id(sub))
        self.bed_blocker[printer.name] = sub.discord_thread.id
        self.save_queue()
        await self.say(sub, f"Uploading `{file_name}` to {printer.name}...")
        try:
            await asyncio.to_thread(printer.upload_and_start_print, sub)
        except Exception as e:
            print(f"Upload/start error on {printer.name}: {e}")
        finally:
            self.starting.discard(id(sub))
            if sub.status != RUNNING:
                self.bed_blocker.pop(printer.name, None)  # nothing started, release the bed
            self.save_queue()

        if sub.status != RUNNING:
            if n >= MAX_START_ATTEMPTS:
                sub.status = FAILED
                await self.say(sub, f"Failed to start the print on {printer.name} after {n} attempts. Giving up. "
                                    "Reply `retry` to try again (it keeps its place in the queue).")
            else:
                await self.say(sub, f"Upload/start failed on {printer.name} (attempt {n}/{MAX_START_ATTEMPTS}). Will retry.")
            return

        await self.say(sub, f"Print started on {printer.name}. When it's finished and you've cleared the bed, "
                            "reply `done` in this thread so the next print can start.")
        await self.refresh_queue_message()

        # Hold the dispatch lock until the printer leaves its idle state,
        # so the next queued job can't be sent before telemetry catches up.
        for _ in range(30):
            await asyncio.sleep(2)
            if await asyncio.to_thread(printer_state, printer) not in IDLE_STATES:
                break

    async def dispatch_loop(self):
        await self.wait_until_ready()
        await self.queue_restored.wait()
        while not self.is_closed():
            try:
                await self.dispatch_queue()
            except Exception as e:
                print(f"Dispatch loop error: {e}")
            await asyncio.sleep(DISPATCH_INTERVAL)

    # ---- pause / failure / finish alerts ----
    async def get_errors(self, printer):
        try:
            return await asyncio.to_thread(printer.get_errors)
        except Exception as e:
            print(f"Could not read errors from {printer.name}: {e}")
            return []

    async def monitor_printers(self):
        await self.wait_until_ready()
        await self.queue_restored.wait()
        while not self.is_closed():
            for printer in self.printer_manager.printers:
                try:
                    await self.check_printer_alert(printer)
                except Exception as e:
                    print(f"Monitor error for {printer.name}: {e}")
            await asyncio.sleep(MONITOR_INTERVAL)

    async def check_printer_alert(self, printer):
        state = await asyncio.to_thread(printer_state, printer)
        previous = self.last_state.get(printer.name)
        self.last_state[printer.name] = state
        # First poll only records the state, so a restart doesn't repeat an old alert
        if previous is None or state == previous or state not in ALERT_STATES:
            return

        # Only alert in the thread of the print this bot started on that printer
        thread_id = self.bed_blocker.get(printer.name)
        sub = next((s for s in self.queue.prints
                    if s.discord_thread is not None and s.discord_thread.id == thread_id
                    and s.status == RUNNING), None)
        if sub is None:
            return

        file_name = display_name(sub.print_attributes)

        if state == "FINISH":
            photo = await self.capture_photo(printer, sub)
            text = (f"{printer.name} finished `{file_name}`. Please remove the print and clear the bed, "
                    "then reply `done` here so the next print can start.")
            if photo is None:
                text += "\n(Couldn't capture a photo.)"
            await self.say(sub, text, file_path=photo)
            if photo:
                try:
                    os.remove(photo)
                except OSError:
                    pass
            return

        await asyncio.sleep(3)  # error codes can arrive a moment after the state change
        errors = await self.get_errors(printer)

        if errors:
            reason = "Reported: " + ", ".join(errors)
        else:
            reason = "No error codes were reported."

        if state == "PAUSE":
            text = (f"{printer.name} is **paused** during `{file_name}`. {reason}\n"
                    "Check the printer, then resume or stop it there.")
        else:
            sub.status = FAILED
            self.save_queue()
            text = (f"{printer.name} reports the print **failed**: `{file_name}`. {reason}\n"
                    "Check the printer and clear the bed. Reply `retry` to print it again (it keeps its place "
                    "in the queue), or `done` to release the printer for the next print.")
        await self.say(sub, text)
        if state == "FAILED":
            await self.refresh_queue_message()

    # ---- queue message ----
    def build_queue_sections(self):
        """[(printer name, text)] for the queue embed."""
        names = {p.name for p in self.printer_manager.printers} | set(self.bed_blocker)
        queued = sorted(
            (s for s in self.queue.prints if s.status == QUEUED and s.print_attributes),
            key=lambda s: s.submission_time or 0,
        )
        by_printer = {}
        for s in queued:
            name = resolve_printer_name(s.print_attributes.get("target_printer"))
            if name:
                by_printer.setdefault(name, []).append(s)
                names.add(name)

        sections = []
        for name in sorted(names):
            lines = []
            blocker = self.bed_blocker.get(name)
            if blocker is None:
                lines.append("**Now:** idle, bed clear")
            else:
                cur = next((s for s in self.queue.prints
                            if s.discord_thread is not None and s.discord_thread.id == blocker), None)
                label = f"`{display_name(cur.print_attributes)}` " if cur is not None and cur.print_attributes else ""
                if cur is not None and id(cur) in self.starting:
                    lines.append(f"**Now:** uploading {label}(<#{blocker}>)")
                elif cur is not None and cur.status == FAILED:
                    lines.append(f"**Now:** {label}(<#{blocker}>) 🔴 failed")
                    lines.append("↳ reply `retry` there to try again, or `done` to release the printer")
                else:
                    lines.append(f"**Now:** {label}(<#{blocker}>)")
                    lines.append("↳ reply `done` there when finished and the bed is clear")

            waiting = [s for s in by_printer.get(name, []) if s.discord_thread.id != blocker]
            if not waiting:
                lines.append("**Next up:** nothing queued")
            else:
                lines.append("**Next up:**")
                for i, s in enumerate(waiting[:10], 1):
                    a = s.print_attributes
                    lines.append(f"{i}. `{display_name(a)}` (<#{s.discord_thread.id}>) · "
                                 f"~{fmt_duration(a.get('print_time'))} · "
                                 f"{describe_notice(self.notices.get(id(s)))}")
                if len(waiting) > 10:
                    lines.append(f"...and {len(waiting) - 10} more")
            sections.append((name, "\n".join(lines)))
        return sections

    async def refresh_queue_message(self):
        if self.get_status_channel() is None:
            return
        async with self.queue_msg_lock:
            embed = queue_embed(self.build_queue_sections())
            state = embed.to_dict()
            if state == self._queue_text:
                return
            try:
                await self.update_status_message("Print queue", embed=embed)
                self._queue_text = state
            except Exception as e:
                print(f"Queue message update failed: {e}")

    # ---- status messages ----
    async def build_status_embed(self, printer):
        getter = getattr(printer, "get_status_data", None)
        data = text = None
        if getter is not None:
            data = await asyncio.to_thread(getter)
        else:  # printer class without get_status_data(): show its plain status text
            text = await asyncio.to_thread(printer.get_status)
        try:
            filaments = await asyncio.to_thread(printer.get_filaments)
        except Exception as e:
            print(f"Could not read filaments from {printer.name}: {e}")
            filaments = None
        errors = await self.get_errors(printer)
        return status_embed(printer.name, data, filaments, errors, fallback_text=text)

    async def update_printers_status(self):
        await self.wait_until_ready()
        while not self.is_closed():
            for printer in self.printer_manager.printers:
                try:
                    embed = await self.build_status_embed(printer)
                except Exception as e:
                    embed = status_embed(printer.name, fallback_text=f"Printer unreachable: {e}")
                try:
                    await self.update_status_message(printer.name, embed=embed)
                except Exception as e:
                    print(f"Status update failed for {printer.name}: {e}")
            await self.refresh_queue_message()
            await asyncio.sleep(STATUS_INTERVAL)