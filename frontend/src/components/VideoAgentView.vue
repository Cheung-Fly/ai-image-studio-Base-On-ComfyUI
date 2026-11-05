<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { apiJson, errorMessage } from "../api";
import { beginAgentActivity } from "../agentActivity";
import type {
  CustomModel,
  GenerationBackend,
  Provider,
  RunningHubConfig,
  VideoEpisode,
  VideoEpisodeAsset,
  VideoEpisodeShot,
  VideoSeries,
  VideoSeriesSummary,
} from "../types";

const props = defineProps<{
  provider: Provider;
  selectedCustomId: number | null;
  customModels: CustomModel[];
  runningHubConfig: RunningHubConfig | null;
}>();

type SelectedSeriesFile = {
  file: File;
  relativePath: string;
};

const MAX_SERIES_FILES = 10_000;
const videoWorkflows = ref<Array<{ file: string; backend: string }>>([]);
const workflowFile = ref("");
const generationBackend = ref<GenerationBackend>("comfyui");

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

const series = ref<VideoSeries | null>(null);
const seriesHistory = ref<VideoSeriesSummary[]>([]);
const selectedEpisodeId = ref<number | null>(null);
const selectedFiles = ref<SelectedSeriesFile[]>([]);
const selectedFolderName = ref("");
const folderDragActive = ref(false);
const busy = ref(false);
const loading = ref(false);
const error = ref("");
const openShotIndex = ref<number | null>(null);
const folderInput = ref<HTMLInputElement | null>(null);
let pollTimer: number | undefined;
let endSeriesActivity: (() => void) | null = null;

const selectedEntry = computed(
  () =>
    series.value?.episodes.find(
      (entry) => entry.episode.id === selectedEpisodeId.value,
    ) || null,
);
const episode = computed(() => selectedEntry.value?.episode || null);
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
const aspectRatio = computed({
  get: () => episode.value?.aspect_ratio || "16:9 (Widescreen)",
  set: (value: string) => {
    if (episode.value) episode.value.aspect_ratio = value;
  },
});
const megapixels = computed({
  get: () => episode.value?.megapixels || 1,
  set: (value: number) => {
    if (episode.value) episode.value.megapixels = value;
  },
});
const isRunning = computed(
  () => series.value?.status === "queued" || series.value?.status === "processing",
);
const totalDuration = computed(() =>
  (episode.value?.shots || []).reduce(
    (total, shot) => total + Number(shot.duration_seconds || 0),
    0,
  ),
);
const longShotCount = computed(
  () =>
    episode.value?.shots.filter(
      (shot) => shot.duration_seconds < 2 || shot.duration_seconds > 15,
    ).length || 0,
);
const hasInvalidGenerationSettings = computed(
  () => {
    if (
      !series.value ||
      (generationBackend.value === "runninghub" && !runningHubSelection.value)
    ) return true;
    return series.value.episodes.some(({ episode: item }) =>
      !aspectOptions.includes(item.aspect_ratio) ||
      !Number.isFinite(item.megapixels) ||
      item.megapixels < 0.1 ||
      item.megapixels > 16 ||
      item.shots.some(
        (shot) =>
          !Number.isFinite(Number(shot.duration_seconds)) ||
          Number(shot.duration_seconds) <= 0 ||
          Number(shot.duration_seconds) > 300,
      ),
    );
  },
);

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    draft: "待检查",
    queued: "排队中",
    processing: "制作中",
    completed: "已完成",
    failed: "制作失败",
    cancelled: "已取消",
    pending: "等待中",
  };
  return labels[status] || status;
}

function mediaLabel(kind: VideoEpisodeAsset["media_type"]): string {
  return { image: "图片", video: "视频", audio: "音频" }[kind];
}

function shotSegmentPlan(seconds: number): string {
  if (seconds <= 15) {
    return seconds < 2 ? `生成 2 秒，成片裁到 ${seconds} 秒` : "单段生成";
  }
  const fullSegments = Math.floor(seconds / 15);
  const remainder = seconds - fullSegments * 15;
  const count = fullSegments + (remainder > 0 ? 1 : 0);
  return `拆为 ${count} 段，后续镜头续接`;
}

function setSelectedFolder(files: SelectedSeriesFile[]) {
  selectedFiles.value = [];
  selectedFolderName.value = "";
  if (!files.length) {
    error.value = "所选文件夹中没有文件";
    return;
  }
  if (files.length > MAX_SERIES_FILES) {
    error.value = `整剧文件夹最多包含 ${MAX_SERIES_FILES} 个文件`;
    return;
  }
  const paths = files.map((item) => item.relativePath.split("/"));
  const roots = new Set(paths.map((parts) => parts[0]));
  if (
    roots.size !== 1 ||
    paths.some((parts) => parts.length < 3 || parts.some((part) => !part))
  ) {
    error.value = "请添加一个整剧根文件夹，并确保每集都有独立子文件夹";
    return;
  }
  selectedFiles.value = files;
  selectedFolderName.value = paths[0][0];
  error.value = "";
}

function chooseFolder(fileList: FileList | null) {
  if (!fileList?.length) return;
  const files = Array.from(fileList || []).map((file) => ({
    file,
    relativePath: file.webkitRelativePath,
  }));
  setSelectedFolder(files);
}

function activateFolderDrop(event: DragEvent) {
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  folderDragActive.value = true;
}

function readDirectoryEntries(
  reader: FileSystemDirectoryReader,
): Promise<FileSystemEntry[]> {
  return new Promise((resolve, reject) => {
    reader.readEntries(resolve, reject);
  });
}

function readFileEntry(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => {
    entry.file(resolve, reject);
  });
}

async function collectDroppedEntry(
  entry: FileSystemEntry,
  parentPath: string,
  files: SelectedSeriesFile[],
): Promise<void> {
  if (entry.isFile) {
    const file = await readFileEntry(entry as FileSystemFileEntry);
    files.push({ file, relativePath: `${parentPath}${entry.name}` });
    if (files.length > MAX_SERIES_FILES) {
      throw new Error(`整剧文件夹最多包含 ${MAX_SERIES_FILES} 个文件`);
    }
    return;
  }
  if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader();
    const children: FileSystemEntry[] = [];
    while (true) {
      const batch = await readDirectoryEntries(reader);
      if (!batch.length) break;
      children.push(...batch);
    }
    for (const child of children) {
      await collectDroppedEntry(child, `${parentPath}${entry.name}/`, files);
    }
  }
}

async function dropFolder(event: DragEvent) {
  folderDragActive.value = false;
  const items = Array.from(event.dataTransfer?.items || []);
  const files: SelectedSeriesFile[] = [];
  try {
    for (const item of items) {
      const entry = item.webkitGetAsEntry();
      if (entry) {
        await collectDroppedEntry(entry, "", files);
      } else {
        const file = item.getAsFile();
        if (file) files.push({ file, relativePath: file.name });
      }
    }
    setSelectedFolder(files);
  } catch (cause) {
    selectedFiles.value = [];
    selectedFolderName.value = "";
    error.value = errorMessage(cause);
  }
}

function summaryFromSeries(item: VideoSeries): VideoSeriesSummary {
  return {
    id: item.id,
    title: item.title,
    status: item.status,
    total_episodes: item.total_episodes,
    current_episode_index: item.current_episode_index,
    progress_message: item.progress_message,
    created_at: item.created_at,
    updated_at: item.updated_at,
  };
}

async function loadSeries() {
  loading.value = true;
  error.value = "";
  try {
    seriesHistory.value = await apiJson<VideoSeriesSummary[]>(
      "/api/video-agent/series",
    );
    if (!series.value && seriesHistory.value.length) {
      await selectSeries(seriesHistory.value[0].id);
    }
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}

async function loadVideoWorkflows(): Promise<void> {
  try {
    videoWorkflows.value = await apiJson<Array<{ file: string; backend: string }>>(
      "/api/video-agent/series/workflows",
    );
    workflowFile.value =
      videoWorkflows.value.find((item) => item.backend === "comfyui")?.file || "";
  } catch (cause) {
    error.value = `读取视频工作流失败：${errorMessage(cause)}`;
  }
}

function setSeries(next: VideoSeries) {
  series.value = next;
  const hasSelectedEpisode = next.episodes.some(
    (entry) => entry.episode.id === selectedEpisodeId.value,
  );
  if (!hasSelectedEpisode) {
    selectedEpisodeId.value = next.episodes[0]?.episode.id ?? null;
  }
  openShotIndex.value = null;
}

async function selectSeries(seriesId: number) {
  loading.value = true;
  error.value = "";
  try {
    const selected = await apiJson<VideoSeries>(
      `/api/video-agent/series/${seriesId}`,
    );
    setSeries(selected);
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}

function selectEpisode(episodeId: number) {
  selectedEpisodeId.value = episodeId;
  openShotIndex.value = null;
}

async function refreshSeries(seriesId: number) {
  const next = await apiJson<VideoSeries>(
    `/api/video-agent/series/${seriesId}`,
  );
  if (series.value?.id === seriesId) setSeries(next);
  seriesHistory.value = seriesHistory.value.map((item) =>
    item.id === seriesId ? summaryFromSeries(next) : item,
  );
}

function syncPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
  if (!isRunning.value || !series.value) return;
  const seriesId = series.value.id;
  pollTimer = window.setInterval(() => {
    void refreshSeries(seriesId).catch((cause: unknown) => {
      error.value = errorMessage(cause);
    });
  }, 3000);
}

watch(
  () => `${series.value?.id ?? ""}:${series.value?.status ?? ""}`,
  syncPolling,
);

watch(
  isRunning,
  (running) => {
    if (running && !endSeriesActivity) {
      endSeriesActivity = beginAgentActivity();
    } else if (!running && endSeriesActivity) {
      endSeriesActivity();
      endSeriesActivity = null;
    }
  },
  { immediate: true },
);

async function importSeries() {
  if (!selectedFiles.value.length) {
    error.value = "先选择整剧视频制作材料文件夹";
    return;
  }
  if (props.provider === "custom" && !props.selectedCustomId) {
    error.value = "先在工作区选择一个可用的自定义模型";
    return;
  }
  busy.value = true;
  error.value = "";
  const form = new FormData();
  for (const item of selectedFiles.value) {
    form.append("files", item.file, item.relativePath);
  }
  form.set("provider", props.provider);
  if (props.provider === "custom" && props.selectedCustomId) {
    form.set("custom_config_id", String(props.selectedCustomId));
  }
  try {
    const imported = await apiJson<VideoSeries>(
      "/api/video-agent/series",
      { method: "POST", body: form },
      "导入整剧材料失败",
    );
    setSeries(imported);
    selectedFiles.value = [];
    selectedFolderName.value = "";
    if (folderInput.value) folderInput.value.value = "";
    seriesHistory.value = [
      summaryFromSeries(imported),
      ...seriesHistory.value.filter((item) => item.id !== imported.id),
    ];
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

function toggleAssetPicker(shot: VideoEpisodeShot, event: Event) {
  const details = event.currentTarget as HTMLDetailsElement;
  openShotIndex.value = details.open ? shot.shot_index : null;
}

async function startSeries() {
  if (!series.value) return;
  busy.value = true;
  error.value = "";
  try {
    const started = await apiJson<VideoSeries>(
      `/api/video-agent/series/${series.value.id}/start`,
      {
        method: "POST",
        body: JSON.stringify({
          episodes: series.value.episodes.map(({ episode: item }) => ({
            episode_id: item.id,
            settings: {
              aspect_ratio: item.aspect_ratio,
              megapixels: item.megapixels,
              generation_backend: generationBackend.value,
              workflow_file:
                generationBackend.value === "comfyui" ? workflowFile.value : "",
              ...(generationBackend.value === "runninghub"
                ? { runninghub: runningHubSelection.value }
                : {}),
              shots: item.shots.map((shot) => ({
                shot_no: shot.shot_no,
                duration_seconds: Number(shot.duration_seconds),
                asset_ids: shot.asset_ids,
              })),
            },
          })),
        }),
      },
      "启动整剧制作失败",
    );
    setSeries(started);
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

async function cancelSeries() {
  if (!series.value) return;
  busy.value = true;
  error.value = "";
  try {
    const updated = await apiJson<VideoSeries>(
      `/api/video-agent/series/${series.value.id}/cancel`,
      { method: "POST" },
      "取消整剧制作失败",
    );
    setSeries(updated);
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

async function resumeSeries() {
  if (!series.value) return;
  busy.value = true;
  error.value = "";
  try {
    const updated = await apiJson<VideoSeries>(
      `/api/video-agent/series/${series.value.id}/resume`,
      { method: "POST" },
      "继续整剧制作失败",
    );
    setSeries(updated);
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

function episodeProgress(item: VideoEpisode): number {
  if (item.status === "completed") return 100;
  if (!item.total_shots) return 0;
  const completed = item.shots.filter((shot) => shot.status === "completed").length;
  return Math.round((completed / item.total_shots) * 100);
}

function seriesProgress(item: VideoSeries): number {
  const total = item.episodes.reduce(
    (sum, entry) => sum + entry.episode.total_shots,
    0,
  );
  if (item.status === "completed") return 100;
  if (!total) return 0;
  const completed = item.episodes.reduce(
    (sum, entry) =>
      sum +
      entry.episode.shots.filter((shot) => shot.status === "completed").length,
    0,
  );
  return Math.round((completed / total) * 100);
}

onMounted(() => {
  void loadSeries();
  void loadVideoWorkflows();
});

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer);
  endSeriesActivity?.();
  endSeriesActivity = null;
});
</script>

<template>
  <section class="video-agent-page">
    <header class="video-heading">
      <div>
        <p class="video-kicker">整剧制作</p>
        <h1>把整剧材料放进一个文件夹，逐集完成制作。</h1>
        <p class="video-intro">
          每集一个子文件夹，包含该集分镜和素材。确认所有镜头后，后台按集号顺序连续生成。
        </p>
      </div>
      <div class="workflow-mark" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
      </div>
    </header>

    <div v-if="error" class="video-alert" role="alert">
      <span>{{ error }}</span>
      <button type="button" aria-label="关闭错误提示" @click="error = ''">×</button>
    </div>

    <section class="package-strip" aria-labelledby="package-title">
      <div class="package-copy">
        <h2 id="package-title">导入整剧视频制作材料</h2>
        <p>选择整剧根文件夹。每集子文件夹内放一份分镜 Excel 或 JSON，以及该集素材。</p>
        <p class="package-limit">最多 100 集 · 总量 4 GB · 图片 20 MB · 视频 500 MB · 音频 100 MB</p>
      </div>
      <label
        class="package-drop"
        :class="{ 'drag-active': folderDragActive }"
        @dragover.prevent="activateFolderDrop($event)"
        @dragleave.prevent="folderDragActive = false"
        @drop.prevent="void dropFolder($event)"
      >
        <input
          ref="folderInput"
          type="file"
          webkitdirectory
          directory
          multiple
          @change="chooseFolder(($event.target as HTMLInputElement).files)"
        />
        <span class="drop-icon" aria-hidden="true">▣</span>
        <span class="drop-name">{{ selectedFolderName || "选择整剧材料文件夹" }}</span>
        <span class="drop-hint">
          {{ selectedFiles.length ? `已选 ${selectedFiles.length} 个文件` : "点击选择或拖入整剧文件夹" }}
        </span>
      </label>
      <button
        class="video-button primary"
        type="button"
        :disabled="busy || !selectedFiles.length"
        @click="void importSeries()"
      >
        {{ busy ? "正在分析整剧素材…" : "导入并分析全剧" }}
      </button>
    </section>

    <section v-if="seriesHistory.length" class="episode-history" aria-label="整剧任务历史">
      <span class="history-label">最近整剧</span>
      <button
        v-for="item in seriesHistory.slice(0, 5)"
        :key="item.id"
        type="button"
        class="history-item"
        :class="{ active: series?.id === item.id }"
        @click="void selectSeries(item.id)"
      >
        <span>{{ item.title }}</span>
        <small>{{ statusLabel(item.status) }}</small>
      </button>
      <span v-if="loading" class="history-loading">读取中</span>
    </section>

    <section v-if="series" class="series-workspace">
      <header class="series-title-row">
        <div>
          <div class="episode-title-meta">
            <span class="state-led" :class="`state-${series.status}`" />
            <span>{{ statusLabel(series.status) }}</span>
            <span class="meta-divider">/</span>
            <span>{{ series.total_episodes }} 集</span>
          </div>
          <h2>{{ series.title }}</h2>
        </div>
        <div class="episode-actions">
          <button
            v-if="series.status === 'draft'"
            class="video-button primary"
            type="button"
            :disabled="busy || !series.episodes.length || hasInvalidGenerationSettings"
            @click="void startSeries()"
          >
            {{ busy ? "提交中…" : "确认所有分集并开始" }}
          </button>
          <button
            v-else-if="isRunning"
            class="video-button quiet"
            type="button"
            :disabled="busy"
            @click="void cancelSeries()"
          >
            取消整剧制作
          </button>
          <button
            v-else-if="series.status === 'failed'"
            class="video-button primary"
            type="button"
            :disabled="busy"
            @click="void resumeSeries()"
          >
            {{ busy ? "提交中…" : "从失败分集继续" }}
          </button>
        </div>
      </header>

      <section v-if="series.status === 'draft'" class="series-backend-settings">
        <label>
          <span>视频生成服务</span>
          <select v-model="generationBackend" :disabled="busy || isRunning">
            <option value="comfyui">ComfyUI</option>
            <option value="runninghub" :disabled="!runningHubSelection">RunningHub</option>
          </select>
        </label>
        <label v-if="generationBackend === 'comfyui'">
          <span>兼容视频工作流</span>
          <select v-model="workflowFile" :disabled="busy || isRunning">
            <option v-for="item in videoWorkflows" :key="item.file" :value="item.file">
              {{ item.file }}
            </option>
          </select>
        </label>
        <div v-else class="backend-readout">
          <strong>{{ runningHubSelection?.workflow_id || "尚未配置" }}</strong>
          <span>
            {{
              runningHubSelection
                ? `提示词输入：${runningHubSelection.prompt_node_id} / ${runningHubSelection.prompt_field}`
                : "请先配置 RunningHub 工作流"
            }}
          </span>
        </div>
      </section>

      <div v-if="series.error" class="episode-error" role="alert">
        {{ series.error }}
      </div>

      <div v-if="isRunning" class="episode-progress" role="status">
        <div class="progress-track">
          <span :style="{ width: `${seriesProgress(series)}%` }" />
        </div>
        <div class="progress-copy">
          <strong>{{ series.progress_message }}</strong>
          <span>{{ seriesProgress(series) }}%</span>
        </div>
      </div>

      <nav class="series-episodes" aria-label="分集检查">
        <button
          v-for="entry in series.episodes"
          :key="entry.episode.id"
          type="button"
          class="series-episode-tab"
          :class="{
            active: episode?.id === entry.episode.id,
            [`tab-${entry.episode.status}`]: true,
          }"
          @click="selectEpisode(entry.episode.id)"
        >
          <span>{{ entry.folder_name }}</span>
          <small>{{ statusLabel(entry.episode.status) }}</small>
        </button>
      </nav>

    <section v-if="episode" class="episode-workspace">
      <div class="episode-main">
        <header class="episode-title-row">
          <div>
            <div class="episode-title-meta">
              <span class="state-led" :class="`state-${episode.status}`" />
              <span>{{ statusLabel(episode.status) }}</span>
              <span class="meta-divider">/</span>
              <span>{{ episode.shots.length }} 个镜头</span>
              <span class="meta-divider">/</span>
              <span>{{ Math.round(totalDuration) }} 秒</span>
            </div>
            <h2>{{ episode.title }}</h2>
          </div>
        </header>

        <div v-if="episode.mapping_warning" class="mapping-warning" role="status">
          {{ episode.mapping_warning }}
        </div>
        <div v-if="episode.error" class="episode-error" role="alert">
          {{ episode.error }}
        </div>

        <div v-if="isRunning" class="episode-progress" role="status">
          <div class="progress-track">
            <span :style="{ width: `${episodeProgress(episode)}%` }" />
          </div>
          <div class="progress-copy">
            <strong>{{ episode.progress_message }}</strong>
            <span>{{ episodeProgress(episode) }}%</span>
          </div>
        </div>

        <div class="shot-track-heading">
          <h3>镜头检查表</h3>
          <p>
            {{
              episode.status === "draft"
                ? "调整时长与素材。确认后按镜号顺序执行。"
                : episode.generation_backend === "runninghub"
                  ? "镜头按分镜顺序进入 RunningHub 工作流。"
                  : "镜头按分镜顺序进入 ComfyUI 工作流。"
            }}
          </p>
        </div>

        <ol class="shot-track">
          <li
            v-for="shot in episode.shots"
            :key="`${shot.shot_index}-${shot.shot_no}`"
            class="shot-row"
            :class="[`shot-${shot.status}`, { 'shot-open': openShotIndex === shot.shot_index }]"
          >
            <div class="shot-index">
              <span>{{ shot.shot_no }}</span>
            </div>
            <div class="shot-content">
              <div class="shot-title-line">
                <strong>{{ shot.framing || "镜头" }}</strong>
                <span v-if="shot.camera_movement">{{ shot.camera_movement }}</span>
                <span class="shot-status">{{ statusLabel(shot.status) }}</span>
              </div>
              <p class="shot-description">{{ shot.visual_description }}</p>
              <p v-if="shot.prompt" class="shot-prompt">{{ shot.prompt }}</p>
              <p v-if="shot.dialogue" class="shot-note"><b>对白</b>{{ shot.dialogue }}</p>
              <p v-if="shot.sound_effects" class="shot-note"><b>音效</b>{{ shot.sound_effects }}</p>
              <p v-if="shot.error" class="shot-error">{{ shot.error }}</p>
              <details
                class="asset-picker"
                :open="openShotIndex === shot.shot_index"
                @toggle="toggleAssetPicker(shot, $event)"
              >
                <summary>
                  素材
                  <span class="asset-count">{{ shot.asset_ids.length }}</span>
                  <span v-if="shot.match_source === 'agent' && episode.status === 'draft'" class="match-badge">
                    Agent 建议 · {{ Math.round(shot.match_confidence * 100) }}%
                  </span>
                  <span v-else-if="episode.status === 'draft' && !shot.asset_ids.length" class="unmatched-badge">
                    尚未匹配
                  </span>
                </summary>
                <div v-if="openShotIndex === shot.shot_index" class="asset-options">
                  <label
                    v-for="asset in episode.assets"
                    :key="asset.id"
                    class="asset-option"
                  >
                    <input
                      v-model="shot.asset_ids"
                      type="checkbox"
                      :value="asset.id"
                      :disabled="episode.status !== 'draft'"
                    />
                    <img
                      v-if="asset.media_type === 'image'"
                      :src="asset.file_url"
                      :alt="asset.filename"
                      loading="lazy"
                    />
                    <span v-else class="asset-kind-icon">{{ asset.media_type === "video" ? "▶" : "♫" }}</span>
                    <span class="asset-option-copy">
                      <strong>{{ asset.filename }}</strong>
                      <small>{{ mediaLabel(asset.media_type) }} · {{ (asset.size / 1048576).toFixed(1) }} MB</small>
                    </span>
                  </label>
                  <p v-if="!episode.assets.length" class="no-assets">本集没有可选素材。</p>
                </div>
              </details>
            </div>
            <div class="shot-controls">
              <label>
                <span>成片时长（秒）</span>
                <input
                  v-model.number="shot.duration_seconds"
                  type="number"
                  min="0.1"
                  max="300"
                  step="0.1"
                  :disabled="episode.status !== 'draft'"
                />
              </label>
              <small :class="{ 'duration-warning': shot.duration_seconds < 2 || shot.duration_seconds > 15 }">
                {{ shotSegmentPlan(shot.duration_seconds) }}
              </small>
              <small v-if="shot.segment_count" class="segment-count">
                已生成 {{ shot.segment_count }} 段
              </small>
            </div>
          </li>
        </ol>
      </div>

      <aside class="episode-rail">
        <section class="rail-section">
          <h3>整集画幅</h3>
          <label class="rail-field">
            <span>画面比例</span>
            <select v-model="aspectRatio" :disabled="episode.status !== 'draft'">
              <option v-for="option in aspectOptions" :key="option" :value="option">
                {{ option }}
              </option>
            </select>
          </label>
          <label class="rail-field">
            <span>目标百万像素</span>
            <input
              v-model.number="megapixels"
              type="number"
              min="0.1"
              max="16"
              step="0.1"
              :disabled="episode.status !== 'draft'"
            />
          </label>
          <p class="rail-help">整集统一画幅，避免镜头拼接时画面跳变。</p>
        </section>

        <section class="rail-section plan-section">
          <h3>制作顺序</h3>
          <ol class="plan-list">
            <li><span>1</span>按镜号逐镜生成</li>
            <li><span>2</span>超长镜头续段生成</li>
            <li><span>3</span>裁切短镜并统一画幅</li>
            <li><span>4</span>合成整集 MP4</li>
          </ol>
          <div v-if="longShotCount" class="duration-note">
            {{ longShotCount }} 个镜头需要按工作流时长适配。
          </div>
        </section>

        <section v-if="episode.output_url" class="rail-section final-section">
          <h3>整集成片</h3>
          <video :src="episode.output_url" controls preload="metadata" />
          <a class="video-button primary download-button" :href="episode.output_url" download>
            下载 MP4
          </a>
        </section>

        <section class="rail-section asset-summary">
          <h3>素材清单</h3>
          <p>{{ episode.assets.length }} 个素材已导入</p>
          <div class="asset-totals">
            <span v-for="kind in ['image', 'video', 'audio'] as const" :key="kind">
              <b>{{ episode.assets.filter((asset) => asset.media_type === kind).length }}</b>
              {{ mediaLabel(kind) }}
            </span>
          </div>
        </section>
      </aside>
    </section>
    </section>

    <section v-else-if="!loading" class="empty-video">
      <div class="empty-frame" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <h2>先选择整剧视频制作材料文件夹</h2>
      <p>每集一个子文件夹，放入该集分镜与素材。导入后检查全部镜头，再按集号顺序连续制作。</p>
    </section>
  </section>
</template>

<style scoped>
.video-agent-page {
  --pine: #263a38;
  --rust: #b64e3d;
  --paper: #f7f8f3;
  --mist: #e8eee8;
  --ink: #263445;
  --muted: #64736e;
  --rule: #cbd5cc;
  min-height: calc(100vh - 74px);
  padding: 38px clamp(20px, 4vw, 58px) 72px;
  color: var(--ink);
  background: var(--paper);
}

.video-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 30px;
  padding-bottom: 27px;
  border-bottom: 1px solid var(--rule);
}

.video-kicker {
  margin: 0 0 9px;
  color: var(--rust);
  font-size: 13px;
  font-weight: 700;
}

.video-heading h1 {
  max-width: 720px;
  margin: 0;
  color: var(--pine);
  font-size: clamp(26px, 3vw, 38px);
  font-weight: 700;
  letter-spacing: -0.035em;
  line-height: 1.2;
}

.video-intro {
  max-width: 670px;
  margin: 13px 0 0;
  color: var(--muted);
  font-size: 15px;
}

.workflow-mark {
  display: flex;
  align-items: center;
  gap: 5px;
  width: 124px;
  height: 66px;
  padding: 0 17px;
  border-left: 1px solid var(--rule);
}

.workflow-mark span {
  display: block;
  width: 15px;
  border: 1px solid var(--pine);
  background: var(--mist);
}

.workflow-mark span:nth-child(1),
.workflow-mark span:nth-child(4) {
  height: 21px;
}

.workflow-mark span:nth-child(2) {
  height: 42px;
  border-color: var(--rust);
  background: var(--rust);
}

.workflow-mark span:nth-child(3) {
  height: 31px;
}

.video-alert,
.mapping-warning,
.episode-error {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-top: 18px;
  padding: 12px 15px;
  border-left: 3px solid var(--rust);
  color: #76352b;
  background: #f8e9e5;
}

.video-alert button {
  border: 0;
  color: inherit;
  background: transparent;
  font-size: 20px;
  line-height: 1;
}

.package-strip {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(230px, 0.9fr) auto;
  align-items: center;
  gap: 22px;
  margin-top: 24px;
  padding: 21px 23px;
  border: 1px solid var(--rule);
  background: #fff;
}

.package-copy h2,
.rail-section h3,
.shot-track-heading h3 {
  margin: 0;
  color: var(--pine);
  font-size: 16px;
}

.package-copy p {
  margin: 6px 0 0;
  color: var(--muted);
}

.package-copy .package-limit {
  margin-top: 8px;
  color: #7d8983;
  font-size: 12px;
}

.package-drop {
  position: relative;
  display: grid;
  grid-template-columns: 35px minmax(0, 1fr);
  align-items: center;
  min-height: 66px;
  padding: 9px 13px;
  border: 1px dashed #9aaa9e;
  background: var(--paper);
  cursor: pointer;
}

.package-drop.drag-active {
  border-color: var(--rust);
  background: #f8ece8;
}

.package-drop input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

.package-drop:focus-within {
  outline: 3px solid rgba(182, 78, 61, 0.25);
  outline-offset: 3px;
}

.drop-icon {
  grid-row: span 2;
  color: var(--rust);
  font-size: 27px;
  text-align: center;
}

.drop-name {
  overflow: hidden;
  color: var(--pine);
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.drop-hint {
  color: #78857e;
  font-size: 12px;
}

.video-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 40px;
  padding: 0 15px;
  border: 1px solid transparent;
  font-weight: 650;
  text-decoration: none;
  white-space: nowrap;
}

.video-button.primary {
  border-color: var(--pine);
  color: #fff;
  background: var(--pine);
}

.video-button.primary:hover:not(:disabled) {
  background: #34504b;
}

.video-button.quiet {
  border-color: var(--rule);
  color: var(--pine);
  background: #fff;
}

.video-button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.video-button:focus-visible,
.history-item:focus-visible,
.series-episode-tab:focus-visible,
.asset-picker summary:focus-visible,
.asset-option input:focus-visible {
  outline: 3px solid rgba(182, 78, 61, 0.28);
  outline-offset: 3px;
}

.episode-history {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 16px 0 0;
  overflow-x: auto;
}

.history-label {
  flex: 0 0 auto;
  margin-right: 5px;
  color: var(--muted);
  font-size: 12px;
}

.history-item {
  display: flex;
  align-items: center;
  gap: 9px;
  max-width: 240px;
  padding: 6px 10px;
  border: 1px solid transparent;
  color: var(--pine);
  background: transparent;
  cursor: pointer;
}

.history-item.active {
  border-color: var(--rule);
  background: #fff;
}

.history-item span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-item small {
  flex: 0 0 auto;
  color: var(--muted);
}

.history-loading {
  color: var(--muted);
  font-size: 12px;
}

.series-workspace {
  margin-top: 25px;
}

.series-title-row,
.episode-workspace {
  min-width: 0;
}

.series-title-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  padding-bottom: 19px;
  border-bottom: 2px solid var(--pine);
}

.series-title-row h2 {
  margin: 7px 0 0;
  color: var(--pine);
  font-size: 25px;
  letter-spacing: -0.02em;
}

.series-backend-settings {
  display: grid;
  grid-template-columns: minmax(180px, 220px) minmax(240px, 1fr);
  gap: 14px;
  align-items: end;
  margin: 18px 0;
  padding: 16px 18px;
  background: #eef2ed;
  border: 1px solid #d5ded6;
  border-radius: 9px;
}

.series-backend-settings label,
.backend-readout {
  display: grid;
  gap: 6px;
  color: #64716a;
  font-size: 11px;
}

.series-backend-settings select {
  min-height: 38px;
  padding: 7px 10px;
  border: 1px solid #cbd6cc;
  border-radius: 6px;
  color: #263a38;
  background: #fff;
}

.backend-readout {
  align-self: stretch;
  align-content: center;
  padding: 8px 12px;
  background: #e2eae2;
  border-radius: 6px;
  overflow-wrap: anywhere;
}

.backend-readout strong {
  color: #263a38;
  font-size: 12px;
}

.series-episodes {
  display: flex;
  gap: 6px;
  margin-top: 13px;
  overflow-x: auto;
  border-bottom: 1px solid var(--rule);
}

.series-episode-tab {
  display: grid;
  flex: 0 0 auto;
  gap: 4px;
  min-width: 105px;
  padding: 9px 11px 8px;
  border: 0;
  border-bottom: 2px solid transparent;
  color: var(--pine);
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.series-episode-tab.active {
  border-bottom-color: var(--rust);
  background: #fff;
}

.series-episode-tab span {
  max-width: 180px;
  overflow: hidden;
  font-size: 12px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.series-episode-tab small {
  color: var(--muted);
  font-size: 10px;
}

.series-episode-tab.tab-completed small {
  color: #56806c;
}

.series-episode-tab.tab-failed small,
.series-episode-tab.tab-cancelled small {
  color: var(--rust);
}

.episode-workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 286px;
  align-items: start;
  gap: 28px;
  margin-top: 25px;
}

.episode-main {
  min-width: 0;
}

.episode-title-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  padding-bottom: 19px;
  border-bottom: 2px solid var(--pine);
}

.episode-title-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.state-led {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #87958c;
}

.state-processing,
.state-queued {
  background: #bc8553;
}

.state-completed {
  background: #56806c;
}

.state-failed,
.state-cancelled {
  background: var(--rust);
}

.meta-divider {
  color: #a4afa8;
}

.episode-title-row h2 {
  margin: 7px 0 0;
  color: var(--pine);
  font-size: 25px;
  letter-spacing: -0.02em;
}

.episode-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 8px;
}

.episode-progress {
  margin: 17px 0 0;
  padding: 13px 15px;
  border: 1px solid var(--rule);
  background: #fff;
}

.progress-track {
  height: 4px;
  background: var(--mist);
}

.progress-track span {
  display: block;
  height: 100%;
  background: var(--rust);
  transition: width 0.25s ease;
}

.progress-copy {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  color: var(--pine);
  font-size: 12px;
}

.progress-copy span {
  font-variant-numeric: tabular-nums;
}

.shot-track-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 14px;
  margin-top: 23px;
}

.shot-track-heading p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
  text-align: right;
}

.shot-track {
  margin: 10px 0 0;
  padding: 0;
  border-top: 1px solid var(--rule);
  list-style: none;
}

.shot-row {
  display: grid;
  grid-template-columns: 48px minmax(0, 1fr) 140px;
  gap: 13px;
  padding: 17px 0 16px;
  border-bottom: 1px solid var(--rule);
}

.shot-index {
  position: relative;
  display: flex;
  justify-content: center;
  padding-top: 2px;
}

.shot-index::after {
  position: absolute;
  top: 28px;
  bottom: -34px;
  width: 1px;
  background: #c6d0c8;
  content: "";
}

.shot-row:last-child .shot-index::after {
  display: none;
}

.shot-index span {
  display: grid;
  place-items: center;
  width: 29px;
  height: 29px;
  border: 1px solid #aebbb1;
  border-radius: 50%;
  color: var(--pine);
  background: var(--paper);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.shot-completed .shot-index span {
  border-color: var(--pine);
  color: #fff;
  background: var(--pine);
}

.shot-processing .shot-index span {
  border-color: var(--rust);
  color: #fff;
  background: var(--rust);
}

.shot-content {
  min-width: 0;
}

.shot-title-line {
  display: flex;
  align-items: baseline;
  gap: 9px;
  color: var(--pine);
}

.shot-title-line > span:not(.shot-status) {
  color: var(--muted);
  font-size: 12px;
}

.shot-status {
  margin-left: auto;
  color: var(--muted);
  font-size: 11px;
}

.shot-processing .shot-status {
  color: var(--rust);
  font-weight: 700;
}

.shot-description {
  margin: 6px 0 0;
  line-height: 1.65;
}

.shot-prompt {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}

.shot-note {
  display: flex;
  gap: 8px;
  margin: 6px 0 0;
  color: #53645c;
  font-size: 12px;
}

.shot-note b {
  flex: 0 0 auto;
  color: var(--pine);
}

.shot-error {
  margin: 7px 0 0;
  color: #8d392f;
  font-size: 12px;
}

.asset-picker {
  margin-top: 10px;
}

.asset-picker summary {
  display: flex;
  align-items: center;
  gap: 7px;
  width: fit-content;
  color: var(--pine);
  font-size: 12px;
  cursor: pointer;
  list-style-position: inside;
}

.asset-count {
  display: inline-grid;
  place-items: center;
  min-width: 18px;
  height: 18px;
  border-radius: 50%;
  color: var(--pine);
  background: var(--mist);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}

.match-badge,
.unmatched-badge {
  color: #66776c;
  font-size: 11px;
}

.unmatched-badge {
  color: #9a5c3c;
}

.asset-options {
  display: grid;
  gap: 3px;
  max-height: 310px;
  margin-top: 9px;
  padding: 7px;
  overflow: auto;
  border: 1px solid var(--rule);
  background: #fff;
}

.asset-option {
  display: grid;
  grid-template-columns: 17px 32px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  padding: 5px;
  background: var(--paper);
  cursor: pointer;
}

.asset-option img,
.asset-kind-icon {
  display: grid;
  place-items: center;
  width: 32px;
  height: 30px;
  object-fit: cover;
  color: var(--rust);
  background: var(--mist);
}

.asset-option-copy {
  display: grid;
  min-width: 0;
}

.asset-option-copy strong {
  overflow: hidden;
  color: var(--pine);
  font-size: 11px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.asset-option-copy small {
  color: var(--muted);
  font-size: 10px;
}

.no-assets {
  margin: 8px;
  color: var(--muted);
  font-size: 12px;
}

.shot-controls {
  display: grid;
  align-content: start;
  gap: 5px;
  padding-top: 1px;
}

.shot-controls label,
.rail-field {
  display: grid;
  gap: 5px;
}

.shot-controls label span,
.rail-field span {
  color: var(--muted);
  font-size: 11px;
}

.shot-controls input,
.rail-field input,
.rail-field select {
  width: 100%;
  min-height: 34px;
  padding: 5px 8px;
  border: 1px solid var(--rule);
  border-radius: 0;
  color: var(--pine);
  background: #fff;
  font-variant-numeric: tabular-nums;
}

.shot-controls small {
  color: var(--muted);
  font-size: 10px;
  line-height: 1.4;
}

.shot-controls .duration-warning {
  color: #96553c;
}

.shot-controls .segment-count {
  color: var(--pine);
}

.episode-rail {
  border-top: 2px solid var(--pine);
  background: #fff;
}

.rail-section {
  padding: 18px 17px;
  border: 1px solid var(--rule);
  border-top: 0;
}

.rail-section h3 {
  margin-bottom: 13px;
}

.rail-field + .rail-field {
  margin-top: 12px;
}

.rail-help {
  margin: 10px 0 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
}

.plan-list {
  display: grid;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.plan-list li {
  display: flex;
  align-items: center;
  gap: 9px;
  color: #53645c;
  font-size: 12px;
}

.plan-list li span {
  display: grid;
  place-items: center;
  width: 21px;
  height: 21px;
  border: 1px solid #aebbb1;
  border-radius: 50%;
  color: var(--pine);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}

.duration-note {
  margin-top: 15px;
  padding-top: 11px;
  border-top: 1px solid var(--rule);
  color: #96553c;
  font-size: 11px;
}

.final-section video {
  display: block;
  width: 100%;
  margin: 0 0 12px;
  background: #15201e;
}

.download-button {
  width: 100%;
}

.asset-summary > p {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}

.asset-totals {
  display: flex;
  gap: 12px;
  margin-top: 12px;
  color: var(--muted);
  font-size: 11px;
}

.asset-totals span {
  display: grid;
  gap: 3px;
}

.asset-totals b {
  color: var(--pine);
  font-size: 16px;
  font-variant-numeric: tabular-nums;
}

.empty-video {
  display: grid;
  justify-items: center;
  max-width: 570px;
  margin: 86px auto 0;
  padding: 25px;
  text-align: center;
}

.empty-frame {
  display: flex;
  align-items: center;
  gap: 5px;
  height: 58px;
  margin-bottom: 16px;
}

.empty-frame span {
  width: 19px;
  height: 32px;
  border: 1px solid #8da095;
  background: var(--mist);
}

.empty-frame span:nth-child(2) {
  height: 50px;
  border-color: var(--rust);
  background: #f0d7d0;
}

.empty-video h2 {
  margin: 0;
  color: var(--pine);
  font-size: 20px;
}

.empty-video p {
  max-width: 470px;
  margin: 8px 0 0;
  color: var(--muted);
}

@media (max-width: 1120px) {
  .episode-workspace {
    grid-template-columns: minmax(0, 1fr) 250px;
    gap: 18px;
  }

  .shot-row {
    grid-template-columns: 40px minmax(0, 1fr) 120px;
    gap: 9px;
  }
}

@media (max-width: 820px) {
  .package-strip {
    grid-template-columns: minmax(0, 1fr) minmax(190px, 0.8fr);
  }

  .package-strip > .video-button {
    grid-column: 2;
  }

  .episode-workspace {
    grid-template-columns: 1fr;
  }

  .episode-rail {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 580px) {
  .video-agent-page {
    padding: 26px 15px 48px;
  }

  .workflow-mark {
    display: none;
  }

  .package-strip {
    grid-template-columns: 1fr;
    gap: 13px;
    padding: 17px;
  }

  .package-strip > .video-button {
    grid-column: 1;
  }

  .series-title-row,
  .episode-title-row,
  .shot-track-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .series-backend-settings {
    grid-template-columns: 1fr;
  }

  .episode-actions {
    width: 100%;
  }

  .episode-actions .video-button {
    width: 100%;
  }

  .shot-track-heading p {
    text-align: left;
  }

  .shot-row {
    grid-template-columns: 34px minmax(0, 1fr);
    gap: 8px;
  }

  .shot-controls {
    grid-column: 2;
    grid-template-columns: minmax(100px, 140px) minmax(0, 1fr);
    align-items: end;
    margin-top: 2px;
  }

  .shot-controls small {
    align-self: center;
  }

  .episode-rail {
    grid-template-columns: 1fr;
  }
}

@media (prefers-reduced-motion: reduce) {
  .progress-track span {
    transition: none;
  }
}
</style>
