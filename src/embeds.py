"""Discord embeds for printer status, parsed print info and the queue."""
import re
import time
import zipfile

import discord

from src.print_utils import fmt_duration

BLURPLE = 0x5865F2

# state -> (emoji, color, label)
STATE_STYLE = {
    "RUNNING": ("🟢", 0x2ECC71, "Printing"),
    "PREPARE": ("🟡", 0xF1C40F, "Preparing"),
    "PAUSE": ("🟠", 0xE67E22, "Paused"),
    "FAILED": ("🔴", 0xE74C3C, "Failed"),
    "FINISH": ("✅", 0x3498DB, "Finished"),
    "IDLE": ("⚪", 0x95A5A6, "Idle"),
}
ACTIVE_STATES = {"RUNNING", "PREPARE", "PAUSE"}


def display_name(attrs):
    """File name without the folder and the thread-id prefix added at download time."""
    name = re.split(r"[\\/]", (attrs or {}).get("file_name") or "print")[-1]
    return re.sub(r"^\d+_", "", name)


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def progress_bar(pct, width=12):
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def format_filaments(filaments):
    if filaments is None:
        return "Unavailable"
    if not filaments:
        return "None loaded (set the filament type on the printer)"
    lines = []
    for f in filaments:
        parts = [f"**{f.get('type')}**"]
        if f.get("brand"):
            parts.append(str(f["brand"]))
        if f.get("color"):
            parts.append(f"`{f['color']}`")
        remain = _num(f.get("remain_pct"))
        if remain is not None and remain > 0:
            parts.append(f"{remain:.0f}% left")
        line = " · ".join(parts)
        if len(filaments) > 1:
            line = f"{f.get('location', 'Spool')}: {line}"
        lines.append(line)
    return "\n".join(lines)


def status_embed(name, data=None, filaments=None, errors=None, fallback_text=None):
    """data: dict from BambuPrinter.get_status_data(), or None to show fallback_text instead."""
    state = (data or {}).get("state", "UNKNOWN")
    emoji, color, label = STATE_STYLE.get(state, ("⚫", 0x7F8C8D, str(state).title()))
    if errors:
        color = 0xE74C3C

    embed = discord.Embed(title=name, description=f"{emoji} **{label}**", color=color,
                          timestamp=discord.utils.utcnow())
    embed.set_footer(text="Updated")

    if data is None:
        if fallback_text:
            embed.description = f"```\n{fallback_text}\n```"
    else:
        pct = _num(data.get("percentage")) or 0
        layer = _num(data.get("layer")) or 0
        total = _num(data.get("total_layers")) or 0
        if total > 0 or pct > 0:
            lines = [f"{progress_bar(pct)} **{pct:.0f}%**"]
            if total > 0:
                lines.append(f"Layer {layer:.0f} / {total:.0f}")
            embed.add_field(name="Progress", value="\n".join(lines), inline=False)

        temps = []
        bed, nozzle = _num(data.get("bed_temp")), _num(data.get("nozzle_temp"))
        if bed is not None:
            temps.append(f"Bed `{bed:.0f}°C`")
        if nozzle is not None:
            temps.append(f"Nozzle `{nozzle:.0f}°C`")
        if temps:
            embed.add_field(name="Temperatures", value="\n".join(temps), inline=True)

        remaining = _num(data.get("remaining_minutes"))
        if remaining and remaining > 0 and state in ACTIVE_STATES:
            eta = int(time.time() + remaining * 60)
            style = "t" if remaining < 720 else "f"  # time of day, or full date if far away
            embed.add_field(name="Time left",
                            value=f"{fmt_duration(remaining * 60)}\nDone around <t:{eta}:{style}>", inline=True)

    embed.add_field(name="Filament", value=format_filaments(filaments), inline=False)
    if errors:
        embed.add_field(name="Errors", value="```\n" + "\n".join(errors)[:1000] + "\n```", inline=False)
    return embed


def print_info_embed(attrs, target, position=None, has_preview=False):
    embed = discord.Embed(title="Added to queue", description=f"`{display_name(attrs)}`", color=BLURPLE)

    printer = f"**{target}**" if target else "**Unknown**"
    if attrs.get("target_printer"):
        printer += f"\n`{attrs['target_printer']}`"
    embed.add_field(name="Printer", value=printer, inline=True)
    embed.add_field(name="Estimated time", value=fmt_duration(attrs.get("print_time")), inline=True)
    if position:
        embed.add_field(name="Queue position", value=f"#{position}", inline=True)

    lines = []
    for ftype, grams in (attrs.get("filament_used") or {}).items():
        g = _num(grams)
        lines.append(f"**{ftype}**" + (f" · {g:g} g" if g else ""))
    embed.add_field(name="Filament", value="\n".join(lines) or "Unknown", inline=False)

    embed.set_footer(text="Reply cancel to remove it from the queue")
    if has_preview:
        embed.set_thumbnail(url="attachment://preview.png")
    return embed


def queue_embed(sections):
    """sections: list of (printer name, text)."""
    embed = discord.Embed(title="Print queue", color=BLURPLE)
    if not sections:
        embed.description = "No printers connected."
    for name, value in sections:
        embed.add_field(name=name, value=value[:1024], inline=False)
    return embed


def read_plate_preview(file_path):
    """Best effort: the plate preview image embedded in a sliced .gcode.3mf, or None."""
    try:
        with zipfile.ZipFile(file_path) as z:
            names = z.namelist()
            if "Metadata/plate_1.png" in names:
                return z.read("Metadata/plate_1.png")
            for n in names:
                if re.fullmatch(r"Metadata/plate_\d+\.png", n):
                    return z.read(n)
    except Exception:
        pass
    return None