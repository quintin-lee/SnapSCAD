# SnapSCAD

[![CI](https://github.com/quintin-lee/SnapSCAD/actions/workflows/ci.yml/badge.svg)](https://github.com/quintin-lee/SnapSCAD/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![OpenSCAD](https://img.shields.io/badge/OpenSCAD-2021.01+-orange.svg)](https://openscad.org/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/quintin-lee/SnapSCAD/pulls)

> **Turn photos into parametric OpenSCAD models in a snap.**  
> 随手一拍，即刻代码造物。

**SnapSCAD** 是一个全自动多模态 3D CAD 逆向工程 Agent：给定单张或多张产品/零件照片或自然语言描述，自动反求几何结构，生成可参数化微调的 OpenSCAD (`.scad`) 源码，并自动化完成 CGAL 语法编译自愈、多视角审查闭环、适印性体检、双格式 3D 打印切片模型 (STL/3MF)、激光切割 2D 轮廓 (DXF/SVG)、便携式 3D 网页查看器与工程资产打包。

## 架构

```
 图片 (jpg/png)
   │  ① 预处理 (ImageMagick 缩放 ≤2048px)
   ▼
 Agnes Vision API (agnes-3.0-flash, OpenAI 兼容多模态)
   │  ② 识别 → 结构化 JSON 模型规格 (CSG 原语 + 孔洞 + 装配)
   ▼
 OpenSCAD 生成器 (geometry.py)
   │  ③ 规格 → .scad 源码
   ▼
 OpenSCAD CLI 校验
   │  ④ 失败 → 携编译错误回传 LLM 自动修正 (≤3 轮)
   ▼
 OpenSCAD 渲染导出  ⑤ .stl/.obj/.3mf + 2D 预览图
   ▼
 渲染图审查闭环  ⑥ 预览图 + 原图 → LLM 比对
   │  不符合 → 自动修正规格 + 重渲染 (≤3 轮，含回归保护)
   │  符合 / 达上限 → 输出最终模型
```

设计要点：
- **结构化中间表示 (IR)**：LLM 不直接写 OpenSCAD 代码，而是输出受 schema
  约束的 JSON 规格，再由确定性转译器生成源码，大幅降低语法错误率。
- **自动修复回路**：OpenSCAD 编译错误被回灌给 LLM，实现 self-healing 闭环。
- **渲染图审查闭环**：渲染出的预览图与原图回传 LLM 比对，不符合预期则
  自动修正规格并重渲染，直到符合或达上限；含回归保护（拒绝减少部件数的
  “简化型”修正）与得分回退（未收敛时保留迄今最好的一版）。
- **自动接地平贴 Z=0**：自动计算包围盒最低点并垂直对齐，确保模型平贴打印底板，开箱即切片。
- **局域网无线 3D 资产共享 (`--serve`)**：内置极简 HTTP 服务与局域网 IP 嗅探，一键在手机/平板浏览器中触摸旋转 3D 模型与展厅。
- **便携式 HTML 3D 查看器**：导出 STL 同步生成内嵌 Three.js 查看器，双击即可在任何浏览器 360° 旋转检视，支持实时截面剖切 (Clip Plane)、3D 尺寸标定盒、影棚明暗主题切换与自定义角度一键高清截屏。
- **现代化切片 3MF 联动导出**：默认同步生成紧凑的 `.3mf` 格式，原生适配 Bambu Studio / OrcaSlicer / PrusaSlicer。
- **3D 模型展厅画廊 (Showcase Gallery)**：自动聚合全部历史模型，生成现代化响应式 `gallery.html` 展厅，提供实时搜索与全套资源下载。
- **参数化 Customizer 支持**：自定义参数直接生成 OpenSCAD 全局变量，在 OpenSCAD 中可拖动滑块交互式微调。
- **高阶草图拉伸与阵列**：支持 2D polygon 直拉伸 (`linear_extrude`)、回转体 (`rotate_extrude`) 与环形/线性等距阵列 (`radial_array` / `linear_array`)。
- **3D 网页端实时截面剖切**：独立 HTML 查看器提供 Z 轴动态切片滑块，直观检查内部内腔与壁厚。
- **桌面级历史工程资产库**：GUI 自动归集所有历史生成模型，一键秒级全景回溯规格、源码与 3D 效果。
- **激光切割/CNC 2D投影导出**：支持一键将三维模型投射导出为工业标准 `.dxf` 与 `.svg`，无缝衔接激光切割机与 CNC 雕刻机。
- **多模型生态与本地离线免Key运行**：自动识别 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY`，并原生支持本地 Ollama / vLLM（指向 `localhost:11434` 自动免 Key 离线保密运行）。
- **工业化批量生产流水线**：CLI 支持 `--batch-dir` 自动对目录下的海量设计图连续建模并汇总至 3D 展厅画廊。
- **物理尺度自动归一化**：支持 `cm` / `inch` / `m` 国际单位制自动按比例缩放至标准 `mm`，杜绝尺寸缩水或暴涨。
- **切片工艺指导书与耗材预估**：自动输出工业级 Markdown 指导书（预估打印耗时、耗材克重、推荐层高、外圈数、陀螺仪填充图案与支撑策略）。
- **桌面级生产力控制**：GUI 具备任务实时一键安全中止（Abort）、智能工程公差约束追加（Enhance Prompt）。
- **极速平滑倒圆角 (Fillet) 与 45° 倒棱角 (Chamfer)**：立方体支持 `fillet` 倒圆角与 `chamfer` 45° 倒角，圆柱立柱支持顶部机械导入角，通过 2D `offset(chamfer=true)` 实现毫秒级无卡顿倒棱造型。
- **高阶 CSG 层级装配布尔运算**：装配层支持并集 (`union_parts`)、交集 (`intersection_parts`) 与布尔差集减除 (`difference_parts` / `difference_from`) 的任意组合。
- **多模型生态与提供商一键预设**：预设支持 Agnes AI、OpenAI、DeepSeek、Google Gemini、SiliconFlow (硅基流动)、Alibaba DashScope (通义千问)、Groq 与本地离线 Ollama/vLLM（免 Key 运行），内置 Token 截断防护与端点自动补齐。
- **全套工程资产打包 (All-in-One ZIP)**：CLI (`--zip`) 与 GUI 均支持一键将模型全量资产（3D、2D图纸、源码、查看器、工艺书）归档为单文件便携 `.zip`。
- **专业 CAD 语法高亮 IDE**：GUI 源码编辑器集成 `OpenScadHighlighter`，提供关键字、变换、原语、数值与注释的全彩语法着色。
- **自愈温控阶梯爬升**：编译重试自愈自动阶梯升温并注入针对性编译器错误诊断建议。
- **机械装配爆炸图 (Exploded View)**：自动生成装配向外发散位移向量与 OpenSCAD Customizer 参数 `explode = 0.0`，在 OpenSCAD 中可拖动滑块将内部隐藏零件向外炸开检视。
- **工业标准紧固件沉孔库**：孔洞原生支持 `standard: "M2"~"M8"`，自动转译为符合 ISO/DIN 规范的双阶梯沉头螺栓孔与公差配合。
- **局域网 3D 打印机一键直发**：GUI 操作栏集成打印机直连推送，原生适配 Klipper (Moonraker) 与 OctoPrint 网络切片直传。
- **桌面布局持久化记忆**：窗口尺寸、位置与三栏分割比例自动记忆恢复。
- **3D 立体文字与铭牌浮雕**：支持 `text` 原语，一键生成带字号、对齐与深度的 3D 浮雕与凹槽刻字标牌。
- **自动化质量基准测试套件**：内置 `test_agent.py`，支持 `main.py --test` 一键全自动化执行 8 大子系统回归自检。
- **GUI 国际化双语切换**：界面支持简体中文 / English 实时无缝热切，自动持久化语言偏好。
- **真实物理正交三视图与等轴测**：集成真实相机参数矩阵（`top`/`front`/`side`/`iso`），输出标准机械正交三视图与轴测图。
- **四视图工业蓝图拼版合成**：自动将主视、轴测、俯视、侧视拼版为 2x2 工业制图工程蓝图图纸 (`*_blueprint.png`)。
- **编译报错精准行号高亮与跳转**：GUI 在线微调编译失败时，自动解析 OpenSCAD 错误行号，光标平滑跳转并以醒目红条高亮出错代码。
- **单件打印耗材制造成本核算**：体检系统自动测算实体克重与单件耗材制造成本（人民币 ¥ 与美元 $），写入切片指南。
- **3D 网页端多零件分色装配与交互零件树**：独立 HTML 查看器支持按每个部件独立真实语义色彩渲染，侧边栏内置零件装配树与实时 `[👁️]` 显隐复选框。
- **规格树与源码双向联动高亮**：GUI 中单击树形规格中的部件节点，代码编辑器自动平滑定位并亮蓝高亮对应的 `module <id>()` 定义。
- **装配空间干涉与碰撞自检**：体检系统自动测算多部件之间的包围盒空间侵入体积与贴合接触面积，预防意外几何穿透。
- **Canny 物理几何边缘轮廓并联**：自动提取高反差物理轮廓图并送入多模态大模型，彻底消除反光与阴影纹理干扰，直击机械物理轮廓与孔位边界。
- **源码版本快照与一键回滚**：编辑区支持保存多版本增量快照 (`_v1.scad`, `_v2.scad`)，并提供时间戳选择器一键快速撤销回退。
- **零第三方渲染依赖**：只调用系统 `openscad` 与 `magick` 可执行文件。

## 依赖

系统包：
```bash
sudo pacman -S openscad imagemagick   # Manjaro/Arch
```

Python（建议虚拟环境，支持标准 pip 安装）：
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```
安装后即可直接全局调用 `snapscad` (CLI) 或 `snapscad-gui` (GUI) 命令！

## 配置

Agnes API key 通过环境变量提供（任一）：
```bash
export AGNES_API_KEY=sk-...
# 或 AGNES_API_TOKEN / APIHUB_AGNES_API_KEY
```
Base URL 默认 `https://apihub.agnes-ai.com`，模型默认 `agnes-3.0-flash`。

## 使用

### CLI

```bash
# 1) 图片建模（支持单张或多角度照片联合建模）
.venv/bin/python main.py --image ./photos/gearbox_front.jpg ./photos/gearbox_side.jpg

# 2) 导出 OBJ 而非 STL
.venv/bin/python main.py -i photo.png -f obj

# 3) 纯文字描述建模
.venv/bin/python main.py --describe "一个 M3 螺栓, 12mm 长, 圆柱头"

# 4) 只生成 .scad 不渲染
.venv/bin/python main.py -i photo.jpg --no-render

# 5) 批量图片工业化建模流水线
.venv/bin/python main.py --batch-dir ./my_sketches/ -o ./out_batch

# 6) 一键生成或更新 3D 模型展厅画廊
.venv/bin/python main.py --gallery

# 7) 启动交互式终端建模向导
snapscad --wizard

# 8) 启动局域网 3D 资产共享服务 (供手机/平板无线浏览)
snapscad --serve 8080

# 9) 执行全套自动化质量基准与回归测试
snapscad --test

# 查看 LLM 识别用的系统提示词/规格 schema
.venv/bin/python main.py -i photo.jpg --show-prompt
```

### 图形界面 (PyQt)

```bash
.venv/bin/python gui.py
```

界面分三栏：
- **左** 输入：选图片 / 输入文字描述 / 补充说明 / 导出格式与输出目录
- **中** 结果：LLM 识别出的模型规格 JSON 树 + 生成的 OpenSCAD 源码
- **右** 预览：渲染的 2D 预览图 + 输出文件列表

底部为实时日志控制台。点击「开始建模」后台执行全流程。

输出位于 `./out/`：
- `<name>_spec.json` — LLM 识别出的模型规格
- `<name>.scad` — 生成的 OpenSCAD 源码
- `<name>.stl` (或所选格式) — 可打印 3D 模型
- `<name>_preview.png` — OpenSCAD 2D 渲染预览
- `<name>_viewer.html` — 便携式 3D 网页交互查看器（双击直接在浏览器离线检视）

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `openscad_agent/core.py` | 配置、OpenSCAD 渲染器（多视角/精度控制/binstl）、图片预处理、3D网页查看器 |
| `openscad_agent/spec.py` | 规格校验与规范化层（类型净化/缺省补全/原语别名/锥台） |
| `openscad_agent/doctor.py` | 3D 打印适印性自检系统（壁厚/微孔/细长比/热床尺寸诊断） |
| `openscad_agent/gallery.py` | 3D 资产展厅画廊生成器（响应式网页、卡片搜索、一键下载） |
| `openscad_agent/packager.py` | 全套 CAD 工程资产归档打包器（一键导出便携式工程 ZIP） |
| `openscad_agent/server.py` | 局域网 3D 资产共享 Web 服务器（支持手机/多端无线 3D 交互） |
| `openscad_agent/geometry.py` | 规格 → OpenSCAD 源码转译（支持 rotation/capsule/mirror/接地Z=0） |
| `openscad_agent/vision.py` | Agnes 视觉 API 客户端 + 连续会话审查闭环 + Few-Shot 提示词 |
| `openscad_agent/agent.py` | 全流程编排 + 编译修复 + 会话链审查闭环 + 多图联合输入 |
| `openscad_agent/log.py` | 统一日志系统（控制台 + `out/agent.log` 文件落盘） |
| `main.py` | CLI 入口（支持多图、格式选择、适印性报告） |
| `gui.py` | PyQt6 图形界面（多图拖拽、剪贴板截图、预设库、在线微调） |

## 规格 JSON 结构

LLM 输出的中间表示（完整 schema 见 `core.SPEC_SCHEMA`）：

```jsonc
{
  "name": "gearbox_housing",
  "description": "齿轮箱外壳, 带安装孔",
  "units": "mm",
  "params": { "wall_thickness": 2.0 },
  "parts": [
    {
      "id": "body",
      "primitive": "cube",
      "size": [80, 50, 40],
      "center": [40, 25, 20],
      "holes": [
        { "primitive": "cylinder", "size": [8, 40], "center": [10, 25, 20] }
      ]
    }
  ],
  "assembly": { "union_parts": ["body", "lid"] },
  "print_tips": ["建议 20% 填充, 0.2mm 层高"]
}
```

## 已知限制

- LLM 对复杂自由曲面只能做 CSG 近似（已在规格 `note` 字段标注近似方式）。
- 识别精度依赖图片质量；审查闭环支持多视角比对（top/front/iso/perspective）。
- `polyhedron` 支持 LLM 直接输出 `points`/`faces` 顶点数据驱动真实几何；数据缺失或非法时降级为 box。
