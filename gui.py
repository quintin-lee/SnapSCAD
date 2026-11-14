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

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, Qt, pyqtSignal, QSettings, QUrl
from PyQt6.QtGui import QPixmap, QFont, QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QTextEdit, QComboBox, QGroupBox,
    QFileDialog, QSplitter, QScrollArea, QTreeWidget, QTreeWidgetItem,
    QPlainTextEdit, QMessageBox, QProgressBar, QFrame, QSizePolicy,
    QListWidget, QListWidgetItem, QSpinBox, QDoubleSpinBox, QTabWidget,
)

from openscad_agent import AgentConfig, OpenscadAgent, OpenscadRenderer
from openscad_agent.vision import AgnesVision, SpecParseError


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

    def run(self) -> None:
        try:
            img_desc = (", ".join(Path(p).name for p in self.image) if isinstance(self.image, (list, tuple))
                        else (Path(self.image).name if self.image else "-"))
            self.log_signal.emit(f"开始建模: image={img_desc} desc={self.description or '-'} review={self.review}")
            if self.image:
                result = self.agent.build_from_image(self.image, hint=self.hint, review=self.review)
            else:
                result = self.agent.build_from_description(self.description)
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
    SETTINGS_ORG = "openscad_agent"
    SETTINGS_APP = "openscad_agent"

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OpenSCAD Agent - 图片→3D模型")
        self.resize(1200, 800)
        self._worker: Optional[BuildWorker] = None
        self._scad_path: Optional[str] = None
        self._last_image: Optional[str] = None
        self._last_desc: str = ""
        self._current_preview_png: Optional[str] = None
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
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左：输入区（图片选择 / 文字描述 / 补充说明 / 参数设置）
        left = self._build_input_panel()
        splitter.addWidget(left)

        # 中：规格 + 流程
        mid = self._build_spec_panel()
        splitter.addWidget(mid)

        # 右：预览
        right = self._build_preview_panel()
        splitter.addWidget(right)

        splitter.setSizes([320, 480, 420])
        root.addWidget(splitter, stretch=1)

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
            "font-size: 14px; border-radius: 6px; }"
            "QPushButton:hover { background-color: #1d4ed8; }"
            "QPushButton:disabled { background-color: #cbd5e1; }"
        )
        self.btn_build.clicked.connect(self.on_build)
        lay.addWidget(self.btn_build)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(10)
        self.progress.setMaximum(100)
        lay.addWidget(self.progress, stretch=1)

        self.lbl_status = QLabel("空闲")
        lay.addWidget(self.lbl_status)
        return w

    def _build_input_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)

        # 输入模式切换
        mode_box = QGroupBox("输入源")
        mode_lay = QVBoxLayout(mode_box)
        self.image_path = QLineEdit()
        self.image_path.setPlaceholderText("选择图片…")
        btn_img = QPushButton("选择图片")
        btn_img.clicked.connect(self.on_pick_image)
        mode_lay.addWidget(self.image_path)
        mode_lay.addWidget(btn_img)

        self.txt_desc = QTextEdit()
        self.txt_desc.setPlaceholderText("或输入文字描述（无图片场景）")
        self.txt_desc.setFixedHeight(80)
        mode_lay.addWidget(self.txt_desc)

        self.txt_hint = QLineEdit()
        self.txt_hint.setPlaceholderText("补充说明（可选，帮助理解物体）")
        mode_lay.addWidget(self.txt_hint)
        lay.addWidget(mode_box)

        # 参数
        param_box = QGroupBox("参数")
        param_lay = QVBoxLayout(param_box)

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

        lay.addWidget(param_box)

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
        lay.addWidget(tree, stretch=1)

        scad_bar = QHBoxLayout()
        scad_bar.addWidget(QLabel("<b>OpenSCAD 源码 (支持在线微调)</b>"))
        scad_bar.addStretch(1)
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
        self.txt_scad.setStyleSheet("font-family: 'Monospace', monospace;")
        self.txt_scad.setFixedHeight(220)
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
        for btn in (self.btn_view_persp, self.btn_view_top, self.btn_view_front, self.btn_view_iso):
            btn.setFixedHeight(26)
            view_bar.addWidget(btn)
        view_bar.addStretch(1)
        lay.addLayout(view_bar)

        self.btn_view_persp.clicked.connect(lambda: self.on_switch_view("perspective"))
        self.btn_view_top.clicked.connect(lambda: self.on_switch_view("top"))
        self.btn_view_front.clicked.connect(lambda: self.on_switch_view("front"))
        self.btn_view_iso.clicked.connect(lambda: self.on_switch_view("iso"))

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
        cmp_split.addWidget(self.lbl_preview)

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

        self.btn_open_dir = QPushButton("📁 打开输出目录")
        self.btn_open_dir.clicked.connect(self.on_open_output_dir)
        act_bar.addWidget(self.btn_open_dir)
        lay.addLayout(act_bar)

        lay.addWidget(QLabel("<b>输出文件 (单击预览 / 双击打开)</b>", wordWrap=True))
        self.lst_files = QListWidget()
        self.lst_files.itemClicked.connect(self.on_file_clicked)
        self.lst_files.itemDoubleClicked.connect(self.on_file_double_clicked)
        lay.addWidget(self.lst_files, stretch=1)
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
        )
        # 把审查/修复轮数写入 agent
        config_api_key = config.api_key
        if not config_api_key:
            QMessageBox.warning(
                self, "缺少 API Key",
                "未检测到 Agnes API key。\n"
                "请设置环境变量 AGNES_API_KEY 后重启本程序。",
            )
            return

        agent = OpenscadAgent(config)
        # 所有可变 agent 参数在主线程一次性写入，worker 只读，避免竞态
        agent.max_fix_rounds = self.spn_fix_rounds.value()
        agent.max_review_rounds = self.spn_review_rounds.value()
        agent.enable_multi_view = self.chk_review.isChecked()  # 审查开启时才做多视角
        review = self.chk_review.isChecked() and bool(image_input)  # 仅图片场景可审查（需原图比对）
        self._worker = BuildWorker(agent, image_input, desc, hint, review=review, parent=self)
        self._worker.log_signal.connect(self.log)
        self._worker.done_signal.connect(self.on_done)
        self._worker.error_signal.connect(self.on_error)

        # 保存本次原图供对比展示，并清空旧预览
        self._last_image = raw_imgs[0] if raw_imgs else None
        self._last_desc = desc
        self._clear_compare()

        self.btn_build.setEnabled(False)
        self.lbl_status.setText("运行中…")
        self.progress.setRange(0, 0)  # 不定进度
        self.log("启动后台任务…")
        self._worker.start()
        self._save_settings()

    def on_done(self, result) -> None:
        self.btn_build.setEnabled(True)
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

        if result.success:
            self.log("✅ 建模成功")
            if result.model_file:
                self.log(f"3D 模型: {result.model_file}")
            # 审查闭环结论
            rv = getattr(result, "review", None)
            rr = getattr(result, "review_rounds", 0)
            if rv:
                self.log(f"🔍 审查闭环: {rr} 轮 | match={rv.get('match')} score={rv.get('score')}")
                if rv.get("verdict"):
                    self.log(f"   结论: {rv.get('verdict')}")
                for iss in (rv.get("issues") or [])[:5]:
                    self.log(f"   问题: {iss}")
            elif rr:
                self.log(f"🔍 审查闭环: {rr} 轮（已收敛）")
        else:
            self.log(f"❌ 失败: {'; '.join(result.errors)}")

    def on_error(self, msg: str) -> None:
        self.btn_build.setEnabled(True)
        self.progress.setRange(0, 100)
        self.lbl_status.setText("错误")
        self.log(f"❌ 错误: {msg}")

    def on_switch_view(self, view_name: str) -> None:
        """切换预览视角显示。"""
        if not self._current_preview_png:
            self.log("提示: 尚未渲染预览图")
            return
        p = Path(self._current_preview_png)
        # 查找对应视角图片 (例如 xxx_preview_v_top.png)
        candidates = []
        if view_name == "perspective":
            candidates = [p, p.with_name(f"{p.stem}_v_perspective.png")]
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
        self.log(f"提示: 未找到 {view_name} 视角图（可能未渲染多视角）")

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
            QMessageBox.warning(self, "编译失败", f"OpenSCAD 语法或编译错误:\n{msg[:500]}")
            self.log(f"❌ 重新编译失败: {msg[:300]}")
            return

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
        except Exception as e:
            self.log(f"渲染预览图异常: {e}")

        self._refresh_file_list(outdir)

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

    def _refresh_file_list(self, out_dir: Path) -> None:
        self.lst_files.clear()
        if not out_dir.exists():
            return
        for f in sorted(out_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in (
                ".scad", ".stl", ".obj", ".3mf", ".csg", ".amf", ".png", ".json", ".html"
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
        """任何参数变更时自动保存。"""
        for widget in (
            self.txt_model, self.txt_base_url, self.txt_outdir,
            self.spn_temperature, self.spn_max_side,
            self.spn_fix_rounds, self.spn_review_rounds,
        ):
            widget.textChanged if hasattr(widget, "textChanged") else widget.valueChanged
        # 简单策略：窗口关闭时保存

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
