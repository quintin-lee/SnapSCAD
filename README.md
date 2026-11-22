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
- **多色多耗材 3MF 生产制造包与多喷头工具映射 (`multimaterial.py`)**：原生支持拓竹 Bambu Studio (AMS 4~16色)、OrcaSlicer、PrusaSlicer (MMU3/换刀机) 与双喷头 3D 打印；零外部重依赖纯原生生成符合 3MF Core & Material Extension 规范的标准工程包 (`*_multimaterial.3mf`)，自动将装配体各部件离散为独立 Mesh 构件，绑定 `<m:colorgroup>` 调色盘与颜色，并自动映射工具头编号（Extruder 1..N），切片软件直接识别为多构件装配，告别手动油漆桶分色。
- **工业级轻量热力学离散形变估算与装配公差孔径热缩预补偿 (`fea.py`)**：内置材料物性热膨胀与结晶收缩率数据库（PLA、PETG、ABS、PC、PA-CF/尼龙碳纤等）；自动执行网格化一维/二维热应力弯曲抬升测算与封箱控温建议（`estimate_thermal_warping`）；支持过渡配合 (Normal)、间隙配合 (Loose) 与过盈配合 (Press Fit) 的智能装配补偿（`apply_tolerance_compensation`），根据材料结晶收缩率自动逆向放大零件孔径与外形轮廓，打印成品免二次铰孔打磨直接严丝合缝装配。
- **机构运动学铰链约束求解与 Web 3D 动态仿真交互 (`kinematics.py`)**：自动识别装配体相对运动副关系（旋转副 `revolute` 与滑动副 `prismatic`），生成符合 OpenSCAD 标准的 `$t` 周期时间变量连续动画代码；Web 协同工作台内置 Three.js 运动学驱动引擎，支持交互式 3D 机构运动仿真、单轴滑块姿态调节与运动行程极限约束。
- **工业标准机械制图与自动尺寸标注图纸引擎 (`blueprint.py`)**：从模型规格自动投影并生成工业标准工程制图（主视图、俯视图、左视图及轴测图），全自动标注关键形位尺寸、通孔/沉孔引出线标注（Leader Callouts），附带符合 GB/ISO 制图标准的标准化标题栏（Title Block）与材料明细，直出纯矢量 SVG 与高分辨率 PNG。
- **视觉目标主体 ROI 自动裁剪与去噪 (`auto_crop_roi`)**：预处理阶段自动分析图片背景色域与非主体杂色，将输入照片自适应裁剪至目标主体有效区域 (Region of Interest)，消除桌面杂物与冗余空白背景，使多模态视觉模型聚焦核心几何形体，显著提升长宽比反求精度并压缩视觉 Token。
- **工业常识先验规范库与自动薄壁加固 (`apply_engineering_priors`)**：内置机械/电子壳体、承载支架、齿轮传动与免胶卡扣等领域的制造公差与工艺规范知识库；根据用户输入或模型语义自动注入标准制造参数（推荐壁厚、配合间隙），自动扫描并将过于单薄的危险壁厚加固至安全阈值（壳体 $\ge 1.6\,\text{mm}$，受力件 $\ge 2.5\,\text{mm}$），杜绝脆弱薄膜与废品打印。
- **服务端 JSON 结构化语法约束解码与栈式容错解析 (`response_format` & `robust_json_loads`)**：API 交互引入 `response_format={"type": "json_object"}` 强制大模型在 Token 解码阶段锁定合法 JSON 语法（不支持的服务端自动优雅降级）；内置栈式括号追踪算法 `robust_json_loads`，自动清理多余尾逗号并按反序逆向补全因网络中断或 Token 截断导致的未闭合括号，彻底根除 JSON 语法崩溃。
- **确定性语义锚点与相对几何约束求解器 (`resolve_geometric_constraints`)**：引入声明式装配相对依附语义（`attach_to` / `relative_to` / `face` / `align` / `offset`）与孔系阵列语义（`align: center` / `pattern: 4_corners` / `corner_inset`），由 Python 端包围盒求解器直接闭式推导为绝对空间坐标，彻底摆脱大模型三维浮点空间矩阵心算负担，保证孔位与装配面 100% 精确对齐。
- **本地零延迟自愈规则库与局部差量修补 (`heal_spec_locally` & `patch_spec`)**：当 OpenSCAD 编译校验失败时，系统优先触发内存级确定性规则引擎，自动修正负/零尺寸、清洗 polyhedron 越界索引面、补全退化多边形并剔除装配悬空引用（0ms 瞬间自愈且零 Token 损耗）；若需大模型介入，通过编译器日志与行号精准定位故障部件（`extract_failing_part_id`），仅对出错部件发起局部差量 Patch 修补，杜绝全局重写的幻觉与次生破坏。
- **几何转译防共面微延展保护 (Automatic Epsilon Offset)**：在进行孔洞布尔挖除与差集切削时，底层几何转译器自动引入 $\varepsilon = 0.05\,\text{mm}$ 微延展扩展，彻底杜绝 OpenSCAD CGAL 经典的共面重叠（Z-fighting）假象以及 0 厚度非流形破面缺陷。
- **全参数提升与 OpenSCAD Customizer 交互滑块绑定 (Parameter Hoisting)**：自动将零件核心特征尺寸（长宽高、直径、孔径）提升为顶部全局变量，并自动注入规范的 Customizer 滑块注释范围（`// [min:step:max]`），使生成的 `.scad` 代码具备极佳的可视化交互微调能力。
- **工业级标准 STEP 实体导出 (ISO 10303-21 AP214) 与 FreeCAD B-Rep 建模脚本 (`interop.py`)**：零重型外部依赖、纯原生 Python 算法构建标准的 STEP AP214 几何实体 (`MANIFOLD_SOLID_BREP` + `CLOSED_SHELL` + `POLY_LOOP`)，生成的 `.step` 可直接在 SolidWorks、Autodesk Inventor、Fusion 360、Siemens NX 及 FreeCAD 中作为无损实体特征打开进行二次装配与工程出图；同步输出参数化 FreeCAD Python 脚本，支持在 FreeCAD GUI 或无头 `freecadcmd` 下一键复原几何树与布尔装配。
- **机械结构力学应力集中与孔壁撕裂深度体检 (`doctor.py`)**：3D 打印医生系统新增结构力学分析模块 `analyze_structural_stress`，自动计算通孔与外轮廓的最小绝对边距及边径比（预警危险孔壁撕裂并指导外扩/加强筋补偿）；深度辨识零件内凹 90° 直角与拐角阶梯处的应力集中系数（缺口效应预警 $K_t \approx 2.8$ 并推荐过渡圆角 `fillet` 半径）；动态识别细长截面刚度突变，大幅提升工程承载件机械抗疲劳寿命与 3D 打印良品率。
- **增量 mtime 渲染缓存与工程四视图多线程并发加速**：基于文件时间戳精准判断，已渲染模型与视图在未发生几何改动时实现 0ms 瞬间命中复用；利用 Python `ThreadPoolExecutor` 绕过 GIL 并发调用系统级 OpenSCAD 进程，多视角与工业蓝图渲染提速 60%+，并自动探测注入现代 OpenSCAD 扩展参数 (`--enable=manifold`, `fast-csg`, `lazy-union`)。
- **ISO 标准公制内外螺纹生成器 (`thread`)**：采用数学闭式解析与无缝流形拓扑算法，直接生成 100% 封闭无自交的螺纹单 `polyhedron` 实体；支持外螺纹螺栓、内螺纹六角螺母套筒、标准螺距自动查表、两端 45° 机械导入倒角与 FDM 3D 打印热胀冷缩间隙补偿。
- **工业级渐开线齿轮构建器 (`gear`)**：原生支持模数 (`module`)、齿数 (`teeth`)、压力角 (`pressure_angle`)，基于 2D 渐开线极坐标方程生成精准齿廓，自带中心传动轴孔与标准键槽/D轴防滑切口。
- **OpenSCAD 模块生态包导出 (`export_scad_library`)**：一键将逆向出的零件封装为带命名空间隔离的标准化 `.scad` 库函数（`snapscad_<model>_<part>`），自动暴露尺寸、缩放比与间隙入参，并配套生成 `example.scad`、`README.md` 与生态归档 ZIP 包，外部工程直接 `use <model_lib.scad>;` 即可开箱调用。
- **云原生 REST API 微服务与制造驾驶舱协同工作台 (`workspace.html`)**：提供无状态 REST API 微服务（支持 `/api/compile` 动态重编译、`/api/slice` 在线切片、`/api/patch` 增量修剪、`/api/dispatch` 机群推送、`/api/model/{stem}/gcode` 原生走丝流传输），并支持云原生 FastAPI 容器化部署；配套现代化 3D WebGL 制造驾驶舱工作台，支持 `[ 🧊 STL 实体 ]` 与 `[ 🍰 G-code 走丝 ]` 双模渲染切换，提供 0 ~ Max Layer 切片层数滑块过滤与自动逐层走丝回放；内置双向打印机实时遥测仪表板（`/api/printer/status` 实时抓取 Klipper/OctoPrint/拓竹 喷头/热床温度曲线、任务进度百分比与剩余时间）；集成三维表面点对点测距仪（`Raycaster 3D Measurement`），实时呈现直线跨度与各轴分量。
- **参数化增量修剪引擎与红线热重载 (`patch_spec` & `apply_patch_to_model`)**：突破传统从头重新识别的性能瓶颈，支持免全量逆向的精准局部参数迭代。支持结构化字典修剪与常见自然语言红线指令（如 `"高度增加 10mm"`、`"孔径扩大 1mm"`、`"倒角 2mm"`、`"等比缩放 1.2"`），毫秒级修剪 CSG 部件树并触发 OpenSCAD 实体热重载；桌面 GUI 与 Web 协同工作台均已深度集成。
- **无头切片引擎与工业级 G-code 直出 (`slicer.py`)**：双级切片引擎架构，自动探测系统安装的 OrcaSlicer / PrusaSlicer / BambuStudio 并直连调用；内置零重型依赖、极速确定性的 `BuiltinGcodeGenerator`，自动解析 STL 实体拓扑与流形体积，输出包含首层引线、交叉网格填充、周长壁层、精细回抽与热床/喷嘴温控的标准 RepRap/Klipper G-code，并反向解析打印耗时、耗材线长与克重。
- **全协议 3D 打印机机群网络调度器 (`dispatcher.py`)**：统一支持 Klipper (Moonraker) REST API、OctoPrint REST API 与拓竹 (Bambu Lab X1/P1/A1) 局域网 FTPS (TLS 990/21) 安全上传与 LAN MQTT 打印开工协议，一键从设计直推机床并附带完备排错诊断指引。
- **并发批量逆向产线与展厅切片看板**：CLI `--batch-dir` 升级支持 `--batch-workers` 多线程并发生产调度，配合 `--slice` 自动直出 G-code，展厅画廊自动展示各模型真实切片耗时、耗材克重与 G-code 直链下载。
- **标准 GT2 传动同步轮 (`timing_pulley`)**：内置齿数与节距参数化，原生生成精准 GT2 圆弧齿槽、双侧防脱带法兰挡边与步进电机中心轴孔，开箱即打即装。
- **免胶快速装配结构 (`snap_joint` & `magnet_pocket`)**：
  - 悬臂梁卡扣 (`snap_joint`)：一键生成带导入斜角与弹性预留行程的免螺丝卡扣勾角；
  - 永磁体沉孔 (`magnet_pocket`)：支持 4/6/8/10mm 标准强磁片压配沉孔，自带微公差与背面贯通式顶针退磁孔，可作为实体基座或孔洞挖除特征。
- **物理基准参照物标尺自动校准 (Metric Visual Calibration)**：内置硬币 (1元/5角/1角/US Quarter)、身份证/银行卡、A4纸、标尺数据库，画面中含参照物时视觉 Agent 自动识别并推算真实毫米比例因子，实现绝对尺度免测逆向。
- **结构化中间表示 (IR)**：LLM 不直接写 OpenSCAD 代码，而是输出受 schema
  约束的 JSON 规格，再由确定性转译器生成源码，大幅降低语法错误率。
- **自动修复回路**：OpenSCAD 编译错误被回灌给 LLM，实现 self-healing 闭环。
- **渲染图审查闭环**：渲染出的预览图与原图回传 LLM 比对，不符合预期则
  自动修正规格并重渲染，直到符合或达上限；含回归保护（拒绝减少部件数的
  “简化型”修正）与得分回退（未收敛时保留迄今最好的一版）。
- **精确 3D Euler 旋转包围盒与 Z=0 自动接地**：基于 $R_z R_y R_x$ 闭式解析与顶点映射矩阵精确计算倾斜多轴旋转部件包围盒，彻底杜绝倾斜零件悬空或漂移，确保模型完美平贴打印底板，开箱即切片。
- **多材质工业级体检与切片指导书**：内置 PLA、PETG、ABS、TPU、PC 材质物性数据库，依据材料密度动态测算克重与成本，针对高收缩材质 (ABS/PC) 自动提示封闭保温箱与热缩孔径补偿，针对软胶 (TPU) 提供降速防拉丝指导。
- **便携式 HTML 3D 查看器**：导出 STL 同步生成内嵌 Three.js 查看器，双击即可在任何浏览器 360° 旋转检视，支持实时截面剖切 (Clip Plane)、3D 尺寸标定盒、红绿蓝空间坐标轴 (AxesHelper)、影棚明暗主题切换与自定义角度一键高清截屏。
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
- **自动化质量基准测试套件**：内置 `test_agent.py`，支持 `main.py --test` 一键全自动化执行 25 大核心工程质量基准自检套件 (100% 通过)。
- **GUI 国际化双语切换**：界面支持简体中文 / English 实时无缝热切，自动持久化语言偏好。
- **GUI 原生内嵌 3D 实时交互视口**：PyQt6 桌面客户端在 2D 静态工程图与 3D 交互视口间无缝热切（`🎮 3D 交互视口`），无需切换外部浏览器即可直接在主窗口执行 360° 旋转、缩放平移、剖切与测量，同时具备 WebEngine 环境探测与优雅回退。
- **轻量有限元 (FEA) 热力学形变估算与 3D 装配公差补偿**：基于热膨胀梯度与结晶收缩率，自动估算 FDM 3D 打印底面边缘翘曲抬升量（预测 corner lift in mm）并划分翘曲风险等级（LOW/MEDIUM/HIGH）；智能孔洞热缩负补偿与装配配合等级（过渡/间隙/过盈），自动将孔洞根据切片喷丝张力与材料热缩进行精准反向预补偿（如孔径自动 $+0.2\sim 0.35\text{mm}$），杜绝装配扩孔打磨痛点，一键更新并重载 STL/SCAD/3MF 实体模型。
- **机械机构运动学铰链约束与 Web 3D 动态仿真**：支持机械机构运动学拓扑解析（旋转副 Revolute/Hinge、移动副滑块 Prismatic/Slider 与齿轮副传动 Gear Coupling），自动转译为 OpenSCAD 原生动画变量（`anim_t = $t;`），在 OpenSCAD 勾选 `View -> Animate` 即可免插件物理连续运动；在线协同 3D 工作台 (`workspace.html`) 内置 60 FPS 物理机构运动学动态仿真引擎，提供播放/暂停、速度倍率调节、时间轴进度拖拽与多自由度关节滑块微调。
- **工业级工程制图自动尺寸标注与矢量图纸引擎**：内置纯矢量 SVG 机械工程图纸引擎，自动解算正交三视图（主视/俯视/侧视）与轴测图（符合“长对正、高平齐、宽相等”投影规范），自动绘制符合 ISO/GB 制图标准的尺寸界线、尺寸线、双向机械箭头、中心点划线、通孔引出标注（如 `⌀6 THRU`）与标准工程标题栏（Title Block，包含第三角投影法图符、零件名、材料、比例 1:1 与图号），零第三方重型依赖直接输出纯矢量 `*_blueprint.svg`。
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

# 7) 逆向建模并同步导出 STEP 工业标准实体与 FreeCAD 脚本
.venv/bin/python main.py -i photo.jpg --export-step

# 8) 启动交互式终端建模向导
snapscad --wizard

# 9) 启动局域网 3D 资产共享服务 (供手机/平板无线浏览)
snapscad --serve 8080

# 10) 执行全套自动化质量基准与回归测试
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
