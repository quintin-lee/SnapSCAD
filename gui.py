"""PyQt6 图形界面：图片/文字 → OpenSCAD 3D 模型 全流程可视化。

设计：
- 左侧：输入区（图片选择 / 文字描述 / 补充说明 / 参数设置）
- 中间：流程步骤与 LLM 识别的模型规格（JSON 树形展示）
- 右侧：3D 预览（渲染 PNG 展示）+ 3D 模型文件列表
- 底部：日志控制台（实时输出 agent 运行日志）

所有耗时操作（API 调用、OpenSCAD 渲染）通过 QThread 后台执行，
UI 不卡顿；结果通过 pyqtSignal 回传主线程更新。
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, Qt, pyqtSignal, QSettings, QUrl, QRegularExpression
from PyQt6.QtGui import QPixmap, QFont, QColor, QDesktopServices, QKeySequence, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QTextEdit, QComboBox, QGroupBox,
    QFileDialog, QSplitter, QScrollArea, QTreeWidget, QTreeWidgetItem,
    QPlainTextEdit, QMessageBox, QProgressBar, QFrame, QSizePolicy,
    QListWidget, QListWidgetItem, QSpinBox, QDoubleSpinBox, QTabWidget,
    QInputDialog, QStackedWidget,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except ImportError:
    QWebEngineView = None
    HAS_WEBENGINE = False

from openscad_agent import AgentConfig, OpenscadAgent, OpenscadRenderer
from openscad_agent.vision import AgnesVision, SpecParseError


# ---------------------------------------------------------------------------
# OpenSCAD 语法高亮器
# ---------------------------------------------------------------------------

class OpenScadHighlighter(QSyntaxHighlighter):
    """OpenSCAD 语法高亮器（模块、函数、变换、原语、注释、数值、字符串）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rules: list[tuple[QRegularExpression, QTextCharFormat]] = []

        # 1. 结构与流程关键字
        kw_fmt = QTextCharFormat()
        kw_fmt.setForeground(QColor("#818cf8"))  # 亮紫蓝
        kw_fmt.setFontWeight(QFont.Weight.Bold)
        keywords = [
            "module", "function", "union", "difference", "intersection",
            "if", "else", "for", "let", "echo", "render", "use", "include",
        ]
        for kw in keywords:
            self.rules.append((QRegularExpression(rf"\b{kw}\b"), kw_fmt))

        # 2. 变换与挤出函数
        trans_fmt = QTextCharFormat()
        trans_fmt.setForeground(QColor("#38bdf8"))  # 天蓝
        trans_fmt.setFontWeight(QFont.Weight.DemiBold)
        transforms = [
            "translate", "rotate", "scale", "mirror", "color", "offset", "hull",
            "minkowski", "linear_extrude", "rotate_extrude", "projection",
        ]
        for tf in transforms:
            self.rules.append((QRegularExpression(rf"\b{tf}\b"), trans_fmt))

        # 3. 几何原语
        prim_fmt = QTextCharFormat()
        prim_fmt.setForeground(QColor("#34d399"))  # 薄荷绿
        prim_fmt.setFontWeight(QFont.Weight.Bold)
        primitives = [
            "cube", "cylinder", "sphere", "circle", "square", "polygon", "polyhedron",
        ]
        for pr in primitives:
            self.rules.append((QRegularExpression(rf"\b{pr}\b"), prim_fmt))

        # 4. 常量与内置变量
        const_fmt = QTextCharFormat()
        const_fmt.setForeground(QColor("#fbbf24"))  # 琥珀黄
        for c in (r"\$fn\b", r"\$fa\b", r"\$fs\b", r"\$t\b", r"\btrue\b", r"\bfalse\b", r"\bPI\b"):
            self.rules.append((QRegularExpression(c), const_fmt))

        # 5. 数值
        num_fmt = QTextCharFormat()
        num_fmt.setForeground(QColor("#f472b6"))  # 亮粉色
        self.rules.append((QRegularExpression(r"\b[-+]?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?\b"), num_fmt))

        # 6. 字符串
        str_fmt = QTextCharFormat()
        str_fmt.setForeground(QColor("#a3e635"))  # 柠檬绿
        self.rules.append((QRegularExpression(r'"[^"\\]*(\\.[^"\\]*)*"'), str_fmt))

        # 7. 单行注释
        comment_fmt = QTextCharFormat()
        comment_fmt.setForeground(QColor("#64748b"))  # 蓝灰注释
        comment_fmt.setFontItalic(True)
        self.rules.append((QRegularExpression(r"//[^\n]*"), comment_fmt))

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        for pattern, fmt in self.rules:
            match_iter = pattern.globalMatch(text)
            while match_iter.hasNext():
                match = match_iter.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


# ---------------------------------------------------------------------------
# 后台工作线程
# ---------------------------------------------------------------------------

class BuildWorker(QThread):
    """后台执行 OpenscadAgent 全流程。"""

    log_signal = pyqtSignal(str)
    done_signal = pyqtSignal(object)   # AgentResult
    error_signal = pyqtSignal(str)

    def __init__(self, agent: OpenscadAgent, image: Optional[str | list[str]],
                 description: str, hint: str, review: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.agent = agent  # agent 已在主线程构造完成且参数已写入，worker 只读
        self.image = image
        self.description = description
        self.hint = hint
        self.review = review
        self._cancelled = False

    def cancel(self) -> None:
        """协作式取消：在建模子调用边界检查，不中断当前 openscad 子进程。"""
        self._cancelled = True

    def run(self) -> None:
        try:
            if self._cancelled:
                self.log_signal.emit("任务已取消")
                return
            img_desc = (", ".join(Path(p).name for p in self.image) if isinstance(self.image, (list, tuple))
                        else (Path(self.image).name if self.image else "-"))
            self.log_signal.emit(f"开始建模: image={img_desc} desc={self.description or '-'} review={self.review}")
            if self.image:
                result = self.agent.build_from_image(self.image, hint=self.hint, review=self.review)
            else:
                result = self.agent.build_from_description(self.description)
            if self._cancelled:
                self.log_signal.emit("任务已取消")
                return
            self.log_signal.emit(
                f"建模完成: success={result.success} fix_rounds={result.fix_rounds} review_rounds={getattr(result,'review_rounds',0)}"
            )
            self.done_signal.emit(result)
        except Exception as e:  # noqa: BLE001
            self.log_signal.emit(f"异常: {e}")
            self.error_signal.emit(str(e))


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    SETTINGS_ORG = "SnapSCAD"
    SETTINGS_APP = "SnapSCAD"

    I18N = {
        "zh": {
            "title": "SnapSCAD - 照片→参数化 3D CAD 模型 Agent",
            "btn_build": "▶ 开始建模",
            "btn_cancel": "⏹ 终止",
            "idle": "空闲",
            "box_input": "输入源",
            "btn_pick_img": "📁 选择图片 (多选)",
            "btn_clip": "📋 剪贴板粘贴截图",
            "label_preset": "预设范例:",
            "label_desc": "文字描述:",
            "btn_enhance": "🪄 追加工程约束",
            "box_params": "参数与模型提供商",
            "view_persp": "透视 (主)",
            "view_top": "俯视",
            "view_front": "正视",
            "view_iso": "等轴测",
            "view_explode": "💥 爆炸图",
            "view_blueprint": "📐 蓝图",
            "view_3d": "🎮 3D 交互视口",
            "btn_open_scad": "🚀 在 OpenSCAD 中打开 3D 模型",
            "btn_web_viewer": "🌐 网页 3D 查看器",
            "btn_workspace": "🌐 协同工作台",
            "btn_gallery": "🏛️ 3D 展厅画廊",
            "btn_dxf": "📐 激光切割 DXF",
            "btn_slice": "🍰 切片 G-code",
            "btn_patch": "✍️ 增量修剪",
            "btn_step": "📐 导出 STEP",
            "btn_export_lib": "📦 导出生态库",
            "btn_zip": "📦 打包工程 ZIP",
            "btn_printer": "🖨️ 发送至打印机",
            "btn_serve": "📡 局域网分享",
            "btn_open_dir": "📁 打开输出目录",
            "tab_projects": "📚 历史工程库 (单击全景回溯)",
            "tab_files": "📁 全部输出文件",
            "btn_rerender": "⚡ 重新渲染源码",
            "btn_snapshot": "📸 保存快照",
            "btn_rollback": "⏪ 回滚快照",
        },
        "en": {
            "title": "OpenSCAD Agent - Image to 3D CAD",
            "btn_build": "▶ Start Build",
            "btn_cancel": "⏹ Abort",
            "idle": "Idle",
            "box_input": "Input Source",
            "btn_pick_img": "📁 Pick Images (Multi)",
            "btn_clip": "📋 Paste Screenshot",
            "label_preset": "CAD Presets:",
            "label_desc": "Description:",
            "btn_enhance": "🪄 Add Engineering Specs",
            "box_params": "Settings & LLM Provider",
            "view_persp": "Perspective",
            "view_top": "Top",
            "view_front": "Front",
            "view_iso": "Isometric",
            "view_explode": "💥 Explode",
            "view_blueprint": "📐 Blueprint",
            "view_3d": "🎮 3D Viewport",
            "btn_open_scad": "🚀 Open in OpenSCAD",
            "btn_web_viewer": "🌐 Web 3D Viewer",
            "btn_workspace": "🌐 Web Workspace",
            "btn_gallery": "🏛️ 3D Showcase Gallery",
            "btn_dxf": "📐 Laser Cut DXF",
            "btn_slice": "🍰 Slice G-code",
            "btn_patch": "✍️ Patch Spec",
            "btn_step": "📐 Export STEP",
            "btn_export_lib": "📦 Export Library",
            "btn_zip": "📦 Export Project ZIP",
            "btn_printer": "🖨️ Send to 3D Printer",
            "btn_serve": "📡 LAN Share",
            "btn_open_dir": "📁 Open Output Folder",
            "tab_projects": "📚 Projects Catalog",
            "tab_files": "📁 Output Files",
            "btn_rerender": "⚡ Re-render SCAD",
            "btn_snapshot": "📸 Snapshot",
            "btn_rollback": "⏪ Rollback",
        }
    }

    PRESETS = {
        "机械: 带沉孔圆形法兰 (Flange Mount)": (
            "一个工业级圆形法兰安装座，包含外径60mm厚度8mm的底盘，4个均布的M4沉孔，中央有一个直径25mm高20mm的凸台，中央贯穿12mm的轴孔。",
            "底盘直径60mm，4个沉孔分布在R=22mm的圆周上，同心度要求高"
        ),
        "机械: 阶梯双联齿轮 (Step Gear)": (
            "一个阶梯式双联圆柱齿轮，大齿轮外径50mm厚度10mm，小齿轮外径30mm厚度15mm，中央带有直径8mm贯穿轴孔。",
            "大齿轮与小齿轮同轴相连，轴孔贯穿整体"
        ),
        "数码: 树莓派保护壳 (Enclosure Case)": (
            "一个电子开发板外壳下盖，外尺寸90x65x25mm，壁厚2mm，四个角带有M2.5安装柱，侧面有USB接口开口与电源线槽开口。",
            "四角安装孔距为58x49mm，壁厚均匀，内腔倒角"
        ),
        "文创: 极简咖啡杯 (Coffee Mug)": (
            "一个现代极简主义流线型马克杯，杯身呈上宽下窄的微锥形（底部直径70mm，口部直径85mm，高度100mm），壁厚3mm，侧面带有一个符合人体工学的手柄。",
            "内腔掏空厚度3mm，手柄使用胶囊体平滑连接在杯身外侧"
        ),
        "生活: 桌面斜角手机支架 (Phone Stand)": (
            "一个倾斜60度的桌面手机支架，底座尺寸80x70x6mm，倾斜支撑背板高100mm，前部带有高15mm的防滑托钩，背板中央开有用于充电线的理线槽。",
            "整体一体成型，底部平整贴合桌面，重心稳固"
        ),
        "建筑: 东方六角古亭 (Hexagonal Pavilion)": (
            "一个传统中式六角亭子，由六边形底座平台、六根立柱构成的开敞空间，以及顶部的六角攒尖式飞檐斜坡屋顶组合而成。",
            "各立柱对称分布在半径35mm的圆周上，屋顶高25mm呈六角锥形"
        ),
    }

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SnapSCAD - 照片→参数化 3D CAD 模型 Agent")
        self.resize(1200, 800)
        self._worker: Optional[BuildWorker] = None
        self._scad_path: Optional[str] = None
        self._last_image: Optional[str] = None
        self._last_desc: str = ""
        self._current_preview_png: Optional[str] = None
        self._current_viewer_html: Optional[str] = None
        self._settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        self.setAcceptDrops(True)
        self._build_ui()
        self._load_settings()
        self._connect_settings_signals()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        # 顶部工具栏
        root.addWidget(self._build_toolbar())

        # 中间三栏
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：输入区（图片选择 / 文字描述 / 补充说明 / 参数设置）
        left = self._build_input_panel()
        self.main_splitter.addWidget(left)

        # 中：规格 + 流程
        mid = self._build_spec_panel()
        self.main_splitter.addWidget(mid)

        # 右：预览
        right = self._build_preview_panel()
        self.main_splitter.addWidget(right)

        self.main_splitter.setSizes([320, 480, 420])
        root.addWidget(self.main_splitter, stretch=1)

        # 底部日志
        root.addWidget(self._build_log_panel())

        # 状态栏
        self.statusBar().showMessage("就绪")

    def _build_toolbar(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 4)

        self.btn_build = QPushButton("▶ 开始建模")
        self.btn_build.setMinimumHeight(40)
        self.btn_build.setStyleSheet(
            "QPushButton { background-color: #2563eb; color: white; font-weight: bold; "
            "font-size: 14px; border-radius: 6px; padding: 0 16px; }"
            "QPushButton:hover { background-color: #1d4ed8; }"
            "QPushButton:disabled { background-color: #cbd5e1; }"
        )
        self.btn_build.clicked.connect(self.on_build)
        lay.addWidget(self.btn_build)

        self.btn_cancel = QPushButton("⏹ 终止")
        self.btn_cancel.setMinimumHeight(40)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setStyleSheet(
            "QPushButton { background-color: #ef4444; color: white; font-weight: bold; "
            "font-size: 14px; border-radius: 6px; padding: 0 14px; }"
            "QPushButton:hover { background-color: #dc2626; }"
            "QPushButton:disabled { background-color: #e2e8f0; color: #94a3b8; }"
        )
        self.btn_cancel.clicked.connect(self.on_cancel_build)
        lay.addWidget(self.btn_cancel)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(10)
        self.progress.setMaximum(100)
        lay.addWidget(self.progress, stretch=1)

        self.lbl_status = QLabel("空闲")
        lay.addWidget(self.lbl_status)

        lay.addWidget(QLabel("🌐"))
        self.cmb_lang = QComboBox()
        self.cmb_lang.addItems(["简体中文", "English"])
        self.cmb_lang.currentIndexChanged.connect(self.on_language_changed)
        lay.addWidget(self.cmb_lang)
        return w

    def _build_input_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)

        # 输入模式切换
        self.mode_box = QGroupBox("输入源")
        mode_lay = QVBoxLayout(self.mode_box)
        self.image_path = QLineEdit()
        self.image_path.setPlaceholderText("选择或拖入图片（支持多选）…")
        row_img_btns = QHBoxLayout()
        self.btn_img = QPushButton("📁 选择图片 (多选)")
        self.btn_img.clicked.connect(self.on_pick_image)
        self.btn_clip = QPushButton("📋 剪贴板粘贴截图")
        self.btn_clip.clicked.connect(self.on_paste_clipboard)
        row_img_btns.addWidget(self.btn_img)
        row_img_btns.addWidget(self.btn_clip)
        mode_lay.addWidget(self.image_path)
        mode_lay.addLayout(row_img_btns)

        row_preset = QHBoxLayout()
        self.lbl_preset = QLabel("预设范例:")
        row_preset.addWidget(self.lbl_preset)
        self.cmb_preset = QComboBox()
        self.cmb_preset.addItems(["选择预设范例…"] + list(self.PRESETS.keys()))
        self.cmb_preset.currentIndexChanged.connect(self.on_preset_selected)
        row_preset.addWidget(self.cmb_preset, stretch=1)
        mode_lay.addLayout(row_preset)

        row_desc_hdr = QHBoxLayout()
        self.lbl_desc = QLabel("文字描述:")
        row_desc_hdr.addWidget(self.lbl_desc)
        row_desc_hdr.addStretch(1)
        self.btn_enhance = QPushButton("🪄 追加工程约束")
        self.btn_enhance.setStyleSheet("font-size: 11px; padding: 2px 8px; color: #2563eb;")
        self.btn_enhance.clicked.connect(self.on_enhance_prompt)
        row_desc_hdr.addWidget(self.btn_enhance)
        mode_lay.addLayout(row_desc_hdr)

        self.txt_desc = QTextEdit()
        self.txt_desc.setPlaceholderText("或在此输入文字描述 / 从上方选择预设范例…")
        self.txt_desc.setFixedHeight(80)
        mode_lay.addWidget(self.txt_desc)

        self.txt_hint = QLineEdit()
        self.txt_hint.setPlaceholderText("补充说明（可选，帮助理解物体）")
        mode_lay.addWidget(self.txt_hint)
        lay.addWidget(self.mode_box)

        # 参数
        self.param_box = QGroupBox("参数与模型提供商")
        param_lay = QVBoxLayout(self.param_box)

        # Provider 预设切换
        row_prov = QHBoxLayout()
        row_prov.addWidget(QLabel("服务商"))
        self.cmb_provider = QComboBox()
        self.cmb_provider.addItems([
            "Agnes AI (默认云端)",
            "OpenAI (官方接口)",
            "DeepSeek (官方接口)",
            "Google Gemini (官方兼容接口)",
            "SiliconFlow 硅基流动",
            "Alibaba DashScope (通义千问)",
            "Groq (极速推理)",
            "本地 Ollama (完全离线免Key)",
            "自定义端点",
        ])
        self.cmb_provider.currentIndexChanged.connect(self.on_provider_changed)
        row_prov.addWidget(self.cmb_provider, stretch=1)
        param_lay.addLayout(row_prov)

        # 模型 & API
        row_model = QHBoxLayout()
        row_model.addWidget(QLabel("模型"))
        self.txt_model = QLineEdit("agnes-3.0-flash")
        row_model.addWidget(self.txt_model, stretch=1)
        param_lay.addLayout(row_model)

        row_url = QHBoxLayout()
        row_url.addWidget(QLabel("Base URL"))
        self.txt_base_url = QLineEdit("https://apihub.agnes-ai.com")
        row_url.addWidget(self.txt_base_url, stretch=1)
        param_lay.addLayout(row_url)

        row_temp = QHBoxLayout()
        row_temp.addWidget(QLabel("温度"))
        self.spn_temperature = QDoubleSpinBox()
        self.spn_temperature.setRange(0.0, 2.0)
        self.spn_temperature.setSingleStep(0.1)
        self.spn_temperature.setValue(0.2)
        row_temp.addWidget(self.spn_temperature, stretch=1)
        param_lay.addLayout(row_temp)

        row_fmt = QHBoxLayout()
        row_fmt.addWidget(QLabel("导出格式"))
        self.cmb_format = QComboBox()
        self.cmb_format.addItems(["stl", "obj", "3mf", "csg", "amf"])
        row_fmt.addWidget(self.cmb_format, stretch=1)
        param_lay.addLayout(row_fmt)

        row_prec = QHBoxLayout()
        row_prec.addWidget(QLabel("渲染精度"))
        self.cmb_precision = QComboBox()
        self.cmb_precision.addItems(["Fast", "Slow", "High"])
        self.cmb_precision.setCurrentText("High")
        row_prec.addWidget(self.cmb_precision, stretch=1)
        param_lay.addLayout(row_prec)

        row_out = QHBoxLayout()
        row_out.addWidget(QLabel("输出目录"))
        self.txt_outdir = QLineEdit("out")
        row_out.addWidget(self.txt_outdir, stretch=1)
        param_lay.addLayout(row_out)

        # 图片预处理
        row_img = QHBoxLayout()
        row_img.addWidget(QLabel("图片最大边"))
        self.spn_max_side = QSpinBox()
        self.spn_max_side.setRange(256, 8192)
        self.spn_max_side.setSingleStep(64)
        self.spn_max_side.setValue(2048)
        row_img.addWidget(self.spn_max_side, stretch=1)
        param_lay.addLayout(row_img)

        # 审查闭环
        row_fix = QHBoxLayout()
        row_fix.addWidget(QLabel("编译修复轮数"))
        self.spn_fix_rounds = QSpinBox()
        self.spn_fix_rounds.setRange(0, 10)
        self.spn_fix_rounds.setValue(3)
        row_fix.addWidget(self.spn_fix_rounds, stretch=1)
        param_lay.addLayout(row_fix)

        row_review = QHBoxLayout()
        row_review.addWidget(QLabel("审查轮数"))
        self.spn_review_rounds = QSpinBox()
        self.spn_review_rounds.setRange(0, 10)
        self.spn_review_rounds.setValue(3)
        row_review.addWidget(self.spn_review_rounds, stretch=1)
        param_lay.addLayout(row_review)

        self.chk_render = QPushButton("渲染 3D 模型")
        self.chk_render.setCheckable(True)
        self.chk_render.setChecked(True)
        param_lay.addWidget(self.chk_render)

        self.chk_review = QPushButton("渲染图审查闭环 (自动修正直到符合预期)")
        self.chk_review.setCheckable(True)
        self.chk_review.setChecked(True)
        param_lay.addWidget(self.chk_review)

        # Canny 几何边缘轮廓并联辅助 (Dual-Vision)
        self.chk_edge_map = QPushButton("并联 Canny 物理边缘轮廓图 (消除光影反光干扰)")
        self.chk_edge_map.setCheckable(True)
        self.chk_edge_map.setChecked(True)
        param_lay.addWidget(self.chk_edge_map)

        # 3D 切片文件与 2D 激光投影同步导出
        self.chk_3mf = QPushButton("同步导出 3MF 切片文件 (Cura/Prusa 一键联动)")
        self.chk_3mf.setCheckable(True)
        self.chk_3mf.setChecked(True)
        param_lay.addWidget(self.chk_3mf)

        self.chk_2d = QPushButton("同步导出 2D 激光切割投影 (DXF+SVG)")
        self.chk_2d.setCheckable(True)
        self.chk_2d.setChecked(False)
        param_lay.addWidget(self.chk_2d)

        lay.addWidget(self.param_box)

        lay.addStretch(1)
        return w

    def _build_spec_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)

        lay.addWidget(QLabel("<b>模型规格 (JSON)</b>", wordWrap=True))
        tree = QTreeWidget()
        tree.setHeaderLabels(["字段", "值"])
        tree.setColumnWidth(0, 160)
        self.spec_tree = tree
        self.spec_tree.itemClicked.connect(self.on_spec_tree_clicked)
        lay.addWidget(tree, stretch=1)

        scad_bar = QHBoxLayout()
        scad_bar.addWidget(QLabel("<b>OpenSCAD 源码 (支持在线微调)</b>"))
        scad_bar.addStretch(1)

        self.btn_snapshot = QPushButton("📸 保存快照")
        self.btn_snapshot.setStyleSheet("padding: 3px 8px; font-size: 11px;")
        self.btn_snapshot.clicked.connect(self.on_save_snapshot)
        scad_bar.addWidget(self.btn_snapshot)

        self.btn_rollback = QPushButton("⏪ 回滚快照")
        self.btn_rollback.setStyleSheet("padding: 3px 8px; font-size: 11px;")
        self.btn_rollback.clicked.connect(self.on_rollback_snapshot)
        scad_bar.addWidget(self.btn_rollback)

        self.btn_rerender = QPushButton("⚡ 重新渲染源码")
        self.btn_rerender.setStyleSheet(
            "QPushButton { background-color: #059669; color: white; font-weight: bold; padding: 3px 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #047857; }"
        )
        self.btn_rerender.clicked.connect(self.on_rerender_scad)
        scad_bar.addWidget(self.btn_rerender)
        lay.addLayout(scad_bar)

        self.txt_scad = QPlainTextEdit()
        self.txt_scad.setReadOnly(False)
        self.txt_scad.setPlaceholderText("// 在此处直接编辑 OpenSCAD 源码，点击右上角「重新渲染源码」即刻生效")
        self.txt_scad.setStyleSheet(
            "QPlainTextEdit { background-color: #0f172a; color: #f8fafc; font-family: 'Monospace', monospace; "
            "font-size: 13px; line-height: 1.4; border-radius: 6px; border: 1px solid rgba(255,255,255,0.1); padding: 4px; }"
        )
        self.txt_scad.setFixedHeight(220)
        self.txt_scad.textChanged.connect(lambda: self.txt_scad.setExtraSelections([]))
        self._scad_highlighter = OpenScadHighlighter(self.txt_scad.document())
        lay.addWidget(self.txt_scad)
        return w

    def _build_preview_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)

        lay.addWidget(QLabel("<b>原图 vs 预览对比</b>", wordWrap=True))

        # 视角切换按钮组
        view_bar = QHBoxLayout()
        view_bar.addWidget(QLabel("视角:"))
        self.btn_view_persp = QPushButton("透视 (主)")
        self.btn_view_top = QPushButton("俯视")
        self.btn_view_front = QPushButton("正视")
        self.btn_view_iso = QPushButton("等轴测")
        self.btn_view_explode = QPushButton("💥 爆炸图")
        self.btn_view_blueprint = QPushButton("📐 蓝图")
        self.btn_view_3d = QPushButton("🎮 3D 交互视口")
        self.btn_view_3d.setStyleSheet("font-weight: bold;")
        for btn in (
            self.btn_view_persp, self.btn_view_top, self.btn_view_front,
            self.btn_view_iso, self.btn_view_explode, self.btn_view_blueprint,
            self.btn_view_3d,
        ):
            btn.setFixedHeight(26)
            view_bar.addWidget(btn)
        view_bar.addStretch(1)
        lay.addLayout(view_bar)

        self.btn_view_persp.clicked.connect(lambda: self.on_switch_view("perspective"))
        self.btn_view_top.clicked.connect(lambda: self.on_switch_view("top"))
        self.btn_view_front.clicked.connect(lambda: self.on_switch_view("front"))
        self.btn_view_iso.clicked.connect(lambda: self.on_switch_view("iso"))
        self.btn_view_explode.clicked.connect(lambda: self.on_switch_view("explode"))
        self.btn_view_blueprint.clicked.connect(lambda: self.on_switch_view("blueprint"))
        self.btn_view_3d.clicked.connect(self.on_switch_to_3d_viewport)

        # 原图 | 预览图 并排
        cmp_split = QSplitter(Qt.Orientation.Horizontal)

        self.lbl_origin = QLabel("（原图）")
        self.lbl_origin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_origin.setMinimumSize(200, 260)
        self.lbl_origin.setStyleSheet("QLabel { border: 2px dashed #cbd5e1; border-radius: 8px; }")
        self.lbl_origin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        cmp_split.addWidget(self.lbl_origin)

        self.lbl_preview = QLabel("（预览）")
        self.lbl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_preview.setMinimumSize(200, 260)
        self.lbl_preview.setStyleSheet("QLabel { border: 2px dashed #cbd5e1; border-radius: 8px; }")
        self.lbl_preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # 内嵌 3D 交互视口面板
        self.widget_3d_viewport = QWidget()
        lay_3d = QVBoxLayout(self.widget_3d_viewport)
        lay_3d.setContentsMargins(0, 0, 0, 0)
        lay_3d.setSpacing(4)

        bar_3d = QHBoxLayout()
        self.lbl_3d_title = QLabel("<b>🎮 3D 实时交互视口</b>")
        bar_3d.addWidget(self.lbl_3d_title)
        bar_3d.addStretch(1)

        self.btn_reload_3d = QPushButton("🔄 刷新")
        self.btn_reload_3d.setToolTip("重新载入当前 3D 场景")
        self.btn_reload_3d.setFixedHeight(24)
        self.btn_reload_3d.clicked.connect(self._reload_3d_viewer)
        bar_3d.addWidget(self.btn_reload_3d)

        self.btn_pop_browser = QPushButton("🌐 外部浏览器打开")
        self.btn_pop_browser.setToolTip("在系统默认浏览器中独立打开 3D 查看器")
        self.btn_pop_browser.setFixedHeight(24)
        self.btn_pop_browser.clicked.connect(self.on_open_web_viewer)
        bar_3d.addWidget(self.btn_pop_browser)
        lay_3d.addLayout(bar_3d)

        if HAS_WEBENGINE and QWebEngineView is not None:
            self.web_view_3d = QWebEngineView()
            self.web_view_3d.setStyleSheet("border: 2px solid #2563eb; border-radius: 8px;")
            self.web_view_3d.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            lay_3d.addWidget(self.web_view_3d)
            self.widget_fallback_3d = None
        else:
            self.web_view_3d = None
            self.widget_fallback_3d = QFrame()
            self.widget_fallback_3d.setStyleSheet(
                "QFrame { background-color: #0f172a; border: 2px dashed #334155; border-radius: 8px; color: #f8fafc; padding: 20px; }"
            )
            fb_lay = QVBoxLayout(self.widget_fallback_3d)
            fb_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fb_lay.setSpacing(12)

            icon_lbl = QLabel("<div style='font-size: 36px;'>🎮</div>")
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            fb_lay.addWidget(icon_lbl)

            info_lbl = QLabel(
                "<b style='font-size: 14px; color: #38bdf8;'>内嵌 3D 交互视口已就绪</b><br><br>"
                "<span style='color: #94a3b8; font-size: 12px; line-height: 1.6;'>"
                "当前环境可通过下方按钮一键调起外部浏览器进行 360° 旋转检视；<br>"
                "或安装 <code>pip install PyQt6-WebEngine</code> 激活原生内置视口。"
                "</span>"
            )
            info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            info_lbl.setWordWrap(True)
            fb_lay.addWidget(info_lbl)

            btn_open_fb = QPushButton("🚀 在浏览器中打开 3D 查看器 (360° 旋转 / 剖切 / 测距)")
            btn_open_fb.setFixedHeight(34)
            btn_open_fb.setStyleSheet(
                "QPushButton { background-color: #2563eb; color: white; font-weight: bold; border-radius: 6px; padding: 6px 16px; font-size: 12px; }"
                "QPushButton:hover { background-color: #1d4ed8; }"
            )
            btn_open_fb.clicked.connect(self.on_open_web_viewer)
            fb_lay.addWidget(btn_open_fb)
            lay_3d.addWidget(self.widget_fallback_3d)

        # 预览区堆叠控件：Index 0 为 2D 静态渲染，Index 1 为 3D 交互视口
        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.lbl_preview)
        self.preview_stack.addWidget(self.widget_3d_viewport)
        cmp_split.addWidget(self.preview_stack)

        cmp_split.setSizes([260, 260])
        lay.addWidget(cmp_split, stretch=2)

        # 快捷操作栏
        act_bar = QHBoxLayout()
        self.btn_open_scad = QPushButton("🚀 在 OpenSCAD 中打开 3D 模型")
        self.btn_open_scad.setStyleSheet("font-weight: bold; padding: 4px 8px;")
        self.btn_open_scad.clicked.connect(self.on_open_in_openscad)
        act_bar.addWidget(self.btn_open_scad)

        self.btn_open_web = QPushButton("🌐 网页 3D 查看器")
        self.btn_open_web.clicked.connect(self.on_open_web_viewer)
        act_bar.addWidget(self.btn_open_web)

        self.btn_workspace = QPushButton("🌐 协同工作台")
        self.btn_workspace.clicked.connect(self.on_open_workspace)
        act_bar.addWidget(self.btn_workspace)

        self.btn_gallery = QPushButton("🏛️ 3D 展厅画廊")
        self.btn_gallery.clicked.connect(self.on_open_gallery)
        act_bar.addWidget(self.btn_gallery)

        self.btn_export_dxf = QPushButton("📐 激光切割 DXF")
        self.btn_export_dxf.clicked.connect(self.on_export_dxf)
        act_bar.addWidget(self.btn_export_dxf)

        self.btn_slice = QPushButton("🍰 切片 G-code")
        self.btn_slice.clicked.connect(self.on_slice_model)
        act_bar.addWidget(self.btn_slice)

        self.btn_patch = QPushButton("✍️ 增量修剪")
        self.btn_patch.clicked.connect(self.on_patch_model)
        act_bar.addWidget(self.btn_patch)

        self.btn_export_step = QPushButton("📐 导出 STEP")
        self.btn_export_step.clicked.connect(self.on_export_step)
        act_bar.addWidget(self.btn_export_step)

        self.btn_export_blueprint = QPushButton("📐 导出工程图纸")
        self.btn_export_blueprint.clicked.connect(self.on_export_blueprint)
        act_bar.addWidget(self.btn_export_blueprint)

        self.btn_export_multi_3mf = QPushButton("🎨 导出多色 3MF")
        self.btn_export_multi_3mf.clicked.connect(self.on_export_multimaterial_3mf)
        act_bar.addWidget(self.btn_export_multi_3mf)

        self.btn_tolerance = QPushButton("🌡️ 热缩与公差补偿")
        self.btn_tolerance.clicked.connect(self.on_tolerance_compensation)
        act_bar.addWidget(self.btn_tolerance)

        self.btn_lattice = QPushButton("🪶 晶格减重")
        self.btn_lattice.clicked.connect(self.on_apply_lattice_lightweighting)
        act_bar.addWidget(self.btn_lattice)

        self.btn_export_sop = QPushButton("🛠️ 装配工艺 SOP")
        self.btn_export_sop.clicked.connect(self.on_export_assembly_sop)
        act_bar.addWidget(self.btn_export_sop)

        self.btn_clash = QPushButton("🔍 干涉体检")
        self.btn_clash.clicked.connect(self.on_check_assembly_interference)
        act_bar.addWidget(self.btn_clash)

        self.btn_fasteners = QPushButton("🔩 紧固件库")
        self.btn_fasteners.clicked.connect(self.on_open_fasteners_dialog)
        act_bar.addWidget(self.btn_fasteners)

        self.btn_piping = QPushButton("🚰 工业管路")
        self.btn_piping.clicked.connect(self.on_open_piping_dialog)
        act_bar.addWidget(self.btn_piping)

        self.btn_sheetmetal = QPushButton("📐 钣金展开")
        self.btn_sheetmetal.clicked.connect(self.on_open_sheetmetal_dialog)
        act_bar.addWidget(self.btn_sheetmetal)

        self.btn_enclosure = QPushButton("🖥️ 电子外壳")
        self.btn_enclosure.clicked.connect(self.on_open_enclosure_dialog)
        act_bar.addWidget(self.btn_enclosure)

        self.btn_cnc_dfm = QPushButton("⚙️ CNC 体检")
        self.btn_cnc_dfm.clicked.connect(self.on_open_cnc_dfm_dialog)
        act_bar.addWidget(self.btn_cnc_dfm)

        self.btn_export_lib = QPushButton("📦 导出生态库")
        self.btn_export_lib.clicked.connect(self.on_export_library)
        act_bar.addWidget(self.btn_export_lib)

        self.btn_pack_zip = QPushButton("📦 打包工程 ZIP")
        self.btn_pack_zip.clicked.connect(self.on_package_zip)
        act_bar.addWidget(self.btn_pack_zip)

        self.btn_send_printer = QPushButton("🖨️ 发送至打印机")
        self.btn_send_printer.clicked.connect(self.on_send_to_printer)
        act_bar.addWidget(self.btn_send_printer)

        self.btn_serve = QPushButton("📡 局域网分享")
        self.btn_serve.clicked.connect(self.on_serve_assets)
        act_bar.addWidget(self.btn_serve)

        self.btn_open_dir = QPushButton("📁 打开输出目录")
        self.btn_open_dir.clicked.connect(self.on_open_output_dir)
        act_bar.addWidget(self.btn_open_dir)

        # 建模结果全量输出面板 (切片指导书 + 多视角预览 + 查看器状态)
        self.lbl_result_info = QLabel("")
        self.lbl_result_info.setWordWrap(True)
        self.lbl_result_info.setStyleSheet(
            "QLabel { background-color: rgba(5,150,105,0.12); border: 1px solid rgba(5,150,105,0.35); "
            "border-radius: 6px; padding: 6px; font-size: 12px; }"
        )
        self.lbl_result_info.hide()  # 建模完成前隐藏
        lay.addWidget(self.lbl_result_info)
        lay.addLayout(act_bar)

        self.tabs_assets = QTabWidget()

        # Tab 1: 历史工程资产库
        tab_proj = QWidget()
        lay_proj = QVBoxLayout(tab_proj)
        lay_proj.setContentsMargins(2, 2, 2, 2)
        self.lst_projects = QListWidget()
        self.lst_projects.itemClicked.connect(self.on_project_clicked)
        self.lst_projects.itemDoubleClicked.connect(self.on_project_double_clicked)
        lay_proj.addWidget(self.lst_projects)
        self.tabs_assets.addTab(tab_proj, "📚 历史工程库 (单击全景回溯)")

        # Tab 2: 全部散落输出文件
        tab_files = QWidget()
        lay_files = QVBoxLayout(tab_files)
        lay_files.setContentsMargins(2, 2, 2, 2)
        self.lst_files = QListWidget()
        self.lst_files.itemClicked.connect(self.on_file_clicked)
        self.lst_files.itemDoubleClicked.connect(self.on_file_double_clicked)
        lay_files.addWidget(self.lst_files)
        self.tabs_assets.addTab(tab_files, "📁 全部输出文件")

        lay.addWidget(self.tabs_assets, stretch=1)
        return w

    def _build_log_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFixedHeight(140)
        self.log_view.setStyleSheet("font-family: 'Monospace', monospace;")
        lay.addWidget(self.log_view)
        return w

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def on_language_changed(self, _index: int = 0) -> None:
        """多语言切换：在简体中文与 English 间无缝热切。"""
        lang = "en" if "English" in self.cmb_lang.currentText() else "zh"
        d = self.I18N.get(lang, self.I18N["zh"])
        self.setWindowTitle(d["title"])
        self.btn_build.setText(d["btn_build"])
        self.btn_cancel.setText(d["btn_cancel"])
        self.mode_box.setTitle(d["box_input"])
        self.btn_img.setText(d["btn_pick_img"])
        self.btn_clip.setText(d["btn_clip"])
        self.lbl_preset.setText(d["label_preset"])
        self.lbl_desc.setText(d["label_desc"])
        self.btn_enhance.setText(d["btn_enhance"])
        self.param_box.setTitle(d["box_params"])
        self.btn_view_persp.setText(d["view_persp"])
        self.btn_view_top.setText(d["view_top"])
        self.btn_view_front.setText(d["view_front"])
        self.btn_view_iso.setText(d["view_iso"])
        self.btn_view_explode.setText(d["view_explode"])
        self.btn_view_blueprint.setText(d["view_blueprint"])
        self.btn_view_3d.setText(d.get("view_3d", "🎮 3D 交互视口"))
        self.btn_open_scad.setText(d["btn_open_scad"])
        self.btn_open_web.setText(d["btn_web_viewer"])
        self.btn_workspace.setText(d["btn_workspace"])
        self.btn_gallery.setText(d["btn_gallery"])
        self.btn_export_dxf.setText(d["btn_dxf"])
        self.btn_slice.setText(d["btn_slice"])
        self.btn_patch.setText(d.get("btn_patch", "✍️ 增量修剪"))
        self.btn_export_step.setText(d.get("btn_step", "📐 导出 STEP"))
        self.btn_export_lib.setText(d["btn_export_lib"])
        self.btn_pack_zip.setText(d["btn_zip"])
        self.btn_send_printer.setText(d["btn_printer"])
        self.btn_serve.setText(d["btn_serve"])
        self.btn_open_dir.setText(d["btn_open_dir"])
        self.tabs_assets.setTabText(0, d["tab_projects"])
        self.tabs_assets.setTabText(1, d["tab_files"])
        self.btn_rerender.setText(d["btn_rerender"])
        self.btn_snapshot.setText(d["btn_snapshot"])
        self.btn_rollback.setText(d["btn_rollback"])
        self.lbl_status.setText(d["idle"])
        self._settings.setValue("language", lang)
        self.log(f"Language switched to: {self.cmb_lang.currentText()}")

    def on_spec_tree_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """单击规格树节点：自动定位并高亮源码中对应的 module 定义。"""
        target_id = ""
        if item.text(0) == "id":
            target_id = item.text(1).strip()
        else:
            for i in range(item.childCount()):
                child = item.child(i)
                if child.text(0) == "id":
                    target_id = child.text(1).strip()
                    break

        if not target_id:
            candidate = item.text(1).strip() or item.text(0).strip()
            if candidate and f"module {candidate}()" in self.txt_scad.toPlainText():
                target_id = candidate

        if not target_id:
            return

        doc = self.txt_scad.document()
        find_cursor = doc.find(f"module {target_id}()")
        if not find_cursor.isNull():
            self.txt_scad.setTextCursor(find_cursor)
            self.txt_scad.centerCursor()
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(59, 130, 246, 90))  # 亮蓝高亮
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sel.cursor = find_cursor
            self.txt_scad.setExtraSelections([sel])
            self.log(f"🔍 规格树联动定位: module {target_id}()")

    def on_pick_image(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片 (支持多选多角度参考照片)", "",
            "图片 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
        )
        if paths:
            combined = "; ".join(paths)
            self.image_path.setText(combined)
            self._last_image = paths[0]
            self.log(f"已选择 {len(paths)} 张参考图片: {', '.join(Path(p).name for p in paths)}")
            pm = QPixmap(paths[0])
            if not pm.isNull():
                scaled = pm.scaled(
                    self.lbl_origin.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.lbl_origin.setPixmap(scaled)
                self.lbl_origin.setStyleSheet("QLabel { border: 2px solid #16a34a; border-radius: 8px; }")

    def on_preset_selected(self, index: int) -> None:
        """从预设下拉框载入经典建模范例描述。"""
        title = self.cmb_preset.currentText()
        if title in self.PRESETS:
            desc, hint = self.PRESETS[title]
            self.txt_desc.setPlainText(desc)
            self.txt_hint.setText(hint)
            self.log(f"已载入预设范例: {title}")

    def on_provider_changed(self, index: int) -> None:
        """快速切换主流大模型服务商配置或本地离线 Ollama。"""
        choice = self.cmb_provider.currentText()
        if "Agnes" in choice:
            self.txt_base_url.setText("https://apihub.agnes-ai.com")
            self.txt_model.setText("agnes-3.0-flash")
        elif "OpenAI" in choice:
            self.txt_base_url.setText("https://api.openai.com/v1")
            self.txt_model.setText("gpt-4o")
        elif "DeepSeek" in choice:
            self.txt_base_url.setText("https://api.deepseek.com")
            self.txt_model.setText("deepseek-chat")
        elif "Gemini" in choice:
            self.txt_base_url.setText("https://generativelanguage.googleapis.com/v1beta/openai")
            self.txt_model.setText("gemini-2.0-flash")
        elif "SiliconFlow" in choice:
            self.txt_base_url.setText("https://api.siliconflow.cn/v1")
            self.txt_model.setText("Qwen/Qwen2.5-VL-72B-Instruct")
        elif "DashScope" in choice:
            self.txt_base_url.setText("https://dashscope.aliyuncs.com/compatible-mode/v1")
            self.txt_model.setText("qwen-vl-max")
        elif "Groq" in choice:
            self.txt_base_url.setText("https://api.groq.com/openai/v1")
            self.txt_model.setText("llama-3.2-90b-vision-preview")
        elif "Ollama" in choice:
            self.txt_base_url.setText("http://localhost:11434/v1")
            self.txt_model.setText("qwen2.5-coder:latest")
            self.log("已切换至本地 Ollama 离线模式 (无需 API Key)")

    def on_paste_clipboard(self) -> None:
        """从系统剪贴板获取截图并载入。"""
        cb = QApplication.clipboard()
        img = cb.image()
        if img.isNull():
            text = cb.text().strip()
            if text and Path(text).exists() and Path(text).suffix.lower() in (
                ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"
            ):
                self.image_path.setText(text)
                self._last_image = text
                pm = QPixmap(text)
                if not pm.isNull():
                    scaled = pm.scaled(
                        self.lbl_origin.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self.lbl_origin.setPixmap(scaled)
                    self.lbl_origin.setStyleSheet("QLabel { border: 2px solid #16a34a; border-radius: 8px; }")
                self.log(f"已从剪贴板载入图片路径: {Path(text).name}")
                return
            QMessageBox.information(self, "提示", "剪贴板中未找到图片数据。\n可先按 PrintScreen / 截图工具截图后再次点击粘贴。")
            return

        outdir = Path(self.txt_outdir.text().strip() or "out") / "_prep"
        outdir.mkdir(parents=True, exist_ok=True)
        clip_path = outdir / "clipboard_screenshot.png"
        img.save(str(clip_path), "PNG")

        self.image_path.setText(str(clip_path))
        self._last_image = str(clip_path)
        self.log(f"📋 已从剪贴板载入截图: {clip_path.name} ({img.width()}×{img.height()}px)")

        pm = QPixmap.fromImage(img)
        scaled = pm.scaled(
            self.lbl_origin.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.lbl_origin.setPixmap(scaled)
        self.lbl_origin.setStyleSheet("QLabel { border: 2px solid #16a34a; border-radius: 8px; }")

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """快捷键支持：在主窗口非输入区域按 Ctrl+V 触发剪贴板截图粘贴。"""
        if event.matches(QKeySequence.StandardKey.Paste):
            focus = QApplication.focusWidget()
            if not isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit)):
                self.on_paste_clipboard()
                return
        super().keyPressEvent(event)

    def on_build(self) -> None:
        raw_imgs = [p.strip() for p in self.image_path.text().split(";") if p.strip()]
        image_input = raw_imgs if len(raw_imgs) > 1 else (raw_imgs[0] if raw_imgs else None)
        desc = self.txt_desc.toPlainText().strip()
        hint = self.txt_hint.text().strip()

        if not image_input and not desc:
            QMessageBox.warning(self, "缺少输入", "请选择图片或输入文字描述。")
            return

        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "进行中", "已有任务在执行。")
            return

        # 构建 config
        outdir = self.txt_outdir.text().strip() or "out"
        fmt = self.cmb_format.currentText()
        config = AgentConfig(
            output_dir=outdir,
            export_format=fmt,
            base_url=self.txt_base_url.text().strip() or "https://apihub.agnes-ai.com",
            model=self.txt_model.text().strip() or "agnes-3.0-flash",
            temperature=self.spn_temperature.value(),
            render_precision=self.cmb_precision.currentText(),
            image_max_side=self.spn_max_side.value(),
            enable_edge_map=self.chk_edge_map.isChecked(),
        )
        # 把审查/修复轮数写入 agent
        if not config.api_key and not config.is_local_endpoint:
            QMessageBox.warning(
                self, "缺少 API Key",
                "未检测到 API Key。\n"
                "请设置环境变量 AGNES_API_KEY（或 OPENAI_API_KEY / DEEPSEEK_API_KEY）。\n"
                "若使用本地模型，请将服务商切换为「本地 Ollama (完全离线免Key)」。",
            )
            return

        agent = OpenscadAgent(config)
        # 所有可变 agent 参数在主线程一次性写入，worker 只读，避免竞态
        agent.max_fix_rounds = self.spn_fix_rounds.value()
        agent.max_review_rounds = self.spn_review_rounds.value()
        agent.enable_multi_view = self.chk_review.isChecked()  # 审查开启时才做多视角
        agent.export_3mf = self.chk_3mf.isChecked()  # 3MF 切片同步导出开关
        agent.export_2d = self.chk_2d.isChecked()   # 2D 激光投影 (DXF+SVG) 开关
        review = self.chk_review.isChecked() and bool(image_input)  # 仅图片场景可审查（需原图比对）
        # 兜底防连点：isRunning 拦截已覆盖主流路径；若旧 worker 残留先标记取消，避免引用被覆盖后失控
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
        self._worker = BuildWorker(agent, image_input, desc, hint, review=review, parent=self)
        self._worker.log_signal.connect(self.log)
        self._worker.done_signal.connect(self.on_done)
        self._worker.error_signal.connect(self.on_error)

        # 保存本次原图供对比展示，并清空旧预览
        self._last_image = raw_imgs[0] if raw_imgs else None
        self._last_desc = desc
        self._clear_compare()

        self.btn_build.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.lbl_status.setText("运行中…")
        self.progress.setRange(0, 0)  # 不定进度
        self.log("启动后台任务…")
        self._worker.start()
        self._save_settings()

    def on_cancel_build(self) -> None:
        """安全中止正在执行的后台建模任务（协作式取消，不强制杀线程）。"""
        if self._worker and self._worker.isRunning():
            self.log("⚠️ 收到取消指令，正在取消后台任务…")
            self._worker.cancel()
            # 最多等 3s 让当前 openscad 子进程收敛；terminate 会留下孤儿 openscad 进程，
            # 超时则让 worker 自然结束后由 finished 信号清理。
            self._worker.wait(3000)
            self.btn_build.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.lbl_status.setText("已中止")
            self.log("🛑 建模任务已由用户手动中止。")

    def on_enhance_prompt(self) -> None:
        """为当前描述智能追加工业级公差、接地与壁厚约束。"""
        cur = self.txt_desc.toPlainText().strip()
        if not cur:
            cur = "一个工业级零部件结构"
        enhancement = "，要求结构底部平整平贴 Z=0 打印底板，壁厚均匀（建议2.0mm），配合孔与活动特征预留 0.3mm 间隙公差，关键受力转角倒圆角过渡。"
        if "Z=0" not in cur and "壁厚" not in cur:
            self.txt_desc.setPlainText(cur + enhancement)
            self.log("🪄 已智能追加工程级壁厚、配合公差与 Z=0 接地约束！")
        else:
            self.log("提示: 当前描述已包含专业工程约束。")

    def on_done(self, result) -> None:
        self.btn_build.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.lbl_status.setText("完成" if result.success else "失败")

        if result.spec:
            self._populate_spec_tree(result.spec)
        if result.scad_path:
            self._scad_path = result.scad_path
            try:
                src = Path(result.scad_path).read_text(encoding="utf-8")
                self.txt_scad.setPlainText(src)
            except OSError:
                self.txt_scad.setPlainText("(无法读取)")
        if result.model_file:
            self._refresh_file_list(Path(result.model_file).parent)
        if result.preview_png and Path(result.preview_png).exists():
            self._current_preview_png = result.preview_png
            self._show_preview(result.preview_png, origin_image=self._last_image)
        if getattr(result, "viewer_html", None) and Path(result.viewer_html).exists():
            self._current_viewer_html = result.viewer_html
            if self.preview_stack.currentIndex() == 1:
                self._load_3d_viewer(result.viewer_html)

        if result.success:
            self.log("✅ 建模成功")
            if result.model_file:
                self.log(f"3D 模型: {result.model_file}")
            # 3MF 切片文件同步导出反馈
            if getattr(self, "chk_3mf", None) and self.chk_3mf.isChecked() and result.model_file:
                p3mf = Path(result.model_file).with_suffix(".3mf")
                if p3mf.exists():
                    self.log(f"📦 已同步生成 3MF 切片文件: {p3mf.name}")
            # 2D 激光投影同步导出反馈
            if getattr(self, "chk_2d", None) and self.chk_2d.isChecked() and result.model_file:
                pstem = Path(result.model_file).stem
                for ext in ("dxf", "svg"):
                    p2d = Path(result.model_file).with_suffix(f".{ext}")
                    if p2d.exists():
                        self.log(f"📐 已同步生成 2D 激光投影 ({ext.upper()}): {p2d.name}")
            doc = getattr(result, "doctor_report", None)
            if doc:
                self.log(f"🩺 适印性自检: {doc.summary()} | 尺寸: {doc.bounding_box[0]}×{doc.bounding_box[1]}×{doc.bounding_box[2]}mm | 预估耗材: {doc.weight_g}g (约¥{doc.cost_cny})")
                for w in doc.warnings[:2]:
                    self.log(f"   ⚠️ {w}")
            # 审查闭环结论
            rv = getattr(result, "review", None)
            rr = getattr(result, "review_rounds", 0)
            if rv:
                traj = " -> ".join(str(s) for s in getattr(result, "score_trajectory", []))
                traj_info = f" (走势: {traj})" if traj else ""
                self.log(f"🔍 审查闭环: {rr} 轮 | match={rv.get('match')} score={rv.get('score')}{traj_info}")
                if rv.get("verdict"):
                    self.log(f"   结论: {rv.get('verdict')}")
                for iss in (rv.get("issues") or [])[:5]:
                    self.log(f"   问题: {iss}")
            elif rr:
                self.log(f"🔍 审查闭环: {rr} 轮（已收敛）")
            # 建模结果全量输出面板 (切片指导书 + 查看器 + 多视角预览 + 3MF/2D)
            self._update_result_panel(result)
        else:
            self.log(f"❌ 失败: {'; '.join(result.errors)}")

    def on_error(self, msg: str) -> None:
        self.btn_build.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress.setRange(0, 100)
        self.lbl_status.setText("错误")
        self.log(f"❌ 错误: {msg}")

    def _update_result_panel(self, result) -> None:
        """建模完成后更新结果面板: 汇总展示全量输出资产。

        补齐 CLI 全量输出在 GUI 侧的可视化反馈:
        - 切片指导书路径 (print_guide)
        - 3D 网页查看器路径 (viewer_html)
        - 多视角预览图数量 (多视角 png 文件)
        - 3MF/2D 文件状态
        """
        lines: list[str] = []
        model_file = getattr(result, "model_file", None)
        out_dir = Path(model_file).parent if model_file else None

        # 切片指导书
        print_guide = getattr(result, "print_guide", None)
        if print_guide and Path(print_guide).exists():
            lines.append(f"📄 切片指导书: {Path(print_guide).name}")

        # 3D 网页查看器
        viewer_html = getattr(result, "viewer_html", None)
        if viewer_html and Path(viewer_html).exists():
            lines.append(f"🌐 3D 查看器: {Path(viewer_html).name}")

        # 多视角预览图
        if out_dir and model_file:
            stem = Path(model_file).stem
            multi_views = sorted(out_dir.glob(f"{stem}_v_*.png"))
            if multi_views:
                lines.append(f"📸 多视角预览: {len(multi_views)} 张")
            # 3MF 状态
            p3mf = out_dir / f"{stem}.3mf"
            if p3mf.exists():
                lines.append(f"📦 3MF 切片: {p3mf.name}")
            # 2D 投影状态
            p2d = [out_dir / f"{stem}.{ext}" for ext in ("dxf", "svg") if (out_dir / f"{stem}.{ext}").exists()]
            if p2d:
                lines.append(f"📐 2D 投影: {', '.join(p.name for p in p2d)}")

        # 装配干涉检测结果 (doctor_report)
        doc = getattr(result, "doctor_report", None)
        if doc and doc.warnings:
            for w in doc.warnings[:3]:
                lines.append(f"⚠️ {w}")

        if lines:
            text = " | ".join(lines)
            self.lbl_result_info.setText(f"<b>✅ 全量输出:</b> {text}")
            self.lbl_result_info.show()
        else:
            self.lbl_result_info.hide()

    def on_switch_view(self, view_name: str) -> None:
        """切换预览视角显示（已渲染则秒切，未预渲染则即时生成）。"""
        self.preview_stack.setCurrentIndex(0)
        self._update_view_buttons_active(view_name)
        if not self._current_preview_png:
            self.log("提示: 尚未渲染预览图")
            return
        p = Path(self._current_preview_png)
        # 查找对应视角图片 (例如 xxx_preview_v_top.png)
        candidates = []
        if view_name == "perspective":
            candidates = [p, p.with_name(f"{p.stem}_v_perspective.png")]
        elif view_name == "explode":
            candidates = [p.with_name(f"{p.stem}_v_explode.png"), p.with_name(f"{p.stem}_explode.png")]
        elif view_name == "blueprint":
            candidates = [p.with_name(f"{p.stem}_blueprint.png"), p.with_name(f"{p.stem}_v_blueprint.png")]
        else:
            candidates = [
                p.with_name(f"{p.stem}_v_{view_name}.png"),
                p.with_name(f"{p.stem}_{view_name}.png"),
            ]
        for c in candidates:
            if c.exists():
                self._show_preview(str(c), origin_image=self._last_image)
                self.log(f"已切换预览视角: {view_name} -> {c.name}")
                return

        # 若未预渲染多视角，根据当前源码即时按需渲染该视角 (约0.2秒完成)
        if self._scad_path and Path(self._scad_path).exists():
            target_view_png = p.with_name(f"{p.stem}_v_{view_name}.png")
            try:
                self.log(f"正在即时渲染 {view_name} 视角图…")
                config = AgentConfig(
                    output_dir=str(p.parent),
                    render_precision=self.cmb_precision.currentText(),
                )
                renderer = OpenscadRenderer(config)
                if view_name == "explode":
                    renderer.render_exploded_view(Path(self._scad_path), target_view_png, size=(600, 450), explode_factor=1.2)
                elif view_name == "blueprint":
                    renderer.render_blueprint(Path(self._scad_path), target_view_png)
                else:
                    renderer.render_preview_view(Path(self._scad_path), target_view_png, size=(600, 450), view=view_name)
                self._show_preview(str(target_view_png), origin_image=self._last_image)
                self.log(f"✅ 即时渲染视角成功: {target_view_png.name}")
                self._refresh_file_list(p.parent)
                return
            except Exception as e:
                self.log(f"即时渲染视角异常: {e}")

        self.log(f"提示: 未找到 {view_name} 视角图")

    def on_switch_to_3d_viewport(self) -> None:
        """切换至内嵌 3D 实时交互视口。"""
        self.preview_stack.setCurrentIndex(1)
        self._update_view_buttons_active("3d")
        html_p = self._find_current_viewer_html()
        if html_p and html_p.exists():
            self._load_3d_viewer(str(html_p))
        else:
            self.log("提示: 当前模型尚未生成 3D 网页查看器，完成建模后即可交互检视。")

    def _find_current_viewer_html(self) -> Optional[Path]:
        """查找当前模型对应的 _viewer.html 路径。"""
        if self._current_viewer_html and Path(self._current_viewer_html).exists():
            return Path(self._current_viewer_html)
        if self._scad_path:
            p = Path(self._scad_path)
            html = p.with_name(f"{p.stem}_viewer.html")
            if html.exists():
                return html
        if self._current_preview_png:
            p = Path(self._current_preview_png)
            stem_clean = p.stem.replace("_preview", "").replace("_v_perspective", "")
            cand = p.with_name(f"{stem_clean}_viewer.html")
            if cand.exists():
                return cand
        out_dir = Path(self.txt_outdir.text().strip() or "out")
        viewers = sorted(out_dir.glob("*_viewer.html"))
        if viewers:
            return viewers[-1]
        return None

    def _load_3d_viewer(self, html_path: str) -> None:
        """将指定 HTML 网页查看器载入内嵌 3D 视口。"""
        p = Path(html_path)
        if not p.exists():
            return
        self._current_viewer_html = str(p)
        if HAS_WEBENGINE and self.web_view_3d is not None:
            self.web_view_3d.setUrl(QUrl.fromLocalFile(str(p.resolve())))
            self.log(f"🎮 已在内嵌 3D 视口中载入模型: {p.name}")
        else:
            self.log(f"🎮 3D 视口已就绪: {p.name}")

    def _reload_3d_viewer(self) -> None:
        """刷新当前 3D 视口场景。"""
        html_p = self._find_current_viewer_html()
        if html_p and html_p.exists():
            self._load_3d_viewer(str(html_p))

    def _update_view_buttons_active(self, active_view: str) -> None:
        """更新视角按钮高亮状态。"""
        btn_map = {
            "perspective": self.btn_view_persp,
            "top": self.btn_view_top,
            "front": self.btn_view_front,
            "iso": self.btn_view_iso,
            "explode": self.btn_view_explode,
            "blueprint": self.btn_view_blueprint,
            "3d": self.btn_view_3d,
        }
        for k, btn in btn_map.items():
            if k == active_view:
                btn.setStyleSheet(
                    "QPushButton { font-weight: bold; background-color: #2563eb; color: white; border-radius: 4px; }"
                )
            else:
                btn.setStyleSheet("")

    def on_open_in_openscad(self) -> None:
        """后台拉起 OpenSCAD 检视当前 .scad 源码。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成 OpenSCAD 文件。请先完成一次建模。")
            return
        try:
            subprocess.Popen(["openscad", self._scad_path])
            self.log(f"已在 OpenSCAD 中打开: {self._scad_path}")
        except Exception as e:
            QMessageBox.warning(self, "无法启动 OpenSCAD", f"启动 OpenSCAD 失败: {e}\n请确认已安装 openscad。")

    def on_open_output_dir(self) -> None:
        """用系统文件管理器打开输出目录。"""
        out_dir = Path(self.txt_outdir.text().strip() or "out").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(out_dir)))
        self.log(f"已打开输出目录: {out_dir}")

    def on_open_web_viewer(self) -> None:
        """用系统默认浏览器打开便携式 3D 网页查看器。"""
        if self._scad_path:
            p = Path(self._scad_path)
            html = p.with_name(f"{p.stem}_viewer.html")
            if html.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(html)))
                self.log(f"已在浏览器中打开 3D 查看器: {html.name}")
                return
        out_dir = Path(self.txt_outdir.text().strip() or "out")
        viewers = sorted(out_dir.glob("*_viewer.html"))
        if viewers:
            html = viewers[-1]
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(html)))
            self.log(f"已在浏览器中打开 3D 查看器: {html.name}")
            return
        QMessageBox.information(self, "提示", "尚未生成 3D 网页查看器。导出 STL 3D模型时将自动配套生成。")

    def on_open_gallery(self) -> None:
        """扫描输出目录生成并打开 3D 模型展厅画廊网页。"""
        out_dir = Path(self.txt_outdir.text().strip() or "out").resolve()
        from openscad_agent.gallery import generate_gallery
        gpath = generate_gallery(out_dir)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(gpath)))
        self.log(f"🏛️ 3D 模型展厅画廊已生成并在浏览器中打开: {gpath.name}")

    def on_export_dxf(self) -> None:
        """为当前模型导出 2D 激光切割/CNC DXF 与 SVG 投影轮廓。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成 OpenSCAD 文件。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        out_dxf = scad_p.with_suffix(".dxf")
        out_svg = scad_p.with_suffix(".svg")
        config = AgentConfig(output_dir=str(scad_p.parent))
        renderer = OpenscadRenderer(config)
        try:
            renderer.export_2d_projection(scad_p, out_dxf, cut=False)
            renderer.export_2d_projection(scad_p, out_svg, cut=False)
            self.log(f"📐 激光切割/CNC 轮廓导出成功:\n   DXF: {out_dxf.name}\n   SVG: {out_svg.name}")
            self._refresh_file_list(scad_p.parent)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"2D 投影导出异常: {e}")

    def on_slice_model(self) -> None:
        """为当前模型直接生成生产级切片 G-code 并核算打印制造指标。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成 OpenSCAD 文件。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        stl_p = scad_p.with_suffix(".stl")
        if not stl_p.exists():
            QMessageBox.information(self, "提示", f"未找到可切片的 STL 模型文件 ({stl_p.name})。请先渲染导出 3D 模型。")
            return

        materials = ["PLA", "PETG", "ABS", "TPU", "PC"]
        cur_mat = getattr(self._agent.config, "material", "PLA") if self._agent else "PLA"
        idx = materials.index(cur_mat) if cur_mat in materials else 0
        mat, ok = QInputDialog.getItem(self, "选择切片耗材类型", "请选择 3D 打印推荐耗材物性模板:", materials, idx, False)
        if not ok or not mat:
            return

        from openscad_agent.slicer import slice_model, SlicingConfig
        gcode_p = scad_p.with_suffix(".gcode")
        try:
            s_cfg = SlicingConfig(material=mat, layer_height=0.2, infill_density=15)
            self.log(f"🍰 正在调用切片引擎生成 G-code ({mat}, 层高 0.2mm, 填充 15%)…")
            res = slice_model(stl_p, out_gcode=gcode_p, config=s_cfg, force=True)
            if res.success:
                self.log(f"✅ 切片成功: {gcode_p.name} (耗时: {res.estimated_time_formatted}, 耗材: {res.filament_weight_grams}g, 层数: {res.layer_count})")
                self._refresh_file_list(scad_p.parent)
                QMessageBox.information(
                    self, "切片成功",
                    f"物理切片文件已生成:\n{gcode_p.name}\n\n"
                    f"⏱️ 预估打印耗时: {res.estimated_time_formatted}\n"
                    f"⚖️ 耗材消耗克重: {res.filament_weight_grams} g ({res.filament_length_meters} m)\n"
                    f"📐 物理切片总层数: {res.layer_count} 层 (层高 0.2mm)\n"
                    f"⚙️ 调度切片引擎: {res.slicer_engine}"
                )
            else:
                QMessageBox.warning(self, "切片未完成", f"切片引擎报错:\n{res.error_msg}")
        except Exception as e:
            QMessageBox.warning(self, "切片失败", f"切片异常: {e}")

    def on_patch_model(self) -> None:
        """交互式自然语言红线指令增量修剪当前模型并热重载。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        stem = scad_p.stem

        cmd, ok = QInputDialog.getText(
            self, "参数化增量修剪",
            "请输入自然语言红线指令 (例如: 高度增加 10mm, 孔径扩大 1mm, 倒角 2mm, 等比缩放 1.2):",
            QLineEdit.EchoMode.Normal, "高度增加 10mm"
        )
        if not ok or not cmd.strip():
            return
        cmd = cmd.strip()

        from openscad_agent.geometry import apply_patch_to_model
        try:
            self.log(f"✍️ 正在应用增量修剪指令: '{cmd}'...")
            spec_p, scad_p, updated = apply_patch_to_model(stem, cmd, out_dir=scad_p.parent, recompile=True)
            self._scad_path = str(scad_p)
            self.code_edit.setPlainText(scad_p.read_text(encoding="utf-8"))
            stl_p = scad_p.with_suffix(".stl")
            if stl_p.exists() and hasattr(self, "_render_3d_preview"):
                self._render_3d_preview(stl_p)
            self.log(f"✅ 模型增量修剪成功并已热重载: {stem}")
            QMessageBox.information(self, "修剪成功", f"指令 '{cmd}' 已应用！模型已重新编译。")
        except Exception as e:
            QMessageBox.warning(self, "修剪失败", f"增量修剪异常: {e}")

    def on_export_step(self) -> None:
        """将当前模型导出为工业级标准 ISO 10303-21 STEP 实体模型与 FreeCAD 脚本。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        stl_p = scad_p.with_suffix(".stl")
        if not stl_p.exists():
            QMessageBox.information(self, "提示", f"未找到可转换的 STL 文件 ({stl_p.name})。请先渲染导出 3D 模型。")
            return

        from openscad_agent.interop import export_step_from_stl, export_freecad_script
        try:
            step_p = export_step_from_stl(stl_p, out_step=scad_p.with_suffix(".step"), name=scad_p.stem)
            cand_spec = scad_p.with_name(f"{scad_p.stem}_spec.json")
            if cand_spec.exists():
                import json
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                export_freecad_script(spec, out_script=scad_p.with_name(f"{scad_p.stem}_freecad.py"))
            self.log(f"📐 工业级 STEP 模型已导出: {step_p.name}")
            self._refresh_file_list(scad_p.parent)
            QMessageBox.information(
                self, "STEP 导出成功",
                f"已生成工业级标准 ISO 10303-21 STEP 实体模型:\n{step_p.name}\n\n"
                f"可直接无缝导入 SolidWorks, Fusion 360, Autodesk Inventor, Siemens NX 与 FreeCAD！"
            )
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"导出 STEP 异常: {e}")

    def on_export_blueprint(self) -> None:
        """从当前模型规范生成带工业级尺寸标注的纯矢量三视图与轴测工程图纸 (SVG/PNG)。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        cand_spec = scad_p.with_name(f"{scad_p.stem}_spec.json")
        if not cand_spec.exists():
            QMessageBox.information(self, "提示", f"未找到规格文件 ({cand_spec.name})。请先生成模型规格。")
            return

        from openscad_agent.blueprint import generate_technical_blueprint
        try:
            spec = json.loads(cand_spec.read_text(encoding="utf-8"))
            out_svg = scad_p.with_name(f"{scad_p.stem}_blueprint.svg")
            out_png = scad_p.with_name(f"{scad_p.stem}_blueprint.png")
            generate_technical_blueprint(
                spec,
                out_svg=out_svg,
                out_png=out_png,
                preview_png=scad_p.with_suffix(".png"),
            )
            self.log(f"📐 工业级尺寸标注工程图纸已生成: {out_svg.name}")
            self._refresh_file_list(scad_p.parent)
            reply = QMessageBox.question(
                self, "工程图纸生成成功",
                f"已生成工业级标准尺寸标注图纸:\n• 纯矢量图纸: {out_svg.name}\n"
                f"• 光栅高清图: {out_png.name if out_png.exists() else '未渲染'}\n\n"
                f"是否立即在浏览器或默认看图软件中打开查看？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(out_svg)))
        except Exception as e:
            QMessageBox.warning(self, "图纸生成失败", f"生成工程图纸异常: {e}")

    def on_export_multimaterial_3mf(self) -> None:
        """从当前模型规格与源码中导出多构件多色 3MF 生产切片工程文件 (Bambu Studio / OrcaSlicer / Prusa MMU)。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        cand_spec = scad_p.with_name(f"{scad_p.stem}_spec.json")
        if not cand_spec.exists():
            QMessageBox.information(self, "提示", f"未找到规格文件 ({cand_spec.name})。请先生成模型规格。")
            return

        from openscad_agent.multimaterial import export_multimaterial_3mf
        try:
            spec = json.loads(cand_spec.read_text(encoding="utf-8"))
            out_3mf = scad_p.with_name(f"{scad_p.stem}_multimaterial.3mf")
            res_3mf = export_multimaterial_3mf(scad_p, spec, out_3mf)
            self.log(f"🎨 多构件多色 3MF 生产切片包已生成: {res_3mf.name}")
            self._refresh_file_list(scad_p.parent)
            QMessageBox.information(
                self, "多色 3MF 导出成功",
                f"已生成原生多色多耗材 3MF 制造工程包:\n{res_3mf.name}\n\n"
                f"各分色部件已封装为独立 Mesh 并映射颜色/工具头，可直接在 Bambu Studio、OrcaSlicer 或 PrusaSlicer (MMU) 中一键分配多色喷头打印！"
            )
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"导出多色 3MF 异常: {e}")

    def on_tolerance_compensation(self) -> None:
        """根据耗材收缩率与配合要求，执行智能装配配合公差与孔径热缩反向预补偿。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        cand_spec = scad_p.with_name(f"{scad_p.stem}_spec.json")
        if not cand_spec.exists():
            QMessageBox.information(self, "提示", f"未找到规格文件 ({cand_spec.name})。请先生成模型规格。")
            return

        fits = [
            ("过渡配合 Normal (+0.20mm, 推荐紧固件固定)", "normal"),
            ("间隙配合 Loose (+0.35mm, 推荐活动合页/滑块)", "loose"),
            ("过盈配合 Press Fit (+0.05mm, 免胶水紧压配)", "press_fit"),
        ]
        items = [f[0] for f in fits]
        chosen, ok = QInputDialog.getItem(self, "选择配合公差等级", "请选择装配公差模式与孔径热缩补偿级别:", items, 0, False)
        if not ok or not chosen:
            return
        fit_key = next((f[1] for f in fits if f[0] == chosen), "normal")

        from openscad_agent.fea import compensate_and_recompile_model
        try:
            mat = getattr(self, "cmb_material", None)
            mat_name = mat.currentText() if mat else "PLA"
            _, _, _, actions = compensate_and_recompile_model(
                scad_p.stem, out_dir=scad_p.parent, material=mat_name, fit_type=fit_key, recompile=True
            )
            self.log(f"🌡️ 装配公差与孔径热缩预补偿完成: {len(actions)} 项修正")
            self._refresh_file_list(scad_p.parent)
            self._load_scad_into_editor(scad_p)
            QMessageBox.information(
                self, "公差补偿与重载成功",
                f"已为模型应用 {mat_name} 热缩与装配配合补偿:\n\n• 修正明细:\n" + "\n".join(actions) +
                "\n\n已自动更新 3D 实体模型与源码！"
            )
        except Exception as e:
            QMessageBox.warning(self, "补偿失败", f"公差补偿异常: {e}")

    def on_apply_lattice_lightweighting(self) -> None:
        """为当前模型注入拓扑轻量化点阵晶格/蜂窝减重结构并重新渲染。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        cand_spec = scad_p.with_name(f"{scad_p.stem}_spec.json")
        if not cand_spec.exists():
            QMessageBox.information(self, "提示", f"未找到规格文件 ({cand_spec.name})。请先生成模型规格。")
            return

        patterns = [
            ("六边形蜂窝 Honeycomb (航空级高比刚度，推荐承载平板)", "honeycomb"),
            ("正交十字格栅 Orthogrid (双向对称加强筋，推荐箱体壳体)", "orthogrid"),
            ("三维八面体 Diamond (全向等刚度微桁架，推荐机械臂与连接件)", "diamond"),
            ("螺旋极小曲面 Gyroid (柔性 TPU 缓震与流体散热孔)", "gyroid"),
        ]
        items = [p[0] for p in patterns]
        chosen, ok = QInputDialog.getItem(self, "选择轻量化晶格构型", "请选择拓扑减重内部微结构单元:", items, 0, False)
        if not ok or not chosen:
            return
        pat_key = next((p[1] for p in patterns if p[0] == chosen), "honeycomb")

        from openscad_agent.lattice import apply_lattice_lightweighting, estimate_lattice_weight_savings
        from openscad_agent.geometry import spec_to_scad
        from openscad_agent.core import OpenscadRenderer, AgentConfig
        try:
            spec = json.loads(cand_spec.read_text(encoding="utf-8"))
            new_spec, actions = apply_lattice_lightweighting(spec, pattern=pat_key, cell_size=8.0, wall_thickness=1.2)
            cand_spec.write_text(json.dumps(new_spec, indent=2, ensure_ascii=False), encoding="utf-8")
            scad_code = spec_to_scad(new_spec, fn=48, ground_z=True)
            scad_p.write_text(scad_code, encoding="utf-8")
            cfg = AgentConfig(output_dir=str(scad_p.parent))
            renderer = OpenscadRenderer(cfg)
            renderer.export_model(scad_p, scad_p.with_suffix(".stl"), force=True)

            p0 = (new_spec.get("parts") or [{}])[0]
            sz = p0.get("size") or [50, 40, 10]
            w, l, h = (float(sz[0]), float(sz[1]), float(sz[2])) if len(sz) >= 3 else (50.0, 40.0, 10.0)
            rep = estimate_lattice_weight_savings(w, l, h, pattern=pat_key, cell_size=8.0, wall_thickness=1.2)

            self.log(f"🪶 拓扑轻量化减重完成: 预计减重 {rep.weight_reduction_pct}% (节省耗材 {rep.material_saved_grams}g)")
            self._refresh_file_list(scad_p.parent)
            self._load_scad_into_editor(scad_p)
            QMessageBox.information(
                self, "轻量化减重成功",
                f"已成功应用 {chosen.split()[0]} 拓扑晶格减重结构:\n\n"
                f"• 预计减重比例: {rep.weight_reduction_pct}%\n"
                f"• 预计节省耗材: {rep.material_saved_grams} g\n"
                f"• 结构工艺建议:\n" + "\n".join(f"  - {r}" for r in rep.recommendations) +
                "\n\n已自动更新 3D 实体模型与源码！"
            )
        except Exception as e:
            QMessageBox.warning(self, "减重失败", f"轻量化处理异常: {e}")

    def on_export_assembly_sop(self) -> None:
        """从当前模型规格自动推导装配拓扑时序并导出工业标准作业指导书 (SOP HTML)。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        cand_spec = scad_p.with_name(f"{scad_p.stem}_spec.json")
        if not cand_spec.exists():
            QMessageBox.information(self, "提示", f"未找到规格文件 ({cand_spec.name})。请先生成模型规格。")
            return

        from openscad_agent.assembly_sop import derive_assembly_sequence, generate_sop_guide_html
        try:
            spec = json.loads(cand_spec.read_text(encoding="utf-8"))
            plan = derive_assembly_sequence(spec)
            out_sop = scad_p.with_name(f"{scad_p.stem}_sop.html")
            generate_sop_guide_html(plan, out_sop)
            self.log(f"🛠️ 工业标准装配工艺指导书已生成: {out_sop.name} (工步数={plan.total_steps})")
            self._refresh_file_list(scad_p.parent)
            reply = QMessageBox.question(
                self, "装配指导书生成成功",
                f"已生成工业级标准作业指导书 (SOP):\n• {out_sop.name}\n"
                f"• 规划工步: {plan.total_steps} 步 (含 BOM 清单与无碰撞脱出向量)\n\n"
                f"是否立即在浏览器中打开查看？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(out_sop)))
        except Exception as e:
            QMessageBox.warning(self, "生成失败", f"导出装配指导书异常: {e}")

    def on_check_assembly_interference(self) -> None:
        """对当前装配体执行三维包围盒碰撞检测与孔轴配合过盈干涉体检。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        cand_spec = scad_p.with_name(f"{scad_p.stem}_spec.json")
        if not cand_spec.exists():
            QMessageBox.information(self, "提示", f"未找到规格文件 ({cand_spec.name})。请先生成模型规格。")
            return

        from openscad_agent.interference import check_assembly_interference
        try:
            spec = json.loads(cand_spec.read_text(encoding="utf-8"))
            report = check_assembly_interference(spec, clearance_threshold_mm=0.30)
            self.log(f"🔍 装配物理干涉校验完成: 硬碰撞={report.hard_clashes_count}, 配合过盈={report.fit_interferences_count}, 临界预警={report.clearance_warnings_count}")

            if report.is_assembly_valid and report.clearance_warnings_count == 0:
                QMessageBox.information(
                    self, "干涉检查通过",
                    f"✅ 装配体验证完美！\n\n已校验全部 {report.total_pairs_checked} 对构件组合，无空间实体碰撞、无配合过盈且安全间隙充裕。"
                )
            else:
                details = []
                for c in report.clashes[:5]:
                    details.append(f"• [{c.part_a} ⚡ {c.part_b}] ({c.clash_type}): {c.description}\n  建议: {c.resolution_advice}")
                if len(report.clashes) > 5:
                    details.append(f"... 另有 {len(report.clashes) - 5} 项干涉")

                title = "装配存在严重干涉" if not report.is_assembly_valid else "装配间隙预警"
                icon_fn = QMessageBox.critical if not report.is_assembly_valid else QMessageBox.warning
                icon_fn(
                    self, title,
                    f"{report.summary}\n\n冲突明细:\n" + "\n\n".join(details)
                )
        except Exception as e:
            QMessageBox.warning(self, "体检失败", f"干涉校验异常: {e}")

    def on_open_fasteners_dialog(self) -> None:
        """打开标准公制紧固件选择与负形沉头切削模块生成器。"""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        from openscad_agent.fasteners import (
            FASTENER_CATALOG,
            FastenerStandard,
            generate_bolt_scad,
            generate_nut_scad,
            generate_washer_scad,
            generate_heat_set_insert_scad,
            generate_counterbore_cutter,
            generate_countersink_cutter,
            generate_hex_pocket_cutter,
            generate_heat_set_hole_cutter,
            generate_fasteners_bom,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("🔩 标准紧固件与沉头孔配置库 (ISO / Heat-Set)")
        dlg.resize(520, 480)
        d_lay = QVBoxLayout(dlg)

        form_lay = QGridLayout()
        form_lay.addWidget(QLabel("紧固件类型:"), 0, 0)
        cmb_type = QComboBox()
        cmb_type.addItems([
            "ISO 4762 (内六角圆柱头螺钉)",
            "ISO 10642 (90° 沉头螺钉)",
            "ISO 7380 (内六角半圆头螺钉)",
            "ISO 4032 (六角螺母)",
            "ISO 7089 (标准平垫圈)",
            "热熔铜螺母 (Heat-Set Insert)",
        ])
        form_lay.addWidget(cmb_type, 0, 1)

        form_lay.addWidget(QLabel("公称规格:"), 1, 0)
        cmb_size = QComboBox()
        cmb_size.addItems(["M2", "M2.5", "M3", "M4", "M5", "M6", "M8", "M10"])
        cmb_size.setCurrentText("M3")
        form_lay.addWidget(cmb_size, 1, 1)

        form_lay.addWidget(QLabel("公称长度 / 板厚 (mm):"), 2, 0)
        spn_len = QDoubleSpinBox()
        spn_len.setRange(2.0, 100.0)
        spn_len.setValue(12.0)
        spn_len.setSingleStep(1.0)
        form_lay.addWidget(spn_len, 2, 1)

        d_lay.addLayout(form_lay)

        txt_preview = QTextEdit()
        txt_preview.setPlaceholderText("// 紧固件实体代码或差集切削模块预览…")
        txt_preview.setFontFamily("Monospace")
        txt_preview.setFontPointSize(9)
        d_lay.addWidget(txt_preview)

        def update_preview():
            t_idx = cmb_type.currentIndex()
            sz = cmb_size.currentText()
            l = spn_len.value()
            if t_idx == 0:
                txt_preview.setText(generate_bolt_scad("ISO_4762", size=sz, length=l, thread=True))
            elif t_idx == 1:
                txt_preview.setText(generate_bolt_scad("ISO_10642", size=sz, length=l, thread=True))
            elif t_idx == 2:
                txt_preview.setText(generate_bolt_scad("ISO_7380", size=sz, length=l, thread=True))
            elif t_idx == 3:
                txt_preview.setText(generate_nut_scad(size=sz))
            elif t_idx == 4:
                txt_preview.setText(generate_washer_scad(size=sz))
            elif t_idx == 5:
                txt_preview.setText(generate_heat_set_insert_scad(size=sz))

        cmb_type.currentIndexChanged.connect(lambda _: update_preview())
        cmb_size.currentIndexChanged.connect(lambda _: update_preview())
        spn_len.valueChanged.connect(lambda _: update_preview())
        update_preview()

        btn_row = QHBoxLayout()
        btn_copy_cutter = QPushButton("✂️ 复制差集沉孔切削代码")
        def copy_cutter():
            t_idx = cmb_type.currentIndex()
            sz = cmb_size.currentText()
            l = spn_len.value()
            if t_idx == 1:
                code = generate_countersink_cutter(size=sz, plate_thickness=l)
            elif t_idx == 3:
                code = generate_hex_pocket_cutter(size=sz, pocket_depth=3.0)
            elif t_idx == 5:
                code = generate_heat_set_hole_cutter(size=sz, plate_thickness=l)
            else:
                code = generate_counterbore_cutter(size=sz, plate_thickness=l)
            QApplication.clipboard().setText(code)
            QMessageBox.information(dlg, "已复制", "差集负形沉头切削模块代码已成功复制到剪贴板！\n可直接在 OpenSCAD 代码中使用 difference() 扣除。")

        btn_copy_cutter.clicked.connect(copy_cutter)
        btn_row.addWidget(btn_copy_cutter)

        btn_load_editor = QPushButton("📥 载入至源码区")
        def load_to_editor():
            if hasattr(self, "txt_scad"):
                self.txt_scad.setPlainText(txt_preview.toPlainText())
                dlg.accept()
                self.log("📥 紧固件实体代码已载入 OpenSCAD 源码编辑器")
        btn_load_editor.clicked.connect(load_to_editor)
        btn_row.addWidget(btn_load_editor)

        d_lay.addLayout(btn_row)

        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bbox.rejected.connect(dlg.reject)
        d_lay.addWidget(bbox)
        dlg.exec()

    def on_open_piping_dialog(self) -> None:
        """打开工业管路、弯头、三通、法兰与宝塔接头配置对话框。"""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        from openscad_agent.piping import (
            PIPE_CATALOG,
            generate_pipe_segment,
            generate_pipe_elbow,
            generate_tee_junction,
            generate_y_splitter,
            generate_pipe_flange,
            generate_hose_barb,
            generate_manifold_block,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("🚰 工业流体管路与连接件库 (Piping & Manifolds)")
        dlg.resize(540, 500)
        d_lay = QVBoxLayout(dlg)

        form_lay = QGridLayout()
        form_lay.addWidget(QLabel("管件构型:"), 0, 0)
        cmb_type = QComboBox()
        cmb_type.addItems([
            "光滑圆弧弯管 (Elbow)",
            "工业直管段 (Straight Pipe)",
            "正交连通三通 (Tee Junction)",
            "Y 型分流管 (Y-Splitter)",
            "管道法兰盘 (Flange)",
            "宝塔软管接头 (Hose Barb)",
            "多通道歧管块 (Manifold Block)",
        ])
        form_lay.addWidget(cmb_type, 0, 1)

        form_lay.addWidget(QLabel("公称规格 (DN/PU):"), 1, 0)
        cmb_dn = QComboBox()
        cmb_dn.addItems(["DN6", "DN8", "DN10", "DN15", "DN20", "DN25", "PU_6x4", "PU_8x5"])
        cmb_dn.setCurrentText("DN15")
        form_lay.addWidget(cmb_dn, 1, 1)

        form_lay.addWidget(QLabel("弯曲角度 / 数量:"), 2, 0)
        spn_ang = QDoubleSpinBox()
        spn_ang.setRange(5.0, 180.0)
        spn_ang.setValue(90.0)
        spn_ang.setSingleStep(5.0)
        form_lay.addWidget(spn_ang, 2, 1)

        form_lay.addWidget(QLabel("长度 / 半径 R (mm):"), 3, 0)
        spn_rad = QDoubleSpinBox()
        spn_rad.setRange(5.0, 200.0)
        spn_rad.setValue(30.0)
        spn_rad.setSingleStep(5.0)
        form_lay.addWidget(spn_rad, 3, 1)

        d_lay.addLayout(form_lay)

        txt_preview = QTextEdit()
        txt_preview.setPlaceholderText("// 工业管路 OpenSCAD 几何源码预览…")
        txt_preview.setFontFamily("Monospace")
        txt_preview.setFontPointSize(9)
        d_lay.addWidget(txt_preview)

        def update_preview():
            t_idx = cmb_type.currentIndex()
            dn = cmb_dn.currentText()
            ang = spn_ang.value()
            rad = spn_rad.value()
            info = PIPE_CATALOG.get(dn, PIPE_CATALOG["DN15"])
            od = info["od"]
            id_ = info["id"]
            if t_idx == 0:
                code = generate_pipe_elbow(bend_angle=ang, bend_radius=rad, od=od, id=id_, dn=dn)
            elif t_idx == 1:
                code = generate_pipe_segment(od=od, id=id_, length=rad * 2.0, dn=dn)
            elif t_idx == 2:
                code = generate_tee_junction(main_od=od, main_id=id_, main_len=rad * 2.5, branch_len=rad, angle=ang)
            elif t_idx == 3:
                code = generate_y_splitter(main_od=od, main_id=id_, split_angle=ang, branch_len=rad)
            elif t_idx == 4:
                code = generate_pipe_flange(pipe_od=od, pipe_id=id_, thickness=8.0, o_ring_groove=True)
            elif t_idx == 5:
                code = generate_hose_barb(hose_id=id_, barb_count=3)
            elif t_idx == 6:
                code = generate_manifold_block(port_count=int(ang) if ang <= 12 else 4)
            else:
                code = generate_pipe_segment(od=od, id=id_, length=rad)
            txt_preview.setText(code)

        cmb_type.currentIndexChanged.connect(lambda _: update_preview())
        cmb_dn.currentIndexChanged.connect(lambda _: update_preview())
        spn_ang.valueChanged.connect(lambda _: update_preview())
        spn_rad.valueChanged.connect(lambda _: update_preview())
        update_preview()

        btn_row = QHBoxLayout()
        btn_load_editor = QPushButton("📥 载入至源码区")
        def load_to_editor():
            if hasattr(self, "txt_scad"):
                self.txt_scad.setPlainText(txt_preview.toPlainText())
                dlg.accept()
                self.log("📥 工业管路代码已载入 OpenSCAD 源码编辑器")
        btn_load_editor.clicked.connect(load_to_editor)
        btn_row.addWidget(btn_load_editor)

        btn_copy = QPushButton("📋 复制源码")
        def copy_code():
            QApplication.clipboard().setText(txt_preview.toPlainText())
            QMessageBox.information(dlg, "已复制", "管路 OpenSCAD 代码已成功复制到剪贴板！")
        btn_copy.clicked.connect(copy_code)
        btn_row.addWidget(btn_copy)

        d_lay.addLayout(btn_row)

        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bbox.rejected.connect(dlg.reject)
        d_lay.addWidget(bbox)
        dlg.exec()

    def on_open_sheetmetal_dialog(self) -> None:
        """打开钣金折弯三维建模与 DXF/SVG 展开下料配置对话框。"""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog
        from openscad_agent.sheetmetal import (
            SHEET_METAL_MATERIALS,
            create_sheet_metal_preset,
            unfold_sheet_metal,
        )

        dlg = QDialog(self)
        dlg.setWindowTitle("📐 钣金折弯与展开下料引擎 (K-Factor & DXF/SVG)")
        dlg.resize(560, 520)
        d_lay = QVBoxLayout(dlg)

        form_lay = QGridLayout()
        form_lay.addWidget(QLabel("构型预设:"), 0, 0)
        cmb_preset = QComboBox()
        cmb_preset.addItems(["U 型折弯槽架 (u_channel)", "L 型安装支架 (l_bracket)", "四周翻边托盘 (enclosure_tray)"])
        form_lay.addWidget(cmb_preset, 0, 1)

        form_lay.addWidget(QLabel("钣金材料:"), 1, 0)
        cmb_mat = QComboBox()
        mat_keys = list(SHEET_METAL_MATERIALS.keys())
        cmb_mat.addItems([f"{k} - {SHEET_METAL_MATERIALS[k]['name']} (K={SHEET_METAL_MATERIALS[k]['k_factor']})" for k in mat_keys])
        form_lay.addWidget(cmb_mat, 1, 1)

        form_lay.addWidget(QLabel("基板宽 W (mm):"), 2, 0)
        spn_w = QDoubleSpinBox()
        spn_w.setRange(15.0, 500.0)
        spn_w.setValue(80.0)
        form_lay.addWidget(spn_w, 2, 1)

        form_lay.addWidget(QLabel("基板高 H (mm):"), 3, 0)
        spn_h = QDoubleSpinBox()
        spn_h.setRange(15.0, 500.0)
        spn_h.setValue(60.0)
        form_lay.addWidget(spn_h, 3, 1)

        form_lay.addWidget(QLabel("折弯翻边 F (mm):"), 4, 0)
        spn_fl = QDoubleSpinBox()
        spn_fl.setRange(5.0, 200.0)
        spn_fl.setValue(25.0)
        form_lay.addWidget(spn_fl, 4, 1)

        form_lay.addWidget(QLabel("板材厚度 T (mm):"), 5, 0)
        spn_t = QDoubleSpinBox()
        spn_t.setRange(0.5, 10.0)
        spn_t.setValue(1.5)
        spn_t.setSingleStep(0.5)
        form_lay.addWidget(spn_t, 5, 1)

        d_lay.addLayout(form_lay)

        lbl_summary = QLabel("展开计算结果将在此显示…")
        lbl_summary.setStyleSheet("font-weight: 600; color: #0f766e; padding: 6px; background: #f0fdfa; border-radius: 4px;")
        d_lay.addWidget(lbl_summary)

        txt_preview = QTextEdit()
        txt_preview.setFontFamily("Monospace")
        txt_preview.setFontPointSize(9)
        d_lay.addWidget(txt_preview)

        state = {"flat": None}

        def update_sheetmetal():
            p_idx = cmb_preset.currentIndex()
            preset_name = ["u_channel", "l_bracket", "enclosure_tray"][p_idx]
            mat_key = mat_keys[cmb_mat.currentIndex()]
            part = create_sheet_metal_preset(
                preset_name=preset_name,
                width=spn_w.value(),
                height=spn_h.value(),
                flange_len=spn_fl.value(),
                thickness=spn_t.value(),
                material=mat_key,
            )
            flat = unfold_sheet_metal(part)
            state["flat"] = flat
            lbl_summary.setText(
                f"展开毛坯尺寸: {flat.flat_width:.2f} × {flat.flat_height:.2f} mm | "
                f"面积: {flat.blank_area_mm2:.1f} mm² | 估算重量: {flat.estimated_mass_g:.1f} g | "
                f"折弯数: {len(flat.bend_lines)}"
            )
            txt_preview.setText(flat.scad_3d)

        for w_ctrl in (cmb_preset, cmb_mat):
            w_ctrl.currentIndexChanged.connect(lambda _: update_sheetmetal())
        for s_ctrl in (spn_w, spn_h, spn_fl, spn_t):
            s_ctrl.valueChanged.connect(lambda _: update_sheetmetal())
        update_sheetmetal()

        btn_row = QHBoxLayout()
        btn_save_dxf = QPushButton("💾 导出切割 DXF")
        def save_dxf():
            if not state["flat"]:
                return
            out_f, _ = QFileDialog.getSaveFileName(dlg, "导出激光切割 DXF 下料图", "sheetmetal_flat.dxf", "DXF Files (*.dxf)")
            if out_f:
                Path(out_f).write_text(state["flat"].dxf_content, encoding="utf-8")
                QMessageBox.information(dlg, "导出成功", f"已导出分层 DXF 下料图 (含 CUT / BEND 图层):\n{out_f}")
        btn_save_dxf.clicked.connect(save_dxf)
        btn_row.addWidget(btn_save_dxf)

        btn_save_svg = QPushButton("💾 导出展开 SVG")
        def save_svg():
            if not state["flat"]:
                return
            out_f, _ = QFileDialog.getSaveFileName(dlg, "导出矢量工程展开图 SVG", "sheetmetal_flat.svg", "SVG Files (*.svg)")
            if out_f:
                Path(out_f).write_text(state["flat"].svg_content, encoding="utf-8")
                QMessageBox.information(dlg, "导出成功", f"已导出钣金展开工程图纸:\n{out_f}")
        btn_save_svg.clicked.connect(save_svg)
        btn_row.addWidget(btn_save_svg)

        btn_load = QPushButton("📥 载入 3D 源码")
        def load_3d():
            if hasattr(self, "txt_scad") and state["flat"]:
                self.txt_scad.setPlainText(state["flat"].scad_3d)
                dlg.accept()
                self.log("📥 钣金三维折弯源码已载入 OpenSCAD 编辑器")
        btn_load.clicked.connect(load_3d)
        btn_row.addWidget(btn_load)

        d_lay.addLayout(btn_row)
        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bbox.rejected.connect(dlg.reject)
        d_lay.addWidget(bbox)
        dlg.exec()

    def on_open_enclosure_dialog(self) -> None:
        """打开智能电子外壳 PCB 导柱与散热开槽参数化生成器对话框。"""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QCheckBox
        from openscad_agent.enclosure import PCB_TEMPLATES, EnclosureConfig, generate_enclosure_scad

        dlg = QDialog(self)
        dlg.setWindowTitle("🖥️ 电子外壳与 PCB 导柱/I/O 开孔智能生成器")
        dlg.resize(560, 500)
        d_lay = QVBoxLayout(dlg)

        form_lay = QGridLayout()
        form_lay.addWidget(QLabel("PCB 开发板模板:"), 0, 0)
        cmb_tmpl = QComboBox()
        t_keys = list(PCB_TEMPLATES.keys()) + ["CUSTOM_GENERIC"]
        cmb_tmpl.addItems([PCB_TEMPLATES[k]["name"] if k in PCB_TEMPLATES else "自定义通用 PCB (70x50mm)" for k in t_keys])
        form_lay.addWidget(cmb_tmpl, 0, 1)

        form_lay.addWidget(QLabel("散热开槽样式:"), 1, 0)
        cmb_vent = QComboBox()
        v_keys = ["linear_slots", "honeycomb", "circular_grid", "none"]
        cmb_vent.addItems(["条形对流百叶槽 (linear_slots)", "六边形蜂窝散热孔 (honeycomb)", "圆形阵列孔 (circular_grid)", "全封闭外壳 (none)"])
        form_lay.addWidget(cmb_vent, 1, 1)

        form_lay.addWidget(QLabel("外壳壁厚 (mm):"), 2, 0)
        spn_wall = QDoubleSpinBox()
        spn_wall.setRange(1.2, 6.0)
        spn_wall.setValue(2.4)
        spn_wall.setSingleStep(0.2)
        form_lay.addWidget(spn_wall, 2, 1)

        chk_lid = QCheckBox("包含合盖顶盖与止口唇边 (平铺单板排版)")
        chk_lid.setChecked(True)
        form_lay.addWidget(chk_lid, 3, 0, 1, 2)

        d_lay.addLayout(form_lay)

        lbl_info = QLabel("壳体工程统计信息将在此显示…")
        lbl_info.setStyleSheet("font-weight: 600; color: #4f46e5; padding: 6px; background: #eef2ff; border-radius: 4px;")
        d_lay.addWidget(lbl_info)

        txt_preview = QTextEdit()
        txt_preview.setFontFamily("Monospace")
        txt_preview.setFontPointSize(9)
        d_lay.addWidget(txt_preview)

        def update_enclosure():
            cfg = EnclosureConfig(
                template=t_keys[cmb_tmpl.currentIndex()],
                vent_pattern=v_keys[cmb_vent.currentIndex()],
                wall_thickness=spn_wall.value(),
                include_lid=chk_lid.isChecked(),
                split_layout=True,
            )
            code, rep = generate_enclosure_scad(cfg)
            odims = rep["outer_dimensions_mm"]
            lbl_info.setText(
                f"外尺寸: {odims[0]}×{odims[1]}×{odims[2]} mm | "
                f"PCB柱: {rep['standoff_count']}根 | I/O孔: {rep['io_cutouts_count']}处 | "
                f"散热面积: {rep['vent_open_area_mm2']} mm² ({rep['vent_open_ratio_pct']}%)"
            )
            txt_preview.setText(code)

        cmb_tmpl.currentIndexChanged.connect(lambda _: update_enclosure())
        cmb_vent.currentIndexChanged.connect(lambda _: update_enclosure())
        spn_wall.valueChanged.connect(lambda _: update_enclosure())
        chk_lid.stateChanged.connect(lambda _: update_enclosure())
        update_enclosure()

        btn_row = QHBoxLayout()
        btn_load = QPushButton("📥 载入至源码编辑器")
        def load_code():
            if hasattr(self, "txt_scad"):
                self.txt_scad.setPlainText(txt_preview.toPlainText())
                dlg.accept()
                self.log("📥 电子外壳双件式源码已载入 OpenSCAD 编辑器")
        btn_load.clicked.connect(load_code)
        btn_row.addWidget(btn_load)

        btn_copy = QPushButton("📋 复制源码")
        def copy_code():
            QApplication.clipboard().setText(txt_preview.toPlainText())
            QMessageBox.information(dlg, "已复制", "电子外壳 OpenSCAD 源码已复制到剪贴板！")
        btn_copy.clicked.connect(copy_code)
        btn_row.addWidget(btn_copy)

        d_lay.addLayout(btn_row)
        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bbox.rejected.connect(dlg.reject)
        d_lay.addWidget(bbox)
        dlg.exec()

    def on_open_cnc_dfm_dialog(self) -> None:
        """打开机械零件三轴 CNC 铣削可制造性体检与 CAM 工步顾问对话框。"""
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox
        from openscad_agent.cam_dfm import CNC_MATERIALS, check_cnc_machinability, generate_dogbone_pocket_scad

        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成模型。请先完成一次建模。")
            return
        scad_p = Path(self._scad_path)
        cand_spec = scad_p.with_name(f"{scad_p.stem}_spec.json")
        if not cand_spec.exists():
            QMessageBox.information(self, "提示", f"未找到规格文件 ({cand_spec.name})。请先生成模型规格。")
            return

        spec = json.loads(cand_spec.read_text(encoding="utf-8"))
        dlg = QDialog(self)
        dlg.setWindowTitle("⚙️ 三轴 CNC 减材可制造性 (DFM) 与 CAM 工步顾问")
        dlg.resize(600, 520)
        d_lay = QVBoxLayout(dlg)

        form_lay = QGridLayout()
        form_lay.addWidget(QLabel("切削材料:"), 0, 0)
        cmb_mat = QComboBox()
        mat_keys = list(CNC_MATERIALS.keys())
        cmb_mat.addItems([f"{k} - {CNC_MATERIALS[k]['name']}" for k in mat_keys])
        form_lay.addWidget(cmb_mat, 0, 1)

        form_lay.addWidget(QLabel("最小精铣刀径 (mm):"), 1, 0)
        spn_tool = QDoubleSpinBox()
        spn_tool.setRange(1.0, 12.0)
        spn_tool.setValue(3.0)
        spn_tool.setSingleStep(1.0)
        form_lay.addWidget(spn_tool, 1, 1)
        d_lay.addLayout(form_lay)

        txt_rep = QTextEdit()
        txt_rep.setReadOnly(True)
        d_lay.addWidget(txt_rep)

        def refresh_dfm():
            mat_key = mat_keys[cmb_mat.currentIndex()]
            rep = check_cnc_machinability(spec, material=mat_key, min_tool_d=spn_tool.value())
            lines = [
                f"📊 {rep.summary}",
                f"• 建议毛坯包络: {rep.stock_dimensions_mm[0]} × {rep.stock_dimensions_mm[1]} × {rep.stock_dimensions_mm[2]} mm (毛坯 {rep.stock_volume_cm3:.1f} cm³ / 零件 {rep.part_volume_cm3:.1f} cm³)",
                f"• 3 轴直接可加工性: {'✅ 合规' if rep.is_3axis_feasible else '⚠️ 存在不可清角或强风险项'}",
                "",
                "🛠️ 推荐 CAM 工步与参数表:",
            ]
            for op in rep.operations:
                lines.append(
                    f"  {op.step_no}. {op.operation_name}\n"
                    f"     刀具: {op.tool_spec} | 转速: {op.spindle_rpm} RPM | 进给: {op.feed_rate_mm_min} mm/min | 切深: {op.step_down_mm}mm | 耗时: ~{op.est_time_min:.1f}min"
                )
            lines.append("")
            lines.append("🔍 DFM 工艺体检诊断明细:")
            if not rep.issues:
                lines.append("  ✅ 未发现明显不可加工特征，可直接投入 CNC 编程！")
            for iss in rep.issues:
                lines.append(f"  [{iss.severity}] {iss.part_id} ({iss.issue_type}): {iss.description}\n     💡 建议: {iss.recommendation}")
            txt_rep.setPlainText("\n".join(lines))

        cmb_mat.currentIndexChanged.connect(lambda _: refresh_dfm())
        spn_tool.valueChanged.connect(lambda _: refresh_dfm())
        refresh_dfm()

        btn_dog = QPushButton("🦴 复制狗骨头避让槽 (Dogbone) OpenSCAD 模块")
        def copy_dog():
            code = generate_dogbone_pocket_scad(tool_d=spn_tool.value())
            QApplication.clipboard().setText(code)
            QMessageBox.information(dlg, "已复制", "CNC 狗骨头避让内直角模块代码已复制到剪贴板！")
        btn_dog.clicked.connect(copy_dog)
        d_lay.addWidget(btn_dog)

        bbox = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bbox.rejected.connect(dlg.reject)
        d_lay.addWidget(bbox)
        dlg.exec()

    def on_export_library(self) -> None:
        """把当前模型封装导出为标准 OpenSCAD 模块生态库包。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成 OpenSCAD 文件。请先完成一次建模。")
            return
        from openscad_agent.packager import export_scad_library
        try:
            lib_p = export_scad_library(self._scad_path)
            self.log(f"📦 OpenSCAD 模块生态包导出成功: {lib_p.name} (位于 {lib_p.parent.name}/)")
            self._refresh_file_list(Path(self._scad_path).parent)
            QMessageBox.information(
                self, "生态包导出成功",
                f"已生成符合工业标准的 OpenSCAD 可复用模块库:\n{lib_p}\n\n"
                f"包含:\n"
                f"  - 核心生态库: {lib_p.name} (带命名空间隔离与可调入参)\n"
                f"  - 调用示例: example.scad (直接可编译)\n"
                f"  - 接口文档: README.md\n"
                f"  - 完整归档包: {lib_p.stem}_bundle.zip"
            )
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"生态包导出异常: {e}")

    def on_open_workspace(self) -> None:
        """打开浏览器端在线协同 3D WebGL 建模工作台。"""
        out_dir = Path(self.txt_outdir.text().strip() or "out").resolve()
        from openscad_agent.workspace import generate_workspace_html
        try:
            ws_p = generate_workspace_html(out_dir)
            import webbrowser
            webbrowser.open(ws_p.as_uri())
            self.log(f"🌐 已在浏览器中打开在线协同建模工作台: {ws_p.name}")
        except Exception as e:
            QMessageBox.warning(self, "打开工作台失败", f"工作台生成异常: {e}")

    def on_package_zip(self) -> None:
        """把当前模型全套工程资产一键打包为 ZIP 归档。"""
        if not self._scad_path or not Path(self._scad_path).exists():
            QMessageBox.information(self, "提示", "尚未生成 OpenSCAD 文件。请先完成一次建模。")
            return
        from openscad_agent.packager import package_project
        try:
            zip_p = package_project(self._scad_path)
            self.log(f"📦 全套工程资产打包成功: {zip_p.name} (大小: {zip_p.stat().st_size} 字节)")
            self._refresh_file_list(Path(self._scad_path).parent)
            QMessageBox.information(self, "打包成功", f"全套工程资产已打包为:\n{zip_p}\n包含 3D模型、2D图纸、源码、网页查看器与工艺指导书。")
        except Exception as e:
            QMessageBox.warning(self, "打包失败", f"工程打包异常: {e}")

    def on_send_to_printer(self) -> None:
        """一键将切片文件 (G-code, 3MF 或 STL) 推送至局域网 Klipper / OctoPrint / 拓竹 打印机。"""
        if not self._scad_path:
            QMessageBox.information(self, "提示", "尚未生成模型文件。请先完成建模。")
            return
        scad_p = Path(self._scad_path)
        # 优选真实切片 G-code，次选 3MF 现代格式，再次选二进制 STL
        target_f = scad_p.with_suffix(".gcode")
        if not target_f.exists():
            target_f = scad_p.with_suffix(".3mf")
        if not target_f.exists():
            target_f = scad_p.with_suffix(".stl")
        if not target_f.exists():
            QMessageBox.information(self, "提示", "未找到可供打印的 G-code、3MF 或 STL 模型文件。")
            return

        from openscad_agent.core import upload_to_3d_printer
        saved_url = self._settings.value("printer_url", "http://mainsail.local:7125")
        url, ok = QInputDialog.getText(
            self, "推送至 3D 打印机",
            f"请输入局域网 3D 打印机地址 (Klipper / OctoPrint / 拓竹 IP):\n将上传: {target_f.name}",
            QLineEdit.EchoMode.Normal, saved_url
        )
        if not ok or not url.strip():
            return

        url = url.strip()
        self._settings.setValue("printer_url", url)
        self.log(f"正在向打印机推送文件: {url} <- {target_f.name}…")
        success, msg = upload_to_3d_printer(target_f, server_url=url, timeout=10)
        if success:
            QMessageBox.information(self, "推送成功", msg)
            self.log(f"✅ {msg}")
        else:
            QMessageBox.warning(self, "推送未完成", f"打印机推送响应:\n{msg}")
            self.log(f"⚠️ {msg}")

    def on_serve_assets(self) -> None:
        """启动后台 Web 资产服务器并打印局域网访问地址。

        端口持久化: 首次使用默认 8080, 后续调用复用上次使用的端口
        (QSettings key "serve_port")。若端口被占用, 自动寻找空闲端口并提示。
        """
        out_dir = Path(self.txt_outdir.text().strip() or "out").resolve()
        from openscad_agent.server import serve_assets, is_port_in_use
        # 端口持久化: 从 QSettings 读取上次使用的端口, 默认 8080
        saved_port = int(self._settings.value("serve_port", "8080"))
        try:
            if is_port_in_use(saved_port):
                self.log(f"⚠️  端口 {saved_port} 已被占用, 自动寻找空闲端口…")
            local_url, lan_url, port = serve_assets(out_dir, port=saved_port, open_browser=True, blocking=False)
            # 持久化本次实际使用的端口
            if port != saved_port:
                self.log(f"⚠️  端口 {saved_port} 不可用, 已自动切换到 {port}")
            self._settings.setValue("serve_port", str(port))
            self.log(f"📡 局域网 3D 资产服务已在后台就绪:\n   💻 本机: {local_url}\n   📱 手机无线: {lan_url}")
            QMessageBox.information(
                self, "局域网 3D 资产分享已启动",
                f"Web 服务已在后台托管 {out_dir.name}/\n\n"
                f"💻 本机电脑: {local_url}\n"
                f"📱 手机/平板 (同 Wi-Fi): {lan_url}\n\n"
                f"您可直接在手机浏览器中打开该地址无线检视 3D 模型！"
            )
        except Exception as e:
            QMessageBox.warning(self, "启动失败", f"资产服务启动异常: {e}")

    def on_file_clicked(self, item: QListWidgetItem) -> None:
        """单击文件列表项预览内容。"""
        fpath = item.data(Qt.ItemDataRole.UserRole)
        if not fpath:
            return
        p = Path(fpath)
        if not p.exists():
            return
        suffix = p.suffix.lower()
        if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            self._show_preview(str(p), origin_image=self._last_image)
            self.log(f"查看图片: {p.name}")
        elif suffix in (".scad", ".json", ".log"):
            try:
                txt = p.read_text(encoding="utf-8")
                self.txt_scad.setPlainText(txt)
                self.log(f"已载入文件内容: {p.name}")
            except Exception as e:
                self.log(f"无法读取文件 {p.name}: {e}")
        elif suffix == ".html":
            self.log(f"3D 网页交互查看器: {p.name} (双击直接在浏览器中交互检视)")

    def on_file_double_clicked(self, item: QListWidgetItem) -> None:
        """双击文件列表项调起系统默认程序打开。"""
        fpath = item.data(Qt.ItemDataRole.UserRole)
        if fpath and Path(fpath).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(fpath)))
            self.log(f"调用系统默认程序打开: {Path(fpath).name}")

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        """拖入图片文件时显示允许放置。"""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                ext = Path(url.toLocalFile()).suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        """释放拖入的图片：自动填入路径（支持多图多选）并立即刷新原图展示。"""
        img_paths = []
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                fpath = url.toLocalFile()
                ext = Path(fpath).suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
                    img_paths.append(fpath)
        if img_paths:
            combined = "; ".join(img_paths)
            self.image_path.setText(combined)
            self._last_image = img_paths[0]
            self.log(f"已拖入 {len(img_paths)} 张参考图片: {', '.join(Path(p).name for p in img_paths)}")
            pm = QPixmap(img_paths[0])
            if not pm.isNull():
                scaled = pm.scaled(
                    self.lbl_origin.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.lbl_origin.setPixmap(scaled)
                self.lbl_origin.setStyleSheet("QLabel { border: 2px solid #16a34a; border-radius: 8px; }")
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def on_rerender_scad(self) -> None:
        """在线微调 OpenSCAD 源码后，一键重新渲染 3D 模型与预览图。"""
        src = self.txt_scad.toPlainText().strip()
        if not src:
            QMessageBox.warning(self, "提示", "源码为空，无法渲染。")
            return

        outdir = Path(self.txt_outdir.text().strip() or "out")
        outdir.mkdir(parents=True, exist_ok=True)
        if self._scad_path:
            scad_path = Path(self._scad_path)
        else:
            scad_path = outdir / "custom_model.scad"
            self._scad_path = str(scad_path)

        scad_path.write_text(src, encoding="utf-8")
        self.log(f"⚡ 源码已保存至: {scad_path.name}，开始重新编译渲染…")

        fmt = self.cmb_format.currentText()
        config = AgentConfig(
            output_dir=str(outdir),
            export_format=fmt,
            render_precision=self.cmb_precision.currentText(),
        )
        renderer = OpenscadRenderer(config)

        # 语法校验
        ok, msg = renderer.validate(scad_path)
        if not ok:
            import re
            m = re.search(r", line (\d+)", msg) or re.search(r"line (\d+)", msg)
            if m:
                err_line = int(m.group(1))
                block = self.txt_scad.document().findBlockByNumber(err_line - 1)
                if block.isValid():
                    cursor = QTextCursor(block)
                    self.txt_scad.setTextCursor(cursor)
                    self.txt_scad.centerCursor()
                    sel = QTextEdit.ExtraSelection()
                    sel.format.setBackground(QColor(239, 68, 68, 90))  # 醒目半透明红
                    sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                    sel.cursor = cursor
                    self.txt_scad.setExtraSelections([sel])
                    self.log(f"⚠️ 语法错误已自动定位并高亮至第 {err_line} 行")
            QMessageBox.warning(self, "编译失败", f"OpenSCAD 语法或编译错误:\n{msg[:500]}")
            self.log(f"❌ 重新编译失败: {msg[:300]}")
            return
        else:
            self.txt_scad.setExtraSelections([])

        # 导出 3D 模型
        if self.chk_render.isChecked():
            model_file = scad_path.with_suffix("." + fmt)
            try:
                renderer.export_model(scad_path, model_file)
                self.log(f"已重新导出模型: {model_file.name}")
            except Exception as e:
                self.log(f"导出模型异常: {e}")

        # 渲染预览图
        preview_png = scad_path.with_name(f"{scad_path.stem}_preview.png")
        try:
            renderer.render_preview(scad_path, preview_png)
            self._current_preview_png = str(preview_png)
            self._show_preview(str(preview_png), origin_image=self._last_image)
            self.log(f"✅ 重新渲染预览图成功: {preview_png.name}")
            # 同步渲染多视角 (top/front/iso)，确保视角切换按钮即刻可用
            renderer.render_multi_view(
                scad_path, preview_png.with_name(f"{scad_path.stem}_preview_v"),
                views=OpenscadRenderer.VIEWS,
                size=(500, 400),
            )
        except Exception as e:
            self.log(f"渲染预览图异常: {e}")

        self._refresh_file_list(outdir)

    def on_save_snapshot(self) -> None:
        """为当前 OpenSCAD 源码保存一个版本快照文件 (_v1.scad, _v2.scad...)。"""
        src = self.txt_scad.toPlainText().strip()
        if not src:
            QMessageBox.warning(self, "提示", "源码为空，无法保存快照。")
            return
        out_dir = Path(self.txt_outdir.text().strip() or "out")
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(self._scad_path).stem if self._scad_path else "model"
        base_stem = stem.split("_v")[0]

        existing = sorted(out_dir.glob(f"{base_stem}_v*.scad"))
        next_v = len(existing) + 1
        snap_file = out_dir / f"{base_stem}_v{next_v}.scad"
        snap_file.write_text(src, encoding="utf-8")
        self.log(f"📸 已成功保存版本快照: {snap_file.name}")
        self._refresh_file_list(out_dir)

    def on_rollback_snapshot(self) -> None:
        """从历史版本快照中选择并一键回滚载入。"""
        out_dir = Path(self.txt_outdir.text().strip() or "out")
        stem = Path(self._scad_path).stem if self._scad_path else "model"
        base_stem = stem.split("_v")[0]

        snapshots = sorted(out_dir.glob(f"{base_stem}_v*.scad"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not snapshots:
            snapshots = sorted(out_dir.glob("*_v*.scad"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not snapshots:
            QMessageBox.information(self, "提示", "尚未保存任何版本快照。请先点击「保存快照」。")
            return

        items = [f"{s.name} ({datetime.datetime.fromtimestamp(s.stat().st_mtime).strftime('%H:%M:%S')}, {len(s.read_text('utf-8').splitlines())}行)" for s in snapshots]
        chosen, ok = QInputDialog.getItem(self, "回滚源码快照", "请选择要回滚恢复的历史快照版本:", items, 0, False)
        if ok and chosen:
            idx = items.index(chosen)
            target_snap = snapshots[idx]
            txt = target_snap.read_text(encoding="utf-8")
            self.txt_scad.setPlainText(txt)
            self.log(f"⏪ 已回滚载入快照: {target_snap.name}")

    # ------------------------------------------------------------------
    # UI 辅助
    # ------------------------------------------------------------------

    def log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)

    def _populate_spec_tree(self, spec: dict) -> None:
        self.spec_tree.clear()
        self._fill_tree(self.spec_tree.invisibleRootItem(), spec)

    def _fill_tree(self, parent: QTreeWidgetItem, obj) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                item = QTreeWidgetItem([str(k), self._scalar(v)])
                parent.addChild(item)
                if isinstance(v, (dict, list)):
                    self._fill_tree(item, v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                item = QTreeWidgetItem([str(i), self._scalar(v)])
                parent.addChild(item)
                if isinstance(v, (dict, list)):
                    self._fill_tree(item, v)

    @staticmethod
    def _scalar(v) -> str:
        if isinstance(v, (dict, list)):
            return ""  # 容器由子节点展示
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    def _show_preview(self, png_path: str, origin_image: Optional[str] = None) -> None:
        """预览图 + 可选原图并排对比。"""
        pm = QPixmap(png_path)
        if not pm.isNull():
            scaled = pm.scaled(
                self.lbl_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.lbl_preview.setPixmap(scaled)
            self.lbl_preview.setStyleSheet("QLabel { border: 2px solid #2563eb; border-radius: 8px; }")
        self.log(f"预览图已加载: {png_path}")
        # 原图对比
        if origin_image and Path(origin_image).exists():
            opm = QPixmap(origin_image)
            if not opm.isNull():
                oscaled = opm.scaled(
                    self.lbl_origin.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.lbl_origin.setPixmap(oscaled)
                self.lbl_origin.setStyleSheet("QLabel { border: 2px solid #16a34a; border-radius: 8px; }")
                self.log(f"原图已加载: {origin_image}")
        else:
            self.lbl_origin.clear()
            self.lbl_origin.setText("（无原图）")

    def _clear_compare(self) -> None:
        self.lbl_origin.clear()
        self.lbl_origin.setText("（原图）")
        self.lbl_origin.setStyleSheet("QLabel { border: 2px dashed #cbd5e1; border-radius: 8px; }")
        self.lbl_preview.clear()
        self.lbl_preview.setText("（预览）")
        self.lbl_preview.setStyleSheet("QLabel { border: 2px dashed #cbd5e1; border-radius: 8px; }")

    def on_project_clicked(self, item: QListWidgetItem) -> None:
        """单击历史工程：全景回溯加载该工程的规格、源码、预览与产物。"""
        sf_path = item.data(Qt.ItemDataRole.UserRole)
        if not sf_path:
            return
        sf = Path(sf_path)
        if not sf.exists():
            return
        try:
            spec = json.loads(sf.read_text(encoding="utf-8"))
            self._populate_spec_tree(spec)
        except Exception:
            spec = None

        name = spec.get("name") if spec else sf.stem.replace("_spec", "")
        out_dir = sf.parent

        scad_cand = out_dir / f"{name}.scad"
        if not scad_cand.exists():
            scad_cand = out_dir / f"{sf.stem.replace('_spec','')}.scad"
        if scad_cand.exists():
            self._scad_path = str(scad_cand)
            try:
                self.txt_scad.setPlainText(scad_cand.read_text(encoding="utf-8"))
            except Exception:
                pass

        prev_cand = out_dir / f"{name}_preview.png"
        if not prev_cand.exists():
            prev_cand = out_dir / f"{sf.stem.replace('_spec','')}_preview.png"
        if prev_cand.exists():
            self._current_preview_png = str(prev_cand)
            self._show_preview(str(prev_cand), origin_image=None)

        cand_html = out_dir / f"{name}_viewer.html"
        if not cand_html.exists():
            cand_html = out_dir / f"{sf.stem.replace('_spec','')}_viewer.html"
        if cand_html.exists():
            self._current_viewer_html = str(cand_html)
            if self.preview_stack.currentIndex() == 1:
                self._load_3d_viewer(str(cand_html))

        self.log(f"📚 已全景回溯载入历史工程: {name}")

    def on_project_double_clicked(self, item: QListWidgetItem) -> None:
        """双击历史工程：优先调起浏览器 3D 网页查看器，无查看器则在 OpenSCAD 中打开。"""
        sf_path = item.data(Qt.ItemDataRole.UserRole)
        if not sf_path:
            return
        sf = Path(sf_path)
        base = sf.stem.replace("_spec", "")
        out_dir = sf.parent
        html = out_dir / f"{base}_viewer.html"
        if html.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(html)))
            self.log(f"已在浏览器中交互检视: {html.name}")
            return
        scad = out_dir / f"{base}.scad"
        if scad.exists():
            subprocess.Popen(["openscad", str(scad)])
            self.log(f"已在 OpenSCAD 中打开: {scad.name}")

    def _refresh_projects(self, out_dir: Path) -> None:
        """扫描输出目录下的所有 *_spec.json，解析为结构化历史工程列表。"""
        self.lst_projects.clear()
        if not out_dir.exists():
            return
        from openscad_agent.doctor import check_printability

        spec_files = sorted(out_dir.glob("*_spec.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        for sf in spec_files:
            try:
                spec = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                continue
            name = spec.get("name") or sf.stem.replace("_spec", "")
            desc = spec.get("description", "")
            mtime = datetime.datetime.fromtimestamp(sf.stat().st_mtime).strftime("%m-%d %H:%M")

            doc = check_printability(spec)
            score_badge = "✅" if doc.score >= 90 else ("⚠️" if doc.score >= 70 else "❌")
            dim_str = f"{doc.bounding_box[0]}×{doc.bounding_box[1]}×{doc.bounding_box[2]}mm"
            parts_cnt = len(spec.get("parts", []))

            has_stl = (out_dir / f"{name}.stl").exists() or (out_dir / f"{sf.stem.replace('_spec','')}.stl").exists()
            has_view = (out_dir / f"{name}_viewer.html").exists() or (out_dir / f"{sf.stem.replace('_spec','')}_viewer.html").exists()
            badges = []
            if has_stl:
                badges.append("3D")
            if has_view:
                badges.append("Web")
            badge_str = f"[{'/'.join(badges)}]" if badges else ""

            label = f"{score_badge} {name}  {badge_str}  ({mtime})  {parts_cnt}部件 | {dim_str}"
            if desc:
                label += f"\n   ↳ {desc[:40]}"

            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(sf))
            self.lst_projects.addItem(item)

    def _refresh_file_list(self, out_dir: Path) -> None:
        self.lst_files.clear()
        self._refresh_projects(out_dir)
        if not out_dir.exists():
            return
        for f in sorted(out_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in (
                ".scad", ".stl", ".obj", ".3mf", ".csg", ".amf", ".png", ".json", ".html", ".dxf", ".svg", ".zip", ".md"
            ):
                item = QListWidgetItem(f.name)
                item.setData(Qt.ItemDataRole.UserRole, str(f))
                self.lst_files.addItem(item)

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self._refresh_file_list(Path(self.txt_outdir.text().strip() or "out"))

    # ------------------------------------------------------------------
    # 设置持久化
    # ------------------------------------------------------------------

    def _connect_settings_signals(self) -> None:
        """参数变更即时持久化: 文本/数值/选择控件变更即写 QSettings，避免关闭窗口前未保存。"""
        text_widgets = (self.txt_model, self.txt_base_url, self.txt_outdir)
        for w in text_widgets:
            w.textChanged.connect(lambda _text, w=w: self._settings.setValue(
                {self.txt_model: "model", self.txt_base_url: "base_url", self.txt_outdir: "out_dir"}[w],
                w.text().strip()
            ))
        spin_widgets = {
            self.spn_temperature: "temperature",
            self.spn_max_side: "max_side",
            self.spn_fix_rounds: "fix_rounds",
            self.spn_review_rounds: "review_rounds",
        }
        for w, key in spin_widgets.items():
            w.valueChanged.connect(lambda _v, k=key: self._settings.setValue(k, str(w.value())))
        combo_widgets = {
            self.cmb_format: "format",
            self.cmb_precision: "precision",
            self.cmb_lang: "language",
        }
        for w, key in combo_widgets.items():
            w.currentTextChanged.connect(lambda _text, w=w, k=key: self._settings.setValue(
                k, "en" if k == "language" and "English" in _text else _text
            ))
        chk_widgets = {
            self.chk_render: "chk_render",
            self.chk_review: "chk_review",
            self.chk_edge_map: "chk_edge_map",
            self.chk_3mf: "chk_3mf",
            self.chk_2d: "chk_2d",
        }
        for w, key in chk_widgets.items():
            w.toggled.connect(lambda _checked, k=key: self._settings.setValue(k, str(w.isChecked())))

    def _load_settings(self) -> None:
        s = self._settings
        if s.contains("model"):
            self.txt_model.setText(s.value("model", "agnes-3.0-flash"))
        if s.contains("base_url"):
            self.txt_base_url.setText(s.value("base_url", "https://apihub.agnes-ai.com"))
        if s.contains("temperature"):
            self.spn_temperature.setValue(float(s.value("temperature", "0.2")))
        if s.contains("format"):
            idx = self.cmb_format.findText(s.value("format", "stl"))
            if idx >= 0:
                self.cmb_format.setCurrentIndex(idx)
        if s.contains("precision"):
            idx = self.cmb_precision.findText(s.value("precision", "High"))
            if idx >= 0:
                self.cmb_precision.setCurrentIndex(idx)
        if s.contains("out_dir"):
            self.txt_outdir.setText(s.value("out_dir", "out"))
        if s.contains("max_side"):
            self.spn_max_side.setValue(int(s.value("max_side", "2048")))
        if s.contains("fix_rounds"):
            self.spn_fix_rounds.setValue(int(s.value("fix_rounds", "3")))
        if s.contains("review_rounds"):
            self.spn_review_rounds.setValue(int(s.value("review_rounds", "3")))
        if s.contains("chk_render"):
            self.chk_render.setChecked(s.value("chk_render", True).lower() != "false")
        if s.contains("chk_review"):
            self.chk_review.setChecked(s.value("chk_review", True).lower() != "false")
        if s.contains("chk_edge_map"):
            self.chk_edge_map.setChecked(s.value("chk_edge_map", True).lower() != "false")
        if s.contains("chk_3mf"):
            self.chk_3mf.setChecked(s.value("chk_3mf", True).lower() != "false")
        if s.contains("chk_2d"):
            self.chk_2d.setChecked(s.value("chk_2d", False).lower() != "false")
        if s.contains("window_geometry"):
            self.restoreGeometry(s.value("window_geometry"))
        if s.contains("language") and s.value("language") == "en":
            self.cmb_lang.setCurrentIndex(1)
        if s.contains("splitter_sizes") and hasattr(self, "main_splitter"):
            try:
                sizes = [int(x) for x in s.value("splitter_sizes")]
                if len(sizes) == 3 and all(x > 0 for x in sizes):
                    self.main_splitter.setSizes(sizes)
            except Exception:
                pass

    def _save_settings(self) -> None:
        s = self._settings
        s.setValue("model", self.txt_model.text().strip())
        s.setValue("base_url", self.txt_base_url.text().strip())
        s.setValue("temperature", str(self.spn_temperature.value()))
        s.setValue("format", self.cmb_format.currentText())
        s.setValue("precision", self.cmb_precision.currentText())
        s.setValue("out_dir", self.txt_outdir.text().strip())
        s.setValue("max_side", str(self.spn_max_side.value()))
        s.setValue("fix_rounds", str(self.spn_fix_rounds.value()))
        s.setValue("review_rounds", str(self.spn_review_rounds.value()))
        s.setValue("chk_render", str(self.chk_render.isChecked()))
        s.setValue("chk_review", str(self.chk_review.isChecked()))
        s.setValue("chk_edge_map", str(self.chk_edge_map.isChecked()))
        s.setValue("chk_3mf", str(self.chk_3mf.isChecked()))
        s.setValue("chk_2d", str(self.chk_2d.isChecked()))
        if hasattr(self, "main_splitter"):
            s.setValue("splitter_sizes", self.main_splitter.sizes())
        s.setValue("language", "en" if "English" in self.cmb_lang.currentText() else "zh")
        s.setValue("window_geometry", self.saveGeometry())
        s.sync()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_settings()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    # fcitx5 输入法支持：PyQt6 自带的 Qt 库没有 fcitx5 输入上下文插件，
    # 需要指向系统 fcitx5-qt 插件目录（Qt 6.11 与 PyQt6 6.11 版本匹配）
    import os
    os.environ.setdefault("QT_IM_MODULE", "fcitx5")
    os.environ.setdefault("QT_PLUGIN_PATH", "/usr/lib/qt6/plugins")

    # 日志落盘（控制台 + out/agent.log）；GUI 面板另行订阅信号，不依赖 logging
    from openscad_agent.log import setup_logging
    setup_logging(log_file="out/agent.log")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
