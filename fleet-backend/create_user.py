# create_user.py
"""
สคริปต์สำหรับสร้าง user ใหม่ (username + password) ลงตาราง `users`
ของ KTC Fleet Telematics backend

ใช้ pattern เดียวกับ fleet-backend/fix_admin_password.py ที่มีอยู่แล้ว
(bcrypt cost=12 + asyncpg) เพื่อให้ hash ตรงกับที่
app/auth/dependencies.py -> verify_password() ใช้ bcrypt.checkpw() เทียบ

ตาราง users (จาก docker/postgres/init.sql):
    id              SERIAL PRIMARY KEY
    username        VARCHAR(50)  UNIQUE NOT NULL
    email           VARCHAR(100) UNIQUE NOT NULL
    hashed_password VARCHAR(255) NOT NULL
    full_name       VARCHAR(100)
    is_active       BOOLEAN DEFAULT true
    role            VARCHAR(20)  DEFAULT 'user'   -- user | manager | admin
    created_at      TIMESTAMPTZ  DEFAULT NOW()

วิธีใช้ (PowerShell / Windows environment ตาม memory ของโปรเจกต์):

    # แก้ค่า USERS_TO_CREATE ด้านล่างก่อน แล้วรันผ่าน backend container
    docker compose run --rm backend python create_user.py

    หรือถ้ารันจากเครื่อง host ตรงๆ (ต้องมี asyncpg + bcrypt +
    เข้าถึง DB port 5435 ตาม docker-compose.yml):

    python create_user.py

ปรับ DB connection ด้านล่างให้ตรงกับ environment ของคุณ
(ค่า default อิงจาก fix_admin_password.py เดิม: host=localhost,
port=5435, user=fleet_user, password=fleet_pass, database=fleet_db)
"""

import asyncio
import sys

import bcrypt
import asyncpg
import os
# ─────────────────────────────────────────────────────────────
# 1) ตั้งค่าการเชื่อมต่อฐานข้อมูล — แก้ให้ตรง environment ของคุณ
# ─────────────────────────────────────────────────────────────
_IN_DOCKER = os.environ.get("RUN_IN_DOCKER") == "1"

DB_CONFIG = dict(
    host="timescaledb" if _IN_DOCKER else "localhost",
    port=5432 if _IN_DOCKER else 5435,
    user="fleet_user",
    password="fleet_pass",
    database="fleet_db",
)

# ─────────────────────────────────────────────────────────────
# 2) รายชื่อ user ที่ต้องการสร้าง — แก้/เพิ่มรายการได้ตามต้องการ
#    role: "user" | "manager" | "admin"
# ─────────────────────────────────────────────────────────────
USERS_TO_CREATE = [
    {
        "username": "admin",
        "email": "admin@kotchasaan.local",
        "password": "admin1234",
        "full_name": "System Administrator",
        "role": "admin",
    },
    {
        "username": "fleet_manager",
        "email": "manager@kotchasaan.local",
        "password": "manager1234",
        "full_name": "Fleet Manager",
        "role": "manager",
    },
    # เพิ่ม dict แบบนี้ต่อได้เรื่อยๆ
    # {
    #     "username": "viewer1",
    #     "email": "viewer1@kotchasaan.local",
    #     "password": "viewer1234",
    #     "full_name": "Viewer Account",
    #     "role": "user",
    # },
]


def hash_password(plain: str) -> str:
    """เหมือน hash_password() ใน app/auth/dependencies.py (bcrypt cost=12)"""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(12)).decode()


async def create_users():
    conn = await asyncpg.connect(**DB_CONFIG)
    print(f"เชื่อมต่อฐานข้อมูล {DB_CONFIG['database']}@{DB_CONFIG['host']}:{DB_CONFIG['port']} สำเร็จ\n")

    created, skipped, failed = 0, 0, 0

    try:
        for u in USERS_TO_CREATE:
            username = u["username"]
            email = u["email"]
            password_hash = hash_password(u["password"])
            full_name = u.get("full_name")
            role = u.get("role", "user")

            # กันสร้างซ้ำ — username หรือ email ต้อง unique ตาม schema
            existing = await conn.fetchrow(
                "SELECT id FROM users WHERE username = $1 OR email = $2",
                username, email,
            )

            if existing:
                print(f"ข้าม '{username}' — มีอยู่แล้วในระบบ (id={existing['id']})")
                skipped += 1
                continue

            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO users
                        (username, email, hashed_password, full_name, is_active, role)
                    VALUES
                        ($1, $2, $3, $4, TRUE, $5)
                    RETURNING id, username, role, created_at
                    """,
                    username, email, password_hash, full_name, role,
                )
                print(
                    f"สร้างสำเร็จ: id={row['id']} username={row['username']} "
                    f"role={row['role']} created_at={row['created_at']}"
                )
                created += 1

            except Exception as e:
                print(f"สร้าง '{username}' ล้มเหลว: {e}")
                failed += 1

    finally:
        await conn.close()

    print(f"\nสรุป: สร้างใหม่ {created} | ข้าม (มีอยู่แล้ว) {skipped} | ล้มเหลว {failed}")


if __name__ == "__main__":
    try:
        asyncio.run(create_users())
    except Exception as e:
        print(f"เชื่อมต่อหรือรันสคริปต์ล้มเหลว: {e}")
        sys.exit(1)
