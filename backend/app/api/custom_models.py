"""自定义模型配置接口：保存 / 列出 / 删除（API Key 加密存储，不返回明文）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CustomModelConfig, User
from ..schemas import CustomModelIn
from ..security import encrypt_secret
from .auth import get_current_user

router = APIRouter(tags=["custom-models"])


@router.post("/custom-models", status_code=201)
def create_config(
    req: CustomModelIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """保存一个自定义模型配置，API Key 加密后落库。"""
    cfg = CustomModelConfig(
        user_id=user.id,
        name=req.name,
        base_url=req.base_url.rstrip("/"),
        model=req.model,
        api_key_encrypted=encrypt_secret(req.api_key),
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return {"id": cfg.id, "name": cfg.name, "base_url": cfg.base_url, "model": cfg.model}


@router.get("/custom-models")
def list_configs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """列出当前用户的自定义模型配置（不含 API Key 明文）。"""
    rows = db.query(CustomModelConfig).filter(CustomModelConfig.user_id == user.id).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "base_url": r.base_url,
            "model": r.model,
            "has_key": bool(r.api_key_encrypted),
        }
        for r in rows
    ]


@router.delete("/custom-models/{config_id}", status_code=200)
def delete_config(
    config_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除一个自定义模型配置。"""
    cfg = db.get(CustomModelConfig, config_id)
    if cfg is None or cfg.user_id != user.id:
        raise HTTPException(404, "配置不存在")
    db.delete(cfg)
    db.commit()
    return {"deleted": config_id}
