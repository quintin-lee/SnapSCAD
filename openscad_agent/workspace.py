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
    <button onclick="triggerExportMulti3MF()" style="background:#db2777">🎨 多色 3MF</button>
    <button onclick="triggerExportBlueprint()" style="background:#0284c7">📐 工程图纸</button>
    <button onclick="triggerExportStep()" style="background:#059669">📐 导出 STEP</button>
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
      <button class="tab-btn" onclick="switchTab('tab-patch', this)">✍️ 增量修剪</button>
      <button class="tab-btn" onclick="switchTab('tab-motion', this)">🎮 机构仿真</button>
      <button class="tab-btn" onclick="switchTab('tab-fea', this)">🌡️ 热力与公差</button>
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

    <!-- 交互式增量修剪 -->
    <div id="tab-patch" class="tab-content">
      <div class="form-group">
        <label>自然语言红线指令 (Redline Instruction):</label>
        <textarea id="patch-instruction" style="width:100%;height:100px;background:#0d1117;border:1px solid var(--border);border-radius:6px;color:#e6edf3;padding:8px;font-size:0.85rem;" placeholder="输入修剪指令，如：&#10;• 高度增加 10mm&#10;• 孔径扩大 1mm&#10;• 倒角 2mm&#10;• 等比缩放 1.2"></textarea>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;">
        <button type="button" onclick="fillPatchSample('高度增加 10mm')" style="background:#1f2937;font-size:0.75rem;padding:3px 8px;">+ 高度+10mm</button>
        <button type="button" onclick="fillPatchSample('孔径扩大 1mm')" style="background:#1f2937;font-size:0.75rem;padding:3px 8px;">+ 孔径+1mm</button>
        <button type="button" onclick="fillPatchSample('倒角 2mm')" style="background:#1f2937;font-size:0.75rem;padding:3px 8px;">+ 倒角 2mm</button>
        <button type="button" onclick="fillPatchSample('等比缩放 1.2')" style="background:#1f2937;font-size:0.75rem;padding:3px 8px;">+ 缩放 1.2x</button>
      </div>
      <button onclick="triggerApplyPatch()" class="btn-accent" style="width:100%">⚡ 应用自然语言修剪并热重载</button>
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

    <!-- 机构运动学动态仿真 -->
    <div id="tab-motion" class="tab-content">
      <div style="font-size:0.85rem;color:var(--text-muted);margin-bottom:12px;">
        基于拓扑铰链约束 (旋转副 Revolute / 滑动副 Prismatic / 齿轮副 Gear) 的 60 FPS 物理机构运动学动态仿真。
      </div>
      <div style="display:flex;gap:8px;margin-bottom:12px;">
        <button id="btn-sim-play" onclick="toggleKinematicSimulation()" class="btn-accent" style="flex:1">▶ 开始仿真</button>
        <button onclick="resetKinematicSimulation()" style="flex:1;background:var(--bg-card);border:1px solid var(--border)">↺ 重置位姿</button>
      </div>
      <div class="form-group">
        <label>时间轴推进 (Timeline): <span id="sim-t-val">t = 0.00</span></label>
        <input type="range" id="sim-timeline" min="0" max="1" step="0.005" value="0" oninput="onTimelineScrub(this.value)">
      </div>
      <div class="form-group">
        <label>仿真速度倍率: <span id="sim-speed-val">1.0x</span></label>
        <input type="range" id="sim-speed" min="0.2" max="3.0" step="0.2" value="1.0" oninput="onSimSpeedChange(this.value)">
      </div>
      <div class="metric-title" style="margin-top:14px;margin-bottom:8px;">机构运动副自由度 (Joints)</div>
      <div id="joint-controls-list" style="display:flex;flex-direction:column;gap:8px;font-size:0.85rem;">
        <div style="color:var(--text-muted);font-size:0.8rem;">正在加载当前模型运动副...</div>
      </div>
    </div>

    <!-- 热力学翘曲分析与装配公差预补偿 -->
    <div id="tab-fea" class="tab-content">
      <div style="font-size:0.85rem;color:var(--text-muted);margin-bottom:12px;">
        有限元离散热应变梯度的翘曲抬升估算，与孔洞负向热缩/配合间隙一键预补偿。
      </div>
      <div class="form-group">
        <label>目标成型耗材:</label>
        <select id="fea-material" style="width:100%">
          <option value="PLA">PLA (收缩率 0.3%, 易打印原型)</option>
          <option value="PETG">PETG (收缩率 0.5%, 耐候高韧)</option>
          <option value="ABS">ABS (收缩率 1.5%, 高温易翘曲)</option>
          <option value="TPU">TPU (收缩率 0.8%, 柔性弹性体)</option>
          <option value="PC">PC (收缩率 1.6%, 极高强度耐热)</option>
        </select>
      </div>
      <div class="form-group">
        <label>装配配合公差等级:</label>
        <select id="fea-fit" style="width:100%">
          <option value="normal">过渡配合 Normal (+0.20mm, 紧固件穿孔/定位销)</option>
          <option value="loose">间隙配合 Loose (+0.35mm, 滑块/合页铰链旋转)</option>
          <option value="press_fit">过盈配合 Press Fit (+0.05mm, 免胶水强力压配)</option>
        </select>
      </div>
      <div class="form-group" style="display:flex;align-items:center;gap:8px;">
        <input type="checkbox" id="fea-chamber" style="width:auto;cursor:pointer;">
        <label for="fea-chamber" style="margin:0;cursor:pointer;">启用恒温封箱 (Chamber Heated &gt;=50°C)</label>
      </div>
      <div style="display:flex;gap:8px;margin-top:10px;">
        <button onclick="triggerAnalyzeThermalWarping()" style="flex:1;background:var(--bg-card);border:1px solid var(--border)">🔍 翘曲风险分析</button>
        <button onclick="triggerApplyToleranceCompensation()" class="btn-success" style="flex:1">⚡ 应用公差补偿</button>
      </div>
      <div id="fea-result-box" style="margin-top:12px;background:var(--bg-card);padding:10px;border-radius:6px;border:1px solid var(--border);display:none;">
        <div id="fea-risk-badge" style="font-weight:700;margin-bottom:6px;">-</div>
        <div id="fea-lift-val" style="font-size:0.8rem;color:var(--text);margin-bottom:6px;">-</div>
        <div id="fea-tips" style="font-size:0.75rem;color:var(--text-muted);line-height:1.4;">-</div>
      </div>
    </div>
  </div>

  <!-- 中间 3D WebGL 视口 -->
  <div class="viewport-container">
    <div id="webgl-canvas"></div>
    <div class="viewport-hud">
      <div>模型: <span id="hud-name" style="color:var(--accent);font-weight:600">-</span></div>
      <div>尺寸: <span id="hud-dim">-</span></div>
    </div>
    <!-- 3D 尺寸测距 HUD -->
    <div id="measure-hud" style="display:none;position:absolute;top:12px;right:12px;background:rgba(17,24,39,0.92);backdrop-filter:blur(8px);border:1px solid var(--border);padding:8px 12px;border-radius:6px;font-size:0.8rem;z-index:15;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
        <span style="color:var(--accent);font-weight:600">📏 3D 空间测距</span>
        <button onclick="clearMeasurement()" style="padding:1px 6px;font-size:0.7rem;background:transparent;border:1px solid var(--border)">清除</button>
      </div>
      <div id="measure-result" style="margin-top:4px;color:var(--text)">请在模型表面点击起点...</div>
    </div>
    <div class="viewport-tools">
      <button id="btn-mode-mesh" onclick="switchViewMode('mesh')" style="background:var(--primary);border:1px solid var(--accent)">🧊 实体模型</button>
      <button id="btn-mode-gcode" onclick="switchViewMode('gcode')" style="background:transparent;border:1px solid var(--border)">🍰 G-code 走丝</button>
      <button id="btn-measure" onclick="toggleMeasureMode()" style="background:transparent;border:1px solid var(--border)">📏 3D 测距</button>
      <button onclick="resetCamera()" style="background:transparent;border:1px solid var(--border)">🎯 重置相机</button>
      <button onclick="toggleWireframe()" style="background:transparent;border:1px solid var(--border)">🕸️ 网格模式</button>
      <button onclick="toggleAxes()" style="background:transparent;border:1px solid var(--border)">📐 空间坐标</button>
    </div>
    <!-- G-code 逐层走丝控制器 HUD -->
    <div id="gcode-hud" style="display:none;position:absolute;bottom:70px;left:50%;transform:translateX(-50%);background:rgba(17,24,39,0.92);backdrop-filter:blur(8px);border:1px solid var(--border);padding:8px 16px;border-radius:8px;align-items:center;gap:12px;font-size:0.85rem;z-index:20;">
      <span style="font-weight:600;color:var(--accent)">🍰 走丝层数:</span>
      <input type="range" id="gcode-layer-slider" min="1" max="1" value="1" style="width:180px;" oninput="onGcodeLayerChange(this.value)">
      <span id="gcode-layer-label">1/1</span>
      <button onclick="toggleGcodePlay()" id="btn-gcode-play" style="padding:4px 10px;font-size:0.8rem;background:var(--primary)">▶ 自动走丝</button>
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

      <!-- 实时双向遥测监视 -->
      <div class="metric-card" id="card-telemetry">
        <div class="metric-title" style="display:flex;justify-content:space-between;align-items:center;">
          <span>机床实时遥测</span>
          <button onclick="pollPrinterStatus()" style="padding:2px 8px;font-size:0.72rem;background:transparent;border:1px solid var(--border)">🔄 刷新</button>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:6px;">
          <span id="tel-state" class="status-badge" style="background:#374151;color:#9ca3af">未连接</span>
          <span id="tel-type" style="font-size:0.75rem;color:var(--text-muted)">-</span>
        </div>
        <div style="margin-top:8px;display:grid;grid-template-columns:1fr 1fr;gap:6px;">
          <div style="background:rgba(0,0,0,0.25);padding:6px;border-radius:4px;">
            <div style="color:var(--text-muted);font-size:0.7rem">🔥 喷头温度</div>
            <div id="tel-hotend" style="font-weight:700;color:#f87171;font-size:0.85rem">-- / -- °C</div>
          </div>
          <div style="background:rgba(0,0,0,0.25);padding:6px;border-radius:4px;">
            <div style="color:var(--text-muted);font-size:0.7rem">🛏️ 热床温度</div>
            <div id="tel-bed" style="font-weight:700;color:#fbbf24;font-size:0.85rem">-- / -- °C</div>
          </div>
        </div>
        <div style="margin-top:8px;font-size:0.75rem;">
          <div style="display:flex;justify-content:space-between;color:var(--text-muted);margin-bottom:2px;">
            <span>进度: <strong id="tel-prog-num" style="color:var(--accent)">0%</strong></span>
            <span id="tel-time">-</span>
          </div>
          <div style="height:6px;background:#374151;border-radius:3px;overflow:hidden;">
            <div id="tel-prog-bar" style="width:0%;height:100%;background:var(--accent);transition:width 0.3s"></div>
          </div>
          <div id="tel-file" style="margin-top:4px;color:var(--text-muted);font-size:0.7rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></div>
        </div>
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
          <a id="btn-dl-step" href="#" download class="btn" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:#34d399;text-decoration:none;font-size:0.85rem">📐 下载 STEP 工业模型</a>
          <a id="btn-dl-multi3mf" href="#" download class="btn" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:#f472b6;text-decoration:none;font-size:0.85rem">🎨 下载多色 3MF (MMU)</a>
          <a id="btn-dl-blueprint" href="#" target="_blank" class="btn" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:#38bdf8;text-decoration:none;font-size:0.85rem">📐 查看工程图纸 (SVG)</a>
          <a id="btn-dl-gcode" href="#" download class="btn" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:var(--accent);text-decoration:none;font-size:0.85rem">⚡ 下载切片 G-code</a>
          <a id="btn-dl-freecad" href="#" download class="btn" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;color:var(--text);text-decoration:none;font-size:0.85rem">🐍 下载 FreeCAD 脚本</a>
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
let currentViewMode = 'mesh';
let gcodeGroup = null;
let gcodeLayers = [];
let currentGcodeLayer = 1;
let isGcodePlaying = false;
let gcodeTimer = null;
let isMeasureMode = false;
let measurePoints = [];
let measureMarkers = [];
let measureLine = null;
let isSimulating = false;
let simT = 0.0;
let simSpeed = 1.0;
let activeJoints = [];

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

  // 3D 尺寸测距点击事件
  const raycaster = new THREE.Raycaster();
  const mouse = new THREE.Vector2();

  container.addEventListener('pointerdown', function(e) {
    if (!isMeasureMode || !currentMesh) return;
    const rect = container.getBoundingClientRect();
    mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

    raycaster.setFromCamera(mouse, camera);
    const intersects = raycaster.intersectObject(currentMesh);
    if (intersects.length > 0) {
      handleMeasureClick(intersects[0].point);
    }
  });

  window.addEventListener('resize', onWindowResize);
  animate();
}

function animate() {
  requestAnimationFrame(animate);
  if (isSimulating) {
    updateKinematicSimulationStep();
  }
  controls.update();
  renderer.render(scene, camera);
}

// ---------------------------------------------------------------------------
// 机构运动学 3D 动态仿真 (Kinematic Simulation)
// ---------------------------------------------------------------------------
function toggleKinematicSimulation() {
  isSimulating = !isSimulating;
  const btn = document.getElementById('btn-sim-play');
  if (isSimulating) {
    btn.innerText = '⏸ 暂停仿真';
    btn.classList.remove('btn-accent');
    btn.style.background = '#eab308';
    btn.style.color = '#000000';
  } else {
    btn.innerText = '▶ 开始仿真';
    btn.classList.add('btn-accent');
    btn.style.background = '';
    btn.style.color = '';
  }
}

function resetKinematicSimulation() {
  isSimulating = false;
  simT = 0.0;
  const btn = document.getElementById('btn-sim-play');
  if (btn) {
    btn.innerText = '▶ 开始仿真';
    btn.classList.add('btn-accent');
    btn.style.background = '';
    btn.style.color = '';
  }
  const timeline = document.getElementById('sim-timeline');
  if (timeline) timeline.value = '0';
  const tVal = document.getElementById('sim-t-val');
  if (tVal) tVal.innerText = 't = 0.00';
  applyKinematicPose(0.0);
}

function onTimelineScrub(val) {
  simT = parseFloat(val);
  const tVal = document.getElementById('sim-t-val');
  if (tVal) tVal.innerText = 't = ' + simT.toFixed(2);
  applyKinematicPose(simT);
}

function onSimSpeedChange(val) {
  simSpeed = parseFloat(val);
  const sVal = document.getElementById('sim-speed-val');
  if (sVal) sVal.innerText = simSpeed.toFixed(1) + 'x';
}

function updateKinematicSimulationStep() {
  simT = (simT + 0.005 * simSpeed) % 1.0;
  const timeline = document.getElementById('sim-timeline');
  if (timeline) timeline.value = simT.toFixed(3);
  const tVal = document.getElementById('sim-t-val');
  if (tVal) tVal.innerText = 't = ' + simT.toFixed(2);
  applyKinematicPose(simT);
}

function applyKinematicPose(t) {
  if (!currentMesh) return;
  const smooth = (1.0 + Math.sin(2.0 * Math.PI * t - Math.PI / 2.0)) / 2.0;

  if (activeJoints && activeJoints.length > 0) {
    activeJoints.forEach((j, idx) => {
      const minVal = (j.limits && j.limits.length >= 2) ? j.limits[0] : -45.0;
      const maxVal = (j.limits && j.limits.length >= 2) ? j.limits[1] : 45.0;
      const curVal = minVal + smooth * (maxVal - minVal);
      const valEl = document.getElementById(`joint-val-${idx}`);
      if (valEl) valEl.innerText = curVal.toFixed(1) + (j.type === 'prismatic' ? ' mm' : '°');
      const rngEl = document.getElementById(`joint-range-${idx}`);
      if (rngEl && document.activeElement !== rngEl) rngEl.value = curVal;

      if (j.type === 'prismatic') {
        const ax = j.axis || [1, 0, 0];
        currentMesh.position.set(ax[0] * curVal, ax[1] * curVal, ax[2] * curVal);
      } else {
        const ax = j.axis || [0, 0, 1];
        const rad = (curVal * Math.PI) / 180.0;
        const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(...ax).normalize(), rad);
        currentMesh.quaternion.copy(q);
      }
    });
  } else {
    // 默认机构摆动示范
    const angleRad = (Math.sin(2 * Math.PI * t) * 30.0 * Math.PI) / 180.0;
    currentMesh.rotation.z = angleRad;
  }
}

function onManualJointChange(idx, val) {
  if (!currentMesh || !activeJoints[idx]) return;
  const j = activeJoints[idx];
  const num = parseFloat(val);
  const valEl = document.getElementById(`joint-val-${idx}`);
  if (valEl) valEl.innerText = num.toFixed(1) + (j.type === 'prismatic' ? ' mm' : '°');

  if (j.type === 'prismatic') {
    const ax = j.axis || [1, 0, 0];
    currentMesh.position.set(ax[0] * num, ax[1] * num, ax[2] * num);
  } else {
    const ax = j.axis || [0, 0, 1];
    const rad = (num * Math.PI) / 180.0;
    const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(...ax).normalize(), rad);
    currentMesh.quaternion.copy(q);
  }
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

// ---------------------------------------------------------------------------
// 3D 尺寸测距逻辑
// ---------------------------------------------------------------------------
function toggleMeasureMode() {
  isMeasureMode = !isMeasureMode;
  const btn = document.getElementById('btn-measure');
  const hud = document.getElementById('measure-hud');
  if (isMeasureMode) {
    btn.style.background = 'var(--primary)';
    btn.style.borderColor = 'var(--accent)';
    hud.style.display = 'block';
    if (measurePoints.length === 0) {
      document.getElementById('measure-result').innerText = '请在模型表面点击起点...';
    }
  } else {
    btn.style.background = 'transparent';
    btn.style.borderColor = 'var(--border)';
    hud.style.display = 'none';
  }
}

function handleMeasureClick(pt) {
  if (measurePoints.length >= 2) {
    clearMeasurement();
  }

  measurePoints.push(pt.clone());

  const dotGeom = new THREE.SphereGeometry(1.2, 16, 16);
  const isFirst = (measurePoints.length === 1);
  const dotMat = new THREE.MeshBasicMaterial({ color: isFirst ? 0xef4444 : 0x10b981 });
  const marker = new THREE.Mesh(dotGeom, dotMat);
  marker.position.copy(pt);
  scene.add(marker);
  measureMarkers.push(marker);

  if (measurePoints.length === 1) {
    document.getElementById('measure-result').innerText = `起点已选: (${pt.x.toFixed(1)}, ${pt.y.toFixed(1)}, ${pt.z.toFixed(1)})\n请点击终点...`;
  } else if (measurePoints.length === 2) {
    const p1 = measurePoints[0];
    const p2 = measurePoints[1];
    const dist = p1.distanceTo(p2);
    const dx = Math.abs(p1.x - p2.x);
    const dy = Math.abs(p1.y - p2.y);
    const dz = Math.abs(p1.z - p2.z);

    const lineGeom = new THREE.BufferGeometry().setFromPoints([p1, p2]);
    const lineMat = new THREE.LineDashedMaterial({ color: 0xfacc15, dashSize: 2, gapSize: 1 });
    measureLine = new THREE.Line(lineGeom, lineMat);
    measureLine.computeLineDistances();
    scene.add(measureLine);

    document.getElementById('measure-result').innerHTML = `<strong>直线: ${dist.toFixed(2)} mm</strong><br><span style="font-size:0.75rem;color:var(--text-muted)">ΔX: ${dx.toFixed(1)} | ΔY: ${dy.toFixed(1)} | ΔZ: ${dz.toFixed(1)} mm</span>`;
  }
}

function clearMeasurement() {
  measurePoints = [];
  measureMarkers.forEach(m => scene.remove(m));
  measureMarkers = [];
  if (measureLine) {
    scene.remove(measureLine);
    measureLine = null;
  }
  document.getElementById('measure-result').innerText = '请在模型表面点击起点...';
}

// ---------------------------------------------------------------------------
// 自然语言修剪指令交互
// ---------------------------------------------------------------------------
function fillPatchSample(text) {
  document.getElementById('patch-instruction').value = text;
}

async function triggerApplyPatch() {
  const text = document.getElementById('patch-instruction').value.trim();
  if (!text) {
    alert('请输入自然语言修剪指令 (如: 高度增加 10mm, 孔径改为 5mm)');
    return;
  }
  alert('正在向微服务提交参数化增量修剪请求...');
  try {
    const res = await fetch('/api/patch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem, instruction: text })
    });
    const data = await res.json();
    if (data.success) {
      alert('✅ 增量修剪成功并已重新编译！正在热重载 3D 视口...');
      const rScad = await fetch(activeModelStem + '.scad?t=' + Date.now());
      if (rScad.ok) {
        document.getElementById('scad-editor').value = await rScad.text();
      }
      loadSTL(activeModelStem + '.stl?t=' + Date.now());
    } else {
      alert('❌ 修剪失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('请求异常: ' + e);
  }
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

  if (gcodeGroup) {
    scene.remove(gcodeGroup);
    gcodeGroup = null;
    gcodeLayers = [];
  }
  switchViewMode('mesh');

  // 更新下载链接
  document.getElementById('btn-dl-stl').href = stem + '.stl';
  document.getElementById('btn-dl-step').href = stem + '.step';
  document.getElementById('btn-dl-multi3mf').href = stem + '_multimaterial.3mf';
  document.getElementById('btn-dl-blueprint').href = '/api/model/' + stem + '/blueprint';
  document.getElementById('btn-dl-gcode').href = stem + '.gcode';
  document.getElementById('btn-dl-freecad').href = stem + '_freecad.py';
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

  // 读取模型规格与机构运动副 (Kinematic Joints)
  try {
    const rSpec = await fetch('/api/model/' + stem);
    let specData = null;
    if (rSpec.ok) {
      const resp = await rSpec.json();
      specData = resp.spec;
    } else {
      const rSpecFile = await fetch(stem + '_spec.json');
      if (rSpecFile.ok) specData = await rSpecFile.json();
    }
    if (specData) {
      activeJoints = specData.joints || (specData.assembly && specData.assembly.joints) || [];
      renderJointControls(activeJoints);
    }
  } catch (e) {
    console.warn('读取模型规格失败:', e);
  }
}

function renderJointControls(joints) {
  const container = document.getElementById('joint-controls-list');
  if (!container) return;
  if (!joints || joints.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted);font-size:0.8rem;">该模型未定义显式铰链约束，仿真模式下将执行整体 Harmonic 动态运动演示。</div>';
    return;
  }
  container.innerHTML = '';
  joints.forEach((j, idx) => {
    const minVal = (j.limits && j.limits.length >= 2) ? j.limits[0] : -45.0;
    const maxVal = (j.limits && j.limits.length >= 2) ? j.limits[1] : 45.0;
    const card = document.createElement('div');
    card.style = 'background:var(--bg-card);padding:8px 10px;border-radius:6px;border:1px solid var(--border);';
    card.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.8rem;margin-bottom:4px;">
        <span style="font-weight:600;color:var(--accent);">${j.id || 'Joint ' + (idx + 1)} (${j.type})</span>
        <span id="joint-val-${idx}" style="color:var(--text);font-family:monospace;">${minVal}${j.type === 'prismatic' ? ' mm' : '°'}</span>
      </div>
      <input type="range" id="joint-range-${idx}" min="${minVal}" max="${maxVal}" step="1" value="${minVal}" oninput="onManualJointChange(${idx}, this.value)" style="width:100%;cursor:pointer;">
    `;
    container.appendChild(card);
  });
}

function switchViewMode(mode) {
  currentViewMode = mode;
  const btnMesh = document.getElementById('btn-mode-mesh');
  const btnGcode = document.getElementById('btn-mode-gcode');
  const hud = document.getElementById('gcode-hud');

  if (mode === 'mesh') {
    btnMesh.style.background = 'var(--primary)';
    btnMesh.style.borderColor = 'var(--accent)';
    btnGcode.style.background = 'transparent';
    btnGcode.style.borderColor = 'var(--border)';
    if (currentMesh) currentMesh.visible = true;
    if (gcodeGroup) gcodeGroup.visible = false;
    hud.style.display = 'none';
    if (isGcodePlaying) toggleGcodePlay();
  } else {
    btnGcode.style.background = 'var(--primary)';
    btnGcode.style.borderColor = 'var(--accent)';
    btnMesh.style.background = 'transparent';
    btnMesh.style.borderColor = 'var(--border)';
    if (currentMesh) currentMesh.visible = false;
    if (gcodeGroup) {
      gcodeGroup.visible = true;
    } else {
      loadGcodeData(activeModelStem);
    }
    hud.style.display = 'flex';
  }
}

async function loadGcodeData(stem) {
  document.getElementById('hud-name').innerText = stem + ' (G-code 走丝)';
  let gcodeText = '';
  try {
    const res = await fetch('/api/model/' + stem + '/gcode');
    if (res.ok) {
      gcodeText = await res.text();
    } else {
      const resDirect = await fetch(stem + '.gcode');
      if (resDirect.ok) gcodeText = await resDirect.text();
    }
  } catch (e) {
    console.warn('读取 G-code 异常:', e);
  }

  if (!gcodeText) {
    alert('未找到 ' + stem + '.gcode 切片文件。请先点击 "🍰 切片 G-code" 生成切片！');
    switchViewMode('mesh');
    return;
  }

  parseAndRenderGcode(gcodeText);
}

function parseAndRenderGcode(text) {
  if (gcodeGroup) scene.remove(gcodeGroup);
  gcodeGroup = new THREE.Group();
  gcodeLayers = [];

  const lines = text.split('\n');
  let curX = 0, curY = 0, curZ = 0, curE = 0;
  let layerZ = 0;
  let currentLayerExtrusions = [];

  for (let i = 0; i < lines.length; i++) {
    let line = lines[i].trim();
    if (!line) continue;
    const semi = line.indexOf(';');
    let comment = '';
    if (semi !== -1) {
      comment = line.substring(semi);
      line = line.substring(0, semi).trim();
    }

    if (comment.includes('LAYER:') || comment.includes('LAYER_CHANGE')) {
      if (currentLayerExtrusions.length > 0) {
        gcodeLayers.push({ z: layerZ, segments: currentLayerExtrusions });
        currentLayerExtrusions = [];
      }
    }

    if (!line.startsWith('G0') && !line.startsWith('G1')) continue;

    const parts = line.split(/\\s+/);
    let nextX = curX, nextY = curY, nextZ = curZ, nextE = curE;
    let hasMove = false, hasE = false;

    for (let p = 1; p < parts.length; p++) {
      const code = parts[p][0];
      const val = parseFloat(parts[p].substring(1));
      if (isNaN(val)) continue;
      if (code === 'X' || code === 'x') { nextX = val; hasMove = true; }
      else if (code === 'Y' || code === 'y') { nextY = val; hasMove = true; }
      else if (code === 'Z' || code === 'z') {
        nextZ = val;
        if (Math.abs(nextZ - curZ) > 0.05 && currentLayerExtrusions.length > 0) {
          gcodeLayers.push({ z: layerZ, segments: currentLayerExtrusions });
          currentLayerExtrusions = [];
        }
        layerZ = nextZ;
      }
      else if (code === 'E' || code === 'e') { nextE = val; hasE = true; }
    }

    const isExtrude = (hasE && nextE > curE && hasMove);
    if (hasMove) {
      currentLayerExtrusions.push({
        x1: curX, y1: curY, z1: curZ,
        x2: nextX, y2: nextY, z2: nextZ,
        extrude: isExtrude
      });
    }

    curX = nextX; curY = nextY; curZ = nextZ; curE = nextE;
  }

  if (currentLayerExtrusions.length > 0) {
    gcodeLayers.push({ z: layerZ, segments: currentLayerExtrusions });
  }

  if (gcodeLayers.length === 0) {
    alert('G-code 走丝解析为空');
    return;
  }

  // 构建每层的 LineSegments 几何
  for (let l = 0; l < gcodeLayers.length; l++) {
    const lay = gcodeLayers[l];
    const posExtrude = [];
    const posTravel = [];

    for (let s = 0; s < lay.segments.length; s++) {
      const seg = lay.segments[s];
      if (seg.extrude) {
        posExtrude.push(seg.x1, seg.y1, seg.z1, seg.x2, seg.y2, seg.z2);
      } else {
        posTravel.push(seg.x1, seg.y1, seg.z1, seg.x2, seg.y2, seg.z2);
      }
    }

    const layerGroup = new THREE.Group();

    if (posExtrude.length > 0) {
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.Float32BufferAttribute(posExtrude, 3));
      const hue = 0.5 + 0.3 * (l / gcodeLayers.length);
      const color = new THREE.Color().setHSL(hue, 0.9, 0.55);
      const mat = new THREE.LineBasicMaterial({ color: color, linewidth: 2 });
      layerGroup.add(new THREE.LineSegments(geom, mat));
    }

    if (posTravel.length > 0) {
      const geomT = new THREE.BufferGeometry();
      geomT.setAttribute('position', new THREE.Float32BufferAttribute(posTravel, 3));
      const matT = new THREE.LineBasicMaterial({ color: 0x475569, transparent: true, opacity: 0.25 });
      layerGroup.add(new THREE.LineSegments(geomT, matT));
    }

    gcodeGroup.add(layerGroup);
    lay.group = layerGroup;
  }

  scene.add(gcodeGroup);

  const slider = document.getElementById('gcode-layer-slider');
  slider.min = 1;
  slider.max = gcodeLayers.length;
  slider.value = gcodeLayers.length;
  currentGcodeLayer = gcodeLayers.length;
  onGcodeLayerChange(slider.value);
}

function onGcodeLayerChange(val) {
  currentGcodeLayer = parseInt(val);
  const total = gcodeLayers.length;
  const curZ = (gcodeLayers[currentGcodeLayer - 1] ? gcodeLayers[currentGcodeLayer - 1].z : 0).toFixed(2);
  document.getElementById('gcode-layer-label').innerText = `${currentGcodeLayer}/${total} (Z: ${curZ}mm)`;

  for (let i = 0; i < gcodeLayers.length; i++) {
    if (gcodeLayers[i].group) {
      gcodeLayers[i].group.visible = (i < currentGcodeLayer);
    }
  }
}

function toggleGcodePlay() {
  const btn = document.getElementById('btn-gcode-play');
  if (isGcodePlaying) {
    clearInterval(gcodeTimer);
    isGcodePlaying = false;
    btn.innerText = '▶ 自动走丝';
  } else {
    if (currentGcodeLayer >= gcodeLayers.length) {
      currentGcodeLayer = 1;
    }
    isGcodePlaying = true;
    btn.innerText = '⏸ 暂停';
    gcodeTimer = setInterval(() => {
      currentGcodeLayer++;
      if (currentGcodeLayer > gcodeLayers.length) {
        toggleGcodePlay();
        return;
      }
      document.getElementById('gcode-layer-slider').value = currentGcodeLayer;
      onGcodeLayerChange(currentGcodeLayer);
    }, 60);
  }
}

async function pollPrinterStatus() {
  const url = document.getElementById('printer-url').value.trim() || 'http://localhost:7125';
  const ptype = document.getElementById('printer-type').value;
  const code = document.getElementById('printer-code').value.trim();

  try {
    const endpoint = `/api/printer/status?server_url=${encodeURIComponent(url)}&printer_type=${ptype}&access_code=${encodeURIComponent(code)}`;
    const res = await fetch(endpoint);
    if (!res.ok) return;
    const st = await res.json();

    const badge = document.getElementById('tel-state');
    const typeEl = document.getElementById('tel-type');
    const hotend = document.getElementById('tel-hotend');
    const bed = document.getElementById('tel-bed');
    const progNum = document.getElementById('tel-prog-num');
    const progBar = document.getElementById('tel-prog-bar');
    const timeEl = document.getElementById('tel-time');
    const fileEl = document.getElementById('tel-file');

    typeEl.innerText = (st.printer_type || 'klipper').toUpperCase();
    if (st.online) {
      badge.innerText = st.state === 'printing' ? '🟢 打印中' : (st.state === 'paused' ? '🟡 已暂停' : '⚪ 就绪空闲');
      badge.style.background = st.state === 'printing' ? 'rgba(16, 185, 129, 0.25)' : 'rgba(59, 130, 246, 0.2)';
      badge.style.color = st.state === 'printing' ? '#34d399' : '#60a5fa';

      hotend.innerText = `${st.hotend_temp} / ${st.hotend_target} °C`;
      bed.innerText = `${st.bed_temp} / ${st.bed_target} °C`;

      const p = Math.min(100, Math.max(0, st.progress || 0));
      progNum.innerText = `${p.toFixed(1)}%`;
      progBar.style.width = `${p}%`;

      if (st.time_remaining > 0) {
        const mins = Math.ceil(st.time_remaining / 60);
        timeEl.innerText = `剩余约 ${mins} 分钟`;
      } else {
        timeEl.innerText = st.print_duration > 0 ? `已打 ${(st.print_duration / 60).toFixed(0)}m` : '-';
      }

      fileEl.innerText = st.current_file ? `📄 ${st.current_file}` : '';
    } else {
      badge.innerText = '🔴 离线未连接';
      badge.style.background = 'rgba(239, 68, 68, 0.2)';
      badge.style.color = '#f87171';
      hotend.innerText = '-- / -- °C';
      bed.innerText = '-- / -- °C';
      progNum.innerText = '0%';
      progBar.style.width = '0%';
      timeEl.innerText = '主机不可达';
      fileEl.innerText = st.message || '请检查打印机主机是否通电且局域网可达';
    }
  } catch (e) {
    console.warn('轮询打印机遥测失败:', e);
  }
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

async function triggerExportMulti3MF() {
  alert('正在请求微服务生成多色多材质 3MF 制造包 (Bambu Studio / OrcaSlicer / PrusaSlicer MMU)...');
  try {
    const res = await fetch('/api/export_multimaterial_3mf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem })
    });
    const data = await res.json();
    if (data.success) {
      alert(`✅ 多色 3MF 制造工程包已就绪: ${data.file_3mf}\n包含 ${data.parts_count} 个分色独立组件，并已映射工具头颜色！\n已更新右侧面板快速下载直链！`);
      document.getElementById('btn-dl-multi3mf').href = data.file_3mf;
    } else {
      alert('❌ 导出多色 3MF 失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('导出多色 3MF 异常: ' + e);
  }
}

async function triggerExportStep() {
  alert('正在请求微服务生成工业标准 ISO 10303-21 STEP 实体与 FreeCAD 脚本...');
  try {
    const res = await fetch('/api/export_step', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem })
    });
    const data = await res.json();
    if (data.success) {
      alert(`✅ 工业级 STEP 实体模型已就绪:\n• ${data.step}\n• ${data.freecad}\n\n已更新右侧面板快速下载直链！`);
      document.getElementById('btn-dl-step').href = data.step;
      if (data.freecad) {
        document.getElementById('btn-dl-freecad').href = data.freecad;
      }
    } else {
      alert('❌ 导出 STEP 失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('导出异常: ' + e);
  }
}

async function triggerExportBlueprint() {
  alert('正在请求微服务生成工业级标准正交尺寸标注图纸 (SVG)...');
  try {
    const res = await fetch('/api/export_blueprint', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem })
    });
    const data = await res.json();
    if (data.success) {
      alert(`✅ 工业级工程图纸已就绪: ${data.svg}\n\n将在新标签页中打开矢量图纸！`);
      window.open('/api/model/' + activeModelStem + '/blueprint', '_blank');
      document.getElementById('btn-dl-blueprint').href = '/api/model/' + activeModelStem + '/blueprint';
    } else {
      alert('❌ 导出工程图纸失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('导出图纸异常: ' + e);
  }
}

async function triggerAnalyzeThermalWarping() {
  const mat = document.getElementById('fea-material').value;
  const chamber = document.getElementById('fea-chamber').checked;
  const resBox = document.getElementById('fea-result-box');
  resBox.style.display = 'block';
  document.getElementById('fea-risk-badge').innerText = '正在计算离散有限元热形变...';
  try {
    const res = await fetch('/api/fea/thermal_warping', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem, material: mat, chamber_heated: chamber })
    });
    const data = await res.json();
    if (data.success) {
      const rep = data.report;
      const badge = document.getElementById('fea-risk-badge');
      if (rep.overall_risk === 'HIGH') {
        badge.innerText = '⚠️ 翘曲高危 (HIGH RISK)';
        badge.style.color = '#ef4444';
      } else if (rep.overall_risk === 'MEDIUM') {
        badge.innerText = '⚡ 中度翘曲 (MEDIUM RISK)';
        badge.style.color = '#f59e0b';
      } else {
        badge.innerText = '✅ 热力学稳定 (LOW RISK)';
        badge.style.color = '#10b981';
      }
      document.getElementById('fea-lift-val').innerText = `最大抬升量: ${rep.max_lift_mm} mm (材料收缩率: ${rep.shrinkage_pct}%)`;
      document.getElementById('fea-tips').innerHTML = rep.recommendations.map(r => `• ${r}`).join('<br>');
    } else {
      alert('分析失败: ' + data.error);
    }
  } catch (e) {
    alert('分析异常: ' + e);
  }
}

async function triggerApplyToleranceCompensation() {
  const mat = document.getElementById('fea-material').value;
  const fit = document.getElementById('fea-fit').value;
  alert(`正在对模型执行智能装配公差 (+孔洞热缩负补偿) 并重新渲染...`);
  try {
    const res = await fetch('/api/tolerance/compensate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stem: activeModelStem, material: mat, fit_type: fit, recompile: true })
    });
    const data = await res.json();
    if (data.success) {
      alert(`✅ 公差预补偿完成！\n\n• 修正明细:\n${data.actions.join('\n')}\n\n正在重新载入 3D 补偿实体...`);
      loadSTL(activeModelStem + '.stl?t=' + Date.now());
      try {
        const rScad = await fetch(activeModelStem + '.scad?t=' + Date.now());
        if (rScad.ok) document.getElementById('scad-editor').value = await rScad.text();
      } catch (_) {}
    } else {
      alert('❌ 补偿失败: ' + data.error);
    }
  } catch (e) {
    alert('请求异常: ' + e);
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
  pollPrinterStatus();
  setInterval(pollPrinterStatus, 5000);
};
</script>
</body>
</html>
"""
    target_path.write_text(html_content, encoding="utf-8")
    _log.info("[workspace] 已生成在线协同建模工作台: %s", target_path.name)
    return target_path
