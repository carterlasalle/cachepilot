import sqlite3

c = sqlite3.connect("/tmp/r9-wrong.db")
c.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, name TEXT)")
c.commit()
c.close()
print("created /tmp/r9-wrong.db (unrelated schema)")