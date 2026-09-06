"""Retrieval, parsing and caching of the latest available Antarctic iceberg positions.

Primary source: U.S. National Ice Center (USNIC) "Antarctic Icebergs" CSV product,
linked from https://usicecenter.gov/Products/AntarcIcebergs/ and served from
``/File/DownloadCurrent?pId=134``.

Fallback source: NASA SCP / BYU "Current Antarctic Iceberg Positions" HTML table
(https://www.scp.byu.edu/current_icebergs.html), which is derived from
near-real-time ASCAT/OSCAT-2 data.
"""

import csv
import io
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

USNIC_CSV_URL = "https://usicecenter.gov/File/DownloadCurrent?pId=134"
BYU_HTML_URL = "https://www.scp.byu.edu/current_icebergs.html"

USNIC_SOURCE = "USNIC"
BYU_SOURCE = "NASA SCP/BYU"

REQUEST_TIMEOUT = 20.0
CACHE_TTL_SECONDS = 60 * 60

_BYU_ROW = re.compile(
    r"<tr>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*<td>([^<]+)</td>\s*</tr>",
    re.IGNORECASE,
)
_BYU_REVISED = re.compile(r"Last revised:\s*[\d:]+\s*(\d{2})/(\d{2})/(\d{2})")
_BYU_COORD = re.compile(r"^\s*(\d+)\s+(\d+(?:\.\d+)?)'?\s*([NSEW])\s*$", re.IGNORECASE)


class IcebergDataError(RuntimeError):
    """Raised when no iceberg observations could be retrieved from any source."""


def _to_float(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _is_valid_position(latitude, longitude):
    return (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def parse_usnic_csv(text):
    """Parse the USNIC Antarctic iceberg CSV into normalized records."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    records = []

    for row in reader:
        cleaned = {
            (key or "").strip().lstrip("\ufeff"): (value or "").strip()
            for key, value in row.items()
            if key is not None
        }

        iceberg_id = cleaned.get("Iceberg", "").upper()
        latitude = _to_float(cleaned.get("Latitude"))
        longitude = _to_float(cleaned.get("Longitude"))

        if not iceberg_id or not _is_valid_position(latitude, longitude):
            logger.warning("Skipping unusable USNIC record: %s", cleaned)
            continue

        records.append(
            {
                "id": iceberg_id,
                "latitude": latitude,
                "longitude": longitude,
                "length_nm": _to_float(cleaned.get("Length (NM)")),
                "width_nm": _to_float(cleaned.get("Width (NM)")),
                "last_updated": _parse_usnic_date(cleaned.get("Last Update")),
                "source": USNIC_SOURCE,
            }
        )

    return records


def _parse_usnic_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError:
        logger.warning("Unrecognised USNIC date: %r", value)
        return None


def dms_to_decimal(value):
    """Convert a BYU coordinate such as ``50 40'W`` into signed decimal degrees."""
    match = _BYU_COORD.match(value or "")
    if not match:
        return None

    degrees, minutes, hemisphere = match.groups()
    decimal = float(degrees) + float(minutes) / 60
    if hemisphere.upper() in ("S", "W"):
        decimal = -decimal
    return round(decimal, 4)


def _byu_observation_date(day_of_year, revised_on):
    if day_of_year is None or revised_on is None:
        return None

    year = revised_on.year
    observed = date(year, 1, 1) + timedelta(days=int(day_of_year) - 1)
    if observed > revised_on:
        observed = date(year - 1, 1, 1) + timedelta(days=int(day_of_year) - 1)
    return observed


def parse_byu_html(html):
    """Parse the BYU current-icebergs HTML table into normalized records."""
    revised_match = _BYU_REVISED.search(html)
    revised_on = None
    if revised_match:
        month, day, year = (int(part) for part in revised_match.groups())
        revised_on = date(2000 + year, month, day)

    records = []
    for name, longitude_text, latitude_text, day_of_year_text in _BYU_ROW.findall(html):
        iceberg_id = name.strip().upper()
        latitude = dms_to_decimal(latitude_text)
        longitude = dms_to_decimal(longitude_text)

        if not iceberg_id or not _is_valid_position(latitude, longitude):
            continue

        records.append(
            {
                "id": iceberg_id,
                "latitude": latitude,
                "longitude": longitude,
                "length_nm": None,
                "width_nm": None,
                "last_updated": _byu_observation_date(
                    _to_float(day_of_year_text), revised_on
                ),
                "source": BYU_SOURCE,
            }
        )

    return records


def _fetch(url):
    response = httpx.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
    response.raise_for_status()
    return response.text


class IcebergService:
    """Fetches the latest iceberg observations and caches them in memory."""

    def __init__(self, ttl_seconds=CACHE_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0.0

    def clear_cache(self):
        with self._lock:
            self._cached = None
            self._cached_at = 0.0

    def get_icebergs(self, refresh=False):
        with self._lock:
            if not refresh and self._cached is not None:
                if time.monotonic() - self._cached_at < self._ttl:
                    return self._cached

            records = self._fetch_latest()
            self._cached = records
            self._cached_at = time.monotonic()
            return records

    def _fetch_latest(self):
        errors = []

        for label, url, parser in (
            (USNIC_SOURCE, USNIC_CSV_URL, parse_usnic_csv),
            (BYU_SOURCE, BYU_HTML_URL, parse_byu_html),
        ):
            try:
                records = parser(_fetch(url))
            except Exception as exc:
                logger.warning("Iceberg source %s unavailable: %s", label, exc)
                errors.append(f"{label}: {exc}")
                continue

            if records:
                return records

            errors.append(f"{label}: no usable records")

        raise IcebergDataError("; ".join(errors))


iceberg_service = IcebergService()
