import { computed, ref } from "vue";

const STORAGE_PREFIX = "ai-studio:agent-activity:";
const STORAGE_TTL_MS = 120_000;
const tabId =
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const ownStorageKey = `${STORAGE_PREFIX}${tabId}`;
const activeActivities = new Set<number>();
const localActivityCount = ref(0);
const remoteActivity = ref(false);

let nextActivityId = 0;
let heartbeatTimer: number | undefined;

export const agentIsBusy = computed(
  () => localActivityCount.value > 0 || remoteActivity.value,
);

function publishLocalActivity(): void {
  if (typeof window === "undefined") return;
  try {
    if (activeActivities.size) {
      window.localStorage.setItem(
        ownStorageKey,
        JSON.stringify({ updatedAt: Date.now() }),
      );
    } else {
      window.localStorage.removeItem(ownStorageKey);
    }
  } catch {
    // Keep the in-tab indicator working when storage is unavailable.
  }
}

function refreshRemoteActivity(): void {
  if (typeof window === "undefined") return;
  const now = Date.now();
  let foundActivity = false;
  const expiredKeys: string[] = [];

  try {
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (!key?.startsWith(STORAGE_PREFIX) || key === ownStorageKey) continue;

      const raw = window.localStorage.getItem(key);
      if (!raw) continue;
      try {
        const value = JSON.parse(raw) as { updatedAt?: unknown };
        const updatedAt =
          typeof value.updatedAt === "number" ? value.updatedAt : 0;
        if (now - updatedAt <= STORAGE_TTL_MS) {
          foundActivity = true;
        } else {
          expiredKeys.push(key);
        }
      } catch {
        expiredKeys.push(key);
      }
    }
    for (const key of expiredKeys) window.localStorage.removeItem(key);
  } catch {
    foundActivity = false;
  }

  remoteActivity.value = foundActivity;
}

function refreshActivity(): void {
  localActivityCount.value = activeActivities.size;
  refreshRemoteActivity();
}

function stopHeartbeat(): void {
  if (heartbeatTimer !== undefined) {
    window.clearInterval(heartbeatTimer);
    heartbeatTimer = undefined;
  }
}

function startHeartbeat(): void {
  if (heartbeatTimer !== undefined || typeof window === "undefined") return;
  publishLocalActivity();
  heartbeatTimer = window.setInterval(() => {
    publishLocalActivity();
    refreshRemoteActivity();
  }, 20_000);
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (!event.key || event.key.startsWith(STORAGE_PREFIX)) {
      refreshRemoteActivity();
    }
  });
  window.addEventListener("pagehide", () => {
    stopHeartbeat();
    try {
      window.localStorage.removeItem(ownStorageKey);
    } catch {
      // The current tab still has its in-memory activity state.
    }
  });
  window.addEventListener("pageshow", () => {
    if (activeActivities.size) startHeartbeat();
    refreshActivity();
  });
  window.setInterval(refreshRemoteActivity, 10_000);
  refreshRemoteActivity();
}

export function beginAgentActivity(): () => void {
  const activityId = ++nextActivityId;
  activeActivities.add(activityId);
  refreshActivity();
  startHeartbeat();

  let ended = false;
  return () => {
    if (ended) return;
    ended = true;
    activeActivities.delete(activityId);
    if (!activeActivities.size) stopHeartbeat();
    publishLocalActivity();
    refreshActivity();
  };
}
