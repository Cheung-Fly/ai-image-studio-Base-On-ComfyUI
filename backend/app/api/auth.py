"""匿名单实例运行时的内部资料依赖，不提供登录或注册端点。"""

from fastapi import Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User

_LOCAL_PROFILE_USERNAME = "local"


def get_current_user(db: Session = Depends(get_db)) -> User:
    """返回内部本地资料，复用现有数据归属字段且不要求请求凭据。"""
    user = db.query(User).filter(User.username == _LOCAL_PROFILE_USERNAME).first()
    if user is not None:
        return user

    user = db.query(User).order_by(User.id.asc()).first()
    if user is None:
        user = User(username=_LOCAL_PROFILE_USERNAME, password_hash="disabled")
        db.add(user)
    else:
        user.username = _LOCAL_PROFILE_USERNAME

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        user = db.query(User).filter(User.username == _LOCAL_PROFILE_USERNAME).first()
        if user is None:
            raise
    else:
        db.refresh(user)
    return user
