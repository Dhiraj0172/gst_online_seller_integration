import sqlite3
conn = sqlite3.connect('/mnt/hidden_xpool/__JULES_BACKUP/metadata.db')
c = conn.cursor()
c.execute("SELECT * FROM backups")
for row in c.fetchall():
    print(row)
