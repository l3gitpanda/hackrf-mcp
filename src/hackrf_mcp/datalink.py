"""Aircraft positions sent over ACARS and VDL2 datalink: ADS-C reports and
CPDLC position reports, from the ARINC 622 messages libacars decodes inside
acarsdec (a top-level "libacars" object) and dumpvdl2 (vdl2.avlc.acars).

An aircraft sends these itself, saying where it was and when; they are not
fixes heard live. So each position carries the time the aircraft gives, which
can be minutes before the message was received. Over land, on VHF, they are
rare (oceanic ADS-C and CPDLC go mostly over HF and satellite), and an
aircraft heard on VHF is about as close as one heard on ADS-B.

What is taken, from libacars' JSON (v2.2):
  ADS-C ("adsc_msg"): each report with the aircraft's current position, the
    tags whose objects hold lat, lon and ts_sec (seconds past the hour):
    basic_report, emerg_basic_report and the event reports. Predicted and
    projected points (eta_sec) are where it will be, and are left out.
    earth_ref_data gives the ground speed and true track.
  CPDLC ("fans1a_cpdlc_msg"): a POSITION REPORT (dM48) whose current
    position is a latitude and longitude, at its time at that position
    (hours and minutes). A fix name, navaid or airport would need a
    navigation database, and is left out.
Messages that fail ARINC 622's CRC are left out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# A report whose time comes out older than this is left out, as is one
# more than SKEW_S in the future (the clocks of the station and the aircraft
# differ a little).
MAX_AGE_S = 3600
SKEW_S = 120
FEET_PER_METRE = 3.28084


@dataclass(frozen=True)
class DatalinkPosition:
    ts: float  # when the aircraft was there (Unix seconds, UTC)
    lat: float
    lon: float
    source: str  # "ADS-C" or "CPDLC"
    report: str  # libacars' name for it, e.g. "basic_report"
    alt: float | None = None  # feet
    speed: float | None = None  # knots over the ground
    track: float | None = None  # degrees true

    def fields(self) -> dict:
        """What a log row holds of it (see the acars and vdl2 profiles)."""
        out = {"lat": round(self.lat, 5), "lon": round(self.lon, 5), "pos_source": self.source, "pos_ts": round(self.ts, 3)}
        if self.alt is not None:
            out["alt"] = int(round(self.alt))
        return out


def arinc622_of(event: dict) -> dict | None:
    """The ARINC 622 object libacars decoded in an acarsdec or dumpvdl2 JSON
    line (already parsed), if any."""
    if not isinstance(event, dict):
        return None
    la = event.get("libacars")
    if isinstance(la, dict) and isinstance(la.get("arinc622"), dict):
        return la["arinc622"]
    v = event.get("vdl2", event)
    acars = ((v.get("avlc") or {}).get("acars") or {}) if isinstance(v, dict) else {}
    a = acars.get("arinc622") if isinstance(acars, dict) else None
    return a if isinstance(a, dict) else None


def report_time(received: float, *, seconds_past_hour: float | None = None, hour: int | None = None, minute: int | None = None) -> float | None:
    """The latest time, not after `received` (give or take SKEW_S), that
    matches the report's own: seconds past the hour (ADS-C) or an hour and
    minute of the day, UTC (CPDLC). None if it would be older than
    MAX_AGE_S or the parts are out of range."""
    if seconds_past_hour is not None:
        if not 0 <= seconds_past_hour < 3600:
            return None
        period, offset = 3600, float(seconds_past_hour)
    else:
        if hour is None or minute is None or not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        period, offset = 86400, hour * 3600.0 + minute * 60.0
    t = math.floor(received / period) * period + offset
    while t > received + SKEW_S:
        t -= period
    return t if received - t <= MAX_AGE_S else None


def _number(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _adsc(adsc: dict, received: float) -> list[DatalinkPosition]:
    tags = adsc.get("tags") if isinstance(adsc, dict) else None
    if not isinstance(tags, list):
        return []
    speed = track = None
    reports = []
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        for name, body in tag.items():
            if not isinstance(body, dict):
                continue
            if name == "earth_ref_data":
                speed = _number(body.get("gnd_spd_kts"))
                if body.get("true_trk_valid"):
                    track = _number(body.get("true_trk_deg"))
            elif "ts_sec" in body and "lat" in body and "lon" in body:
                reports.append((name, body))
    out = []
    for name, body in reports:
        lat, lon, sec = _number(body.get("lat")), _number(body.get("lon")), _number(body.get("ts_sec"))
        if lat is None or lon is None or sec is None:
            continue
        ts = report_time(received, seconds_past_hour=sec)
        if ts is None:
            continue
        out.append(DatalinkPosition(ts, lat, lon, "ADS-C", name, _number(body.get("alt")), speed, track))
    return out


def _altitude(alt: dict) -> float | None:
    """Feet, from a CPDLC altitude: a flight level, or a value in feet or
    metres; None for a metric flight level or anything else."""
    data = alt.get("data") if isinstance(alt, dict) else None
    if not isinstance(data, dict):
        return None
    if "flight_level" in data:
        fl = _number(data["flight_level"])
        return fl * 100 if fl is not None else None
    for v in data.values():
        if isinstance(v, dict) and "val" in v:
            x = _number(v.get("val"))
            if x is None:
                return None
            unit = str(v.get("unit") or "").lower()
            if unit == "ft":
                return x
            if unit == "m":
                return x * FEET_PER_METRE
    return None


def _angle(part: dict, positive: str) -> float | None:
    deg = _number(part.get("deg")) if isinstance(part, dict) else None
    if deg is None:
        return None
    minutes = _number(part.get("min")) or 0.0
    value = deg + minutes / 60.0
    return value if part.get("dir") == positive else -value


def _pos_reports(node) -> list[dict]:
    """Every "pos_report" object anywhere under a CPDLC message (a message
    can hold more than one element)."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "pos_report" and isinstance(v, dict):
                found.append(v)
            else:
                found += _pos_reports(v)
    elif isinstance(node, list):
        for v in node:
            found += _pos_reports(v)
    return found


def _cpdlc(cpdlc: dict, received: float) -> list[DatalinkPosition]:
    out = []
    for r in _pos_reports(cpdlc.get("atc_downlink_msg") if isinstance(cpdlc, dict) else None):
        cur = r.get("pos_current") or {}
        if cur.get("choice") != "latitudeLongitude":
            continue
        ll = (cur.get("data") or {}).get("lat_lon") or {}
        lat, lon = _angle(ll.get("lat"), "north"), _angle(ll.get("lon"), "east")
        when = r.get("time_at_pos_current") or {}
        if lat is None or lon is None or not isinstance(when, dict):
            continue
        try:
            ts = report_time(received, hour=int(when.get("hour")), minute=int(when.get("min")))
        except (TypeError, ValueError):
            continue
        if ts is None:
            continue
        out.append(DatalinkPosition(ts, lat, lon, "CPDLC", "position_report", _altitude(r.get("alt"))))
    return out


def positions(event: dict, received: float) -> list[DatalinkPosition]:
    """The aircraft positions in one decoded acarsdec or dumpvdl2 message,
    oldest first; none for anything else."""
    a = arinc622_of(event)
    if not a or a.get("crc_ok") is not True:
        return []
    out: list[DatalinkPosition] = []
    if a.get("msg_type") == "adsc_msg":
        out = _adsc(a.get("adsc"), received)
    elif a.get("msg_type") == "fans1a_cpdlc_msg":
        out = _cpdlc(a.get("cpdlc"), received)
    out = [p for p in out if -90 <= p.lat <= 90 and -180 <= p.lon <= 180]
    return sorted(out, key=lambda p: p.ts)
