"""SnapSCAD 在线交互协同建模工作台 (Interactive Web Workspace)。

在浏览器端提供轻量级全功能 CAD 与切片协同工作环境：
- 3D WebGL 视口: Three.js 驱动，支持 360° 旋转、缩放平移、截面剖切 (Clip Plane) 与坐标轴
- 参数化在线微调: 实时调整几何尺寸比例、爆炸图展开、圆柱平滑度 ($fn) 与耗材材料
- 源码动态编辑: 在线编辑 OpenSCAD 源码，一键提交后端微服务实时重编译 (Live Compile)
- 3D 打印切片与机群调度: 在线调参一键生成 G-code，直接向 Klipper / OctoPrint / 拓竹机群投递任务
- 生态库一键导出: 在线触发打包标准 OpenSCAD 模块生态库包
"""

from __future__ import annotations

import html as html_lib
import json
from pathlib import Path
from typing import Any, Optional

from .log import get_logger as _log_module

_log = _log_module()


def generate_workspace_html(output_dir: str | Path, html_filename: str = "workspace.html") -> Path:
    """生成并刷新交互式 Web 建模工作台页面。"""
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target_path = out_dir / html_filename

    html_content = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SnapSCAD 交互协同建模工作台 (v1.0.0)</title>
<style>
  :root {
    --bg-main: #0b0f19;
    --bg-panel: #111827;
    --bg-card: #1f2937;
    --border: rgba(255, 255, 255, 0.1);
    --primary: #2563eb;
    --primary-hover: #1d4ed8;
    --accent: #38bdf8;
    --success: #10b981;
    --warning: #f59e0b;
    --danger: #ef4444;
    --text: #f9fafb;
    --text-muted: #9ca3af;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background-color: var(--bg-main);
    color: var(--text);
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  header {
    height: 52px;
    background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 16px;
    z-index: 10;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 1.1rem;
    font-weight: 700;
    color: var(--text);
  }
  .brand span { color: var(--accent); }
  .nav-controls {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  select, button, input {
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 0.85rem;
    outline: none;
    cursor: pointer;
  }
  select:focus, input:focus { border-color: var(--accent); }
  button {
    background: var(--primary);
    border: none;
    font-weight: 600;
    transition: all 0.15s ease;
  }
  button:hover { background: var(--primary-hover); }
  button.btn-accent { background: #0284c7; }
  button.btn-accent:hover { background: #0369a1; }
  button.btn-success { background: var(--success); }
  button.btn-success:hover { background: #059669; }

  /* 主工作区布局: 左编辑微调 + 中 3D 视口 + 右制造分析 */
  .workspace-main {
    flex: 1;
    display: flex;
    overflow: hidden;
  }
  .side-panel {
    width: 380px;
    background: var(--bg-panel);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .side-panel.right {
    width: 320px;
    border-right: none;
    border-left: 1px solid var(--border);
  }
  .panel-tabs {
    display: flex;
    border-bottom: 1px solid var(--border);
    background: rgba(0, 0, 0, 0.2);
  }
  .tab-btn {
    flex: 1;
    background: transparent;
    border-radius: 0;
    padding: 10px;
    color: var(--text-muted);
    font-weight: 500;
    border-bottom: 2px solid transparent;
  }
  .tab-btn.active {
    color: var(--accent);
    border-bottom: 2px solid var(--accent);
    background: rgba(255, 255, 255, 0.03);
  }
  .tab-content {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: none;
  }
  .tab-content.active { display: block; }

  /* 视口区域 */
  .viewport-container {
    flex: 1;
    position: relative;
    background: radial-gradient(circle at 50% 50%, #1e293b 0%, #0f172a 100%);
  }
  #webgl-canvas {
    width: 100%;
    height: 100%;
    display: block;
  }
  .viewport-hud {
    position: absolute;
    top: 12px;
    left: 12px;
    background: rgba(17, 24, 39, 0.75);
    backdrop-filter: blur(8px);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 0.8rem;
    pointer-events: none;
  }
  .viewport-tools {
    position: absolute;
    bottom: 16px;
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    gap: 8px;
    background: rgba(17, 24, 39, 0.8);
    backdrop-filter: blur(8px);
    border: 1px solid var(--border);
    padding: 6px 10px;
    border-radius: 8px;
  }

  /* 参数滑块与控件 */
  .form-group {
    margin-bottom: 16px;
  }
  .form-group label {
    display: flex;
    justify-content: space-between;
    font-size: 0.8rem;
    color: var(--text-muted);
    margin-bottom: 6px;
  }
  .form-group input[type="range"] {
    width: 100%;
    padding: 0;
    cursor: pointer;
  }
  textarea.code-editor {
    width: 100%;
    height: calc(100vh - 170px);
    background: #0d1117;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: #e6edf3;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.85rem;
    line-height: 1.5;
    padding: 10px;
    resize: none;
    outline: none;
    white-space: pre;
  }

  /* 制造卡片 */
  .metric-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 14px;
  }
  .metric-title {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    margin-bottom: 6px;
  }
  .metric-val {
    font-size: 1.25rem;
    font-weight: 700;
    color: var(--accent);
  }
  .status-badge {
    display: inline-block;
    padding: 3px 8px;
    border-radius: 9999px;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .badge-success { background: rgba(16, 185, 129, 0.2); color: #34d399; }
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/STLLoader.js"></script>
</head>
<body>

<header>
  <div class="brand">
    <span>⚡ SnapSCAD</span> 在线协同建模工作台
    <span class="status-badge badge-success">Cloud v1.0</span>
  </div>
  <div class="nav-controls">
    <label style="font-size:0.85rem;color:var(--text-muted)">当前工程:</label>
    <select id="model-select" onchange="onModelChange()"></select>
    <button onclick="triggerCompile()" class="btn-accent">⚡ 重新编译 (F5)</button>
    <button onclick="triggerSlice()">🍰 切片 G-code</button>
    <button onclick="triggerExportLib()" style="background:#4b5563">📦 导出生态库</button>
    <a href="/gallery.html" style="color:var(--accent);font-size:0.85rem;text-decoration:none;margin-left:8px;">🏛️ 返回展厅</a>
  </div>
</header>

<div class="workspace-main">
  <!-- 左侧控制面板: 源码编辑 + 参数化滑块 -->
  <div class="side-panel">
    <div class="panel-tabs">
      <button class="tab-btn active" onclick="switchTab('tab-editor', this)">💻 SCAD 源码</button>
      <button class="tab-btn" onclick="switchTab('tab-params', this)">🎛️ 参数调优</button>
      <button class="tab-btn" onclick="switchTab('tab-slicer', this)">🍰 切片工艺</button>
    </div>

    <!-- 源码编辑 -->
    <div id="tab-editor" class="tab-content active">
      <textarea id="scad-editor" class="code-editor" spellcheck="false" placeholder="// OpenSCAD 源码..."></textarea>
      <div style="margin-top:10px;display:flex;justify-content:flex-end;">
        <button onclick="triggerCompile()" class="btn-accent" style="width:100%">⚡ 应用修改并求值编译</button>
      </div>
    </div>

    <!-- 参数微调 -->
    <div id="tab-params" class="tab-content">
      <div class="form-group">
        <label>全局等比缩放 (Scale): <span id="val-scale">1.0x</span></label>
        <input type="range" id="param-scale" min="0.2" max="3.0" step="0.1" value="1.0" oninput="updateParamVal('scale', this.value)">
      </div>
      <div class="form-group">
        <label>装配爆炸图间距 (Explode): <span id="val-explode">0.0</span></label>
        <input type="range" id="param-explode" min="0.0" max="2.5" step="0.1" value="0.0" oninput="updateParamVal('explode', this.value)">
      </div>
      <div class="form-group">
        <label>圆柱分段精度 ($fn): <span id="val-fn">64</span></label>
        <input type="range" id="param-fn" min="16" max="128" step="8" value="64" oninput="updateParamVal('fn', this.value)">
      </div>
      <div class="form-group">
        <label>剖切截面高度 (Clip Z): <span id="val-clip">全模型</span></label>
        <input type="range" id="param-clip" min="-50" max="150" step="1" value="150" oninput="updateClipZ(this.value)">
      </div>
    </div>

    <!-- 切片工艺 -->
    <div id="tab-slicer" class="tab-content">
      <div class="form-group">
        <label>耗材类型 (Material):</label>
        <select id="slice-material" style="width:100%">
          <option value="PLA">PLA (210°C / 60°C)</option>
          <option value="PETG">PETG (240°C / 80°C)</option>
          <option value="ABS">ABS (250°C / 100°C)</option>
          <option value="TPU">TPU 95A (220°C / 50°C)</option>
          <option value="PC">PC (270°C / 110°C)</option>
        </select>
      </div>
      <div class="form-group">
        <label>打印层高: <span id="val-lh">0.20 mm</span></label>
        <input type="range" id="slice-lh" min="0.12" max="0.28" step="0.04" value="0.20" oninput="document.getElementById('val-lh').innerText = this.value + ' mm'">
      </div>
      <div class="form-group">
        <label>填充率: <span id="val-infill">15 %</span></label>
        <input type="range" id="slice-infill" min="5" max="100" step="5" value="15" oninput="document.getElementById('val-infill').innerText = this.value + ' %'">
      </div>
      <button onclick="triggerSlice()" class="btn-success" style="width:100%;margin-top:10px;">🍰 立即切片生成 G-code</button>
    </div>
  </div>

  <!-- 中间 3D WebGL 视口 -->
  <div class="viewport-container">
    <div id="webgl-canvas"></div>
    <div class="viewport-hud">
      <div>模型: <span id="hud-name" style="color:var(--accent);font-weight:600">-</span></div>
      <div>尺寸: <span id="hud-dim">-</span></div>
    </div>
    <div class="viewport-tools">
      <button onclick="resetCamera()" style="background:transparent;border:1px solid var(--border)">🎯 重置相机</button>
      <button onclick="toggleWireframe()" style="background:transparent;border:1px solid var(--border)">🕸️ 网格模式</button>
      <button onclick="toggleAxes()" style="background:transparent;border:1px solid var(--border)">📐 空间坐标</button>
    </div>
  </div>

  <!-- 右侧面板: 制造指标与机群调度 -->
  <div class="side-panel right">
    <div style="padding:16px;border-bottom:1px solid var(--border);font-weight:700;font-size:0.9rem;">
      🏭 制造分析与机群直连
    </div>
    <div style="flex:1;overflow-y:auto;padding:16px;">
      <div class="metric-card">
        <div class="metric-title">体检适印性得分</div>
        <div id="doc-score" class="metric-val" style="color:#10b981">100 分</div>
        <div id="doc-summary" style="font-size:0.75rem;color:var(--text-muted);margin-top:4px;">体检通过，准备就绪</div>
      </div>

      <div class="metric-card">
        <div class="metric-title">预估打印耗时</div>
        <div id="stat-time" class="metric-val">-</div>
        <div id="stat-filament" style="font-size:0.75rem;color:var(--text-muted);margin-top:4px;">耗材: - g</div>
      </div>

      <div class="metric-card">
        <div class="metric-title">机床直连推送</div>
        <div style="margin-top:8px;">
          <input type="text" id="printer-url" placeholder="http://192.168.1.100:7125" style="width:100%;margin-bottom:8px;">
          <select id="printer-type" style="width:100%;margin-bottom:8px;">
            <option value="auto">自动探测固件</option>
            <option value="klipper">Klipper / Moonraker</option>
            <option value="octoprint">OctoPrint</option>
            <option value="bambu">拓竹 (Bambu Lab LAN)</option>
          </select>
          <input type="password" id="printer-code" placeholder="局域网访问码 / API Key" style="width:100%;margin-bottom:8px;">
          <button onclick="triggerDispatch()" class="btn-accent" style="width:100%">🚀 一键推送到打印机</button>
        </div>
      </div>

      <div class="metric-card">
        <div class="metric-title">资产快速下载</div>
        <div style="display:flex;flex-direction:column;gap:6px;margin-top:8px;">
          <a id="btn-dl-stl" href="#" download class="btn" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:var(--text);text-decoration:none;font-size:0.85rem">⬇️ 下载 STL 模型</a>
          <a id="btn-dl-gcode" href="#" download class="btn" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:var(--accent);text-decoration:none;font-size:0.85rem">⚡ 下载切片 G-code</a>
          <a id="btn-dl-zip" href="#" download class="btn" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:var(--text);text-decoration:none;font-size:0.85rem">📦 打包全套 ZIP</a>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let scene, camera, renderer, controls, currentMesh, axesHelper, clipPlane;
let allModels = [];
let activeModelStem = "";

function init3D() {
  const container = document.getElementById('webgl-canvas');
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(45, container.clientWidth / container.clientHeight, 0.1, 2000);
  camera.position.set(120, 120, 100);

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.localClippingEnabled = true;
  container.appendChild(renderer.domElement);

  controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;

  // 光照
  const hemi = new THREE.HemisphereLight(0xffffff, 0x334155, 1.2);
  scene.add(hemi);
  const dir = new THREE.DirectionalLight(0xffffff, 0.8);
  dir.position.set(100, 150, 120);
  scene.add(dir);

  // 网格底板与坐标轴
  const grid = new THREE.GridHelper(200, 20, 0x38bdf8, 0x334155);
  grid.rotation.x = Math.PI / 2;
  scene.add(grid);

  axesHelper = new THREE.AxesHelper(60);
  scene.add(axesHelper);

  // 截面剖切面
  clipPlane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 150);

  window.addEventListener('resize', onWindowResize);
  animate();
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}

function onWindowResize() {
  const container = document.getElementById('webgl-canvas');
  camera.aspect = container.clientWidth / container.clientHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(container.clientWidth, container.clientHeight);
}

function switchTab(tabId, btn) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
  document.getElementById(tabId).classList.add('active');
  btn.classList.add('active');
}

function updateParamVal(type, val) {
  document.getElementById('val-' + type).innerText = (type === 'scale' ? val + 'x' : val);
  if (type === 'scale' && currentMesh) {
    const s = parseFloat(val);
    currentMesh.scale.set(s, s, s);
  }
}

function updateClipZ(val) {
  document.getElementById('val-clip').innerText = (val >= 140 ? '全模型' : val + ' mm');
  clipPlane.constant = parseFloat(val);
}

function resetCamera() {
  camera.position.set(120, 120, 100);
  controls.target.set(0, 0, 10);
  controls.update();
}

function toggleWireframe() {
  if (currentMesh && currentMesh.material) {
    currentMesh.material.wireframe = !currentMesh.material.wireframe;
  }
}

function toggleAxes() {
  axesHelper.visible = !axesHelper.visible;
}

// 加载模型列表与当前模型
async function loadModels() {
  try {
    const res = await fetch('/api/models');
    if (!res.ok) return;
    allModels = await res.json();
    const sel = document.getElementById('model-select');
    sel.innerHTML = '';
    allModels.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.stem;
      opt.innerText = m.name + ' (' + m.stem + ')';
      sel.appendChild(opt);
    });
    if (allModels.length > 0) {
      loadModelDetail(allModels[0].stem);
    }
  } catch (e) {
    console.warn('API 未连接或处于静态浏览模式:', e);
  }
}

async function loadModelDetail(stem) {
  activeModelStem = stem;
  document.getElementById('hud-name').innerText = stem;

  // 更新下载链接
  document.getElementById('btn-dl-stl').href = stem + '.stl';
  document.getElementById('btn-dl-gcode').href = stem + '.gcode';
  document.getElementById('btn-dl-zip').href = stem + '_project.zip';

  // 读取 SCAD 源码
  try {
    const rScad = await fetch(stem + '.scad');
    if (rScad.ok) {
      document.getElementById('scad-editor').value = await rScad.text();
    }
  } catch (_) {}

  // 加载 STL 到 3D 视口
  loadSTL(stem + '.stl');
}

function loadSTL(url) {
  const loader = new THREE.STLLoader();
  loader.load(url, function (geometry) {
    if (currentMesh) scene.remove(currentMesh);
    geometry.computeBoundingBox();
    const bb = geometry.boundingBox;
    const sx = (bb.max.x - bb.min.x).toFixed(1);
    const sy = (bb.max.y - bb.min.y).toFixed(1);
    const sz = (bb.max.z - bb.min.z).toFixed(1);
    document.getElementById('hud-dim').innerText = `${sx} × ${sy} × ${sz} mm`;

    const material = new THREE.MeshStandardMaterial({
      color: 0x38bdf8,
      roughness: 0.35,
      metalness: 0.1,
      clippingPlanes: [clipPlane],
      clipShadows: true,
    });
    currentMesh = new THREE.Mesh(geometry, material);
    scene.add(currentMesh);
    resetCamera();
  }, undefined, function(err) {
    console.warn('无法加载 STL:', url);
  });
}

function onModelChange() {
  const sel = document.getElementById('model-select');
  loadModelDetail(sel.value);
}

// API 交互操作
async function triggerCompile() {
  const code = document.getElementById('scad-editor').value;
  alert('正在请求后端微服务重新编译求值...');
  try {
    const res = await fetch('/api/compile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem, scad_code: code })
    });
    const data = await res.json();
    if (data.success) {
      alert('✅ 编译成功！正在重新载入 3D 实体...');
      loadSTL(activeModelStem + '.stl?t=' + Date.now());
    } else {
      alert('❌ 编译失败: ' + data.errors.join('; '));
    }
  } catch (e) {
    alert('请求失败: ' + e);
  }
}

async function triggerSlice() {
  const mat = document.getElementById('slice-material').value;
  const lh = parseFloat(document.getElementById('slice-lh').value);
  const infill = parseInt(document.getElementById('slice-infill').value);
  alert('正在调用切片引擎生成 G-code...');
  try {
    const res = await fetch('/api/slice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem, material: mat, layer_height: lh, infill_density: infill })
    });
    const data = await res.json();
    if (data.success) {
      document.getElementById('stat-time').innerText = data.metrics.time_formatted || '就绪';
      document.getElementById('stat-filament').innerText = `耗材: ${data.metrics.filament_weight_g} g (${data.metrics.layer_count} 层)`;
      alert(`✅ 切片成功！耗时约 ${data.metrics.time_formatted}，耗材约 ${data.metrics.filament_weight_g}g`);
    } else {
      alert('❌ 切片失败: ' + data.error);
    }
  } catch (e) {
    alert('请求失败: ' + e);
  }
}

async function triggerDispatch() {
  const url = document.getElementById('printer-url').value;
  const ptype = document.getElementById('printer-type').value;
  const code = document.getElementById('printer-code').value;
  if (!url) {
    alert('请输入打印机地址 (如 http://192.168.1.100:7125)');
    return;
  }
  alert('正在向局域网打印机推送切片/模型文件...');
  try {
    const res = await fetch('/api/dispatch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem, server_url: url, printer_type: ptype, access_code: code })
    });
    const data = await res.json();
    if (data.success) {
      alert('✅ 推送成功: ' + data.message);
    } else {
      alert('❌ 推送失败: ' + data.message);
    }
  } catch (e) {
    alert('推送异常: ' + e);
  }
}

async function triggerExportLib() {
  try {
    const res = await fetch('/api/export_library', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem, namespace: 'snapscad' })
    });
    const data = await res.json();
    if (data.success) {
      alert('✅ 模块生态库导出成功: ' + data.lib_file);
    } else {
      alert('❌ 导出失败: ' + data.error);
    }
  } catch (e) {
    alert('导出异常: ' + e);
  }
}

window.onload = function() {
  init3D();
  loadModels();
};
</script>
</body>
</html>
"""
    target_path.write_text(html_content, encoding="utf-8")
    _log.info("[workspace] 已生成在线协同建模工作台: %s", target_path.name)
    return target_path
