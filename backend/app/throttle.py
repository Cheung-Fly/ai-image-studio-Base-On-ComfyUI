"""限流与额度控制。

- 每日 LLM 调用额度：基于 UsageRecord 表，按 (user_id, 日期) 累计。
- 简单频率限流：内存级滑动窗口（单进程内有效，多 worker 部署需换 Redis 实现）。
"""
import time
from collections import defaultdict, deque
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from .config import settings
from .models import UsageRecord, User

# 内存级限流：user_id -> 最近请求时间戳队列（单进程内有效）
_window: defaultdict = defaultdict(deque)
# 每用户每分钟最大请求数
RATE_LIMIT_PER_MINUTE = 20


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def check_rate_limit(user: User) -> None:
    """频率限流：每用户每分钟最多 RATE_LIMIT_PER_MINUTE 次。"""
    now = time.time()
    q = _window[user.id]
    # 清理 60 秒前的记录
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
    q.append(now)


def check_and_increment_chat_quota(user: User, db: Session) -> None:
    """每日 LLM 额度检查并 +1；超限抛 429。"""
    if settings.daily_chat_quota < 0:
        return  # 不限额度
    today = _today()
    record = (
        db.query(UsageRecord)
        .filter(UsageRecord.user_id == user.id, UsageRecord.date == today)
        .first()
    )
    if record is None:
        record = UsageRecord(user_id=user.id, date=today, chat_count=0)
        db.add(record)
    if record.chat_count >= settings.daily_chat_quota:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "今日 LLM 调用额度已用完")
    record.chat_count += 1
    db.commit()
