"""全局配置：环境变量优先，未设置时使用项目内默认值。"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（config.py 位于 backend/app/config.py）
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ComfyUI 服务地址（API 模式）
    comfyui_base_url: str = "http://127.0.0.1:8188"
    # ComfyUI input 目录（素材上传落盘位置，供 LoadImage/LoadVideo/LoadAudio 读取）
    comfyui_input_dir: str = ""
    # ComfyUI loras 目录（列出可用 LoRA 文件，供前端下拉选择）
    comfyui_loras_dir: str = ""
    # 数据库连接串
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'aistudio.db').as_posix()}"
    # 图片存储目录（相对 backend/）
    image_storage_dir: str = "data/images"
    # Celery broker / result backend
    redis_url: str = "redis://127.0.0.1:6379/0"
    # 工作流模板目录（相对 backend/）
    workflow_dir: str = "../workflows"
    # 工作流模板文件名（Krea2 文生图 + 潜空间放大）
    workflow_file: str = "Krea2-极清生图流+SeedVR2-int8图像放大.json"
    # 模型组合预设文件（相对 backend/），定义多套 UNET+CLIP+VAE 组合
    model_presets_file: str = "model_presets.json"
    # 默认模型组合预设名（空 = 不覆盖，使用工作流 JSON 里写死的模型）
    default_model_preset: str = ""
    # 单次生成超时（秒）
    comfy_timeout: int = 900
    # ComfyUI 输出轮询间隔（秒）
    comfy_poll_interval: float = 2.0
    # LLM 对话服务：多 provider 支持，运行时由请求指定（前端下拉框切换）
    # OpenAI 兼容服务（OpenAI / DeepSeek / 通义 / vLLM / Ollama 等）
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # 本地 llama.cpp server
    llama_base_url: str = "http://127.0.0.1:8080/v1"
    llama_api_key: str = ""
    llama_model: str = "llama-3.1-8b"
    # llama.cpp server 专用：最大生成 token 数（其部分版本不传 max_tokens 会报错）
    llama_max_tokens: int = 512
    # 默认使用的 provider：openai / llama
    default_llm_provider: str = "openai"
    # JWT 签名密钥（生产环境务必通过环境变量覆盖为随机长字符串）
    jwt_secret: str = "change-me-in-production-please"
    # API Key 加密密钥（用于加密存储用户自定义模型的 API Key，生产环境务必覆盖）
    encryption_key: str = "change-me-in-production-encryption-key"
    # token 有效期（分钟）
    jwt_expire_minutes: int = 60 * 24
    # 每用户每日 LLM 调用额度（-1 表示不限）
    daily_chat_quota: int = 100
    # CORS 允许来源（逗号分隔），生产环境收敛为具体前端域名
    cors_origins: str = "*"
    # RAG 知识库
    rag_docs_dir: str = "/app/knowledge"      # 固定文档目录（镜像内）
    rag_chroma_dir: str = "/data/chroma_db"   # 向量库持久化目录
    rag_hf_cache_dir: str = "/data/hf_cache"  # HF 模型缓存（bge 下载一次可复用）
    rag_embed_model: str = "BAAI/bge-small-zh-v1.5"
    rag_top_k: int = 5
    rag_dynamic_dir: str = "/data/rag_docs"   # 动态文档目录（前端上传，可增删）

    @property
    def image_dir(self) -> Path:
        p = Path(self.image_storage_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def workflow_path(self) -> Path:
        p = Path(self.workflow_dir) / self.workflow_file
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def model_presets_path(self) -> Path:
        p = Path(self.model_presets_file)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def comfyui_input_path(self) -> Path | None:
        """素材上传目录；未配置时回退到项目内 data/upload。"""
        if self.comfyui_input_dir:
            return Path(self.comfyui_input_dir)
        return BASE_DIR / "data" / "upload"

    @property
    def comfyui_loras_path(self) -> Path | None:
        """ComfyUI LoRA 目录；未配置时返回 None（前端不展示 LoRA 列表）。"""
        if self.comfyui_loras_dir:
            return Path(self.comfyui_loras_dir)
        return None


settings = Settings()
