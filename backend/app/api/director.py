"""Director Agent endpoints for screenplay-to-storyboard work."""

import logging
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CustomModelConfig, DirectorSkill, User
from ..schemas import (
    DirectorSkillIn,
    DirectorSkillOut,
    DirectorStoryboardOut,
    DirectorStoryboardRequest,
)
from ..security import decrypt_secret
from ..services.director_agent import (
    MAX_SKILL_MARKDOWN_BYTES,
    DirectorOutputError,
    content_disposition,
    export_filename,
    export_storyboard_xlsx,
    generate_storyboard,
    parse_skill_markdown,
)
from ..services.llm_client import LLMError
from ..throttle import check_and_increment_chat_quota, check_rate_limit
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/director", tags=["director"])


@router.get("/skills", response_model=list[DirectorSkillOut])
def list_skills(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List reusable storyboard skills belonging to the current user."""
    return (
        db.query(DirectorSkill)
        .filter(DirectorSkill.user_id == user.id)
        .order_by(DirectorSkill.id)
        .all()
    )


@router.post("/skills", response_model=DirectorSkillOut, status_code=201)
def create_skill(
    req: DirectorSkillIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save a reusable storyboard skill for the current user."""
    skill = DirectorSkill(
        user_id=user.id,
        name=req.name,
        instructions=req.instructions,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


@router.post("/skills/import", response_model=DirectorSkillOut, status_code=201)
async def import_skill(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Import a reusable storyboard skill from an uploaded SKILL.md file."""
    try:
        content = await file.read(MAX_SKILL_MARKDOWN_BYTES + 1)
    finally:
        await file.close()
    if len(content) > MAX_SKILL_MARKDOWN_BYTES:
        raise HTTPException(413, "SKILL.md 不能超过 256 KB")
    try:
        name, instructions = parse_skill_markdown(content, file.filename or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    skill = DirectorSkill(
        user_id=user.id,
        name=name,
        instructions=instructions,
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)
    return skill


@router.put("/skills/{skill_id}", response_model=DirectorSkillOut)
def update_skill(
    skill_id: int,
    req: DirectorSkillIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a storyboard skill belonging to the current user."""
    skill = (
        db.query(DirectorSkill)
        .filter(DirectorSkill.id == skill_id, DirectorSkill.user_id == user.id)
        .first()
    )
    if skill is None:
        raise HTTPException(404, "Skill 不存在")
    skill.name = req.name
    skill.instructions = req.instructions
    db.commit()
    db.refresh(skill)
    return skill


@router.delete("/skills/{skill_id}", status_code=200)
def delete_skill(
    skill_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a storyboard skill belonging to the current user."""
    skill = (
        db.query(DirectorSkill)
        .filter(DirectorSkill.id == skill_id, DirectorSkill.user_id == user.id)
        .first()
    )
    if skill is None:
        raise HTTPException(404, "Skill 不存在")
    db.delete(skill)
    db.commit()
    return {"deleted": skill_id}


@router.post("/storyboard", response_model=DirectorStoryboardOut)
def create_storyboard(
    req: DirectorStoryboardRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a single-episode storyboard with the selected LLM."""
    check_rate_limit(user)

    skills_by_id = {}
    if req.skill_ids:
        skills = (
            db.query(DirectorSkill)
            .filter(
                DirectorSkill.user_id == user.id,
                DirectorSkill.id.in_(req.skill_ids),
            )
            .all()
        )
        skills_by_id = {skill.id: skill for skill in skills}
        if len(skills_by_id) != len(req.skill_ids):
            raise HTTPException(400, "所选 Skill 不存在或无权访问")
    selected_skills = [
        (skills_by_id[skill_id].name, skills_by_id[skill_id].instructions)
        for skill_id in req.skill_ids
    ]

    custom_config = None
    if req.provider == "custom":
        config = (
            db.get(CustomModelConfig, req.custom_config_id)
            if req.custom_config_id
            else None
        )
        if config is None or config.user_id != user.id:
            raise HTTPException(400, "自定义模型配置无效")
        try:
            api_key = decrypt_secret(config.api_key_encrypted)
        except ValueError as exc:
            raise HTTPException(502, "自定义模型 API Key 解密失败") from exc
        custom_config = {
            "base_url": config.base_url,
            "api_key": api_key,
            "model": config.model,
        }

    check_and_increment_chat_quota(user, db)
    try:
        return generate_storyboard(
            req.script,
            req.video_title,
            req.provider,
            custom_config,
            selected_skills,
        )
    except LLMError as exc:
        logger.error("导演 Agent LLM 调用失败: %s", exc)
        raise HTTPException(502, "导演模型暂不可用，请检查模型配置后重试") from exc
    except DirectorOutputError as exc:
        logger.warning("导演 Agent 输出结构无效: %s", exc)
        raise HTTPException(
            502,
            "模型未返回有效分镜表，请缩短剧本或重试",
        ) from exc


@router.post("/export")
def export_storyboard(
    storyboard: DirectorStoryboardOut,
    _user: User = Depends(get_current_user),
):
    """Download an editable Excel workbook matching the supplied storyboard."""
    filename = export_filename(storyboard.video_title)
    content = export_storyboard_xlsx(storyboard)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(filename)},
    )
