import sqlite3
db = r"D:\Study-Plan\ai-image-studio\backend\data\aistudio.db"
conn = sqlite3.connect(db)
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in c.fetchall()]
lines = []
lines.append("TABLES: " + ", ".join(tables))
for t in tables:
    c.execute(f"SELECT COUNT(*) FROM {t}")
    cnt = c.fetchone()[0]
    lines.append(f"--- {t}: {cnt} rows ---")
    # 只列关键字段，避免长文本
    if t == "users":
        c.execute("SELECT id, username, length(password_hash) as phash_len, created_at FROM users")
        for r in c.fetchall():
            lines.append("  user: " + str(r))
    elif t in ("tasks",):
        c.execute("SELECT id, user_id, substr(prompt,1,30), status FROM tasks")
        for r in c.fetchall():
            lines.append("  task: " + str(r))
    elif t == "images":
        c.execute("SELECT id, task_id, filename FROM images")
        for r in c.fetchall():
            lines.append("  img: " + str(r))
    elif t == "conversations":
        c.execute("SELECT id, user_id, provider FROM conversations")
        for r in c.fetchall():
            lines.append("  conv: " + str(r))
    elif t == "messages":
        c.execute("SELECT id, conversation_id, role, substr(content,1,30) FROM messages")
        for r in c.fetchall():
            lines.append("  msg: " + str(r))
    elif t == "usage_records":
        c.execute("SELECT id, user_id, date, chat_count, image_count FROM usage_records")
        for r in c.fetchall():
            lines.append("  usage: " + str(r))
    else:
        c.execute(f"SELECT * FROM {t} LIMIT 5")
        for r in c.fetchall():
            lines.append("  row: " + str(r))
conn.close()
with open(r"D:\Study-Plan\ai-image-studio\_dumpout.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
