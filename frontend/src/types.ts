export type Provider = "openai" | "llama" | "custom";
export type GenerationBackend = "comfyui" | "runninghub";
export type ChatRole = "user" | "assistant" | "system";
export type MediaKind = "image" | "video";

export interface AgentHistoryEntry {
  role: "user" | "assistant";
  content: string;
}

export interface AgentMedia {
  kind: MediaKind;
  url: string;
}

export interface AgentMessage {
  id: string;
  role: ChatRole;
  text: string;
  media: AgentMedia[];
  notices: string[];
}

export interface CustomModel {
  id: number;
  name: string;
  base_url: string;
  model: string;
}

export interface RunningHubField {
  node_id: string;
  node_title: string;
  field_name: string;
}

export interface RunningHubConfig {
  configured: boolean;
  workflow_id?: string | null;
  prompt_node_id?: string | null;
  prompt_field?: string | null;
  negative_prompt_node_id?: string | null;
  negative_prompt_field?: string | null;
}

export interface RunningHubMapping {
  node_id: string;
  field_name: string;
}

export interface AgentStep {
  tool: string;
  result?: string;
}

export interface AgentFile {
  filename: string;
  kind: MediaKind;
}

export interface GalleryEntry {
  kind: MediaKind;
  url: string;
  label: string;
}

export interface AgentPreference {
  style: string;
  workflow: string;
  model_preset: string;
  aspect_ratio: string;
  megapixels: string | number;
  negative_prompt: string;
  notes: string;
}

export interface WorkflowOption {
  file: string;
}

export interface AutoImageEpisodeOption {
  episode_index: number;
  title: string;
  shot_count: number;
}

export interface AutoImageAsset {
  id: number;
  asset_key: string;
  category: "character" | "scene" | "prop";
  name: string;
  episode_indices: number[];
  prompt: string;
  status: string;
  error?: string | null;
  task_id?: number | null;
  image_url?: string | null;
}

export interface AutoImageEvent {
  id: number;
  event_type: string;
  level: string;
  message: string;
  duration_ms?: number | null;
  created_at: string;
}

export interface AutoImageBatch {
  id: number;
  title: string;
  source_filename: string;
  episode_count: number;
  selected_episode_index: number;
  status: string;
  episodes: AutoImageEpisodeOption[];
  assets: AutoImageAsset[];
  logs: AutoImageEvent[];
  completed_assets: number;
  total_assets: number;
  created_at: string;
  updated_at: string;
}

export interface ModelPresetOption {
  name: string;
  display_name: string;
}

export interface RagDocument {
  filename: string;
  dynamic: boolean;
}

export interface InstanceCheck {
  id: string;
  status: string;
  label: string;
  message: string;
}

export interface InstanceDiagnostics {
  status: string;
  checks: InstanceCheck[];
}

export interface ImageRecord {
  media_type: MediaKind;
  file_url: string;
  task_id: number | string;
}

export interface AgentFileRecord extends AgentFile {
  created_at: number;
}

export interface AgentProgress {
  steps?: AgentStep[];
  current_tool?: string | null;
  running?: boolean;
  review_attempt?: number | null;
  max_review_attempts?: number | null;
}

export interface AgentConfirmation {
  confirmation_id: string;
  tool: string;
  arguments?: {
    prompt_ids?: string[];
    unload_models?: boolean;
    filename?: string;
  };
}

export interface AgentTaskStatus {
  state: string;
  confirmation?: AgentConfirmation | null;
  progress?: AgentProgress | null;
  result?: {
    status?: string;
    error?: string;
    answer?: string;
    steps?: AgentStep[];
  } | string;
}

export interface VideoEpisodeAsset {
  id: string;
  filename: string;
  media_type: "image" | "video" | "audio";
  size: number;
  file_url: string;
}

export interface VideoEpisodeShot {
  shot_index: number;
  shot_no: string;
  framing: string;
  camera_movement: string;
  visual_description: string;
  duration_seconds: number;
  dialogue: string;
  sound_effects: string;
  prompt: string;
  asset_ids: string[];
  suggested_asset_ids: string[];
  match_confidence: number;
  match_source: string;
  status: string;
  segment_count: number;
  error?: string | null;
}

export interface VideoEpisode {
  id: number;
  title: string;
  status: string;
  assets: VideoEpisodeAsset[];
  shots: VideoEpisodeShot[];
  aspect_ratio: string;
  megapixels: number;
  generation_backend: GenerationBackend;
  workflow_config: Record<string, unknown>;
  current_shot_index: number;
  total_shots: number;
  progress_message: string;
  mapping_warning?: string | null;
  error?: string | null;
  output_url?: string | null;
  created_at: string;
  updated_at: string;
}

export interface VideoEpisodeSummary {
  id: number;
  title: string;
  status: string;
  current_shot_index: number;
  total_shots: number;
  progress_message: string;
  created_at: string;
  updated_at: string;
}

export interface VideoSeriesEpisode {
  folder_name: string;
  episode: VideoEpisode;
}

export interface VideoSeries {
  id: number;
  title: string;
  status: string;
  total_episodes: number;
  current_episode_index: number;
  progress_message: string;
  error?: string | null;
  episodes: VideoSeriesEpisode[];
  created_at: string;
  updated_at: string;
}

export interface VideoSeriesSummary {
  id: number;
  title: string;
  status: string;
  total_episodes: number;
  current_episode_index: number;
  progress_message: string;
  created_at: string;
  updated_at: string;
}
