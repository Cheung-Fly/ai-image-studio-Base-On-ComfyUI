"""Automatic reference-image planning and generation for storyboard workbooks."""

import json
import logging
import re
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..database import get_db
from ..models import (
    AutoImageAsset,
    AutoImageBatch,
    CustomModelConfig,
    GenerationTask,
    ImageAsset,
    RunningHubConfig,
    TaskStatus,
    User,
)
from ..schemas import (
    AutoImageAssetOut,
    AutoImageBatchOut,
    AutoImageEpisodeOption,
    AutoImageStartRequest,
    DirectorStoryboardOut,
    RunningHubWorkflowSelection,
)
from ..security import decrypt_secret
from ..services.auto_image_agent import safe_asset_filename
from ..services.auto_image_events import add_auto_image_event
from ..services.runninghub import RunningHubClient, RunningHubError
from ..services.task_reliability import (
    MAX_MANUAL_RETRIES,
    TaskDispatchError,
    add_task_event,
    dispatch_generation,
)
from ..services.video_episode import (
    MAX_SERIES_BYTES,
    MAX_SERIES_FILES,
    MAX_STORYBOARD_BYTES,
    EpisodePackageError,
    parse_series_storyboards,
)
from ..services.workflow import list_workflows
from ..throttle import check_and_increment_chat_quota, check_rate_limit
from ..workers.auto_image_tasks import plan_auto_image_batch, run_runninghub_image_task
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auto-image", tags=["auto-image"])
_CATEGORY_LABELS = {
    "character": "角色",
    "scene": "场景",
    "prop": "道具",
}
_WINDOWS_RESERVED_COMPONENTS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _safe_archive_component(value: str, fallback: str) -> str:
    component = re.sub(r'[<>:"/\\|?*\r\n]+', "-", value).strip(" .-")[:80]
    component = component.rstrip(" .")
    if not component:
        return fallback
    stem = component.split(".", 1)[0].rstrip(" .").upper()
    if stem in _WINDOWS_RESERVED_COMPONENTS:
        return f"_{component}"
    return component


def _content_disposition(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"


def _custom_config(
    db: Session,
    user: User,
    provider: str,
    custom_config_id: int | None,
) -> dict[str, str] | None:
    if provider not in {"openai", "llama", "custom"}:
        raise HTTPException(422, "不支持的资产规划模型")
    if provider != "custom":
        return None
    config = (
        db.query(CustomModelConfig)
        .filter(
            CustomModelConfig.id == custom_config_id,
            CustomModelConfig.user_id == user.id,
        )
        .first()
    )
    if config is None:
        raise HTTPException(400, "自定义模型配置无效")
    try:
        api_key = decrypt_secret(config.api_key_encrypted)
    except ValueError as exc:
        raise HTTPException(502, "自定义模型 API Key 解密失败") from exc
    return {"base_url": config.base_url, "api_key": api_key, "model": config.model}


def _get_batch(db: Session, user: User, batch_id: int) -> AutoImageBatch:
    batch = (
        db.query(AutoImageBatch)
        .filter(AutoImageBatch.id == batch_id, AutoImageBatch.user_id == user.id)
        .first()
    )
    if batch is None:
        raise HTTPException(404, "自动图片任务不存在")
    return batch


def _validate_runninghub_selection(
    db: Session,
    selection: RunningHubWorkflowSelection,
) -> None:
    config = db.get(RunningHubConfig, 1)
    if config is None or not config.api_key_encrypted:
        raise HTTPException(400, "请先配置 RunningHub API Key")
    try:
        api_key = decrypt_secret(config.api_key_encrypted)
    except ValueError as exc:
        raise HTTPException(502, "RunningHub API Key 解密失败") from exc
    client = RunningHubClient(api_key)
    try:
        fields = client.get_workflow_text_fields(selection.workflow_id)
    except RunningHubError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        client.close()
    if not any(
        field["node_id"] == selection.prompt_node_id
        and field["field_name"] == selection.prompt_field
        for field in fields
    ):
        raise HTTPException(422, "所选正向提示词输入不属于该 RunningHub 工作流")
    if selection.negative_prompt_node_id and not any(
        field["node_id"] == selection.negative_prompt_node_id
        and field["field_name"] == selection.negative_prompt_field
        for field in fields
    ):
        raise HTTPException(422, "所选负向提示词输入不属于该 RunningHub 工作流")


def _validate_generation_request(
    batch: AutoImageBatch,
    request: AutoImageStartRequest,
    db: Session,
) -> None:
    if batch.status in {"planning", "planning_failed"}:
        raise HTTPException(409, "资产清单尚未准备好，暂时无法开始图片生成")
    if request.episode_index > batch.episode_count:
        raise HTTPException(422, "所选集号不存在")
    if request.generation_backend == "comfyui":
        if request.workflow_file and request.workflow_file not in list_workflows():
            raise HTTPException(422, "所选 ComfyUI 图片工作流不存在")
    elif request.runninghub:
        _validate_runninghub_selection(db, request.runninghub)


def _episode_options(batch: AutoImageBatch) -> list[AutoImageEpisodeOption]:
    stored = json.loads(batch.storyboards_json)
    return [
        AutoImageEpisodeOption(
            episode_index=index,
            title=entry["storyboard"]["video_title"],
            shot_count=len(entry["storyboard"]["shots"]),
        )
        for index, entry in enumerate(stored, start=1)
    ]


def _batch_response(batch: AutoImageBatch, db: Session) -> AutoImageBatchOut:
    selected_index = batch.selected_episode_index
    assets = []
    selected_count = 0
    completed_count = 0
    has_failed = False
    has_active = False
    for item in batch.assets:
        episode_indices = json.loads(item.episode_indices_json)
        task = db.get(GenerationTask, item.task_id) if item.task_id else None
        status = task.status.value if task and isinstance(task.status, TaskStatus) else (
            str(task.status) if task else "draft"
        )
        error = task.error if task else None
        image = None
        if task:
            image = (
                db.query(ImageAsset)
                .filter(ImageAsset.task_id == task.id, ImageAsset.media_type == "image")
                .order_by(ImageAsset.id)
                .first()
            )
        if selected_index in episode_indices:
            selected_count += 1
            completed_count += status == "completed" and image is not None
            has_failed |= status in {"failed", "cancelled"}
            has_active |= status in {"pending", "processing"}
        assets.append(
            AutoImageAssetOut(
                id=item.id,
                asset_key=item.asset_key,
                category=item.category,
                name=item.name,
                episode_indices=episode_indices,
                prompt=item.prompt,
                status=status,
                error=error,
                error_code=task.error_code if task else None,
                retry_available=(
                    status == "failed"
                    and task is not None
                    and (task.manual_retry_count or 0) < MAX_MANUAL_RETRIES
                ),
                task_id=task.id if task else None,
                image_url=image.file_url if image else None,
            )
        )

    if batch.status in {"planning", "planning_failed"}:
        status = batch.status
    else:
        if selected_count == 0:
            status = "draft"
        elif completed_count == selected_count:
            status = "completed"
        elif has_failed:
            status = "failed"
        elif has_active:
            status = "processing"
        else:
            status = "draft"
        batch.status = status
        batch.updated_at = datetime.utcnow()
        db.commit()
    return AutoImageBatchOut(
        id=batch.id,
        title=batch.title,
        source_filename=batch.source_filename,
        episode_count=batch.episode_count,
        selected_episode_index=selected_index,
        status=status,
        episodes=_episode_options(batch),
        assets=assets,
        logs=batch.events,
        completed_assets=completed_count,
        total_assets=selected_count,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


@router.post("/plan", response_model=AutoImageBatchOut, status_code=201)
async def create_asset_plan(
    file: UploadFile = File(...),
    episode_index: int = Form(1),
    provider: str = Form("openai"),
    custom_config_id: int | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Parse a multi-sheet storyboard and create one cross-series asset inventory."""
    if not file.filename or Path(file.filename).suffix.casefold() != ".xlsx":
        raise HTTPException(400, "请上传包含逐集分镜的 .xlsx 工作簿")
    content = await file.read(MAX_STORYBOARD_BYTES + 1)
    await file.close()
    if len(content) > MAX_STORYBOARD_BYTES:
        raise HTTPException(413, "分镜 Excel 超过 50 MB")
    _custom_config(db, user, provider, custom_config_id)
    check_rate_limit(user)
    check_and_increment_chat_quota(user, db)
    parse_started_at = time.monotonic()
    try:
        storyboards = await run_in_threadpool(parse_series_storyboards, content)
    except EpisodePackageError as exc:
        logger.warning(
            "自动图片工作簿解析失败 file=%s error_type=%s error=%s",
            Path(file.filename).name,
            type(exc).__name__,
            str(exc),
        )
        raise HTTPException(400, str(exc)) from exc
    parse_duration_ms = round((time.monotonic() - parse_started_at) * 1000)
    if episode_index < 1 or episode_index > len(storyboards):
        raise HTTPException(422, f"测试分集必须在 1 到 {len(storyboards)} 之间")
    source_filename = Path(file.filename).name[:255]
    title = Path(source_filename).stem[:128] or "未命名整剧"
    stored_storyboards = [
        {
            "sheet_name": sheet_name,
            "storyboard": storyboard.model_dump(mode="json"),
        }
        for sheet_name, storyboard in storyboards
    ]
    batch = AutoImageBatch(
        user_id=user.id,
        title=title,
        source_filename=source_filename,
        episode_count=len(storyboards),
        storyboards_json=json.dumps(stored_storyboards, ensure_ascii=False),
        selected_episode_index=episode_index,
        status="planning",
    )
    db.add(batch)
    db.flush()
    add_auto_image_event(
        db,
        batch.id,
        "workbook_parsed",
        f"工作簿解析完成：{len(storyboards)} 集，"
        f"{sum(len(storyboard.shots) for _, storyboard in storyboards)} 个镜头。",
        duration_ms=parse_duration_ms,
    )
    add_auto_image_event(
        db, batch.id, "planning_queued", "资产规划已加入后台队列，正在等待 Worker。"
    )
    db.commit()
    db.refresh(batch)
    try:
        plan_auto_image_batch.delay(batch.id, provider, custom_config_id)
    except Exception as exc:
        logger.error(
            "自动图片资产规划任务入队失败 batch_id=%s error_type=%s",
            batch.id,
            type(exc).__name__,
        )
        batch.status = "planning_failed"
        batch.updated_at = datetime.utcnow()
        add_auto_image_event(
            db,
            batch.id,
            "planning_queue_failed",
            f"后台任务无法入队（{type(exc).__name__}），请检查 Celery Worker 与 Redis 服务日志。",
            level="error",
        )
        db.commit()
    return _batch_response(batch, db)


@router.get("/batches", response_model=list[AutoImageBatchOut])
def list_batches(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if limit < 1 or limit > 100:
        raise HTTPException(400, "分页参数无效")
    batches = (
        db.query(AutoImageBatch)
        .filter(AutoImageBatch.user_id == user.id)
        .order_by(AutoImageBatch.id.desc())
        .limit(limit)
        .all()
    )
    return [_batch_response(batch, db) for batch in batches]


@router.get("/batches/{batch_id}", response_model=AutoImageBatchOut)
def get_batch(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _batch_response(_get_batch(db, user, batch_id), db)


@router.get(
    "/batches/{batch_id}/episodes/{episode_index}/storyboard",
    response_model=DirectorStoryboardOut,
)
def get_episode_storyboard(
    batch_id: int,
    episode_index: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    batch = _get_batch(db, user, batch_id)
    if episode_index < 1 or episode_index > batch.episode_count:
        raise HTTPException(404, "分集不存在")
    entries = json.loads(batch.storyboards_json)
    return DirectorStoryboardOut.model_validate(
        entries[episode_index - 1]["storyboard"]
    )


@router.post("/batches/{batch_id}/start", response_model=AutoImageBatchOut, status_code=202)
def start_episode_assets(
    batch_id: int,
    request: AutoImageStartRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    batch = _get_batch(db, user, batch_id)
    _validate_generation_request(batch, request, db)

    batch.selected_episode_index = request.episode_index
    selected = [
        item
        for item in batch.assets
        if request.episode_index in json.loads(item.episode_indices_json)
    ]
    if not selected:
        raise HTTPException(422, "该集没有可生成的角色、场景或道具设定图")
    add_auto_image_event(
        db,
        batch.id,
        "episode_generation_requested",
        f"已请求生成第 {request.episode_index} 集设定图，共 {len(selected)} 项资产。",
    )

    runninghub_config = (
        request.runninghub.model_dump()
        if request.runninghub is not None
        else None
    )
    for item in selected:
        current_task = db.get(GenerationTask, item.task_id) if item.task_id else None
        if current_task and current_task.status in {
            TaskStatus.pending,
            TaskStatus.processing,
        }:
            continue
        if current_task and current_task.status == TaskStatus.completed:
            existing_image = (
                db.query(ImageAsset)
                .filter(
                    ImageAsset.task_id == current_task.id,
                    ImageAsset.media_type == "image",
                )
                .first()
            )
            if existing_image is not None:
                continue
        task = GenerationTask(
            user_id=user.id,
            prompt=item.prompt,
            negative_prompt="",
            workflow=request.workflow_file if request.generation_backend == "comfyui" else "",
            model_preset=request.model_preset if request.generation_backend == "comfyui" else "",
            aspect_ratio=request.aspect_ratio,
            megapixels=request.megapixels,
            seed=request.seed,
            steps=request.steps,
            refine_steps=request.refine_steps,
            status=TaskStatus.pending,
        )
        db.add(task)
        db.flush()
        item.task_id = task.id
        item.generation_config_json = json.dumps(
            {
                "backend": request.generation_backend,
                "runninghub": runninghub_config,
            },
            ensure_ascii=False,
        )
        if request.generation_backend == "comfyui":
            try:
                dispatch_generation(db, task)
            except TaskDispatchError:
                logger.warning("自动图片任务入队失败 task_id=%s", task.id)
        else:
            task_id = uuid.uuid4().hex
            task.celery_task_id = task_id
            add_task_event(db, task, "queued", "任务已加入 RunningHub 生成队列。")
            db.commit()
            try:
                run_runninghub_image_task.apply_async(args=[task.id], task_id=task_id)
            except Exception:
                logger.exception("RunningHub 自动图片任务入队失败 task_id=%s", task.id)
                current = db.get(GenerationTask, task.id)
                if current is not None:
                    current.status = TaskStatus.failed
                    current.error_code = "queue_unavailable"
                    current.error = "任务无法加入生成队列，请检查后台 worker 与 Redis。"
                    current.updated_at = datetime.utcnow()
                    add_task_event(
                        db,
                        current,
                        "enqueue_failed",
                        current.error,
                        error_code=current.error_code,
                    )
                    db.commit()
    db.commit()
    db.refresh(batch)
    return _batch_response(batch, db)


@router.post(
    "/batches/{batch_id}/assets/{asset_id}/retry",
    response_model=AutoImageBatchOut,
    status_code=202,
)
def retry_asset_generation(
    batch_id: int,
    asset_id: int,
    request: AutoImageStartRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retry one failed asset while preserving the task's event history."""
    batch = _get_batch(db, user, batch_id)
    _validate_generation_request(batch, request, db)
    item = (
        db.query(AutoImageAsset)
        .filter(AutoImageAsset.id == asset_id, AutoImageAsset.batch_id == batch.id)
        .first()
    )
    if item is None:
        raise HTTPException(404, "设定图不存在")
    if request.episode_index not in json.loads(item.episode_indices_json):
        raise HTTPException(404, "该设定图不属于所选分集")
    task = db.get(GenerationTask, item.task_id) if item.task_id else None
    if task is None:
        raise HTTPException(409, "设定图任务不存在，请重新启动本集生成")
    if task.status != TaskStatus.failed:
        raise HTTPException(409, "只有生成失败的设定图可以重新生成")
    if (task.manual_retry_count or 0) >= MAX_MANUAL_RETRIES:
        raise HTTPException(
            409,
            f"已达到手动重试上限（{MAX_MANUAL_RETRIES} 次），请调整配置后重新生成",
        )

    runninghub_config = (
        request.runninghub.model_dump()
        if request.runninghub is not None
        else None
    )
    task_updates = {
        GenerationTask.status: TaskStatus.pending,
        GenerationTask.error: None,
        GenerationTask.error_code: None,
        GenerationTask.workflow: (
            request.workflow_file if request.generation_backend == "comfyui" else ""
        ),
        GenerationTask.model_preset: (
            request.model_preset if request.generation_backend == "comfyui" else ""
        ),
        GenerationTask.aspect_ratio: request.aspect_ratio,
        GenerationTask.megapixels: request.megapixels,
        GenerationTask.seed: request.seed,
        GenerationTask.steps: request.steps,
        GenerationTask.refine_steps: request.refine_steps,
        GenerationTask.comfy_prompt_id: None,
        GenerationTask.runninghub_task_id: None,
        GenerationTask.celery_task_id: None,
        GenerationTask.manual_retry_count: GenerationTask.manual_retry_count + 1,
        GenerationTask.updated_at: datetime.utcnow(),
    }
    updated = (
        db.query(GenerationTask)
        .filter(
            GenerationTask.id == task.id,
            GenerationTask.user_id == user.id,
            GenerationTask.status == TaskStatus.failed,
            GenerationTask.manual_retry_count < MAX_MANUAL_RETRIES,
        )
        .update(task_updates, synchronize_session=False)
    )
    if not updated:
        db.rollback()
        raise HTTPException(409, "任务状态已变化或已达到重试上限，请刷新后重试")

    updated_asset = (
        db.query(AutoImageAsset)
        .filter(
            AutoImageAsset.id == item.id,
            AutoImageAsset.batch_id == batch.id,
            AutoImageAsset.task_id == task.id,
        )
        .update(
            {
                AutoImageAsset.generation_config_json: json.dumps(
                    {
                        "backend": request.generation_backend,
                        "runninghub": runninghub_config,
                    },
                    ensure_ascii=False,
                ),
                AutoImageAsset.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    if not updated_asset:
        db.rollback()
        raise HTTPException(409, "设定图任务已变化，请刷新后重试")

    batch.selected_episode_index = request.episode_index
    db.refresh(task)
    add_task_event(
        db,
        task,
        "manual_retry_requested",
        (
            f"已请求重新生成设定图“{item.name}”（"
            f"第 {task.manual_retry_count}/{MAX_MANUAL_RETRIES} 次）。"
        ),
    )
    add_auto_image_event(
        db,
        batch.id,
        "asset_generation_retry_requested",
        f"已请求重新生成设定图“{item.name}”。",
    )
    db.commit()

    if request.generation_backend == "comfyui":
        try:
            dispatch_generation(db, task)
        except TaskDispatchError:
            logger.warning("自动图片任务重新入队失败 task_id=%s", task.id)
            current = db.get(GenerationTask, task.id)
            if current is not None:
                add_auto_image_event(
                    db,
                    batch.id,
                    "asset_generation_retry_enqueue_failed",
                    current.error or "任务无法加入生成队列。",
                    level="error",
                )
                db.commit()
    else:
        celery_task_id = uuid.uuid4().hex
        task.celery_task_id = celery_task_id
        add_task_event(db, task, "queued", "任务已加入 RunningHub 生成队列。")
        db.commit()
        try:
            run_runninghub_image_task.apply_async(
                args=[task.id],
                task_id=celery_task_id,
            )
        except Exception:
            logger.exception("RunningHub 自动图片任务重新入队失败 task_id=%s", task.id)
            current = db.get(GenerationTask, task.id)
            if current is not None:
                current.status = TaskStatus.failed
                current.error_code = "queue_unavailable"
                current.error = "任务无法加入生成队列，请检查后台 worker 与 Redis。"
                current.updated_at = datetime.utcnow()
                add_task_event(
                    db,
                    current,
                    "enqueue_failed",
                    current.error,
                    error_code=current.error_code,
                )
                add_auto_image_event(
                    db,
                    batch.id,
                    "asset_generation_retry_enqueue_failed",
                    current.error,
                    level="error",
                )
                db.commit()

    db.refresh(batch)
    return _batch_response(batch, db)


@router.get("/batches/{batch_id}/episodes/{episode_index}.zip")
def download_episode_package(
    batch_id: int,
    episode_index: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    batch = _get_batch(db, user, batch_id)
    if episode_index < 1 or episode_index > batch.episode_count:
        raise HTTPException(404, "分集不存在")
    entries = json.loads(batch.storyboards_json)
    storyboard = DirectorStoryboardOut.model_validate(
        entries[episode_index - 1]["storyboard"]
    )
    relevant = [
        item
        for item in batch.assets
        if episode_index in json.loads(item.episode_indices_json)
    ]
    archive = BytesIO()
    safe_root = re.sub(r'[<>:"/\\|?*\r\n]+', "-", batch.title).strip(" .") or "series"
    episode_folder = f"{episode_index:02d}-{safe_root}"
    package_root = f"{safe_root}/{episode_folder}"
    included = []
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr(
                f"{package_root}/storyboard.json",
                storyboard.model_dump_json(indent=2),
            )
            for item in relevant:
                task = db.get(GenerationTask, item.task_id) if item.task_id else None
                if task is None or task.status != TaskStatus.completed:
                    raise HTTPException(409, "本集仍有未完成的设定图")
                image = (
                    db.query(ImageAsset)
                    .filter(
                        ImageAsset.task_id == task.id,
                        ImageAsset.media_type == "image",
                    )
                    .order_by(ImageAsset.id)
                    .first()
                )
                if image is None:
                    raise HTTPException(409, f"设定图“{item.name}”没有可打包的图片")
                path = (settings.image_dir / image.filepath).resolve()
                if settings.image_dir.resolve() not in path.parents or not path.is_file():
                    raise HTTPException(404, f"设定图“{item.name}”文件已丢失")
                filename = safe_asset_filename(item.category, item.name, item.asset_key)
                target = f"{package_root}/assets/{filename}{path.suffix.lower()}"
                output.write(path, target)
                included.append(
                    {
                        "category": _CATEGORY_LABELS[item.category],
                        "name": item.name,
                        "filename": f"assets/{filename}{path.suffix.lower()}",
                    }
                )
            output.writestr(
                f"{package_root}/assets_manifest.json",
                json.dumps(included, ensure_ascii=False, indent=2),
            )
    except zipfile.LargeZipFile as exc:
        raise HTTPException(413, "本集素材包超过 ZIP 支持大小") from exc
    archive.seek(0)
    filename = f"{safe_root}-{episode_index:02d}.zip"
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.get("/batches/{batch_id}/series.zip")
def download_series_package(
    batch_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    batch = _get_batch(db, user, batch_id)
    if batch.status in {"planning", "planning_failed"}:
        raise HTTPException(409, "资产清单尚未准备好，暂时无法导出整剧素材包")

    entries = json.loads(batch.storyboards_json)
    if len(entries) != batch.episode_count:
        raise HTTPException(409, "分集分镜数据不完整，无法导出整剧素材包")

    safe_root = _safe_archive_component(batch.title, "series")
    with tempfile.NamedTemporaryFile(
        prefix="aistudio-series-",
        suffix=".zip",
        delete=False,
    ) as archive_file:
        archive_path = Path(archive_file.name)
    delivery_episodes = []
    entry_count = 0
    uncompressed_bytes = 0
    category_folders = {
        "character": "01-characters",
        "scene": "02-scenes",
        "prop": "03-props",
    }

    try:
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as output:
            def write_text(path: str, content: str) -> None:
                nonlocal entry_count, uncompressed_bytes
                encoded = content.encode("utf-8")
                entry_count += 1
                uncompressed_bytes += len(encoded)
                if entry_count > MAX_SERIES_FILES:
                    raise HTTPException(413, "整剧素材包文件数量超过 Agent3 导入上限")
                if uncompressed_bytes > MAX_SERIES_BYTES:
                    raise HTTPException(413, "整剧素材包解压后超过 4 GB")
                output.writestr(path, encoded)

            for episode_index, entry in enumerate(entries, start=1):
                storyboard = DirectorStoryboardOut.model_validate(
                    entry["storyboard"]
                )
                episode_title = _safe_archive_component(
                    storyboard.video_title,
                    f"episode-{episode_index:02d}",
                )
                episode_folder = f"{episode_index:02d}-{episode_title}"
                episode_root = f"{safe_root}/{episode_folder}"
                write_text(
                    f"{episode_root}/storyboard.json",
                    storyboard.model_dump_json(indent=2),
                )

                included_assets = []
                category_order = {
                    "character": 0,
                    "scene": 1,
                    "prop": 2,
                }
                relevant = sorted(
                    (
                        item
                        for item in batch.assets
                        if episode_index in json.loads(item.episode_indices_json)
                    ),
                    key=lambda item: (
                        category_order.get(item.category, 3),
                        item.name.casefold(),
                        item.asset_key,
                    ),
                )
                for asset_order, item in enumerate(relevant, start=1):
                    if item.category not in category_folders:
                        raise HTTPException(
                            409,
                            f"设定图“{item.name}”的分类无法加入 Agent3 素材包",
                        )
                    task = db.get(GenerationTask, item.task_id) if item.task_id else None
                    if task is None or task.status != TaskStatus.completed:
                        raise HTTPException(
                            409,
                            f"第 {episode_index} 集设定图“{item.name}”尚未完成",
                        )
                    image = (
                        db.query(ImageAsset)
                        .filter(
                            ImageAsset.task_id == task.id,
                            ImageAsset.media_type == "image",
                        )
                        .order_by(ImageAsset.id)
                        .first()
                    )
                    if image is None:
                        raise HTTPException(
                            409,
                            f"第 {episode_index} 集设定图“{item.name}”没有可打包的图片",
                        )
                    path = (settings.image_dir / image.filepath).resolve()
                    if (
                        settings.image_dir.resolve() not in path.parents
                        or not path.is_file()
                    ):
                        raise HTTPException(
                            404,
                            f"第 {episode_index} 集设定图“{item.name}”文件已丢失",
                        )
                    filename = safe_asset_filename(
                        item.category,
                        item.name,
                        item.asset_key,
                    )
                    relative_path = (
                        f"assets/{category_folders[item.category]}/"
                        f"{filename}{path.suffix.lower()}"
                    )
                    entry_count += 1
                    uncompressed_bytes += path.stat().st_size
                    if entry_count > MAX_SERIES_FILES:
                        raise HTTPException(413, "整剧素材包文件数量超过 Agent3 导入上限")
                    if uncompressed_bytes > MAX_SERIES_BYTES:
                        raise HTTPException(413, "整剧素材包解压后超过 4 GB")
                    output.write(path, f"{episode_root}/{relative_path}")
                    included_assets.append(
                        {
                            "order": asset_order,
                            "asset_key": item.asset_key,
                            "category": item.category,
                            "name": item.name,
                            "path": relative_path,
                        }
                    )

                write_text(
                    f"{episode_root}/assets_manifest.json",
                    json.dumps(
                        {
                            "order": episode_index,
                            "episode_index": episode_index,
                            "asset_scope": "episode_shared_reference",
                            "assets": included_assets,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                delivery_episodes.append(
                    {
                        "episode_index": episode_index,
                        "title": storyboard.video_title,
                        "folder": episode_folder,
                        "shot_order": [
                            {
                                "order": order,
                                "shot_no": shot.shot_no,
                            }
                            for order, shot in enumerate(storyboard.shots, start=1)
                        ],
                        "assets": included_assets,
                    }
                )

            write_text(
                f"{safe_root}/delivery_manifest.json",
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "series_title": batch.title,
                        "episodes": delivery_episodes,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    except zipfile.LargeZipFile as exc:
        archive_path.unlink(missing_ok=True)
        raise HTTPException(413, "整剧素材包超过 ZIP 支持大小") from exc
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    filename = f"{safe_root}-Agent3.zip"

    def stream_archive():
        try:
            with archive_path.open("rb") as archive:
                while chunk := archive.read(1024 * 1024):
                    yield chunk
        finally:
            archive_path.unlink(missing_ok=True)

    return StreamingResponse(
        stream_archive(),
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(filename)},
    )
