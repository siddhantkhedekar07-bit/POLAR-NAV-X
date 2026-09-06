# Backend - Iceberg Trajectory Prediction API

FastAPI service that serves the trained XGBoost models from `ml/models/`.

```
backend/
├── app/
│   ├── main.py             # FastAPI app, endpoints, error handling
│   ├── model_service.py    # Model loading at startup + inference
│   ├── iceberg_service.py  # Latest iceberg observations: fetch, parse, cache
│   └── schemas.py          # Pydantic request/response models
└── requirements.txt
```

## Setup

```bash
pip install -r backend/requirements.txt
```

The models must exist at `ml/models/latitude_model.json`,
`ml/models/longitude_sin_model.json` and `ml/models/longitude_cos_model.json`.
Regenerate them with `python ml/train_xgboost.py` (see `ml/README.md`).

## Run

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

Models are loaded once during application startup, not per request.

## Endpoints

### `GET /health`

```json
{ "status": "ok", "model_loaded": true }
```

### `POST /predict`

Body — the 16 training features, in the training order:

```json
{
  "latitude": -75.42,
  "longitude": -39.83,
  "previous_latitude": -75.40,
  "previous_longitude": -39.80,
  "delta_latitude": -0.02,
  "delta_longitude_wrapped": -0.03,
  "time_difference": 1.0,
  "speed": 0.42,
  "lat_velocity": -0.02,
  "lon_velocity": -0.03,
  "movement_distance_deg": 0.036,
  "movement_rate_deg_per_day": 0.036,
  "year": 2026,
  "month": 9,
  "day_of_year": 248,
  "target_time_difference": 1.0
}
```

Response:

```json
{ "predicted_latitude": -75.44, "predicted_longitude": -39.86 }
```

### `GET /api/icebergs`

Latest available Antarctic iceberg observations, normalized:

```json
[
  {
    "id": "A23A",
    "latitude": -60.72,
    "longitude": -49.1,
    "length_nm": 40.0,
    "width_nm": 30.0,
    "last_updated": "2026-09-04",
    "source": "USNIC"
  }
]
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Iceberg designator (e.g. `A23A`), upper-cased |
| `latitude` | float | Decimal degrees, `-90..90` |
| `longitude` | float | Decimal degrees, `-180..180` |
| `length_nm` | float \| null | Nautical miles; null when the source omits it |
| `width_nm` | float \| null | Nautical miles; null when the source omits it |
| `last_updated` | date \| null | Observation/update date reported by the source |
| `source` | string | `USNIC` or `NASA SCP/BYU` |

#### Data sources

1. **Primary — U.S. National Ice Center (USNIC)**, Antarctic Icebergs CSV product
   linked from <https://usicecenter.gov/Products/AntarcIcebergs/> and downloaded
   from `https://usicecenter.gov/File/DownloadCurrent?pId=134`. USNIC publishes
   iceberg position, length and width in nautical miles and a `Last Update` date;
   the product is refreshed roughly weekly.
2. **Fallback — NASA SCP / BYU**, "Current Antarctic Iceberg Positions"
   (<https://www.scp.byu.edu/current_icebergs.html>), derived from near-real-time
   ASCAT/OSCAT-2 data and updated once or twice a week. Its coordinates are given
   as degrees/minutes with a hemisphere (`50 40'W`) and are converted to decimal
   degrees; the observation date is reconstructed from the day-of-year column and
   the page's "Last revised" timestamp. Dimensions are not published there, so
   `length_nm`/`width_nm` are null. BYU is used only when USNIC is unreachable or
   returns no usable records.

Nothing is downloaded to disk or committed to the repository: the CSV/HTML is
fetched on demand and parsed in memory.

#### Validation

Records without an iceberg ID, without parseable coordinates, or with
`latitude` outside `-90..90` or `longitude` outside `-180..180` are dropped
rather than returned. Optional fields that the source does not provide are
returned as `null`.

#### Caching

Results are cached in process for one hour (`CACHE_TTL_SECONDS` in
`app/iceberg_service.py`); upstream is only contacted when the cache is empty or
stale. The cache is per worker process and is lost on restart — no Redis,
database or background job is involved.

#### Errors

If both sources fail (network error, HTTP error, unparseable payload, or no
valid records), the endpoint returns HTTP 503 with a short message; the
application keeps running and other endpoints are unaffected.

#### Limitations

- The data is the **latest available / near-real-time** observation set, not an
  operational real-time feed. Positions can be several days old.
- This endpoint is **not an operational navigation service**. Both sources warn
  that their lists cannot contain every hazardous iceberg, and smaller fragments
  are not tracked. Do not use it for navigation decisions.
- Only large, named/tracked icebergs are included.

### Prediction model notes

The longitude models predict the sine and cosine of the target angle; the API
decodes them with `atan2` and returns a longitude in (-180, 180].

Unknown fields are rejected and missing/invalid fields return HTTP 422. If the
models could not be loaded, `/predict` returns HTTP 503 and `/health` reports
`"model_loaded": false`; unexpected inference failures return HTTP 500 without
leaking a stack trace.

Interactive docs: `http://localhost:8000/docs`.

## Tests

```bash
python -m pytest backend/tests
```

The iceberg tests mock the upstream HTTP request, so they do not require
network access.
