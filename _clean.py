import sqlite3, shutil, os, json
from datetime import datetime

db = r"D:\Study-Plan\ai-image-studio\backend\data\aistudio.db"
imgdir = r"D:\Study-Plan\ai-image-studio\backend\data\images"

# 1. 备份数据库
backup = db + ".bak_" + datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy2(db, backup)

conn = sqlite3.connect(db)
c = conn.cursor()
c.execute("PRAGMA foreign_keys = ON")

# 2. 找出 admin 用户 id
c.execute("SELECT id, username FROM users WHERE username='admin'")
admin = c.fetchone()
result = {}
if not admin:
    result["error"] = "未找到 admin 用户"
else:
    admin_id = admin[0]
    result["admin"] = admin

    # 3. 删除非 admin 用户
    c.execute("SELECT id, username FROM users WHERE id != ?", (admin_id,))
    other_users = c.fetchall()
    result["deleted_users"] = other_users

    for uid, uname in other_users:
        c.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=?)", (uid,))
        c.execute("DELETE FROM conversations WHERE user_id=?", (uid,))
        c.execute("DELETE FROM usage_records WHERE user_id=?", (uid,))
        c.execute("DELETE FROM images WHERE task_id IN (SELECT id FROM tasks WHERE user_id=?)", (uid,))
        c.execute("DELETE FROM tasks WHERE user_id=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))

    # 4. 清空所有图片记录
    c.execute("DELETE FROM images")
    result["deleted_images_rows"] = c.rowcount

    # 5. 清空历史任务（user_id IS NULL）
    c.execute("DELETE FROM tasks WHERE user_id IS NULL")
    result["deleted_null_tasks"] = c.rowcount

    # 6. 清空 admin 的任务（admin 的图片已被第4步全清）
    c.execute("DELETE FROM tasks WHERE user_id=?", (admin_id,))
    result["deleted_admin_tasks"] = c.rowcount

    conn.commit()

    summary = {}
    for t in ["users", "tasks", "images", "conversations", "messages", "usage_records"]:
        c.execute("SELECT COUNT(*) FROM " + t)
        summary[t] = c.fetchone()[0]
    result["final_counts"] = summary

conn.close()

# 8. 删除磁盘图片文件
deleted_files = []
if os.path.isdir(imgdir):
    for root, dirs, files in os.walk(imgdir):
        for f in files:
            fp = os.path.join(root, f)
            os.remove(fp)
            deleted_files.append(fp.replace("D:\\Study-Plan\\ai-image-studio\\", ""))
    for root, dirs, files in os.walk(imgdir, topdown=False):
        if root != imgdir and not os.listdir(root):
            os.rmdir(root)
result["deleted_disk_files"] = deleted_files
result["backup"] = backup

with open(r"D:\Study-Plan\ai-image-studio\_clean_result.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, default=str, indent=2)
print("OK")
