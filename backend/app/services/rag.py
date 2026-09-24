"""RAG 检索增强模块：文档切片 -> 向量化 -> 检索 -> 拼 prompt。

依赖 chromadb + sentence-transformers，embedding 用 CPU 跑（不占 GPU 显存）。
固定文档在镜像内 /app/knowledge（不可删），动态文档在 /data/rag_docs（可增删）。
"""
import os
from pathlib import Path

from ..config import settings

# 关键：必须在 import sentence_transformers 之前设置 HF 镜像与缓存目录
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HOME", settings.rag_hf_cache_dir)

import chromadb
from sentence_transformers import SentenceTransformer

CHUNK_SIZE = 400
CHUNK_OVERLAP = 50
COLLECTION_NAME = "project_docs"
DYNAMIC_PREFIX = "custom:"   # 动态文档 source 前缀，避免与固定文档重名冲突

_embedder = None
_client = None
_collection = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(settings.rag_embed_model, device="cpu")
    return _embedder


def _get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.PersistentClient(path=settings.rag_chroma_dir)
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
    return _collection


def _chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        chunks.append(text[start : start + size].strip())
        if start + size >= n:
            break
        start = start + size - overlap
    return [c for c in chunks if c]


def _add_chunks(collection, source, text):
    """把一段文本按 source 切片并向量化写入 collection，返回切片数。"""
    ids, chunks, metas = [], [], []
    for i, c in enumerate(_chunk_text(text)):
        ids.append(f"{source}#{i}")
        chunks.append(c)
        metas.append({"source": source, "chunk": i})
    if not chunks:
        return 0
    embedder = _get_embedder()
    batch = 64
    for i in range(0, len(chunks), batch):
        embs = embedder.encode(chunks[i : i + batch], normalize_embeddings=True).tolist()
        collection.add(
            ids=ids[i : i + batch],
            documents=chunks[i : i + batch],
            metadatas=metas[i : i + batch],
            embeddings=embs,
        )
    return len(chunks)


def _remove_by_source(collection, source):
    """按 source 删除该文档的所有切片。"""
    try:
        collection.delete(where={"source": source})
    except Exception:
        pass


def ensure_index():
    """确保向量库已建：为空则扫描固定+动态目录建索引，返回条目数。"""
    collection = _get_collection()
    if collection.count() > 0:
        return collection.count()

    total = 0
    docs_dir = Path(settings.rag_docs_dir)
    if docs_dir.exists():
        for path in sorted(docs_dir.rglob("*.md")):
            total += _add_chunks(collection, path.name, path.read_text(encoding="utf-8"))
    dyn_dir = Path(settings.rag_dynamic_dir)
    if dyn_dir.exists():
        for path in sorted(dyn_dir.rglob("*.md")):
            total += _add_chunks(collection, DYNAMIC_PREFIX + path.name, path.read_text(encoding="utf-8"))
    return total


def add_document(filename, content):
    """添加（或覆盖）一个动态文档到知识库，返回切片数。"""
    dyn_dir = Path(settings.rag_dynamic_dir)
    dyn_dir.mkdir(parents=True, exist_ok=True)
    (dyn_dir / filename).write_text(content, encoding="utf-8")

    collection = _get_collection()
    source = DYNAMIC_PREFIX + filename
    _remove_by_source(collection, source)   # 覆盖：先删旧的
    return _add_chunks(collection, source, content)


def remove_document(filename):
    """删除一个动态文档（向量库 + 文件），返回是否删除成功。"""
    collection = _get_collection()
    source = DYNAMIC_PREFIX + filename
    _remove_by_source(collection, source)
    p = Path(settings.rag_dynamic_dir) / filename
    if p.exists():
        p.unlink()
        return True
    return False


def list_documents():
    """列出所有知识库文档：[{filename, dynamic}]，dynamic 表示是否可删。"""
    docs = []
    docs_dir = Path(settings.rag_docs_dir)
    if docs_dir.exists():
        for path in sorted(docs_dir.rglob("*.md")):
            docs.append({"filename": path.name, "dynamic": False})
    dyn_dir = Path(settings.rag_dynamic_dir)
    if dyn_dir.exists():
        for path in sorted(dyn_dir.rglob("*.md")):
            docs.append({"filename": path.name, "dynamic": True})
    return docs


def retrieve(question, top_k=None):
    """检索相关片段，返回 [(source, text), ...]。"""
    top_k = top_k or settings.rag_top_k
    collection = _get_collection()
    embedder = _get_embedder()
    q_emb = embedder.encode([question], normalize_embeddings=True).tolist()
    res = collection.query(query_embeddings=q_emb, n_results=top_k)
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    return [(m["source"], d) for d, m in zip(docs, metas)]


def build_rag_prompt(question, contexts):
    """把检索到的上下文拼成 RAG prompt。"""
    context = "\n\n".join(f"[来源: {src}]\n{text}" for src, text in contexts)
    return (
        "请只根据以下参考资料回答问题，不要编造。若资料中无答案，请明确说「资料中没有相关信息」。\n\n"
        f"参考资料：\n{context}\n\n问题：{question}\n\n回答："
    )
