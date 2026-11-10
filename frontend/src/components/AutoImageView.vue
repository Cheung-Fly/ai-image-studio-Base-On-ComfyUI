<script setup lang="ts">
import {
  computed,
  defineAsyncComponent,
  onMounted,
  onUnmounted,
  ref,
  watch,
} from "vue";
import { apiFetch, apiJson, errorMessage } from "../api";
import { beginAgentActivity } from "../agentActivity";
import type {
  AutoImageAsset,
  AutoImageBatch,
  CustomModel,
  DirectorStoryboard,
  Provider,
  RunningHubConfig,
  WorkflowOption,
} from "../types";

const StoryboardRail = defineAsyncComponent(
  () => import("./StoryboardRail.vue"),
);

const props = defineProps<{
  provider: Provider;
  selectedCustomId: number | null;
  customModels: CustomModel[];
  runningHubConfig: RunningHubConfig | null;
}>();

const aspectOptions = [
  "16:9 (Widescreen)",
  "9:16 (Portrait Widescreen)",
  "1:1 (Square)",
  "4:3 (Standard)",
  "3:2 (Photo)",
  "2:3 (Portrait Photo)",
  "3:4 (Portrait Standard)",
  "21:9 (Ultrawide)",
];
const categoryOrder = ["character", "scene", "prop"];
const categoryLabels: Record<string, string> = {
  character: "角色",
  scene: "场景",
  prop: "道具",
};

const file = ref<File | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const batch = ref<AutoImageBatch | null>(null);
const storyboard = ref<DirectorStoryboard | null>(null);
const workflows = ref<WorkflowOption[]>([]);
const selectedEpisode = ref(1);
const selectedShotIndex = ref(0);
const workflowFile = ref("");
const generationBackend = ref<"comfyui" | "runninghub">("comfyui");
const aspectRatio = ref("3:2 (Photo)");
const megapixels = ref(1);
const busy = ref(false);
const retryingAssetId = ref<number | null>(null);
const expandedFailureId = ref<number | null>(null);
const loading = ref(false);
const storyboardLoading = ref(false);
const storyboardError = ref("");
const exportingSeries = ref(false);
const elapsedNow = ref(Date.now());
const error = ref("");
const notice = ref("");
let pollTimer: number | undefined;
let elapsedTimer: number | undefined;
let endGenerationActivity: (() => void) | null = null;
let storyboardRequestId = 0;

const runningHubSelection = computed(() => {
  const config = props.runningHubConfig;
  if (
    !config?.configured ||
    !config.workflow_id ||
    !config.prompt_node_id ||
    !config.prompt_field
  ) {
    return null;
  }
  return {
    workflow_id: config.workflow_id,
    prompt_node_id: config.prompt_node_id,
    prompt_field: config.prompt_field,
    negative_prompt_node_id: config.negative_prompt_node_id || null,
    negative_prompt_field: config.negative_prompt_field || null,
  };
});

const episodeAssets = computed(() =>
  (batch.value?.assets || []).filter((item) =>
    item.episode_indices.includes(selectedEpisode.value),
  ),
);
const groupedAssets = computed(() =>
  categoryOrder.map((category) => ({
    category,
    label: categoryLabels[category],
    assets: episodeAssets.value.filter((asset) => asset.category === category),
  })),
);
const completeCount = computed(
  () => episodeAssets.value.filter((asset) => asset.status === "completed" && asset.image_url).length,
);
const pendingCount = computed(
  () => episodeAssets.value.filter((asset) => ["pending", "processing"].includes(asset.status)).length,
);
const failedAssets = computed(
  () => episodeAssets.value.filter((asset) => asset.status === "failed"),
);
const pendingSeriesAssets = computed(
  () =>
    (batch.value?.assets || []).filter(
      (asset) => asset.status !== "completed" || !asset.image_url,
    ),
);
const canDownloadSeries = computed(
  () =>
    Boolean(batch.value?.episodes.length) &&
    !isPlanning.value &&
    pendingSeriesAssets.value.length === 0,
);
const selectedShot = computed(
  () => storyboard.value?.shots[selectedShotIndex.value] ?? null,
);
const canDownload = computed(
  () => episodeAssets.value.length > 0 && completeCount.value === episodeAssets.value.length,
);
const isRunning = computed(() => pendingCount.value > 0);
const isPlanning = computed(() => batch.value?.status === "planning");
const isProcessing = computed(() => isPlanning.value || isRunning.value);
const planningElapsedSeconds = computed(() => {
  if (!isPlanning.value || !batch.value) return 0;
  const startedAt =
    batch.value.logs.find((event) => event.event_type === "planning_queued")?.created_at ||
    batch.value.created_at;
  return Math.max(
    0,
    Math.floor((elapsedNow.value - parseUtcTimestamp(startedAt).getTime()) / 1000),
  );
});

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    draft: "未生成",
    pending: "等待生成",
    processing: "生成中",
    planning: "资产规划中",
    planning_failed: "规划失败",
    completed: "已完成",
    failed: "生成失败",
    cancelled: "已取消",
  };
  return labels[status] || status;
}

function formatElapsed(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
}

function formatLogTime(value: string): string {
  return parseUtcTimestamp(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function parseUtcTimestamp(value: string): Date {
  return new Date(/[zZ]|[+-]\d{2}:\d{2}$/.test(value) ? value : `${value}Z`);
}

function formatLogDuration(value: number | null | undefined): string {
  if (value == null) return "";
  return value < 1000 ? `${value} 毫秒` : `${(value / 1000).toFixed(1)} 秒`;
}

function selectFile(event: Event): void {
  const input = event.target as HTMLInputElement;
  file.value = input.files?.[0] || null;
  error.value = "";
  notice.value = "";
}

async function loadWorkflows(): Promise<void> {
  try {
    workflows.value = await apiJson<WorkflowOption[]>("/api/workflows");
    workflowFile.value = workflows.value[0]?.file || "";
  } catch (cause) {
    error.value = `读取 ComfyUI 工作流失败：${errorMessage(cause)}`;
  }
}

async function refreshBatch(batchId: number): Promise<void> {
  const updated = await apiJson<AutoImageBatch>(`/api/auto-image/batches/${batchId}`);
  if (batch.value?.id === batchId) {
    batch.value = updated;
  }
}

async function loadStoryboard(batchId: number, episodeIndex: number): Promise<void> {
  const requestId = ++storyboardRequestId;
  storyboard.value = null;
  selectedShotIndex.value = 0;
  storyboardError.value = "";
  storyboardLoading.value = true;
  try {
    const result = await apiJson<DirectorStoryboard>(
      `/api/auto-image/batches/${batchId}/episodes/${episodeIndex}/storyboard`,
      undefined,
      "读取分镜失败",
    );
    if (requestId === storyboardRequestId) storyboard.value = result;
  } catch (cause) {
    if (requestId === storyboardRequestId) {
      storyboardError.value = `读取本集分镜失败：${errorMessage(cause)}`;
    }
  } finally {
    if (requestId === storyboardRequestId) storyboardLoading.value = false;
  }
}

async function loadLatestBatch(): Promise<void> {
  loading.value = true;
  try {
    const batches = await apiJson<AutoImageBatch[]>("/api/auto-image/batches?limit=1");
    if (batches.length) {
      batch.value = batches[0];
      selectedEpisode.value = batches[0].selected_episode_index;
    }
  } catch (cause) {
    error.value = `读取资产任务失败：${errorMessage(cause)}`;
  } finally {
    loading.value = false;
  }
}

async function createPlan(): Promise<void> {
  if (!file.value) {
    error.value = "请先选择分镜 Excel 文件。";
    return;
  }
  if (props.provider === "custom" && !props.selectedCustomId) {
    error.value = "先在工作区选择一个可用的自定义模型。";
    return;
  }
  busy.value = true;
  const endActivity = beginAgentActivity();
  error.value = "";
  notice.value = "正在读取分集并整理跨集复用的角色、场景和道具…";
  const form = new FormData();
  form.set("file", file.value);
  form.set("episode_index", "1");
  form.set("provider", props.provider);
  if (props.provider === "custom" && props.selectedCustomId) {
    form.set("custom_config_id", String(props.selectedCustomId));
  }
  try {
    const created = await apiJson<AutoImageBatch>(
      "/api/auto-image/plan",
      { method: "POST", body: form },
      "资产清单生成失败",
    );
    batch.value = created;
    selectedEpisode.value = 1;
    if (created.status === "planning_failed") {
      error.value = "资产规划未能启动，请查看处理记录中的错误详情。";
      notice.value = "";
    } else {
      notice.value =
        created.status === "planning"
          ? `工作簿已解析，共 ${created.episode_count} 集；后台正在规划资产，可在处理记录中查看进度。`
          : `已整理 ${created.assets.length} 个跨集去重资产，共 ${created.episode_count} 集。`;
    }
    file.value = null;
    if (fileInput.value) fileInput.value.value = "";
  } catch (cause) {
    error.value = `资产清单生成失败：${errorMessage(cause)}`;
    notice.value = "";
  } finally {
    endActivity();
    busy.value = false;
  }
}

async function startEpisode(): Promise<void> {
  if (!batch.value) return;
  if (generationBackend.value === "runninghub" && !runningHubSelection.value) {
    error.value = "请先在「RunningHub 工作流」中配置可用的图片工作流。";
    return;
  }
  busy.value = true;
  error.value = "";
  notice.value = `正在提交第 ${selectedEpisode.value} 集的设定图任务…`;
  const request = generationRequest(selectedEpisode.value);
  try {
    batch.value = await apiJson<AutoImageBatch>(
      `/api/auto-image/batches/${batch.value.id}/start`,
      { method: "POST", body: JSON.stringify(request) },
      "启动设定图生成失败",
    );
    notice.value = `第 ${selectedEpisode.value} 集已进入生成队列。`;
  } catch (cause) {
    error.value = `启动设定图生成失败：${errorMessage(cause)}`;
    notice.value = "";
  } finally {
    busy.value = false;
  }
}

function generationRequest(episodeIndex: number): Record<string, unknown> {
  const request: Record<string, unknown> = {
    episode_index: episodeIndex,
    generation_backend: generationBackend.value,
    workflow_file: generationBackend.value === "comfyui" ? workflowFile.value : "",
    model_preset: "",
    aspect_ratio: aspectRatio.value,
    megapixels: megapixels.value,
    seed: -1,
    steps: 8,
    refine_steps: 4,
  };
  if (generationBackend.value === "runninghub") {
    request.runninghub = runningHubSelection.value;
  }
  return request;
}

async function retryAsset(asset: AutoImageAsset): Promise<void> {
  if (!batch.value || !asset.retry_available || busy.value) return;
  if (generationBackend.value === "runninghub" && !runningHubSelection.value) {
    error.value = "请先在「RunningHub 工作流」中配置可用的图片工作流。";
    return;
  }
  busy.value = true;
  retryingAssetId.value = asset.id;
  error.value = "";
  notice.value = "";
  try {
    const updated = await apiJson<AutoImageBatch>(
      `/api/auto-image/batches/${batch.value.id}/assets/${asset.id}/retry`,
      {
        method: "POST",
        body: JSON.stringify(generationRequest(selectedEpisode.value)),
      },
      "重新生成失败",
    );
    batch.value = updated;
    const retriedAsset = updated.assets.find((item) => item.id === asset.id);
    if (retriedAsset?.status === "failed") {
      expandedFailureId.value = asset.id;
      error.value = `重新生成“${asset.name}”未成功：${retriedAsset.error || "任务未能加入生成队列"}`;
    } else {
      notice.value = `已单独重新提交“${asset.name}”；其他已完成资产不会重跑。`;
    }
  } catch (cause) {
    expandedFailureId.value = asset.id;
    error.value = `重新生成“${asset.name}”失败：${errorMessage(cause)}`;
  } finally {
    retryingAssetId.value = null;
    busy.value = false;
  }
}

function toggleFailureDetails(assetId: number): void {
  expandedFailureId.value =
    expandedFailureId.value === assetId ? null : assetId;
}

async function downloadEpisode(): Promise<void> {
  if (!batch.value || !canDownload.value) return;
  await downloadPackage(
    `/api/auto-image/batches/${batch.value.id}/episodes/${selectedEpisode.value}.zip`,
    `${batch.value.title}-${String(selectedEpisode.value).padStart(2, "0")}.zip`,
    `第 ${selectedEpisode.value} 集素材包已下载，可交给自动视频栏目。`,
    "下载素材包失败",
  );
}

async function downloadSeriesPackage(): Promise<void> {
  if (!batch.value || !canDownloadSeries.value) return;
  exportingSeries.value = true;
  await downloadPackage(
    `/api/auto-image/batches/${batch.value.id}/series.zip`,
    `${batch.value.title}-Agent3.zip`,
    "整剧交付包已下载，解压后可将根文件夹导入 Agent3。",
    "下载整剧交付包失败",
  );
  exportingSeries.value = false;
}

async function downloadPackage(
  endpoint: string,
  filename: string,
  successMessage: string,
  errorPrefix: string,
): Promise<void> {
  error.value = "";
  try {
    const response = await apiFetch(endpoint);
    if (!response.ok) {
      const body = await response.text();
      let detail = "";
      try {
        const parsed = JSON.parse(body) as { detail?: string };
        detail = parsed.detail || "";
      } catch {
        detail = "";
      }
      throw new Error(detail || `下载失败（HTTP ${response.status}）`);
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice.value = successMessage;
  } catch (cause) {
    error.value = `${errorPrefix}：${errorMessage(cause)}`;
  }
}

watch(
  () =>
    [
      batch.value?.id ?? 0,
      selectedEpisode.value,
      batch.value?.status ?? "",
    ] as const,
  ([batchId, episodeIndex]) => {
    if (!batchId || isPlanning.value || batch.value?.status === "planning_failed") {
      storyboardRequestId += 1;
      storyboard.value = null;
      storyboardLoading.value = false;
      return;
    }
    void loadStoryboard(batchId, episodeIndex);
  },
  { immediate: true },
);

watch(
  () => `${batch.value?.id ?? ""}:${isProcessing.value}`,
  () => {
    if (pollTimer !== undefined) {
      window.clearInterval(pollTimer);
      pollTimer = undefined;
    }
    if (!batch.value || !isProcessing.value) return;
    const batchId = batch.value.id;
    pollTimer = window.setInterval(() => {
      void refreshBatch(batchId).catch((cause: unknown) => {
        error.value = `刷新生成状态失败：${errorMessage(cause)}`;
      });
    }, 3000);
  },
);

watch(
  isPlanning,
  (planning) => {
    if (elapsedTimer !== undefined) {
      window.clearInterval(elapsedTimer);
      elapsedTimer = undefined;
    }
    if (!planning) return;
    elapsedNow.value = Date.now();
    elapsedTimer = window.setInterval(() => {
      elapsedNow.value = Date.now();
    }, 1000);
  },
);

watch(
  isProcessing,
  (running) => {
    if (running && !endGenerationActivity) {
      endGenerationActivity = beginAgentActivity();
    } else if (!running && endGenerationActivity) {
      endGenerationActivity();
      endGenerationActivity = null;
    }
  },
  { immediate: true },
);

onMounted(() => {
  void loadWorkflows();
  void loadLatestBatch();
});

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer);
  if (elapsedTimer !== undefined) window.clearInterval(elapsedTimer);
  endGenerationActivity?.();
  endGenerationActivity = null;
});
</script>

<template>
  <section class="auto-image-page">
    <header class="auto-image-heading">
      <div>
        <p class="auto-image-kicker">分镜资产 / 设定图</p>
        <h1>先定下整部剧的视觉资产。</h1>
        <p>
          导入整剧分镜，跨集合并重复角色、场景与道具；先跑一集，确认设定图后再交给自动视频。
        </p>
      </div>
      <div class="asset-stamp" aria-hidden="true">
        <span>美术资产</span>
        <strong>{{ isPlanning ? "…" : batch?.assets.length ?? "—" }}</strong>
        <small>跨集去重</small>
      </div>
    </header>

    <div v-if="error" class="auto-image-alert" role="alert">
      <span>{{ error }}</span>
      <button type="button" aria-label="关闭错误提示" @click="error = ''">×</button>
    </div>
    <div v-if="notice" class="auto-image-notice" role="status">{{ notice }}</div>

    <div class="auto-image-layout">
      <aside class="asset-setup">
        <section class="setup-section">
          <div class="setup-title">
            <span class="setup-number">输入</span>
            <h2>整剧分镜表</h2>
          </div>
          <label class="workbook-drop">
            <input
              ref="fileInput"
              type="file"
              accept=".xlsx"
              :disabled="busy"
              @change="selectFile"
            />
            <span class="file-mark" aria-hidden="true">XLSX</span>
            <strong>{{ file?.name || "选择 Excel 工作簿" }}</strong>
            <small>支持多工作表分集分镜 · 最大 50 MB</small>
          </label>
          <button
            class="asset-button primary"
            type="button"
            :disabled="busy || !file"
            @click="void createPlan()"
          >
            {{ busy && !batch ? "正在整理资产…" : busy ? "处理中…" : "生成全剧资产清单" }}
          </button>
          <p class="setup-note">
            只分析并编目资产，不会自动启动生图。每个唯一实体只生成一次，关联集数会保留。
          </p>
        </section>

        <section class="setup-section generation-settings">
          <div class="setup-title">
            <span class="setup-number">生成</span>
            <h2>本次试跑设置</h2>
          </div>
          <label class="asset-field">
            <span>生成服务</span>
            <select v-model="generationBackend" :disabled="busy || isRunning">
              <option value="comfyui">ComfyUI</option>
              <option value="runninghub" :disabled="!runningHubSelection">RunningHub</option>
            </select>
          </label>
          <div v-if="generationBackend === 'comfyui'" class="asset-field">
            <label for="auto-image-workflow">图片工作流</label>
            <select
              id="auto-image-workflow"
              v-model="workflowFile"
              :disabled="busy || isRunning"
            >
              <option value="">使用服务默认工作流</option>
              <option v-for="workflow in workflows" :key="workflow.file" :value="workflow.file">
                {{ workflow.file }}
              </option>
            </select>
          </div>
          <div v-else class="workflow-readout">
            <strong>{{ runningHubSelection?.workflow_id || "尚未配置" }}</strong>
            <span>
              {{
                runningHubSelection
                  ? `提示词输入：${runningHubSelection.prompt_node_id} / ${runningHubSelection.prompt_field}`
                  : "请先从左侧「RunningHub 工作流」完成配置"
              }}
            </span>
          </div>
          <label class="asset-field">
            <span>画面比例</span>
            <select v-model="aspectRatio" :disabled="busy || isRunning">
              <option v-for="option in aspectOptions" :key="option" :value="option">
                {{ option }}
              </option>
            </select>
          </label>
          <label class="asset-field">
            <span>目标百万像素</span>
            <input
              v-model.number="megapixels"
              type="number"
              min="0.1"
              max="16"
              step="0.1"
              :disabled="busy || isRunning"
            />
          </label>
          <p class="setup-note">
            生成模型：{{ provider === "custom" ? customModels.find((item) => item.id === selectedCustomId)?.name || "未选择" : provider === "llama" ? "llama.cpp 本地模型" : "OpenAI 兼容模型" }}
          </p>
        </section>
      </aside>

      <main class="asset-inventory">
        <template v-if="batch">
          <header class="inventory-header">
            <div>
              <p class="inventory-source">{{ batch.source_filename }} · {{ batch.episode_count }} 集</p>
              <h2>{{ batch.title }}</h2>
              <p>
                {{
                  isPlanning
                    ? "正在分析分镜并整理跨集复用资产；处理记录会持续更新。"
                    : "资产总表会跨集复用；以下只展示当前试跑集需要的设定图。"
                }}
              </p>
            </div>
            <div class="inventory-progress">
              <strong>
                {{ isPlanning ? "…" : completeCount }}
                <small v-if="!isPlanning"> / {{ episodeAssets.length }}</small>
              </strong>
              <span>{{ isPlanning ? statusLabel("planning") : "本集已完成" }}</span>
            </div>
          </header>

          <div
            v-if="isPlanning"
            class="planning-status"
            role="status"
            aria-live="off"
          >
            <span class="planning-indicator" aria-hidden="true" />
            <span>
              资产规划仍在运行 · 已等待 {{ formatElapsed(planningElapsedSeconds) }} ·
              单次模型请求超时上限为 300 秒
            </span>
          </div>
          <div v-else-if="batch.status === 'planning_failed'" class="planning-failure" role="alert">
            资产清单未生成。请查看下方处理记录中的错误详情。
          </div>

          <section v-if="batch.logs.length" class="process-log">
            <details :open="isPlanning || batch.status === 'planning_failed'">
              <summary>
                <span>处理记录</span>
                <small>
                  {{ batch.logs.length }} 条 ·
                  {{ isProcessing ? "自动刷新中" : "已保存" }}
                </small>
              </summary>
              <ol>
                <li
                  v-for="event in batch.logs"
                  :key="event.id"
                  class="process-log-entry"
                  :class="`process-log-${event.level}`"
                >
                  <time :datetime="event.created_at">{{ formatLogTime(event.created_at) }}</time>
                  <p>{{ event.message }}</p>
                  <small v-if="event.duration_ms != null">
                    用时 {{ formatLogDuration(event.duration_ms) }}
                  </small>
                </li>
              </ol>
            </details>
          </section>

          <div v-if="isPlanning" class="asset-empty planning-empty">
            <span class="planning-indicator" aria-hidden="true" />
            正在分析分镜内容并规划跨集角色、场景与道具…
          </div>
          <div v-else-if="batch.status !== 'planning_failed'" class="episode-picker">
            <label for="auto-image-episode">本次制作分集</label>
            <select
              id="auto-image-episode"
              v-model.number="selectedEpisode"
              :disabled="busy || isRunning"
            >
              <option
                v-for="item in batch.episodes"
                :key="item.episode_index"
                :value="item.episode_index"
              >
                第 {{ item.episode_index }} 集 · {{ item.title }}（{{ item.shot_count }} 镜）
              </option>
            </select>
            <button
              class="asset-button primary"
              type="button"
              :disabled="busy || isRunning || !episodeAssets.length || (generationBackend === 'runninghub' && !runningHubSelection)"
              @click="void startEpisode()"
            >
              {{ isRunning ? "设定图生成中…" : `生成第 ${selectedEpisode} 集设定图` }}
            </button>
            <button
              class="asset-button secondary"
              type="button"
              :disabled="!canDownload"
              @click="void downloadEpisode()"
            >
              下载本集素材包
            </button>
            <button
              class="asset-button secondary"
              type="button"
              :disabled="!canDownloadSeries || exportingSeries || busy"
              aria-describedby="series-package-status"
              @click="void downloadSeriesPackage()"
            >
              {{ exportingSeries ? "正在整理整剧…" : "下载 Agent3 整剧包" }}
            </button>
            <p
              id="series-package-status"
              class="series-package-status"
              :class="{ blocked: !canDownloadSeries }"
              role="status"
            >
              {{
                canDownloadSeries
                  ? "按分集顺序包含分镜、角色、场景和道具设定图。"
                  : `全剧还有 ${pendingSeriesAssets.length} 项设定图未完成。`
              }}
            </p>
            <p v-if="failedAssets.length" class="asset-package-blocked" role="status">
              本集有 {{ failedAssets.length }} 项设定图失败。全部图片完成前不会生成素材包；
              请查看失败项详情并单独重新生成。
            </p>
          </div>

          <section
            v-if="storyboardLoading || storyboard || storyboardError"
            class="episode-script"
            aria-label="本集分镜脚本"
          >
            <p v-if="storyboardLoading" class="script-loading" role="status">
              正在读取第 {{ selectedEpisode }} 集分镜…
            </p>
            <p v-else-if="storyboardError" class="script-error" role="alert">
              {{ storyboardError }}
              <button
                v-if="batch"
                type="button"
                @click="void loadStoryboard(batch.id, selectedEpisode)"
              >
                重新读取分镜
              </button>
            </p>
            <template v-else-if="storyboard">
              <StoryboardRail
                :shots="storyboard.shots"
                :selected-index="selectedShotIndex"
                @select="selectedShotIndex = $event"
              />
              <div v-if="selectedShot" class="shot-detail">
                <article class="shot-copy">
                  <p class="shot-position">
                    第 {{ selectedEpisode }} 集 · 镜号 {{ selectedShot.shot_no }}
                  </p>
                  <h3>{{ selectedShot.visual_description }}</h3>
                  <dl>
                    <div>
                      <dt>景别</dt>
                      <dd>{{ selectedShot.framing || "未填写" }}</dd>
                    </div>
                    <div>
                      <dt>运镜</dt>
                      <dd>{{ selectedShot.camera_movement || "未填写" }}</dd>
                    </div>
                    <div>
                      <dt>时长</dt>
                      <dd>{{ selectedShot.duration_seconds }} 秒</dd>
                    </div>
                  </dl>
                  <p v-if="selectedShot.dialogue" class="shot-note">
                    <strong>对白</strong>{{ selectedShot.dialogue }}
                  </p>
                  <p v-if="selectedShot.prompt" class="shot-note">
                    <strong>生成提示词</strong>{{ selectedShot.prompt }}
                  </p>
                  <p v-if="selectedShot.sound_effects" class="shot-note">
                    <strong>音效</strong>{{ selectedShot.sound_effects }}
                  </p>
                </article>
                <aside class="shared-assets" aria-label="本集共享设定图">
                  <h4>本集共享设定图</h4>
                  <p>Agent3 导入后可在镜头间复用这些资产。</p>
                  <ul v-if="episodeAssets.length">
                    <li v-for="asset in episodeAssets" :key="asset.id">
                      <img
                        v-if="asset.image_url"
                        :src="asset.image_url"
                        :alt="`${asset.name}设定图`"
                        loading="lazy"
                      />
                      <span v-else class="shared-asset-empty">
                        {{ categoryLabels[asset.category].slice(0, 1) }}
                      </span>
                      <span class="shared-asset-copy">
                        <strong>{{ asset.name }}</strong>
                        <small>
                          {{ categoryLabels[asset.category] }} · {{ statusLabel(asset.status) }}
                        </small>
                      </span>
                    </li>
                  </ul>
                  <p v-else class="shared-assets-empty">
                    本集没有需要交付的角色、场景或道具设定图。
                  </p>
                </aside>
              </div>
            </template>
          </section>

          <div v-if="!isPlanning && batch.status !== 'planning_failed' && !episodeAssets.length" class="asset-empty">
            当前分集没有从分镜中识别出角色、场景或道具。
          </div>
          <section
            v-if="!isPlanning && batch.status !== 'planning_failed'"
            v-for="group in groupedAssets"
            :key="group.category"
            class="asset-group"
          >
            <header v-if="group.assets.length" class="asset-group-heading">
              <h3>{{ group.label }}</h3>
              <span>{{ group.assets.length }} 项</span>
            </header>
            <article
              v-for="asset in group.assets"
              :key="asset.id"
              class="asset-row"
            >
              <div class="asset-preview">
                <img v-if="asset.image_url" :src="asset.image_url" :alt="`${asset.name}设定图`" loading="lazy" />
                <span v-else class="preview-placeholder" aria-hidden="true">
                  {{ group.label.slice(0, 1) }}
                </span>
              </div>
              <div class="asset-copy">
                <h4>{{ asset.name }}</h4>
                <p>{{ asset.prompt }}</p>
                <small>关联第 {{ asset.episode_indices.join("、") }} 集</small>
              </div>
              <div class="asset-state" :class="`asset-state-${asset.status}`">
                <span class="asset-state-label">
                  <span class="state-dot" />
                  {{ statusLabel(asset.status) }}
                </span>
                <button
                  v-if="asset.status === 'failed'"
                  class="asset-detail-toggle"
                  type="button"
                  :aria-expanded="expandedFailureId === asset.id"
                  :aria-controls="`asset-failure-${asset.id}`"
                  @click="toggleFailureDetails(asset.id)"
                >
                  {{ expandedFailureId === asset.id ? "收起详情" : "查看详情" }}
                </button>
              </div>
              <section
                v-if="asset.status === 'failed'"
                :id="`asset-failure-${asset.id}`"
                v-show="expandedFailureId === asset.id"
                class="asset-failure-details"
                role="region"
                :aria-label="`${asset.name}生成失败详情`"
              >
                <p v-if="asset.error_code" class="asset-failure-code">
                  错误代码：{{ asset.error_code }}
                </p>
                <p class="asset-failure-message">
                  {{ asset.error || "服务未返回具体错误信息，请检查生成服务后重试。" }}
                </p>
                <div class="asset-failure-actions">
                  <button
                    v-if="asset.retry_available"
                    class="asset-button primary"
                    type="button"
                    :disabled="busy || retryingAssetId === asset.id"
                    @click="void retryAsset(asset)"
                  >
                    {{ retryingAssetId === asset.id ? "重新提交中…" : "重新生成" }}
                  </button>
                  <p v-else class="asset-retry-limit">
                    已达到最多 3 次手动重试。调整生成服务或工作流后，再重新启动本集任务。
                  </p>
                  <small>只重试这项素材，已完成的设定图会保留。</small>
                </div>
              </section>
            </article>
          </section>
        </template>
        <div v-else class="asset-empty landing-empty">
          <div class="empty-swatch" aria-hidden="true"><span /><span /><span /></div>
          <h2>{{ loading ? "正在读取最近任务…" : "从一份整剧分镜开始" }}</h2>
          <p>先生成资产清单，再选择一集试跑。生成完成后，可下载分镜与设定图素材包。</p>
        </div>
      </main>
    </div>
  </section>
</template>

<style scoped>
.auto-image-page {
  --pine: #263a38;
  --moss: #526a60;
  --paper: #f7f8f3;
  --leaf: #e8eee8;
  --rust: #b64e3d;
  --line: #d7dfd8;
  --muted: #748078;
  max-width: 1540px;
  margin: 0 auto;
  padding: 46px clamp(22px, 4vw, 64px) 72px;
  color: var(--pine);
}

.auto-image-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 30px;
  padding: 0 0 30px;
  border-bottom: 1px solid var(--line);
}

.auto-image-kicker,
.inventory-source {
  margin: 0 0 10px;
  color: var(--moss);
  font-size: 12px;
  font-weight: 700;
}

.auto-image-heading h1 {
  margin: 0;
  font-size: clamp(30px, 4vw, 48px);
  font-weight: 650;
  letter-spacing: -0.045em;
  line-height: 1.14;
}

.auto-image-heading > div:first-child > p:last-child {
  max-width: 650px;
  margin: 15px 0 0;
  color: #64716a;
  font-size: 15px;
  line-height: 1.75;
}

.asset-stamp {
  display: grid;
  align-content: center;
  min-width: 150px;
  min-height: 130px;
  padding: 18px 22px;
  color: var(--paper);
  background: var(--pine);
  border-radius: 2px 22px 2px 2px;
}

.asset-stamp span,
.asset-stamp small {
  color: #cad7ce;
  font-size: 11px;
}

.asset-stamp strong {
  margin: 2px 0;
  font-size: 44px;
  font-weight: 550;
  line-height: 1;
}

.auto-image-alert,
.auto-image-notice {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  margin-top: 18px;
  padding: 12px 16px;
  border-radius: 8px;
}

.auto-image-alert {
  color: #81372e;
  background: #fae9e5;
}

.auto-image-alert button {
  border: 0;
  color: inherit;
  background: transparent;
  font-size: 20px;
}

.auto-image-notice {
  color: #315b4a;
  background: var(--leaf);
}

.auto-image-layout {
  display: grid;
  grid-template-columns: minmax(270px, 330px) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
  padding-top: 24px;
}

.asset-setup,
.asset-inventory {
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 12px;
}

.asset-setup {
  overflow: hidden;
}

.setup-section {
  padding: 23px;
}

.setup-section + .setup-section {
  border-top: 1px solid var(--line);
}

.setup-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 18px;
}

.setup-number {
  color: var(--rust);
  font-family: Georgia, serif;
  font-size: 14px;
  font-style: italic;
}

.setup-title h2 {
  margin: 0;
  font-size: 17px;
  font-weight: 650;
}

.workbook-drop {
  display: grid;
  justify-items: start;
  gap: 8px;
  padding: 17px;
  border: 1px dashed #9cab9e;
  border-radius: 9px;
  background: #f0f3ed;
  cursor: pointer;
}

.workbook-drop:focus-within {
  outline: 3px solid rgba(38, 58, 56, 0.18);
  outline-offset: 2px;
}

.workbook-drop input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.file-mark {
  display: grid;
  place-items: center;
  width: 41px;
  height: 42px;
  color: var(--paper);
  background: var(--moss);
  border-radius: 5px 5px 12px 5px;
  font-size: 10px;
  font-weight: 800;
}

.workbook-drop strong {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

.workbook-drop small,
.setup-note {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.6;
}

.asset-button {
  width: 100%;
  min-height: 42px;
  margin-top: 13px;
  padding: 10px 14px;
  border: 1px solid var(--pine);
  border-radius: 7px;
  font-weight: 650;
}

.asset-button.primary {
  color: white;
  background: var(--pine);
}

.asset-button.secondary {
  color: var(--pine);
  background: transparent;
}

.asset-button:disabled {
  cursor: not-allowed;
  opacity: 0.48;
}

.setup-note {
  margin: 12px 0 0;
}

.asset-field {
  display: grid;
  gap: 6px;
  margin: 12px 0;
  color: #5e6b63;
  font-size: 12px;
}

.asset-field select,
.asset-field input,
.episode-picker select {
  width: 100%;
  min-height: 39px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--pine);
  background: white;
}

.workflow-readout {
  display: grid;
  gap: 4px;
  margin: 12px 0;
  padding: 11px 12px;
  background: var(--leaf);
  border-radius: 6px;
  overflow-wrap: anywhere;
}

.workflow-readout strong {
  font-size: 12px;
}

.workflow-readout span {
  color: var(--muted);
  font-size: 11px;
}

.asset-inventory {
  min-height: 480px;
  padding: clamp(20px, 3vw, 32px);
}

.inventory-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 20px;
}

.inventory-header h2 {
  margin: 0;
  font-size: 25px;
  font-weight: 650;
  letter-spacing: -0.025em;
}

.inventory-header p:last-child {
  margin: 8px 0 0;
  color: var(--muted);
  font-size: 12px;
}

.inventory-progress {
  display: grid;
  gap: 2px;
  min-width: 104px;
  text-align: right;
}

.inventory-progress strong {
  font-size: 29px;
  font-weight: 600;
}

.inventory-progress strong small {
  color: #8c9890;
  font-size: 15px;
}

.inventory-progress span {
  color: var(--muted);
  font-size: 11px;
}

.planning-status,
.planning-failure {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 20px 0 0;
  padding: 12px 14px;
  border-left: 3px solid var(--moss);
  color: var(--pine);
  background: var(--leaf);
  font-size: 12px;
  line-height: 1.5;
}

.planning-failure {
  border-left-color: var(--rust);
  color: #81372e;
  background: #fae9e5;
}

.planning-indicator {
  flex: 0 0 auto;
  width: 9px;
  height: 9px;
  border: 2px solid #aebdb1;
  border-top-color: var(--pine);
  border-radius: 50%;
  animation: planning-spin 1s linear infinite;
}

.planning-failure .planning-indicator {
  display: none;
}

.process-log {
  margin: 18px 0 22px;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}

.process-log summary {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 2px;
  color: var(--pine);
  cursor: pointer;
  font-size: 13px;
  font-weight: 650;
}

.process-log summary small {
  color: var(--muted);
  font-size: 10px;
  font-weight: 400;
}

.process-log ol {
  display: grid;
  gap: 0;
  max-height: 280px;
  margin: 0;
  padding: 0 4px 8px 0;
  overflow: auto;
  list-style: none;
}

.process-log-entry {
  display: grid;
  grid-template-columns: 58px minmax(0, 1fr);
  column-gap: 12px;
  padding: 9px 0;
  border-top: 1px solid #e6ebe5;
  font-size: 11px;
}

.process-log-entry time {
  padding-top: 1px;
  color: #89958d;
  font-variant-numeric: tabular-nums;
}

.process-log-entry p {
  margin: 0;
  color: #52645a;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.process-log-entry > small {
  grid-column: 2;
  margin-top: 3px;
  color: #89958d;
  font-size: 10px;
}

.process-log-error p {
  color: #9c3e32;
}

.planning-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  min-height: 190px;
  padding: 24px;
  color: var(--moss);
  line-height: 1.6;
}

@keyframes planning-spin {
  to { transform: rotate(360deg); }
}

.episode-picker {
  display: grid;
  grid-template-columns:
    auto
    minmax(150px, 1fr)
    repeat(3, minmax(135px, auto));
  align-items: center;
  gap: 10px;
  margin: 24px 0 26px;
  padding: 13px;
  background: var(--leaf);
  border-radius: 8px;
}

.episode-picker > label {
  padding: 0 4px;
  font-size: 12px;
  font-weight: 650;
}

.episode-picker .asset-button {
  width: auto;
  margin: 0;
}

.series-package-status {
  grid-column: 1 / -1;
  margin: 0;
  color: #526a60;
  font-size: 11px;
  line-height: 1.5;
}

.series-package-status.blocked {
  color: #81372e;
}

.episode-script {
  margin: 0 0 28px;
}

.script-loading,
.script-error {
  margin: 0 0 14px;
  padding: 12px 14px;
  font-size: 13px;
}

.script-loading {
  color: #526a60;
  background: #e8eee8;
}

.script-error {
  color: #81372e;
  background: #fae9e5;
}

.script-error button {
  margin-left: 10px;
  padding: 3px 8px;
  border: 1px solid currentColor;
  color: inherit;
  background: transparent;
  cursor: pointer;
}

.script-error button:focus-visible {
  outline: 3px solid #263a38;
  outline-offset: 2px;
}

.shot-detail {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(220px, 290px);
  gap: 20px;
  padding: 20px;
  border: 1px solid var(--line);
  background: #fff;
}

.shot-copy {
  min-width: 0;
}

.shot-position {
  margin: 0 0 8px;
  color: var(--moss);
  font-size: 12px;
  font-weight: 650;
}

.shot-copy h3 {
  max-width: 70ch;
  margin: 0;
  color: var(--pine);
  font-size: clamp(18px, 2vw, 24px);
  font-weight: 620;
  line-height: 1.4;
}

.shot-copy dl {
  display: flex;
  flex-wrap: wrap;
  gap: 18px 34px;
  margin: 18px 0;
  padding: 12px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}

.shot-copy dl div {
  min-width: 72px;
}

.shot-copy dt {
  color: var(--muted);
  font-size: 11px;
}

.shot-copy dd {
  margin: 3px 0 0;
  color: var(--pine);
  font-size: 13px;
}

.shot-note {
  margin: 10px 0 0;
  color: #45554d;
  font-size: 13px;
  line-height: 1.65;
  white-space: pre-wrap;
}

.shot-note strong {
  display: block;
  margin-bottom: 3px;
  color: #526a60;
  font-size: 11px;
}

.shared-assets {
  padding-left: 18px;
  border-left: 1px solid var(--line);
}

.shared-assets h4,
.shared-assets > p {
  margin: 0;
}

.shared-assets h4 {
  font-size: 13px;
}

.shared-assets > p {
  margin-top: 5px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.5;
}

.shared-assets ul {
  display: grid;
  gap: 9px;
  max-height: 290px;
  margin: 13px 0 0;
  padding: 0 4px 0 0;
  overflow: auto;
  list-style: none;
}

.shared-assets li {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.shared-assets li img,
.shared-asset-empty {
  width: 48px;
  height: 38px;
  flex: 0 0 auto;
  object-fit: cover;
  background: var(--leaf);
}

.shared-asset-empty {
  display: grid;
  place-items: center;
  color: var(--moss);
  font-size: 14px;
  font-weight: 700;
}

.shared-asset-copy {
  display: grid;
  min-width: 0;
  gap: 3px;
}

.shared-asset-copy strong {
  overflow: hidden;
  color: var(--pine);
  font-size: 11px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.shared-asset-copy small {
  color: var(--muted);
  font-size: 10px;
}

.shared-assets .shared-assets-empty {
  margin-top: 12px;
  color: #526a60;
}

.asset-package-blocked {
  grid-column: 1 / -1;
  margin: 0;
  padding: 9px 11px;
  border-left: 3px solid var(--rust);
  color: #81372e;
  background: #fae9e5;
  font-size: 11px;
  line-height: 1.55;
}

.asset-group + .asset-group {
  margin-top: 27px;
}

.asset-group-heading {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
}

.asset-group-heading h3 {
  margin: 0;
  font-size: 14px;
}

.asset-group-heading span {
  color: var(--muted);
  font-size: 11px;
}

.asset-row {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr) 100px;
  align-items: center;
  gap: 16px;
  padding: 13px 0;
  border-bottom: 1px solid #e6ebe5;
}

.asset-preview {
  width: 90px;
  aspect-ratio: 4 / 3;
  overflow: hidden;
  background: #e9eee8;
  border-radius: 6px;
}

.asset-preview img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.preview-placeholder {
  display: grid;
  place-items: center;
  width: 100%;
  height: 100%;
  color: var(--moss);
  font-family: Georgia, serif;
  font-size: 30px;
}

.asset-copy {
  min-width: 0;
}

.asset-copy h4 {
  margin: 0;
  font-size: 14px;
}

.asset-copy p {
  display: -webkit-box;
  max-width: 760px;
  margin: 5px 0;
  overflow: hidden;
  color: #617067;
  font-size: 11px;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.asset-copy small {
  color: #89958d;
  font-size: 10px;
}

.asset-state {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: center;
  gap: 5px;
  color: #66746b;
  font-size: 11px;
  text-align: right;
}

.asset-state-label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.asset-detail-toggle {
  padding: 1px 0;
  border: 0;
  color: var(--moss);
  background: transparent;
  font-size: 11px;
  font-weight: 650;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.asset-detail-toggle:hover {
  color: var(--pine);
}

.asset-failure-details {
  grid-column: 2 / -1;
  display: grid;
  gap: 7px;
  padding: 12px 14px;
  border-left: 3px solid var(--rust);
  color: #81372e;
  background: #fae9e5;
  font-size: 12px;
}

.asset-failure-code,
.asset-failure-message {
  margin: 0;
  overflow-wrap: anywhere;
}

.asset-failure-code {
  font-weight: 700;
}

.asset-failure-message {
  white-space: pre-wrap;
  line-height: 1.6;
}

.asset-failure-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.asset-failure-actions .asset-button {
  width: auto;
  min-width: 112px;
  margin: 0;
}

.asset-failure-actions small,
.asset-retry-limit {
  margin: 0;
  color: #6e5a56;
  font-size: 11px;
  line-height: 1.5;
}

.asset-failure-actions small {
  flex-basis: 100%;
  text-align: right;
}

.state-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #9da69f;
}

.asset-state-completed .state-dot {
  background: #438565;
}

.asset-state-processing .state-dot {
  background: #c17a31;
  box-shadow: 0 0 0 3px rgba(193, 122, 49, 0.14);
}

.asset-state-failed .state-dot {
  background: var(--rust);
}

.asset-empty {
  padding: 70px 20px;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
}

.landing-empty h2 {
  margin: 22px 0 8px;
  color: var(--pine);
  font-size: 20px;
}

.landing-empty p {
  max-width: 440px;
  margin: auto;
  line-height: 1.7;
}

.empty-swatch {
  display: flex;
  justify-content: center;
  align-items: end;
  gap: 7px;
  height: 47px;
}

.empty-swatch span {
  width: 25px;
  border-radius: 8px 8px 2px 2px;
  background: #c6d1c7;
}

.empty-swatch span:nth-child(1) { height: 30px; }
.empty-swatch span:nth-child(2) { height: 45px; background: var(--moss); }
.empty-swatch span:nth-child(3) { height: 36px; background: #d8b7a8; }

@media (max-width: 980px) {
  .auto-image-layout {
    grid-template-columns: 1fr;
  }

  .asset-setup {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }

  .setup-section + .setup-section {
    border-top: 0;
    border-left: 1px solid var(--line);
  }

  .shot-detail {
    grid-template-columns: minmax(0, 1fr) minmax(190px, 240px);
    gap: 14px;
    padding: 16px;
  }
}

@media (max-width: 720px) {
  .auto-image-page {
    padding: 28px 16px 48px;
  }

  .auto-image-heading {
    align-items: flex-start;
  }

  .asset-stamp {
    min-width: 92px;
    min-height: 96px;
    padding: 12px;
  }

  .asset-stamp strong {
    font-size: 32px;
  }

  .asset-setup {
    grid-template-columns: 1fr;
  }

  .setup-section + .setup-section {
    border-top: 1px solid var(--line);
    border-left: 0;
  }

  .episode-picker {
    grid-template-columns: 1fr 1fr;
  }

  .episode-picker > label {
    grid-column: 1 / -1;
  }

  .episode-picker .asset-button {
    min-height: 42px;
    padding-inline: 9px;
    font-size: 11px;
  }

  .shot-detail {
    grid-template-columns: 1fr;
    gap: 18px;
    padding: 15px;
  }

  .shared-assets {
    padding: 16px 0 0;
    border-top: 1px solid var(--line);
    border-left: 0;
  }

  .asset-row {
    grid-template-columns: 72px minmax(0, 1fr);
    gap: 11px;
  }

  .asset-preview {
    width: 72px;
  }

  .asset-state {
    grid-column: 2;
    justify-content: flex-start;
    text-align: left;
  }

  .asset-detail-toggle {
    text-align: left;
  }

  .asset-failure-details {
    grid-column: 2;
  }

  .asset-failure-actions {
    align-items: flex-start;
    flex-direction: column;
  }

  .asset-failure-actions small {
    text-align: left;
  }
}

@media (prefers-reduced-motion: reduce) {
  .planning-indicator {
    animation: none;
  }

  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
