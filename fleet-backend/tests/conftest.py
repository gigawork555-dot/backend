# tests/conftest.py
"""
Helper กลางสำหรับ print ค่าจริงที่ได้จากฟังก์ชันก่อน assert
ทำให้เวลารัน pytest -v -s เห็นค่าที่คำนวณได้จริง ไม่ใช่แค่ PASSED/FAILED

วิธีใช้ในไฟล์ test:
    from conftest import check, check_is

    result = filter_imu_noise_event(ax=-0.5, ay=0.0, az=1.0)
    check_is("is_harsh_braking",      result["is_harsh_braking"],      True)
    check_is("is_harsh_acceleration", result["is_harsh_acceleration"], False)
    check_is("is_harsh_cornering",    result["is_harsh_cornering"],    False)

ผลลัพธ์ตอนรันจะเห็นบรรทัดแบบนี้ก่อน assert ทุกตัว (ต้องใช้ -s):
    🔎 is_harsh_braking      -> actual=True   | expected=True   ✅
"""


def check(label, actual, expected):
    """เทียบด้วย == พร้อม print ค่าจริง/ค่าคาดหวังก่อนเสมอ"""
    ok = actual == expected
    mark = "✅" if ok else "❌"
    print(f"  🔎 {label:<28} -> actual={actual!r:<12} expected={expected!r:<12} {mark}")
    assert ok, f"{label} FAILED: actual={actual!r}, expected={expected!r}"


def check_is(label, actual, expected):
    """เทียบด้วย is (สำหรับ True/False/None) พร้อม print ค่าจริง/ค่าคาดหวังก่อนเสมอ"""
    ok = actual is expected
    mark = "✅" if ok else "❌"
    print(f"  🔎 {label:<28} -> actual={actual!r:<12} expected={expected!r:<12} {mark}")
    assert ok, f"{label} FAILED: actual={actual!r}, expected={expected!r} (is-check)"


def check_approx(label, actual, expected, abs_tol=1e-6):
    """เทียบตัวเลขทศนิยมแบบ approx พร้อม print"""
    ok = abs(actual - expected) <= abs_tol
    mark = "✅" if ok else "❌"
    print(f"  🔎 {label:<28} -> actual={actual!r:<12} expected≈{expected!r:<12} {mark}")
    assert ok, f"{label} FAILED: actual={actual!r}, expected≈{expected!r}"


def check_range(label, actual, low, high):
    """เช็คว่าค่าอยู่ในช่วง [low, high] พร้อม print"""
    ok = low <= actual <= high
    mark = "✅" if ok else "❌"
    print(f"  🔎 {label:<28} -> actual={actual!r:<12} expected in [{low}, {high}] {mark}")
    assert ok, f"{label} FAILED: actual={actual!r} not in [{low}, {high}]"
