# app/main.py

import asyncio
import logging
import sys

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings

from app.database import (
    create_db_pool,
    close_db_pool,
)

from app.services.mqtt_subscriber import (
    mqtt_subscriber_task,
)

from app.api import routes_vehicles
from app.api import routes_trips
from app.api import routes_drivers
from app.api import routes_config
from app.api import routes_reports

from app.auth.routes import router as auth_router

# ──────────────────────────────────────────────
# Windows Compatibility
# ──────────────────────────────────────────────

if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="🚀 %(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────

mqtt_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):

    global mqtt_task

    logger.info("Application startup")

    try:
        #
        # 1. Create Database Pool
        #
        await create_db_pool()

        logger.info("Database connected")

        #
        # 2. Start MQTT Worker
        #
        mqtt_task = asyncio.create_task(
            mqtt_subscriber_task(),
            name="mqtt-subscriber"
        )

        logger.info("MQTT worker started")

        yield

    except Exception:
        logger.exception("Application startup failed")
        raise

    finally:

        logger.info("Application shutdown")

        #
        # 1. Stop MQTT Worker
        #
        if mqtt_task is not None:

            mqtt_task.cancel()

            try:
                await mqtt_task

            except asyncio.CancelledError:
                logger.info("MQTT worker stopped")

            except Exception:
                logger.exception("MQTT worker shutdown error")

        #
        # 2. Close Database Pool
        #
        await close_db_pool()

        logger.info("Application shutdown completed")


# ──────────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────────

app = FastAPI(
    title="Kotchasaan Fleet Management Platform",
    version="2.0.0",
    lifespan=lifespan,

    description="""
# Fleet Management Backend

ระบบบริหารจัดการ Fleet และ Driver Behavior Monitoring

---

## Authentication

สำหรับ Login และ User Management

---

## ESP32 Device APIs

สำหรับ ESP32 / GPS Device

- Device Registration
- Device Configuration
- Telemetry Upload ผ่าน MQTT

---

## Fleet Dashboard APIs

สำหรับ Dashboard และ Live Tracking

- Vehicle List
- Vehicle Location
- Fleet Live Tracking

---

## Odoo Integration APIs

สำหรับเชื่อมต่อ Odoo

- Sync Trip Logs
- Push Scoring Config

---

## Reports APIs

สำหรับรายงานและวิเคราะห์ข้อมูล

- Driver Score
- Fleet Summary
- Fuel Efficiency
- Maintenance Forecast
"""
)

# ──────────────────────────────────────────────
# CORS
# ──────────────────────────────────────────────

frontend_url = getattr(settings, "FRONTEND_URL", None)

if frontend_url:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[frontend_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ──────────────────────────────────────────────
# Static Files
# ──────────────────────────────────────────────

try:
    app.mount(
        "/static",
        StaticFiles(directory="static"),
        name="static"
    )
except Exception:
    pass


@app.get("/tester", include_in_schema=False)
async def api_tester():
    return FileResponse("static/fleet_api_tester.html")

# ──────────────────────────────────────────────
# Routers
# ──────────────────────────────────────────────

app.include_router(routes_vehicles.router)
app.include_router(routes_vehicles.fleet_router)
app.include_router(routes_trips.router)
app.include_router(routes_drivers.router)
app.include_router(routes_config.router)
app.include_router(routes_reports.router)
app.include_router(auth_router)

# ──────────────────────────────────────────────
# Root Endpoint
# ──────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "running",
        "project": (
            "Kotchasaan Fleet Telematics "
            "& Driver Behavior Monitoring System"
        ),
        "compliance": "FDD v1.4 Full",
        "version": "2.0.0",
        "tester_ui": "/tester",
        "docs": "/docs",
    }