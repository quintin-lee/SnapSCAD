# OpenSCAD Agent

图片 → 3D 模型 自动化 Agent：给定一张产品/零件照片，自动识别几何结构，
生成 OpenSCAD (`.scad`) 源码并渲染导出可 3D 打印的 STL/OBJ/3MF 模型。

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
- **便携式 HTML 3D 查看器**：导出 STL 同步生成内嵌 Three.js 查看器，双击即可在任何浏览器 360° 旋转检视。
- **参数化 Customizer 支持**：自定义参数直接生成 OpenSCAD 全局变量，在 OpenSCAD 中可拖动滑块交互式微调。
- **零第三方渲染依赖**：只调用系统 `openscad` 与 `magick` 可执行文件。

## 依赖

系统包：
```bash
sudo pacman -S openscad imagemagick   # Manjaro/Arch
```

Python（建议虚拟环境，仅标准库 + requests）：
```bash
python3 -m venv .venv
.venv/bin/python -m pip install requests
```

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
| `openscad_agent/core.py` | 配置、OpenSCAD 渲染器（多视角/精度控制）、图片预处理 |
| `openscad_agent/spec.py` | 规格校验与规范化层（类型净化/缺省补全/原语别名） |
| `openscad_agent/geometry.py` | 规格 → OpenSCAD 源码转译（支持 rotation / polyhedron 真顶点） |
| `openscad_agent/vision.py` | Agnes 视觉 API 客户端 + 渲染图多视角审查闭环 |
| `openscad_agent/agent.py` | 全流程编排 + 编译修复 + 审查闭环（回归保护/得分回退） |
| `openscad_agent/log.py` | 统一日志系统（控制台 + `out/agent.log` 文件落盘） |
| `main.py` | CLI 入口 |
| `gui.py` | PyQt6 图形界面（原图 vs 预览并排、参数持久化） |

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
