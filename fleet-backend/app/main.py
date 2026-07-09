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
    get_db_pool,
)

from app.cache import (
    create_redis_pool,
    close_redis_pool,
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
# Data Retention — FDD v1.4 §13
# "trip summary 3 ปี" — trip_logs ไม่ใช่ TimescaleDB hypertable
# (ต่างจาก telemetry_raw ที่ใช้ add_retention_policy() ใน init.sql
# ได้โดยตรง) จึงต้องลบเองผ่าน DELETE ที่รันเป็น background task
# ──────────────────────────────────────────────

TRIP_LOGS_RETENTION_INTERVAL_SECONDS: int = 24 * 60 * 60  # ทุก 24 ชั่วโมง
TRIP_LOGS_RETENTION_PERIOD: str = "3 years"               # FDD §13


async def trip_logs_retention_task() -> None:
    """
    Background task: ลบ trip_logs ที่ created_at เก่ากว่า 3 ปี
    ตาม FDD v1.4 §13 Data Retention ("trip summary 3 ปี")

    ออกแบบให้:
    - รันเป็น asyncio loop แยกจาก event loop หลัก ไม่บล็อกการรับ
      request หรือ MQTT worker (เหมือน mqtt_subscriber_task())
    - ครอบ try/except ทุกรอบ — ถ้า DELETE ล้มเหลว (เช่น DB
      ขาดการเชื่อมต่อชั่วคราว) จะ log แล้วรอรอบถัดไป ไม่ทำให้ task
      ตายเงียบ และไม่ทำให้ FastAPI app ทั้งตัว crash
    - ไม่ลบข้อมูลใดๆ ทันทีตอน deploy — เงื่อนไข WHERE created_at <
      NOW() - INTERVAL '3 years' หมายความว่ารอบแรกที่รันจะลบเฉพาะ
      แถวที่เก่าเกิน 3 ปีจริงๆ ณ ขณะนั้นเท่านั้น (ถ้ายังไม่มีข้อมูล
      เก่าขนาดนั้น จะไม่มีอะไรถูกลบเลย)
    """

    logger.info(
        "[Retention] trip_logs retention task started "
        f"(interval={TRIP_LOGS_RETENTION_INTERVAL_SECONDS}s, "
        f"keep={TRIP_LOGS_RETENTION_PERIOD})"
    )

    while True:

        try:
            pool = await get_db_pool()

            # หมายเหตุ: ห้ามใช้ "$1::interval" ตรงๆ กับการ bind python str —
            # asyncpg จะพยายาม encode ค่าเป็น interval type ที่ฝั่ง client
            # เอง (คาดหวัง timedelta object) แล้ว error ก่อนถึง Postgres
            # ("'str' object has no attribute 'days'") แก้โดย concat
            # string ว่างก่อน แล้วค่อย cast — บังคับให้ asyncpg ส่งเป็น
            # text ธรรมดา ให้ Postgres เป็นผู้ parse interval เอง
            # (ใช้ pattern เดียวกับ routes_reports.py: "($1 || ' days')::interval")
            result = await pool.execute(
                "DELETE FROM trip_logs WHERE created_at < NOW() - ($1 || '')::interval",
                TRIP_LOGS_RETENTION_PERIOD,
            )

            logger.info(
                f"[Retention] trip_logs cleanup completed: {result}"
            )

        except asyncio.CancelledError:
            logger.info(
                "[Retention] trip_logs retention task cancelled — shutting down"
            )
            raise

        except Exception:
            # ไม่ raise ต่อ — log แล้วปล่อยให้ loop วนไปรอรอบถัดไป
            logger.exception(
                "[Retention] trip_logs cleanup failed — will retry next cycle"
            )

        try:
            await asyncio.sleep(TRIP_LOGS_RETENTION_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            logger.info(
                "[Retention] trip_logs retention task cancelled during sleep"
            )
            raise


# ──────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────

mqtt_task: asyncio.Task | None = None
trip_retention_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):

    global mqtt_task, trip_retention_task

    logger.info("Application startup")

    try:
        #
        # 1. Create Database Pool
        #
        await create_db_pool()

        logger.info("Database connected")

        #
        # 1b. Create Redis Pool (FDD §11.1 — Session, rate limit,
        #     real-time dashboard cache)
        #
        # หมายเหตุ: create_redis_pool() ไม่ raise แม้ Redis จะต่อไม่ได้
        # (คืน None แล้ว log warning แทน) — Redis เป็น optional cache
        # layer ไม่ใช่ hard dependency เหมือน DB จึงไม่ทำให้ startup
        # ทั้งหมด fail ถ้า Redis ล่มอยู่ตอน deploy
        #
        await create_redis_pool()

        logger.info("Redis pool initialized (or degraded gracefully if unavailable)")

        #
        # 2. Start MQTT Worker
        #
        mqtt_task = asyncio.create_task(
            mqtt_subscriber_task(),
            name="mqtt-subscriber"
        )

        logger.info("MQTT worker started")

        #
        # 3. Start Trip Logs Retention Worker (FDD §13)
        #
        trip_retention_task = asyncio.create_task(
            trip_logs_retention_task(),
            name="trip-logs-retention"
        )

        logger.info("Trip logs retention worker started")

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
        # 2. Stop Trip Logs Retention Worker
        #
        if trip_retention_task is not None:

            trip_retention_task.cancel()

            try:
                await trip_retention_task

            except asyncio.CancelledError:
                logger.info("Trip logs retention worker stopped")

            except Exception:
                logger.exception("Trip logs retention worker shutdown error")

        #
        # 2b. Close Redis Pool
        #
        # ปิดก่อน close_db_pool() — ลำดับ shutdown ย้อนกลับจากตอน
        # startup (Redis ถูกสร้างหลัง DB จึงปิดก่อน DB) โดยไม่แตะ
        # ลำดับเดิมของ MQTT/DB ที่มีอยู่แล้วเลย
        #
        await close_redis_pool()

        #
        # 3. Close Database Pool
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
