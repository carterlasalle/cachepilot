import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/r14-wrong.db"
c = sqlite3.connect(path)
c.execute("DROP TABLE IF EXISTS unrelated")
c.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, name TEXT)")
c.commit()
c.close()
print(f"created {path} (unrelated schema)")