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

export interface ModelPresetOption {
  name: string;
  display_name: string;
}

export interface RagDocument {
  filename: string;
  dynamic: boolean;
}

export interface InstanceCheck {
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
