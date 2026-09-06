---
name: polar-api-testing
description: Run POLAR-NAV-X backend runtime checks using Swagger and real iceberg sources.
---

# Backend runtime testing

## Devin Secrets Needed
None for local Swagger or public USNIC/BYU data.

## Start the service
- Use a repository-root Python venv with `backend/requirements.txt` installed.
- XGBoost model loading also needs scikit-learn; check it is installed if health reports unloaded models. Version 1.7.2 worked with the supplied artifacts.
- Run from `backend`: `../.venv/bin/python -m uvicorn app.main:app --port 8000`.
- Do not regenerate or modify `ml/models/` for API testing.
- Open `http://localhost:8000/docs`; expand an operation, click Try it out, then Execute.
- Use the sixteen-feature sample in `backend/README.md`, not Swagger's generated boundary-value example, for prediction plausibility.

## Live observation checks
- Compare the actual Swagger response against a separately downloaded official CSV at `https://usicecenter.gov/File/DownloadCurrent?pId=134`.
- Decode the CSV with `utf-8-sig` and compare complete ID sets, coordinates, dimensions and observation dates. Avoid hard-coding the current iceberg names or dates.
- Inspect the actual Server response section, not Swagger's Example Value.
- Cache is per process; restart once before measuring cold/warm requests.
- Passive browser fetch instrumentation can capture exact payloads and request durations while native clicks trigger requests.
- To verify cache avoids upstream access, start the server under `strace -f -tt -e trace=connect -o /tmp/polar-network.log ...`; comparing logs after cold and warm calls should show no additional upstream connection on the warm call. Attaching strace to an already running process may be restricted.
- Do not claim fallback, total-source failure or cache expiry were covered unless those runtime states were actually induced.
