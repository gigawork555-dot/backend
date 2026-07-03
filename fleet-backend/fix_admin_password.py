import bcrypt
import asyncio
import asyncpg

async def update():
    # สร้าง hash ใหม่
    password_hash = bcrypt.hashpw(b'admin1234', bcrypt.gensalt(12)).decode()
    print(f"Hash: {password_hash}")

    # อัปเดตลง DB
    conn = await asyncpg.connect(
        host="localhost",
        port=5435,
        user="fleet_user",
        password="fleet_pass",
        database="fleet_db"
    )
    await conn.execute(
        "UPDATE users SET hashed_password = $1 WHERE username = 'admin'",
        password_hash
    )
    await conn.close()
    print("✅ อัปเดตสำเร็จ")

asyncio.run(update())
