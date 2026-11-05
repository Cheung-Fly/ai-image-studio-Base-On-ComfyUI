<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { agentIsBusy } from "../agentActivity";

const POSITION_STORAGE_KEY = "ai-studio:panda-position:v1";
const PANDA_WIDTH = 156;
const PANDA_HEIGHT = 196;
const position = ref({ x: 0, y: 0 });
const bubbleOpen = ref(false);
const bubbleText = ref("竹子真脆！今天也陪你一起创作。");
const walking = ref(false);
const facingLeft = ref(false);
const beingPetted = ref(false);
const gaze = ref({ x: 0, y: 0 });
const dragging = ref<{
  pointerId: number;
  pointerX: number;
  pointerY: number;
  startX: number;
  startY: number;
  moved: boolean;
} | null>(null);

let ignoreNextClick = false;
let reducedMotion = false;
let wanderTimer: number | undefined;
let walkTimer: number | undefined;
let bubbleTimer: number | undefined;
let petTimer: number | undefined;
let nextGreeting = 0;

const idleGreetings = [
  "竹子真脆！今天也陪你一起创作。",
  "摸摸头？我会开心地转一圈。",
  "灵感来啦！等我先啃完这一口。",
  "别着急，我一直在这儿陪你。",
];

const positionStyle = computed(() => ({
  left: `${position.value.x}px`,
  top: `${position.value.y}px`,
  "--gaze-x": `${gaze.value.x}px`,
  "--gaze-y": `${gaze.value.y}px`,
}));
const bubbleBelow = computed(() => position.value.y < window.innerHeight / 2);
const bubbleOnLeft = computed(() => position.value.x > window.innerWidth / 2);
const statusText = computed(() => {
  if (agentIsBusy.value) return "工作中";
  if (walking.value) return "散步中";
  return "吃竹子";
});
const interactionLabel = computed(() =>
  agentIsBusy.value
    ? "团团正在电脑前工作。点击互动，拖动移动，方向键也可以移动。"
    : "团团正在吃竹子。点击互动，拖动移动，方向键也可以移动。",
);

function clampPosition(x: number, y: number) {
  return {
    x: Math.max(4, Math.min(x, window.innerWidth - PANDA_WIDTH - 4)),
    y: Math.max(4, Math.min(y, window.innerHeight - PANDA_HEIGHT - 4)),
  };
}

function savePosition(): void {
  try {
    window.localStorage.setItem(
      POSITION_STORAGE_KEY,
      JSON.stringify(position.value),
    );
  } catch {
    // Keep the pet movable if browser storage is unavailable.
  }
}

function moveTo(x: number, y: number): void {
  position.value = clampPosition(x, y);
  savePosition();
}

function clearWanderTimers(): void {
  if (wanderTimer !== undefined) window.clearTimeout(wanderTimer);
  if (walkTimer !== undefined) window.clearTimeout(walkTimer);
  wanderTimer = undefined;
  walkTimer = undefined;
}

function scheduleWander(delay = 4500 + Math.random() * 4500): void {
  clearWanderTimers();
  if (
    reducedMotion ||
    agentIsBusy.value ||
    dragging.value ||
    document.hidden
  ) {
    walking.value = false;
    return;
  }

  wanderTimer = window.setTimeout(() => {
    const minY = Math.max(8, Math.floor(window.innerHeight * 0.4));
    const maxY = Math.max(minY, window.innerHeight - PANDA_HEIGHT - 72);
    const nextX = clampPosition(
      position.value.x + (Math.random() * 2 - 1) * 230,
      position.value.y,
    ).x;
    const nextY = Math.min(
      maxY,
      Math.max(minY, position.value.y + (Math.random() * 2 - 1) * 64),
    );

    facingLeft.value = nextX < position.value.x;
    walking.value = true;
    moveTo(nextX, nextY);
    walkTimer = window.setTimeout(() => {
      walking.value = false;
      scheduleWander(4000 + Math.random() * 5000);
    }, 1750);
  }, delay);
}

function showBubble(message: string): void {
  bubbleText.value = message;
  bubbleOpen.value = true;
  if (bubbleTimer !== undefined) window.clearTimeout(bubbleTimer);
  bubbleTimer = window.setTimeout(() => {
    bubbleOpen.value = false;
    bubbleTimer = undefined;
  }, 4200);
}

function handlePointerDown(event: PointerEvent): void {
  if (event.button !== 0) return;
  event.preventDefault();
  clearWanderTimers();
  walking.value = false;
  ignoreNextClick = false;
  dragging.value = {
    pointerId: event.pointerId,
    pointerX: event.clientX,
    pointerY: event.clientY,
    startX: position.value.x,
    startY: position.value.y,
    moved: false,
  };
  (event.currentTarget as HTMLButtonElement).setPointerCapture(event.pointerId);
}

function handlePointerMove(event: PointerEvent): void {
  const activeDrag = dragging.value;
  if (!activeDrag || activeDrag.pointerId !== event.pointerId) return;
  const deltaX = event.clientX - activeDrag.pointerX;
  const deltaY = event.clientY - activeDrag.pointerY;
  if (Math.abs(deltaX) > 4 || Math.abs(deltaY) > 4) {
    activeDrag.moved = true;
    ignoreNextClick = true;
    facingLeft.value = deltaX < 0;
  }
  if (activeDrag.moved) {
    position.value = clampPosition(
      activeDrag.startX + deltaX,
      activeDrag.startY + deltaY,
    );
  }
}

function handlePointerUp(event: PointerEvent): void {
  if (dragging.value?.pointerId !== event.pointerId) return;
  dragging.value = null;
  savePosition();
  if (!agentIsBusy.value) scheduleWander(8500);
}

function handleClick(event: MouseEvent): void {
  if (ignoreNextClick && event.detail > 0) {
    ignoreNextClick = false;
    return;
  }
  ignoreNextClick = false;
  clearWanderTimers();
  walking.value = false;
  beingPetted.value = true;
  if (petTimer !== undefined) window.clearTimeout(petTimer);
  petTimer = window.setTimeout(() => {
    beingPetted.value = false;
    petTimer = undefined;
  }, 950);

  if (agentIsBusy.value) {
    showBubble("我在认真敲电脑，马上陪你聊。");
  } else {
    showBubble(idleGreetings[nextGreeting % idleGreetings.length]);
    nextGreeting += 1;
  }
  if (!agentIsBusy.value) scheduleWander(8500);
}

function handleKeydown(event: KeyboardEvent): void {
  const distance = event.shiftKey ? 36 : 18;
  const moves: Record<string, [number, number]> = {
    ArrowLeft: [-distance, 0],
    ArrowRight: [distance, 0],
    ArrowUp: [0, -distance],
    ArrowDown: [0, distance],
  };
  const move = moves[event.key];
  if (!move) return;
  event.preventDefault();
  clearWanderTimers();
  facingLeft.value = event.key === "ArrowLeft";
  moveTo(position.value.x + move[0], position.value.y + move[1]);
  if (!agentIsBusy.value) scheduleWander(8500);
}

function handleWindowPointerMove(event: PointerEvent): void {
  const centerX = position.value.x + 78;
  const centerY = position.value.y + 57;
  gaze.value = {
    x: Math.max(-2.2, Math.min(2.2, (event.clientX - centerX) / 80)),
    y: Math.max(-1.6, Math.min(1.6, (event.clientY - centerY) / 100)),
  };
}

function handleResize(): void {
  moveTo(position.value.x, position.value.y);
}

function handleVisibilityChange(): void {
  if (document.hidden) {
    clearWanderTimers();
    walking.value = false;
  } else if (!agentIsBusy.value) {
    scheduleWander(1800);
  }
}

watch(agentIsBusy, (busy) => {
  clearWanderTimers();
  walking.value = false;
  if (busy) {
    bubbleOpen.value = false;
  } else {
    scheduleWander(1800);
  }
});

onMounted(() => {
  let saved: { x?: unknown; y?: unknown } | null = null;
  try {
    saved = JSON.parse(
      window.localStorage.getItem(POSITION_STORAGE_KEY) || "null",
    ) as { x?: unknown; y?: unknown } | null;
  } catch {
    saved = null;
  }
  position.value =
    saved && typeof saved.x === "number" && typeof saved.y === "number"
      ? clampPosition(saved.x, saved.y)
      : clampPosition(
          window.innerWidth - PANDA_WIDTH - 22,
          window.innerHeight - PANDA_HEIGHT - 72,
        );
  reducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches;
  window.addEventListener("resize", handleResize);
  window.addEventListener("pointermove", handleWindowPointerMove, {
    passive: true,
  });
  document.addEventListener("visibilitychange", handleVisibilityChange);
  if (!agentIsBusy.value) scheduleWander(2800);
});

onUnmounted(() => {
  clearWanderTimers();
  if (bubbleTimer !== undefined) window.clearTimeout(bubbleTimer);
  if (petTimer !== undefined) window.clearTimeout(petTimer);
  window.removeEventListener("resize", handleResize);
  window.removeEventListener("pointermove", handleWindowPointerMove);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>

<template>
  <aside
    class="panda-pet"
    :class="{
      'panda-pet-busy': agentIsBusy,
      'panda-pet-walking': walking && !agentIsBusy,
      'panda-pet-facing-left': facingLeft,
      'panda-pet-petted': beingPetted,
      'panda-pet-dragging': dragging,
    }"
    :style="positionStyle"
  >
    <div
      v-if="bubbleOpen"
      class="panda-pet-bubble"
      :class="{
        'panda-pet-bubble-left': bubbleOnLeft,
        'panda-pet-bubble-below': bubbleBelow,
      }"
      role="status"
      aria-live="polite"
    >
      <strong>团团</strong>
      <span>{{ bubbleText }}</span>
    </div>
    <button
      class="panda-pet-button"
      type="button"
      :aria-label="interactionLabel"
      :aria-pressed="bubbleOpen"
      @pointerdown="handlePointerDown"
      @pointermove="handlePointerMove"
      @pointerup="handlePointerUp"
      @pointercancel="handlePointerUp"
      @click="handleClick"
      @keydown="handleKeydown"
    >
      <svg
        class="panda-pet-art"
        viewBox="0 0 156 196"
        role="img"
        :aria-label="agentIsBusy ? '团团正在电脑前工作' : '团团正在吃竹子'"
      >
        <defs>
          <radialGradient id="panda-coat" cx="42%" cy="28%" r="78%">
            <stop offset="0" stop-color="#fffefa" />
            <stop offset="0.76" stop-color="#f8f6ec" />
            <stop offset="1" stop-color="#e4e6d9" />
          </radialGradient>
          <linearGradient id="panda-ink" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#39443c" />
            <stop offset="1" stop-color="#222b27" />
          </linearGradient>
          <linearGradient id="panda-bamboo" x1="0" y1="1" x2="0.8" y2="0">
            <stop offset="0" stop-color="#688d52" />
            <stop offset="1" stop-color="#9cbd72" />
          </linearGradient>
          <linearGradient id="panda-cyan" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#72e4e6" />
            <stop offset="1" stop-color="#35aebc" />
          </linearGradient>
          <filter id="panda-shadow" x="-35%" y="-35%" width="170%" height="180%">
            <feDropShadow dx="0" dy="2" stdDeviation="2.4" flood-color="#344136" flood-opacity="0.14" />
          </filter>
        </defs>

        <ellipse class="panda-pet-shadow" cx="78" cy="188" rx="43" ry="5" />

        <g v-if="agentIsBusy" class="panda-pet-working-pose">
          <path
            class="panda-torso"
            d="M37 118c2-20 18-31 41-31s39 11 41 31l-4 49H41Z"
          />
          <path class="panda-vest" d="M42 101q36-20 72 0l11 48-15 12-32-14-32 14-15-12Z" />
          <path class="panda-vest-panel" d="m47 103 13 5-7 43-12 5-8-8Zm62 0-13 5 7 43 12 5 8-8Z" />
          <path class="panda-vest-seam" d="m50 111 8 3m48-3-8 3" />
          <circle class="panda-badge" cx="47" cy="126" r="6" />
          <path class="panda-badge-bamboo" d="m46 130 3-8m-2 5-3-3m5 1 3-3" />
          <ellipse class="panda-paw panda-work-arm-left" cx="47" cy="142" rx="12" ry="18" transform="rotate(22 47 142)" />
          <ellipse class="panda-paw panda-work-arm-right" cx="109" cy="142" rx="12" ry="18" transform="rotate(-22 109 142)" />
          <ellipse class="panda-foot" cx="54" cy="171" rx="23" ry="12" />
          <ellipse class="panda-foot" cx="102" cy="171" rx="23" ry="12" />
          <ellipse class="panda-toe" cx="43" cy="172" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="54" cy="174" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="65" cy="172" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="91" cy="172" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="102" cy="174" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="113" cy="172" rx="3" ry="2" />

          <g class="panda-laptop">
            <rect class="panda-screen-frame" x="31" y="119" width="94" height="55" rx="9" />
            <rect class="panda-screen" x="37" y="125" width="82" height="42" rx="5" />
            <circle class="panda-screen-light" cx="42" cy="130" r="1.7" />
            <circle class="panda-screen-light" cx="48" cy="130" r="1.7" />
            <circle class="panda-screen-light" cx="54" cy="130" r="1.7" />
            <path class="panda-screen-code" d="M44 140h29m-29 7h47m-47 7h34" />
            <path class="panda-laptop-base" d="m25 176 8-5h90l8 5-3 5H28Z" />
            <path class="panda-trackpad" d="M70 175h18l2 3H68Z" />
          </g>
          <ellipse class="panda-paw panda-typing-left" cx="54" cy="165" rx="12" ry="7" />
          <ellipse class="panda-paw panda-typing-right" cx="102" cy="165" rx="12" ry="7" />
        </g>

        <g v-else class="panda-pet-idle-pose" :class="{ 'panda-pet-step': walking }">
          <path
            class="panda-torso"
            d="M35 119c3-20 19-32 43-32s40 12 43 32l-4 49H39Z"
          />
          <path class="panda-belly" d="M54 111c0-11 10-19 24-19s24 8 24 19v48H54Z" />
          <path class="panda-scarf-tail" d="M48 111q-2 20-17 39l10 5q16-17 18-39Z" />
          <path class="panda-scarf-tail" d="M103 111q6 20 19 32l-8 7q-17-13-21-35Z" />
          <path class="panda-scarf" d="M37 105q41 22 82 0l-2 13q-39 22-78 0Z" />
          <path class="panda-scarf-fold" d="M44 112q34 18 68 0" />
          <ellipse class="panda-leg panda-step-leg-left" cx="57" cy="163" rx="22" ry="13" />
          <ellipse class="panda-leg panda-step-leg-right" cx="100" cy="163" rx="22" ry="13" />
          <ellipse class="panda-foot panda-step-foot-left" cx="48" cy="174" rx="20" ry="11" />
          <ellipse class="panda-foot panda-step-foot-right" cx="109" cy="174" rx="20" ry="11" />
          <ellipse class="panda-toe" cx="39" cy="174" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="48" cy="176" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="57" cy="174" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="100" cy="174" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="109" cy="176" rx="3" ry="2" />
          <ellipse class="panda-toe" cx="118" cy="174" rx="3" ry="2" />

          <g v-if="walking" class="panda-walking-arms">
            <ellipse class="panda-arm" cx="34" cy="124" rx="10" ry="18" transform="rotate(24 34 124)" />
            <ellipse class="panda-arm" cx="122" cy="124" rx="10" ry="18" transform="rotate(-24 122 124)" />
          </g>
          <g v-else class="panda-eating-arms">
            <path class="panda-arm" d="M43 111c-15 4-18 17-11 26 5 7 15 7 23 0l11-12-8-10Z" />
            <path class="panda-arm" d="M113 111c15 4 18 17 11 26-5 7-15 7-23 0l-11-12 8-10Z" />
            <ellipse class="panda-paw panda-holding-paw" cx="101" cy="109" rx="10" ry="8" transform="rotate(-25 101 109)" />
          </g>
          <g v-if="!walking" class="panda-bamboo-stick">
            <path d="M99 117 126 80" />
            <path class="panda-bamboo-joint" d="m107 105 5 4m5-12 5 4" />
            <path class="panda-leaf" d="M114 94c-2-12 4-20 15-21 1 11-4 18-15 21Z" />
            <path class="panda-leaf" d="M117 91c9-10 19-10 27-2-8 9-17 10-27 2Z" />
            <path class="panda-leaf" d="M121 84c-3-10 1-17 10-21 4 9 1 16-10 21Z" />
          </g>
        </g>

        <g class="panda-head" filter="url(#panda-shadow)">
          <path class="panda-headband" d="M28 66C24 26 45 8 78 8s54 18 50 58" />
          <circle class="panda-ear" cx="42" cy="43" r="21" />
          <circle class="panda-ear" cx="114" cy="43" r="21" />
          <circle class="panda-ear-inner" cx="42" cy="43" r="10" />
          <circle class="panda-ear-inner" cx="114" cy="43" r="10" />
          <ellipse class="panda-head-coat" cx="78" cy="65" rx="50" ry="44" />
          <g class="panda-headphones">
            <rect class="panda-earcup" x="25" y="49" width="17" height="29" rx="8" />
            <rect class="panda-earcup" x="114" y="49" width="17" height="29" rx="8" />
            <rect class="panda-earcup-inset" x="29" y="55" width="8" height="17" rx="4" />
            <rect class="panda-earcup-inset" x="119" y="55" width="8" height="17" rx="4" />
            <circle class="panda-headphone-light" cx="123" cy="63" r="2" />
            <path class="panda-microphone" d="M128 73q12 5 7 16l-12 7" />
            <circle class="panda-microphone-tip" cx="121" cy="97" r="3" />
          </g>
          <g v-if="agentIsBusy" class="panda-cap">
            <path class="panda-cap-crown" d="M34 39Q40 12 77 10q37 1 45 30l-5 8q-40-12-79 1Z" />
            <path class="panda-cap-brim" d="M35 42q42-13 87-2-9 10-43 10-29 0-44-8Z" />
            <path class="panda-cap-bamboo" d="m77 21 1 13m0-8-5-4m6 7 5-5m-6 10-5-4" />
          </g>
          <g v-else class="panda-tuft">
            <path d="M61 27q-3-12 7-16l5 11q4-14 14-13l-1 14q8-11 16-6l-7 14Z" />
            <path class="panda-tuft-shine" d="M68 20q4-5 6-2m10-4q4-2 6 1" />
          </g>
          <ellipse class="panda-eye-patch" cx="56" cy="65" rx="14" ry="18" transform="rotate(20 56 65)" />
          <ellipse class="panda-eye-patch" cx="100" cy="65" rx="14" ry="18" transform="rotate(-20 100 65)" />
          <ellipse class="panda-eye-white" cx="56" cy="65" rx="8" ry="10" />
          <ellipse class="panda-eye-white" cx="100" cy="65" rx="8" ry="10" />
          <path class="panda-brow" d="M43 48q11-7 22-2m26 0q11-5 22 2" />
          <circle
            class="panda-pupil"
            :cx="56 + gaze.x"
            :cy="(agentIsBusy ? 64 : 67) + gaze.y"
            r="5.3"
          />
          <circle
            class="panda-pupil"
            :cx="100 + gaze.x"
            :cy="(agentIsBusy ? 64 : 67) + gaze.y"
            r="5.3"
          />
          <circle class="panda-eye-glint" :cx="54 + gaze.x" :cy="(agentIsBusy ? 62 : 65) + gaze.y" r="1.7" />
          <circle class="panda-eye-glint" :cx="98 + gaze.x" :cy="(agentIsBusy ? 62 : 65) + gaze.y" r="1.7" />
          <path v-if="!agentIsBusy" class="panda-sleepy-lids" d="M46 64q10-9 20 0v-7H46Zm44 0q10-9 20 0v-7H90Z" />
          <ellipse class="panda-blush" cx="43" cy="82" rx="8" ry="4.3" />
          <ellipse class="panda-blush" cx="113" cy="82" rx="8" ry="4.3" />
          <ellipse class="panda-muzzle" cx="78" cy="82" rx="20" ry="14" />
          <ellipse class="panda-nose" cx="78" cy="77" rx="5.5" ry="4" />
          <path class="panda-smile" d="M78 81v5m0 0c-4 5-9 5-12 1m12-1c4 5 9 5 12 1" />
          <path class="panda-smile-working" d="M70 86c4 3 12 3 16 0" />
        </g>

        <g v-if="beingPetted" class="panda-hearts" aria-hidden="true">
          <path d="M23 45c-8-7-17 3 0 15 17-12 8-22 0-15Z" />
          <path d="M137 51c-6-5-13 2 0 12 13-10 6-17 0-12Z" />
        </g>
      </svg>
      <span class="panda-pet-caption" aria-hidden="true">
        <i :class="{ 'panda-status-busy': agentIsBusy }" />
        <b>团团</b>
        <span>{{ statusText }}</span>
      </span>
    </button>
  </aside>
</template>

<style scoped>
.panda-pet {
  position: fixed;
  z-index: 45;
  width: 156px;
  height: 196px;
  user-select: none;
  transition: left 1.6s cubic-bezier(0.2, 0.78, 0.25, 1),
    top 1.6s cubic-bezier(0.2, 0.78, 0.25, 1);
}

.panda-pet-dragging {
  z-index: 60;
  transition: none;
}

.panda-pet-button {
  position: relative;
  display: grid;
  width: 156px;
  height: 196px;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 48%;
  background: transparent;
  cursor: grab;
  touch-action: none;
}

.panda-pet-button:active {
  cursor: grabbing;
}

.panda-pet-button:focus-visible {
  outline: 3px solid #769572;
  outline-offset: -2px;
}

.panda-pet-art {
  display: block;
  width: 156px;
  height: 196px;
  overflow: visible;
  transform-origin: 78px 175px;
}

.panda-pet-facing-left .panda-pet-art {
  transform: scaleX(-1);
}

.panda-pet-shadow {
  fill: rgba(57, 71, 58, 0.16);
  transform-box: fill-box;
  transform-origin: center;
  animation: panda-shadow-breathe 2.8s ease-in-out infinite;
}

.panda-torso,
.panda-head-coat {
  fill: url("#panda-coat");
  stroke: #dedfd3;
  stroke-width: 1.5;
}

.panda-belly,
.panda-muzzle {
  fill: #fffefa;
}

.panda-headband {
  fill: none;
  stroke: #303b37;
  stroke-linecap: round;
  stroke-width: 5.5;
}

.panda-earcup {
  fill: url("#panda-cyan");
  stroke: #344b49;
  stroke-width: 2;
}

.panda-earcup-inset {
  fill: #2f4d49;
  stroke: #b6f3ee;
  stroke-width: 0.8;
}

.panda-headphone-light,
.panda-microphone-tip {
  fill: #a7f2e8;
}

.panda-headphone-light {
  opacity: 0.8;
}

.panda-microphone {
  fill: none;
  stroke: #344b49;
  stroke-linecap: round;
  stroke-width: 3;
}

.panda-cap-crown,
.panda-cap-brim {
  fill: #293633;
  stroke: #1d2826;
  stroke-linejoin: round;
  stroke-width: 1.4;
}

.panda-cap-brim {
  fill: #3b4b46;
}

.panda-cap-bamboo {
  fill: none;
  stroke: #9dc96e;
  stroke-linecap: round;
  stroke-width: 2.2;
}

.panda-tuft {
  fill: url("#panda-coat");
  stroke: #d7d9cd;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.2;
}

.panda-tuft-shine {
  fill: none;
  stroke: #fffefa;
  stroke-linecap: round;
  stroke-width: 1.5;
}

.panda-leg,
.panda-ear,
.panda-arm,
.panda-foot,
.panda-paw,
.panda-eye-patch {
  fill: url("#panda-ink");
}

.panda-ear-inner {
  fill: #586258;
}

.panda-toe {
  fill: #c9cbbf;
}

.panda-pupil,
.panda-nose {
  fill: #202824;
}

.panda-eye-white {
  fill: #fffefa;
}

.panda-pupil {
  transition: cx 90ms ease-out, cy 90ms ease-out;
}

.panda-eye-glint {
  fill: #fffefa;
  pointer-events: none;
}

.panda-blush {
  fill: #eaa5a1;
  opacity: 0.7;
}

.panda-smile,
.panda-smile-working {
  fill: none;
  stroke: #39413b;
  stroke-linecap: round;
  stroke-width: 1.8;
}

.panda-smile-working {
  display: none;
}

.panda-pet-busy .panda-smile {
  display: none;
}

.panda-pet-busy .panda-smile-working {
  display: block;
}

.panda-pet-busy .panda-blush {
  opacity: 0.45;
}

.panda-scarf {
  fill: #88ad68;
  stroke: #5f7e4c;
  stroke-width: 1.5;
}

.panda-scarf-fold {
  fill: none;
  stroke: #b4cf8d;
  stroke-linecap: round;
  stroke-width: 2;
}

.panda-scarf-tail {
  fill: #789a5b;
  stroke: #5f7e4c;
  stroke-linejoin: round;
  stroke-width: 1.4;
}

.panda-vest {
  fill: #35433e;
  stroke: #24312e;
  stroke-width: 1.5;
}

.panda-vest-panel {
  fill: #53665b;
  stroke: #718779;
  stroke-width: 1;
}

.panda-vest-seam {
  fill: none;
  stroke: #91a993;
  stroke-linecap: round;
  stroke-width: 1.2;
}

.panda-badge {
  fill: #a7cb70;
  stroke: #4e6f43;
  stroke-width: 1.5;
}

.panda-badge-bamboo {
  fill: none;
  stroke: #41653f;
  stroke-linecap: round;
  stroke-width: 1.1;
}

.panda-eating-arms,
.panda-bamboo-stick {
  transform-origin: 78px 116px;
  animation: panda-chew-sway 1.7s ease-in-out infinite;
}

.panda-arm {
  stroke: #313b34;
  stroke-width: 1;
}

.panda-holding-paw {
  stroke: #222b27;
  stroke-width: 1;
}

.panda-bamboo-stick > path:first-child {
  fill: none;
  stroke: url("#panda-bamboo");
  stroke-linecap: round;
  stroke-width: 7;
}

.panda-bamboo-joint {
  fill: none;
  stroke: #d7ce91;
  stroke-linecap: round;
  stroke-width: 1.7;
}

.panda-leaf {
  fill: #87a96b;
  stroke: #6d8d56;
  stroke-width: 1;
  transform-box: fill-box;
  transform-origin: left bottom;
  animation: panda-leaf-flutter 2.2s ease-in-out infinite alternate;
}

.panda-leaf:nth-of-type(3) {
  animation-delay: 280ms;
}

.panda-leaf:nth-of-type(4) {
  animation-delay: 560ms;
}

.panda-sleepy-lids {
  fill: #f8f6ec;
  transform-box: fill-box;
  transform-origin: center;
  animation: panda-blink 5.2s ease-in-out infinite;
}

.panda-brow {
  fill: none;
  stroke: #303731;
  stroke-linecap: round;
  stroke-width: 2.5;
}

.panda-pet-busy .panda-brow {
  stroke-width: 2.8;
}

.panda-pet-busy .panda-headphone-light {
  animation: panda-status-pulse 1.4s ease-in-out infinite;
}

.panda-pet-idle-pose {
  transform-box: fill-box;
  transform-origin: center bottom;
  animation: panda-idle-bob 2.8s ease-in-out infinite;
}

.panda-pet-walking .panda-pet-idle-pose {
  animation: panda-walk-bob 300ms ease-in-out infinite alternate;
}

.panda-pet-walking .panda-step-leg-left,
.panda-pet-walking .panda-step-foot-left {
  animation: panda-step-left 360ms ease-in-out infinite alternate;
  transform-box: fill-box;
  transform-origin: center;
}

.panda-pet-walking .panda-step-leg-right,
.panda-pet-walking .panda-step-foot-right {
  animation: panda-step-right 360ms ease-in-out infinite alternate;
  transform-box: fill-box;
  transform-origin: center;
}

.panda-walking-arms {
  transform-box: fill-box;
  transform-origin: center;
  animation: panda-arms-swing 360ms ease-in-out infinite alternate;
}

.panda-pet-working-pose {
  transform-box: fill-box;
  transform-origin: center bottom;
  animation: panda-work-breathe 2.6s ease-in-out infinite;
}

.panda-pet-busy .panda-head {
  animation: panda-focus-bob 2.4s ease-in-out infinite;
  transform-box: fill-box;
  transform-origin: center bottom;
}

.panda-laptop {
  filter: drop-shadow(0 2px 2px rgba(37, 53, 42, 0.18));
}

.panda-screen-frame {
  fill: #354d48;
  stroke: #62c9c7;
  stroke-width: 1.5;
}

.panda-screen {
  fill: #e4f8f3;
}

.panda-screen-light {
  fill: #72bfa6;
}

.panda-screen-code {
  fill: none;
  stroke: #63afa2;
  stroke-linecap: round;
  stroke-width: 2.4;
  animation: panda-code-blink 1.1s steps(2, end) infinite;
}

.panda-laptop-base {
  fill: #637e72;
  stroke: #354d48;
  stroke-width: 1;
}

.panda-trackpad {
  fill: #94a890;
}

.panda-typing-left,
.panda-typing-right {
  transform-box: fill-box;
  transform-origin: center;
  animation: panda-type 260ms ease-in-out infinite alternate;
}

.panda-typing-right {
  animation-delay: 130ms;
}

.panda-pet-petted .panda-head {
  animation: panda-pet-bounce 450ms cubic-bezier(0.2, 1.4, 0.4, 1);
  transform-box: fill-box;
  transform-origin: center bottom;
}

.panda-hearts {
  fill: #e69b9c;
  animation: panda-heart-pop 900ms ease-out both;
  transform-box: fill-box;
  transform-origin: center;
}

.panda-pet-caption {
  position: absolute;
  right: 3px;
  bottom: 1px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 8px;
  border: 1px solid rgba(204, 211, 195, 0.9);
  border-radius: 999px;
  color: #475344;
  background: rgba(255, 254, 248, 0.95);
  box-shadow: 0 2px 8px rgba(45, 59, 45, 0.1);
  font-size: 10px;
  line-height: 1.2;
  pointer-events: none;
}

.panda-pet-caption i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #88a56d;
}

.panda-pet-caption .panda-status-busy {
  background: #6e9a73;
  box-shadow: 0 0 0 3px rgba(110, 154, 115, 0.15);
  animation: panda-status-pulse 1.4s ease-in-out infinite;
}

.panda-pet-caption b {
  font-weight: 700;
}

.panda-pet-caption span {
  color: #778071;
}

.panda-pet-bubble {
  position: absolute;
  z-index: 1;
  right: calc(100% - 32px);
  bottom: calc(100% - 26px);
  display: grid;
  width: 224px;
  gap: 4px;
  padding: 11px 14px;
  border: 1px solid #d5ddcf;
  border-radius: 17px 17px 5px 17px;
  color: #3d483d;
  background: #fffdf6;
  box-shadow: 0 8px 22px rgba(44, 58, 44, 0.16);
  font-size: 12px;
  line-height: 1.45;
  animation: panda-bubble-in 180ms ease-out both;
  pointer-events: none;
}

.panda-pet-bubble::after {
  position: absolute;
  right: 19px;
  bottom: -7px;
  width: 12px;
  height: 12px;
  border-right: 1px solid #d5ddcf;
  border-bottom: 1px solid #d5ddcf;
  background: #fffdf6;
  content: "";
  transform: rotate(45deg);
}

.panda-pet-bubble-left {
  right: auto;
  left: calc(100% - 32px);
  border-radius: 17px 17px 17px 5px;
}

.panda-pet-bubble-left::after {
  right: auto;
  left: 19px;
  transform: rotate(45deg);
}

.panda-pet-bubble-below {
  top: calc(100% - 20px);
  bottom: auto;
  border-radius: 17px 17px 5px 17px;
}

.panda-pet-bubble-below::after {
  top: -7px;
  right: 19px;
  bottom: auto;
  border: 0;
  border-top: 1px solid #d5ddcf;
  border-left: 1px solid #d5ddcf;
  transform: rotate(45deg);
}

.panda-pet-bubble-below.panda-pet-bubble-left {
  border-radius: 17px 17px 17px 5px;
}

.panda-pet-bubble-below.panda-pet-bubble-left::after {
  right: auto;
  left: 19px;
}

.panda-pet-bubble strong {
  color: #61794e;
  font-size: 11px;
}

@keyframes panda-idle-bob {
  0%, 100% { transform: translateY(0) rotate(-1deg); }
  50% { transform: translateY(-3px) rotate(1deg); }
}

@keyframes panda-chew-sway {
  0%, 100% { transform: rotate(-1deg); }
  50% { transform: rotate(2deg) translateY(1px); }
}

@keyframes panda-leaf-flutter {
  from { transform: rotate(-3deg); }
  to { transform: rotate(5deg); }
}

@keyframes panda-walk-bob {
  from { transform: translateY(0) rotate(-2deg); }
  to { transform: translateY(-3px) rotate(2deg); }
}

@keyframes panda-step-left {
  from { transform: translate(-2px, 0) rotate(-4deg); }
  to { transform: translate(2px, -2px) rotate(4deg); }
}

@keyframes panda-step-right {
  from { transform: translate(2px, -2px) rotate(4deg); }
  to { transform: translate(-2px, 0) rotate(-4deg); }
}

@keyframes panda-arms-swing {
  from { transform: rotate(-6deg); }
  to { transform: rotate(6deg); }
}

@keyframes panda-work-breathe {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(1px); }
}

@keyframes panda-focus-bob {
  0%, 100% { transform: rotate(0); }
  50% { transform: rotate(1.2deg) translateY(1px); }
}

@keyframes panda-type {
  from { transform: translateY(-1px) rotate(-4deg); }
  to { transform: translateY(1px) rotate(4deg); }
}

@keyframes panda-code-blink {
  0%, 100% { opacity: 0.52; }
  50% { opacity: 1; }
}

@keyframes panda-blink {
  0%, 40%, 44%, 100% { transform: scaleY(1); }
  42% { transform: scaleY(1.55); }
}

@keyframes panda-shadow-breathe {
  0%, 100% { transform: scaleX(1); opacity: 0.16; }
  50% { transform: scaleX(0.88); opacity: 0.11; }
}

@keyframes panda-status-pulse {
  50% { opacity: 0.56; }
}

@keyframes panda-pet-bounce {
  0%, 100% { transform: translateY(0) rotate(0); }
  35% { transform: translateY(-9px) rotate(-5deg); }
  70% { transform: translateY(1px) rotate(4deg); }
}

@keyframes panda-heart-pop {
  0% { opacity: 0; transform: scale(0.5) translateY(5px); }
  35% { opacity: 1; transform: scale(1.1) translateY(-5px); }
  100% { opacity: 0; transform: scale(0.9) translateY(-13px); }
}

@keyframes panda-bubble-in {
  from { opacity: 0; transform: translateY(4px) scale(0.96); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

@media (max-width: 640px) {
  .panda-pet {
    transform: scale(0.85);
    transform-origin: top left;
  }

  .panda-pet-bubble {
    width: 200px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .panda-pet *,
  .panda-pet *::before,
  .panda-pet *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
