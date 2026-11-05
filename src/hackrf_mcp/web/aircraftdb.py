"""Aircraft details for the web UI: what kind of aircraft an ICAO address
is (military, law enforcement, government, commercial, private) and whose,
from two public databases that hackrf-web downloads and merges into one
SQLite file, aircraft.db, in its state directory.

Off unless the operator turns it on (HACKRF_WEB_AIRCRAFT_DB=weekly): it is
the only part of hackrf-web that connects out, and it only downloads. When
on, the database is built at start if missing, then rebuilt once it is a
week old; a failed attempt is retried after 6 hours. Lookups work without
it, from the rules that need no data (the address range, the callsign).

Sources
  FAA Aircraft Registry, the "Releasable Aircraft Database" (public domain,
  refreshed daily): every US civil registration with its ICAO address
  (MODE S CODE HEX), registrant type and name, aircraft type, and make and
  model from ACFTREF. Only those fields are kept: no addresses or other
  co-owners' names.
  plane-alert-db, by SDR-Enthusiasts and contributors (ODbL 1.0; contents
  DbCL 1.0): about 17,000 notable aircraft worldwide, each tagged Mil, Pol,
  Gov or Civ, with operator, type and category.

Classes, first match wins:
  1. plane-alert Mil / Pol                   military / law_enforcement
  2. an address in ADF7C8-AFFFFF             military: inside the US block
                                             (A00000-AFFFFF) but above every
                                             civil N-number (N99999 is ADF7C7;
                                             the FAA file's highest address was
                                             ADF7C5 on 2026-11-05)
  3. an FAA government registrant (code 5)   military if named for an armed
     named for an armed force or for         force; law_enforcement if for
     police                                  police, a sheriff, a patrol, an
                                             "xxPD" and so on
  4. plane-alert Gov, an FAA government      government (the Civil Air Patrol
     registrant, the Civil Air Patrol        is the US Air Force's auxiliary)
  5. an airline-style callsign               commercial (3 letters, then a
                                             flight number)
  6. any other FAA registration or a         private
     plane-alert Civ entry
  7. nothing known                           unknown

A city's or county's aircraft whose owner name doesn't say police (many
police helicopters are registered to "CITY OF ...") can only be called
government.

The FAA code tables are from its layout document (ardata.pdf, 2025-05-08).
"""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import threading
import time
import urllib.request
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

FAA_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
PLANE_ALERT_URL = "https://raw.githubusercontent.com/sdr-enthusiasts/plane-alert-db/main/plane-alert-db.csv"
# Who is asking, plainly.
USER_AGENT = "hackrf-web aircraft database refresh"
# Larger than any real download, to stop a runaway one filling the disk.
MAX_DOWNLOAD = 400 * 1024 * 1024
MAX_AGE_S = 7 * 86400
RETRY_S = 6 * 3600
CHECK_S = 3600
CREDIT = (
    "Aircraft: FAA Aircraft Registry (public domain); plane-alert-db © SDR-Enthusiasts "
    "and contributors (ODbL 1.0)"
)

# The classes, in the order the UI lists them, with a colour each.
CLASSES = (
    ("military", "Military", "#ff6b6b"),
    ("law_enforcement", "Law enforcement", "#5aa9ff"),
    ("government", "Government", "#c58cff"),
    ("commercial", "Commercial", "#ffd166"),
    ("private", "Private", "#8fd46a"),
    ("unknown", "Unknown", "#8a96a6"),
)

# FAA MASTER "TYPE REGISTRANT" and "TYPE AIRCRAFT" (ardata.pdf).
REGISTRANT = {
    "1": "Individual",
    "2": "Partnership",
    "3": "Corporation",
    "4": "Co-Owned",
    "5": "Government",
    "7": "LLC",
    "8": "Non Citizen Corporation",
    "9": "Non Citizen Co-Owned",
}
AIRCRAFT_TYPE = {
    "1": "Glider",
    "2": "Balloon",
    "3": "Blimp/Dirigible",
    "4": "Fixed wing single engine",
    "5": "Fixed wing multi engine",
    "6": "Rotorcraft",
    "7": "Weight-shift-control",
    "8": "Powered Parachute",
    "9": "Gyroplane",
    "H": "Hybrid Lift",
    "O": "Other",
}
GOVERNMENT = "5"
US_MILITARY = (0xADF7C8, 0xAFFFFF)
PLANE_ALERT_CLASS = {"Mil": "military", "Pol": "law_enforcement", "Gov": "government"}

ARMED_FORCE = re.compile(r"\b(AIR FORCE|NAVY|NAVAL|ARMY|MARINE CORPS|NATIONAL GUARD|DEPARTMENT OF DEFENSE|COAST GUARD)\b")
LAW_ENFORCEMENT = re.compile(
    r"\b(POLICE|SHERIFFS?|HIGHWAY PATROL|STATE PATROL|PUBLIC SAFETY|LAW ENFORCEMENT|MARSHALS?|CUSTOMS|"
    r"BORDER PROTECTION|DRUG ENFORCEMENT|FEDERAL BUREAU OF INVESTIGATION|STATE TROOPERS?|"
    r"DEPARTMENT OF CORRECTIONS|CONSTABLE|HOMELAND SECURITY|IMMIGRATION|AIR SUPPORT|[A-Z]{1,4}PD)\b"
)
CIVIL_AIR_PATROL = re.compile(r"\bCIVIL AIR PATROL\b")
AIRLINE_CALLSIGN = re.compile(r"^([A-Z]{3})(\d{1,4}[A-Z]{0,2})$")
HEX6 = re.compile(r"^[0-9A-F]{6}$")

SCHEMA = """
CREATE TABLE aircraft (
    icao TEXT PRIMARY KEY,
    faa INTEGER NOT NULL DEFAULT 0,
    registration TEXT,
    manufacturer TEXT,
    model TEXT,
    type_aircraft TEXT,
    registrant_type TEXT,
    owner TEXT,
    pa_class TEXT,
    pa_operator TEXT,
    pa_type TEXT,
    pa_category TEXT,
    pa_tags TEXT
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def normalise_icao(text: str) -> str | None:
    v = (text or "").strip().upper()
    return v if HEX6.match(v) else None


def classify(icao: str, callsign: str | None, rec: dict | None) -> tuple[str, str]:
    """(class, why) for an aircraft, from its database record (or None)
    and the callsign it is flying with; see the module's table."""
    rec = rec or {}
    pa = rec.get("pa_class")
    cat = rec.get("pa_category")
    if pa in ("Mil", "Pol"):
        return PLANE_ALERT_CLASS[pa], f"plane-alert-db: {cat or pa}"
    try:
        addr = int(icao, 16)
    except (TypeError, ValueError):
        addr = -1
    if US_MILITARY[0] <= addr <= US_MILITARY[1]:
        return "military", "US address above the civil N-number range (ADF7C8-AFFFFF)"
    owner = (rec.get("owner") or "").upper()
    government = rec.get("registrant_type") == GOVERNMENT
    if government and ARMED_FORCE.search(owner):
        return "military", f"FAA: government registrant, {rec.get('owner')}"
    if government and LAW_ENFORCEMENT.search(owner):
        return "law_enforcement", f"FAA: government registrant, {rec.get('owner')}"
    if pa == "Gov":
        return "government", f"plane-alert-db: {cat or pa}"
    if government:
        return "government", f"FAA: government registrant, {rec.get('owner')}"
    cs = (callsign or "").strip().upper()
    if CIVIL_AIR_PATROL.search(owner) or cs.startswith("CAP") and AIRLINE_CALLSIGN.match(cs):
        return "government", "Civil Air Patrol (the US Air Force's auxiliary)"
    if m := AIRLINE_CALLSIGN.match(cs):
        return "commercial", f"airline-style callsign ({m.group(1)})"
    if rec.get("faa"):
        kind = REGISTRANT.get(rec.get("registrant_type") or "")
        return "private", f"FAA: {kind.lower()} registrant" if kind else "FAA registration"
    if pa == "Civ":
        return "private", f"plane-alert-db: {cat or 'civil'}"
    return "unknown", "not in the aircraft database"


def describe(icao: str, callsign: str | None, rec: dict | None) -> dict:
    """What the API gives for one aircraft: its class and why, and what the
    databases know about it."""
    cls, why = classify(icao, callsign, rec)
    out: dict = {"class": cls, "why": why}
    if rec:
        for key, name in (
            ("registration", "registration"),
            ("manufacturer", "manufacturer"),
            ("model", "model"),
            ("owner", "owner"),
            ("pa_operator", "operator"),
            ("pa_type", "type"),
            ("pa_category", "category"),
        ):
            if rec.get(key):
                out[name] = rec[key]
        if rec.get("type_aircraft"):
            out["kind"] = AIRCRAFT_TYPE.get(rec["type_aircraft"], rec["type_aircraft"])
        if rec.get("registrant_type"):
            out["registrant"] = REGISTRANT.get(rec["registrant_type"], rec["registrant_type"])
        if rec.get("pa_tags"):
            out["tags"] = rec["pa_tags"].split("|")
    return out


# --- Building aircraft.db -------------------------------------------------------------


def _rows(member: io.BufferedIOBase) -> Iterable[dict]:
    """A FAA text file as dicts keyed by its stripped header names."""
    text = io.TextIOWrapper(member, encoding="utf-8-sig", errors="replace", newline="")
    reader = csv.reader(text)
    head = [h.strip() for h in next(reader)]
    for row in reader:
        yield {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(head) if h}


def build(db_path: Path, faa_zip: Path, plane_alert_csv: Path, now: float | None = None) -> dict:
    """Builds aircraft.db at db_path from the two downloads, in a temporary
    file swapped into place, so a reader never sees half of one. Returns
    the counts it stored in meta."""
    tmp = db_path.with_name(db_path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        counts = _build(tmp, faa_zip, plane_alert_csv, now)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, db_path)
    return counts


def _build(tmp: Path, faa_zip: Path, plane_alert_csv: Path, now: float | None) -> dict:
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA)
        faa = 0
        with zipfile.ZipFile(faa_zip) as z:
            models: dict[str, tuple[str, str]] = {}
            with z.open("ACFTREF.txt") as f:
                for r in _rows(f):
                    models[r.get("CODE", "")] = (r.get("MFR", ""), r.get("MODEL", ""))
            batch: list[tuple] = []
            with z.open("MASTER.txt") as f:
                for r in _rows(f):
                    icao = normalise_icao(r.get("MODE S CODE HEX", ""))
                    if icao is None:
                        continue
                    mfr, model = models.get(r.get("MFR MDL CODE", ""), ("", ""))
                    # Only what the UI shows: never the registrant's address.
                    batch.append(
                        (
                            icao,
                            "N" + r.get("N-NUMBER", ""),
                            mfr or None,
                            model or None,
                            r.get("TYPE AIRCRAFT") or None,
                            r.get("TYPE REGISTRANT") or None,
                            r.get("NAME") or None,
                        )
                    )
                    if len(batch) >= 5000:
                        faa += _insert_faa(conn, batch)
                        batch = []
            faa += _insert_faa(conn, batch)
            del models
        pa = 0
        with open(plane_alert_csv, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            head = next(reader)
            col = {h: i for i, h in enumerate(head)}
            need = ("$ICAO", "#CMPG")
            if any(h not in col for h in need):
                raise ValueError(f"plane-alert-db's columns changed: {head}")

            def cell(row, name):
                i = col.get(name)
                return row[i].strip() if i is not None and i < len(row) else ""

            for row in reader:
                icao = normalise_icao(cell(row, "$ICAO"))
                cmpg = cell(row, "#CMPG")
                if icao is None or cmpg not in ("Mil", "Pol", "Gov", "Civ"):
                    continue
                category = cell(row, "Category")
                if "://" in category:  # a link that slipped into the wrong column
                    category = ""
                tags = [t for t in (cell(row, "$Tag 1"), cell(row, "$#Tag 2"), cell(row, "$#Tag 3")) if t and "://" not in t]
                conn.execute(
                    "INSERT INTO aircraft (icao, registration, pa_class, pa_operator, pa_type, pa_category, pa_tags) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(icao) DO UPDATE SET pa_class = excluded.pa_class, "
                    "pa_operator = excluded.pa_operator, pa_type = excluded.pa_type, "
                    "pa_category = excluded.pa_category, pa_tags = excluded.pa_tags, "
                    "registration = coalesce(aircraft.registration, excluded.registration)",
                    (
                        icao,
                        cell(row, "$Registration") or None,
                        cmpg,
                        cell(row, "$Operator") or None,
                        cell(row, "$Type") or None,
                        category or None,
                        "|".join(tags) or None,
                    ),
                )
                pa += 1
        meta = {"built_at": str(now if now is not None else time.time()), "faa_rows": str(faa), "plane_alert_rows": str(pa)}
        conn.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        conn.commit()
    finally:
        conn.close()
    if faa == 0:
        raise ValueError("the FAA download held no aircraft")
    return {"faa_rows": faa, "plane_alert_rows": pa}


def _insert_faa(conn: sqlite3.Connection, batch: list[tuple]) -> int:
    conn.executemany(
        "INSERT OR REPLACE INTO aircraft (icao, faa, registration, manufacturer, model, type_aircraft, registrant_type, owner) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        batch,
    )
    return len(batch)


def download(url: str, dest: Path, timeout: float = 300.0) -> None:
    """Fetches url into dest (through a temporary file), as a plain client
    that says what it is."""
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            size = 0
            while chunk := r.read(1 << 20):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ValueError(f"{url} is larger than {MAX_DOWNLOAD >> 20} MiB")
                f.write(chunk)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


# --- The database in service -------------------------------------------------------------


@dataclass
class AircraftDB:
    """aircraft.db in `path`, kept fresh by a background thread when
    `enabled`, and looked up per request."""

    path: Path
    enabled: bool = False
    fetch: Callable[[str, Path], None] = download
    clock: Callable[[], float] = time.time
    max_age_s: float = MAX_AGE_S
    retry_s: float = RETRY_S
    last_error: str | None = None
    last_attempt: float | None = None
    refreshing: bool = False
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self) -> None:
        if self.enabled and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="aircraft-db", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.due():
                self.refresh()
            self._stop.wait(CHECK_S)

    def meta(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with closing(self._open()) as conn:
                return dict(conn.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.Error:
            return {}

    def due(self) -> bool:
        now = self.clock()
        if self.last_error and self.last_attempt is not None and now - self.last_attempt < self.retry_s:
            return False
        built = self.meta().get("built_at")
        return built is None or now - float(built) >= self.max_age_s

    def refresh(self) -> bool:
        """Downloads both sources and rebuilds; True when it worked. The
        old database stays in use until the new one is complete."""
        with self._lock:
            self.refreshing = True
            self.last_attempt = self.clock()
            work = self.path.parent / "aircraftdb-tmp"
            try:
                work.mkdir(parents=True, exist_ok=True)
                faa, pa = work / "ReleasableAircraft.zip", work / "plane-alert-db.csv"
                self.fetch(FAA_URL, faa)
                self.fetch(PLANE_ALERT_URL, pa)
                build(self.path, faa, pa, now=self.clock())
                self.last_error = None
                return True
            except Exception as exc:  # a failed refresh keeps the old database
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                return False
            finally:
                for p in work.glob("*"):
                    p.unlink(missing_ok=True)
                try:
                    work.rmdir()
                except OSError:
                    pass
                self.refreshing = False

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def lookup(self, wanted: list[tuple[str, str | None]]) -> dict[str, dict]:
        """{icao: describe(...)} for each (icao, callsign); addresses that
        aren't 24-bit hex are skipped."""
        pairs = [(i, c) for i, c in ((normalise_icao(i), c) for i, c in wanted) if i]
        recs: dict[str, dict] = {}
        if pairs and self.path.exists():
            try:
                with closing(self._open()) as conn:
                    keys = sorted({i for i, _ in pairs})
                    for k in range(0, len(keys), 500):
                        chunk = keys[k : k + 500]
                        q = f"SELECT * FROM aircraft WHERE icao IN ({','.join('?' * len(chunk))})"
                        for row in conn.execute(q, chunk):
                            recs[row["icao"]] = dict(row)
            except sqlite3.Error:
                recs = {}
        return {i: describe(i, c, recs.get(i)) for i, c in pairs}

    def status(self) -> dict:
        m = self.meta()
        return {
            "enabled": self.enabled,
            "built_at": float(m["built_at"]) if m.get("built_at") else None,
            "faa_rows": int(m["faa_rows"]) if m.get("faa_rows") else 0,
            "plane_alert_rows": int(m["plane_alert_rows"]) if m.get("plane_alert_rows") else 0,
            "refreshing": self.refreshing,
            "last_error": self.last_error,
        }
