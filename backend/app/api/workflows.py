"""可用工作流列表接口（供前端侧边栏渲染）。"""

from fastapi import APIRouter, Depends

from ..config import settings
from ..models import User
from ..schemas import WorkflowOut
from ..services.workflow import list_workflows
from .auth import get_current_user

router = APIRouter(tags=["workflows"])


@router.get("/workflows", response_model=list[WorkflowOut])
def get_workflows(user: User = Depends(get_current_user)):
    """列出当前可用工作流（需登录），附带是否默认标记。"""
    files = list_workflows()
    return [
        WorkflowOut(file=f, is_default=(f == settings.workflow_file))
        for f in files
    ]
