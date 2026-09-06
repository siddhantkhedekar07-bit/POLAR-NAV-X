import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app import iceberg_service as service_module
from app.iceberg_service import (
    BYU_SOURCE,
    USNIC_SOURCE,
    IcebergDataError,
    IcebergService,
    dms_to_decimal,
    parse_byu_html,
    parse_usnic_csv,
)
from app.iceberg_service import iceberg_service
from app.main import app

USNIC_CSV = (
    "\ufeffIceberg,Length (NM),Width (NM),Latitude,Longitude,"
    "Area (sqMI),Area (sqNM),Area (sqKM),Last Update\n"
    "A23A,40,30,-60.72,-49.10,1200.00,900.00,3100.00,09/04/2026\n"
    "A76C,16,7,-53.29,-26.88,112.44,84.90,291.21,09/04/2026\n"
)

USNIC_CSV_WITH_BAD_ROWS = USNIC_CSV + (
    "BAD1,10,5,-120.5,-30.0,1.0,1.0,1.0,09/04/2026\n"
    "BAD2,10,5,-70.0,200.0,1.0,1.0,1.0,09/04/2026\n"
    "BAD3,10,5,,,1.0,1.0,1.0,09/04/2026\n"
    ",10,5,-70.0,-30.0,1.0,1.0,1.0,09/04/2026\n"
)

USNIC_CSV_MISSING_FIELDS = (
    "Iceberg,Length (NM),Width (NM),Latitude,Longitude,Last Update\n"
    "D30B,,,-60.57,-45.72,\n"
)

BYU_HTML = """
<p>* Last revised: 16:26:28 09/02/26</p>
<table><tr><td>Iceberg***</td><td>Longitude</td><td>Latitude</td><td>Most recent</td></tr>
<tr><td>a81</td><td>50 40'W</td><td> 58 36'S</td><td> 235</td></tr>
<tr><td>b09b</td><td>143 13'E</td><td> 66 3'S</td><td> 235</td></tr>
<tr><td>bad1</td><td>250 40'W</td><td> 58 36'S</td><td> 235</td></tr>
</table>
"""


class _StubResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def _stub_get(responses):
    """Return an ``httpx.get`` replacement driven by a {url-substring: text|Exception} map."""

    def fake_get(url, **kwargs):
        for key, value in responses.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return _StubResponse(value)
        raise AssertionError(f"unexpected request to {url}")

    return fake_get


@pytest.fixture(autouse=True)
def clear_cache():
    iceberg_service.clear_cache()
    yield
    iceberg_service.clear_cache()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_parse_usnic_csv_normalizes_records():
    records = parse_usnic_csv(USNIC_CSV)

    assert records[0] == {
        "id": "A23A",
        "latitude": -60.72,
        "longitude": -49.10,
        "length_nm": 40.0,
        "width_nm": 30.0,
        "last_updated": date(2026, 9, 4),
        "source": USNIC_SOURCE,
    }
    assert [record["id"] for record in records] == ["A23A", "A76C"]


def test_parse_usnic_csv_drops_invalid_coordinates():
    records = parse_usnic_csv(USNIC_CSV_WITH_BAD_ROWS)
    assert [record["id"] for record in records] == ["A23A", "A76C"]


def test_parse_usnic_csv_allows_missing_optional_fields():
    (record,) = parse_usnic_csv(USNIC_CSV_MISSING_FIELDS)
    assert record["length_nm"] is None
    assert record["width_nm"] is None
    assert record["last_updated"] is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("50 40'W", -50.6667),
        ("143 13'E", 143.2167),
        (" 58 36'S", -58.6),
        ("66 3'N", 66.05),
        ("not a coordinate", None),
    ],
)
def test_dms_to_decimal(text, expected):
    assert dms_to_decimal(text) == expected


def test_parse_byu_html_converts_coordinates_and_dates():
    records = parse_byu_html(BYU_HTML)

    assert [record["id"] for record in records] == ["A81", "B09B"]
    assert records[0]["latitude"] == -58.6
    assert records[0]["longitude"] == -50.6667
    assert records[0]["last_updated"] == date(2026, 8, 23)
    assert records[0]["source"] == BYU_SOURCE
    assert records[0]["length_nm"] is None


def test_service_falls_back_to_byu(monkeypatch):
    monkeypatch.setattr(
        service_module.httpx,
        "get",
        _stub_get(
            {
                "usicecenter.gov": httpx.ConnectError("boom"),
                "scp.byu.edu": BYU_HTML,
            }
        ),
    )

    records = IcebergService().get_icebergs()
    assert {record["source"] for record in records} == {BYU_SOURCE}


def test_service_raises_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr(
        service_module.httpx,
        "get",
        _stub_get(
            {
                "usicecenter.gov": httpx.ConnectError("boom"),
                "scp.byu.edu": httpx.ConnectError("boom"),
            }
        ),
    )

    with pytest.raises(IcebergDataError):
        IcebergService().get_icebergs()


def test_service_caches_between_calls(monkeypatch):
    calls = []

    def counting_get(url, **kwargs):
        calls.append(url)
        return _StubResponse(USNIC_CSV)

    monkeypatch.setattr(service_module.httpx, "get", counting_get)

    service = IcebergService()
    first = service.get_icebergs()
    second = service.get_icebergs()

    assert first == second
    assert len(calls) == 1

    service.get_icebergs(refresh=True)
    assert len(calls) == 2


def test_expired_cache_is_refetched(monkeypatch):
    calls = []

    def counting_get(url, **kwargs):
        calls.append(url)
        return _StubResponse(USNIC_CSV)

    monkeypatch.setattr(service_module.httpx, "get", counting_get)

    service = IcebergService(ttl_seconds=0)
    service.get_icebergs()
    service.get_icebergs()
    assert len(calls) == 2


def test_api_returns_normalized_icebergs(client, monkeypatch):
    monkeypatch.setattr(
        service_module.httpx, "get", _stub_get({"usicecenter.gov": USNIC_CSV_WITH_BAD_ROWS})
    )

    response = client.get("/api/icebergs")
    assert response.status_code == 200

    body = response.json()
    assert body[0] == {
        "id": "A23A",
        "latitude": -60.72,
        "longitude": -49.1,
        "length_nm": 40.0,
        "width_nm": 30.0,
        "last_updated": "2026-09-04",
        "source": "USNIC",
    }
    assert all(-90 <= item["latitude"] <= 90 for item in body)
    assert all(-180 <= item["longitude"] <= 180 for item in body)


def test_api_returns_503_when_upstream_fails(client, monkeypatch):
    monkeypatch.setattr(
        service_module.httpx,
        "get",
        _stub_get(
            {
                "usicecenter.gov": httpx.ConnectError("boom"),
                "scp.byu.edu": httpx.ConnectError("boom"),
            }
        ),
    )

    response = client.get("/api/icebergs")
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
