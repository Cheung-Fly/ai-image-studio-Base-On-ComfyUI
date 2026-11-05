FROM node:22-alpine AS frontend-build

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim

WORKDIR /app

# 先只拷依赖清单，利用 Docker 层缓存：依赖不变时不会重复装
COPY backend/requirements.txt .
# Azure deployment runs on CPU; avoid pulling the much larger CUDA runtime wheels.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir -r requirements.txt

# Copy the application and the compiled Vue frontend.
COPY backend/app ./app
COPY backend/model_presets.json ./model_presets.json
COPY workflows /workflows
COPY --from=frontend-build /frontend/dist /frontend/dist

# RAG 知识库固定文档（镜像内 /app/knowledge）
COPY docs /app/knowledge/docs
COPY README.md start_local.md /app/knowledge/

ENV WORKFLOW_DIR=/workflows \
    DATABASE_URL=sqlite:////data/aistudio.db \
    IMAGE_STORAGE_DIR=/data/images \
    COMFYUI_BASE_URL=http://host.docker.internal:8188

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /data/images \
    && chown -R app:app /data /home/app

USER app:app

EXPOSE 8000

# 日常开发用 --reload 热更新；生产可去掉 --reload
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
