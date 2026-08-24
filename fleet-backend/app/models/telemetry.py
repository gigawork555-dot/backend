# app/models/telemetry.py
from sqlalchemy import Column, String, Integer, BigInteger, SmallInteger, Float, Boolean, DateTime, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class TelemetryRaw(Base):
    __tablename__ = 'telemetry_raw'
    id        = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(20), nullable=False, index=True)
    ts        = Column(DateTime(timezone=True), nullable=False, index=True)
    lat       = Column(Float)
    lon       = Column(Float)
    speed     = Column(Float)
    heading   = Column(SmallInteger)
    ignition  = Column(Boolean, default=True, nullable=False)
    event     = Column(String(30))
    event_severity = Column(Float)

class ScoringConfig(Base):
    """โมเดลเก็บโครงสร้าง JSON ตัวแปรสูตรคะแนนที่ฝั่งหน้าบ้านปรับแต่งได้อิสระ"""
    __tablename__ = 'scoring_config'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_name = Column(String(50), unique=True, nullable=False)
    config_data = Column(JSONB, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=text('NOW()'), onupdate=text('NOW()'))
