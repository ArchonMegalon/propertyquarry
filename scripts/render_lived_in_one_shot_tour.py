#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


TOP22_SPEC: dict[str, object] = {
    "name": "top22-lived-in",
    "rooms": [
        {"name": "entry", "x": 1.0, "z": 5.3, "w": 3.6, "d": 3.9, "kind": "entry"},
        {"name": "bath_wc", "x": 1.0, "z": 1.0, "w": 2.0, "d": 4.3, "kind": "bath"},
        {"name": "living_kitchen", "x": 4.6, "z": 1.0, "w": 6.2, "d": 2.8, "kind": "living_kitchen"},
        {"name": "bedroom_1", "x": 4.6, "z": 3.8, "w": 3.1, "d": 5.4, "kind": "bedroom"},
        {"name": "bedroom_2", "x": 7.7, "z": 3.8, "w": 3.1, "d": 3.2, "kind": "bedroom_child"},
        {"name": "balcony", "x": 7.7, "z": 7.0, "w": 3.1, "d": 2.2, "kind": "balcony"},
    ],
    "route": [
        {"room": "entry", "at": [2.55, 7.45], "start_deg": -80, "sweep_deg": 210},
        {"room": "bath_wc", "at": [2.10, 3.55], "start_deg": -35, "sweep_deg": 180},
        {"room": "living_kitchen", "at": [6.90, 2.55], "start_deg": -75, "sweep_deg": 220},
        {"room": "bedroom_1", "at": [6.55, 6.45], "start_deg": -130, "sweep_deg": 180},
        {"room": "bedroom_2", "at": [9.10, 5.70], "start_deg": -45, "sweep_deg": 190},
        {"room": "balcony", "at": [9.20, 8.10], "start_deg": 90, "sweep_deg": 180},
    ],
}


HTML_TEMPLATE = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; background: #111; }
    canvas { display: block; width: 100vw; height: 100vh; }
  </style>
</head>
<body>
<script type="module">
import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.165.0/build/three.module.js";

const spec = __SPEC__;
const width = __WIDTH__;
const height = __HEIGHT__;
const duration = __DURATION__;
const roomTime = duration / spec.route.length;
const moveShare = 0.38;
const scene = new THREE.Scene();
scene.background = new THREE.Color(0xf4f0e8);
scene.fog = new THREE.Fog(0xf4f0e8, 8, 20);

const camera = new THREE.PerspectiveCamera(68, width / height, 0.05, 60);
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setSize(width, height);
renderer.setPixelRatio(1);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.appendChild(renderer.domElement);

scene.add(new THREE.HemisphereLight(0xffffff, 0x92785d, 1.7));
const sun = new THREE.DirectionalLight(0xfff3d6, 2.2);
sun.position.set(2, 8, -4);
sun.castShadow = true;
scene.add(sun);
const warm = new THREE.PointLight(0xffdfb0, 1.25, 12);
warm.position.set(7, 2.4, 2.3);
scene.add(warm);

const mat = {
  floor: new THREE.MeshStandardMaterial({ color: 0xb99063, roughness: 0.82 }),
  balconyFloor: new THREE.MeshStandardMaterial({ color: 0x9b9b90, roughness: 0.9 }),
  wall: new THREE.MeshStandardMaterial({ color: 0xf1eee8, roughness: 0.75 }),
  accent: new THREE.MeshStandardMaterial({ color: 0xd9d2c8, roughness: 0.8 }),
  dark: new THREE.MeshStandardMaterial({ color: 0x262626, roughness: 0.7 }),
  wood: new THREE.MeshStandardMaterial({ color: 0x8f643f, roughness: 0.75 }),
  fabric: new THREE.MeshStandardMaterial({ color: 0x6f7f93, roughness: 0.9 }),
  green: new THREE.MeshStandardMaterial({ color: 0x5f7d55, roughness: 0.9 }),
  red: new THREE.MeshStandardMaterial({ color: 0xaa5550, roughness: 0.82 }),
  toyBlue: new THREE.MeshStandardMaterial({ color: 0x397eb9, roughness: 0.7 }),
  white: new THREE.MeshStandardMaterial({ color: 0xf8f5ef, roughness: 0.55 }),
  metal: new THREE.MeshStandardMaterial({ color: 0xb9b5ad, roughness: 0.35, metalness: 0.25 }),
  screen: new THREE.MeshStandardMaterial({ color: 0x101114, roughness: 0.25, emissive: 0x25385f, emissiveIntensity: 0.35 }),
  skin: new THREE.MeshStandardMaterial({ color: 0xc99d78, roughness: 0.65 }),
  shirt: new THREE.MeshStandardMaterial({ color: 0x7d5148, roughness: 0.8 }),
};

function box(name, x, y, z, sx, sy, sz, material, cast=true) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(sx, sy, sz), material);
  mesh.name = name;
  mesh.position.set(x, y, z);
  mesh.castShadow = cast;
  mesh.receiveShadow = true;
  scene.add(mesh);
  return mesh;
}
function cyl(name, x, y, z, radius, h, material, segments=32) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, h, segments), material);
  mesh.name = name;
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  scene.add(mesh);
  return mesh;
}
function sphere(name, x, y, z, radius, material) {
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(radius, 32, 18), material);
  mesh.name = name;
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  scene.add(mesh);
  return mesh;
}
function rug(x, z, sx, sz, color) {
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(sx, sz), new THREE.MeshStandardMaterial({ color, roughness: 0.95 }));
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.set(x, 0.012, z);
  mesh.receiveShadow = true;
  scene.add(mesh);
}
let wallSegmentIndex = 0;
function wallSegment(x1, z1, x2, z2) {
  const dx = x2 - x1, dz = z2 - z1;
  const len = Math.hypot(dx, dz);
  const isShell = wallSegmentIndex < 4;
  wallSegmentIndex += 1;
  const h = isShell ? 2.5 : 0.22;
  const mesh = box(isShell ? "wall" : "room_threshold", (x1+x2)/2, h / 2, (z1+z2)/2, len, h, 0.055, isShell ? mat.wall : mat.accent, false);
  mesh.rotation.y = -Math.atan2(dz, dx);
}
function addRoom(room) {
  const cx = room.x + room.w / 2;
  const cz = room.z + room.d / 2;
  const floorMat = room.kind === "balcony" ? mat.balconyFloor : mat.floor;
  box(`${room.name}_floor`, cx, -0.025, cz, room.w, 0.05, room.d, floorMat, false);
  box(`${room.name}_ceiling`, cx, 2.52, cz, room.w, 0.04, room.d, mat.wall, false);
}
for (const room of spec.rooms) addRoom(room);

// Shell and partial dividers. Openings stay open so the shot reads as one continuous walk.
wallSegment(1.0, 1.0, 10.8, 1.0);
wallSegment(10.8, 1.0, 10.8, 9.2);
wallSegment(10.8, 9.2, 1.0, 9.2);
wallSegment(1.0, 9.2, 1.0, 1.0);
wallSegment(3.0, 1.0, 3.0, 3.55);
wallSegment(3.0, 4.75, 3.0, 5.3);
wallSegment(4.6, 1.0, 4.6, 1.95);
wallSegment(4.6, 3.05, 4.6, 5.2);
wallSegment(4.6, 6.25, 4.6, 9.2);
wallSegment(4.6, 3.8, 6.45, 3.8);
wallSegment(7.3, 3.8, 10.8, 3.8);
wallSegment(7.7, 3.8, 7.7, 5.25);
wallSegment(7.7, 6.4, 7.7, 9.2);
wallSegment(7.7, 7.0, 9.35, 7.0);
wallSegment(10.25, 7.0, 10.8, 7.0);

// Doorway hints are floor-level thresholds. Tall trims read like walls in a first-person walkthrough.
box("entry_bath_threshold", 3.02, 0.035, 4.3, 0.06, 0.07, 1.05, mat.accent, false);
box("entry_living_threshold", 4.58, 0.035, 2.45, 0.06, 0.07, 1.15, mat.accent, false);
box("living_bed1_threshold", 6.9, 0.035, 3.82, 1.0, 0.07, 0.06, mat.accent, false);
box("bed1_bed2_threshold", 7.7, 0.035, 5.7, 0.06, 0.07, 1.05, mat.accent, false);
box("bed2_balcony_door_glass", 9.9, 1.2, 7.02, 1.2, 2.2, 0.04, new THREE.MeshStandardMaterial({ color: 0x9bc0d6, transparent: true, opacity: 0.38, roughness: 0.25 }), false);

// Entry: coats, shoes, storage.
box("entry_runner", 2.3, 0.02, 7.1, 1.45, 0.04, 2.2, mat.red);
box("shoe_cabinet", 1.34, 0.45, 7.0, 0.45, 0.9, 1.6, mat.wood);
for (let i = 0; i < 5; i++) {
  box("coat", 1.25, 1.35 - i*0.02, 6.1 + i*0.25, 0.08, 0.85, 0.26, i % 2 ? mat.dark : mat.fabric);
}
for (let i = 0; i < 4; i++) box("shoes", 2.0 + i*0.28, 0.08, 8.65, 0.22, 0.12, 0.38, i % 2 ? mat.dark : mat.wood);

// Bath/WC.
box("vanity", 1.35, 0.48, 2.0, 0.48, 0.85, 0.9, mat.white);
box("mirror", 1.08, 1.55, 2.0, 0.04, 0.7, 0.95, mat.metal, false);
cyl("toilet", 2.25, 0.26, 2.8, 0.32, 0.52, mat.white);
box("shower_glass", 2.0, 1.05, 4.45, 1.25, 2.0, 0.05, new THREE.MeshStandardMaterial({ color: 0xcfe4ef, transparent: true, opacity: 0.34 }), false);
box("towels", 1.12, 1.2, 3.35, 0.05, 0.75, 0.55, mat.green);

// Living kitchen.
box("kitchen_wall", 5.8, 1.0, 1.18, 2.25, 2.0, 0.42, mat.white);
box("counter", 5.9, 0.48, 2.1, 2.5, 0.9, 0.72, mat.white);
box("island", 6.45, 0.48, 2.85, 1.55, 0.9, 0.75, mat.wood);
for (let i = 0; i < 5; i++) cyl("cups", 5.5 + i*0.25, 0.98, 2.42, 0.055, 0.14, i % 2 ? mat.red : mat.toyBlue, 18);
// Person cooking.
cyl("person_body", 6.75, 1.05, 2.1, 0.18, 0.95, mat.shirt, 24);
sphere("person_head", 6.75, 1.65, 2.1, 0.18, mat.skin);
box("person_arm", 6.52, 1.25, 2.22, 0.5, 0.08, 0.08, mat.skin);
box("sofa", 9.25, 0.38, 2.62, 1.85, 0.75, 0.82, mat.fabric);
box("sofa_back", 9.25, 0.82, 3.02, 1.9, 0.7, 0.18, mat.fabric);
box("coffee_table", 8.55, 0.22, 2.2, 0.82, 0.28, 0.45, mat.wood);
box("tv_screen", 10.66, 1.15, 2.35, 0.08, 0.72, 1.12, mat.screen, false);
rug(8.85, 2.55, 2.55, 1.3, 0xddd1c2);
for (let i = 0; i < 9; i++) box("toys", 8.25 + (i%3)*0.22, 0.08, 3.2 + Math.floor(i/3)*0.18, 0.13, 0.13, 0.13, i % 3 === 0 ? mat.toyBlue : (i % 3 === 1 ? mat.red : mat.green));

// Bedrooms.
box("bed1", 5.55, 0.35, 6.6, 1.35, 0.7, 2.05, mat.green);
box("bed1_pillow", 5.55, 0.78, 5.72, 1.08, 0.18, 0.36, mat.white);
box("desk1", 6.95, 0.42, 8.45, 1.05, 0.85, 0.48, mat.wood);
box("chair1", 6.6, 0.35, 8.02, 0.45, 0.7, 0.45, mat.fabric);
box("laundry_stack", 7.32, 0.18, 7.95, 0.42, 0.26, 0.62, mat.white);
rug(6.05, 7.8, 1.5, 1.0, 0xb6a58c);
box("bed2", 8.95, 0.34, 5.0, 1.26, 0.68, 1.72, mat.red);
box("bed2_pillow", 8.95, 0.77, 4.35, 0.96, 0.16, 0.34, mat.white);
box("toy_shelf", 10.38, 0.72, 5.85, 0.45, 1.35, 0.95, mat.wood);
for (let i = 0; i < 7; i++) sphere("child_toys", 8.35 + (i%4)*0.25, 0.12, 6.45 + (i%2)*0.23, 0.09, i % 2 ? mat.toyBlue : mat.green);

// Balcony.
box("balcony_rail", 9.25, 1.0, 9.12, 2.7, 1.5, 0.08, mat.metal, false);
box("balcony_chair", 8.65, 0.35, 8.15, 0.55, 0.7, 0.55, mat.wood);
box("balcony_table", 9.35, 0.35, 8.25, 0.62, 0.7, 0.62, mat.wood);
for (let i = 0; i < 5; i++) {
  cyl("plant_pot", 10.15, 0.18, 7.35 + i*0.35, 0.16, 0.35, mat.wood);
  sphere("plant", 10.15, 0.55, 7.35 + i*0.35, 0.22, mat.green);
}

function smoothstep(t) { t = Math.max(0, Math.min(1, t)); return t*t*(3-2*t); }
function lerp(a,b,t) { return a + (b-a)*t; }
function wrapAngle(a) {
  while (a > Math.PI) a -= Math.PI * 2;
  while (a < -Math.PI) a += Math.PI * 2;
  return a;
}
function poseAt(timeSeconds) {
  const t = Math.max(0, Math.min(duration - 0.001, timeSeconds));
  const idx = Math.min(spec.route.length - 1, Math.floor(t / roomTime));
  const local = (t - idx * roomTime) / roomTime;
  const stop = spec.route[idx];
  const prev = idx === 0 ? { at: [stop.at[0] - 0.04, stop.at[1] + 0.9], start_deg: stop.start_deg } : spec.route[idx - 1];
  const sx = prev.at[0], sz = prev.at[1], ex = stop.at[0], ez = stop.at[1];
  const startYaw = Math.atan2(ex - sx, ez - sz);
  const scanStart = THREE.MathUtils.degToRad(stop.start_deg);
  const scanEnd = THREE.MathUtils.degToRad(stop.start_deg + stop.sweep_deg);
  let x, z, yaw;
  if (local < moveShare) {
    const u = smoothstep(local / moveShare);
    x = lerp(sx, ex, u);
    z = lerp(sz, ez, u);
    yaw = startYaw + wrapAngle(scanStart - startYaw) * u;
  } else {
    const u = smoothstep((local - moveShare) / (1 - moveShare));
    x = ex;
    z = ez;
    yaw = lerp(scanStart, scanEnd, u);
  }
  const bob = Math.sin(timeSeconds * 2.1) * 0.012 + Math.sin(timeSeconds * 4.4) * 0.005;
  return { x, z, yaw, y: 1.52 + bob };
}
window.renderAt = (timeSeconds) => {
  const p = poseAt(timeSeconds);
  camera.position.set(p.x, p.y, p.z);
  const target = new THREE.Vector3(p.x + Math.sin(p.yaw), p.y - 0.03, p.z + Math.cos(p.yaw));
  camera.lookAt(target);
  renderer.render(scene, camera);
};
window.capturePng = () => renderer.domElement.toDataURL("image/png");
window.renderAt(0);
</script>
</body>
</html>
"""


def _write_html(path: Path, *, spec: dict[str, object], width: int, height: int, duration: float) -> None:
    html = (
        HTML_TEMPLATE.replace("__SPEC__", json.dumps(spec, ensure_ascii=False))
        .replace("__WIDTH__", str(width))
        .replace("__HEIGHT__", str(height))
        .replace("__DURATION__", str(duration))
    )
    path.write_text(html, encoding="utf-8")


def _run_ffmpeg(frame_dir: Path, out_path: Path, fps: int) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frame_dir / "frame_%05d.png"),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "medium",
        "-crf",
        "19",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    subprocess.run(command, check=True)


def render_one_shot(*, out_path: Path, spec: dict[str, object], width: int, height: int, fps: int, duration: float) -> Path:
    total_frames = int(round(duration * fps))
    with tempfile.TemporaryDirectory(prefix="pq-one-shot-") as tmp:
        tmp_path = Path(tmp)
        html_path = tmp_path / "scene.html"
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        _write_html(html_path, spec=spec, width=width, height=height, duration=duration)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--use-gl=swiftshader",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.goto(html_path.as_uri(), wait_until="networkidle", timeout=120_000)
            page.wait_for_function("() => typeof window.renderAt === 'function' && typeof window.capturePng === 'function'", timeout=120_000)
            for frame in range(total_frames):
                seconds = frame / fps
                data_url = page.evaluate("(seconds) => { window.renderAt(seconds); return window.capturePng(); }", seconds)
                encoded = str(data_url).split(",", 1)[1]
                (frame_dir / f"frame_{frame:05d}.png").write_bytes(base64.b64decode(encoded))
                if frame and frame % max(1, fps * 10) == 0:
                    print(json.dumps({"frames": frame, "seconds": round(seconds, 2)}), flush=True)
            browser.close()
        _run_ffmpeg(frame_dir, out_path, fps)
    return out_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a continuous lived-in 3D one-shot walkthrough from a floorplan route.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--spec-json", default="", help="Optional spec JSON. Defaults to the Top 22 reference spec.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--duration", type=float, default=90.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg_missing")
    spec = TOP22_SPEC
    if args.spec_json:
        spec = json.loads(Path(args.spec_json).read_text(encoding="utf-8"))
    out_path = Path(args.out).expanduser()
    render_one_shot(
        out_path=out_path,
        spec=spec,
        width=max(640, int(args.width)),
        height=max(360, int(args.height)),
        fps=max(12, int(args.fps)),
        duration=max(30.0, float(args.duration)),
    )
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
