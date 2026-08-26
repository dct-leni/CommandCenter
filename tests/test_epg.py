import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from app.epg import _format_xmltv_time, generate_epg

def test_format_xmltv_time():
    dt = datetime(2026, 8, 27, 14, 30, 0)
    assert _format_xmltv_time(dt, "+03:00") == "20260827143000 +0300"
    assert _format_xmltv_time(dt, "+0300") == "20260827143000 +0300"
    assert _format_xmltv_time(dt, "-05:00") == "20260827143000 -0500"
    assert _format_xmltv_time(dt, "0000") == "20260827143000 +0000"

def test_generate_epg_xml(tmp_path):
    folder = tmp_path / "stream_folder"
    folder.mkdir()

    slots = [
        {
            "port": 1935,
            "files": ["movie1.ts", "movie2.ts"],
            "durations": [3600.0, 7200.0],
        },
        {
            "port": 1936,
            "files": ["show1.ts"],
            "durations": [1800.0],
        }
    ]

    out_file = generate_epg(
        folder_path=str(folder),
        slots=slots,
        start_date=date(2026, 8, 27),
        end_date=date(2026, 8, 28),
        lang="tr",
        channel_prefix="Salon",
        timezone_str="+0300",
        port_range_start=1935,
    )

    assert Path(out_file).exists()

    tree = ET.parse(out_file)
    root = tree.getroot()
    assert root.tag == "tv"

    # Channels
    channels = root.findall("channel")
    assert len(channels) == 2
    assert channels[0].get("id") == "Salon1 HD"
    assert channels[1].get("id") == "Salon2 HD"

    # Programmes
    programmes = root.findall("programme")
    assert len(programmes) > 0
    # First programme on channel 1
    p1 = programmes[0]
    assert p1.get("channel") == "1"
    assert p1.find("title").text == "movie1"
