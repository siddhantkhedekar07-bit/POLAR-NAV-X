"""FastAPI service exposing the trained XGBoost iceberg trajectory models."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.iceberg_service import IcebergDataError, iceberg_service
from app.model_service import ModelLoadError, model_service
from app.schemas import HealthResponse, Iceberg, PredictionRequest, PredictionResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        model_service.load()
        logger.info("Trained trajectory models loaded")
    except ModelLoadError:
        logger.exception("Could not load trained trajectory models")
    yield


app = FastAPI(
    title="POLAR-NAV-X Iceberg Trajectory API",
    description="Serves the trained XGBoost models that forecast iceberg positions.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok", model_loaded=model_service.is_loaded)


@app.post("/predict", response_model=PredictionResponse)
def predict(request: PredictionRequest):
    if not model_service.is_loaded:
        raise HTTPException(
            status_code=503,
            detail="Trajectory models are not loaded; see server logs for details",
        )

    try:
        latitude, longitude = model_service.predict(request.model_dump())
    except Exception:
        logger.exception("Prediction failed")
        raise HTTPException(status_code=500, detail="Prediction failed")

    return PredictionResponse(predicted_latitude=latitude, predicted_longitude=longitude)


@app.get("/api/icebergs", response_model=list[Iceberg])
def icebergs():
    """Latest available Antarctic iceberg observations from USNIC (BYU fallback)."""
    try:
        records = iceberg_service.get_icebergs()
    except IcebergDataError:
        logger.exception("Could not retrieve iceberg observations")
        raise HTTPException(
            status_code=503,
            detail="Latest iceberg observations are currently unavailable upstream",
        )

    return [Iceberg(**record) for record in records]
