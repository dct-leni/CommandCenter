"""
EPG (Electronic Programme Guide) generator.
Produces XMLTV-compatible XML files for streaming playlists.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List
import logging

logger = logging.getLogger(__name__)


def _format_xmltv_time(dt: datetime, tz_str: str) -> str:
    """Format a datetime as XMLTV timestamp: YYYYMMDDHHmmss +HHMM"""
    # Normalize tz_str: "+0300" or "+03:00" → "+0300"
    tz_clean = tz_str.replace(":", "")
    if not tz_clean.startswith(("+", "-")):
        tz_clean = "+" + tz_clean
    return dt.strftime("%Y%m%d%H%M%S") + " " + tz_clean


def generate_epg(
    folder_path: str,
    slots: list,         # list of dicts: {port, files: [str], durations: [float]}
    start_date: date,
    end_date: date,
    lang: str,
    channel_prefix: str,
    timezone_str: str,
    port_range_start: int = 1935,
) -> str:
    """
    Generate an XMLTV EPG XML file for the given slot configuration.

    Each slot = one channel. Files play round-robin. The programme list
    is generated from start_date 00:00 through end_date 00:00 (exclusive).

    Returns the absolute path to the generated XML file.
    """
    output_filename = f"{channel_prefix.lower()}.xml"
    output_path = Path(folder_path) / output_filename

    root = ET.Element("tv", attrib={"generator-info-name": "CommandCenter"})

    end_dt = datetime(end_date.year, end_date.month, end_date.day, 0, 0, 0)

    # Sort slots by port ascending
    sorted_slots = sorted(slots, key=lambda s: int(s.get("port") or 0))

    def _get_ch_num(slot: dict, fallback_idx: int) -> int:
        port = slot.get("port")
        if port is not None and port_range_start is not None:
            try:
                val = int(port) - int(port_range_start) + 1
                if val > 0:
                    return val
            except (ValueError, TypeError):
                pass
        return fallback_idx

    # --- Channel declarations ---
    for idx, slot in enumerate(sorted_slots, start=1):
        files = slot.get("files", [])
        durations = slot.get("durations", [])
        if not files or not durations:
            continue
        ch_num = _get_ch_num(slot, idx)
        channel_id = f"{channel_prefix}{ch_num} HD"
        ch_el = ET.SubElement(root, "channel", attrib={"id": channel_id})
        name_el = ET.SubElement(ch_el, "display-name", attrib={"lang": lang})
        name_el.text = str(ch_num)

    # --- Programme entries ---
    for idx, slot in enumerate(sorted_slots, start=1):
        files = slot.get("files", [])
        durations = slot.get("durations", [])

        if not files or not durations:
            continue

        ch_num = _get_ch_num(slot, idx)
        channel_id = f"{channel_prefix}{ch_num} HD"

        # Start from start_date 00:00
        cursor = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0)
        file_cycle_index = 0

        while cursor < end_dt:
            fname = files[file_cycle_index % len(files)]
            dur = durations[file_cycle_index % len(durations)]

            if dur <= 0:
                dur = 3600.0  # fallback 1h if no duration

            # Title = filename without extension
            title = Path(fname).stem

            prog_el = ET.SubElement(root, "programme", attrib={
                "channel": str(ch_num),
                "start": _format_xmltv_time(cursor, timezone_str),
            })
            title_el = ET.SubElement(prog_el, "title", attrib={"lang": lang})
            title_el.text = title

            cursor += timedelta(seconds=dur)
            file_cycle_index += 1

            # Safety cap: stop if we've generated a huge amount
            if file_cycle_index > 100000:
                logger.warning(f"EPG generation capped at 100k entries for channel {ch_num}")
                break

    # Pretty-print XML
    _indent_xml(root)

    tree = ET.ElementTree(root)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="unicode", xml_declaration=False)

    logger.info(f"EPG written to {output_path}")
    return str(output_path)


def _indent_xml(elem, level=0):
    """Add pretty-print indentation to an XML element tree."""
    indent = "\n" + "   " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "   "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            _indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent
    if not level:
        elem.tail = "\n"
