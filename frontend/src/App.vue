<script setup lang="ts">
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  reactive,
  ref,
  watch,
} from "vue";
import { apiFetch, apiJson, errorMessage } from "./api";
import { beginAgentActivity } from "./agentActivity";
import AutoImageView from "./components/AutoImageView.vue";
import DirectorView from "./components/DirectorView.vue";
import PandaAgent from "./components/PandaPet.vue";
import VideoAgentView from "./components/VideoAgentView.vue";
import type {
  AgentConfirmation,
  AgentFile,
  AgentFileRecord,
  AgentHistoryEntry,
  AgentMessage,
  AgentPreference,
  AgentProgress,
  AgentStep,
  AgentTaskStatus,
  ChatRole,
  CustomModel,
  GalleryEntry,
  GenerationBackend,
  ImageRecord,
  InstanceDiagnostics,
  ModelPresetOption,
  Provider,
  RagDocument,
  RunningHubConfig,
  RunningHubField,
  RunningHubMapping,
  WorkflowOption,
} from "./types";

const PROFILE_ID = "local";
const WELCOME_TEXT = "你好，我是创作 Agent。你可以描述想生成的图片或视频。";
let healthCheckTimer: number | undefined;
const EMPTY_MEMORY: AgentPreference = {
  style: "",
  workflow: "",
  model_preset: "",
  aspect_ratio: "",
  megapixels: "",
  negative_prompt: "",
  notes: "",
};

function createId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function readHistory(): AgentHistoryEntry[] {
  const previousUsername = localStorage.getItem("auth_username") || "";
  if (previousUsername && previousUsername !== PROFILE_ID) {
    for (const prefix of ["comfyui_web_url:", "agent_history:"]) {
      const previousValue = localStorage.getItem(prefix + previousUsername);
      const localKey = prefix + PROFILE_ID;
      if (previousValue && !localStorage.getItem(localKey)) {
        localStorage.setItem(localKey, previousValue);
      }
    }
  }
  localStorage.removeItem("auth_token");
  localStorage.removeItem("auth_username");

  try {
    const stored: unknown = JSON.parse(
      localStorage.getItem("agent_history:" + PROFILE_ID) || "[]",
    );
    if (!Array.isArray(stored)) return [];
    return stored
      .filter(
        (message): message is AgentHistoryEntry =>
          typeof message === "object" &&
          message !== null &&
          ["user", "assistant"].includes(
            (message as AgentHistoryEntry).role,
          ) &&
          typeof (message as AgentHistoryEntry).content === "string",
      )
      .slice(-20);
  } catch {
    return [];
  }
}

function normalizeComfyUIUrl(value: string): URL {
  const url = new URL(value.trim());
  if (
    !["http:", "https:"].includes(url.protocol) ||
    !url.hostname ||
    url.username ||
    url.password
  ) {
    throw new Error("请输入不含凭据的 HTTP/HTTPS 地址");
  }
  return url;
}

function parseMapping(value: string): RunningHubMapping | null {
  if (!value) return null;
  try {
    return JSON.parse(value) as RunningHubMapping;
  } catch {
    return null;
  }
}

function mapHistoryToMessages(history: AgentHistoryEntry[]): AgentMessage[] {
  return history.map((entry) => ({
    id: createId(),
    role: entry.role,
    text: entry.content,
    media: [],
    notices: [],
  }));
}

function runningHubMappingValue(nodeId: string, fieldName: string): string {
  return JSON.stringify({ node_id: nodeId, field_name: fieldName });
}

type StudioView =
  | "overview"
  | "agent"
  | "director"
  | "images"
  | "video"
  | "roughcut"
  | "pipeline"
  | "library"
  | "settings";
type SettingsTab = "comfyui" | "runninghub" | "models" | "skills";

function toolLabel(tool: string): string {
  const labels: Record<string, string> = {
    comfy_status: "查询 ComfyUI 状态",
    comfy_list_models: "查询可用模型",
    comfy_list_workflows: "查询工作流",
    comfy_workflow_details: "查看工作流结构",
    comfy_describe_node: "查询 ComfyUI 节点详情",
    comfy_list_input_files: "查看输入素材",
    comfy_get_preferences: "读取个人偏好",
    comfy_remember_preferences: "保存个人偏好",
    comfy_forget_preferences: "清除个人偏好",
    comfy_generate_image: "生成图片",
    comfy_generate_video: "生成视频",
    comfy_run_workflow: "执行工作流",
    runninghub_generate: "运行 RunningHub 工作流",
    harness_review: "视觉质量审查",
    comfy_queue: "查询队列",
    comfy_history: "查询生成历史",
    comfy_interrupt: "中断当前任务",
    comfy_clear_queue: "清空等待队列",
    comfy_delete_queued: "删除指定排队任务",
    comfy_free_memory: "释放显存",
    comfy_delete_input_file: "删除输入素材",
  };
  return labels[tool] || tool;
}

function progressText(progress: AgentProgress): string {
  const lines = (progress.steps || []).map(
    (step, index) => `${index + 1}. ${toolLabel(step.tool)}`,
  );
  if (progress.running) {
    const current = progress.current_tool
      ? toolLabel(progress.current_tool)
      : "准备中";
    const attempt = progress.review_attempt
      ? `（第 ${progress.review_attempt}/${progress.max_review_attempts} 轮）`
      : "";
    lines.push(`正在执行：${current}${attempt}`);
  }
  return lines.length
    ? `MCP 执行进度：\n${lines.join("\n")}`
    : "MCP Agent 正在准备工具…";
}

function confirmationText(confirmation: AgentConfirmation): string {
  const args = confirmation.arguments || {};
  const prompts: Record<string, string> = {
    comfy_interrupt: "中断当前正在执行的 ComfyUI 任务？当前生成可能不完整。",
    comfy_clear_queue: "清空 ComfyUI 所有等待中的任务？",
    comfy_delete_queued:
      "从等待队列中删除指定任务：\n" + (args.prompt_ids || []).join("\n"),
    comfy_free_memory:
      "释放 ComfyUI 显存" +
      (args.unload_models ? "并卸载已加载模型" : "") +
      "？",
    comfy_delete_input_file: "删除 ComfyUI 输入素材：\n" + (args.filename || ""),
    comfy_forget_preferences: "清除助手保存的全部个人生成偏好？",
  };
  return prompts[confirmation.tool] || `确认执行操作：${confirmation.tool}`;
}

function reviewNotices(steps: AgentStep[]): string[] {
  const notices: string[] = [];
  for (const step of steps) {
    try {
      const result = JSON.parse(step.result || "{}") as {
        quality_review?: {
          status?: string;
          attempts?: Array<{ score?: number }>;
          threshold?: number;
          max_retries?: number;
          error?: string;
        };
      };
      const review = result.quality_review;
      if (!review) continue;
      const attempts = review.attempts || [];
      const finalScore = attempts.at(-1)?.score ?? "—";
      if (review.status === "passed") {
        notices.push(
          `视觉审查通过：${finalScore}/10（阈值 ${review.threshold ?? 8}/10，共 ${attempts.length} 轮）`,
        );
      } else if (review.status === "retry_limit") {
        notices.push(
          `视觉审查未通过：已达 ${review.max_retries ?? 2} 次优化重试上限，最终 ${finalScore}/10。`,
        );
      } else {
        notices.push(
          `视觉审查未完成或重生成失败：${review.error || "结果未达到通过条件"}`,
        );
      }
    } catch {
      // Non-JSON tool output is already represented by the Agent response.
    }
  }
  return notices;
}

function generatedFiles(steps: AgentStep[]): AgentFile[] {
  const files: AgentFile[] = [];
  for (const step of steps) {
    try {
      const result = JSON.parse(step.result || "{}") as {
        ok?: boolean;
        files?: AgentFile[];
      };
      if (result.ok && Array.isArray(result.files)) files.push(...result.files);
    } catch {
      // Non-JSON tool output is not a file result.
    }
  }
  return files;
}

const state = reactive({
  activeView: "overview" as StudioView,
  settingsTab: "comfyui" as SettingsTab,
  activePanel: null as "memory" | "rag" | null,
  apiStatus: "checking" as "checking" | "online" | "offline",
  history: readHistory(),
  messages: [] as AgentMessage[],
  chatInput: "",
  chatBusy: false,
  provider: "openai" as Provider,
  providerSelectionMade: false,
  customModels: [] as CustomModel[],
  customModelsError: "",
  selectedCustomId: null as number | null,
  customFormOpen: false,
  customFormBusy: false,
  customFormError: "",
  customForm: {
    name: "",
    baseUrl: "",
    model: "",
    apiKey: "",
  },
  generationBackend:
    (localStorage.getItem("generation_backend:" + PROFILE_ID) === "runninghub"
      ? "runninghub"
      : "comfyui") as GenerationBackend,
  useRag: false,
  qualityReviewEnabled: false,
  comfyUiUrl: "",
  comfyUiInput: "",
  comfyUiStatus: "正在读取 ComfyUI 网页地址…",
  diagnostics: null as InstanceDiagnostics | null,
  diagnosticsBusy: false,
  diagnosticsError: "",
  gallery: [] as GalleryEntry[],
  galleryLoading: false,
  galleryWarning: "",
  galleryError: "",
  lightbox: null as GalleryEntry | null,
  runningHubConfig: null as RunningHubConfig | null,
  runningHubWorkflowId: "",
  runningHubApiKey: "",
  runningHubFields: [] as RunningHubField[],
  runningHubPromptMapping: "",
  runningHubNegativeMapping: "",
  runningHubBusy: false,
  runningHubStatus: "",
  memory: { ...EMPTY_MEMORY } as AgentPreference,
  memoryBusy: false,
  memoryStatus: "",
  workflows: [] as WorkflowOption[],
  workflowStatus: "",
  modelPresets: [] as ModelPresetOption[],
  ragDocs: [] as RagDocument[],
  ragBusy: false,
  ragStatus: "",
});
const ragFileInput = ref<HTMLInputElement | null>(null);

const isRunningHub = computed(
  () => state.generationBackend === "runninghub",
);
const activeComfyUiCheck = computed(() =>
  state.diagnostics?.checks.find((check) => check.id === "comfyui"),
);
const activeBackendStatus = computed(() => {
  if (state.apiStatus === "checking") return "连接中";
  if (state.apiStatus === "offline") return "API 服务离线";
  if (isRunningHub.value) {
    return state.runningHubConfig?.configured
      ? "RunningHub 工作流已配置"
      : "请先配置 RunningHub 工作流";
  }
  if (state.diagnosticsBusy) return "正在检查 ComfyUI";
  if (state.diagnosticsError) return "ComfyUI 状态检查失败";
  if (activeComfyUiCheck.value?.status === "ok") return "ComfyUI 已就绪";
  if (activeComfyUiCheck.value?.status === "error") return "ComfyUI 不可用";
  return "ComfyUI 状态未检查";
});
const activeBackendStatusTone = computed(() => {
  if (state.apiStatus === "offline") return "error";
  if (state.apiStatus === "checking" || state.diagnosticsBusy) {
    return "pending";
  }
  if (isRunningHub.value) {
    return state.runningHubConfig?.configured ? "configured" : "unknown";
  }
  if (state.diagnosticsError || activeComfyUiCheck.value?.status === "error") {
    return "error";
  }
  if (activeComfyUiCheck.value?.status === "ok") return "ready";
  return "unknown";
});
const activeBackendHint = computed(() =>
  isRunningHub.value
    ? "远程任务由 RunningHub 执行；视觉审查重试可能再次提交任务并产生费用。"
    : "通过 MCP 直接控制本地 ComfyUI；有影响的操作会先请求确认。",
);
const librarySearch = ref("");
const libraryKind = ref<"all" | "image" | "video">("all");
const apiStatusLabel = computed(() => {
  if (state.apiStatus === "checking") return "连接中";
  return state.apiStatus === "online" ? "服务在线" : "服务离线";
});
const viewLabels: Record<StudioView, string> = {
  overview: "制作总览",
  agent: "自由创作 Agent",
  director: "Agent 1 · 分镜脚本",
  images: "Agent 2 · 图片资产",
  video: "Agent 3 · 视频生成",
  roughcut: "Agent 4 · 视频粗剪",
  pipeline: "Agent 5 · 一键出片",
  library: "个人资产库",
  settings: "工作流配置",
};
const activeViewLabel = computed(() => viewLabels[state.activeView]);
const filteredGallery = computed(() => {
  const query = librarySearch.value.trim().toLocaleLowerCase();
  return state.gallery.filter((entry) => {
    const matchesKind =
      libraryKind.value === "all" || entry.kind === libraryKind.value;
    const matchesQuery =
      !query || entry.label.toLocaleLowerCase().includes(query);
    return matchesKind && matchesQuery;
  });
});
const galleryImageCount = computed(
  () => state.gallery.filter((entry) => entry.kind === "image").length,
);
const galleryVideoCount = computed(
  () => state.gallery.filter((entry) => entry.kind === "video").length,
);

watch(
  () => state.history,
  (history) => {
    localStorage.setItem(
      "agent_history:" + PROFILE_ID,
      JSON.stringify(history.slice(-20)),
    );
  },
  { deep: true },
);

watch(
  () => state.generationBackend,
  (backend) => {
    localStorage.setItem("generation_backend:" + PROFILE_ID, backend);
  },
);

watch(
  () => state.messages.length,
  async () => {
    await nextTick();
    const chat = document.querySelector<HTMLElement>(".conversation-scroll");
    if (chat) chat.scrollTop = chat.scrollHeight;
  },
);

function pushNotice(text: string): void {
  state.messages.push({
    id: createId(),
    role: "system",
    text,
    media: [],
    notices: [],
  });
}

async function loadComfyUIConfig(): Promise<void> {
  const data = await apiJson<{
    comfyui_web_url: string;
    default_llm_provider?: Provider;
  }>("/api/instance", undefined, "读取 ComfyUI 地址失败");
  if (
    !state.providerSelectionMade &&
    ["openai", "llama"].includes(data.default_llm_provider || "")
  ) {
    state.provider = data.default_llm_provider as Provider;
  }

  const defaultUrl = normalizeComfyUIUrl(data.comfyui_web_url);
  const savedUrl = localStorage.getItem("comfyui_web_url:" + PROFILE_ID);
  if (savedUrl) {
    try {
      const url = normalizeComfyUIUrl(savedUrl);
      state.comfyUiUrl = url.href;
      state.comfyUiInput = url.href;
      state.comfyUiStatus = "当前浏览器已保存：" + url.origin;
      return;
    } catch {
      localStorage.removeItem("comfyui_web_url:" + PROFILE_ID);
      state.comfyUiStatus =
        "已忽略无效的浏览器地址，服务端默认地址：" + defaultUrl.origin;
    }
  } else {
    state.comfyUiStatus = "服务端默认地址：" + defaultUrl.origin;
  }
  state.comfyUiUrl = defaultUrl.href;
  state.comfyUiInput = defaultUrl.href;
}

async function loadCustomModels(): Promise<void> {
  try {
    state.customModels = await apiJson<CustomModel[]>(
      "/api/custom-models",
      undefined,
      "读取自定义模型失败",
    );
    state.customModelsError = "";
    if (
      state.selectedCustomId !== null &&
      !state.customModels.some((model) => model.id === state.selectedCustomId)
    ) {
      state.selectedCustomId = null;
    }
  } catch (error) {
    state.customModelsError = "自定义模型加载失败：" + errorMessage(error);
  }
}

async function loadGallery(): Promise<void> {
  state.galleryLoading = true;
  state.galleryError = "";
  try {
    const [imageResponse, agentResponse] = await Promise.all([
      apiFetch("/api/images"),
      apiFetch("/api/agent/files"),
    ]);
    const images = imageResponse.ok
      ? ((await imageResponse.json()) as ImageRecord[])
      : [];
    const files = agentResponse.ok
      ? ((await agentResponse.json()) as AgentFileRecord[])
      : [];
    if (!imageResponse.ok && !agentResponse.ok) {
      throw new Error("读取结果库失败");
    }
    state.gallery = [
      ...images.map((image) => ({
        kind: image.media_type,
        url: image.file_url,
        label: `任务 #${image.task_id}`,
      })),
      ...files.map((file) => ({
        kind: file.kind,
        url: "/api/agent/files/" + encodeURIComponent(file.filename),
        label: new Date(file.created_at * 1000).toLocaleString(),
      })),
    ];
    state.galleryWarning =
      !imageResponse.ok || !agentResponse.ok
        ? "部分结果来源读取失败；当前仅显示已成功读取的结果。"
        : "";
  } catch (error) {
    state.gallery = [];
    state.galleryWarning = "";
    state.galleryError = "结果库加载失败：" + errorMessage(error);
  } finally {
    state.galleryLoading = false;
  }
}

async function loadAvailableWorkflows(): Promise<void> {
  state.workflowStatus = "正在读取工作流…";
  try {
    state.workflows = await apiJson<WorkflowOption[]>(
      "/api/workflows",
      undefined,
      "读取 ComfyUI 工作流失败",
    );
    state.workflowStatus = state.workflows.length
      ? `已发现 ${state.workflows.length} 个工作流`
      : "未发现可用工作流，请先在 ComfyUI 中保存工作流。";
  } catch (error) {
    state.workflows = [];
    state.workflowStatus = "工作流读取失败：" + errorMessage(error);
  }
}

async function loadRunningHubConfig(): Promise<RunningHubConfig> {
  const config = await apiJson<RunningHubConfig>(
    "/api/runninghub/config",
    undefined,
    "读取 RunningHub 配置失败",
  );
  state.runningHubConfig = config;
  state.runningHubWorkflowId = config.workflow_id || "";
  return config;
}

async function loadRunningHubNodes(
  workflowId = state.runningHubWorkflowId,
  config: RunningHubConfig | null = state.runningHubConfig,
): Promise<void> {
  const normalizedWorkflowId = workflowId.trim();
  if (!normalizedWorkflowId) throw new Error("请填写 Workflow ID");
  const request: { workflow_id: string; api_key?: string } = {
    workflow_id: normalizedWorkflowId,
  };
  if (state.runningHubApiKey.trim()) {
    request.api_key = state.runningHubApiKey.trim();
  }
  const result = await apiJson<{ fields: RunningHubField[] }>(
    "/api/runninghub/workflow-nodes",
    { method: "POST", body: JSON.stringify(request) },
    "读取工作流输入失败",
  );
  state.runningHubFields = result.fields;
  const saved =
    config?.workflow_id === normalizedWorkflowId ? config : null;
  state.runningHubPromptMapping =
    saved?.prompt_node_id && saved.prompt_field
      ? runningHubMappingValue(saved.prompt_node_id, saved.prompt_field)
      : "";
  state.runningHubNegativeMapping =
    saved?.negative_prompt_node_id && saved.negative_prompt_field
      ? runningHubMappingValue(
          saved.negative_prompt_node_id,
          saved.negative_prompt_field,
        )
      : "";
  state.runningHubStatus =
    `已发现 ${result.fields.length} 个文本输入，请选择正向提示词映射。`;
}

async function loadAgentMemory(): Promise<void> {
  state.memoryBusy = true;
  state.memoryStatus = "正在读取个人偏好…";
  try {
    const [workflowResponse, presetResponse, memory] = await Promise.all([
      apiFetch("/api/workflows"),
      apiFetch("/api/model-presets"),
      apiJson<AgentPreference & { updated_at?: string | null }>(
        "/api/agent/memory",
        undefined,
        "读取个人记忆失败",
      ),
    ]);
    state.workflows = workflowResponse.ok
      ? ((await workflowResponse.json()) as WorkflowOption[])
      : [];
    state.modelPresets = presetResponse.ok
      ? ((await presetResponse.json()) as ModelPresetOption[])
      : [];
    state.memory = {
      style: memory.style || "",
      workflow: memory.workflow || "",
      model_preset: memory.model_preset || "",
      aspect_ratio: memory.aspect_ratio || "",
      megapixels: memory.megapixels ?? "",
      negative_prompt: memory.negative_prompt || "",
      notes: memory.notes || "",
    };
    const updated = memory.updated_at
      ? "上次更新：" + new Date(memory.updated_at).toLocaleString()
      : "尚未保存个人偏好";
    state.memoryStatus =
      !workflowResponse.ok || !presetResponse.ok
        ? updated + "（部分选项读取失败）"
        : updated;
  } catch (error) {
    state.memoryStatus = "读取失败：" + errorMessage(error);
  } finally {
    state.memoryBusy = false;
  }
}

async function loadRagDocs(): Promise<void> {
  state.ragBusy = true;
  state.ragStatus = "正在加载知识库…";
  try {
    state.ragDocs = await apiJson<RagDocument[]>(
      "/api/rag/docs",
      undefined,
      "读取知识库失败",
    );
    state.ragStatus = state.ragDocs.length ? "" : "暂无文档";
  } catch (error) {
    state.ragDocs = [];
    state.ragStatus = "加载失败：" + errorMessage(error);
  } finally {
    state.ragBusy = false;
  }
}

async function checkDiagnostics(): Promise<void> {
  state.diagnosticsBusy = true;
  state.diagnosticsError = "";
  state.diagnostics = null;
  try {
    state.diagnostics = await apiJson<InstanceDiagnostics>(
      "/api/instance/diagnostics",
      undefined,
      "检查服务状态失败",
    );
  } catch (error) {
    state.diagnosticsError = errorMessage(error);
  } finally {
    state.diagnosticsBusy = false;
  }
}

async function checkApiAvailability(): Promise<void> {
  try {
    const response = await apiFetch("/health");
    state.apiStatus = response.ok ? "online" : "offline";
  } catch {
    state.apiStatus = "offline";
  }
}

function saveComfyUIUrl(): void {
  try {
    const url = normalizeComfyUIUrl(state.comfyUiInput);
    localStorage.setItem("comfyui_web_url:" + PROFILE_ID, url.href);
    state.comfyUiUrl = url.href;
    state.comfyUiStatus = "当前浏览器已保存：" + url.origin;
  } catch (error) {
    state.comfyUiStatus = "地址无效或保存失败：" + errorMessage(error);
  }
}

async function resetComfyUIUrl(): Promise<void> {
  localStorage.removeItem("comfyui_web_url:" + PROFILE_ID);
  try {
    await loadComfyUIConfig();
  } catch (error) {
    state.comfyUiUrl = "";
    state.comfyUiStatus = "ComfyUI 地址不可用：" + errorMessage(error);
  }
}

async function openSettingsTab(tab: SettingsTab): Promise<void> {
  state.activeView = "settings";
  state.activePanel = null;
  state.settingsTab = tab;
  if (tab === "runninghub") {
    state.runningHubStatus = "正在读取配置…";
    try {
      const config = await loadRunningHubConfig();
      if (config.configured && config.workflow_id) {
        await loadRunningHubNodes(config.workflow_id, config);
      } else {
        state.runningHubStatus = "尚未配置 API Key 和工作流。";
      }
    } catch (error) {
      state.runningHubStatus = "读取失败：" + errorMessage(error);
    }
  } else if (tab === "comfyui") {
    void loadAvailableWorkflows();
  } else if (tab === "models") {
    await loadCustomModels();
  }
}

function selectProvider(provider: Provider, customId: number | null = null): void {
  if (state.chatBusy) return;
  state.providerSelectionMade = true;
  state.provider = provider;
  state.selectedCustomId = provider === "custom" ? customId : null;
}

async function saveCustomModel(): Promise<void> {
  const request = {
    name: state.customForm.name.trim() || "自定义模型",
    base_url: state.customForm.baseUrl.trim(),
    model: state.customForm.model.trim(),
    api_key: state.customForm.apiKey.trim(),
  };
  if (!request.base_url || !request.model || !request.api_key) {
    state.customFormError = "请填写 Base URL、模型名和 API Key";
    return;
  }
  state.customFormBusy = true;
  state.customFormError = "";
  try {
    await apiJson(
      "/api/custom-models",
      { method: "POST", body: JSON.stringify(request) },
      "保存自定义模型失败",
    );
    state.customForm = { name: "", baseUrl: "", model: "", apiKey: "" };
    state.customFormOpen = false;
    await loadCustomModels();
  } catch (error) {
    state.customFormError = "保存失败：" + errorMessage(error);
  } finally {
    state.customFormBusy = false;
  }
}

async function deleteCustomModel(model: CustomModel): Promise<void> {
  if (!window.confirm(`确认删除“${model.name}”模型配置？`)) return;
  try {
    await apiJson(
      "/api/custom-models/" + model.id,
      { method: "DELETE" },
      "删除自定义模型失败",
    );
    if (state.selectedCustomId === model.id) selectProvider("openai");
    await loadCustomModels();
  } catch (error) {
    window.alert("删除失败：" + errorMessage(error));
  }
}

async function togglePanel(panel: "runninghub" | "memory" | "rag"): Promise<void> {
  if (panel === "runninghub") {
    await openSettingsTab("runninghub");
    return;
  }
  if (state.activePanel === panel) {
    state.activePanel = null;
    return;
  }
  state.activePanel = panel;
  if (panel === "memory") {
    await loadAgentMemory();
  } else {
    await loadRagDocs();
  }
}

async function discoverRunningHubFields(): Promise<void> {
  state.runningHubBusy = true;
  state.runningHubStatus = "正在读取工作流文本输入…";
  try {
    await loadRunningHubNodes();
  } catch (error) {
    state.runningHubStatus = "加载失败：" + errorMessage(error);
  } finally {
    state.runningHubBusy = false;
  }
}

async function saveRunningHubConfig(): Promise<void> {
  const prompt = parseMapping(state.runningHubPromptMapping);
  const negative = parseMapping(state.runningHubNegativeMapping);
  if (!prompt) {
    state.runningHubStatus = "请先加载工作流并选择正向提示词输入。";
    return;
  }
  const request = {
    workflow_id: state.runningHubWorkflowId.trim(),
    prompt_node_id: prompt.node_id,
    prompt_field: prompt.field_name,
    negative_prompt_node_id: negative?.node_id || null,
    negative_prompt_field: negative?.field_name || null,
    ...(state.runningHubApiKey.trim()
      ? { api_key: state.runningHubApiKey.trim() }
      : {}),
  };
  state.runningHubBusy = true;
  state.runningHubStatus = "正在加密保存配置…";
  try {
    await apiJson(
      "/api/runninghub/config",
      { method: "PUT", body: JSON.stringify(request) },
      "保存 RunningHub 配置失败",
    );
    state.runningHubApiKey = "";
    const config = await loadRunningHubConfig();
    if (config.workflow_id) {
      await loadRunningHubNodes(config.workflow_id, config);
    }
    state.runningHubStatus = "RunningHub 配置已保存。";
  } catch (error) {
    state.runningHubStatus = "保存失败：" + errorMessage(error);
  } finally {
    state.runningHubBusy = false;
  }
}

async function saveAgentMemory(): Promise<void> {
  const megapixels = String(state.memory.megapixels).trim();
  const request = {
    style: state.memory.style.trim(),
    workflow: state.memory.workflow.trim(),
    model_preset: state.memory.model_preset.trim(),
    aspect_ratio: state.memory.aspect_ratio || null,
    megapixels: megapixels ? Number(megapixels) : null,
    negative_prompt: state.memory.negative_prompt.trim(),
    notes: state.memory.notes.trim(),
  };
  state.memoryBusy = true;
  try {
    await apiJson(
      "/api/agent/memory",
      { method: "PATCH", body: JSON.stringify(request) },
      "保存个人记忆失败",
    );
    state.memoryStatus = "个人偏好已保存";
  } catch (error) {
    state.memoryStatus = "保存失败：" + errorMessage(error);
  } finally {
    state.memoryBusy = false;
  }
}

async function clearAgentMemory(): Promise<void> {
  if (!window.confirm("确认清除助手保存的全部个人生成偏好？")) return;
  state.memoryBusy = true;
  try {
    await apiJson(
      "/api/agent/memory",
      { method: "DELETE" },
      "清除个人记忆失败",
    );
    await loadAgentMemory();
  } catch (error) {
    state.memoryStatus = "清除个人记忆失败：" + errorMessage(error);
  } finally {
    state.memoryBusy = false;
  }
}

async function uploadRagDocument(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  const formData = new FormData();
  formData.append("file", file);
  state.ragBusy = true;
  state.ragStatus = "正在上传…";
  try {
    await apiJson(
      "/api/rag/docs",
      { method: "POST", body: formData },
      "上传知识库文档失败",
    );
    await loadRagDocs();
  } catch (error) {
    state.ragStatus = "上传失败：" + errorMessage(error);
  } finally {
    state.ragBusy = false;
    input.value = "";
  }
}

async function deleteRagDocument(filename: string): Promise<void> {
  if (!window.confirm(`确认删除 ${filename}？`)) return;
  state.ragBusy = true;
  try {
    await apiJson(
      "/api/rag/docs/" + encodeURIComponent(filename),
      { method: "DELETE" },
      "删除知识库文档失败",
    );
    await loadRagDocs();
  } catch (error) {
    state.ragStatus = "删除失败：" + errorMessage(error);
  } finally {
    state.ragBusy = false;
  }
}

async function pollAgent(
  text: string,
  history: AgentHistoryEntry[],
  onProgress: (text: string) => void,
): Promise<{ answer: string; steps: AgentStep[] }> {
  const request: Record<string, unknown> = {
    request: text,
    history,
    provider: state.provider,
    generation_backend: state.generationBackend,
    use_rag: state.useRag,
    quality_review_enabled: state.qualityReviewEnabled,
  };
  if (state.provider === "custom") {
    request.custom_config_id = state.selectedCustomId;
  }
  const submitted = await apiJson<{ task_id: string }>(
    "/api/agent/mcp",
    { method: "POST", body: JSON.stringify(request) },
    "Agent 提交失败",
  );
  onProgress(
    state.generationBackend === "runninghub"
      ? "Agent 已接收，正在连接 RunningHub…"
      : "Agent 已接收，正在连接 ComfyUI…",
  );

  const deadline = Date.now() + 20 * 60 * 1000;
  const handledConfirmations = new Set<string>();
  let lastProgressKey = "";
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    const status = await apiJson<AgentTaskStatus>(
      "/api/agent/result/" + encodeURIComponent(submitted.task_id),
      undefined,
      "读取 Agent 任务状态失败",
    );
    if (status.state === "CONFIRMATION_REQUIRED" && status.confirmation) {
      const confirmation = status.confirmation;
      if (!handledConfirmations.has(confirmation.confirmation_id)) {
        handledConfirmations.add(confirmation.confirmation_id);
        const confirmed = window.confirm(confirmationText(confirmation));
        await apiJson(
          "/api/agent/confirm/" + encodeURIComponent(submitted.task_id),
          {
            method: "POST",
            body: JSON.stringify({
              confirmation_id: confirmation.confirmation_id,
              confirmed,
            }),
          },
          "提交操作确认失败",
        );
        onProgress(
          confirmed
            ? "已确认操作，Agent 继续执行…"
            : "已拒绝操作，Agent 正在收尾…",
        );
      }
    }
    if (status.progress) {
      const key = [
        (status.progress.steps || []).length,
        status.progress.current_tool || "",
        Boolean(status.progress.running),
      ].join(":");
      if (key !== lastProgressKey) {
        lastProgressKey = key;
        onProgress(progressText(status.progress));
      }
    }
    if (status.state === "SUCCESS") {
      const result =
        typeof status.result === "object" && status.result !== null
          ? status.result
          : {};
      if (result.status === "failed") {
        throw new Error(result.error || "Agent 执行失败");
      }
      return {
        answer: result.answer || "Agent 已完成。",
        steps: result.steps || [],
      };
    }
    if (status.state === "FAILURE" || status.state === "REVOKED") {
      throw new Error(
        typeof status.result === "string"
          ? status.result
          : status.state,
      );
    }
  }
  throw new Error("等待 Agent 超时；后台任务可能仍在执行");
}

async function sendChat(): Promise<void> {
  const text = state.chatInput.trim();
  if (!text || state.chatBusy) return;
  if (state.provider === "custom" && state.selectedCustomId === null) {
    pushNotice("请先选择一个有效的自定义模型配置。");
    return;
  }
  if (state.generationBackend === "runninghub") {
    try {
      const config = await loadRunningHubConfig();
      if (!config.configured) {
        pushNotice("请先打开“RunningHub 设置”并完成 API Key 与工作流配置。");
        void openSettingsTab("runninghub");
        return;
      }
    } catch (error) {
      pushNotice("读取 RunningHub 配置失败：" + errorMessage(error));
      void openSettingsTab("runninghub");
      return;
    }
  }

  const history = state.history.slice(-20);
  state.history.push({ role: "user", content: text });
  state.history = state.history.slice(-20);
  const userMessage: AgentMessage = {
    id: createId(),
    role: "user",
    text,
    media: [],
    notices: [],
  };
  const progressId = createId();
  const progressMessage: AgentMessage = {
    id: progressId,
    role: "assistant",
    text: "正在准备创作任务…",
    media: [],
    notices: [],
  };
  state.messages.push(userMessage, progressMessage);
  state.chatInput = "";
  state.chatBusy = true;
  const endActivity = beginAgentActivity();

  try {
    const result = await pollAgent(text, history, (progress) => {
      const message = state.messages.find((item) => item.id === progressId);
      if (message) message.text = progress;
    });
    const message = state.messages.find((item) => item.id === progressId);
    if (message) {
      message.text = result.answer;
      message.media = generatedFiles(result.steps).map((file) => ({
        kind: file.kind,
        url: "/api/agent/files/" + encodeURIComponent(file.filename),
      }));
      message.notices = reviewNotices(result.steps);
    }
    state.history.push({ role: "assistant", content: result.answer });
    state.history = state.history.slice(-20);
    await loadGallery();
  } catch (error) {
    const message = state.messages.find((item) => item.id === progressId);
    if (message) message.text = "Agent 执行失败：" + errorMessage(error);
  } finally {
    endActivity();
    state.chatBusy = false;
    await nextTick();
    document.querySelector<HTMLTextAreaElement>(".composer-input")?.focus();
  }
}

function clearChat(): void {
  if (state.chatBusy || !window.confirm("确认清空当前 Agent 会话？")) return;
  state.history = [];
  state.messages = [];
  localStorage.removeItem("agent_history:" + PROFILE_ID);
}

function openLightbox(entry: GalleryEntry): void {
  state.lightbox = entry;
}

function closeLightbox(): void {
  state.lightbox = null;
}

function handleEscape(event: KeyboardEvent): void {
  if (event.key === "Escape") closeLightbox();
}

function suggestPrompt(prompt: string): void {
  state.chatInput = prompt;
  document.querySelector<HTMLTextAreaElement>(".composer-input")?.focus();
}

function handleWindowFocus(): void {
  void checkApiAvailability();
}

onMounted(() => {
  state.messages = mapHistoryToMessages(state.history);
  void checkApiAvailability();
  healthCheckTimer = window.setInterval(() => {
    void checkApiAvailability();
  }, 30_000);
  window.addEventListener("focus", handleWindowFocus);
  void loadCustomModels();
  void loadGallery();
  loadComfyUIConfig().catch((error: unknown) => {
    const savedUrl = localStorage.getItem("comfyui_web_url:" + PROFILE_ID);
    if (savedUrl) {
      try {
        const url = normalizeComfyUIUrl(savedUrl);
        state.comfyUiUrl = url.href;
        state.comfyUiInput = url.href;
        state.comfyUiStatus =
          "当前浏览器已保存（服务端配置不可读）：" + url.origin;
        return;
      } catch (savedUrlError) {
        state.comfyUiStatus =
          "已保存的 ComfyUI 地址无效：" + errorMessage(savedUrlError);
        return;
      }
    }
    state.comfyUiStatus = "ComfyUI 地址不可用：" + errorMessage(error);
  });
  loadRunningHubConfig().catch((error: unknown) => {
    state.runningHubConfig = null;
    state.runningHubStatus = "配置状态不可读：" + errorMessage(error);
  });
  window.addEventListener("keydown", handleEscape);
});

onUnmounted(() => {
  if (healthCheckTimer !== undefined) window.clearInterval(healthCheckTimer);
  window.removeEventListener("focus", handleWindowFocus);
  window.removeEventListener("keydown", handleEscape);
});
</script>

<template>
  <div class="studio-shell">
    <aside class="sidebar">
      <a class="brand" href="#" @click.prevent="state.activeView = 'overview'">
        <span class="brand-mark">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <path d="m9.5 16 12-8m-12 8 12 8" />
            <circle cx="7" cy="16" r="3" />
            <circle cx="24" cy="7" r="3" />
            <circle cx="24" cy="25" r="3" />
          </svg>
        </span>
        <span class="brand-copy">
          <strong>AI STUDIO</strong>
          <small>本地与云端工作流</small>
        </span>
      </a>

      <div class="sidebar-label">制作工作台</div>
      <nav class="side-nav" aria-label="制作工作台导航">
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'overview' }"
          type="button"
          :aria-current="state.activeView === 'overview' ? 'page' : undefined"
          @click="state.activeView = 'overview'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="m3.5 10 8.5-6.5 8.5 6.5v10h-6v-6h-5v6h-6z" />
          </svg>
          <span>制作总览</span>
        </button>
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'director' }"
          type="button"
          :aria-current="state.activeView === 'director' ? 'page' : undefined"
          @click="state.activeView = 'director'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 3.5h9l5 5V20H5zM14 3.5v5h5M8 12h8m-8 4h8" />
          </svg>
          <span>Agent 1 · 分镜脚本</span>
        </button>
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'images' }"
          type="button"
          :aria-current="state.activeView === 'images' ? 'page' : undefined"
          @click="state.activeView = 'images'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 5.5h16v13H4zM5 17l5-5 3 3 2-2 4 4" />
            <circle cx="15.5" cy="9" r="1.5" />
          </svg>
          <span>Agent 2 · 图片资产</span>
        </button>
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'video' }"
          type="button"
          :aria-current="state.activeView === 'video' ? 'page' : undefined"
          @click="state.activeView = 'video'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 5.5h11.5v13H4zM15.5 10l5-3v10l-5-3" />
            <path d="M7 8.5h5.5M7 12h5.5M7 15.5h3" />
          </svg>
          <span>Agent 3 · 视频生成</span>
        </button>
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'roughcut' }"
          type="button"
          :aria-current="state.activeView === 'roughcut' ? 'page' : undefined"
          @click="state.activeView = 'roughcut'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-5 2v4" />
          </svg>
          <span>Agent 4 · 视频粗剪</span>
          <span class="nav-stage-state">未就绪</span>
        </button>
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'pipeline' }"
          type="button"
          :aria-current="state.activeView === 'pipeline' ? 'page' : undefined"
          @click="state.activeView = 'pipeline'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 5h5v5H4zM15 14h5v5h-5zM9 7.5h3a3 3 0 0 1 3 3V14M6.5 10v7H15" />
          </svg>
          <span>Agent 5 · 一键出片</span>
          <span class="nav-stage-state">未就绪</span>
        </button>
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'library' }"
          type="button"
          :aria-current="state.activeView === 'library' ? 'page' : undefined"
          @click="state.activeView = 'library'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 6h6l2 2h8v11H4zM4 9h16" />
          </svg>
          <span>个人资产库</span>
        </button>
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'settings' }"
          type="button"
          :aria-current="state.activeView === 'settings' ? 'page' : undefined"
          @click="void openSettingsTab('comfyui')"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="3" />
            <path d="m19.4 15 .1.1 1.4 1.1-1.6 2.8-1.7-.7c-.5.4-1.1.7-1.8.9l-.2 1.8h-3.2l-.2-1.8a7 7 0 0 1-1.8-.9l-1.7.7-1.6-2.8 1.4-1.1a7 7 0 0 1 0-2l-1.4-1.1 1.6-2.8 1.7.7c.5-.4 1.1-.7 1.8-.9l.2-1.8h3.2l.2 1.8c.7.2 1.3.5 1.8.9l1.7-.7 1.6 2.8-1.4 1.1a7 7 0 0 1 0 2Z" />
          </svg>
          <span>工作流配置</span>
        </button>
        <button
          class="side-nav-item"
          :class="{ active: state.activeView === 'agent' }"
          type="button"
          :aria-current="state.activeView === 'agent' ? 'page' : undefined"
          @click="state.activeView = 'agent'"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 3.2 14.7 9.3 21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7L12 3.2Z" />
          </svg>
          <span>自由创作</span>
        </button>
      </nav>

      <div class="sidebar-label sidebar-label-spaced">管理与工具</div>
      <div class="side-tools">
        <button type="button" @click="void togglePanel('memory')">
          <span class="tool-dot blue" />
          个人创作偏好
        </button>
        <button type="button" @click="void togglePanel('rag')">
          <span class="tool-dot mint" />
          项目知识库
        </button>
      </div>

      <div class="sidebar-spacer" />
      <div class="sidebar-status">
        <span
          class="status-pulse"
          :class="`status-${state.apiStatus}`"
        />
        <div>
          <strong>本地工作区</strong>
          <small>{{ apiStatusLabel }}</small>
        </div>
        <span class="status-menu">···</span>
      </div>
      <div class="sidebar-footer">版本 0.1 · LOCAL FIRST</div>
    </aside>

    <main class="main-content">
      <header class="topbar">
        <div class="breadcrumb">
          <span>工作区</span>
          <span class="breadcrumb-divider">/</span>
          <strong>{{ activeViewLabel }}</strong>
        </div>
        <div class="topbar-actions">
          <span class="connection-pill" :class="`connection-${state.apiStatus}`">
            <span class="status-pulse" :class="`status-${state.apiStatus}`" />
            <span>API</span>
            <b>{{ apiStatusLabel }}</b>
          </span>
          <button
            class="icon-button"
            type="button"
            aria-label="刷新创作结果"
            title="刷新创作结果"
            @click="void loadGallery()"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M20 7v5h-5M4 17v-5h5" />
              <path d="M5.7 9A7 7 0 0 1 18 6.8L20 12M4 12l2 5.2A7 7 0 0 0 18.3 15" />
            </svg>
          </button>
          <a
            class="topbar-comfy-link"
            :href="state.comfyUiUrl || '#'"
            target="_blank"
            rel="noopener noreferrer"
            :aria-disabled="!state.comfyUiUrl"
            @click="!state.comfyUiUrl && $event.preventDefault()"
          >
            打开 ComfyUI
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M14 4h6v6m-.5-5.5L11 13m-2-7H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-3" />
            </svg>
          </a>
        </div>
      </header>

      <section v-if="state.activeView === 'overview'" class="creator-page">
        <div class="creator-heading">
          <div>
            <p class="creator-kicker">制作工作台</p>
            <h1>把故事，一步步交付为分集粗剪。</h1>
            <p class="creator-intro">
              从分集分镜到图片、逐镜头视频，再按集组接。每一步都使用真实任务和产物。
            </p>
          </div>
          <button
            class="small-primary-button"
            type="button"
            @click="state.activeView = 'director'"
          >
            开始制作分镜
          </button>
        </div>

        <section class="creator-flow" aria-label="Agent 1 至 Agent 5 制作流程">
          <div class="creator-section-heading">
            <div>
              <h2>制作流程</h2>
              <p>先完成单阶段制作，再接入分集粗剪与一键编排。</p>
            </div>
            <span class="creator-flow-note">当前 Agent 1–3 已接入现有功能</span>
          </div>
          <div class="creator-flow-steps">
            <button
              class="creator-flow-step is-available"
              type="button"
              @click="state.activeView = 'director'"
            >
              <span class="creator-step-number">01</span>
              <strong>分镜脚本</strong>
              <small>输入剧本，生成并编辑分镜</small>
            </button>
            <span class="creator-flow-connector" aria-hidden="true" />
            <button
              class="creator-flow-step is-available"
              type="button"
              @click="state.activeView = 'images'"
            >
              <span class="creator-step-number">02</span>
              <strong>图片资产</strong>
              <small>{{ galleryImageCount }} 个图片结果已归档</small>
            </button>
            <span class="creator-flow-connector" aria-hidden="true" />
            <button
              class="creator-flow-step is-available"
              type="button"
              @click="state.activeView = 'video'"
            >
              <span class="creator-step-number">03</span>
              <strong>视频生成</strong>
              <small>{{ galleryVideoCount }} 个视频结果已归档</small>
            </button>
            <span class="creator-flow-connector is-pending" aria-hidden="true" />
            <button
              class="creator-flow-step is-pending"
              type="button"
              @click="state.activeView = 'roughcut'"
            >
              <span class="creator-step-number">04</span>
              <strong>视频粗剪</strong>
              <small>后端组接任务尚未接入</small>
            </button>
            <span class="creator-flow-connector is-pending" aria-hidden="true" />
            <button
              class="creator-flow-step is-pending"
              type="button"
              @click="state.activeView = 'pipeline'"
            >
              <span class="creator-step-number">05</span>
              <strong>一键出片</strong>
              <small>可恢复编排任务尚未接入</small>
            </button>
          </div>
        </section>

        <div class="creator-overview-grid">
          <section class="creator-panel creator-assets-summary">
            <div class="creator-section-heading">
              <div>
                <h2>现有创作产物</h2>
                <p>从当前 API 结果中读取，不展示演示数据。</p>
              </div>
              <button
                class="text-action"
                type="button"
                @click="state.activeView = 'library'"
              >
                打开资产库
              </button>
            </div>
            <div class="creator-asset-counts">
              <button
                type="button"
                class="creator-asset-count"
                @click="
                  libraryKind = 'image';
                  state.activeView = 'library';
                "
              >
                <span class="creator-asset-icon image-mark">图</span>
                <span><strong>{{ galleryImageCount }}</strong><small>图片</small></span>
              </button>
              <button
                type="button"
                class="creator-asset-count"
                @click="
                  libraryKind = 'video';
                  state.activeView = 'library';
                "
              >
                <span class="creator-asset-icon video-mark">影</span>
                <span><strong>{{ galleryVideoCount }}</strong><small>视频</small></span>
              </button>
              <div class="creator-asset-count is-unavailable">
                <span class="creator-asset-icon script-mark">分</span>
                <span><strong>未接入</strong><small>统一分镜资产</small></span>
              </div>
              <div class="creator-asset-count is-unavailable">
                <span class="creator-asset-icon cut-mark">剪</span>
                <span><strong>未接入</strong><small>分集粗剪资产</small></span>
              </div>
            </div>
          </section>

          <section class="creator-panel creator-runtime-summary">
            <div class="creator-section-heading">
              <div>
                <h2>运行连接</h2>
                <p>任务使用的当前后端与工作区状态。</p>
              </div>
              <button
                class="text-action"
                type="button"
                @click="void openSettingsTab('comfyui')"
              >
                配置
              </button>
            </div>
            <div class="creator-runtime-row">
              <span class="creator-runtime-indicator" :class="`tone-${state.apiStatus}`" />
              <span>工作区 API</span>
              <strong>{{ apiStatusLabel }}</strong>
            </div>
            <div class="creator-runtime-row">
              <span
                class="creator-runtime-indicator"
                :class="`tone-${activeBackendStatusTone}`"
              />
              <span>{{ state.generationBackend === "runninghub" ? "RunningHub" : "ComfyUI" }}</span>
              <strong>{{ activeBackendStatus }}</strong>
            </div>
            <p class="creator-runtime-hint">{{ activeBackendHint }}</p>
          </section>
        </div>
      </section>

      <section v-if="state.activeView === 'agent'" class="workspace">
        <div class="page-heading">
          <div>
            <div class="eyebrow">
              <span class="eyebrow-line" /> 创作控制台
            </div>
            <h1>从描述，到可控的出图。</h1>
            <p>
              Agent 理解需求并调度生成，你仍可选择 ComfyUI 或 RunningHub，掌控每条工作流。
            </p>
            <div class="hero-process" aria-label="创作流程">
              <span><b />描述想法</span>
              <i />
              <span><b />Agent 调度</span>
              <i />
              <span><b />查看作品</span>
            </div>
          </div>
          <div class="heading-art" aria-hidden="true">
            <div class="workflow-map">
              <div class="map-node prompt-node">
                <span class="map-node-symbol">Aa</span>
                <span>提示词</span>
              </div>
              <div class="map-node engine-node">
                <span class="map-node-symbol">
                  <svg viewBox="0 0 24 24">
                    <rect x="4" y="4" width="6" height="6" rx="1" />
                    <rect x="14" y="14" width="6" height="6" rx="1" />
                    <path d="M10 7h3a2 2 0 0 1 2 2v5" />
                  </svg>
                </span>
                <span>工作流</span>
              </div>
              <div class="map-node output-node">
                <span class="map-node-symbol">
                  <svg viewBox="0 0 24 24">
                    <rect x="3.5" y="3.5" width="17" height="17" rx="2" />
                    <circle cx="9" cy="9" r="1.5" />
                    <path d="m5 18 5-5 3 3 2-2 4 4" />
                  </svg>
                </span>
                <span>作品</span>
              </div>
            </div>
            <span class="map-caption">从描述到成片，由你决定每一步</span>
          </div>
        </div>

        <div class="workspace-grid">
          <section class="conversation-panel">
            <div class="conversation-header">
              <div class="assistant-identity">
                <span class="assistant-avatar">
                  <svg viewBox="0 0 32 32" aria-hidden="true">
                    <path d="M16 4.5 18.8 12l7.7 4-7.7 3L16 27l-2.8-8L5.5 16l7.7-4L16 4.5Z" />
                    <circle cx="23.5" cy="7.5" r="1.5" />
                  </svg>
                </span>
                <span>
                  <strong>创作 Agent</strong>
                  <small>随时准备把灵感变成作品</small>
                </span>
              </div>
              <div class="conversation-tools">
                <span class="agent-online" :class="`agent-${state.apiStatus}`">
                  <i />{{ apiStatusLabel }}
                </span>
                <button
                  class="icon-button subtle"
                  type="button"
                  title="清空会话"
                  aria-label="清空会话"
                  :disabled="state.chatBusy"
                  @click="clearChat"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M4 7h16M9 7V4h6v3m3 0-.8 13H6.8L6 7m4 4v5m4-5v5" />
                  </svg>
                </button>
              </div>
            </div>

            <div class="conversation-scroll">
              <div v-if="!state.messages.length" class="welcome-state">
                <div class="welcome-spark">✦</div>
                <h2>你好，今天想创作什么？</h2>
                <p>
                  写下一个简单的想法，或从下面的灵感开始。Agent 会理解你的需求并调用选定的生成工作流。
                </p>
                <div class="prompt-suggestions">
                  <button
                    type="button"
                    @click="suggestPrompt('一张雨后东京街头的电影感夜景，霓虹倒映在湿润路面，胶片摄影风格')"
                  >
                    <span>✦</span> 电影感城市夜景
                  </button>
                  <button
                    type="button"
                    @click="suggestPrompt('一只戴着宇航员头盔的柯基漂浮在梦幻星云中，温暖柔和的插画风格')"
                  >
                    <span>◈</span> 梦幻角色插画
                  </button>
                  <button
                    type="button"
                    @click="suggestPrompt('为一款极简香水创作高级商业产品摄影，柔和自然光，米白背景')"
                  >
                    <span>▧</span> 商业产品摄影
                  </button>
                </div>
              </div>

              <div
                v-for="message in state.messages"
                :key="message.id"
                class="message-row"
                :class="`message-${message.role}`"
              >
                <span
                  v-if="message.role === 'assistant'"
                  class="message-avatar"
                  aria-label="Agent"
                >
                  ✦
                </span>
                <div class="message-content">
                  <div class="message-meta">
                    {{ message.role === "user" ? "你" : message.role === "assistant" ? "创作 Agent" : "提示" }}
                  </div>
                  <div
                    class="message-bubble"
                    :class="{
                      'message-bubble-user': message.role === 'user',
                      'message-bubble-system': message.role === 'system',
                      'message-bubble-progress':
                        state.chatBusy &&
                        message.role === 'assistant' &&
                        message.id === state.messages.at(-1)?.id,
                    }"
                  >
                    <span class="message-text">{{ message.text }}</span>
                    <span
                      v-if="
                        state.chatBusy &&
                        message.role === 'assistant' &&
                        message.id === state.messages.at(-1)?.id
                      "
                      class="typing-dots"
                      aria-label="正在执行"
                    >
                      <i /><i /><i />
                    </span>
                  </div>
                  <div
                    v-for="(notice, index) in message.notices"
                    :key="`${message.id}-notice-${index}`"
                    class="review-notice"
                  >
                    <span>✧</span>{{ notice }}
                  </div>
                  <div v-if="message.media.length" class="message-media">
                    <a
                      v-for="(media, index) in message.media"
                      :key="`${message.id}-media-${index}`"
                      class="message-media-card"
                      href="#"
                      @click.prevent="
                        openLightbox({
                          ...media,
                          label: media.kind === 'video' ? 'Agent 生成视频' : 'Agent 生成图片',
                        })
                      "
                    >
                      <video
                        v-if="media.kind === 'video'"
                        :src="media.url"
                        muted
                        preload="metadata"
                      />
                      <img v-else :src="media.url" alt="Agent 生成结果" />
                      <span class="media-open">查看作品 ↗</span>
                    </a>
                  </div>
                </div>
              </div>
            </div>

            <form class="composer" @submit.prevent="void sendChat()">
              <div class="composer-inner">
                <textarea
                  v-model="state.chatInput"
                  class="composer-input"
                  aria-label="创作需求"
                  :disabled="state.chatBusy"
                  placeholder="描述你的创意，例如：黄昏海边，一只柯基在追逐浪花……"
                  rows="2"
                  @keydown.enter.exact.prevent="void sendChat()"
                />
                <div class="composer-bottom">
                  <div class="composer-options">
                    <label class="option-chip" title="使用项目文档增强回答">
                      <input v-model="state.useRag" type="checkbox" />
                      <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M5 4.5h14v15H5zM8 8h8m-8 4h8m-8 4h5" />
                      </svg>
                      知识库
                    </label>
                    <label
                      class="option-chip"
                      title="使用当前模型进行视觉审查；低于 8/10 最多自动优化重试 2 次"
                    >
                      <input
                        v-model="state.qualityReviewEnabled"
                        type="checkbox"
                      />
                      <svg viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M2.8 12s3.2-6 9.2-6 9.2 6 9.2 6-3.2 6-9.2 6-9.2-6-9.2-6Z" />
                        <circle cx="12" cy="12" r="2.5" />
                      </svg>
                      视觉审查
                    </label>
                    <span class="composer-hint">Enter 发送 · Shift + Enter 换行</span>
                  </div>
                  <button
                    class="send-button"
                    type="submit"
                    :disabled="state.chatBusy || !state.chatInput.trim()"
                  >
                    <span>{{ state.chatBusy ? "正在创作" : "开始创作" }}</span>
                    <svg v-if="!state.chatBusy" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M5 12h13m-5-5 5 5-5 5" />
                    </svg>
                    <span v-else class="send-spinner" />
                  </button>
                </div>
              </div>
            </form>
          </section>

          <aside class="workspace-rail">
            <section class="rail-card backend-card">
              <div class="rail-card-heading">
                <span class="rail-icon purple-icon">✦</span>
                <div>
                  <strong>生成引擎</strong>
                  <small>选择创作执行后端</small>
                </div>
                <span
                  class="backend-live"
                  :class="`backend-live--${activeBackendStatusTone}`"
                >
                  <i />{{ activeBackendStatus }}
                </span>
              </div>
              <div class="backend-switch">
                <button
                  type="button"
                  :class="{ selected: state.generationBackend === 'comfyui' }"
                  @click="state.generationBackend = 'comfyui'"
                >
                  <span class="engine-icon">C</span>
                  <span><b>ComfyUI</b><small>本地工作流</small></span>
                  <span class="radio-dot" />
                </button>
                <button
                  type="button"
                  :class="{ selected: state.generationBackend === 'runninghub' }"
                  @click="state.generationBackend = 'runninghub'"
                >
                  <span class="engine-icon hub-icon">R</span>
                  <span><b>RunningHub</b><small>云端工作流</small></span>
                  <span class="radio-dot" />
                </button>
              </div>
              <p class="rail-hint">{{ activeBackendHint }}</p>
              <button
                v-if="state.generationBackend === 'runninghub'"
                class="text-action"
                type="button"
                @click="void togglePanel('runninghub')"
              >
                {{ state.runningHubConfig?.configured ? "管理工作流" : "配置 RunningHub" }}
                <span>→</span>
              </button>
            </section>

            <section class="rail-card model-card">
              <div class="rail-card-heading">
                <span class="rail-icon blue-icon">◉</span>
                <div>
                  <strong>创作模型</strong>
                  <small>Agent 理解与规划模型</small>
                </div>
              </div>
              <label class="model-select-label" for="model-provider">
                模型提供方
              </label>
              <select
                id="model-provider"
                :value="state.provider === 'custom' ? `custom:${state.selectedCustomId}` : state.provider"
                :disabled="state.chatBusy"
                @change="
                  (event) => {
                    const value = (event.target as HTMLSelectElement).value;
                    if (value.startsWith('custom:')) {
                      selectProvider('custom', Number(value.slice(7)));
                    } else {
                      selectProvider(value as Provider);
                    }
                  }
                "
              >
                <option value="openai">OpenAI 兼容模型</option>
                <option value="llama">llama.cpp 本地模型</option>
                <option
                  v-for="model in state.customModels"
                  :key="model.id"
                  :value="`custom:${model.id}`"
                >
                  {{ model.name }}
                </option>
              </select>
              <div class="model-card-footer">
                <span>密钥在服务端加密保存</span>
                <button
                  type="button"
                  @click="
                    state.customFormOpen = !state.customFormOpen;
                    state.customFormError = '';
                  "
                >
                  {{ state.customFormOpen ? "收起" : "+ 自定义模型" }}
                </button>
              </div>
              <form
                v-if="state.customFormOpen"
                class="custom-model-form"
                @submit.prevent="void saveCustomModel()"
              >
                <input
                  v-model="state.customForm.name"
                  aria-label="模型名称"
                  placeholder="名称（可选）"
                />
                <input
                  v-model="state.customForm.baseUrl"
                  aria-label="模型 Base URL"
                  placeholder="Base URL"
                />
                <input
                  v-model="state.customForm.model"
                  aria-label="模型名称标识"
                  placeholder="模型名"
                />
                <input
                  v-model="state.customForm.apiKey"
                  aria-label="模型 API Key"
                  type="password"
                  autocomplete="new-password"
                  placeholder="API Key"
                />
                <div v-if="state.customFormError" class="form-error">
                  {{ state.customFormError }}
                </div>
                <button
                  class="small-primary-button"
                  type="submit"
                  :disabled="state.customFormBusy"
                >
                  {{ state.customFormBusy ? "保存中…" : "保存模型" }}
                </button>
              </form>
              <div
                v-if="state.customModelsError"
                class="form-error model-load-error"
              >
                {{ state.customModelsError }}
              </div>
              <div
                v-if="state.customModels.length"
                class="saved-model-list"
              >
                <div
                  v-for="model in state.customModels"
                  :key="model.id"
                  class="saved-model-row"
                >
                  <button
                    type="button"
                    :class="{ chosen: state.selectedCustomId === model.id && state.provider === 'custom' }"
                    @click="selectProvider('custom', model.id)"
                  >
                    {{ model.name }}
                  </button>
                  <button
                    type="button"
                    :aria-label="`删除 ${model.name}`"
                    @click="void deleteCustomModel(model)"
                  >
                    ×
                  </button>
                </div>
              </div>
            </section>

            <section class="rail-card gallery-card">
              <div class="rail-card-heading gallery-heading">
                <span class="rail-icon mint-icon">▧</span>
                <div>
                  <strong>最近创作</strong>
                  <small>{{ state.gallery.length }} 个作品</small>
                </div>
                <button
                  class="icon-button subtle"
                  type="button"
                  aria-label="刷新作品"
                  @click="void loadGallery()"
                >
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M20 7v5h-5M4 17v-5h5M5.7 9A7 7 0 0 1 18 6.8M4 12l2 5.2A7 7 0 0 0 18.3 15" />
                  </svg>
                </button>
              </div>
              <div v-if="state.galleryError" class="gallery-state error-text">
                {{ state.galleryError }}
              </div>
              <div v-else-if="state.galleryLoading && !state.gallery.length" class="gallery-state">
                正在读取作品…
              </div>
              <div v-else-if="!state.gallery.length" class="gallery-state">
                <span class="empty-gallery-icon">✧</span>
                <span>完成第一次创作后<br />作品会展示在这里</span>
              </div>
              <div v-else class="mini-gallery">
                <button
                  v-for="(entry, index) in state.gallery.slice(0, 4)"
                  :key="`${entry.url}-${index}`"
                  class="mini-gallery-item"
                  type="button"
                  :aria-label="`预览：${entry.label}`"
                  @click="openLightbox(entry)"
                >
                  <video
                    v-if="entry.kind === 'video'"
                    :src="entry.url"
                    muted
                    preload="metadata"
                  />
                  <img v-else :src="entry.url" alt="" loading="lazy" />
                  <span v-if="entry.kind === 'video'" class="mini-video-badge">视频</span>
                </button>
              </div>
              <div v-if="state.galleryWarning" class="gallery-warning">
                {{ state.galleryWarning }}
              </div>
              <button
                v-if="state.gallery.length"
                class="text-action gallery-view-all"
                type="button"
                @click="state.activeView = 'library'"
              >
                查看资产库 <span>↗</span>
              </button>
            </section>

            <div class="rail-note">
              <span>✧</span>
              <p>每一次创作都可以继续对话，逐步调整你的画面。</p>
            </div>
          </aside>
        </div>
      </section>

      <KeepAlive>
        <DirectorView
          v-if="state.activeView === 'director'"
          :provider="state.provider"
          :selected-custom-id="state.selectedCustomId"
          :custom-models="state.customModels"
          @select-provider="selectProvider"
        />
      </KeepAlive>

      <KeepAlive>
        <VideoAgentView
          v-if="state.activeView === 'video'"
          :provider="state.provider"
          :selected-custom-id="state.selectedCustomId"
          :custom-models="state.customModels"
          :running-hub-config="state.runningHubConfig"
        />
      </KeepAlive>

      <KeepAlive>
        <AutoImageView
          v-if="state.activeView === 'images'"
          :provider="state.provider"
          :selected-custom-id="state.selectedCustomId"
          :custom-models="state.customModels"
          :running-hub-config="state.runningHubConfig"
        />
      </KeepAlive>

      <section v-if="state.activeView === 'roughcut'" class="creator-page">
        <div class="creator-heading">
          <div>
            <p class="creator-kicker">Agent 4</p>
            <h1>按集组接镜头视频。</h1>
            <p class="creator-intro">
              每集按分镜顺序校验镜头数、视频映射和可读性，再输出独立粗剪。
            </p>
          </div>
          <span class="creator-readiness is-pending">尚未接入</span>
        </div>
        <section class="creator-not-ready">
          <h2>粗剪任务尚未接入</h2>
          <p>
            当前版本还没有统一的镜头 ID / 视频资产映射和分集渲染任务。本页面不会把 Agent 3 的结果伪装成粗剪成片。
          </p>
          <ul>
            <li>逐集检查期望镜头数、镜头顺序和片段可读性</li>
            <li>缺片时标出集次与镜头 ID，不静默跳过</li>
            <li>完整集独立输出 MP4，并保留剪辑计划与源片段</li>
          </ul>
          <button
            class="button-secondary"
            type="button"
            @click="state.activeView = 'video'"
          >
            查看 Agent 3 视频任务
          </button>
        </section>
      </section>

      <section v-if="state.activeView === 'pipeline'" class="creator-page">
        <div class="creator-heading">
          <div>
            <p class="creator-kicker">Agent 5</p>
            <h1>从剧本开始，顺序交付每集粗剪。</h1>
            <p class="creator-intro">
              一键流程需要可恢复的总任务状态，并复用 Agent 1–4 的阶段产物。
            </p>
          </div>
          <span class="creator-readiness is-pending">尚未接入</span>
        </div>
        <section class="creator-not-ready">
          <h2>一键编排任务尚未接入</h2>
          <p>
            当前没有持久化的流水线任务、阶段快照与失败恢复接口，因此暂不提供会产生外部生成费用的假启动按钮。
          </p>
          <ol>
            <li>预检剧本、模型、Skills 和图片/视频工作流</li>
            <li>依次执行 Agent 1 → 2 → 3 → 4，前一阶段成功后再继续</li>
            <li>保留已完成产物，失败后从中断阶段恢复</li>
          </ol>
          <button
            class="button-secondary"
            type="button"
            @click="void openSettingsTab('comfyui')"
          >
            检查工作流配置
          </button>
        </section>
      </section>

      <section v-if="state.activeView === 'library'" class="creator-page">
        <div class="creator-heading">
          <div>
            <p class="creator-kicker">资产管理</p>
            <h1>个人资产库</h1>
            <p class="creator-intro">
              浏览当前工作区 API 已保存的图片与视频结果。
            </p>
          </div>
          <button
            class="button-secondary"
            type="button"
            :disabled="state.galleryLoading"
            @click="void loadGallery()"
          >
            {{ state.galleryLoading ? "刷新中…" : "刷新资产" }}
          </button>
        </div>

        <p class="creator-scope-note">
          当前数据源只包含已有图片和视频结果；统一分镜、逐镜头视频、粗剪资产及多账号隔离尚未接入。
        </p>
        <div class="creator-library-toolbar">
          <div class="creator-filter-group" role="group" aria-label="按资产类型筛选">
            <button
              type="button"
              :class="{ 'is-active': libraryKind === 'all' }"
              :aria-pressed="libraryKind === 'all'"
              @click="libraryKind = 'all'"
            >
              全部 <span>{{ state.gallery.length }}</span>
            </button>
            <button
              type="button"
              :class="{ 'is-active': libraryKind === 'image' }"
              :aria-pressed="libraryKind === 'image'"
              @click="libraryKind = 'image'"
            >
              图片 <span>{{ galleryImageCount }}</span>
            </button>
            <button
              type="button"
              :class="{ 'is-active': libraryKind === 'video' }"
              :aria-pressed="libraryKind === 'video'"
              @click="libraryKind = 'video'"
            >
              视频 <span>{{ galleryVideoCount }}</span>
            </button>
          </div>
          <label class="creator-search">
            <span class="visually-hidden">搜索资产名称</span>
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="8.5" cy="8.5" r="5.5" />
              <path d="m13 13 4 4" />
            </svg>
            <input
              v-model="librarySearch"
              type="search"
              placeholder="搜索当前结果"
            />
          </label>
        </div>

        <p v-if="state.galleryError" class="creator-library-state is-error">
          {{ state.galleryError }}
        </p>
        <p
          v-else-if="state.galleryLoading && !state.gallery.length"
          class="creator-library-state"
        >
          正在读取资产…
        </p>
        <div v-else-if="!filteredGallery.length" class="creator-library-empty">
          <strong>{{ state.gallery.length ? "没有匹配结果" : "还没有可显示的资产" }}</strong>
          <p>
            {{
              state.gallery.length
                ? "调整类型或搜索词后重试。"
                : "完成图片或视频生成后，已保存结果会显示在这里。"
            }}
          </p>
        </div>
        <div v-else class="creator-library-grid">
          <article
            v-for="entry in filteredGallery"
            :key="`${entry.kind}:${entry.url}`"
            class="creator-library-item"
          >
            <button
              class="creator-library-preview"
              type="button"
              :aria-label="`预览${entry.kind === 'video' ? '视频' : '图片'}：${entry.label}`"
              @click="openLightbox(entry)"
            >
              <video
                v-if="entry.kind === 'video'"
                :src="entry.url"
                muted
                preload="metadata"
              />
              <img v-else :src="entry.url" :alt="entry.label" loading="lazy" />
              <span>{{ entry.kind === "video" ? "视频" : "图片" }}</span>
            </button>
            <div class="creator-library-copy">
              <strong :title="entry.label">{{ entry.label }}</strong>
              <a :href="entry.url" :download="entry.label">下载</a>
            </div>
          </article>
        </div>
        <p v-if="state.galleryWarning" class="creator-scope-note">
          {{ state.galleryWarning }}
        </p>
      </section>

      <section v-if="state.activeView === 'settings'" class="creator-page">
        <div class="creator-heading">
          <div>
            <p class="creator-kicker">配置</p>
            <h1>工作流与模型连接</h1>
            <p class="creator-intro">
              使用你自己的 ComfyUI、RunningHub 和模型服务；网页不提供模型或代付服务。
            </p>
          </div>
        </div>
        <p class="creator-scope-note">
          当前配置仍属于本地单用户工作区。登录后的用户级隔离，以及 RunningHub 图片/视频独立工作流配置，尚未接入。
        </p>
        <nav class="creator-settings-tabs" aria-label="配置类别">
          <button
            type="button"
            :class="{ 'is-active': state.settingsTab === 'comfyui' }"
            :aria-current="state.settingsTab === 'comfyui' ? 'page' : undefined"
            @click="void openSettingsTab('comfyui')"
          >
            ComfyUI
          </button>
          <button
            type="button"
            :class="{ 'is-active': state.settingsTab === 'runninghub' }"
            :aria-current="state.settingsTab === 'runninghub' ? 'page' : undefined"
            @click="void openSettingsTab('runninghub')"
          >
            RunningHub
          </button>
          <button
            type="button"
            :class="{ 'is-active': state.settingsTab === 'models' }"
            :aria-current="state.settingsTab === 'models' ? 'page' : undefined"
            @click="void openSettingsTab('models')"
          >
            模型服务
          </button>
          <button
            type="button"
            :class="{ 'is-active': state.settingsTab === 'skills' }"
            :aria-current="state.settingsTab === 'skills' ? 'page' : undefined"
            @click="void openSettingsTab('skills')"
          >
            Skills
          </button>
        </nav>

        <div v-if="state.settingsTab === 'comfyui'" class="creator-settings-content">
          <section class="creator-settings-card">
            <div class="creator-section-heading">
              <div>
                <h2>ComfyUI 连接</h2>
                <p>{{ state.comfyUiStatus }}</p>
              </div>
              <a
                v-if="state.comfyUiUrl"
                class="button-secondary creator-open-link"
                :href="state.comfyUiUrl"
                target="_blank"
                rel="noopener noreferrer"
              >
                打开 ComfyUI
              </a>
            </div>
            <label for="comfyui-url">浏览器访问地址</label>
            <div class="creator-config-row">
              <input
                id="comfyui-url"
                v-model="state.comfyUiInput"
                type="url"
                placeholder="http://127.0.0.1:8188"
              />
              <button class="small-primary-button" type="button" @click="saveComfyUIUrl">
                保存地址
              </button>
              <button class="button-secondary" type="button" @click="void resetComfyUIUrl()">
                恢复默认
              </button>
            </div>
            <p class="creator-help-text">
              该地址只用于打开 ComfyUI 网页；生成 API 与 MCP 连接由服务端配置管理。
            </p>
            <div class="creator-settings-subsection">
              <div class="creator-section-heading">
                <div>
                  <h3>服务自检</h3>
                  <p>检查 API 与 ComfyUI 依赖，不会调用模型。</p>
                </div>
                <button
                  class="button-secondary"
                  type="button"
                  :disabled="state.diagnosticsBusy"
                  @click="void checkDiagnostics()"
                >
                  {{ state.diagnosticsBusy ? "检查中…" : "运行自检" }}
                </button>
              </div>
              <p v-if="state.diagnosticsError" class="creator-inline-error">
                自检失败：{{ state.diagnosticsError }}
              </p>
              <div v-if="state.diagnostics" class="creator-diagnostics">
                <div
                  v-for="(check, index) in state.diagnostics.checks"
                  :key="`${check.label}-${index}`"
                  class="creator-diagnostic-row"
                >
                  <span class="diagnostic-status" :class="`state-${check.status}`">
                    {{ check.status }}
                  </span>
                  <strong>{{ check.label }}</strong>
                  <span>{{ check.message }}</span>
                </div>
              </div>
            </div>
          </section>

          <section class="creator-settings-card">
            <div class="creator-section-heading">
              <div>
                <h2>可用工作流</h2>
                <p>{{ state.workflowStatus || "供 Agent 2 与 Agent 3 选择的 ComfyUI 工作流文件。" }}</p>
              </div>
              <button
                class="button-secondary"
                type="button"
                @click="void loadAvailableWorkflows()"
              >
                刷新
              </button>
            </div>
            <ul v-if="state.workflows.length" class="creator-workflow-list">
              <li v-for="workflow in state.workflows" :key="workflow.file">
                <span>{{ workflow.file }}</span>
                <span>ComfyUI</span>
              </li>
            </ul>
            <p v-else class="creator-empty-note">
              {{ state.workflowStatus || "尚未读取工作流列表。" }}
            </p>
          </section>
        </div>

        <div v-else-if="state.settingsTab === 'runninghub'" class="creator-settings-content">
          <section class="creator-settings-card">
            <div class="creator-section-heading">
              <div>
                <h2>RunningHub 连接</h2>
                <p>API Key 在服务端加密保存，读取时不会回传。</p>
              </div>
              <span
                class="creator-readiness"
                :class="state.runningHubConfig?.configured ? 'is-ready' : 'is-pending'"
              >
                {{ state.runningHubConfig?.configured ? "已配置" : "未配置" }}
              </span>
            </div>
            <p class="creator-scope-note">
              当前后端仅保存一套默认工作流映射，图片工作流和视频工作流尚不能分别配置。
            </p>
            <label for="runninghub-key">RunningHub API Key</label>
            <input
              id="runninghub-key"
              v-model="state.runningHubApiKey"
              type="password"
              autocomplete="new-password"
              placeholder="首次配置必填；留空保留已保存密钥"
            />
            <label for="runninghub-workflow">Workflow ID</label>
            <input
              id="runninghub-workflow"
              v-model="state.runningHubWorkflowId"
              placeholder="RunningHub 工作流 ID"
              @change="
                () => {
                  state.runningHubFields = [];
                  state.runningHubPromptMapping = '';
                  state.runningHubNegativeMapping = '';
                }
              "
            />
            <div class="creator-mapping-fields">
              <label for="runninghub-prompt">正向提示词输入</label>
              <select id="runninghub-prompt" v-model="state.runningHubPromptMapping">
                <option value="">选择正向提示词输入</option>
                <option
                  v-for="field in state.runningHubFields"
                  :key="runningHubMappingValue(field.node_id, field.field_name)"
                  :value="runningHubMappingValue(field.node_id, field.field_name)"
                >
                  {{ field.node_id }} · {{ field.node_title }} · {{ field.field_name }}
                </option>
              </select>
              <label for="runninghub-negative">负向提示词输入（可选）</label>
              <select
                id="runninghub-negative"
                v-model="state.runningHubNegativeMapping"
              >
                <option value="">不映射</option>
                <option
                  v-for="field in state.runningHubFields"
                  :key="runningHubMappingValue(field.node_id, field.field_name)"
                  :value="runningHubMappingValue(field.node_id, field.field_name)"
                >
                  {{ field.node_id }} · {{ field.node_title }} · {{ field.field_name }}
                </option>
              </select>
            </div>
            <div class="creator-config-actions">
              <button
                class="button-secondary"
                type="button"
                :disabled="state.runningHubBusy"
                @click="void discoverRunningHubFields()"
              >
                {{ state.runningHubBusy ? "读取中…" : "加载工作流输入" }}
              </button>
              <button
                class="small-primary-button"
                type="button"
                :disabled="state.runningHubBusy"
                @click="void saveRunningHubConfig()"
              >
                保存配置
              </button>
            </div>
            <p class="creator-config-status" aria-live="polite">
              {{ state.runningHubStatus }}
            </p>
          </section>
        </div>

        <div v-else-if="state.settingsTab === 'models'" class="creator-settings-content">
          <section class="creator-settings-card">
            <div class="creator-section-heading">
              <div>
                <h2>用户自有模型</h2>
                <p>只添加你可访问的 OpenAI 兼容模型服务，不提供模型目录或默认账号。</p>
              </div>
              <button
                class="button-secondary"
                type="button"
                @click="state.customFormOpen = !state.customFormOpen"
              >
                {{ state.customFormOpen ? "取消添加" : "添加模型" }}
              </button>
            </div>
            <form
              v-if="state.customFormOpen"
              class="creator-model-form"
              @submit.prevent="void saveCustomModel()"
            >
              <label>
                模型配置名称
                <input v-model="state.customForm.name" placeholder="名称（可选）" />
              </label>
              <label>
                Base URL
                <input
                  v-model="state.customForm.baseUrl"
                  type="url"
                  placeholder="https://api.example.com/v1"
                />
              </label>
              <label>
                模型标识
                <input v-model="state.customForm.model" placeholder="model-name" />
              </label>
              <label>
                API Key
                <input
                  v-model="state.customForm.apiKey"
                  type="password"
                  autocomplete="new-password"
                  placeholder="输入密钥"
                />
              </label>
              <p v-if="state.customFormError" class="creator-inline-error">
                {{ state.customFormError }}
              </p>
              <button
                class="small-primary-button"
                type="submit"
                :disabled="state.customFormBusy"
              >
                {{ state.customFormBusy ? "保存中…" : "保存模型" }}
              </button>
            </form>
            <p v-if="state.customModelsError" class="creator-inline-error">
              {{ state.customModelsError }}
            </p>
            <ul v-if="state.customModels.length" class="creator-model-list">
              <li v-for="model in state.customModels" :key="model.id">
                <span>
                  <strong>{{ model.name }}</strong>
                  <small>{{ model.model }} · {{ model.base_url }}</small>
                </span>
                <button
                  class="button-secondary"
                  type="button"
                  @click="void deleteCustomModel(model)"
                >
                  删除
                </button>
              </li>
            </ul>
            <p v-else-if="!state.customModelsError" class="creator-empty-note">
              尚未保存自定义模型。
            </p>
          </section>
        </div>

        <div v-else class="creator-settings-content">
          <section class="creator-settings-card creator-skills-card">
            <div>
              <h2>Skills 管理</h2>
              <p>
                当前 SKILL.md 导入、编辑和选择功能位于 Agent 1 分镜页面；在此不会复制一套不同的 Skill 数据。
              </p>
            </div>
            <button
              class="button-secondary"
              type="button"
              @click="state.activeView = 'director'"
            >
              打开 Agent 1 管理 Skills
            </button>
          </section>
        </div>
      </section>

      <footer class="main-footer">
        <span>AI STUDIO</span>
        <span>本地优先，过程可控。</span>
      </footer>
    </main>

    <div
      v-if="state.activePanel"
      class="drawer-backdrop"
      @click.self="state.activePanel = null"
    >
      <aside class="settings-drawer" aria-label="工作区设置">
        <header class="drawer-header">
          <div>
            <span class="drawer-eyebrow">工作区设置</span>
            <h2>
              {{ state.activePanel === "memory" ? "个人创作偏好" : "项目知识库" }}
            </h2>
          </div>
          <button
            class="icon-button"
            type="button"
            aria-label="关闭设置"
            @click="state.activePanel = null"
          >
            ×
          </button>
        </header>

        <div v-if="state.activePanel === 'memory'" class="drawer-content">
          <p class="drawer-description">
            保存常用画风、工作流和生成习惯，Agent 会把它们作为默认偏好。
          </p>
          <label for="memory-style">偏好画风</label>
          <textarea
            id="memory-style"
            v-model="state.memory.style"
            rows="2"
            placeholder="如：柔和水彩、温暖自然光"
          />
          <label for="memory-workflow">默认工作流</label>
          <input
            id="memory-workflow"
            v-model="state.memory.workflow"
            list="workflow-options"
            placeholder="选择或输入工作流文件名"
          />
          <datalist id="workflow-options">
            <option v-for="workflow in state.workflows" :key="workflow.file">
              {{ workflow.file }}
            </option>
          </datalist>
          <label for="memory-preset">默认模型预设</label>
          <input
            id="memory-preset"
            v-model="state.memory.model_preset"
            list="preset-options"
            placeholder="选择或输入预设名"
          />
          <datalist id="preset-options">
            <option
              v-for="preset in state.modelPresets"
              :key="preset.name"
              :value="preset.name"
              :label="preset.display_name"
            />
          </datalist>
          <div class="drawer-field-row">
            <div>
              <label for="memory-aspect">默认宽高比</label>
              <select id="memory-aspect" v-model="state.memory.aspect_ratio">
                <option value="">不指定</option>
                <option>1:1 (Square)</option>
                <option>2:3 (Portrait Photo)</option>
                <option>3:2 (Photo)</option>
                <option>3:4 (Portrait Standard)</option>
                <option>4:3 (Standard)</option>
                <option>9:16 (Portrait Widescreen)</option>
                <option>16:9 (Widescreen)</option>
                <option>21:9 (Ultrawide)</option>
              </select>
            </div>
            <div>
              <label for="memory-megapixels">默认百万像素</label>
              <input
                id="memory-megapixels"
                v-model="state.memory.megapixels"
                type="number"
                min="0.1"
                max="16"
                step="0.1"
                placeholder="不指定"
              />
            </div>
          </div>
          <label for="memory-negative">默认负向提示词</label>
          <textarea
            id="memory-negative"
            v-model="state.memory.negative_prompt"
            rows="2"
          />
          <label for="memory-notes">其他生成习惯</label>
          <textarea
            id="memory-notes"
            v-model="state.memory.notes"
            rows="3"
            maxlength="2000"
            placeholder="仅记录生成偏好，例如构图习惯或常用质量要求"
          />
          <div class="drawer-actions">
            <button
              class="button-secondary"
              type="button"
              :disabled="state.memoryBusy"
              @click="void clearAgentMemory()"
            >
              清除记忆
            </button>
            <button
              class="small-primary-button"
              type="button"
              :disabled="state.memoryBusy"
              @click="void saveAgentMemory()"
            >
              保存偏好
            </button>
          </div>
          <p class="drawer-status" aria-live="polite">{{ state.memoryStatus }}</p>
        </div>

        <div v-else class="drawer-content">
          <p class="drawer-description">
            管理用于 RAG 检索的项目文档。上传 Markdown 文档，或移除已添加的文档。
          </p>
          <input
            class="file-input-hidden"
            ref="ragFileInput"
            type="file"
            accept=".md"
            aria-label="上传 Markdown 文档"
            @change="void uploadRagDocument($event)"
          />
          <div class="drawer-actions">
            <button
              class="small-primary-button"
              type="button"
              :disabled="state.ragBusy"
              @click="ragFileInput?.click()"
            >
              + 上传 Markdown
            </button>
            <button
              class="button-secondary"
              type="button"
              :disabled="state.ragBusy"
              @click="void loadRagDocs()"
            >
              刷新
            </button>
          </div>
          <p class="drawer-status" aria-live="polite">{{ state.ragStatus }}</p>
          <div class="rag-document-list">
            <div
              v-for="doc in state.ragDocs"
              :key="doc.filename"
              class="rag-document"
            >
              <span class="rag-document-icon">MD</span>
              <span class="rag-document-name">
                {{ doc.filename }}
                <small>{{ doc.dynamic ? "自定义文档" : "内置文档" }}</small>
              </span>
              <button
                v-if="doc.dynamic"
                type="button"
                aria-label="删除文档"
                :disabled="state.ragBusy"
                @click="void deleteRagDocument(doc.filename)"
              >
                ×
              </button>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <div
      v-if="state.lightbox"
      id="lightbox"
      class="lightbox"
      role="presentation"
      @click="closeLightbox"
    >
      <button
        class="lightbox-close"
        type="button"
        aria-label="关闭预览"
        @click="closeLightbox"
      >
        ×
      </button>
      <video
        v-if="state.lightbox.kind === 'video'"
        :src="state.lightbox.url"
        autoplay
        controls
        muted
        playsinline
        @click.stop
        @play="($event.target as HTMLVideoElement).muted = false"
      />
      <img
        v-else
        :src="state.lightbox.url"
        :alt="state.lightbox.label"
        @click.stop
      />
    </div>

    <PandaAgent />
  </div>
</template>
