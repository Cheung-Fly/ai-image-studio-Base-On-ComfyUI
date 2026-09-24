"""RAG 知识库管理接口：列出 / 上传 / 删除动态文档。"""
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ..models import User
from ..services import rag
from .auth import get_current_user

router = APIRouter(tags=["rag"])

_MAX_SIZE = 5 * 1024 * 1024  # md 文件上限 5MB


def _safe_filename(filename: str) -> str:
    """安全化文件名：仅保留字母数字下划线连字符与中文，且必须以 .md 结尾。"""
    if not filename.lower().endswith(".md"):
        raise HTTPException(400, "仅支持 .md 文件")
    stem = Path(filename).stem
    stem = re.sub(r"[^\w\u4e00-\u9fff-]", "_", stem) or "doc"
    return f"{stem}.md"


@router.get("/rag/docs")
def list_docs(user: User = Depends(get_current_user)):
    """列出所有知识库文档（固定 + 动态）。"""
    return rag.list_documents()


@router.post("/rag/docs", status_code=201)
async def upload_doc(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """上传一个 md 加入知识库，返回文件名与切片数。"""
    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    filename = _safe_filename(file.filename)

    data = await file.read()
    if not data:
        raise HTTPException(400, "文件内容为空")
    if len(data) > _MAX_SIZE:
        raise HTTPException(413, "文件过大（上限 5MB）")

    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        content = data.decode("utf-8", errors="ignore")

    chunks = rag.add_document(filename, content)
    return {"filename": filename, "chunks": chunks}


@router.delete("/rag/docs/{filename}", status_code=200)
def delete_doc(filename: str, user: User = Depends(get_current_user)):
    """删除一个动态文档（固定文档不可删）。"""
    ok = rag.remove_document(filename)
    if not ok:
        raise HTTPException(404, "文档不存在或不可删除")
    return {"deleted": filename}
