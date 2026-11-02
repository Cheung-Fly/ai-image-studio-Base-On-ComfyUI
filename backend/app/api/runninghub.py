"""RunningHub workflow configuration and discovery endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import RunningHubConfig, User
from ..schemas import RunningHubConfigIn, RunningHubWorkflowNodesRequest
from ..security import decrypt_secret, encrypt_secret
from ..services.runninghub import RunningHubClient, RunningHubError
from .auth import get_current_user

router = APIRouter(tags=["runninghub"])


@router.get("/runninghub/config")
def get_runninghub_config(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    config = db.get(RunningHubConfig, 1)
    if config is None:
        return {"configured": False}
    return {
        "configured": True,
        "workflow_id": config.workflow_id,
        "prompt_node_id": config.prompt_node_id,
        "prompt_field": config.prompt_field,
        "negative_prompt_node_id": config.negative_prompt_node_id,
        "negative_prompt_field": config.negative_prompt_field,
    }


@router.put("/runninghub/config")
def save_runninghub_config(
    req: RunningHubConfigIn,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    config = db.get(RunningHubConfig, 1)
    api_key = req.api_key or (
        decrypt_secret(config.api_key_encrypted) if config is not None else ""
    )
    if not api_key:
        raise HTTPException(400, "请填写 RunningHub API Key")

    if config is None:
        config = RunningHubConfig(id=1)
        db.add(config)
    config.workflow_id = req.workflow_id
    config.prompt_node_id = req.prompt_node_id
    config.prompt_field = req.prompt_field
    config.negative_prompt_node_id = req.negative_prompt_node_id
    config.negative_prompt_field = req.negative_prompt_field
    config.api_key_encrypted = encrypt_secret(api_key)
    db.commit()
    return {"configured": True, "workflow_id": config.workflow_id}


@router.post("/runninghub/workflow-nodes")
def list_runninghub_workflow_nodes(
    req: RunningHubWorkflowNodesRequest,
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    config = db.get(RunningHubConfig, 1)
    api_key = req.api_key or (
        decrypt_secret(config.api_key_encrypted) if config is not None else ""
    )
    workflow_id = req.workflow_id or (config.workflow_id if config is not None else "")
    if not api_key:
        raise HTTPException(400, "请先填写 RunningHub API Key")
    if not workflow_id:
        raise HTTPException(400, "请填写 RunningHub Workflow ID")

    client = RunningHubClient(api_key)
    try:
        fields = client.get_workflow_text_fields(workflow_id)
    except RunningHubError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        client.close()
    if not fields:
        raise HTTPException(400, "工作流中未发现可映射的文本输入")
    return {"workflow_id": workflow_id, "fields": fields}
