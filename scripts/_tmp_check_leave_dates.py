import sqlite3

p = r"C:\Users\z0017fzc\Projects\SCRUM_vF\data\team_tracker.db"
c = sqlite3.connect(p)
print("leave_requests", c.execute("select count(*) from leave_requests").fetchone()[0])
print("min start", c.execute("select min(start_date) from leave_requests").fetchone()[0])
print("max end", c.execute("select max(end_date) from leave_requests").fetchone()[0])
q = """select count(*) from leave_requests
 where start_date<='2026-06-30' and end_date>='2026-06-01'
 and status in ('pending','approved')"""
print("June 2026 overlap", c.execute(q).fetchone()[0])
q2 = """select count(*) from leave_requests
 where start_date<='2026-05-31' and end_date>='2026-05-01'
 and status in ('pending','approved')"""
print("May 2026 overlap", c.execute(q2).fetchone()[0])
# sample employees with leave in june
q3 = """select distinct employee_name from leave_requests
 where start_date<='2026-06-30' and end_date>='2026-06-01'
 and status in ('pending','approved') limit 10"""
print("sample june names", [r[0] for r in c.execute(q3)])
c.close()
