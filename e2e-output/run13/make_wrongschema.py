import sqlite3

c = sqlite3.connect("/tmp/r13-wrong.db")
c.execute("DROP TABLE IF EXISTS unrelated")
c.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, name TEXT)")
c.commit()
c.close()
print("created /tmp/r13-wrong.db (unrelated schema)")