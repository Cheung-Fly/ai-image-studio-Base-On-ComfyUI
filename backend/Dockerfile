FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY ../workflows /workflows

ENV WORKFLOW_DIR=/workflows \
    DATABASE_URL=sqlite:////data/aistudio.db \
    IMAGE_STORAGE_DIR=/data/images \
    COMFYUI_BASE_URL=http://host.docker.internal:8188

RUN mkdir -p /data/images

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
