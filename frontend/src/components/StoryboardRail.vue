<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import type { DirectorStoryboardShot } from "../types";

const props = defineProps<{
  shots: DirectorStoryboardShot[];
  selectedIndex: number;
}>();

const emit = defineEmits<{
  select: [index: number];
}>();

const host = ref<HTMLDivElement | null>(null);
const webglAvailable = ref(true);
const webglError = ref("");
let renderer: THREE.WebGLRenderer | null = null;
let scene: THREE.Scene | null = null;
let camera: THREE.OrthographicCamera | null = null;
let controls: OrbitControls | null = null;
let resizeObserver: ResizeObserver | null = null;
let shotMeshes: THREE.Mesh[] = [];
let pointerStart: { x: number; y: number } | null = null;

const materialColors = {
  muted: 0x82948a,
  selected: 0xc36a4c,
  guide: 0x526a60,
  ground: 0xe8eee8,
};

function render(): void {
  if (renderer && scene && camera) renderer.render(scene, camera);
}

function updateFrustum(): void {
  if (!host.value || !renderer || !camera) return;
  const width = Math.max(host.value.clientWidth, 1);
  const height = Math.max(host.value.clientHeight, 1);
  renderer.setSize(width, height, false);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const aspect = width / height;
  const railWidth = Math.max(6, props.shots.length * 1.42 + 2);
  const viewWidth = Math.max(railWidth, 3.2 * aspect);
  const viewHeight = viewWidth / aspect;
  camera.left = -viewWidth / 2;
  camera.right = viewWidth / 2;
  camera.top = viewHeight / 2;
  camera.bottom = -viewHeight / 2;
  camera.updateProjectionMatrix();
  render();
}

function clearShotMeshes(): void {
  if (!scene) return;
  const removable = scene.children.filter((child) => child.userData.railContent);
  for (const child of removable) {
    scene.remove(child);
    child.traverse((node) => {
      if (node instanceof THREE.Mesh) {
        node.geometry.dispose();
        const materials = Array.isArray(node.material)
          ? node.material
          : [node.material];
        materials.forEach((material) => material.dispose());
      }
      if (node instanceof THREE.Line) {
        node.geometry.dispose();
        const materials = Array.isArray(node.material)
          ? node.material
          : [node.material];
        materials.forEach((material) => material.dispose());
      }
    });
  }
  shotMeshes = [];
}

function drawRail(): void {
  if (!scene) return;
  clearShotMeshes();
  if (!props.shots.length) {
    render();
    return;
  }

  const group = new THREE.Group();
  group.userData.railContent = true;
  const points: THREE.Vector3[] = [];
  props.shots.forEach((shot, index) => {
    const x = (index - (props.shots.length - 1) / 2) * 1.42;
    const duration = Math.max(1, Math.min(shot.duration_seconds, 15));
    const blockHeight = 0.42 + duration * 0.065;
    const blockWidth = 0.72 + Math.min(duration, 8) * 0.035;
    const mesh = new THREE.Mesh(
      new THREE.BoxGeometry(blockWidth, blockHeight, 0.32),
      new THREE.MeshStandardMaterial({
        color:
          index === props.selectedIndex
            ? materialColors.selected
            : materialColors.muted,
        roughness: 0.58,
        metalness: 0.04,
      }),
    );
    mesh.position.set(x, blockHeight / 2, 0);
    mesh.userData.shotIndex = index;
    mesh.userData.railContent = true;
    group.add(mesh);
    shotMeshes.push(mesh);
    points.push(new THREE.Vector3(x, 0.04, -0.22));

    const marker = new THREE.Mesh(
      new THREE.SphereGeometry(index === props.selectedIndex ? 0.13 : 0.09, 16, 12),
      new THREE.MeshStandardMaterial({
        color:
          index === props.selectedIndex
            ? materialColors.selected
            : materialColors.guide,
        roughness: 0.5,
      }),
    );
    marker.position.set(x, blockHeight + 0.16, 0);
    marker.userData.railContent = true;
    group.add(marker);
  });

  const rail = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color: materialColors.guide }),
  );
  rail.userData.railContent = true;
  group.add(rail);

  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(Math.max(6, props.shots.length * 1.42 + 2), 3.2),
    new THREE.MeshStandardMaterial({
      color: materialColors.ground,
      roughness: 1,
      side: THREE.DoubleSide,
    }),
  );
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.06;
  ground.position.z = -0.65;
  ground.userData.railContent = true;
  group.add(ground);
  scene.add(group);
  updateFrustum();
}

function onPointerDown(event: PointerEvent): void {
  pointerStart = { x: event.clientX, y: event.clientY };
}

function onPointerUp(event: PointerEvent): void {
  if (!pointerStart || !renderer) return;
  const distance = Math.hypot(
    event.clientX - pointerStart.x,
    event.clientY - pointerStart.y,
  );
  pointerStart = null;
  if (distance > 6) return;

  const bounds = renderer.domElement.getBoundingClientRect();
  const pointer = new THREE.Vector2(
    ((event.clientX - bounds.left) / bounds.width) * 2 - 1,
    -((event.clientY - bounds.top) / bounds.height) * 2 + 1,
  );
  const raycaster = new THREE.Raycaster();
  raycaster.setFromCamera(pointer, camera!);
  const hit = raycaster.intersectObjects(shotMeshes, false)[0];
  if (typeof hit?.object.userData.shotIndex === "number") {
    emit("select", hit.object.userData.shotIndex);
  }
}

function setupScene(): void {
  if (!host.value) return;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.domElement.setAttribute("aria-hidden", "true");
    host.value.replaceChildren(renderer.domElement);
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf7f8f3);
    camera = new THREE.OrthographicCamera(-8, 8, 4, -4, 0.1, 100);
    camera.position.set(0, 7, 18);
    camera.lookAt(0, 0, 0);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x526a60, 2.2));
    const keyLight = new THREE.DirectionalLight(0xffffff, 2.5);
    keyLight.position.set(-5, 10, 8);
    scene.add(keyLight);

    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false;
    controls.enablePan = false;
    controls.minPolarAngle = 0.65;
    controls.maxPolarAngle = 1.42;
    controls.addEventListener("change", render);
    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    renderer.domElement.addEventListener("pointerup", onPointerUp);
    resizeObserver = new ResizeObserver(updateFrustum);
    resizeObserver.observe(host.value);
    webglAvailable.value = true;
    webglError.value = "";
    drawRail();
  } catch (cause) {
    resizeObserver?.disconnect();
    resizeObserver = null;
    webglError.value =
      cause instanceof Error ? cause.message : "无法创建 WebGL 预览";
    renderer?.dispose();
    renderer?.domElement.remove();
    renderer = null;
    scene = null;
    camera = null;
    controls?.dispose();
    controls = null;
    webglAvailable.value = false;
  }
}

watch(
  () => props.shots,
  () => drawRail(),
  { deep: true },
);

watch(
  () => props.selectedIndex,
  () => drawRail(),
);

onMounted(setupScene);

onUnmounted(() => {
  resizeObserver?.disconnect();
  if (renderer) {
    renderer.domElement.removeEventListener("pointerdown", onPointerDown);
    renderer.domElement.removeEventListener("pointerup", onPointerUp);
  }
  controls?.removeEventListener("change", render);
  controls?.dispose();
  clearShotMeshes();
  renderer?.dispose();
  renderer?.domElement.remove();
  renderer = null;
  scene = null;
  camera = null;
});
</script>

<template>
  <section class="storyboard-rail" aria-label="三维分镜顺序浏览">
    <div class="rail-heading">
      <div>
        <h3>分镜轨道</h3>
        <p>拖动旋转视角，点击镜头查看脚本内容</p>
      </div>
      <span>{{ shots.length }} 镜</span>
    </div>
    <div v-if="webglAvailable" ref="host" class="rail-canvas" />
    <p v-else class="rail-fallback" role="status">
      3D 预览无法启动（{{ webglError }}）；仍可使用下方镜头列表查看和选择分镜。
    </p>
    <nav class="shot-index" aria-label="按顺序选择镜头">
      <button
        v-for="(shot, index) in shots"
        :key="`${index}-${shot.shot_no}`"
        type="button"
        :class="{ selected: selectedIndex === index }"
        :aria-pressed="selectedIndex === index"
        @click="emit('select', index)"
      >
        <span>{{ shot.shot_no }}</span>
        <small>{{ shot.duration_seconds }} 秒</small>
      </button>
    </nav>
  </section>
</template>

<style scoped>
.storyboard-rail {
  overflow: hidden;
  margin: 0 0 24px;
  border: 1px solid #d7dfd8;
  background: #f7f8f3;
}

.rail-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 18px 10px;
  color: #263a38;
}

.rail-heading h3,
.rail-heading p {
  margin: 0;
}

.rail-heading h3 {
  font-size: 15px;
  font-weight: 650;
}

.rail-heading p {
  margin-top: 4px;
  color: #64716a;
  font-size: 12px;
}

.rail-heading > span {
  color: #526a60;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.rail-canvas {
  height: 190px;
  touch-action: pan-y;
  cursor: grab;
}

.rail-canvas:active {
  cursor: grabbing;
}

.rail-canvas :deep(canvas) {
  display: block;
  width: 100%;
  height: 100%;
}

.rail-fallback {
  margin: 0;
  padding: 16px 18px;
  color: #64716a;
  font-size: 13px;
}

.shot-index {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 10px 14px 14px;
  border-top: 1px solid #d7dfd8;
  scrollbar-color: #9baa9f #e8eee8;
}

.shot-index button {
  display: grid;
  flex: 0 0 auto;
  min-width: 76px;
  padding: 7px 10px;
  border: 1px solid #cbd6ce;
  color: #263a38;
  background: #fff;
  text-align: left;
  cursor: pointer;
}

.shot-index button.selected {
  border-color: #b64e3d;
  background: #f5e8e2;
}

.shot-index button:focus-visible {
  outline: 3px solid #263a38;
  outline-offset: 2px;
}

.shot-index span {
  font-size: 13px;
  font-weight: 700;
}

.shot-index small {
  margin-top: 2px;
  color: #64716a;
  font-size: 10px;
}

@media (max-width: 720px) {
  .rail-canvas {
    height: 150px;
  }
}
</style>
