"""Show students/attendance, and add the fictional 4MT24AI054 test record if missing."""
import sqlite3
from pathlib import Path

db = Path(__file__).resolve().with_name("smartbus_ocr_demo.db")
con = sqlite3.connect(db)
con.execute("INSERT OR IGNORE INTO students(usn, name, bus_number, valid_until, active) "
            "VALUES ('4MT24AI054', 'Own Pass Test', 'MITE-BUS-01', '2027-06-30', 1)")
con.commit()
print("students 4MT%:", con.execute(
    "SELECT usn, bus_number, valid_until, active FROM students WHERE usn LIKE '4MT%'").fetchall())
print("attendance:", con.execute(
    "SELECT usn, session, recorded_at FROM attendance ORDER BY id DESC LIMIT 10").fetchall())
print("scans:", con.execute("SELECT COUNT(*) FROM scans").fetchone()[0])
con.close()
