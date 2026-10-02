"""Tiny JSON persistence for the print queue and bed state (survives restarts)."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
QUEUE_FILE = os.environ.get("QUEUE_FILE", os.path.join(ROOT, ".queue.json"))


def _empty():
    return {"prints": [], "bed_blocker": {}}


def save(data, path=QUEUE_FILE):
    """data = {"prints": [...], "bed_blocker": {printer_name: thread_id}}"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)  # atomic: a crash mid-write can't corrupt the saved queue


def load(path=QUEUE_FILE):
    if not os.path.exists(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):  # older format: just the list of prints
            data = {"prints": data}
        data.setdefault("prints", [])
        data.setdefault("bed_blocker", {})
        return data
    except Exception as e:
        print(f"Could not read {path}: {e}")
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return _empty()
