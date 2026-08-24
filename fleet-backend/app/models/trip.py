# app/models/trip.py

from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    Boolean,
    DateTime,
    JSON,
    text
)

from app.models.telemetry import Base


class TripLog(Base):
    """
    โมเดลสรุปผลการขับขี่รายทริป
    ใช้สำหรับ:
    - Driver Score
    - Odoo Sync
    - Dashboard
    - โบนัสพนักงาน
    """

    __tablename__ = "trip_logs"

    # =========================
    # Primary Key
    # =========================
    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    # =========================
    # Device / Vehicle / Driver
    # =========================
    device_id = Column(
        String(50),
        nullable=False,
        index=True
    )

    vehicle_id = Column(
        Integer,
        nullable=True)

    driver_id  = Column(
        Integer,
        nullable=True
    )

    # =========================
    # Trip Time
    # =========================
    trip_start = Column(
        DateTime(timezone=True),
        nullable=False
    )

    trip_end = Column(
        DateTime(timezone=True),
        nullable=True
    )

    # =========================
    # Distance / Duration
    # =========================
    distance_km = Column(
        Float,
        default=0.0
    )

    duration_min = Column(
        Float,
        default=0.0
    )

    # =========================
    # Idle Time
    # =========================
    idle_min = Column(
        Float,
        default=0.0
    )

    # =========================
    # Speed
    # =========================
    max_speed = Column(
        Float,
        default=0.0
    )

    avg_speed = Column(
        Float,
        default=0.0
    )

    # =========================
    # Driving Events
    # =========================
    harsh_brake_count = Column(
        Integer,
        default=0
    )

    harsh_accel_count = Column(
        Integer,
        default=0
    )

    harsh_corner_count = Column(
        Integer,
        default=0
    )

    speeding_count = Column(
        Integer,
        default=0
    )

    # =========================
    # Driver Score
    # =========================
    driver_score = Column(
        Float,
        default=100.0
    )

    # =========================
    # Fuel
    # =========================
    fuel_used = Column(
        Float,
        default=0.0
    )

    # =========================
    # GPS Track
    # =========================
    gps_track = Column(
        JSON,
        nullable=True
    )

    # =========================
    # Odoo Sync Status
    # =========================
    synced_to_odoo = Column(
        Boolean,
        default=False
    )

    # =========================
    # Created Timestamp
    # =========================
    created_at = Column(
        DateTime(timezone=True),
        server_default=text("NOW()")
    )