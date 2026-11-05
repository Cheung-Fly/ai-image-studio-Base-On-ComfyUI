<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { apiFetch, apiJson, errorMessage } from "../api";
import { beginAgentActivity } from "../agentActivity";
import type { CustomModel, Provider } from "../types";

interface StoryboardShot {
  shot_no: string;
  framing: string;
  camera_movement: string;
  visual_description: string;
  duration_seconds: number;
  dialogue: string;
  sound_effects: string;
  prompt: string;
  producer: string;
  expected_submission_time: string;
  reviewer: string;
  actual_submission_time: string;
}

interface Storyboard {
  video_title: string;
  theme: string;
  shooting_time: string;
  deadline: string;
  props: string;
  characters: string;
  shots: StoryboardShot[];
}

interface DirectorSkill {
  id: number;
  name: string;
  instructions: string;
  created_at: string;
  updated_at: string;
}

const props = defineProps<{
  provider: Provider;
  selectedCustomId: number | null;
  customModels: CustomModel[];
}>();

const emit = defineEmits<{
  selectProvider: [provider: Provider, customId?: number | null];
}>();

const script = ref("");
const videoTitle = ref("");
const storyboard = ref<Storyboard | null>(null);
const busy = ref(false);
const exporting = ref(false);
const error = ref("");
const status = ref("");
const skills = ref<DirectorSkill[]>([]);
const selectedSkillIds = ref<number[]>([]);
const skillsLoading = ref(false);
const skillSaving = ref(false);
const skillError = ref("");
const skillFormOpen = ref(false);
const skillFolderInput = ref<HTMLInputElement | null>(null);
const skillForm = reactive({
  id: null as number | null,
  name: "",
  instructions: "",
});
const maxSelectedSkills = 10;
const maxSkillInstructionsLength = 10000;
const shotCount = computed(() => storyboard.value?.shots.length ?? 0);
const totalDuration = computed(() =>
  (storyboard.value?.shots || []).reduce(
    (total, shot) => total + (Number(shot.duration_seconds) || 0),
    0,
  ),
);
const providerSelection = computed(() =>
  props.provider === "custom"
    ? `custom:${props.selectedCustomId ?? ""}`
    : props.provider,
);

onMounted(() => {
  void loadSkills();
});

async function loadSkills(): Promise<void> {
  skillsLoading.value = true;
  try {
    skills.value = await apiJson<DirectorSkill[]>(
      "/api/director/skills",
      undefined,
      "读取分镜 Skill 失败",
    );
    const availableIds = new Set(skills.value.map((skill) => skill.id));
    selectedSkillIds.value = selectedSkillIds.value.filter((id) =>
      availableIds.has(id),
    );
    skillError.value = "";
  } catch (cause) {
    skillError.value = "Skill 加载失败：" + errorMessage(cause);
  } finally {
    skillsLoading.value = false;
  }
}

function openSkillForm(skill?: DirectorSkill): void {
  skillForm.id = skill?.id ?? null;
  skillForm.name = skill?.name ?? "";
  skillForm.instructions = skill?.instructions ?? "";
  skillError.value = "";
  skillFormOpen.value = true;
}

function closeSkillForm(): void {
  skillFormOpen.value = false;
  skillForm.id = null;
  skillForm.name = "";
  skillForm.instructions = "";
}

function handleSkillFolderChange(event: Event): void {
  const input = event.target as HTMLInputElement;
  const files = input.files;
  input.value = "";
  if (!files?.length) return;
  void importSkillFolder(files);
}

async function importSkillFolder(fileList: FileList): Promise<void> {
  skillError.value = "";
  const files = Array.from(fileList).map((file) => ({
    file,
    relativePath: file.webkitRelativePath || file.name,
  }));
  const roots = new Set(
    files.map(({ relativePath }) => relativePath.split(/[\\/]/)[0]),
  );
  if (roots.size !== 1) {
    skillError.value = "请一次选择一个 Skill 文件夹。";
    return;
  }
  const skillFiles = files.filter(
    ({ relativePath }) =>
      relativePath.split(/[\\/]/).at(-1)?.toLowerCase() === "skill.md",
  );
  if (skillFiles.length !== 1) {
    skillError.value =
      skillFiles.length === 0
        ? "所选文件夹中没有 SKILL.md。"
        : "所选文件夹中有多个 SKILL.md，请选择单个 Skill 文件夹。";
    return;
  }
  const [{ file, relativePath }] = skillFiles;
  if (file.size > 256 * 1024) {
    skillError.value = "SKILL.md 不能超过 256 KB。";
    return;
  }

  skillSaving.value = true;
  try {
    const form = new FormData();
    form.append("file", file, relativePath);
    const skill = await apiJson<DirectorSkill>(
      "/api/director/skills/import",
      { method: "POST", body: form },
      "导入 Skill 失败",
    );
    await loadSkills();
    const autoSelected =
      selectedSkillIds.value.length < maxSelectedSkills;
    if (autoSelected) {
      selectedSkillIds.value = [...selectedSkillIds.value, skill.id];
    }
    status.value = autoSelected
      ? `Skill「${skill.name}」已导入并选中。`
      : `Skill「${skill.name}」已导入。`;
  } catch (cause) {
    skillError.value = "导入 Skill 失败：" + errorMessage(cause);
  } finally {
    skillSaving.value = false;
  }
}

async function saveSkill(): Promise<void> {
  skillError.value = "";
  const name = skillForm.name.trim();
  const instructions = skillForm.instructions.trim();
  if (!name || !instructions) {
    skillError.value = "请填写 Skill 名称和指导内容。";
    return;
  }
  skillSaving.value = true;
  try {
    const isNewSkill = skillForm.id === null;
    const skill = await apiJson<DirectorSkill>(
      skillForm.id === null
        ? "/api/director/skills"
        : `/api/director/skills/${skillForm.id}`,
      {
        method: skillForm.id === null ? "POST" : "PUT",
        body: JSON.stringify({ name, instructions }),
      },
      "保存 Skill 失败",
    );
    await loadSkills();
    const autoSelected =
      isNewSkill && selectedSkillIds.value.length < maxSelectedSkills;
    if (autoSelected) {
      selectedSkillIds.value = [...selectedSkillIds.value, skill.id];
    }
    closeSkillForm();
    status.value = autoSelected
      ? `Skill「${skill.name}」已保存并选中。`
      : `Skill「${skill.name}」已保存。`;
  } catch (cause) {
    skillError.value = "保存 Skill 失败：" + errorMessage(cause);
  } finally {
    skillSaving.value = false;
  }
}

async function deleteSkill(skill: DirectorSkill): Promise<void> {
  if (!window.confirm(`删除 Skill「${skill.name}」？`)) return;
  skillError.value = "";
  skillSaving.value = true;
  try {
    await apiJson(
      `/api/director/skills/${skill.id}`,
      { method: "DELETE" },
      "删除 Skill 失败",
    );
    selectedSkillIds.value = selectedSkillIds.value.filter(
      (id) => id !== skill.id,
    );
    if (skillForm.id === skill.id) closeSkillForm();
    await loadSkills();
  } catch (cause) {
    skillError.value = "删除 Skill 失败：" + errorMessage(cause);
  } finally {
    skillSaving.value = false;
  }
}

function selectModel(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  if (value.startsWith("custom:")) {
    emit("selectProvider", "custom", Number(value.slice(7)));
  } else {
    emit("selectProvider", value as Provider, null);
  }
}

async function createStoryboard(): Promise<void> {
  error.value = "";
  status.value = "";
  if (!script.value.trim()) {
    error.value = "请先粘贴一集或一段剧本。";
    return;
  }

  busy.value = true;
  status.value = "导演正在梳理剧情节拍与镜头顺序…";
  const endActivity = beginAgentActivity();
  try {
    const request: Record<string, unknown> = {
      script: script.value,
      video_title: videoTitle.value.trim(),
      provider: props.provider,
      skill_ids: selectedSkillIds.value,
    };
    if (props.provider === "custom") {
      request.custom_config_id = props.selectedCustomId;
    }
    storyboard.value = await apiJson<Storyboard>(
      "/api/director/storyboard",
      { method: "POST", body: JSON.stringify(request) },
      "分镜生成失败",
    );
    videoTitle.value = storyboard.value.video_title;
    status.value = `已拆出 ${storyboard.value.shots.length} 个镜头，可在表格中继续修改。`;
  } catch (cause) {
    error.value = "分镜生成失败：" + errorMessage(cause);
    status.value = "";
  } finally {
    endActivity();
    busy.value = false;
  }
}

async function downloadWorkbook(): Promise<void> {
  if (!storyboard.value) return;
  error.value = "";
  exporting.value = true;
  try {
    const response = await apiFetch("/api/director/export", {
      method: "POST",
      body: JSON.stringify(storyboard.value),
    });
    if (!response.ok) {
      const body = await response.text();
      let detail = "";
      try {
        const parsed = JSON.parse(body) as { detail?: string };
        detail = parsed.detail || "";
      } catch {
        detail = "";
      }
      throw new Error(detail || `导出失败（HTTP ${response.status}）`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download =
      storyboard.value.video_title.replace(/[<>:"/\\|?*]+/g, "-").trim() ||
      "分镜脚本";
    link.download += ".xlsx";
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.value = "Excel 分镜表已下载。";
  } catch (cause) {
    error.value = "导出失败：" + errorMessage(cause);
  } finally {
    exporting.value = false;
  }
}
</script>

<template>
  <section class="director-page">
    <header class="director-heading">
      <div class="director-heading-copy">
        <p class="director-kicker">剧本工作台 / 单集</p>
        <h1>把一场戏，拆成可拍的镜头。</h1>
        <p class="director-intro">
          导演 Agent 按剧情节拍拆镜，保留对白与人物关系，再整理成可编辑、可交付的分镜表。
        </p>
      </div>
      <div class="director-model-picker">
        <label for="director-provider">导演模型</label>
        <select
          id="director-provider"
          :value="providerSelection"
          :disabled="busy"
          @change="selectModel"
        >
          <option value="openai">OpenAI 兼容模型</option>
          <option value="llama">llama.cpp 本地模型</option>
          <option
            v-for="model in customModels"
            :key="model.id"
            :value="`custom:${model.id}`"
          >
            {{ model.name }}
          </option>
        </select>
        <span>使用创作工作区已配置的模型</span>
      </div>
    </header>

    <div class="director-workbench">
      <aside class="script-panel">
        <div class="panel-heading">
          <div>
            <span class="panel-index">输入</span>
            <h2>剧本</h2>
          </div>
          <span class="single-episode">一次一集</span>
        </div>

        <label class="field-label" for="director-title">集名或视频名称</label>
        <input
          id="director-title"
          v-model="videoTitle"
          class="title-input"
          maxlength="128"
          placeholder="可留空，由导演根据剧本拟定"
          :disabled="busy"
        />

        <label class="field-label script-label" for="director-script">剧本原文</label>
        <textarea
          id="director-script"
          v-model="script"
          class="script-input"
          maxlength="50000"
          placeholder="粘贴一集或一段剧本。保留场景标题、动作和对白，导演会据此拆分镜头。"
          :disabled="busy"
        />
        <div class="script-footer">
          <span>{{ script.length.toLocaleString() }} / 50,000 字符</span>
          <span>生成结果可逐格修改</span>
        </div>

        <section class="skill-panel" aria-label="分镜 Skill">
          <div class="skill-heading">
            <div>
              <span class="field-label">分镜 Skill</span>
              <p>可组合辅助规则；导入只读取 SKILL.md，不上传或运行其他文件。</p>
            </div>
            <div class="skill-add-actions">
              <button
                class="skill-add-button"
                type="button"
                :disabled="busy || skillSaving"
                @click="skillFolderInput?.click()"
              >
                导入文件夹
              </button>
              <button
                class="skill-add-button"
                type="button"
                :disabled="busy || skillSaving"
                @click="openSkillForm()"
              >
                手动添加
              </button>
            </div>
          </div>
          <input
            ref="skillFolderInput"
            class="skill-folder-input"
            type="file"
            webkitdirectory
            directory
            multiple
            @change="handleSkillFolderChange"
          />
          <p v-if="skillsLoading" class="skill-empty">正在读取 Skill…</p>
          <div v-else-if="skills.length" class="skill-list">
            <div
              v-for="skill in skills"
              :key="skill.id"
              class="skill-row"
            >
              <label class="skill-option">
                <input
                  v-model="selectedSkillIds"
                  type="checkbox"
                  :value="skill.id"
                  :disabled="
                    busy ||
                    skillSaving ||
                    (!selectedSkillIds.includes(skill.id) &&
                      selectedSkillIds.length >= maxSelectedSkills)
                  "
                />
                <span class="skill-option-copy">
                  <strong>{{ skill.name }}</strong>
                  <small>{{ skill.instructions }}</small>
                </span>
              </label>
              <div class="skill-row-actions">
                <button
                  type="button"
                  :disabled="busy || skillSaving"
                  :aria-label="`编辑 Skill ${skill.name}`"
                  @click="openSkillForm(skill)"
                >
                  编辑
                </button>
                <button
                  type="button"
                  :disabled="busy || skillSaving"
                  :aria-label="`删除 Skill ${skill.name}`"
                  @click="void deleteSkill(skill)"
                >
                  删除
                </button>
              </div>
            </div>
          </div>
          <p v-else class="skill-empty">
            还没有 Skill。添加镜头风格或拆分规则，再勾选用于分镜。
          </p>
          <p class="skill-count">
            已选 {{ selectedSkillIds.length }} / {{ maxSelectedSkills }}
          </p>
          <form
            v-if="skillFormOpen"
            class="skill-form"
            @submit.prevent="void saveSkill()"
          >
            <label>
              Skill 名称
              <input
                v-model="skillForm.name"
                maxlength="64"
                required
                :disabled="skillSaving"
                placeholder="例如：悬疑节奏"
              />
            </label>
            <label>
              指导内容
              <textarea
                v-model="skillForm.instructions"
                :maxlength="maxSkillInstructionsLength"
                required
                rows="4"
                :disabled="skillSaving"
                placeholder="描述拆镜、构图、镜头节奏或提示词方面的要求。"
              />
            </label>
            <div class="skill-form-footer">
              <span>
                {{ skillForm.instructions.length.toLocaleString() }} /
                {{ maxSkillInstructionsLength.toLocaleString() }}
              </span>
              <button
                type="button"
                :disabled="skillSaving"
                @click="closeSkillForm"
              >
                取消
              </button>
              <button type="submit" :disabled="skillSaving">
                {{ skillSaving ? "保存中…" : "保存 Skill" }}
              </button>
            </div>
          </form>
          <p v-if="skillError" class="director-error" role="alert">
            {{ skillError }}
          </p>
        </section>

        <button
          class="director-submit"
          type="button"
          :disabled="busy || !script.trim()"
          @click="void createStoryboard()"
        >
          <span v-if="busy" class="button-spinner" aria-hidden="true" />
          {{ busy ? "正在拆分镜头" : storyboard ? "重新拆分" : "拆分为分镜" }}
          <svg v-if="!busy" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 5.5h16v13H4zM8 5.5v13m8-13v13M2.5 9h3m13 0h3" />
          </svg>
        </button>
        <p class="director-status" aria-live="polite">{{ status }}</p>
        <p v-if="error" class="director-error" role="alert">{{ error }}</p>
      </aside>

      <section class="storyboard-board" aria-label="分镜表">
        <header class="board-heading">
          <div>
            <span class="panel-index">输出 / Excel 结构</span>
            <h2>{{ storyboard?.video_title || "分镜表" }}</h2>
          </div>
          <div v-if="storyboard" class="board-actions">
            <span class="shot-summary">
              {{ shotCount }} 镜 <i /> {{ totalDuration.toFixed(1) }} 秒
            </span>
            <button
              class="export-button"
              type="button"
              :disabled="exporting"
              @click="void downloadWorkbook()"
            >
              {{ exporting ? "正在导出…" : "下载 Excel" }}
            </button>
          </div>
        </header>

        <div v-if="!storyboard" class="empty-board">
          <div class="empty-board-grid" aria-hidden="true">
            <span /><span /><span />
          </div>
          <h3>{{ busy ? "正在读剧本" : "分镜表会出现在这里" }}</h3>
          <p>
            {{ busy ? "先梳理场景和动作，再按样表字段整理每个镜头。" : "粘贴剧本后，导演会生成一张单集分镜表。" }}
          </p>
          <div class="empty-columns" aria-hidden="true">
            <span>镜号</span><span>景别</span><span>运镜</span><span>画面内容</span>
          </div>
        </div>

        <template v-else>
          <div class="storyboard-meta">
            <label>
              <span>视频名称</span>
              <input v-model="storyboard.video_title" aria-label="视频名称" />
            </label>
            <label>
              <span>视频主题</span>
              <input v-model="storyboard.theme" aria-label="视频主题" />
            </label>
            <label>
              <span>主要人物</span>
              <input v-model="storyboard.characters" aria-label="主要人物" />
            </label>
            <label>
              <span>拍摄道具</span>
              <input v-model="storyboard.props" aria-label="拍摄道具" />
            </label>
            <label>
              <span>拍摄时间</span>
              <input v-model="storyboard.shooting_time" aria-label="拍摄时间" />
            </label>
            <label>
              <span>截止时间</span>
              <input v-model="storyboard.deadline" aria-label="截止时间" />
            </label>
          </div>

          <div class="shot-table-wrap" role="region" aria-label="可横向滚动的分镜表" tabindex="0">
            <table class="shot-table">
              <thead>
                <tr>
                  <th>镜号</th>
                  <th>景别</th>
                  <th>运镜</th>
                  <th>画面内容</th>
                  <th>时长</th>
                  <th>台词</th>
                  <th>音效</th>
                  <th>提示词</th>
                  <th>制作人</th>
                  <th>预计提交时间</th>
                  <th>审核人</th>
                  <th>实际提交时间</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(shot, index) in storyboard.shots"
                  :key="`${shot.shot_no}-${index}`"
                >
                  <td class="shot-number">{{ shot.shot_no }}</td>
                  <td><input v-model="shot.framing" :aria-label="`第 ${index + 1} 镜景别`" /></td>
                  <td><textarea v-model="shot.camera_movement" :aria-label="`第 ${index + 1} 镜运镜`" rows="2" /></td>
                  <td class="visual-cell"><textarea v-model="shot.visual_description" :aria-label="`第 ${index + 1} 镜画面内容`" rows="3" /></td>
                  <td class="duration-cell">
                    <input
                      v-model.number="shot.duration_seconds"
                      :aria-label="`第 ${index + 1} 镜时长（秒）`"
                      type="number"
                      min="0.1"
                      max="300"
                      step="0.5"
                    />
                    <span>秒</span>
                  </td>
                  <td><textarea v-model="shot.dialogue" :aria-label="`第 ${index + 1} 镜台词`" rows="3" /></td>
                  <td><textarea v-model="shot.sound_effects" :aria-label="`第 ${index + 1} 镜音效`" rows="3" /></td>
                  <td class="prompt-cell"><textarea v-model="shot.prompt" :aria-label="`第 ${index + 1} 镜提示词`" rows="3" /></td>
                  <td><input v-model="shot.producer" :aria-label="`第 ${index + 1} 镜制作人`" /></td>
                  <td><input v-model="shot.expected_submission_time" :aria-label="`第 ${index + 1} 镜预计提交时间`" /></td>
                  <td><input v-model="shot.reviewer" :aria-label="`第 ${index + 1} 镜审核人`" /></td>
                  <td><input v-model="shot.actual_submission_time" :aria-label="`第 ${index + 1} 镜实际提交时间`" /></td>
                </tr>
              </tbody>
            </table>
          </div>
          <footer class="board-footer">
            <span>制作与审核信息未在剧本中出现，已留空，导出前可补充。</span>
            <span>修改会随 Excel 下载一并导出</span>
          </footer>
        </template>
      </section>
    </div>
  </section>
</template>

<style scoped>
.director-page {
  --director-ink: #263a38;
  --director-muted: #60736e;
  --director-line: #c8d2ca;
  --director-ground: #e3e9e3;
  --director-paper: #f7f8f3;
  --director-accent: #b64e3d;
  padding: 28px clamp(18px, 3vw, 42px) 32px;
  color: var(--director-ink);
}

.director-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 30px;
  margin-bottom: 24px;
}

.director-heading-copy {
  max-width: 690px;
}

.director-kicker,
.panel-index {
  margin: 0;
  color: var(--director-muted);
  font-size: 12px;
  font-weight: 700;
}

.director-heading h1 {
  margin: 8px 0 6px;
  color: var(--director-ink);
  font-size: clamp(25px, 2.4vw, 34px);
  line-height: 1.2;
  letter-spacing: -0.035em;
}

.director-intro {
  max-width: 640px;
  margin: 0;
  color: var(--director-muted);
  font-size: 14px;
}

.director-model-picker {
  display: grid;
  gap: 6px;
  width: min(280px, 34%);
  flex-shrink: 0;
}

.director-model-picker label,
.field-label {
  color: var(--director-ink);
  font-size: 12px;
  font-weight: 700;
}

.director-model-picker select,
.title-input {
  min-height: 42px;
  padding: 0 12px;
  border: 1px solid var(--director-line);
  border-radius: 7px;
  color: var(--director-ink);
  background: var(--director-paper);
}

.director-model-picker span {
  color: var(--director-muted);
  font-size: 11px;
}

.director-workbench {
  display: grid;
  grid-template-columns: minmax(270px, 0.72fr) minmax(0, 1.65fr);
  align-items: start;
  gap: 18px;
}

.script-panel,
.storyboard-board {
  min-width: 0;
  border: 1px solid var(--director-line);
  border-radius: 10px;
  background: var(--director-paper);
}

.script-panel {
  display: flex;
  flex-direction: column;
  padding: 20px;
}

.panel-heading,
.board-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
}

.panel-heading {
  margin-bottom: 22px;
}

.panel-heading h2,
.board-heading h2 {
  margin: 3px 0 0;
  color: var(--director-ink);
  font-size: 20px;
  line-height: 1.3;
}

.single-episode {
  padding: 5px 8px;
  border: 1px solid #d5ddd6;
  border-radius: 5px;
  color: var(--director-muted);
  font-size: 11px;
  white-space: nowrap;
}

.field-label {
  margin-bottom: 7px;
}

.title-input {
  width: 100%;
  margin-bottom: 18px;
}

.script-input {
  min-height: 390px;
  width: 100%;
  resize: vertical;
  padding: 13px;
  border: 1px solid var(--director-line);
  border-radius: 7px;
  color: var(--director-ink);
  background: #edf1ec;
  line-height: 1.7;
}

.script-input::placeholder {
  color: #82908a;
}

.script-footer {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin: 8px 0 16px;
  color: var(--director-muted);
  font-size: 11px;
}

.skill-panel {
  margin: 0 0 16px;
  padding: 12px;
  border: 1px solid var(--director-line);
  border-radius: 7px;
  background: #eff3ee;
}

.skill-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.skill-add-actions {
  display: flex;
  flex-shrink: 0;
  gap: 4px;
}

.skill-folder-input {
  display: none;
}

.skill-heading p,
.skill-empty {
  margin: 4px 0 0;
  color: var(--director-muted);
  font-size: 11px;
  line-height: 1.5;
}

.skill-add-button,
.skill-row-actions button,
.skill-form-footer button {
  padding: 5px 8px;
  border: 1px solid var(--director-line);
  border-radius: 4px;
  color: var(--director-ink);
  background: var(--director-paper);
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}

.skill-add-button:hover:not(:disabled),
.skill-row-actions button:hover:not(:disabled),
.skill-form-footer button:hover:not(:disabled) {
  border-color: var(--director-accent);
  color: var(--director-accent);
}

.skill-list {
  display: grid;
  max-height: 240px;
  gap: 4px;
  overflow: auto;
  margin-top: 10px;
}

.skill-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 7px 6px;
  border-top: 1px solid #d8e0d9;
}

.skill-option {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 7px;
  cursor: pointer;
}

.skill-option input {
  margin: 3px 0 0;
  accent-color: var(--director-accent);
}

.skill-option-copy {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.skill-option-copy strong {
  overflow: hidden;
  color: var(--director-ink);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-option-copy small {
  display: -webkit-box;
  overflow: hidden;
  color: var(--director-muted);
  font-size: 10px;
  line-height: 1.4;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.skill-row-actions {
  display: flex;
  flex-shrink: 0;
  gap: 3px;
}

.skill-count {
  margin: 7px 0 0;
  color: var(--director-muted);
  font-size: 10px;
  text-align: right;
}

.skill-form {
  display: grid;
  gap: 9px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--director-line);
}

.skill-form label {
  display: grid;
  gap: 5px;
  color: var(--director-ink);
  font-size: 11px;
  font-weight: 700;
}

.skill-form input,
.skill-form textarea {
  width: 100%;
  padding: 8px;
  border: 1px solid var(--director-line);
  border-radius: 4px;
  color: var(--director-ink);
  background: var(--director-paper);
  font: inherit;
  font-weight: 400;
}

.skill-form textarea {
  resize: vertical;
  line-height: 1.5;
}

.skill-form-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 5px;
}

.skill-form-footer span {
  margin-right: auto;
  color: var(--director-muted);
  font-size: 10px;
}

.skill-panel :focus-visible {
  outline: 2px solid rgba(182, 78, 61, 0.55);
  outline-offset: 2px;
}

.director-submit,
.export-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  min-height: 43px;
  border: 0;
  border-radius: 6px;
  color: #fff;
  background: var(--director-accent);
  font-weight: 700;
}

.director-submit {
  width: 100%;
}

.director-submit:hover:not(:disabled),
.export-button:hover:not(:disabled) {
  background: #963c30;
}

.director-submit svg {
  width: 18px;
  fill: none;
  stroke: currentColor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.7;
}

.button-spinner {
  width: 15px;
  height: 15px;
  border: 2px solid rgba(255, 255, 255, 0.45);
  border-top-color: #fff;
  border-radius: 50%;
  animation: director-spin 0.8s linear infinite;
}

@keyframes director-spin {
  to { transform: rotate(360deg); }
}

.director-status,
.director-error {
  min-height: 20px;
  margin: 10px 0 0;
  font-size: 12px;
}

.director-status {
  color: var(--director-muted);
}

.director-error {
  color: #a3332d;
}

.storyboard-board {
  overflow: hidden;
}

.board-heading {
  min-height: 74px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--director-line);
  background: #eff3ee;
}

.board-heading h2 {
  max-width: min(48vw, 620px);
  overflow: hidden;
  font-size: 17px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.board-actions {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}

.shot-summary {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: var(--director-muted);
  font-size: 12px;
  white-space: nowrap;
}

.shot-summary i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--director-accent);
}

.export-button {
  min-height: 36px;
  padding: 0 12px;
  font-size: 12px;
  white-space: nowrap;
}

.empty-board {
  display: flex;
  min-height: 400px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 32px;
  color: var(--director-muted);
  text-align: center;
}

.empty-board-grid {
  display: grid;
  grid-template-columns: repeat(3, 27px);
  gap: 5px;
  margin-bottom: 17px;
}

.empty-board-grid span {
  height: 34px;
  border: 1px solid #aebdb2;
  background: #e8eee8;
}

.empty-board-grid span:first-child {
  border-top: 3px solid var(--director-accent);
}

.empty-board h3 {
  margin: 0;
  color: var(--director-ink);
  font-size: 16px;
}

.empty-board p {
  max-width: 340px;
  margin: 7px 0 20px;
  font-size: 13px;
}

.empty-columns {
  display: flex;
  gap: 0;
  overflow: hidden;
  border: 1px solid var(--director-line);
  border-radius: 4px;
  color: #526762;
  background: #eff3ee;
  font-size: 11px;
}

.empty-columns span {
  padding: 7px 10px;
  border-right: 1px solid var(--director-line);
}

.empty-columns span:last-child {
  border: 0;
}

.storyboard-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 16px;
  padding: 8px 18px 14px;
  border-bottom: 1px solid var(--director-line);
}

.storyboard-meta label {
  display: grid;
  min-width: 0;
  gap: 2px;
  padding: 7px 0 4px;
}

.storyboard-meta label span {
  color: var(--director-muted);
  font-size: 10px;
  font-weight: 700;
}

.storyboard-meta input {
  min-width: 0;
  padding: 3px 0;
  border: 0;
  border-bottom: 1px solid transparent;
  border-radius: 0;
  color: var(--director-ink);
  background: transparent;
  font-size: 12px;
}

.storyboard-meta input:focus {
  border-bottom-color: var(--director-accent);
  outline: 0;
}

.shot-table-wrap {
  width: 100%;
  overflow: auto;
  scrollbar-color: #98aaa0 #e7ece7;
}

.shot-table {
  width: 100%;
  min-width: 1920px;
  border-collapse: separate;
  border-spacing: 0;
  table-layout: fixed;
}

.shot-table th,
.shot-table td {
  padding: 9px 8px;
  border-right: 1px solid #d8e0d9;
  border-bottom: 1px solid #d8e0d9;
  vertical-align: top;
}

.shot-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  height: 42px;
  color: #edf3ee;
  background: #344b47;
  font-size: 11px;
  font-weight: 700;
  text-align: left;
  white-space: nowrap;
}

.shot-table th:nth-child(1) { width: 64px; }
.shot-table th:nth-child(2) { width: 108px; }
.shot-table th:nth-child(3) { width: 150px; }
.shot-table th:nth-child(4) { width: 260px; }
.shot-table th:nth-child(5) { width: 80px; }
.shot-table th:nth-child(6) { width: 220px; }
.shot-table th:nth-child(7) { width: 200px; }
.shot-table th:nth-child(8) { width: 310px; }
.shot-table th:nth-child(9),
.shot-table th:nth-child(11) { width: 115px; }
.shot-table th:nth-child(10),
.shot-table th:nth-child(12) { width: 155px; }

.shot-table tbody tr:nth-child(even) {
  background: #edf2ed;
}

.shot-table tbody tr:nth-child(odd) {
  background: var(--director-paper);
}

.shot-table td {
  color: var(--director-ink);
  font-size: 12px;
}

.shot-table .shot-number {
  color: #fff;
  background: var(--director-accent);
  font-size: 13px;
  font-weight: 800;
  text-align: center;
}

.shot-table input,
.shot-table textarea {
  display: block;
  width: 100%;
  min-height: 28px;
  padding: 3px;
  border: 1px solid transparent;
  border-radius: 3px;
  color: inherit;
  background: transparent;
  font-size: 12px;
  line-height: 1.5;
  resize: vertical;
}

.shot-table input:focus,
.shot-table textarea:focus {
  border-color: #91a69a;
  outline: 2px solid rgba(182, 78, 61, 0.2);
  background: #fff;
}

.duration-cell {
  white-space: nowrap;
}

.duration-cell input {
  display: inline-block;
  width: 46px;
  padding-right: 0;
}

.duration-cell span {
  color: var(--director-muted);
  font-size: 11px;
}

.board-footer {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 16px;
  color: var(--director-muted);
  font-size: 11px;
}

.shot-table-wrap:focus-visible {
  outline: 3px solid rgba(182, 78, 61, 0.32);
  outline-offset: -3px;
}

@media (max-width: 1120px) {
  .director-workbench {
    grid-template-columns: minmax(260px, 0.65fr) minmax(0, 1.35fr);
  }

  .script-panel {
    padding: 16px;
  }
}

@media (max-width: 860px) {
  .director-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .director-model-picker {
    width: min(100%, 360px);
  }

  .director-workbench {
    grid-template-columns: minmax(0, 1fr);
  }

  .script-input {
    min-height: 260px;
  }
}

@media (max-width: 560px) {
  .director-page {
    padding: 20px 12px 26px;
  }

  .director-heading h1 {
    font-size: 26px;
  }

  .board-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .board-heading h2 {
    max-width: calc(100vw - 56px);
  }

  .board-actions {
    width: 100%;
    justify-content: space-between;
  }

  .storyboard-meta {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    padding-right: 12px;
    padding-left: 12px;
  }

  .board-footer {
    flex-direction: column;
    gap: 4px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .button-spinner {
    animation: none;
  }
}
</style>
