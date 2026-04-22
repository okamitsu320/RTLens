from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import warnings
from urllib.parse import parse_qs, urlparse, unquote
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Set

from .connectivity import build_connectivity, build_hierarchy, query_signal
from .callable_resolver import (
    resolve_callable_key_any_site,
    resolve_callable_key_for_definition_site,
    resolve_callable_key_from_site,
    token_variants,
)
from .editor_cmd import build_editor_argv
from .editor_config import EDITOR_PRESETS, detect_available_presets, load_editor_template, save_editor_template
from .model import ConnectivityDB, DesignDB, ModuleDef
from .netlistsvg_view import (
    NetlistSvgResult,
    dedupe_existing_files_canonical,
    generate_netlistsvg_prebuild_batch,
)
from .netlistsvg_svg import (
    _inject_svg_data_src_from_json,
    _inline_svg_styles_for_qt,
)
from .rtl_debug import is_rtl_debug_enabled, run_rtl_debug_pipeline
from .rtl_elk_render import render_elk_layout
from .rtl_structure import (
    benchmark_rtl_structure_elk_graph,
    build_rtl_structure_render,
    estimate_rtl_structure_timeout,
    profile_rtl_structure_elk_graph,
    build_rtl_structure_elk_graph,
)
from .slang_backend import SlangBackendError, load_design_with_slang
from .sv_parser import discover_sv_files, parse_sv_files, read_filelist_with_args
from .qt_text_utils import (
    canonical_schematic_name as _canonical_schematic_name_text,
    classify_schematic_cell_type as _classify_schematic_cell_type_text,
    cleanup_wave_name as _cleanup_wave_name_text,
    demangle_paramod_module_name as _demangle_paramod_module_name_text,
    extract_wave_name_candidates as _extract_wave_name_candidates_text,
    normalize_schematic_src as _normalize_schematic_src_text,
    parse_jump_item as _parse_jump_item_text,
)
from .wave import WaveDB, load_wave
from .wave_bridge import WaveBridgeError, create_wave_bridge

try:
    from PySide6.QtCore import QByteArray, QObject, QPointF, QTimer, Qt, QUrl, Slot
    from PySide6.QtGui import QAction, QColor, QDesktopServices, QImage, QPainter, QPainterPath, QPen, QPixmap, QTextCursor, QBrush, QPolygonF, QKeySequence
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import (
        QAbstractScrollArea,
        QApplication,
        QCheckBox,
        QComboBox,
        QColorDialog,
        QDialog,
        QFileDialog,
        QInputDialog,
        QGraphicsScene,
        QGraphicsView,
        QGraphicsRectItem,
        QGraphicsSimpleTextItem,
        QGraphicsPathItem,
        QGraphicsItem,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QLabel,
        QMainWindow,
        QMessageBox,
        QMenu,
        QPlainTextEdit,
        QPushButton,
        QScrollArea,
        QSplitter,
        QTabWidget,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
        QSizePolicy,
        QTextEdit,
    )
except ModuleNotFoundError:
    QAbstractScrollArea = object
    QApplication = None
    Qt = object
    QTimer = object
    QObject = object
    QPointF = object
    QByteArray = object
    QUrl = object
    Slot = lambda *args, **kwargs: (lambda fn: fn)
    QTextCursor = object
    QDesktopServices = object
    QColor = object
    QImage = object
    QPainter = object
    QPainterPath = object
    QPen = object
    QPixmap = object
    QBrush = object
    QPolygonF = object
    QMainWindow = object
    QWidget = object
    QFileDialog = object
    QGraphicsScene = object
    QGraphicsView = object
    QGraphicsRectItem = object
    QGraphicsSimpleTextItem = object
    QGraphicsPathItem = object
    QGraphicsItem = object
    QCheckBox = object
    QComboBox = object
    QColorDialog = object
    QInputDialog = object
    QHBoxLayout = object
    QLabel = object
    QLineEdit = object
    QListWidget = object
    QMessageBox = object
    QPushButton = object
    QSplitter = object
    QTabWidget = object
    QTreeWidget = object
    QTreeWidgetItem = object
    QVBoxLayout = object
    QTextEdit = object
    QSizePolicy = object
    QPlainTextEdit = object
    QMenu = object
    QSvgRenderer = object
    QScrollArea = object
    QKeySequence = object
    QAction = object
    QDialog = object

SV_KEYWORDS = {
    "module",
    "endmodule",
    "input",
    "output",
    "inout",
    "wire",
    "logic",
    "reg",
    "assign",
    "always",
    "always_ff",
    "always_comb",
    "if",
    "else",
    "case",
    "endcase",
    "begin",
    "end",
}

SV_TYPE_KEYWORDS = {
    "byte",
    "shortint",
    "int",
    "longint",
    "integer",
    "time",
    "realtime",
    "real",
    "shortreal",
    "string",
    "chandle",
    "event",
    "bit",
    "logic",
    "reg",
    "wire",
    "tri",
    "uwire",
    "var",
    "signed",
    "unsigned",
    "struct",
    "union",
    "enum",
    "typedef",
    "localparam",
    "parameter",
    "genvar",
}

DEFAULT_QT_SHORTCUTS = {
    "reload_rtl": ["Ctrl+R"],
    "reload_all": ["Ctrl+Shift+R"],
    "reload_wave": ["Ctrl+Shift+W"],
}


class _SchematicBridge(QObject):
    """Web/scene schematic callbacks that jump to source in the Qt window."""

    def __init__(self, window: "SvViewQtWindow") -> None:
        super().__init__()
        self.window = window

    @Slot(str, int, str)
    def jumpToSource(self, file: str, line: int, raw: str) -> None:
        if not file:
            self.window.set_status(f"Schematic src: {raw}")
            return
        self.window.show_file(file, line)
        self.window.right_tabs.setCurrentIndex(0)
        self.window.set_status(f"Schematic src: {raw}")


class _SchematicSvgLabel(QLabel):
    """Clickable QLabel wrapper for embedded schematic SVG interactions."""

    def __init__(self, window: "SvViewQtWindow") -> None:
        super().__init__()
        self.window = window

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if self.window is not None:
            pos = event.position() if hasattr(event, "position") else event.pos()
            self.window.on_schematic_svg_click(float(pos.x()), float(pos.y()))
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if self.window is not None:
            pos = event.position() if hasattr(event, "position") else event.pos()
            mods = event.modifiers() if hasattr(event, "modifiers") else 0
            ctrl_pressed = bool(mods & Qt.ControlModifier) if Qt is not object else False
            self.window.on_schematic_svg_double_click(float(pos.x()), float(pos.y()), ctrl_pressed)
        super().mouseDoubleClickEvent(event)


class _RtlStructureSvgLabel(QLabel):
    """QLabel placeholder for RTL-structure SVG panes."""

    def __init__(self, window: "SvViewQtWindow") -> None:
        super().__init__()
        self.window = window


class _ElideLabel(QLabel):
    """Label that keeps full text but displays an elided middle section."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self._full_text = str(text or "")
        if QSizePolicy is not object:
            self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        self.setWordWrap(False)
        self._apply_elide_text()

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._apply_elide_text()

    def full_text(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        self._apply_elide_text()
        super().resizeEvent(event)

    def _apply_elide_text(self) -> None:
        txt = str(self._full_text or "")
        if Qt is object:
            super().setText(txt)
            return
        avail = max(24, int(self.contentsRect().width()) - 8)
        elided = self.fontMetrics().elidedText(txt, Qt.ElideMiddle, avail)
        super().setText(elided)


class _RtlStructureGraphicsView(QGraphicsView):
    """Graphics view wrapper that forwards double-click navigation events."""

    def __init__(self, window: "SvViewQtWindow", scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.window = window

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if self.window is not None:
            pos = event.position() if hasattr(event, "position") else event.pos()
            mods = event.modifiers() if hasattr(event, "modifiers") else 0
            ctrl_pressed = bool(mods & Qt.ControlModifier) if Qt is not object else False
            self.window.on_rtl_structure_double_click(float(pos.x()), float(pos.y()), ctrl_pressed)
        super().mouseDoubleClickEvent(event)


def _load_qt_webengine():
    """Best-effort loader for optional Qt WebEngine classes.

    Returns:
        Tuple ``(QWebEngineView, QWebChannel)`` or ``(None, None)`` when
        WebEngine bindings are not available in the runtime.
    """
    try:
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineWidgets import QWebEngineView
        return QWebEngineView, QWebChannel
    except Exception:
        return None, None


class SvViewQtWindow(QMainWindow):
    """Main Qt window for RTLens interactive browsing and schematic features."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.setWindowTitle("RTLens")
        self.resize(1280, 760)

        self.design = DesignDB()
        self.connectivity = ConnectivityDB()
        self.current_hier_path: Optional[str] = None
        self.current_file: Optional[str] = None
        self.current_line: int = 1
        self.loaded_filelist_path: str = ""
        self.loaded_filelist_paths: List[str] = []
        self.loaded_dir_path: str = ""
        self.loaded_wave_path: str = ""
        self.loaded_slang_args: List[str] = []
        self.loaded_rtl_files: List[str] = []
        self._current_lines: List[str] = []
        self.compile_log: str = "Compile log is empty."
        self.search_hits: List[tuple[str, int, str]] = []
        self.wave_bridge = create_wave_bridge(args.wave_viewer, args.gtkwave_cmd, args.surfer_cmd)
        self._source_ctx_signal: Optional[str] = None
        self._source_ctx_word: str = ""
        self._source_ctx_line: int = 1
        self._source_ctx_include_path: Optional[str] = None
        self._source_ctx_instance_path: Optional[str] = None
        self._source_ctx_selected_signals: List[str] = []
        self._wave_ctx_signal: Optional[str] = None
        self._trace_ctx_signal: Optional[str] = None
        self._hier_ctx_path: Optional[str] = None
        self.wave: Optional[WaveDB] = None
        self.wave_signals: List[str] = []
        self.trace_actions: List[dict] = []
        self.qt_shortcuts: dict[str, list[int]] = {}
        self._qt_shortcut_notes: List[str] = []
        self.nav_history: List[tuple[str, int]] = []
        self.nav_index: int = -1
        self.recent_locs: List[tuple[str, int]] = []
        self.bookmarks: List[tuple[str, int]] = []
        self.max_recent: int = 200
        self.schematic_dirty: bool = True
        self.schematic_result: Optional[NetlistSvgResult] = None
        self.schematic_module_name: str = ""
        self.schematic_view_mode: str = getattr(args, "schematic_view", "external")
        self.schematic_zoom: float = 1.0
        self.schematic_svg_path: str = ""
        self.schematic_svg_size: tuple[float, float] = (0.0, 0.0)
        self.schematic_hotspots: List[dict] = []
        self.schematic_selected_spot_index: int = -1
        self.schematic_selected_src: str = ""
        self.schematic_net_segments: List[dict] = []
        self.schematic_net_labels: List[dict] = []
        self.schematic_show_net_labels: bool = False
        self.schematic_show_net_colors: bool = True
        self.schematic_net_highlights: List[dict] = []
        self._schematic_net_highlight_palette: List[str] = [
            "#d73a49",
            "#0a7aca",
            "#198754",
            "#e07b00",
            "#7b4cc2",
            "#008b8b",
            "#c2410c",
            "#b91c1c",
        ]
        self._schematic_net_highlight_palette_index: int = 0
        self._schematic_net_highlight_selection_guard: bool = False
        self.schematic_instance_search_hits: List[dict] = []
        self.schematic_instance_search_index: int = -1
        self._schematic_queue: "queue.Queue[tuple[str, NetlistSvgResult]]" = queue.Queue()
        self._schematic_worker: Optional[threading.Thread] = None
        self._schematic_running_module: str = ""
        self._schematic_pending_refresh: bool = False
        self.rtl_structure_dirty: bool = True
        self.rtl_structure_module_name: str = ""
        self.rtl_structure_svg_bytes: bytes = b""
        self.rtl_structure_png_bytes: bytes = b""
        self.rtl_structure_zoom: float = 1.0
        self.rtl_pixmap_item = None
        self.rtl_structure_last_png_path: str = ""
        self.rtl_structure_hotspots: List[dict] = []
        self.rtl_structure_text_items: List[tuple[object, str]] = []
        self.rtl_structure_edge_label_items: List[object] = []
        self.rtl_structure_elk_layout = None
        self.rtl_structure_layout_nodes: List[dict] = []
        self.rtl_show_edge_labels: bool = True
        self.rtl_signal_highlights: List[dict] = []
        self._rtl_signal_highlight_palette: List[str] = [
            "#d73a49",
            "#0a7aca",
            "#198754",
            "#e07b00",
            "#7b4cc2",
            "#008b8b",
            "#c2410c",
            "#b91c1c",
        ]
        self._rtl_signal_highlight_palette_index: int = 0
        self._rtl_signal_highlight_selection_guard: bool = False
        self.rtl_instance_search_hits: List[dict] = []
        self.rtl_instance_search_index: int = -1
        self.schematic_prebuild_top: str = str(getattr(args, "schematic_prebuild_top", "") or "").strip()
        self.schematic_tab_enabled: bool = bool(self.schematic_prebuild_top)
        self.schematic_cache_index: dict[str, dict] = {}
        self.schematic_prebuild_fail_logs: dict[str, str] = {}
        self.schematic_prebuild_last_summary: str = ""
        self.schematic_prebuild_modules_last: Set[str] = set()
        self.schematic_prebuild_paths_last: Set[str] = set()
        self.schematic_prebuild_strict_top_only: bool = bool(self.schematic_prebuild_top)
        self.schematic_prebuild_log_level: str = str(
            getattr(args, "schematic_prebuild_log_level", "phase") or "phase"
        ).strip().lower()
        self.schematic_prebuild_batch_probe_sec: int = max(
            0, int(getattr(args, "schematic_prebuild_batch_probe_sec", 20))
        )
        self.schematic_prebuild_fail_ttl_sec: int = max(
            0, int(getattr(args, "schematic_prebuild_fail_ttl_sec", 300))
        )
        self._startup_schematic_fail_notice_shown: bool = False
        self._rtl_structure_queue: "queue.Queue[tuple[str, bytes, str, str, str, str]]" = queue.Queue()
        self._rtl_structure_worker: Optional[threading.Thread] = None
        self._rtl_structure_running_module: str = ""
        self._rtl_structure_pending_refresh: bool = False
        self._rtl_bench_queue: "queue.Queue[tuple[str, str, str]]" = queue.Queue()
        self._rtl_bench_worker: Optional[threading.Thread] = None
        self.rtl_show_debug_log: bool = bool(getattr(args, "dev_ui", False) or is_rtl_debug_enabled())
        self.ui_colors: dict[str, str] = {
            "bg_main": "#edf3fa",
            "fg_main": "#152336",
            "input_bg": "#ffffff",
            "input_fg": "#152336",
            "border": "#a8b8cf",
            "btn_bg": "#e5edf8",
            "btn_hover": "#d8e4f6",
            "label_fg": "#22324a",
            "menu_disabled": "#97a3b2",
            "rtl_bg": "#edf2f7",
            "rtl_edge": "#566175",
            "rtl_bus": "#0c5bd6",
            "rtl_ctrl": "#3b67c1",
            "rtl_fanout": "#334c69",
            "rtl_highlight": "#c63b13",
            "rtl_highlight_label": "#9f2f10",
            "rtl_node_border": "#3f4d62",
            "rtl_divider": "#8a96a8",
            "rtl_node_fill_default": "#fff4db",
            "rtl_node_fill_assign": "#edf8ef",
            "rtl_node_fill_callable": "#eaf1ff",
            "rtl_node_fill_always": "#f2ebff",
            "schem_select_stroke": "#c63b13",
            "schem_select_fill": "#f7c59f",
            "schem_port_input": "#1d4ed8",
            "schem_port_output": "#b45309",
            "schem_port_inout": "#0f766e",
            "schem_net_label": "#1e293b",
        }
        self.hier_to_path: dict[int, str] = {}
        self.hier_path_to_item: dict[str, QTreeWidgetItem] = {}
        self._suppress_hier_select: bool = False
        self.qt_shortcuts = self._load_qt_shortcuts()
        if self._qt_shortcut_notes:
            self.compile_log += "\n\n[svview] qt shortcut config\n" + "\n".join(self._qt_shortcut_notes)
        self._effective_editor_cmd: str = self._resolve_effective_editor_cmd()
        self._build_ui()
        self._apply_theme()
        self._start_wave_event_timer()
        self._start_schematic_timer()
        self._start_rtl_structure_timer()
        self._load_startup_inputs()

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        v = QVBoxLayout(root)

        toolbar = QHBoxLayout()
        self.btn_open_filelist = QPushButton("Open filelist")
        self.btn_open_filelist.clicked.connect(self.open_filelist)
        toolbar.addWidget(self.btn_open_filelist)
        self.btn_open_dir = QPushButton("Open dir")
        self.btn_open_dir.clicked.connect(self.open_dir)
        toolbar.addWidget(self.btn_open_dir)
        self.btn_reload = QPushButton("Reload RTL")
        self.btn_reload.clicked.connect(self.reload_rtl)
        toolbar.addWidget(self.btn_reload)
        self.btn_reload_wave = QPushButton("Reload Wave")
        self.btn_reload_wave.clicked.connect(self.reload_wave)
        toolbar.addWidget(self.btn_reload_wave)
        self.btn_reload_all = QPushButton("Reload All")
        self.btn_reload_all.clicked.connect(self.reload_all)
        toolbar.addWidget(self.btn_reload_all)
        self.btn_open_editor = QPushButton("Open in editor")
        self.btn_open_editor.clicked.connect(self.open_external_editor)
        toolbar.addWidget(self.btn_open_editor)
        self.btn_open_wave = QPushButton("Load wave")
        self.btn_open_wave.clicked.connect(self.open_wave)
        toolbar.addWidget(self.btn_open_wave)
        self.btn_open_external = QPushButton("Open External Wave")
        self.btn_open_external.clicked.connect(self.open_external_wave)
        toolbar.addWidget(self.btn_open_external)
        self.btn_import_wave_sel = QPushButton("Import Wave Sel")
        self.btn_import_wave_sel.clicked.connect(self.import_wave_selection)
        toolbar.addWidget(self.btn_import_wave_sel)
        toolbar.addStretch(1)
        v.addLayout(toolbar)
        self._update_nav_buttons()

        splitter = QSplitter(Qt.Horizontal)
        v.addWidget(splitter, 1)

        left = QWidget()
        left_l = QVBoxLayout(left)
        self.hier_tree = QTreeWidget()
        self.hier_tree.setHeaderLabels(["Hierarchy"])
        self.hier_tree.itemSelectionChanged.connect(self.on_hier_select)
        self.hier_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.hier_tree.customContextMenuRequested.connect(self.on_hier_context_menu)
        left_l.addWidget(self.hier_tree, 2)

        sig_path_row = QHBoxLayout()
        self.signal_entry = QLineEdit()
        self.signal_entry.setPlaceholderText("signal path")
        self.signal_entry.returnPressed.connect(self.search_signal)
        sig_path_row.addWidget(self.signal_entry, 1)
        left_l.addLayout(sig_path_row)

        sig_row = QHBoxLayout()
        self.search_btn = QPushButton("Search load/drive")
        self.search_btn.clicked.connect(self.search_signal)
        sig_row.addWidget(self.search_btn)
        self.def_btn = QPushButton("Show definition")
        self.def_btn.clicked.connect(self.show_signal_definition)
        sig_row.addWidget(self.def_btn)
        sig_row.addStretch(1)
        left_l.addLayout(sig_row)
        opt_row = QHBoxLayout()
        self.include_control_check = QCheckBox("Include control deps")
        self.include_control_check.stateChanged.connect(lambda _s: self.search_signal_if_any())
        opt_row.addWidget(self.include_control_check)
        self.include_clock_check = QCheckBox("Include clock deps")
        self.include_clock_check.setChecked(True)
        self.include_clock_check.stateChanged.connect(lambda _s: self.search_signal_if_any())
        opt_row.addWidget(self.include_clock_check)
        self.include_ports_check = QCheckBox("Include port sites")
        self.include_ports_check.stateChanged.connect(lambda _s: self.search_signal_if_any())
        opt_row.addWidget(self.include_ports_check)
        opt_row.addStretch(1)
        left_l.addLayout(opt_row)

        left_l.addWidget(QLabel("Drivers"))
        self.driver_list = QListWidget()
        self.driver_list.itemSelectionChanged.connect(self.on_driver_select)
        self.driver_list.itemDoubleClicked.connect(self.on_driver_jump)
        self.driver_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.driver_list.customContextMenuRequested.connect(lambda pos: self.on_trace_context_menu(pos, is_driver=True))
        left_l.addWidget(self.driver_list, 1)
        left_l.addWidget(QLabel("Loads"))
        self.load_list = QListWidget()
        self.load_list.itemSelectionChanged.connect(self.on_load_select)
        self.load_list.itemDoubleClicked.connect(self.on_load_jump)
        self.load_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.load_list.customContextMenuRequested.connect(lambda pos: self.on_trace_context_menu(pos, is_driver=False))
        left_l.addWidget(self.load_list, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_l = QVBoxLayout(right)
        self.right_tabs = QTabWidget()
        right_l.addWidget(self.right_tabs, 1)

        source_tab = QWidget()
        source_l = QVBoxLayout(source_tab)
        source_nav = QHBoxLayout()
        self.source_back_btn = QPushButton("◀")
        self.source_back_btn.clicked.connect(self.go_back)
        source_nav.addWidget(self.source_back_btn)
        self.source_forward_btn = QPushButton("▶")
        self.source_forward_btn.clicked.connect(self.go_forward)
        source_nav.addWidget(self.source_forward_btn)
        source_nav.addWidget(QLabel("Current:"))
        self.source_nav_edit = QLineEdit("(no source)")
        self.source_nav_edit.setReadOnly(True)
        source_nav.addWidget(self.source_nav_edit, 1)
        self.source_bookmark_btn = QPushButton("☆ Bookmark")
        self.source_bookmark_btn.clicked.connect(self.add_bookmark_current)
        source_nav.addWidget(self.source_bookmark_btn)
        source_l.addLayout(source_nav)
        self.source_text = QTextEdit()
        self.source_text.setReadOnly(True)
        self.source_text.setLineWrapMode(QTextEdit.NoWrap)
        self.source_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.source_text.customContextMenuRequested.connect(self.on_source_context_menu)
        source_l.addWidget(self.source_text, 1)
        self.right_tabs.addTab(source_tab, "Source")

        log_tab = QWidget()
        log_l = QVBoxLayout(log_tab)
        self.compile_log_text = QTextEdit()
        self.compile_log_text.setReadOnly(True)
        self.compile_log_text.setLineWrapMode(QTextEdit.NoWrap)
        log_l.addWidget(self.compile_log_text, 1)
        self.right_tabs.addTab(log_tab, "Compile Log")

        search_tab = QWidget()
        search_l = QVBoxLayout(search_tab)
        search_ctl = QHBoxLayout()
        search_ctl.addWidget(QLabel("Pattern"))
        self.search_pattern_entry = QLineEdit()
        self.search_pattern_entry.returnPressed.connect(self.run_text_search)
        search_ctl.addWidget(self.search_pattern_entry, 2)
        search_ctl.addWidget(QLabel("Scope"))
        self.search_scope_combo = QComboBox()
        self.search_scope_combo.addItems(["Current file", "Design files"])
        search_ctl.addWidget(self.search_scope_combo)
        self.search_regex_check = QCheckBox("Regex")
        search_ctl.addWidget(self.search_regex_check)
        self.search_btn2 = QPushButton("Search")
        self.search_btn2.clicked.connect(self.run_text_search)
        search_ctl.addWidget(self.search_btn2)
        search_l.addLayout(search_ctl)
        self.search_result_list = QListWidget()
        self.search_result_list.itemDoubleClicked.connect(self.on_search_result_jump)
        search_l.addWidget(self.search_result_list, 1)
        self.right_tabs.addTab(search_tab, "Search")

        trace_tab = QWidget()
        trace_l = QVBoxLayout(trace_tab)
        trace_ctl = QHBoxLayout()
        self.trace_clear_btn = QPushButton("Clear Trace")
        self.trace_clear_btn.clicked.connect(self.clear_trace_log)
        trace_ctl.addWidget(self.trace_clear_btn)
        trace_ctl.addStretch(1)
        trace_l.addLayout(trace_ctl)
        self.trace_list = QListWidget()
        self.trace_list.itemDoubleClicked.connect(self.on_trace_jump)
        trace_l.addWidget(self.trace_list, 1)
        self.right_tabs.addTab(trace_tab, "Trace Log")

        nav_tab = QWidget()
        nav_l = QVBoxLayout(nav_tab)
        nav_ctl = QHBoxLayout()
        self.nav_add_bookmark_btn = QPushButton("Add Bookmark")
        self.nav_add_bookmark_btn.clicked.connect(self.add_bookmark_current)
        nav_ctl.addWidget(self.nav_add_bookmark_btn)
        self.nav_remove_bookmark_btn = QPushButton("Remove Bookmark")
        self.nav_remove_bookmark_btn.clicked.connect(self.remove_selected_bookmark)
        nav_ctl.addWidget(self.nav_remove_bookmark_btn)
        self.nav_clear_recent_btn = QPushButton("Clear Recent")
        self.nav_clear_recent_btn.clicked.connect(self.clear_recent)
        nav_ctl.addWidget(self.nav_clear_recent_btn)
        nav_ctl.addStretch(1)
        nav_l.addLayout(nav_ctl)
        nav_l.addWidget(QLabel("Bookmarks"))
        self.bookmark_list = QListWidget()
        self.bookmark_list.itemDoubleClicked.connect(self.on_bookmark_jump)
        nav_l.addWidget(self.bookmark_list, 1)
        nav_l.addWidget(QLabel("Recent"))
        self.recent_list = QListWidget()
        self.recent_list.itemDoubleClicked.connect(self.on_recent_jump)
        nav_l.addWidget(self.recent_list, 1)
        self.right_tabs.addTab(nav_tab, "Nav")

        wave_tab = QWidget()
        wave_l = QVBoxLayout(wave_tab)
        wave_ctl = QHBoxLayout()
        wave_ctl.addWidget(QLabel("Filter"))
        self.wave_filter_entry = QLineEdit()
        self.wave_filter_entry.setPlaceholderText("wave signal filter")
        self.wave_filter_entry.textChanged.connect(self.refresh_wave_list)
        wave_ctl.addWidget(self.wave_filter_entry, 1)
        wave_l.addLayout(wave_ctl)
        self.wave_list = QListWidget()
        self.wave_list.itemSelectionChanged.connect(self.on_wave_select)
        self.wave_list.itemDoubleClicked.connect(self.on_wave_jump)
        self.wave_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.wave_list.customContextMenuRequested.connect(self.on_wave_context_menu)
        wave_l.addWidget(self.wave_list, 1)
        self.right_tabs.addTab(wave_tab, "Wave")

        # Keep a strong reference to schematic_tab even when it is not added to tabs.
        # Otherwise local-only ownership can drop and child widgets become deleted.
        schematic_tab = QWidget()
        self.schematic_tab = schematic_tab
        schematic_l = QVBoxLayout(schematic_tab)
        schematic_ctl = QHBoxLayout()
        self.schematic_refresh_btn = QPushButton("Rebuild Prebuild (Top)")
        self.schematic_refresh_btn.clicked.connect(self.rebuild_schematic_prebuild_top)
        schematic_ctl.addWidget(self.schematic_refresh_btn)
        self.schematic_open_btn = QPushButton("Open External")
        self.schematic_open_btn.clicked.connect(self.open_schematic_external)
        schematic_ctl.addWidget(self.schematic_open_btn)
        self.schematic_zoom_out_btn = QPushButton("-")
        self.schematic_zoom_out_btn.clicked.connect(lambda: self.adjust_schematic_zoom(0.8))
        schematic_ctl.addWidget(self.schematic_zoom_out_btn)
        self.schematic_zoom_in_btn = QPushButton("+")
        self.schematic_zoom_in_btn.clicked.connect(lambda: self.adjust_schematic_zoom(1.25))
        schematic_ctl.addWidget(self.schematic_zoom_in_btn)
        self.schematic_fit_btn = QPushButton("Fit")
        self.schematic_fit_btn.clicked.connect(self.fit_schematic)
        schematic_ctl.addWidget(self.schematic_fit_btn)
        self.schematic_info_label = _ElideLabel("No schematic generated.")
        schematic_ctl.addWidget(self.schematic_info_label, 1)
        schematic_l.addLayout(schematic_ctl)

        self.schematic_tools_tabs = QTabWidget()
        self.schematic_tools_tabs.setDocumentMode(True)
        self.schematic_tools_tabs.setMaximumHeight(186)
        if QSizePolicy is not object:
            self.schematic_tools_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        schem_net_tab = QWidget()
        schem_net_tab_l = QVBoxLayout(schem_net_tab)
        schem_net_tab_l.setContentsMargins(4, 4, 4, 4)
        schem_net_tab_l.setSpacing(4)
        schem_net_ctl = QHBoxLayout()
        self.schematic_net_label_check = QCheckBox("Show Net Labels")
        self.schematic_net_label_check.setChecked(self.schematic_show_net_labels)
        self.schematic_net_label_check.stateChanged.connect(self._on_schematic_net_label_check_changed)
        schem_net_ctl.addWidget(self.schematic_net_label_check)
        schem_net_ctl.addWidget(QLabel("Net Match"))
        self.schematic_net_highlight_entry = QLineEdit()
        self.schematic_net_highlight_entry.setPlaceholderText("net name (partial match)")
        self.schematic_net_highlight_entry.returnPressed.connect(self.add_schematic_net_highlight_from_entry)
        schem_net_ctl.addWidget(self.schematic_net_highlight_entry, 2)
        self.schematic_net_highlight_add_btn = QPushButton("Add")
        self.schematic_net_highlight_add_btn.clicked.connect(self.add_schematic_net_highlight_from_entry)
        schem_net_ctl.addWidget(self.schematic_net_highlight_add_btn)
        schem_net_tab_l.addLayout(schem_net_ctl)

        schem_net_ctl2 = QHBoxLayout()
        self.schematic_net_highlight_remove_btn = QPushButton("Remove")
        self.schematic_net_highlight_remove_btn.clicked.connect(self.remove_selected_schematic_net_highlight)
        schem_net_ctl2.addWidget(self.schematic_net_highlight_remove_btn)
        self.schematic_net_highlight_on_btn = QPushButton("On")
        self.schematic_net_highlight_on_btn.clicked.connect(
            lambda: self.set_selected_schematic_net_highlight_enabled(True)
        )
        schem_net_ctl2.addWidget(self.schematic_net_highlight_on_btn)
        self.schematic_net_highlight_off_btn = QPushButton("Off")
        self.schematic_net_highlight_off_btn.clicked.connect(
            lambda: self.set_selected_schematic_net_highlight_enabled(False)
        )
        schem_net_ctl2.addWidget(self.schematic_net_highlight_off_btn)
        self.schematic_net_highlight_edit_btn = QPushButton("Edit")
        self.schematic_net_highlight_edit_btn.clicked.connect(self.edit_selected_schematic_net_highlight_query)
        schem_net_ctl2.addWidget(self.schematic_net_highlight_edit_btn)
        self.schematic_net_highlight_color_btn = QPushButton("Color")
        self.schematic_net_highlight_color_btn.clicked.connect(self.choose_selected_schematic_net_highlight_color)
        schem_net_ctl2.addWidget(self.schematic_net_highlight_color_btn)
        self.schematic_net_highlight_clear_btn = QPushButton("Clear")
        self.schematic_net_highlight_clear_btn.clicked.connect(self.clear_schematic_net_highlights)
        schem_net_ctl2.addWidget(self.schematic_net_highlight_clear_btn)
        schem_net_ctl2.addStretch(1)
        schem_net_tab_l.addLayout(schem_net_ctl2)

        self.schematic_net_highlight_list = QListWidget()
        self.schematic_net_highlight_list.setMaximumHeight(64)
        self.schematic_net_highlight_list.itemDoubleClicked.connect(self._on_schematic_net_highlight_double_click)
        schem_net_tab_l.addWidget(self.schematic_net_highlight_list)
        self.schematic_tools_tabs.addTab(schem_net_tab, "Net")

        schem_inst_tab = QWidget()
        schem_inst_tab_l = QVBoxLayout(schem_inst_tab)
        schem_inst_tab_l.setContentsMargins(4, 4, 4, 4)
        schem_inst_tab_l.setSpacing(4)
        schem_inst_ctl = QHBoxLayout()
        schem_inst_ctl.addWidget(QLabel("Inst Regex"))
        self.schematic_inst_search_entry = QLineEdit()
        self.schematic_inst_search_entry.setPlaceholderText("instance regex")
        self.schematic_inst_search_entry.returnPressed.connect(self.run_schematic_instance_search)
        schem_inst_ctl.addWidget(self.schematic_inst_search_entry, 2)
        self.schematic_inst_search_btn = QPushButton("Find")
        self.schematic_inst_search_btn.clicked.connect(self.run_schematic_instance_search)
        schem_inst_ctl.addWidget(self.schematic_inst_search_btn)
        self.schematic_inst_prev_btn = QPushButton("Prev")
        self.schematic_inst_prev_btn.clicked.connect(self.focus_previous_schematic_instance_search_hit)
        schem_inst_ctl.addWidget(self.schematic_inst_prev_btn)
        self.schematic_inst_next_btn = QPushButton("Next")
        self.schematic_inst_next_btn.clicked.connect(self.focus_next_schematic_instance_search_hit)
        schem_inst_ctl.addWidget(self.schematic_inst_next_btn)
        schem_inst_ctl.addStretch(1)
        schem_inst_tab_l.addLayout(schem_inst_ctl)
        self.schematic_inst_hit_list = QListWidget()
        self.schematic_inst_hit_list.setMaximumHeight(84)
        self.schematic_inst_hit_list.itemSelectionChanged.connect(self._on_schematic_instance_search_select)
        self.schematic_inst_hit_list.itemDoubleClicked.connect(self._on_schematic_instance_search_jump)
        schem_inst_tab_l.addWidget(self.schematic_inst_hit_list)
        self.schematic_tools_tabs.addTab(schem_inst_tab, "Instance")

        schematic_l.addWidget(self.schematic_tools_tabs)
        self.schematic_view = None
        if self.schematic_view_mode == "webengine":
            QWebEngineView, QWebChannel = _load_qt_webengine()
            if QWebEngineView is not None and QWebChannel is not None:
                self.schematic_view = QWebEngineView()
                self.schematic_channel = QWebChannel(self.schematic_view.page())
                self.schematic_bridge = _SchematicBridge(self)
                self.schematic_channel.registerObject("rtlensBridge", self.schematic_bridge)
                self.schematic_view.page().setWebChannel(self.schematic_channel)
                schematic_l.addWidget(self.schematic_view, 1)
            else:
                self.schematic_view_mode = "external"
        if self.schematic_view is None:
            if self.schematic_view_mode == "svg" and QSvgRenderer is not object:
                self.schematic_view = _SchematicSvgLabel(self)
                self.schematic_view.setAlignment(Qt.AlignLeft | Qt.AlignTop)
                self.schematic_view.setText("No schematic generated.")
                if QSizePolicy is not object:
                    self.schematic_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
                self.schematic_view.setMinimumSize(1, 1)
                self.schematic_scroll = QScrollArea()
                self.schematic_scroll.setWidgetResizable(False)
                if QSizePolicy is not object:
                    self.schematic_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                if QAbstractScrollArea is not object:
                    self.schematic_scroll.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
                self.schematic_scroll.setMinimumSize(0, 0)
                self.schematic_scroll.setWidget(self.schematic_view)
                schematic_l.addWidget(self.schematic_scroll, 1)
            else:
                self.schematic_view = QTextEdit()
                self.schematic_view.setReadOnly(True)
                schematic_l.addWidget(self.schematic_view, 1)
        self.schematic_log = QPlainTextEdit()
        self.schematic_log.setReadOnly(True)
        self.schematic_log.setMaximumBlockCount(2000)
        self.schematic_log.setPlaceholderText("Schematic generation log.")
        self.schematic_log.setMaximumHeight(180)
        schematic_l.addWidget(self.schematic_log)
        self._update_schematic_log_visibility()
        if self.schematic_tab_enabled:
            self.right_tabs.addTab(schematic_tab, "Schematic")

        rtl_tab = QWidget()
        rtl_l = QVBoxLayout(rtl_tab)
        rtl_ctl = QHBoxLayout()
        self.rtl_refresh_btn = QPushButton("Refresh RTL Structure")
        self.rtl_refresh_btn.clicked.connect(self.refresh_rtl_structure)
        rtl_ctl.addWidget(self.rtl_refresh_btn)
        self.rtl_bench_btn = QPushButton("Benchmark ELK")
        self.rtl_bench_btn.clicked.connect(self.benchmark_rtl_structure)
        rtl_ctl.addWidget(self.rtl_bench_btn)
        if not bool(getattr(self.args, "dev_ui", False)):
            self.rtl_bench_btn.setVisible(False)
        self.rtl_mode_combo = QComboBox()
        self.rtl_mode_combo.addItem("Auto", "auto")
        self.rtl_mode_combo.addItem("Detailed", "detailed")
        default_mode = str(getattr(self.args, "rtl_structure_mode", "auto") or "auto")
        idx = max(0, self.rtl_mode_combo.findData(default_mode))
        self.rtl_mode_combo.setCurrentIndex(idx)
        self.rtl_mode_combo.currentIndexChanged.connect(self._on_rtl_mode_changed)
        rtl_ctl.addWidget(self.rtl_mode_combo)
        self.rtl_zoom_out_btn = QPushButton("-")
        self.rtl_zoom_out_btn.clicked.connect(lambda: self.adjust_rtl_structure_zoom(0.8))
        rtl_ctl.addWidget(self.rtl_zoom_out_btn)
        self.rtl_zoom_in_btn = QPushButton("+")
        self.rtl_zoom_in_btn.clicked.connect(lambda: self.adjust_rtl_structure_zoom(1.25))
        rtl_ctl.addWidget(self.rtl_zoom_in_btn)
        self.rtl_fit_btn = QPushButton("Fit")
        self.rtl_fit_btn.clicked.connect(self.fit_rtl_structure)
        rtl_ctl.addWidget(self.rtl_fit_btn)
        self.rtl_info_label = QLabel("No RTL structure generated.")
        self.rtl_info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rtl_ctl.addWidget(self.rtl_info_label, 1)
        rtl_l.addLayout(rtl_ctl)

        self.rtl_tools_tabs = QTabWidget()
        self.rtl_tools_tabs.setDocumentMode(True)
        self.rtl_tools_tabs.setMaximumHeight(176)

        rtl_net_tab = QWidget()
        rtl_net_tab_l = QVBoxLayout(rtl_net_tab)
        rtl_net_tab_l.setContentsMargins(4, 4, 4, 4)
        rtl_net_tab_l.setSpacing(4)

        rtl_hl_ctl = QHBoxLayout()
        rtl_hl_ctl.addWidget(QLabel("Net Highlight"))
        self.rtl_net_highlight_entry = QLineEdit()
        self.rtl_net_highlight_entry.setPlaceholderText("net name (partial match)")
        self.rtl_net_highlight_entry.returnPressed.connect(self.add_rtl_signal_highlight_from_entry)
        rtl_hl_ctl.addWidget(self.rtl_net_highlight_entry, 2)
        self.rtl_net_highlight_add_btn = QPushButton("Add")
        self.rtl_net_highlight_add_btn.clicked.connect(self.add_rtl_signal_highlight_from_entry)
        rtl_hl_ctl.addWidget(self.rtl_net_highlight_add_btn)
        rtl_net_tab_l.addLayout(rtl_hl_ctl)

        rtl_hl_ctl2 = QHBoxLayout()
        self.rtl_net_highlight_remove_btn = QPushButton("Remove")
        self.rtl_net_highlight_remove_btn.clicked.connect(self.remove_selected_rtl_signal_highlight)
        rtl_hl_ctl2.addWidget(self.rtl_net_highlight_remove_btn)
        self.rtl_net_highlight_on_btn = QPushButton("On")
        self.rtl_net_highlight_on_btn.clicked.connect(lambda: self.set_selected_rtl_signal_highlight_enabled(True))
        rtl_hl_ctl2.addWidget(self.rtl_net_highlight_on_btn)
        self.rtl_net_highlight_off_btn = QPushButton("Off")
        self.rtl_net_highlight_off_btn.clicked.connect(lambda: self.set_selected_rtl_signal_highlight_enabled(False))
        rtl_hl_ctl2.addWidget(self.rtl_net_highlight_off_btn)
        self.rtl_net_highlight_edit_btn = QPushButton("Edit")
        self.rtl_net_highlight_edit_btn.clicked.connect(self.edit_selected_rtl_signal_highlight_query)
        rtl_hl_ctl2.addWidget(self.rtl_net_highlight_edit_btn)
        self.rtl_net_highlight_color_btn = QPushButton("Color")
        self.rtl_net_highlight_color_btn.clicked.connect(self.choose_selected_rtl_signal_highlight_color)
        rtl_hl_ctl2.addWidget(self.rtl_net_highlight_color_btn)
        self.rtl_net_highlight_clear_btn = QPushButton("Clear All")
        self.rtl_net_highlight_clear_btn.clicked.connect(self.clear_rtl_signal_highlights)
        rtl_hl_ctl2.addWidget(self.rtl_net_highlight_clear_btn)
        self.rtl_net_highlight_export_btn = QPushButton("Export")
        self.rtl_net_highlight_export_btn.clicked.connect(self.export_rtl_signal_highlights)
        rtl_hl_ctl2.addWidget(self.rtl_net_highlight_export_btn)
        self.rtl_net_highlight_import_btn = QPushButton("Import")
        self.rtl_net_highlight_import_btn.clicked.connect(self.import_rtl_signal_highlights)
        rtl_hl_ctl2.addWidget(self.rtl_net_highlight_import_btn)
        self.rtl_edge_label_check = QCheckBox("Show Net Labels")
        self.rtl_edge_label_check.setChecked(self.rtl_show_edge_labels)
        self.rtl_edge_label_check.stateChanged.connect(self._on_rtl_edge_label_check_changed)
        rtl_hl_ctl2.addWidget(self.rtl_edge_label_check)
        rtl_hl_ctl2.addStretch(1)
        rtl_net_tab_l.addLayout(rtl_hl_ctl2)

        self.rtl_highlight_list = QListWidget()
        self.rtl_highlight_list.setMaximumHeight(64)
        self.rtl_highlight_list.itemDoubleClicked.connect(self._on_rtl_signal_highlight_double_click)
        rtl_net_tab_l.addWidget(self.rtl_highlight_list)
        self.rtl_tools_tabs.addTab(rtl_net_tab, "Net")

        rtl_inst_tab = QWidget()
        rtl_inst_tab_l = QVBoxLayout(rtl_inst_tab)
        rtl_inst_tab_l.setContentsMargins(4, 4, 4, 4)
        rtl_inst_tab_l.setSpacing(4)
        rtl_inst_ctl = QHBoxLayout()
        rtl_inst_ctl.addWidget(QLabel("Inst Regex"))
        self.rtl_inst_search_entry = QLineEdit()
        self.rtl_inst_search_entry.setPlaceholderText("instance regex")
        self.rtl_inst_search_entry.returnPressed.connect(self.run_rtl_instance_search)
        rtl_inst_ctl.addWidget(self.rtl_inst_search_entry, 2)
        self.rtl_inst_search_btn = QPushButton("Find")
        self.rtl_inst_search_btn.clicked.connect(self.run_rtl_instance_search)
        rtl_inst_ctl.addWidget(self.rtl_inst_search_btn)
        self.rtl_inst_prev_btn = QPushButton("Prev")
        self.rtl_inst_prev_btn.clicked.connect(self.focus_previous_rtl_instance_search_hit)
        rtl_inst_ctl.addWidget(self.rtl_inst_prev_btn)
        self.rtl_inst_next_btn = QPushButton("Next")
        self.rtl_inst_next_btn.clicked.connect(self.focus_next_rtl_instance_search_hit)
        rtl_inst_ctl.addWidget(self.rtl_inst_next_btn)
        rtl_inst_ctl.addStretch(1)
        rtl_inst_tab_l.addLayout(rtl_inst_ctl)

        self.rtl_inst_hit_list = QListWidget()
        self.rtl_inst_hit_list.setMaximumHeight(84)
        self.rtl_inst_hit_list.itemSelectionChanged.connect(self._on_rtl_instance_search_select)
        self.rtl_inst_hit_list.itemDoubleClicked.connect(self._on_rtl_instance_search_jump)
        rtl_inst_tab_l.addWidget(self.rtl_inst_hit_list)
        self.rtl_tools_tabs.addTab(rtl_inst_tab, "Inst")
        rtl_l.addWidget(self.rtl_tools_tabs)
        self.rtl_scene = QGraphicsScene(self)
        self.rtl_view = _RtlStructureGraphicsView(self, self.rtl_scene)
        self.rtl_view.setDragMode(QGraphicsView.ScrollHandDrag)
        if hasattr(self.rtl_view, "setBackgroundBrush") and QColor is not object:
            self.rtl_view.setBackgroundBrush(QColor(self.ui_colors.get("rtl_bg", "#edf2f7")))
        rtl_l.addWidget(self.rtl_view, 1)
        self.rtl_log = QPlainTextEdit()
        self.rtl_log.setReadOnly(True)
        self.rtl_log.setMaximumBlockCount(2000)
        self.rtl_log.setPlaceholderText("RTL structure generation log.")
        self.rtl_log.setMaximumHeight(180)
        self.rtl_log.setVisible(self.rtl_show_debug_log)
        rtl_l.addWidget(self.rtl_log)
        self.right_tabs.addTab(rtl_tab, "RTL Structure")
        self.right_tabs.currentChanged.connect(self.on_right_tab_changed)

        splitter.addWidget(right)
        splitter.setSizes([520, 1180])

        wave_status_row = QHBoxLayout()
        wave_status_row.addWidget(QLabel("Wave file:"))
        self.wave_file_label = QLabel("(no wave loaded)")
        self.wave_file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        wave_status_row.addWidget(self.wave_file_label, 1)
        v.addLayout(wave_status_row)

        # Dedicated status pane (separate from toolbar) to avoid clipping.
        self.status_view = QPlainTextEdit()
        self.status_view.setReadOnly(True)
        self.status_view.setMaximumHeight(52)
        self.status_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.status_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        v.addWidget(self.status_view)
        self._build_menu_bar()

    def _on_rtl_mode_changed(self) -> None:
        self.rtl_structure_dirty = True
        if self._current_right_tab_name() == "RTL Structure":
            self.refresh_rtl_structure()

    def _current_right_tab_name(self) -> str:
        if not hasattr(self, "right_tabs"):
            return ""
        idx = int(self.right_tabs.currentIndex())
        if idx < 0:
            return ""
        try:
            return str(self.right_tabs.tabText(idx))
        except Exception:
            return ""

    def _normalize_color_hex(self, value: str) -> str:
        raw = str(value or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
            return raw.lower()
        return ""

    def _next_rtl_signal_highlight_color(self) -> str:
        if not self._rtl_signal_highlight_palette:
            return "#d73a49"
        idx = self._rtl_signal_highlight_palette_index % len(self._rtl_signal_highlight_palette)
        self._rtl_signal_highlight_palette_index += 1
        return self._rtl_signal_highlight_palette[idx]

    def _refresh_rtl_signal_highlight_list(self) -> None:
        if not hasattr(self, "rtl_highlight_list"):
            return
        self._rtl_signal_highlight_selection_guard = True
        try:
            self.rtl_highlight_list.clear()
            for i, rule in enumerate(self.rtl_signal_highlights):
                q = str(rule.get("query", "")).strip()
                color = self._normalize_color_hex(str(rule.get("color", ""))) or "#d73a49"
                enabled = bool(rule.get("enabled", True))
                state = "on" if enabled else "off"
                self.rtl_highlight_list.addItem(f"[{state}] {q}  {color}")
        finally:
            self._rtl_signal_highlight_selection_guard = False

    def _redraw_rtl_structure_if_ready(self) -> None:
        if not getattr(self, "rtl_structure_elk_layout", None):
            return
        self._draw_rtl_structure_elk(self.rtl_structure_elk_layout)
        self._apply_rtl_structure_zoom()

    def _rtl_highlight_query_matches_visible_signal(self, query: str) -> bool:
        q = str(query or "").strip().lower()
        if not q:
            return False
        if not isinstance(self.rtl_structure_elk_layout, dict):
            return True
        for edge in self.rtl_structure_elk_layout.get("edges", []):
            sig = str(edge.get("rtlensSignalName", "")).strip()
            if sig and q in sig.lower():
                return True
        return False

    def _selected_rtl_signal_highlight_index(self) -> int:
        if not hasattr(self, "rtl_highlight_list"):
            return -1
        row = int(self.rtl_highlight_list.currentRow())
        if row < 0 or row >= len(self.rtl_signal_highlights):
            return -1
        return row

    def add_rtl_signal_highlight_from_entry(self) -> None:
        query = self.rtl_net_highlight_entry.text().strip() if hasattr(self, "rtl_net_highlight_entry") else ""
        if not query:
            self.set_status("Highlight query is empty")
            return
        if self.rtl_structure_elk_layout and not self._rtl_highlight_query_matches_visible_signal(query):
            self.set_status(f"No visible net matched: {query}")
            return
        self.rtl_signal_highlights.append(
            {
                "query": query,
                "color": self._next_rtl_signal_highlight_color(),
                "enabled": True,
            }
        )
        if hasattr(self, "rtl_net_highlight_entry"):
            self.rtl_net_highlight_entry.clear()
        self._refresh_rtl_signal_highlight_list()
        self._redraw_rtl_structure_if_ready()
        self.set_status(f"Highlight added: {query}")

    def remove_selected_rtl_signal_highlight(self) -> None:
        row = self._selected_rtl_signal_highlight_index()
        if row < 0:
            self.set_status("No highlight selected")
            return
        removed = self.rtl_signal_highlights.pop(row)
        self._refresh_rtl_signal_highlight_list()
        self._redraw_rtl_structure_if_ready()
        self.set_status(f"Highlight removed: {removed.get('query', '')}")

    def set_selected_rtl_signal_highlight_enabled(self, enabled: bool) -> None:
        row = self._selected_rtl_signal_highlight_index()
        if row < 0:
            self.set_status("No highlight selected")
            return
        self.rtl_signal_highlights[row]["enabled"] = bool(enabled)
        self._refresh_rtl_signal_highlight_list()
        if hasattr(self, "rtl_highlight_list"):
            self.rtl_highlight_list.setCurrentRow(row)
        self._redraw_rtl_structure_if_ready()
        self.set_status(
            f"Highlight {'enabled' if enabled else 'disabled'}: "
            f"{self.rtl_signal_highlights[row].get('query', '')}"
        )

    def edit_selected_rtl_signal_highlight_query(self) -> None:
        row = self._selected_rtl_signal_highlight_index()
        if row < 0:
            self.set_status("No highlight selected")
            return
        old_query = str(self.rtl_signal_highlights[row].get("query", "")).strip()
        if QInputDialog is object:
            new_query = self.rtl_net_highlight_entry.text().strip() if hasattr(self, "rtl_net_highlight_entry") else ""
            if not new_query:
                self.set_status("Highlight query is empty")
                return
        else:
            new_query, ok = QInputDialog.getText(
                self,
                "Edit highlighted net",
                "Net name (partial match):",
                QLineEdit.Normal,
                old_query,
            )
            if not ok:
                return
            new_query = str(new_query).strip()
            if not new_query:
                self.set_status("Highlight query is empty")
                return
        if self.rtl_structure_elk_layout and not self._rtl_highlight_query_matches_visible_signal(new_query):
            self.set_status(f"No visible net matched: {new_query}")
            return
        self.rtl_signal_highlights[row]["query"] = new_query
        self._refresh_rtl_signal_highlight_list()
        if hasattr(self, "rtl_highlight_list"):
            self.rtl_highlight_list.setCurrentRow(row)
        self._redraw_rtl_structure_if_ready()
        self.set_status(f"Highlight renamed: {old_query} -> {new_query}")

    def _on_rtl_signal_highlight_double_click(self, item) -> None:
        if not hasattr(self, "rtl_highlight_list"):
            return
        row = int(self.rtl_highlight_list.row(item))
        if row < 0 or row >= len(self.rtl_signal_highlights):
            return
        enabled = bool(self.rtl_signal_highlights[row].get("enabled", True))
        self.rtl_signal_highlights[row]["enabled"] = not enabled
        self._refresh_rtl_signal_highlight_list()
        self.rtl_highlight_list.setCurrentRow(row)
        self._redraw_rtl_structure_if_ready()
        self.set_status(
            f"Highlight {'enabled' if (not enabled) else 'disabled'}: "
            f"{self.rtl_signal_highlights[row].get('query', '')}"
        )

    def choose_selected_rtl_signal_highlight_color(self) -> None:
        if QColorDialog is object or QColor is object:
            self.set_status("Color picker is unavailable in this Qt build")
            return
        row = self._selected_rtl_signal_highlight_index()
        if row < 0:
            self.set_status("No highlight selected")
            return
        current = self._normalize_color_hex(str(self.rtl_signal_highlights[row].get("color", ""))) or "#d73a49"
        chosen = QColorDialog.getColor(QColor(current), self, "Choose highlight color")
        if not hasattr(chosen, "isValid") or not chosen.isValid():
            return
        self.rtl_signal_highlights[row]["color"] = str(chosen.name()).lower()
        self._refresh_rtl_signal_highlight_list()
        self._redraw_rtl_structure_if_ready()
        self.set_status(f"Highlight color updated: {self.rtl_signal_highlights[row].get('query', '')}")

    def clear_rtl_signal_highlights(self) -> None:
        if not self.rtl_signal_highlights:
            return
        self.rtl_signal_highlights = []
        self._refresh_rtl_signal_highlight_list()
        self._redraw_rtl_structure_if_ready()
        self.set_status("All highlights cleared")

    def export_rtl_signal_highlights(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export RTL highlight settings",
            "rtl_highlights.json",
            "JSON files (*.json)",
        )
        if not path:
            return
        payload = {
            "version": 1,
            "net_highlights": list(self.rtl_signal_highlights),
            "show_edge_labels": bool(self.rtl_show_edge_labels),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")
            self.set_status(f"Highlight settings exported: {path}")
        except Exception as e:
            self.set_status(f"Export failed: {e}")

    def import_rtl_signal_highlights(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import RTL highlight settings",
            "",
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.set_status(f"Import failed: {e}")
            return
        if not isinstance(data, dict):
            self.set_status("Import failed: JSON root must be an object")
            return
        highlights_raw = data.get("net_highlights", [])
        if not isinstance(highlights_raw, list):
            self.set_status("Import failed: net_highlights must be a list")
            return
        restored: List[dict] = []
        for item in highlights_raw:
            if not isinstance(item, dict):
                continue
            query = str(item.get("query", "")).strip()
            if not query:
                continue
            color = self._normalize_color_hex(str(item.get("color", ""))) or self._next_rtl_signal_highlight_color()
            enabled = bool(item.get("enabled", True))
            restored.append({"query": query, "color": color, "enabled": enabled})
        self.rtl_signal_highlights = restored
        if "show_edge_labels" in data:
            self.rtl_show_edge_labels = bool(data.get("show_edge_labels"))
            if hasattr(self, "rtl_edge_label_check"):
                self.rtl_edge_label_check.setChecked(self.rtl_show_edge_labels)
        self._refresh_rtl_signal_highlight_list()
        self._redraw_rtl_structure_if_ready()
        self.set_status(f"Highlight settings imported: {len(restored)} rules")

    def _on_rtl_edge_label_check_changed(self, state: int) -> None:
        if Qt is object:
            self.rtl_show_edge_labels = bool(state)
        else:
            try:
                state_int = int(state)
            except Exception:
                state_int = int(getattr(state, "value", 0))
            checked_values: list[int] = []
            for cand in (
                getattr(Qt, "Checked", None),
                getattr(getattr(Qt, "CheckState", object), "Checked", None),
            ):
                if cand is None:
                    continue
                try:
                    checked_values.append(int(cand))
                except Exception:
                    try:
                        checked_values.append(int(getattr(cand, "value", 0)))
                    except Exception:
                        pass
            self.rtl_show_edge_labels = state_int in checked_values if checked_values else bool(state_int)
        self._redraw_rtl_structure_if_ready()
        self.set_status(f"Edge net labels: {'on' if self.rtl_show_edge_labels else 'off'}")

    def _match_rtl_highlight_rule(self, signal_name: str) -> Optional[dict]:
        sig = str(signal_name or "")
        if not sig:
            return None
        text = sig.lower()
        for rule in self.rtl_signal_highlights:
            if not bool(rule.get("enabled", True)):
                continue
            query = str(rule.get("query", "")).strip().lower()
            if query and query in text:
                return rule
        return None

    def _refresh_rtl_instance_hit_list(self) -> None:
        if not hasattr(self, "rtl_inst_hit_list"):
            return
        self.rtl_inst_hit_list.clear()
        for hit in self.rtl_instance_search_hits:
            name = str(hit.get("name", ""))
            module = str(hit.get("module_name", ""))
            self.rtl_inst_hit_list.addItem(f"{name} : {module}")

    def _focus_rtl_instance_search_hit(self, index: int, sync_list: bool = True) -> None:
        if not self.rtl_instance_search_hits:
            return
        idx = index % len(self.rtl_instance_search_hits)
        self.rtl_instance_search_index = idx
        hit = self.rtl_instance_search_hits[idx]
        x0, y0, x1, y1 = hit.get("rect", (0.0, 0.0, 0.0, 0.0))
        cx = (float(x0) + float(x1)) / 2.0
        cy = (float(y0) + float(y1)) / 2.0
        self.rtl_view.centerOn(cx, cy)
        if sync_list and hasattr(self, "rtl_inst_hit_list"):
            self.rtl_inst_hit_list.setCurrentRow(idx)
        self.set_status(
            f"Instance hit {idx + 1}/{len(self.rtl_instance_search_hits)}: "
            f"{hit.get('name', '')}"
        )

    def run_rtl_instance_search(self) -> None:
        query = self.rtl_inst_search_entry.text().strip() if hasattr(self, "rtl_inst_search_entry") else ""
        if not query:
            self.set_status("Instance regex is empty")
            return
        try:
            rx = re.compile(query, re.IGNORECASE)
        except re.error as e:
            self.set_status(f"Invalid regex: {e}")
            return
        hits: List[dict] = []
        for node in self.rtl_structure_layout_nodes:
            if str(node.get("node_type", "")) != "instance":
                continue
            fields = [
                str(node.get("name", "")),
                str(node.get("label", "")),
                str(node.get("module_name", "")),
            ]
            if any(rx.search(field) for field in fields if field):
                hits.append(node)
        self.rtl_instance_search_hits = hits
        self.rtl_instance_search_index = -1
        self._refresh_rtl_instance_hit_list()
        if not hits:
            self.set_status(f"Instance regex hit: 0 ({query})")
            return
        self._focus_rtl_instance_search_hit(0)

    def focus_previous_rtl_instance_search_hit(self) -> None:
        if not self.rtl_instance_search_hits:
            self.set_status("No instance search hits")
            return
        if self.rtl_instance_search_index < 0:
            self._focus_rtl_instance_search_hit(0)
            return
        self._focus_rtl_instance_search_hit(self.rtl_instance_search_index - 1)

    def focus_next_rtl_instance_search_hit(self) -> None:
        if not self.rtl_instance_search_hits:
            self.set_status("No instance search hits")
            return
        if self.rtl_instance_search_index < 0:
            self._focus_rtl_instance_search_hit(0)
            return
        self._focus_rtl_instance_search_hit(self.rtl_instance_search_index + 1)

    def _on_rtl_instance_search_select(self) -> None:
        if not hasattr(self, "rtl_inst_hit_list"):
            return
        row = int(self.rtl_inst_hit_list.currentRow())
        if row < 0 or row >= len(self.rtl_instance_search_hits):
            return
        self._focus_rtl_instance_search_hit(row, sync_list=False)

    def _on_rtl_instance_search_jump(self, item) -> None:
        if not hasattr(self, "rtl_inst_hit_list"):
            return
        row = int(self.rtl_inst_hit_list.row(item))
        if row < 0 or row >= len(self.rtl_instance_search_hits):
            return
        self._focus_rtl_instance_search_hit(row, sync_list=False)

    def _on_schematic_net_label_check_changed(self, state: int) -> None:
        self.schematic_show_net_labels = bool(state)
        self._apply_schematic_zoom()
        self.set_status(f"Schematic net labels: {'on' if self.schematic_show_net_labels else 'off'}")

    def _next_schematic_net_highlight_color(self) -> str:
        if not self._schematic_net_highlight_palette:
            return "#d73a49"
        idx = self._schematic_net_highlight_palette_index % len(self._schematic_net_highlight_palette)
        self._schematic_net_highlight_palette_index += 1
        return self._schematic_net_highlight_palette[idx]

    def _refresh_schematic_net_highlight_list(self) -> None:
        if not hasattr(self, "schematic_net_highlight_list"):
            return
        self._schematic_net_highlight_selection_guard = True
        try:
            self.schematic_net_highlight_list.clear()
            for rule in self.schematic_net_highlights:
                q = str(rule.get("query", "")).strip()
                color = self._normalize_color_hex(str(rule.get("color", ""))) or "#d73a49"
                enabled = bool(rule.get("enabled", True))
                state = "on" if enabled else "off"
                self.schematic_net_highlight_list.addItem(f"[{state}] {q}  {color}")
        finally:
            self._schematic_net_highlight_selection_guard = False

    def _selected_schematic_net_highlight_index(self) -> int:
        if not hasattr(self, "schematic_net_highlight_list"):
            return -1
        row = int(self.schematic_net_highlight_list.currentRow())
        if row < 0 or row >= len(self.schematic_net_highlights):
            return -1
        return row

    def _schematic_net_query_matches_visible_signal(self, query: str) -> bool:
        q = str(query or "").strip().lower()
        if not q:
            return False
        for item in self.schematic_net_labels:
            label = str(item.get("label", "")).strip().lower()
            if label and q in label:
                return True
        return False

    def _match_schematic_net_highlight_rule(self, signal_name: str) -> Optional[dict]:
        sig = str(signal_name or "").strip().lower()
        if not sig:
            return None
        for rule in self.schematic_net_highlights:
            if not bool(rule.get("enabled", True)):
                continue
            q = str(rule.get("query", "")).strip().lower()
            if not q:
                continue
            if q in sig:
                return rule
        return None

    def add_schematic_net_highlight_from_entry(self) -> None:
        query = (
            self.schematic_net_highlight_entry.text().strip()
            if hasattr(self, "schematic_net_highlight_entry")
            else ""
        )
        if not query:
            self.set_status("Schematic net query is empty")
            return
        if not self.schematic_net_labels:
            self.set_status("No source-mapped net labels available in this schematic")
            return
        if not self._schematic_net_query_matches_visible_signal(query):
            self.set_status(f"No visible net matched: {query}")
            return
        self.schematic_net_highlights.append(
            {
                "query": query,
                "color": self._next_schematic_net_highlight_color(),
                "enabled": True,
            }
        )
        if hasattr(self, "schematic_net_highlight_entry"):
            self.schematic_net_highlight_entry.clear()
        self._refresh_schematic_net_highlight_list()
        self._apply_schematic_zoom()
        self.set_status(f"Schematic net highlight added: {query}")

    def remove_selected_schematic_net_highlight(self) -> None:
        row = self._selected_schematic_net_highlight_index()
        if row < 0:
            self.set_status("No schematic net highlight selected")
            return
        removed = self.schematic_net_highlights.pop(row)
        self._refresh_schematic_net_highlight_list()
        self._apply_schematic_zoom()
        self.set_status(f"Schematic net highlight removed: {removed.get('query', '')}")

    def set_selected_schematic_net_highlight_enabled(self, enabled: bool) -> None:
        row = self._selected_schematic_net_highlight_index()
        if row < 0:
            self.set_status("No schematic net highlight selected")
            return
        self.schematic_net_highlights[row]["enabled"] = bool(enabled)
        self._refresh_schematic_net_highlight_list()
        if hasattr(self, "schematic_net_highlight_list"):
            self.schematic_net_highlight_list.setCurrentRow(row)
        self._apply_schematic_zoom()
        self.set_status(
            f"Schematic net highlight {'enabled' if enabled else 'disabled'}: "
            f"{self.schematic_net_highlights[row].get('query', '')}"
        )

    def edit_selected_schematic_net_highlight_query(self) -> None:
        row = self._selected_schematic_net_highlight_index()
        if row < 0:
            self.set_status("No schematic net highlight selected")
            return
        old_query = str(self.schematic_net_highlights[row].get("query", "")).strip()
        if QInputDialog is object:
            new_query = (
                self.schematic_net_highlight_entry.text().strip()
                if hasattr(self, "schematic_net_highlight_entry")
                else ""
            )
            if not new_query:
                self.set_status("Schematic net query is empty")
                return
        else:
            new_query, ok = QInputDialog.getText(
                self,
                "Edit schematic highlighted net",
                "Net name (partial match):",
                QLineEdit.Normal,
                old_query,
            )
            if not ok:
                return
            new_query = str(new_query).strip()
            if not new_query:
                self.set_status("Schematic net query is empty")
                return
        if not self._schematic_net_query_matches_visible_signal(new_query):
            self.set_status(f"No visible net matched: {new_query}")
            return
        self.schematic_net_highlights[row]["query"] = new_query
        self._refresh_schematic_net_highlight_list()
        if hasattr(self, "schematic_net_highlight_list"):
            self.schematic_net_highlight_list.setCurrentRow(row)
        self._apply_schematic_zoom()
        self.set_status(f"Schematic net highlight renamed: {old_query} -> {new_query}")

    def choose_selected_schematic_net_highlight_color(self) -> None:
        if QColorDialog is object or QColor is object:
            self.set_status("Color picker is unavailable in this Qt build")
            return
        row = self._selected_schematic_net_highlight_index()
        if row < 0:
            self.set_status("No schematic net highlight selected")
            return
        current = self._normalize_color_hex(str(self.schematic_net_highlights[row].get("color", ""))) or "#d73a49"
        chosen = QColorDialog.getColor(QColor(current), self, "Choose schematic highlight color")
        if not hasattr(chosen, "isValid") or not chosen.isValid():
            return
        self.schematic_net_highlights[row]["color"] = str(chosen.name()).lower()
        self._refresh_schematic_net_highlight_list()
        self._apply_schematic_zoom()
        self.set_status(
            f"Schematic net highlight color updated: {self.schematic_net_highlights[row].get('query', '')}"
        )

    def clear_schematic_net_highlights(self) -> None:
        if not self.schematic_net_highlights:
            return
        self.schematic_net_highlights = []
        self._refresh_schematic_net_highlight_list()
        self._apply_schematic_zoom()
        self.set_status("All schematic net highlights cleared")

    def _on_schematic_net_highlight_double_click(self, item) -> None:
        if not hasattr(self, "schematic_net_highlight_list"):
            return
        row = int(self.schematic_net_highlight_list.row(item))
        if row < 0 or row >= len(self.schematic_net_highlights):
            return
        enabled = bool(self.schematic_net_highlights[row].get("enabled", True))
        self.schematic_net_highlights[row]["enabled"] = not enabled
        self._refresh_schematic_net_highlight_list()
        self.schematic_net_highlight_list.setCurrentRow(row)
        self._apply_schematic_zoom()
        self.set_status(
            f"Schematic net highlight {'enabled' if (not enabled) else 'disabled'}: "
            f"{self.schematic_net_highlights[row].get('query', '')}"
        )

    def _refresh_schematic_instance_hit_list(self) -> None:
        if not hasattr(self, "schematic_inst_hit_list"):
            return
        self.schematic_inst_hit_list.clear()
        for hit in self.schematic_instance_search_hits:
            self.schematic_inst_hit_list.addItem(
                f"{hit.get('name', '')} : {hit.get('module_name', '')}"
            )

    def _center_schematic_on_spot(self, spot: dict) -> None:
        if self.schematic_view_mode != "svg" or not hasattr(self, "schematic_scroll"):
            return
        src_w, src_h = self.schematic_svg_size
        if src_w <= 0.0 or src_h <= 0.0:
            return
        rendered_w = max(1, int(self.schematic_view.width()))
        rendered_h = max(1, int(self.schematic_view.height()))
        sx = float(rendered_w) / max(1.0, float(src_w))
        sy = float(rendered_h) / max(1.0, float(src_h))
        cx = (float(spot.get("x", 0.0)) + float(spot.get("w", 0.0)) / 2.0) * sx
        cy = (float(spot.get("y", 0.0)) + float(spot.get("h", 0.0)) / 2.0) * sy
        vp = self.schematic_scroll.viewport().size()
        hbar = self.schematic_scroll.horizontalScrollBar()
        vbar = self.schematic_scroll.verticalScrollBar()
        if hbar is not None:
            want = int(round(cx - vp.width() / 2.0))
            hbar.setValue(max(hbar.minimum(), min(hbar.maximum(), want)))
        if vbar is not None:
            want = int(round(cy - vp.height() / 2.0))
            vbar.setValue(max(vbar.minimum(), min(vbar.maximum(), want)))

    def _focus_schematic_instance_search_hit(self, index: int, sync_list: bool = True) -> None:
        if not self.schematic_instance_search_hits:
            return
        idx = index % len(self.schematic_instance_search_hits)
        self.schematic_instance_search_index = idx
        hit = self.schematic_instance_search_hits[idx]
        spot_idx = int(hit.get("spot_index", -1))
        if self._set_selected_schematic_spot(spot_idx):
            self._apply_schematic_zoom()
            self._center_schematic_on_spot(self.schematic_hotspots[spot_idx])
        if sync_list and hasattr(self, "schematic_inst_hit_list"):
            self.schematic_inst_hit_list.setCurrentRow(idx)
        self.set_status(
            f"Schematic instance hit {idx + 1}/{len(self.schematic_instance_search_hits)}: "
            f"{hit.get('name', '')}"
        )

    def run_schematic_instance_search(self) -> None:
        query = self.schematic_inst_search_entry.text().strip() if hasattr(self, "schematic_inst_search_entry") else ""
        if not query:
            self.set_status("Schematic instance regex is empty")
            return
        try:
            rx = re.compile(query, re.IGNORECASE)
        except re.error as e:
            self.set_status(f"Invalid schematic regex: {e}")
            return
        hits: List[dict] = []
        for idx, spot in enumerate(self.schematic_hotspots):
            if str(spot.get("node_kind", "")) != "instance":
                continue
            name = str(spot.get("instance_name", "")).strip()
            mod_name = str(spot.get("instance_module", "")).strip()
            if not name and not mod_name:
                continue
            if rx.search(name) or rx.search(mod_name):
                hits.append(
                    {
                        "spot_index": idx,
                        "name": name,
                        "module_name": mod_name,
                    }
                )
        self.schematic_instance_search_hits = hits
        self.schematic_instance_search_index = -1
        self._refresh_schematic_instance_hit_list()
        if not hits:
            self.set_status(f"Schematic instance regex hit: 0 ({query})")
            return
        self._focus_schematic_instance_search_hit(0)

    def focus_previous_schematic_instance_search_hit(self) -> None:
        if not self.schematic_instance_search_hits:
            self.set_status("No schematic instance search hits")
            return
        if self.schematic_instance_search_index < 0:
            self._focus_schematic_instance_search_hit(0)
            return
        self._focus_schematic_instance_search_hit(self.schematic_instance_search_index - 1)

    def focus_next_schematic_instance_search_hit(self) -> None:
        if not self.schematic_instance_search_hits:
            self.set_status("No schematic instance search hits")
            return
        if self.schematic_instance_search_index < 0:
            self._focus_schematic_instance_search_hit(0)
            return
        self._focus_schematic_instance_search_hit(self.schematic_instance_search_index + 1)

    def _on_schematic_instance_search_select(self) -> None:
        if not hasattr(self, "schematic_inst_hit_list"):
            return
        row = int(self.schematic_inst_hit_list.currentRow())
        if row < 0 or row >= len(self.schematic_instance_search_hits):
            return
        self._focus_schematic_instance_search_hit(row, sync_list=False)

    def _on_schematic_instance_search_jump(self, item) -> None:
        if not hasattr(self, "schematic_inst_hit_list"):
            return
        row = int(self.schematic_inst_hit_list.row(item))
        if row < 0 or row >= len(self.schematic_instance_search_hits):
            return
        self._focus_schematic_instance_search_hit(row, sync_list=False)

    def _apply_theme(self) -> None:
        c = self.ui_colors
        self.setStyleSheet(
            f"""
            QWidget {{ background: {c['bg_main']}; color: {c['fg_main']}; }}
            QLineEdit, QListWidget, QTreeWidget, QTextEdit {{
                background: {c['input_bg']}; color: {c['input_fg']}; border: 1px solid {c['border']};
            }}
            QPushButton {{
                background: {c['btn_bg']}; color: {c['input_fg']}; border: 1px solid {c['border']}; padding: 4px 10px;
            }}
            QPushButton:hover {{ background: {c['btn_hover']}; }}
            QLabel {{ color: {c['label_fg']}; }}
            QMenu {{
                background: {c['input_bg']};
                color: {c['input_fg']};
                border: 1px solid {c['border']};
            }}
            QMenu::item {{
                padding: 4px 22px 4px 22px;
                color: {c['input_fg']};
            }}
            QMenu::item:selected {{
                background: {c['btn_hover']};
                color: {c['input_fg']};
            }}
            QMenu::item:disabled {{
                color: {c['menu_disabled']};
                background: {c['input_bg']};
            }}
            """
        )

    def _load_startup_inputs(self) -> None:
        try:
            sess = self._load_session()
            self._restore_session_lists(sess)
            arg_filelists = self._arg_filelists()
            arg_rtl_files = self._arg_rtl_files()
            valid_arg_filelists = [p for p in arg_filelists if os.path.isfile(p)]
            valid_arg_rtl_files = [p for p in arg_rtl_files if os.path.isfile(p)]
            cli_dir = os.path.abspath(self.args.dir) if self.args.dir else ""
            has_cli_inputs = bool(arg_filelists or arg_rtl_files or cli_dir)
            sess_filelists = [os.path.abspath(x) for x in sess.get("filelists", []) if x]
            valid_sess_filelists = [p for p in sess_filelists if os.path.isfile(p)]
            rtl_dir = (
                cli_dir
                if cli_dir
                else os.path.abspath(sess.get("dir", "")) if sess.get("dir", "") else ""
            )
            wave = (
                os.path.abspath(self.args.wave)
                if getattr(self.args, "wave", "")
                else os.path.abspath(sess.get("wave", "")) if sess.get("wave", "") else ""
            )
            if has_cli_inputs:
                if valid_arg_filelists:
                    self.loaded_filelist_paths = valid_arg_filelists
                    self.loaded_filelist_path = valid_arg_filelists[0]
                    self.loaded_dir_path = ""
                    files, slang_args = self._read_multiple_filelists(valid_arg_filelists)
                    self._parse_files(files, slang_args)
                elif arg_filelists:
                    self.set_status(f"filelist not found: {arg_filelists[0]}")
                elif valid_arg_rtl_files:
                    self.loaded_filelist_paths = []
                    self.loaded_filelist_path = ""
                    self.loaded_dir_path = ""
                    self._parse_files(valid_arg_rtl_files, [])
                elif arg_rtl_files:
                    self.set_status(f"rtl-file not found: {arg_rtl_files[0]}")
                elif cli_dir and os.path.isdir(cli_dir):
                    self.loaded_dir_path = cli_dir
                    self.loaded_filelist_paths = []
                    self.loaded_filelist_path = ""
                    self._parse_files(discover_sv_files(cli_dir), [])
                elif cli_dir:
                    self.set_status(f"dir not found: {cli_dir}")
            else:
                if valid_sess_filelists:
                    self.loaded_filelist_paths = valid_sess_filelists
                    self.loaded_filelist_path = valid_sess_filelists[0]
                    self.loaded_dir_path = ""
                    files, slang_args = self._read_multiple_filelists(valid_sess_filelists)
                    self._parse_files(files, slang_args)
                elif rtl_dir and os.path.isdir(rtl_dir):
                    self.loaded_dir_path = rtl_dir
                    self.loaded_filelist_paths = []
                    self.loaded_filelist_path = ""
                    self._parse_files(discover_sv_files(rtl_dir), [])
                elif sess_filelists:
                    self.set_status(f"filelist not found: {sess_filelists[0]}")
                elif rtl_dir:
                    self.set_status(f"dir not found: {rtl_dir}")
            if wave and os.path.isfile(wave):
                self.loaded_wave_path = wave
                self.load_wave_file(wave)
            cfile = os.path.abspath(sess.get("current_file", "")) if sess.get("current_file") else ""
            cline = int(sess.get("current_line", 1)) if str(sess.get("current_line", "")).strip() else 1
            if (not has_cli_inputs) and cfile and os.path.isfile(cfile):
                self.show_file(cfile, cline)
            if self.schematic_prebuild_last_summary:
                self.set_status(self.schematic_prebuild_last_summary)
            self._schedule_startup_schematic_fail_notice()
        except Exception as e:
            self.set_status(f"startup load failed: {e}")

    def _schedule_startup_schematic_fail_notice(self) -> None:
        if self._startup_schematic_fail_notice_shown:
            return
        if not self.schematic_prebuild_fail_logs:
            return
        self._startup_schematic_fail_notice_shown = True
        try:
            QTimer.singleShot(0, self._show_startup_schematic_fail_notice)
        except Exception:
            pass

    def _show_startup_schematic_fail_notice(self) -> None:
        failed = sorted(self.schematic_prebuild_fail_logs.keys())
        if not failed:
            return
        preview = failed[:20]
        remains = max(0, len(failed) - len(preview))
        lines = []
        for key in preview:
            entry = self.schematic_cache_index.get(key, {})
            mod = str(entry.get("module_name", "") or "")
            lines.append(f" - {self._schematic_cache_label(key, mod)}")
        if remains > 0:
            lines.append(f" - ... ({remains} more)")
        text = (
            f"Schematic prebuild failed for {len(failed)} module(s).\n\n"
            + "\n".join(lines)
            + "\n\nSee Schematic tab log for details."
            + "\n(Use --schematic-prebuild-log-level phase|detail to show startup logs in UI.)"
        )
        try:
            QMessageBox.warning(
                self,
                "Schematic Prebuild Notice",
                text,
            )
        except Exception:
            pass

    def _start_wave_event_timer(self) -> None:
        self._wave_timer = QTimer(self)
        self._wave_timer.setInterval(250)
        self._wave_timer.timeout.connect(self._poll_wave_bridge_events)
        self._wave_timer.start()

    def _start_schematic_timer(self) -> None:
        self._schematic_timer = QTimer(self)
        self._schematic_timer.setInterval(200)
        self._schematic_timer.timeout.connect(self._poll_schematic_results)
        self._schematic_timer.start()

    def _start_rtl_structure_timer(self) -> None:
        self._rtl_structure_timer = QTimer(self)
        self._rtl_structure_timer.setInterval(200)
        self._rtl_structure_timer.timeout.connect(self._poll_rtl_structure_results)
        self._rtl_structure_timer.timeout.connect(self._poll_rtl_benchmark_results)
        self._rtl_structure_timer.start()

    def set_status(self, text: str) -> None:
        s = text or ""
        self.status_view.setPlainText(s)

    def _resolve_effective_editor_cmd(self) -> str:
        cli_template = getattr(self.args, "editor_cmd", None)
        if cli_template is not None:
            return str(cli_template)
        saved_template = load_editor_template()
        if saved_template:
            return saved_template
        from .app_cli import _default_editor_cmd_template

        return _default_editor_cmd_template()

    def _build_menu_bar(self) -> None:
        if QAction is object:
            return
        bar = self.menuBar()
        if bar is None:
            return
        tools_menu = bar.addMenu("Tools")
        self._editor_settings_action = QAction("Editor Settings...", self)
        self._editor_settings_action.triggered.connect(self.open_editor_settings_dialog)
        tools_menu.addAction(self._editor_settings_action)

    def open_editor_settings_dialog(self) -> None:
        if QDialog is object:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Editor Settings")
        dlg.setModal(True)
        layout = QVBoxLayout(dlg)

        available_keys = set(detect_available_presets())
        warning = QLabel("Warning: no preset editors found in PATH. Enter a custom command.")
        warning.setStyleSheet("color:#b00020; font-weight:600;")
        warning.setVisible(len(available_keys) == 0)
        layout.addWidget(warning)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:"))
        preset_combo = QComboBox(dlg)
        for key, meta in EDITOR_PRESETS.items():
            label = str(meta.get("label", key))
            state = "available" if key in available_keys else "not found"
            preset_combo.addItem(f"{label} ({state})", key)
        preset_row.addWidget(preset_combo, 1)
        layout.addLayout(preset_row)

        command_row = QHBoxLayout()
        command_row.addWidget(QLabel("Command:"))
        command_edit = QLineEdit(dlg)
        command_edit.setText(str(self._effective_editor_cmd or ""))
        command_row.addWidget(command_edit, 1)
        layout.addLayout(command_row)

        placeholders = QLabel("Placeholders: {file}  {line}  {basename}  {dir}")
        layout.addWidget(placeholders)

        buttons = QHBoxLayout()
        test_btn = QPushButton("Test", dlg)
        test_btn.setEnabled(bool(self.current_file))
        ok_btn = QPushButton("OK", dlg)
        cancel_btn = QPushButton("Cancel", dlg)
        buttons.addWidget(test_btn)
        buttons.addStretch(1)
        buttons.addWidget(ok_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        current_tpl = str(self._effective_editor_cmd or "")
        preset_templates = {k: str(v.get("template", "")) for k, v in EDITOR_PRESETS.items()}
        selected_key = "custom"
        for key, tpl in preset_templates.items():
            if key == "custom":
                continue
            if tpl == current_tpl:
                selected_key = key
                break
        preset_combo.blockSignals(True)
        for i in range(preset_combo.count()):
            if str(preset_combo.itemData(i)) == selected_key:
                preset_combo.setCurrentIndex(i)
                break
        preset_combo.blockSignals(False)

        def _on_preset_changed(index: int) -> None:
            key = str(preset_combo.itemData(index) or "")
            template = str(EDITOR_PRESETS.get(key, {}).get("template", ""))
            command_edit.setText(template)

        def _run_test() -> None:
            if not self.current_file:
                self.set_status("No source file selected")
                return
            line = self._line_from_source_cursor()
            cmd_tpl = str(command_edit.text())
            try:
                argv = build_editor_argv(cmd_tpl, self.current_file, line)
            except ValueError as e:
                QMessageBox.critical(self, "RTLens", f"invalid editor command template: {e}")
                return
            try:
                subprocess.Popen(argv, shell=False)
                self.set_status(f"Opened editor: {self.current_file}:{line}")
            except Exception as e:
                QMessageBox.critical(self, "RTLens", f"failed to start editor: {e}")

        def _accept() -> None:
            template = str(command_edit.text())
            try:
                save_editor_template(template)
            except Exception as e:
                QMessageBox.critical(self, "RTLens", f"failed to save editor config: {e}")
                return
            self._effective_editor_cmd = template
            self.set_status("Editor command updated")
            dlg.accept()

        preset_combo.currentIndexChanged.connect(_on_preset_changed)
        test_btn.clicked.connect(_run_test)
        ok_btn.clicked.connect(_accept)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def _session_path(self) -> Path:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")))
        return cfg / "rtlens" / "session_qt.json"

    def _shortcut_config_path(self) -> Path:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")))
        return cfg / "rtlens" / "shortcuts_qt.json"

    def _parse_qt_shortcut_values(self, value: object) -> List[str]:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            return [text]
        if isinstance(value, list):
            out: List[str] = []
            for it in value:
                if not isinstance(it, str):
                    continue
                text = it.strip()
                if text:
                    out.append(text)
            return out
        return []

    def _shortcut_values_to_keyseq_ints(self, values: List[str]) -> List[int]:
        out: List[int] = []
        for text in values:
            try:
                seq = QKeySequence(text)
            except Exception:
                self._qt_shortcut_notes.append(f"invalid sequence syntax: {text!r}")
                continue
            count = int(seq.count()) if hasattr(seq, "count") else 0
            if count <= 0:
                self._qt_shortcut_notes.append(f"ignored empty sequence: {text!r}")
                continue
            for i in range(count):
                code = self._keyseq_item_to_int(seq[i])
                if code and code not in out:
                    out.append(code)
        return out

    def _load_qt_shortcuts(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        self._qt_shortcut_notes = []
        for action, values in DEFAULT_QT_SHORTCUTS.items():
            out[action] = self._shortcut_values_to_keyseq_ints(list(values))
        path = self._shortcut_config_path()
        if not path.is_file():
            return out
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._qt_shortcut_notes.append(f"failed to read {path}: {e}")
            return out
        if not isinstance(data, dict):
            self._qt_shortcut_notes.append(f"ignored {path}: top-level JSON must be an object")
            return out
        for action in DEFAULT_QT_SHORTCUTS.keys():
            if action not in data:
                continue
            seq_values = self._parse_qt_shortcut_values(data.get(action))
            if not seq_values:
                out[action] = []
                self._qt_shortcut_notes.append(f"{action}: disabled by config")
                continue
            parsed = self._shortcut_values_to_keyseq_ints(seq_values)
            if parsed:
                out[action] = parsed
            else:
                self._qt_shortcut_notes.append(f"{action}: no valid shortcuts found in config")
        return out

    def _shortcut_combo_for_event(self, event) -> int:
        if Qt is object or not hasattr(event, "key"):
            return 0
        mods = self._enum_to_int(event.modifiers()) if hasattr(event, "modifiers") else 0
        key = self._enum_to_int(event.key())
        try:
            seq = QKeySequence(mods | key)
            if hasattr(seq, "count") and int(seq.count()) > 0:
                return self._keyseq_item_to_int(seq[0])
        except Exception:
            pass
        return 0

    def _enum_to_int(self, value) -> int:
        try:
            return int(value)
        except Exception:
            pass
        raw = getattr(value, "value", None)
        if raw is not None:
            try:
                return int(raw)
            except Exception:
                pass
        return 0

    def _keyseq_item_to_int(self, value) -> int:
        code = self._enum_to_int(value)
        if code:
            return code
        to_combined = getattr(value, "toCombined", None)
        if callable(to_combined):
            try:
                return int(to_combined())
            except Exception:
                pass
        return 0

    def _run_qt_shortcut_action(self, combo: int) -> bool:
        if not combo:
            return False
        action_by_combo: dict[int, str] = {}
        for action, seqs in self.qt_shortcuts.items():
            for seq in seqs:
                action_by_combo.setdefault(int(seq), action)
        action = action_by_combo.get(int(combo), "")
        if not action:
            return False
        if action == "reload_rtl":
            self.reload_rtl()
            return True
        if action == "reload_all":
            self.reload_all()
            return True
        if action == "reload_wave":
            self.reload_wave()
            return True
        return False

    def _load_session(self) -> dict:
        p = self._session_path()
        try:
            if not p.is_file():
                return {}
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_session(self) -> None:
        p = self._session_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            filelists = list(self.loaded_filelist_paths or ([self.loaded_filelist_path] if self.loaded_filelist_path else []))
            data = {
                "filelists": [x for x in filelists if x],
                "dir": self.loaded_dir_path or "",
                "wave": self.loaded_wave_path or "",
                "current_file": self.current_file or "",
                "current_line": int(self.current_line or 1),
                "bookmarks": [{"file": f, "line": int(l)} for f, l in self.bookmarks if f],
                "recent": [{"file": f, "line": int(l)} for f, l in self.recent_locs if f],
                "nav_history": [{"file": f, "line": int(l)} for f, l in self.nav_history if f],
                "nav_index": int(self.nav_index),
                "rtl_signal_highlights": list(self.rtl_signal_highlights),
                "rtl_show_edge_labels": bool(self.rtl_show_edge_labels),
                "schematic_show_net_labels": bool(self.schematic_show_net_labels),
                "schematic_show_net_colors": bool(self.schematic_show_net_colors),
                "schematic_net_highlights": list(self.schematic_net_highlights),
            }
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _restore_session_lists(self, data: dict) -> None:
        self.bookmarks = []
        for it in data.get("bookmarks", []):
            try:
                f = os.path.abspath(str(it.get("file", "")))
                l = int(it.get("line", 1))
            except Exception:
                continue
            if f:
                self.bookmarks.append((f, max(1, l)))
        self.recent_locs = []
        for it in data.get("recent", []):
            try:
                f = os.path.abspath(str(it.get("file", "")))
                l = int(it.get("line", 1))
            except Exception:
                continue
            if f:
                self.recent_locs.append((f, max(1, l)))
        self.nav_history = []
        for it in data.get("nav_history", []):
            try:
                f = os.path.abspath(str(it.get("file", "")))
                l = int(it.get("line", 1))
            except Exception:
                continue
            if f:
                self.nav_history.append((f, max(1, l)))
        try:
            ni = int(data.get("nav_index", len(self.nav_history) - 1))
        except Exception:
            ni = len(self.nav_history) - 1
        if self.nav_history:
            self.nav_index = max(0, min(ni, len(self.nav_history) - 1))
        else:
            self.nav_index = -1
        restored_highlights: List[dict] = []
        for it in data.get("rtl_signal_highlights", []) if isinstance(data.get("rtl_signal_highlights", []), list) else []:
            if not isinstance(it, dict):
                continue
            query = str(it.get("query", "")).strip()
            if not query:
                continue
            color = self._normalize_color_hex(str(it.get("color", ""))) or self._next_rtl_signal_highlight_color()
            enabled = bool(it.get("enabled", True))
            restored_highlights.append({"query": query, "color": color, "enabled": enabled})
        self.rtl_signal_highlights = restored_highlights
        self.rtl_show_edge_labels = bool(data.get("rtl_show_edge_labels", True))
        if hasattr(self, "rtl_edge_label_check"):
            self.rtl_edge_label_check.setChecked(self.rtl_show_edge_labels)
        # Keep schematic net labels default-off on startup for readability.
        # Users can enable it from the Net tab when needed.
        self.schematic_show_net_labels = False
        self.schematic_show_net_colors = bool(data.get("schematic_show_net_colors", True))
        restored_schematic_highlights: List[dict] = []
        for it in (
            data.get("schematic_net_highlights", [])
            if isinstance(data.get("schematic_net_highlights", []), list)
            else []
        ):
            if not isinstance(it, dict):
                continue
            query = str(it.get("query", "")).strip()
            if not query:
                continue
            color = self._normalize_color_hex(str(it.get("color", ""))) or self._next_schematic_net_highlight_color()
            enabled = bool(it.get("enabled", True))
            restored_schematic_highlights.append({"query": query, "color": color, "enabled": enabled})
        self.schematic_net_highlights = restored_schematic_highlights
        if hasattr(self, "schematic_net_label_check"):
            self.schematic_net_label_check.setChecked(self.schematic_show_net_labels)
        self._refresh_rtl_signal_highlight_list()
        self._refresh_schematic_net_highlight_list()
        self._refresh_nav_lists()
        self._update_nav_buttons()

    def _refresh_nav_lists(self) -> None:
        if hasattr(self, "bookmark_list"):
            self.bookmark_list.clear()
            for f, l in self.bookmarks:
                self.bookmark_list.addItem(f"{f}:{l}")
        if hasattr(self, "recent_list"):
            self.recent_list.clear()
            for f, l in self.recent_locs:
                self.recent_list.addItem(f"{f}:{l}")

    def _bookmark_lines_for_current_file(self) -> set[int]:
        if not self.current_file:
            return set()
        cur = os.path.abspath(self.current_file)
        return {int(l) for f, l in self.bookmarks if os.path.abspath(f) == cur and int(l) > 0}

    def _append_recent(self, path: str, line: int) -> None:
        f = os.path.abspath(path)
        l = max(1, int(line))
        item = (f, l)
        self.recent_locs = [x for x in self.recent_locs if x != item]
        self.recent_locs.insert(0, item)
        if len(self.recent_locs) > self.max_recent:
            self.recent_locs = self.recent_locs[: self.max_recent]
        self._refresh_nav_lists()

    def add_bookmark_current(self) -> None:
        if not self.current_file:
            self.set_status("No source file selected")
            return
        line = self._line_from_source_cursor()
        item = (os.path.abspath(self.current_file), max(1, int(line)))
        if item in self.bookmarks:
            self.set_status(f"Bookmark exists: {item[0]}:{item[1]}")
            return
        self.bookmarks.append(item)
        self._refresh_nav_lists()
        self.right_tabs.setCurrentIndex(4)
        self.show_file(item[0], item[1], record_history=False)
        self.set_status(f"Bookmarked: {item[0]}:{item[1]}")

    def remove_selected_bookmark(self) -> None:
        row = self.bookmark_list.currentRow() if hasattr(self, "bookmark_list") else -1
        if row < 0 or row >= len(self.bookmarks):
            return
        item = self.bookmarks.pop(row)
        self._refresh_nav_lists()
        if self.current_file and os.path.abspath(self.current_file) == os.path.abspath(item[0]):
            self.show_file(self.current_file, self.current_line, record_history=False)
        self.set_status(f"Removed bookmark: {item[0]}:{item[1]}")

    def clear_recent(self) -> None:
        self.recent_locs.clear()
        self._refresh_nav_lists()
        self.set_status("Recent list cleared")

    def on_bookmark_jump(self, item) -> None:
        row = self.bookmark_list.row(item)
        if row < 0 or row >= len(self.bookmarks):
            return
        f, l = self.bookmarks[row]
        self.show_file(f, l)
        self.right_tabs.setCurrentIndex(0)

    def on_recent_jump(self, item) -> None:
        row = self.recent_list.row(item)
        if row < 0 or row >= len(self.recent_locs):
            return
        f, l = self.recent_locs[row]
        self.show_file(f, l)
        self.right_tabs.setCurrentIndex(0)

    def _update_nav_buttons(self) -> None:
        if not hasattr(self, "source_back_btn"):
            return
        self.source_back_btn.setEnabled(self.nav_index > 0)
        can_fwd = self.nav_index >= 0 and self.nav_index < len(self.nav_history) - 1
        self.source_forward_btn.setEnabled(can_fwd)

    def _push_nav(self, path: str, line: int) -> None:
        state = (os.path.abspath(path), max(1, int(line)))
        if self.nav_index >= 0 and self.nav_index < len(self.nav_history) and self.nav_history[self.nav_index] == state:
            return
        if self.nav_index < len(self.nav_history) - 1:
            self.nav_history = self.nav_history[: self.nav_index + 1]
        self.nav_history.append(state)
        self.nav_index = len(self.nav_history) - 1
        self._update_nav_buttons()

    def go_back(self) -> None:
        if self.nav_index <= 0:
            return
        self.nav_index -= 1
        path, line = self.nav_history[self.nav_index]
        self.show_file(path, line, record_history=False)
        self._update_nav_buttons()

    def go_forward(self) -> None:
        if self.nav_index < 0 or self.nav_index >= len(self.nav_history) - 1:
            return
        self.nav_index += 1
        path, line = self.nav_history[self.nav_index]
        self.show_file(path, line, record_history=False)
        self._update_nav_buttons()

    def _extra_slang_args_from_cli(self) -> List[str]:
        out: List[str] = []
        if getattr(self.args, "timescale", ""):
            out.extend(["--timescale", self.args.timescale])
        if getattr(self.args, "slang_arg", None):
            out.extend(self.args.slang_arg)
        if getattr(self.args, "slang_opts", ""):
            out.extend(shlex.split(self.args.slang_opts))
        return out

    def _arg_filelists(self) -> List[str]:
        raw = getattr(self.args, "filelist", [])
        if isinstance(raw, str):
            vals = [raw] if raw else []
        else:
            vals = [x for x in raw if x]
        return [os.path.abspath(x) for x in vals]

    def _arg_rtl_files(self) -> List[str]:
        raw = getattr(self.args, "rtl_file", [])
        if isinstance(raw, str):
            vals = [raw] if raw else []
        else:
            vals = [x for x in raw if x]
        return [os.path.abspath(x) for x in vals]

    def _read_multiple_filelists(self, paths: List[str]) -> tuple[List[str], List[str]]:
        all_files: List[str] = []
        all_args: List[str] = []
        seen_files = set()
        for path in paths:
            files, slang_args = read_filelist_with_args(path)
            for f in files:
                af = os.path.abspath(f)
                if af in seen_files:
                    continue
                seen_files.add(af)
                all_files.append(af)
            all_args.extend(slang_args)
        return all_files, all_args

    def _parse_files(self, files: List[str], slang_args: Optional[List[str]] = None) -> None:
        self.loaded_rtl_files = [os.path.abspath(f) for f in files]
        merged_slang_args = list(slang_args or []) + self._extra_slang_args_from_cli()
        self.loaded_slang_args = merged_slang_args
        self.schematic_dirty = True
        self.schematic_result = None
        self.schematic_module_name = ""
        self.rtl_structure_dirty = True
        self.rtl_structure_module_name = ""
        self.rtl_structure_svg_bytes = b""
        self.rtl_structure_png_bytes = b""
        self.rtl_structure_elk_layout = None
        self.rtl_structure_node_boxes = []
        if hasattr(self, "schematic_log"):
            try:
                self.schematic_log.setPlainText("Schematic will regenerate when the Schematic tab is opened.")
            except RuntimeError:
                pass
        if hasattr(self, "schematic_info_label"):
            try:
                self.schematic_info_label.setText("Schematic pending refresh.")
            except RuntimeError:
                pass
        if hasattr(self, "rtl_log"):
            try:
                self.rtl_log.setPlainText("RTL structure will regenerate when the RTL Structure tab is opened.")
            except RuntimeError:
                pass
        if hasattr(self, "rtl_info_label"):
            try:
                self.rtl_info_label.setText("RTL structure pending refresh.")
            except RuntimeError:
                pass
        parse_t0 = time.perf_counter()
        if self.schematic_tab_enabled:
            self._schematic_prebuild_emit_progress(
                f"[rtlens] startup parse start files={len(files)} slang_args={len(merged_slang_args)}"
            )
        try:
            self.design, self.connectivity, self.compile_log = load_design_with_slang(files, self.args.top, merged_slang_args)
            if self.schematic_tab_enabled:
                self._schematic_prebuild_emit_progress(
                    f"[rtlens] startup parse end elapsed={max(0.0, time.perf_counter() - parse_t0):.1f}s"
                )
            self._merge_parser_structure_into_design(files)
            self._refresh_compile_log_view()
            self.refresh_hierarchy()
            self._prebuild_schematic_cache_if_enabled()
            status = f"Parsed by slang: modules={len(self.design.modules)} hier={len(self.design.hier)}"
            if self.schematic_prebuild_last_summary:
                status += f" | {self.schematic_prebuild_last_summary}"
            self.set_status(status)
            return
        except SlangBackendError as e:
            self.compile_log = str(e)
            self._refresh_compile_log_view()
            self.set_status(f"slang backend unavailable, fallback parser used: {e}")
            if self.schematic_tab_enabled:
                self._schematic_prebuild_emit_progress(
                    f"[rtlens] startup parse failed after {max(0.0, time.perf_counter() - parse_t0):.1f}s: {e}"
                )

        self.design = parse_sv_files(files, defined_macros=self._defined_macros())
        build_hierarchy(self.design, self.args.top)
        self.connectivity = build_connectivity(self.design)
        self.compile_log += (
            "\n\n[rtlens] fallback parser summary\n"
            f"modules={len(self.design.modules)} hier={len(self.design.hier)} files={len(files)}"
        )
        self._refresh_compile_log_view()
        self.refresh_hierarchy()
        self._prebuild_schematic_cache_if_enabled()
        status = (
            f"Parsed by fallback parser: modules={len(self.design.modules)} "
            f"hier={len(self.design.hier)} files={len(files)}"
        )
        if self.schematic_prebuild_last_summary:
            status += f" | {self.schematic_prebuild_last_summary}"
        self.set_status(status)

    def _merge_parser_structure_into_design(self, files: List[str]) -> None:
        parsed = parse_sv_files(files, defined_macros=self._defined_macros())
        merged = 0
        added = 0
        line_range_patched = 0

        def _should_update_line_range(
            existing_start: int,
            existing_end: int,
            parsed_start: int,
            parsed_end: int,
        ) -> bool:
            if parsed_start <= 0 or parsed_end < parsed_start:
                return False
            if existing_start <= 0 or existing_end < existing_start:
                return True
            existing_span = existing_end - existing_start + 1
            parsed_span = parsed_end - parsed_start + 1
            # slang can report module range as declaration-only (for example 1/1),
            # which drops callable extraction that depends on real line coverage.
            if existing_span <= 1 and parsed_span > existing_span:
                return True
            return False

        for mod_name, parsed_mod in parsed.modules.items():
            existing = self.design.modules.get(mod_name)
            if existing is None:
                self.design.modules[mod_name] = parsed_mod
                added += 1
                continue
            if not existing.file and parsed_mod.file:
                existing.file = parsed_mod.file
            if _should_update_line_range(
                existing_start=existing.start_line,
                existing_end=existing.end_line,
                parsed_start=parsed_mod.start_line,
                parsed_end=parsed_mod.end_line,
            ):
                existing.start_line = parsed_mod.start_line
                existing.end_line = parsed_mod.end_line
                line_range_patched += 1
            if not existing.ports and parsed_mod.ports:
                existing.ports = dict(parsed_mod.ports)
            if not existing.signals and parsed_mod.signals:
                existing.signals = dict(parsed_mod.signals)
            if not existing.instances and parsed_mod.instances:
                existing.instances = list(parsed_mod.instances)
            if not existing.assignments and parsed_mod.assignments:
                existing.assignments = list(parsed_mod.assignments)
            if not getattr(existing, "always_blocks", None) and getattr(parsed_mod, "always_blocks", None):
                existing.always_blocks = list(parsed_mod.always_blocks)
            merged += 1
        self.compile_log += (
            "\n\n[rtlens] rtl structure merge\n"
            f"merged_modules={merged} added_modules={added} parsed_modules={len(parsed.modules)} "
            f"line_range_patched={line_range_patched}"
        )

    def _refresh_compile_log_view(self) -> None:
        self.compile_log_text.setPlainText(self.compile_log)

    def _design_search_files(self) -> List[str]:
        candidates: List[str] = []
        for f in self.loaded_rtl_files:
            af = os.path.abspath(f)
            if os.path.isfile(af):
                candidates.append(af)
        for mod in self.design.modules.values():
            af = os.path.abspath(mod.file)
            if os.path.isfile(af):
                candidates.append(af)
        deduped, _stats = dedupe_existing_files_canonical(candidates)
        return deduped

    def _schematic_cache_root(self) -> Path:
        return Path(os.path.expanduser("~/.cache/rtlens/schematic_prebuild"))

    def _schematic_cache_index_path(self) -> Path:
        return self._schematic_cache_root() / "index.json"

    def _load_schematic_cache_index(self) -> dict[str, dict]:
        p = self._schematic_cache_index_path()
        try:
            if not p.is_file():
                return {}
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            out: dict[str, dict] = {}
            for mod, entry in data.items():
                if isinstance(mod, str) and isinstance(entry, dict):
                    out[mod] = dict(entry)
            return out
        except Exception:
            return {}

    def _save_schematic_cache_index(self) -> None:
        p = self._schematic_cache_index_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("w", encoding="utf-8") as f:
                json.dump(self.schematic_cache_index, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except Exception:
            pass

    def _schematic_failed_at_sec(self, entry: dict) -> float:
        if not isinstance(entry, dict):
            return 0.0
        raw = entry.get("failed_at", 0.0)
        try:
            return max(0.0, float(raw))
        except Exception:
            return 0.0

    def _schematic_failure_ttl_remaining_sec(self, cache_key: str) -> float:
        ttl = max(0, int(self.schematic_prebuild_fail_ttl_sec))
        if ttl <= 0:
            return 0.0
        entry = self.schematic_cache_index.get(cache_key)
        if not isinstance(entry, dict):
            return 0.0
        if str(entry.get("status", "")).strip().lower() != "fail":
            return 0.0
        failed_at = self._schematic_failed_at_sec(entry)
        if failed_at <= 0.0:
            return 0.0
        remain = float(ttl) - (time.time() - failed_at)
        return max(0.0, remain)

    def _record_schematic_failure_entry(
        self,
        module_name: str,
        fingerprint: str,
        reason: str,
        error_head: str,
    ) -> None:
        self.schematic_cache_index[module_name] = {
            "status": "fail",
            "fingerprint": str(fingerprint or ""),
            "failed_at": float(time.time()),
            "fail_reason": str(reason or "other"),
            "fail_error_head": str(error_head or "")[:400],
            "svg_path": "",
            "html_path": "",
            "json_path": "",
        }

    def _clear_schematic_failure_entry(self, module_name: str) -> None:
        entry = self.schematic_cache_index.get(module_name)
        if not isinstance(entry, dict):
            return
        if str(entry.get("status", "")).strip().lower() != "fail":
            return
        self.schematic_cache_index.pop(module_name, None)

    def _safe_cache_name(self, value: str, fallback: str = "module") -> str:
        raw = str(value or "").strip()
        cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw).strip("._")
        return cleaned or fallback

    def _schematic_cache_key_for_hier_path(self, hier_path: str) -> str:
        path = str(hier_path or "").strip()
        return f"hier:{path}" if path else ""

    def _schematic_hier_path_from_cache_key(self, cache_key: str) -> str:
        key = str(cache_key or "").strip()
        if key.startswith("hier:"):
            return key[len("hier:") :]
        return ""

    def _schematic_cache_label(self, cache_key: str, module_name: str = "") -> str:
        path = self._schematic_hier_path_from_cache_key(cache_key)
        if path:
            node = self.design.hier.get(path)
            mod = str((node.module_name if node else "") or module_name or "").strip()
            if mod:
                return f"{mod} @ {path}"
            return path
        mod = str(module_name or "").strip()
        if mod:
            return mod
        return str(cache_key or "(unknown)")

    def _resolve_schematic_prebuild_seed_paths(self) -> List[str]:
        target = (self.schematic_prebuild_top or "").strip()
        if not target:
            return []
        if target in self.design.hier:
            return [target]
        if target in self.design.modules:
            return sorted([path for path, node in self.design.hier.items() if node.module_name == target])
        if target == (self.design.top_module or ""):
            return list(self.design.roots)
        return []

    def _collect_schematic_prebuild_paths(self, module_scope: Set[str]) -> List[str]:
        seed_paths = self._resolve_schematic_prebuild_seed_paths()
        if not seed_paths:
            return []
        out: List[str] = []
        seen: Set[str] = set()
        for seed in seed_paths:
            prefix = seed + "."
            for path, node in sorted(self.design.hier.items()):
                if path != seed and not path.startswith(prefix):
                    continue
                mod_name = str(node.module_name or "").strip()
                if module_scope and mod_name not in module_scope:
                    continue
                if path in seen:
                    continue
                seen.add(path)
                out.append(path)
        return out

    def _relative_instance_chain_for_prebuild_top(self, hier_path: str, top_module: str) -> Optional[List[str]]:
        path = str(hier_path or "").strip()
        top_mod = str(top_module or "").strip()
        if not path or not top_mod:
            return None

        # First, anchor on explicit prebuild seed path(s). This is robust even
        # when intermediate generated blocks are not present as hierarchy nodes.
        best_seed = ""
        for seed in self._resolve_schematic_prebuild_seed_paths():
            s = str(seed or "").strip()
            if not s:
                continue
            if path == s or path.startswith(s + "."):
                if len(s) > len(best_seed):
                    best_seed = s
        if best_seed:
            suffix = path[len(best_seed) :].lstrip(".")
            if not suffix:
                return []
            return [seg for seg in suffix.split(".") if seg]

        # Fallback: find the deepest hierarchy prefix whose module matches the
        # requested top module, then derive relative path segments by string.
        parts = [seg for seg in path.split(".") if seg]
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            node = self.design.hier.get(prefix)
            if node is None:
                continue
            if str(node.module_name or "").strip() != top_mod:
                continue
            return parts[i:]
        return None

    def _schematic_top_session_fingerprint(self, top_module: str, files: Iterable[str]) -> str:
        h = hashlib.sha1()
        h.update(str(top_module or "").encode("utf-8", errors="ignore"))
        for path in sorted({os.path.abspath(f) for f in files if f and os.path.isfile(f)}):
            h.update(b"\0")
            h.update(path.encode("utf-8", errors="ignore"))
            try:
                h.update(Path(path).read_bytes())
            except Exception:
                pass
        for tok in self.loaded_slang_args:
            h.update(b"\0")
            h.update(str(tok).encode("utf-8", errors="ignore"))
        resolved_sv2v_cmd, _sv2v_source = self._resolve_sv2v_cmd()
        for tok in (
            str(getattr(self.args, "yosys_cmd", "yosys")),
            str(getattr(self.args, "netlistsvg_cmd", "netlistsvg")),
            str(resolved_sv2v_cmd),
            str(getattr(self.args, "netlistsvg_dir", "") or ""),
        ):
            h.update(b"\0")
            h.update(tok.encode("utf-8", errors="ignore"))
        return h.hexdigest()

    def _schematic_instance_fingerprint(
        self,
        cache_key: str,
        module_name: str,
        top_session_fingerprint: str,
    ) -> str:
        h = hashlib.sha1()
        h.update(str(cache_key or "").encode("utf-8", errors="ignore"))
        h.update(b"\0")
        h.update(str(module_name or "").encode("utf-8", errors="ignore"))
        h.update(b"\0")
        h.update(str(top_session_fingerprint or "").encode("utf-8", errors="ignore"))
        return h.hexdigest()

    def _schematic_module_fingerprint(self, module_name: str) -> str:
        h = hashlib.sha1()
        h.update(module_name.encode("utf-8", errors="ignore"))
        mod = self.design.modules.get(module_name)
        if mod and os.path.isfile(mod.file):
            try:
                h.update(Path(mod.file).read_bytes())
            except Exception:
                h.update(mod.file.encode("utf-8", errors="ignore"))
            h.update(str(mod.start_line).encode("utf-8"))
            h.update(str(mod.end_line).encode("utf-8"))
        for tok in self.loaded_slang_args:
            h.update(b"\0")
            h.update(str(tok).encode("utf-8", errors="ignore"))
        resolved_sv2v_cmd, _sv2v_source = self._resolve_sv2v_cmd()
        for tok in (
            str(getattr(self.args, "yosys_cmd", "yosys")),
            str(getattr(self.args, "netlistsvg_cmd", "netlistsvg")),
            str(resolved_sv2v_cmd),
        ):
            h.update(b"\0")
            h.update(tok.encode("utf-8", errors="ignore"))
        return h.hexdigest()

    def _schematic_prebuild_emit_progress(self, message: str, detail: bool = False) -> None:
        level = str(self.schematic_prebuild_log_level or "phase").strip().lower()
        if level not in {"off", "phase", "detail"}:
            level = "phase"
        if level == "off":
            # Off keeps UI debug log hidden, but startup progress stays visible on stdout.
            if detail:
                return
        elif detail and level != "detail":
            return
        try:
            print(message, flush=True)
        except Exception:
            pass

    def _format_schematic_log_for_ui(self, log_text: str, error_text: str = "") -> str:
        level = str(self.schematic_prebuild_log_level or "off").strip().lower()
        if level not in {"off", "phase", "detail"}:
            level = "off"
        if level == "detail":
            return str(log_text or "").strip()
        lines = [str(x).rstrip() for x in str(log_text or "").splitlines()]
        if level == "phase":
            trimmed = lines[:220]
            return "\n".join(trimmed).strip()
        keep: List[str] = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if (
                s.startswith("[rtlens]")
                or low.startswith("module:")
                or low.startswith("reason:")
                or low.startswith("error:")
                or "cache hit" in low
                or "cache miss" in low
                or "timeout" in low
                or "failed" in low
            ):
                keep.append(s)
        if error_text:
            lines = str(error_text or "").splitlines()
            head = lines[0].strip() if lines else ""
            if head:
                keep.append(f"error: {head}")
        if not keep:
            return "Schematic log is hidden (use --schematic-prebuild-log-level phase|detail)."
        return "\n".join(keep[:80]).strip()

    def _update_schematic_log_visibility(self) -> None:
        level = str(self.schematic_prebuild_log_level or "off").strip().lower()
        if level not in {"off", "phase", "detail"}:
            level = "off"
        visible = level == "detail"
        if hasattr(self, "schematic_log"):
            self.schematic_log.setVisible(visible)

    def _set_schematic_log_text(self, log_text: str, error_text: str = "") -> None:
        if not hasattr(self, "schematic_log"):
            return
        self.schematic_log.setPlainText(self._format_schematic_log_for_ui(log_text, error_text))

    def _schematic_prebuild_is_timeout(self, error_text: str, log_text: str = "") -> bool:
        blob = f"{error_text}\n{log_text}".lower()
        return "timeout after" in blob or " timeout\n" in blob

    def _resolve_sv2v_cmd(self) -> tuple[str, str]:
        candidates: List[tuple[str, str]] = []
        cli = str(getattr(self.args, "sv2v_cmd", "") or "").strip()
        if cli:
            candidates.append(("cli", cli))
        env = str(os.environ.get("SV2V_CMD", "") or "").strip()
        if env:
            candidates.append(("env:SV2V_CMD", env))
        in_path = shutil.which("sv2v")
        if in_path:
            candidates.append(("PATH", in_path))

        seen: Set[str] = set()
        for source, raw in candidates:
            cmd = str(raw or "").strip()
            if not cmd or cmd in seen:
                continue
            seen.add(cmd)
            expanded = os.path.expanduser(cmd)
            has_sep = (os.path.sep in expanded) or (os.path.altsep is not None and os.path.altsep in expanded)
            if has_sep:
                if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                    return expanded, source
                continue
            resolved = shutil.which(expanded)
            if resolved:
                return str(resolved), source
        return "", "none"

    def _classify_schematic_prebuild_failure(self, error_text: str, log_text: str = "") -> str:
        err = str(error_text or "").lower()
        log = str(log_text or "").lower()
        blob = err + "\n" + log
        if "copy prebuilt artifacts" in blob:
            return "cache_copy"
        if "sv2v timeout after" in blob:
            return "sv2v_timeout"
        if "yosys timeout after" in blob:
            return "yosys_timeout"
        if "netlistsvg timeout after" in blob:
            return "netlistsvg_timeout"
        if "timeout after" in blob:
            return "timeout"
        if "is not part of the design" in blob:
            return "module_missing"
        if "module not found in synthesized top netlist" in blob:
            return "module_missing"
        if "does not have a port named" in blob or "does not have a parameter named" in blob:
            return "stub_mismatch"
        if "netlistsvg" in err and ("failed" in err or "launch failed" in err):
            return "netlistsvg"
        if (
            "sv2v failed before yosys" in err
            or err.startswith("sv2v ")
            or "sv2v launch failed" in err
            or "sv2v command:" in log and "sv2v return code: 0" not in log
        ):
            return "sv2v"
        if (
            "yosys frontend does not support" in err
            or ("yosys" in err and ("failed" in err or "launch failed" in err))
            or "unsupported constructs detected before yosys run" in log
        ):
            return "yosys"
        if "netlistsvg command:" in log and "netlistsvg stderr:" in log:
            return "netlistsvg"
        if "netlistsvg" in blob:
            return "netlistsvg"
        return "other"

    def _collect_schematic_prebuild_modules(self) -> List[str]:
        target = (self.schematic_prebuild_top or "").strip()
        if not target:
            return []
        resolved_top = self._resolve_schematic_prebuild_top_module()
        if resolved_top:
            files = self._design_search_files()
            if files:
                try:
                    parsed = parse_sv_files(files, defined_macros=self._extract_defined_macros_from_slang_args())
                    if resolved_top in parsed.modules:
                        closure = sorted(self._module_closure_from_parsed_db(parsed, resolved_top))
                        if closure:
                            if resolved_top not in closure:
                                closure.insert(0, resolved_top)
                            return closure
                except Exception:
                    pass
        seed_paths: List[str] = []
        if target in self.design.hier:
            seed_paths = [target]
        elif target in self.design.modules:
            for path, node in self.design.hier.items():
                if node.module_name == target:
                    seed_paths.append(path)
        elif target == (self.design.top_module or ""):
            seed_paths = list(self.design.roots)
        if not seed_paths:
            return []
        mods: List[str] = []
        seen_mods = set()
        for seed in seed_paths:
            prefix = seed + "."
            for path, node in sorted(self.design.hier.items()):
                if path != seed and not path.startswith(prefix):
                    continue
                mod_name = str(node.module_name or "").strip()
                if not mod_name or mod_name in seen_mods:
                    continue
                seen_mods.add(mod_name)
                mods.append(mod_name)
        return mods

    def _extract_defined_macros_from_slang_args(self) -> Set[str]:
        out: Set[str] = set()
        items = list(self.loaded_slang_args or [])
        i = 0
        while i < len(items):
            tok = str(items[i] or "")
            if tok.startswith("+define+"):
                for d in tok.split("+")[2:]:
                    key = str(d).split("=", 1)[0].strip()
                    if key:
                        out.add(key)
            elif tok == "-D" and i + 1 < len(items):
                key = str(items[i + 1] or "").split("=", 1)[0].strip()
                if key:
                    out.add(key)
                i += 1
            elif tok.startswith("-D") and len(tok) > 2:
                key = tok[2:].split("=", 1)[0].strip()
                if key:
                    out.add(key)
            i += 1
        return out

    def _module_closure_from_parsed_db(self, parsed, top_module: str) -> Set[str]:
        if parsed is None or top_module not in parsed.modules:
            return {top_module} if top_module else set()
        out: Set[str] = set()
        stack: List[str] = [top_module]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            mod = parsed.modules.get(cur)
            if not mod:
                continue
            for inst in mod.instances:
                nxt = str(inst.module_type or "").strip()
                if nxt in parsed.modules and nxt not in out:
                    stack.append(nxt)
        return out

    def _resolve_schematic_prebuild_top_module(self) -> str:
        target = (self.schematic_prebuild_top or "").strip()
        if not target:
            return ""
        if target in self.design.hier:
            node = self.design.hier.get(target)
            return str((node.module_name if node else "") or "").strip()
        if target in self.design.modules:
            return target
        if target == (self.design.top_module or ""):
            return target
        return ""

    def _cached_schematic_result_for_key(self, cache_key: str, module_name: str = "") -> Optional[NetlistSvgResult]:
        if not self.schematic_cache_index:
            self.schematic_cache_index = self._load_schematic_cache_index()
        entry = self.schematic_cache_index.get(cache_key)
        if not isinstance(entry, dict):
            return None
        status = str(entry.get("status", "ok")).strip().lower()
        if status == "fail":
            return None
        svg_path = str(entry.get("svg_path", ""))
        html_path = str(entry.get("html_path", ""))
        if not svg_path or not html_path or not os.path.isfile(svg_path) or not os.path.isfile(html_path):
            return None
        expected = str(entry.get("fingerprint", "") or "")
        cached_fp = str(entry.get("fingerprint", ""))
        if not expected or cached_fp != expected:
            return None
        label = self._schematic_cache_label(cache_key, module_name or str(entry.get("module_name", "")))
        return NetlistSvgResult(
            module_name=cache_key,
            html_path=html_path,
            svg_path=svg_path,
            json_path=str(entry.get("json_path", "")),
            log=(
                "[rtlens] schematic prebuild cache hit\n"
                f"key: {cache_key}\n"
                f"module: {label}\n"
                f"fingerprint: {expected}\n"
                f"html: {html_path}\n"
                f"svg: {svg_path}\n"
            ),
            error="",
        )

    def _drop_stale_schematic_cache_entry(self, module_name: str) -> bool:
        if not self.schematic_cache_index:
            self.schematic_cache_index = self._load_schematic_cache_index()
        entry = self.schematic_cache_index.pop(module_name, None)
        if not isinstance(entry, dict):
            return False
        cache_root = self._schematic_cache_root()
        try:
            cache_root_resolved = cache_root.resolve()
        except Exception:
            cache_root_resolved = cache_root
        for key in ("svg_path", "html_path", "json_path"):
            path_text = str(entry.get(key, "") or "").strip()
            if not path_text:
                continue
            try:
                p = Path(path_text).resolve()
                if cache_root_resolved not in p.parents:
                    continue
                if p.is_file():
                    p.unlink()
            except Exception:
                continue
        return True

    def _build_schematic_cache_miss_detail(self, cache_key: str, module_name: str = "") -> str:
        if not self.schematic_cache_index:
            self.schematic_cache_index = self._load_schematic_cache_index()
        entry = self.schematic_cache_index.get(cache_key)
        hier_path = self._schematic_hier_path_from_cache_key(cache_key)
        label = self._schematic_cache_label(cache_key, module_name)
        in_scope = hier_path in (self.schematic_prebuild_paths_last or set()) if hier_path else False
        scoped = len(self.schematic_prebuild_paths_last or set())
        lines = [
            "[rtlens] schematic prebuild cache miss detail",
            f"key: {cache_key}",
            f"module: {label}",
            f"prebuild target: {self.schematic_prebuild_top}",
            f"in prebuild instance scope: {'yes' if in_scope else 'no'}",
            f"prebuild instance count: {scoped}",
        ]
        if not isinstance(entry, dict):
            lines.append("reason: no cache entry in index")
            return "\n".join(lines)
        status = str(entry.get("status", "ok")).strip().lower()
        if status == "fail":
            reason = str(entry.get("fail_reason", "other") or "other")
            failed_at = self._schematic_failed_at_sec(entry)
            failed_at_text = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(failed_at))
                if failed_at > 0.0
                else "(unknown)"
            )
            ttl_remain = self._schematic_failure_ttl_remaining_sec(cache_key)
            lines.extend(
                [
                    "reason: previous prebuild failure",
                    f"fail_reason: {reason}",
                    f"fail_error_head: {str(entry.get('fail_error_head', '') or '(none)')}",
                    f"failed_at: {failed_at_text}",
                    f"ttl_remaining_sec: {int(round(ttl_remain))}",
                ]
            )
            return "\n".join(lines)
        svg_path = str(entry.get("svg_path", "") or "")
        html_path = str(entry.get("html_path", "") or "")
        svg_ok = bool(svg_path and os.path.isfile(svg_path))
        html_ok = bool(html_path and os.path.isfile(html_path))
        if not svg_ok or not html_ok:
            lines.extend(
                [
                    "reason: cached artifact missing",
                    f"svg_path: {svg_path or '(none)'}",
                    f"html_path: {html_path or '(none)'}",
                ]
            )
            return "\n".join(lines)
        expected = str(entry.get("fingerprint", "") or "")
        cached_fp = str(entry.get("fingerprint", ""))
        if not expected or cached_fp != expected:
            lines.extend(
                [
                    "reason: fingerprint mismatch",
                    f"cached: {cached_fp}",
                    f"expected: {expected}",
                ]
            )
            return "\n".join(lines)
        lines.append("reason: cache miss (unknown)")
        return "\n".join(lines)

    def _prebuild_schematic_cache_if_enabled(self) -> None:
        if not self.schematic_tab_enabled:
            self.schematic_prebuild_last_summary = ""
            self.schematic_prebuild_modules_last = set()
            self.schematic_prebuild_paths_last = set()
            return
        prebuild_t0 = time.perf_counter()
        self.schematic_prebuild_fail_logs = {}
        modules = self._collect_schematic_prebuild_modules()
        module_scope = set(modules)
        paths = self._collect_schematic_prebuild_paths(module_scope)
        self.schematic_prebuild_modules_last = set(modules)
        self.schematic_prebuild_paths_last = set(paths)
        prebuild_top_module = self._resolve_schematic_prebuild_top_module()
        if not modules or not paths:
            self.schematic_prebuild_last_summary = f"Schematic prebuild: no target for '{self.schematic_prebuild_top}'"
            self.set_status(self.schematic_prebuild_last_summary)
            self._schematic_prebuild_emit_progress(f"[rtlens] {self.schematic_prebuild_last_summary}")
            return
        if not prebuild_top_module:
            self.schematic_prebuild_last_summary = (
                f"Schematic prebuild: unable to resolve top module for '{self.schematic_prebuild_top}'"
            )
            self.set_status(self.schematic_prebuild_last_summary)
            self._schematic_prebuild_emit_progress(f"[rtlens] {self.schematic_prebuild_last_summary}")
            return
        files = self._design_search_files()
        if not files:
            self.schematic_prebuild_last_summary = "Schematic prebuild: no source files"
            self._schematic_prebuild_emit_progress(f"[rtlens] {self.schematic_prebuild_last_summary}")
            return
        cache_root = self._schematic_cache_root()
        cache_root.mkdir(parents=True, exist_ok=True)
        if not self.schematic_cache_index:
            self.schematic_cache_index = self._load_schematic_cache_index()

        netlistsvg_dir = str(getattr(self.args, "netlistsvg_dir", "") or "").strip()
        sv2v_cmd, sv2v_source = self._resolve_sv2v_cmd()
        per_module_timeout = max(1, int(getattr(self.args, "schematic_timeout", 8)))
        batch_timeout = per_module_timeout
        fail_ttl_sec = max(0, int(self.schematic_prebuild_fail_ttl_sec))
        top_session_fp = self._schematic_top_session_fingerprint(prebuild_top_module, files)

        updated = 0
        skipped = 0
        deferred = 0
        failed = 0
        stale_removed = 0
        pending: List[str] = []
        pending_meta: Dict[str, dict] = {}
        now_ts = time.time()
        for path in paths:
            node = self.design.hier.get(path)
            if node is None:
                continue
            mod = str(node.module_name or "").strip()
            if not mod:
                continue
            cache_key = self._schematic_cache_key_for_hier_path(path)
            if not cache_key:
                continue
            rel_chain = self._relative_instance_chain_for_prebuild_top(path, prebuild_top_module)
            if rel_chain is None:
                failed += 1
                error_head = "failed to resolve relative instance chain for prebuild top"
                reason = "scope_map"
                fp = self._schematic_instance_fingerprint(cache_key, mod, top_session_fp)
                self._record_schematic_failure_entry(
                    module_name=cache_key,
                    fingerprint=fp,
                    reason=reason,
                    error_head=error_head,
                )
                self.schematic_prebuild_fail_logs[cache_key] = (
                    "[rtlens] schematic prebuild scope mapping failed\n"
                    f"module: {mod}\n"
                    f"hier_path: {path}\n"
                    f"prebuild top module: {prebuild_top_module}\n"
                    f"error: {error_head}"
                )
                continue
            fp = self._schematic_instance_fingerprint(cache_key, mod, top_session_fp)
            cur = self.schematic_cache_index.get(cache_key, {})
            if (
                isinstance(cur, dict)
                and str(cur.get("status", "ok")).strip().lower() != "fail"
                and str(cur.get("fingerprint", "")) == fp
                and os.path.isfile(str(cur.get("svg_path", "")))
                and os.path.isfile(str(cur.get("html_path", "")))
            ):
                skipped += 1
                continue
            if isinstance(cur, dict) and str(cur.get("status", "ok")).strip().lower() == "fail":
                cur_reason = str(cur.get("fail_reason", "other") or "other")
                if (
                    fail_ttl_sec > 0
                    and cur_reason != "scope_map"
                    and str(cur.get("fingerprint", "")) == fp
                    and os.path.isfile(str(cur.get("svg_path", "") or ""))
                    and os.path.isfile(str(cur.get("html_path", "") or ""))
                    and self._schematic_failed_at_sec(cur) > 0.0
                    and (now_ts - self._schematic_failed_at_sec(cur)) < float(fail_ttl_sec)
                ):
                    deferred += 1
                    ttl_remain = max(0.0, float(fail_ttl_sec) - (now_ts - self._schematic_failed_at_sec(cur)))
                    reason = cur_reason
                    err_head = str(cur.get("fail_error_head", "") or "(none)")
                    self.schematic_prebuild_fail_logs[cache_key] = (
                        "[rtlens] schematic prebuild deferred by fail TTL\n"
                        f"module: {mod}\n"
                        f"hier_path: {path}\n"
                        f"reason: {reason}\n"
                        f"ttl_remaining_sec: {int(round(ttl_remain))}\n"
                        f"error_head: {err_head}"
                    )
                    continue
            pending.append(cache_key)
            pending_meta[cache_key] = {
                "module_name": mod,
                "hier_path": path,
                "fingerprint": fp,
                "rel_chain": list(rel_chain),
            }

        results_by_mod: dict[str, NetlistSvgResult] = {}
        result_stage: dict[str, str] = {}
        result_elapsed_sec: dict[str, float] = {}
        fail_reason_counts: dict[str, int] = {}
        metric_stage_totals: dict[str, float] = {}
        heartbeat_sec = 5
        progress_state = {"done": 0, "elapsed_sum": 0.0}

        def _prebuild_eta_sec() -> Optional[float]:
            done = int(progress_state.get("done", 0))
            elapsed_sum = float(progress_state.get("elapsed_sum", 0.0))
            if done <= 0:
                return None
            remaining_targets = max(0, len(pending) - done)
            if remaining_targets <= 0:
                return 0.0
            avg = elapsed_sum / float(done)
            return max(0.0, avg * float(remaining_targets))

        def _on_subtool_progress(event: dict) -> None:
            kind = str(event.get("event", ""))
            if kind == "metric":
                stage = str(event.get("stage", "") or "metric")
                module = str(event.get("module", "") or prebuild_top_module)
                elapsed = float(event.get("elapsed_sec", 0.0) or 0.0)
                metric_stage_totals[stage] = float(metric_stage_totals.get(stage, 0.0)) + max(0.0, elapsed)
                extras = []
                for k in (
                    "attempt",
                    "attempts",
                    "file_count",
                    "input_files",
                    "unique_files",
                    "dropped_files",
                    "json_size",
                    "targets",
                    "result",
                ):
                    if k in event:
                        extras.append(f"{k}={event.get(k)}")
                extra_text = (" " + " ".join(extras)) if extras else ""
                self._schematic_prebuild_emit_progress(
                    "[rtlens] schematic prebuild metric "
                    f"stage={stage} module={module} elapsed={elapsed:.3f}s{extra_text}",
                    detail=True,
                )
                return
            if kind not in {"heartbeat", "timeout"}:
                return
            stage = str(event.get("stage", "") or "prebuild")
            module = str(event.get("module", "") or prebuild_top_module)
            elapsed = float(event.get("elapsed_sec", 0.0) or 0.0)
            timeout_v = int(event.get("timeout_sec", 0) or 0)
            remaining_v = float(event.get("remaining_sec", 0.0) or 0.0)
            eta_v = _prebuild_eta_sec()
            eta_text = f" eta~{eta_v:.0f}s" if eta_v is not None else " eta=unknown"
            self._schematic_prebuild_emit_progress(
                "[rtlens] schematic prebuild running "
                f"stage={stage} module={module} elapsed={elapsed:.0f}s "
                f"remaining={remaining_v:.0f}s timeout={timeout_v}s{eta_text}"
            )

        self._schematic_prebuild_emit_progress(
            "[rtlens] schematic prebuild start "
            f"top={prebuild_top_module} modules={len(module_scope)} instances={len(paths)} "
            f"pending={len(pending)} reused={skipped} deferred={deferred} "
            f"timeout={per_module_timeout}s batch_probe={batch_timeout}s "
            f"strict_top_only={'yes' if self.schematic_prebuild_strict_top_only else 'no'} "
            f"sv2v={'enabled' if sv2v_cmd else 'disabled'}"
        )
        self._schematic_prebuild_emit_progress(
            "[rtlens] schematic prebuild toolchain "
            f"yosys={getattr(self.args, 'yosys_cmd', 'yosys')} "
            f"netlistsvg={getattr(self.args, 'netlistsvg_cmd', 'netlistsvg')} "
            f"sv2v_source={sv2v_source} sv2v_cmd={sv2v_cmd or '(none)'}"
        )
        if not sv2v_cmd:
            self._schematic_prebuild_emit_progress(
                "[rtlens] schematic prebuild notice: sv2v is unavailable. "
                "SystemVerilog package/import dependent modules can fail quickly. "
                "Set --sv2v-cmd <path> or SV2V_CMD."
            )
        if pending:
            batch_t0 = time.perf_counter()
            self._schematic_prebuild_emit_progress(
                f"[rtlens] schematic prebuild phase: batch start targets={len(pending)} timeout={batch_timeout}s mode=full"
            )
            instance_requests: Dict[str, List[str]] = {}
            for cache_key in pending:
                rel_chain = pending_meta.get(cache_key, {}).get("rel_chain", [])
                instance_requests[cache_key] = list(rel_chain if isinstance(rel_chain, list) else [])
            results_by_mod = generate_netlistsvg_prebuild_batch(
                files=files,
                top_module=prebuild_top_module,
                module_names=None,
                instance_requests=instance_requests,
                extra_args=self.loaded_slang_args,
                yosys_cmd=getattr(self.args, "yosys_cmd", "yosys"),
                netlistsvg_cmd=getattr(self.args, "netlistsvg_cmd", "netlistsvg"),
                netlistsvg_dir=netlistsvg_dir,
                sv2v_cmd=sv2v_cmd,
                timeout_sec=batch_timeout,
                progress_cb=_on_subtool_progress,
                heartbeat_sec=heartbeat_sec,
                top_cache_key=top_session_fp,
                top_cache_dir=str(cache_root),
            )
            batch_elapsed = max(0.0, time.perf_counter() - batch_t0)
            self._schematic_prebuild_emit_progress(
                f"[rtlens] schematic prebuild phase: batch end elapsed={batch_elapsed:.1f}s"
            )
            batch_per_target_elapsed = batch_elapsed / float(max(1, len(pending)))
            for cache_key in pending:
                result_stage.setdefault(cache_key, "batch")
                result_elapsed_sec.setdefault(cache_key, batch_per_target_elapsed)

        for cache_key in pending:
            meta = pending_meta.get(cache_key, {})
            mod = str(meta.get("module_name", "") or "")
            path = str(meta.get("hier_path", "") or "")
            fp = str(meta.get("fingerprint", "") or "")
            result = results_by_mod.get(cache_key)
            if result is None:
                result = NetlistSvgResult(
                    module_name=cache_key,
                    error="internal error: no prebuild result generated",
                    log="[rtlens] schematic prebuild batch\nerror: no result generated",
                )
            progress_state["done"] = int(progress_state.get("done", 0)) + 1
            progress_done = int(progress_state.get("done", 0))
            stage = result_stage.get(cache_key, "batch")
            elapsed_note = result_elapsed_sec.get(cache_key)
            if isinstance(elapsed_note, float):
                progress_state["elapsed_sum"] = float(progress_state.get("elapsed_sum", 0.0)) + max(0.0, elapsed_note)
            label = self._schematic_cache_label(cache_key, mod)
            if result.error or not result.svg_path or not os.path.isfile(result.svg_path):
                err_text = str(result.error or "").strip()
                self.schematic_prebuild_fail_logs[cache_key] = (
                    result.log if err_text else (result.log + "\nerror: schematic prebuild failed")
                )
                classify_log = result.log
                reason = self._classify_schematic_prebuild_failure(err_text, classify_log)
                fail_reason_counts[reason] = int(fail_reason_counts.get(reason, 0)) + 1
                error_head = (err_text.splitlines()[0].strip() if err_text else "schematic prebuild failed")[:200]
                if self._drop_stale_schematic_cache_entry(cache_key):
                    stale_removed += 1
                self._record_schematic_failure_entry(
                    module_name=cache_key,
                    fingerprint=fp,
                    reason=reason,
                    error_head=error_head,
                )
                cur_entry = self.schematic_cache_index.get(cache_key, {})
                if isinstance(cur_entry, dict):
                    cur_entry["module_name"] = mod
                    cur_entry["hier_path"] = path
                    cur_entry["top_session"] = top_session_fp
                failed += 1
                elapsed_text = f" elapsed={elapsed_note:.1f}s" if isinstance(elapsed_note, float) else ""
                self._schematic_prebuild_emit_progress(
                    "[rtlens] schematic prebuild progress "
                    f"[{progress_done}/{len(pending)}] key={cache_key} module={label} stage={stage} result=fail reason={reason}{elapsed_text} "
                    f"error_head={error_head}"
                )
                continue
            safe_key = self._safe_cache_name(self._schematic_hier_path_from_cache_key(cache_key) or cache_key, "hier")
            base = f"{safe_key}_{fp[:12]}"
            svg_dst = cache_root / f"{base}.svg"
            html_dst = cache_root / f"{base}.html"
            json_dst = cache_root / f"{base}.json"
            try:
                shutil.copy2(result.svg_path, svg_dst)
                if result.html_path and os.path.isfile(result.html_path):
                    shutil.copy2(result.html_path, html_dst)
                if result.json_path and os.path.isfile(result.json_path):
                    shutil.copy2(result.json_path, json_dst)
            except Exception:
                self.schematic_prebuild_fail_logs[cache_key] = result.log + "\nerror: failed to copy prebuilt artifacts"
                reason = "cache_copy"
                fail_reason_counts[reason] = int(fail_reason_counts.get(reason, 0)) + 1
                if self._drop_stale_schematic_cache_entry(cache_key):
                    stale_removed += 1
                self._record_schematic_failure_entry(
                    module_name=cache_key,
                    fingerprint=fp,
                    reason=reason,
                    error_head="failed to copy prebuilt artifacts",
                )
                cur_entry = self.schematic_cache_index.get(cache_key, {})
                if isinstance(cur_entry, dict):
                    cur_entry["module_name"] = mod
                    cur_entry["hier_path"] = path
                    cur_entry["top_session"] = top_session_fp
                failed += 1
                elapsed_text = f" elapsed={elapsed_note:.1f}s" if isinstance(elapsed_note, float) else ""
                self._schematic_prebuild_emit_progress(
                    "[rtlens] schematic prebuild progress "
                    f"[{progress_done}/{len(pending)}] key={cache_key} module={label} stage={stage} result=fail reason={reason}{elapsed_text}"
                )
                continue
            self.schematic_cache_index[cache_key] = {
                "status": "ok",
                "fingerprint": fp,
                "svg_path": str(svg_dst),
                "html_path": str(html_dst),
                "json_path": str(json_dst) if json_dst.is_file() else "",
                "module_name": mod,
                "hier_path": path,
                "top_session": top_session_fp,
            }
            self.schematic_prebuild_fail_logs.pop(cache_key, None)
            updated += 1
            elapsed_text = f" elapsed={elapsed_note:.1f}s" if isinstance(elapsed_note, float) else ""
            self._schematic_prebuild_emit_progress(
                "[rtlens] schematic prebuild progress "
                f"[{progress_done}/{len(pending)}] key={cache_key} module={label} stage={stage} result=ok{elapsed_text}"
            )
        self._save_schematic_cache_index()
        self.schematic_prebuild_last_summary = (
            "Schematic prebuild done: "
            f"modules={len(module_scope)} instances={len(paths)} updated={updated} reused={skipped} failed={failed} deferred={deferred} "
            f"stale_removed={stale_removed}"
        )
        self.set_status(self.schematic_prebuild_last_summary)
        total_elapsed = max(0.0, time.perf_counter() - prebuild_t0)
        self._schematic_prebuild_emit_progress(
            f"[rtlens] {self.schematic_prebuild_last_summary} elapsed={total_elapsed:.1f}s"
        )
        if fail_reason_counts:
            reason_items = [f"{k}={fail_reason_counts[k]}" for k in sorted(fail_reason_counts.keys())]
            self._schematic_prebuild_emit_progress(
                "[rtlens] schematic prebuild fail reasons: " + " ".join(reason_items)
            )
        if metric_stage_totals:
            top_items = sorted(metric_stage_totals.items(), key=lambda kv: kv[1], reverse=True)[:3]
            top_text = " ".join([f"{name}={sec:.1f}s" for name, sec in top_items])
            self._schematic_prebuild_emit_progress(
                "[rtlens] schematic prebuild top stages: " + top_text
            )

    def run_text_search(self) -> None:
        pat = self.search_pattern_entry.text()
        if not pat:
            self.set_status("Search pattern is empty")
            return
        use_regex = bool(self.search_regex_check.isChecked())
        scope = self.search_scope_combo.currentText().strip()
        files: List[str] = []
        if scope == "Current file":
            if not self.current_file:
                self.set_status("No current source file")
                return
            files = [self.current_file]
        else:
            files = self._design_search_files()
            if not files:
                self.set_status("No design files loaded")
                return

        rx = None
        if use_regex:
            try:
                rx = re.compile(pat)
            except re.error as e:
                self.set_status(f"Regex error: {e}")
                return

        self.search_hits = []
        self.search_result_list.clear()
        for path in files:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            except Exception:
                continue
            for i, raw in enumerate(lines, start=1):
                body = raw.rstrip("\n")
                ok = (rx.search(body) is not None) if rx is not None else (pat in body)
                if not ok:
                    continue
                snippet = body.strip()
                if len(snippet) > 140:
                    snippet = snippet[:137] + "..."
                self.search_hits.append((path, i, snippet))
                self.search_result_list.addItem(f"{os.path.basename(path)}:{i}: {snippet}")
        self.set_status(f"Search hits: {len(self.search_hits)} ({scope}, {'regex' if use_regex else 'plain'})")
        self.right_tabs.setCurrentIndex(2)

    def on_search_result_jump(self, item) -> None:
        row = self.search_result_list.row(item)
        if row < 0 or row >= len(self.search_hits):
            return
        path, line, _snippet = self.search_hits[row]
        self.show_file(path, line)
        self.right_tabs.setCurrentIndex(0)

    def refresh_hierarchy(self) -> None:
        self.hier_tree.clear()
        self.hier_to_path.clear()
        self.hier_path_to_item.clear()

        def add_node(path: str, parent: Optional[QTreeWidgetItem]) -> None:
            node = self.design.hier[path]
            text = f"{node.inst_name} : {node.module_name}"
            item = QTreeWidgetItem([text])
            self.hier_to_path[id(item)] = path
            self.hier_path_to_item[path] = item
            if parent is None:
                self.hier_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            for c in node.children:
                add_node(c, item)

        for r in self.design.roots:
            add_node(r, None)
        self.hier_tree.expandToDepth(1)
        if self.hier_tree.topLevelItemCount() > 0:
            top = self.hier_tree.topLevelItem(0)
            self.hier_tree.setCurrentItem(top)
            self.on_hier_select()

    def _schematic_tab_index(self) -> int:
        for i in range(self.right_tabs.count()):
            if self.right_tabs.tabText(i) == "Schematic":
                return i
        return -1

    def _rtl_structure_tab_index(self) -> int:
        for i in range(self.right_tabs.count()):
            if self.right_tabs.tabText(i) == "RTL Structure":
                return i
        return -1

    def on_right_tab_changed(self, index: int) -> None:
        if index == self._schematic_tab_index():
            self.refresh_schematic_if_dirty()
        if index == self._rtl_structure_tab_index():
            self.refresh_rtl_structure_if_dirty()

    def _current_module_for_schematic(self) -> str:
        if not self.current_hier_path or self.current_hier_path not in self.design.hier:
            return ""
        return self.design.hier[self.current_hier_path].module_name or ""

    def refresh_schematic_if_dirty(self) -> None:
        if not self.schematic_dirty:
            return
        self.refresh_schematic(manual=False)

    def _poll_schematic_results(self) -> None:
        try:
            cache_key, result = self._schematic_queue.get_nowait()
        except queue.Empty:
            return
        self._schematic_worker = None
        self._schematic_running_module = ""
        self.schematic_result = result
        path = self._schematic_hier_path_from_cache_key(cache_key)
        node = self.design.hier.get(path) if path else None
        mod_name = str((node.module_name if node else "") or "")
        self.schematic_module_name = mod_name or result.module_name
        self.schematic_dirty = False
        self.schematic_refresh_btn.setEnabled(True)
        self._set_schematic_log_text(result.log, result.error)
        fp = ""
        cur_entry = self.schematic_cache_index.get(cache_key, {})
        if isinstance(cur_entry, dict):
            fp = str(cur_entry.get("fingerprint", "") or "")
        if not fp:
            fp = self._schematic_module_fingerprint(mod_name or result.module_name or cache_key)
        if result.error:
            self.schematic_info_label.setText(f"{self._schematic_cache_label(cache_key, mod_name)}: error")
            self.schematic_svg_path = ""
            self.schematic_svg_size = (0.0, 0.0)
            self.schematic_hotspots = []
            self.schematic_net_segments = []
            self.schematic_net_labels = []
            self.schematic_instance_search_hits = []
            self.schematic_instance_search_index = -1
            self._refresh_schematic_instance_hit_list()
            self.schematic_selected_spot_index = -1
            self.schematic_selected_src = ""
            err_text = str(result.error or "").strip()
            reason = self._classify_schematic_prebuild_failure(err_text, result.log)
            error_head = (err_text.splitlines()[0].strip() if err_text else "schematic generation failed")[:200]
            self._drop_stale_schematic_cache_entry(cache_key)
            self._record_schematic_failure_entry(
                module_name=cache_key,
                fingerprint=fp,
                reason=reason,
                error_head=error_head,
            )
            self.schematic_prebuild_fail_logs[cache_key] = (
                result.log if err_text else (result.log + "\nerror: schematic generation failed")
            )
            self._save_schematic_cache_index()
            if hasattr(self.schematic_view, "setHtml"):
                self.schematic_view.setHtml(
                    "<html><body><h3>Schematic generation failed</h3><pre>"
                    + html.escape(result.error)
                    + "</pre></body></html>"
                )
            elif hasattr(self.schematic_view, "setPlainText"):
                self.schematic_view.setPlainText(result.error)
            elif hasattr(self.schematic_view, "setText"):
                if hasattr(self.schematic_view, "setPixmap"):
                    self.schematic_view.setPixmap(QPixmap())
                self.schematic_view.setText(result.error)
            self.set_status(f"Schematic error: {result.error}")
            if self._schematic_pending_refresh:
                self._schematic_pending_refresh = False
                self.schematic_dirty = True
                self.refresh_schematic_if_dirty()
            return
        cache_root = self._schematic_cache_root()
        cache_root.mkdir(parents=True, exist_ok=True)
        safe_key = self._safe_cache_name(self._schematic_hier_path_from_cache_key(cache_key) or cache_key, "hier")
        base = f"{safe_key}_{fp[:12]}"
        svg_dst = cache_root / f"{base}.svg"
        html_dst = cache_root / f"{base}.html"
        json_dst = cache_root / f"{base}.json"
        try:
            svg_src = Path(result.svg_path).resolve()
            html_src = Path(result.html_path).resolve()
            json_src = Path(result.json_path).resolve() if result.json_path else None
            if svg_src != svg_dst.resolve():
                shutil.copy2(svg_src, svg_dst)
            if result.html_path:
                if html_src != html_dst.resolve():
                    shutil.copy2(html_src, html_dst)
            if result.json_path and json_src is not None and json_src.is_file():
                if json_src != json_dst.resolve():
                    shutil.copy2(json_src, json_dst)
            self.schematic_cache_index[cache_key] = {
                "status": "ok",
                "fingerprint": fp,
                "svg_path": str(svg_dst),
                "html_path": str(html_dst),
                "json_path": str(json_dst) if json_dst.is_file() else "",
                "module_name": mod_name,
                "hier_path": path,
            }
            self._save_schematic_cache_index()
            self.schematic_prebuild_fail_logs.pop(cache_key, None)
        except Exception:
            pass
        try:
            # Keep cached artifacts immutable here. Rewriting SVG on every load
            # can leave renderer-incompatible output in some environments.
            svg_path = Path(result.svg_path)
            if not svg_path.is_file():
                raise FileNotFoundError(str(svg_path))
        except Exception:
            pass
        self.schematic_info_label.setText(f"{self._schematic_cache_label(cache_key, mod_name)} [{os.path.basename(result.svg_path)}]")
        if self.schematic_view_mode == "webengine" and hasattr(self.schematic_view, "load"):
            self.schematic_view.load(QUrl.fromLocalFile(result.html_path))
        elif self.schematic_view_mode == "svg":
            self.schematic_svg_path = result.svg_path
            self.schematic_svg_size, self.schematic_hotspots = self._parse_svg_hotspots(
                result.svg_path, result.json_path, result.module_name
            )
            self.schematic_net_segments, self.schematic_net_labels = self._parse_schematic_net_overlays(
                result.svg_path,
                result.json_path,
                result.module_name,
            )
            self._refresh_schematic_net_highlight_list()
            self.schematic_instance_search_hits = []
            self.schematic_instance_search_index = -1
            if hasattr(self, "schematic_inst_search_entry"):
                query = self.schematic_inst_search_entry.text().strip()
                if query:
                    self.run_schematic_instance_search()
                else:
                    self._refresh_schematic_instance_hit_list()
            self.schematic_selected_spot_index = -1
            self.schematic_selected_src = ""
            if hasattr(self.schematic_view, "setText"):
                self.schematic_view.setText("")
            if self.schematic_zoom <= 0.0:
                self.schematic_zoom = 1.0
            self._apply_schematic_zoom()
        else:
            msg = (
                "Embedded schematic viewer is disabled for stability.\n\n"
                f"Mode: {self.schematic_view_mode}\n"
                f"HTML: {result.html_path}\n"
                f"SVG: {result.svg_path}\n\n"
                "Use 'Open External' to view the interactive schematic in a browser."
            )
            if hasattr(self.schematic_view, "setPlainText"):
                self.schematic_view.setPlainText(msg)
            elif hasattr(self.schematic_view, "setText"):
                self.schematic_view.setText(msg)
        self.set_status(f"Schematic ready: {self._schematic_cache_label(cache_key, mod_name)}")
        if self._schematic_pending_refresh:
            self._schematic_pending_refresh = False
            self.schematic_dirty = True
            self.refresh_schematic_if_dirty()

    def refresh_rtl_structure_if_dirty(self) -> None:
        if not self.rtl_structure_dirty:
            return
        self.refresh_rtl_structure()

    def _poll_rtl_structure_results(self) -> None:
        try:
            mod, png_bytes, cmapx_text, svg_text, log_text, error, elk_layout = self._rtl_structure_queue.get_nowait()
        except queue.Empty:
            return
        self._rtl_structure_worker = None
        self._rtl_structure_running_module = ""
        self.rtl_structure_dirty = False
        self.rtl_refresh_btn.setEnabled(True)
        self.rtl_log.setPlainText(log_text)
        if error:
            self.rtl_info_label.setText(f"{mod}: error")
            self.rtl_structure_svg_bytes = b""
            self.rtl_structure_png_bytes = b""
            self.rtl_structure_last_png_path = ""
            self.rtl_structure_hotspots = []
            self.rtl_scene.clear()
            self.rtl_scene.addText(error)
            self.set_status(f"RTL structure error: {error}")
            if self._rtl_structure_pending_refresh:
                self._rtl_structure_pending_refresh = False
                self.rtl_structure_dirty = True
                self.refresh_rtl_structure_if_dirty()
            return
        self.rtl_structure_module_name = mod
        self.rtl_structure_png_bytes = png_bytes
        self.rtl_structure_svg_bytes = svg_text.encode("utf-8", errors="ignore")
        self.rtl_structure_last_png_path = ""
        self.rtl_structure_hotspots = self._parse_rtl_structure_cmapx(cmapx_text)
        self.rtl_structure_zoom = 1.0
        self.rtl_structure_elk_layout = elk_layout
        self.rtl_structure_node_boxes = []
        self.rtl_structure_text_items = []
        self.rtl_structure_edge_label_items = []
        self.rtl_structure_layout_nodes = []
        self.rtl_instance_search_hits = []
        self.rtl_instance_search_index = -1
        self._refresh_rtl_instance_hit_list()
        if elk_layout:
            self._draw_rtl_structure_elk(elk_layout)
            self._apply_rtl_structure_zoom()
        else:
            self.rtl_scene.clear()
            self._apply_rtl_structure_zoom()
        if elk_layout and hasattr(self, "rtl_inst_search_entry"):
            existing_query = self.rtl_inst_search_entry.text().strip()
            if existing_query:
                self.run_rtl_instance_search()
        QTimer.singleShot(0, self.fit_rtl_structure)
        self.set_status(f"RTL structure ready: {mod}")
        if self._rtl_structure_pending_refresh:
            self._rtl_structure_pending_refresh = False
            self.rtl_structure_dirty = True
            self.refresh_rtl_structure_if_dirty()

    def _poll_rtl_benchmark_results(self) -> None:
        try:
            mod, log_text, error = self._rtl_bench_queue.get_nowait()
        except queue.Empty:
            return
        self._rtl_bench_worker = None
        self.rtl_bench_btn.setEnabled(True)
        self.rtl_log.setPlainText(log_text)
        if error:
            self.rtl_info_label.setText(f"{mod}: benchmark error")
            self.set_status(f"RTL ELK benchmark error: {error}")
        else:
            self.rtl_info_label.setText(f"{mod} [ELK benchmark]")
            self.set_status(f"RTL ELK benchmark ready: {mod}")

    def _apply_rtl_structure_zoom(self) -> None:
        if self.rtl_structure_elk_layout:
            self.rtl_view.resetTransform()
            self.rtl_view.scale(self.rtl_structure_zoom, self.rtl_structure_zoom)
            self._update_rtl_structure_text_visibility()
            return
        if not self.rtl_structure_png_bytes:
            return
        try:
            pixmap = QPixmap()
            if not pixmap.loadFromData(self.rtl_structure_png_bytes, "PNG"):
                self.rtl_scene.clear()
                self.rtl_scene.addText("RTL structure PNG render failed.")
                return
            if pixmap.width() <= 0 or pixmap.height() <= 0:
                self.rtl_scene.clear()
                self.rtl_scene.addText("RTL structure PNG size invalid.")
                return
            if not self.rtl_structure_last_png_path:
                out_dir = Path(tempfile.gettempdir()) / "rtlens_rtl_structure"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"{self.rtl_structure_module_name or 'rtl_structure'}.png"
                out_path.write_bytes(self.rtl_structure_png_bytes)
                self.rtl_structure_last_png_path = str(out_path)
            self.rtl_scene.clear()
            self.rtl_pixmap_item = self.rtl_scene.addPixmap(pixmap)
            self.rtl_scene.setSceneRect(self.rtl_pixmap_item.boundingRect())
            self.rtl_info_label.setText(
                f"{self.rtl_structure_module_name} [rtl structure]"
            )
            self.rtl_view.resetTransform()
            self.rtl_view.scale(self.rtl_structure_zoom, self.rtl_structure_zoom)
        except Exception as e:
            self.rtl_scene.clear()
            self.rtl_scene.addText(f"RTL structure display failed: {e}")

    def _update_rtl_structure_text_visibility(self) -> None:
        if not self.rtl_structure_elk_layout:
            return
        zoom = max(0.01, float(self.rtl_structure_zoom))
        title_visible = zoom >= 0.16
        port_visible = zoom >= 0.42
        edge_visible = self.rtl_show_edge_labels and (zoom >= 0.2)
        title_scale = min(1.0, max(0.40, zoom))
        port_scale = min(1.0, max(0.34, zoom * 0.9))
        edge_scale = min(1.0, max(0.34, zoom * 0.8))
        for item, role in self.rtl_structure_text_items:
            if role == "title":
                item.setVisible(title_visible)
                if title_visible:
                    item.setScale(title_scale)
            elif role == "port":
                item.setVisible(port_visible)
                if port_visible:
                    item.setScale(port_scale)
        for item in self.rtl_structure_edge_label_items:
            item.setVisible(edge_visible)
            if edge_visible:
                item.setScale(edge_scale)

    def _rtl_structure_signal_matches_current(self, signal_name: str) -> bool:
        if not signal_name:
            return False
        q = self.signal_entry.text().strip() if hasattr(self, "signal_entry") else ""
        if not q:
            return False
        def _variants(s: str) -> set[str]:
            out = {s}
            if s.startswith("sig_"):
                out.add(s[4:])
            parts = s.split(".")
            if parts:
                out.add(parts[-1])
                if parts[-1].startswith("sig_"):
                    out.add(parts[-1][4:])
            if len(parts) >= 2:
                out.add(parts[-2] + "." + parts[-1])
                tail = parts[-1]
                if tail.startswith("sig_"):
                    out.add(parts[-2] + "." + tail[4:])
            return {x for x in out if x}

        qvars = _variants(q)
        svars = _variants(signal_name)
        if qvars & svars:
            return True
        for sv in svars:
            if q.endswith("." + sv):
                return True
        for qv in qvars:
            if signal_name.endswith("." + qv):
                return True
        return False

    def _draw_rtl_structure_elk(self, layout: dict) -> None:
        self.rtl_scene.clear()
        self.rtl_structure_node_boxes = []
        self.rtl_structure_layout_nodes = []
        self.rtl_structure_text_items = []
        self.rtl_structure_edge_label_items = []
        if QPen is object or QColor is object:
            self.rtl_scene.addText("ELK drawing unavailable in this Qt build.")
            return

        c = self.ui_colors
        edge_pen = QPen(QColor(c.get("rtl_edge", "#566175")))
        edge_pen.setWidthF(1.2)
        bus_pen = QPen(QColor(c.get("rtl_bus", "#0c5bd6")))
        bus_pen.setWidthF(7.0)
        ctrl_pen = QPen(QColor(c.get("rtl_ctrl", "#3b67c1")))
        ctrl_pen.setWidthF(2.2)
        fanout_pen = QPen(QColor(c.get("rtl_fanout", "#334c69")))
        fanout_pen.setWidthF(2.1)
        highlight_pen = QPen(QColor(c.get("rtl_highlight", "#c63b13")))
        highlight_pen.setWidthF(4.2)
        highlight_fill = QBrush(QColor(c.get("rtl_highlight", "#c63b13")))
        node_border = QPen(QColor(c.get("rtl_node_border", "#3f4d62")))
        divider_pen = QPen(QColor(c.get("rtl_divider", "#8a96a8")))
        divider_pen.setWidthF(0.8)
        signal_pen_map: dict[str, QPen] = {}
        highlighted_edges: list[tuple[dict, QPen, QBrush, str]] = []
        connected_port_ids: set[str] = set()

        def _pen_from_hex(color_hex: str, width: float = 4.2) -> tuple[QPen, QBrush]:
            c = QColor(color_hex)
            pen = QPen(c)
            pen.setWidthF(width)
            return pen, QBrush(c)

        def _text_item(label: str, x: float, y: float, color: str = "#202020", z: float = 3.0):
            item = self.rtl_scene.addSimpleText(label)
            item.setBrush(QColor(color))
            item.setPos(x, y)
            item.setZValue(z)
            if QGraphicsItem is not object:
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            return item

        def _text_size(label: str) -> tuple[float, float]:
            item = self.rtl_scene.addSimpleText(label)
            br = item.boundingRect()
            self.rtl_scene.removeItem(item)
            return br.width(), br.height()

        def _draw_module_pin(x: float, y: float, w: float, h: float, direction: str, label_text: str, source: dict) -> None:
            if direction == "output":
                poly = QPolygonF([QPointF(x, y), QPointF(x + w, y), QPointF(x + w, y + h), QPointF(x, y + h)])
                label_x = x + w + 8.0
            elif direction == "inout":
                poly = QPolygonF([QPointF(x + 6.0, y), QPointF(x + w - 6.0, y), QPointF(x + w, y + h / 2.0), QPointF(x + w - 6.0, y + h), QPointF(x + 6.0, y + h), QPointF(x, y + h / 2.0)])
                label_x = x + w + 8.0
            else:
                poly = QPolygonF([QPointF(x, y), QPointF(x + w - 8.0, y), QPointF(x + w, y + h / 2.0), QPointF(x + w - 8.0, y + h), QPointF(x, y + h)])
                tw, th = _text_size(label_text)
                label_x = x - tw - 8.0
            poly_item = self.rtl_scene.addPolygon(poly, node_border, QBrush(QColor("#e8f1ff")))
            poly_item.setZValue(2)
            tw, th = _text_size(label_text)
            txt = _text_item(label_text, label_x, y + max(0.0, (h - th) / 2.0))
            self.rtl_structure_text_items.append((txt, "port"))
            node_item = {
                "rect": (x, y, x + w, y + h),
                "file": (source.get("file") or ""),
                "line": int(source.get("line") or 0),
                "node_type": "module_port",
                "name": label_text,
                "label": label_text,
                "module_name": "",
                "hier_path": "",
            }
            self.rtl_structure_node_boxes.append(node_item)
            self.rtl_structure_layout_nodes.append(dict(node_item))

        def _draw_block_node(child: dict, x: float, y: float, w: float, h: float, fill: QColor, title_text: str) -> None:
            ports = child.get("ports", [])
            left_ports = [p for p in ports if p.get("rtlensPortSide") == "WEST"]
            right_ports = [p for p in ports if p.get("rtlensPortSide") == "EAST"]
            left_w = 58.0
            right_w = 58.0
            for p in left_ports:
                t = (p.get("labels") or [{}])[0].get("text", p.get("rtlensPortLabel", ""))
                left_w = max(left_w, 10.0 + len(t) * 6.6)
            for p in right_ports:
                t = (p.get("labels") or [{}])[0].get("text", p.get("rtlensPortLabel", ""))
                right_w = max(right_w, 10.0 + len(t) * 6.6)
            center_x0 = x + left_w
            center_x1 = x + w - right_w
            node_type = str(child.get("rtlensNodeType", ""))

            rect = self.rtl_scene.addRect(x, y, w, h, node_border, QBrush(fill))
            rect.setZValue(2)
            left_div = self.rtl_scene.addLine(center_x0, y, center_x0, y + h, divider_pen)
            right_div = self.rtl_scene.addLine(center_x1, y, center_x1, y + h, divider_pen)
            left_div.setZValue(2.2)
            right_div.setZValue(2.2)

            tw, th = _text_size(title_text)
            title_y = y + max(2.0, (h - th) / 2.0)
            params = child.get("rtlensParams") or {}
            params_pos = child.get("rtlensParamsPositional") or []
            if params or params_pos:
                title_y = y + max(2.0, (h * 0.28 - th) / 2.0)
                param_parts = [f"{k}={v}" for k, v in params.items()]
                param_parts.extend(params_pos)
                if len(param_parts) > 2:
                    split = max(1, (len(param_parts) + 1) // 2)
                    param_text = ", ".join(param_parts[:split]) + "\n" + ", ".join(param_parts[split:])
                else:
                    param_text = ", ".join(param_parts)
                ptw, pth = _text_size(param_text)
                param_item = _text_item(param_text, center_x0 + max(4.0, ((center_x1 - center_x0) - ptw) / 2.0), y + h * 0.58 - pth / 2.0, color="#606060")
                self.rtl_structure_text_items.append((param_item, "title"))
            title_item = _text_item(title_text, center_x0 + max(4.0, ((center_x1 - center_x0) - tw) / 2.0), title_y)
            self.rtl_structure_text_items.append((title_item, "title"))

            for port in left_ports:
                px = x + float(port.get("x", 0))
                py = y + float(port.get("y", 0))
                ph = float(port.get("height", 10))
                ptext = (port.get("labels") or [{}])[0].get("text", port.get("rtlensPortLabel", ""))
                port_item = _text_item(ptext, x + 6.0, py + max(-2.0, (ph - 12.0) / 2.0))
                self.rtl_structure_text_items.append((port_item, "port"))
                sig_name = port.get("rtlensSignalName", "")
                tick_pen = signal_pen_map.get(sig_name, node_border)
                dangling = str(port.get("id", "")) not in connected_port_ids
                stub_len = 26.0 if dangling else 8.0
                x0 = px - stub_len
                y0 = py + ph / 2.0
                tick = self.rtl_scene.addLine(x0, y0, px + 2.0, y0, tick_pen)
                tick.setZValue(3)
                if dangling:
                    dangling_kind = str(port.get("rtlensDanglingKind", ""))
                    if dangling_kind == "const_expr_input":
                        marker_pen = QPen(QColor("#2f7a2f"))
                        marker_pen.setWidthF(1.4)
                        marker_fill = QBrush(QColor("#daf2da"))
                    else:
                        marker_pen = tick_pen
                        marker_fill = QBrush(QColor("#ffffff"))
                    end = self.rtl_scene.addEllipse(x0 - 3.5, y0 - 3.5, 7.0, 7.0, marker_pen, marker_fill)
                    end.setZValue(3.1)

            for port in right_ports:
                px = x + float(port.get("x", 0))
                py = y + float(port.get("y", 0))
                pw = float(port.get("width", 10))
                ph = float(port.get("height", 10))
                ptext = (port.get("labels") or [{}])[0].get("text", port.get("rtlensPortLabel", ""))
                port_item = _text_item(ptext, x + w - right_w + 4.0, py + max(-2.0, (ph - 12.0) / 2.0))
                self.rtl_structure_text_items.append((port_item, "port"))
                sig_name = port.get("rtlensSignalName", "")
                tick_pen = signal_pen_map.get(sig_name, node_border)
                dangling = str(port.get("id", "")) not in connected_port_ids
                if node_type == "instance":
                    dangling = False
                stub_len = 26.0 if dangling else 8.0
                x1 = px + pw + stub_len
                y1 = py + ph / 2.0
                tick = self.rtl_scene.addLine(px + pw - 2.0, y1, x1, y1, tick_pen)
                tick.setZValue(3)
                if dangling:
                    dangling_kind = str(port.get("rtlensDanglingKind", ""))
                    if dangling_kind == "internal_state":
                        marker_pen = QPen(QColor("#b33322"))
                        marker_pen.setWidthF(1.4)
                        marker_fill = QBrush(QColor("#ffd2cb"))
                    elif dangling_kind == "const_expr_input":
                        marker_pen = QPen(QColor("#2f7a2f"))
                        marker_pen.setWidthF(1.4)
                        marker_fill = QBrush(QColor("#daf2da"))
                    else:
                        marker_pen = tick_pen
                        marker_fill = QBrush(QColor("#ffffff"))
                    end = self.rtl_scene.addEllipse(x1 - 3.5, y1 - 3.5, 7.0, 7.0, marker_pen, marker_fill)
                    end.setZValue(3.1)

            source = child.get("rtlensSource") or {}
            node_item = {
                "rect": (x, y, x + w, y + h),
                "file": (source.get("file") or ""),
                "line": int(source.get("line") or 0),
                "node_id": str(child.get("id", "")),
                "node_type": node_type,
                "name": str(child.get("rtlensName", "")),
                "label": title_text,
                "module_name": str(child.get("rtlensModuleName", "")),
                "hier_path": "",
            }
            if node_type == "instance":
                cur_hier = (self.current_hier_path or "").strip()
                inst_name = str(node_item.get("name", "")).strip()
                if cur_hier and inst_name:
                    node_item["hier_path"] = f"{cur_hier}.{inst_name}"
            self.rtl_structure_node_boxes.append(node_item)
            self.rtl_structure_layout_nodes.append(dict(node_item))

        for edge in layout.get("edges", []):
            for port_id in edge.get("sources", []):
                connected_port_ids.add(str(port_id))
            for port_id in edge.get("targets", []):
                connected_port_ids.add(str(port_id))

        for edge in layout.get("edges", []):
            signal_name = edge.get("rtlensSignalName", "")
            lower = signal_name.lower()
            fanout = int(edge.get("rtlensFanout", 1) or 1)
            is_current = self._rtl_structure_signal_matches_current(signal_name)
            rule = self._match_rtl_highlight_rule(signal_name)
            if rule:
                color_hex = self._normalize_color_hex(str(rule.get("color", ""))) or "#d73a49"
                pen, fill = _pen_from_hex(color_hex, width=3.9)
                signal_pen_map[signal_name] = pen
                highlighted_edges.append((edge, pen, fill, color_hex))
            elif is_current:
                signal_pen_map[signal_name] = highlight_pen
                highlighted_edges.append((edge, highlight_pen, highlight_fill, c.get("rtl_highlight_label", "#9f2f10")))
            elif edge.get("rtlensIsBus"):
                signal_pen_map[signal_name] = bus_pen
            elif "clk" in lower or "rst" in lower:
                signal_pen_map[signal_name] = ctrl_pen
            elif fanout >= 4:
                signal_pen_map[signal_name] = fanout_pen
            else:
                signal_pen_map.setdefault(signal_name, edge_pen)

        for edge in layout.get("edges", []):
            signal_name = edge.get("rtlensSignalName", "")
            lower = signal_name.lower()
            fanout = int(edge.get("rtlensFanout", 1) or 1)
            rule = self._match_rtl_highlight_rule(signal_name)
            is_current = self._rtl_structure_signal_matches_current(signal_name)
            label_color = "#505050"
            if rule:
                color_hex = self._normalize_color_hex(str(rule.get("color", ""))) or "#d73a49"
                pen, _ = _pen_from_hex(color_hex, width=3.9)
                label_color = color_hex
            elif is_current:
                pen = highlight_pen
                label_color = c.get("rtl_highlight_label", "#9f2f10")
            elif edge.get("rtlensIsBus"):
                pen = bus_pen
                label_color = "#505050"
            elif "clk" in lower or "rst" in lower:
                pen = ctrl_pen
                label_color = "#505050"
            elif fanout >= 4:
                pen = fanout_pen
                label_color = "#3f4a56"
            else:
                pen = edge_pen
            label_done = False
            for sec in edge.get("sections", []):
                path = QPainterPath()
                sx = float(sec.get("startPoint", {}).get("x", 0))
                sy = float(sec.get("startPoint", {}).get("y", 0))
                path.moveTo(sx, sy)
                points = [(sx, sy)]
                for bp in sec.get("bendPoints", []):
                    bx = float(bp.get("x", 0))
                    by = float(bp.get("y", 0))
                    path.lineTo(bx, by)
                    points.append((bx, by))
                ex = float(sec.get("endPoint", {}).get("x", 0))
                ey = float(sec.get("endPoint", {}).get("y", 0))
                path.lineTo(ex, ey)
                points.append((ex, ey))
                item = self.rtl_scene.addPath(path, pen)
                item.setZValue(0)
                show_label = self.rtl_show_edge_labels and bool(signal_name)
                if signal_name and show_label and not label_done:
                    label_x = None
                    label_y = None
                    for (x0, y0), (x1, y1) in zip(points, points[1:]):
                        if abs(x1 - x0) >= 28 or abs(y1 - y0) >= 28:
                            label_x = (x0 + x1) / 2.0
                            label_y = (y0 + y1) / 2.0
                            break
                    if label_x is not None:
                        tw, th = _text_size(signal_name)
                        label_item = _text_item(signal_name, label_x - tw / 2.0, label_y - th / 2.0, color=label_color, z=0.5)
                        self.rtl_structure_edge_label_items.append(label_item)
                        label_done = True

        for edge, overlay_pen, overlay_fill, overlay_label_color in highlighted_edges:
            signal_name = edge.get("rtlensSignalName", "")
            for sec in edge.get("sections", []):
                path = QPainterPath()
                sx = float(sec.get("startPoint", {}).get("x", 0))
                sy = float(sec.get("startPoint", {}).get("y", 0))
                path.moveTo(sx, sy)
                for bp in sec.get("bendPoints", []):
                    bx = float(bp.get("x", 0))
                    by = float(bp.get("y", 0))
                    path.lineTo(bx, by)
                ex = float(sec.get("endPoint", {}).get("x", 0))
                ey = float(sec.get("endPoint", {}).get("y", 0))
                path.lineTo(ex, ey)
                item = self.rtl_scene.addPath(path, overlay_pen)
                item.setZValue(1.4)
                for mx, my in ((sx, sy), (ex, ey)):
                    marker = self.rtl_scene.addEllipse(mx - 3.5, my - 3.5, 7.0, 7.0, overlay_pen, overlay_fill)
                    marker.setZValue(2.9)
            if signal_name and self.rtl_show_edge_labels:
                # keep one visible label above the highlighted route
                sec = (edge.get("sections") or [None])[0]
                if sec:
                    sx = float(sec.get("startPoint", {}).get("x", 0))
                    sy = float(sec.get("startPoint", {}).get("y", 0))
                    ex = float(sec.get("endPoint", {}).get("x", 0))
                    ey = float(sec.get("endPoint", {}).get("y", 0))
                    tw, th = _text_size(signal_name)
                    label_item = _text_item(
                        signal_name,
                        (sx + ex) / 2.0 - tw / 2.0,
                        (sy + ey) / 2.0 - th / 2.0 - 8.0,
                        color=overlay_label_color,
                        z=2.0,
                    )
                    self.rtl_structure_edge_label_items.append(label_item)
                    break

        for child in layout.get("children", []):
            x = float(child.get("x", 0))
            y = float(child.get("y", 0))
            w = float(child.get("width", 80))
            h = float(child.get("height", 30))
            shape = child.get("rtlensShape", "")
            labels = child.get("labels", [])
            text = labels[0].get("text", child.get("id", "")) if labels else child.get("id", "")
            if shape == "module_port":
                _draw_module_pin(x, y, w, h, child.get("rtlensDirection", "input"), text, child.get("rtlensSource") or {})
                continue
            fill = QColor(c.get("rtl_node_fill_default", "#fff4db"))
            if shape == "assign_block":
                fill = QColor(c.get("rtl_node_fill_assign", "#edf8ef"))
            elif shape == "callable_block":
                fill = QColor(c.get("rtl_node_fill_callable", "#eaf1ff"))
            elif shape == "always_block":
                fill = QColor(c.get("rtl_node_fill_always", "#f2ebff"))
            _draw_block_node(child, x, y, w, h, fill, text)

        bounds = self.rtl_scene.itemsBoundingRect()
        if not bounds.isNull():
            self.rtl_scene.setSceneRect(bounds.adjusted(-80.0, -50.0, 80.0, 50.0))
        else:
            max_x = float(layout.get("width", 1000))
            max_y = float(layout.get("height", 800))
            self.rtl_scene.setSceneRect(0, 0, max_x, max_y)
        self._update_rtl_structure_text_visibility()

    def _parse_rtl_structure_cmapx(self, cmapx_text: str) -> List[dict]:
        out: List[dict] = []
        if not cmapx_text.strip():
            return out
        try:
            root = ET.fromstring(cmapx_text)
        except Exception:
            return out
        for area in root.iter():
            if area.tag != "area":
                continue
            href = area.attrib.get("href", "") or area.attrib.get("xlink:href", "")
            if not href.startswith("rtlens://source?"):
                continue
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            file = unquote((qs.get("file") or [""])[0])
            try:
                line = int((qs.get("line") or ["1"])[0])
            except Exception:
                line = 1
            shape = area.attrib.get("shape", "").lower()
            coords_raw = area.attrib.get("coords", "")
            coords = []
            for part in coords_raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    coords.append(float(part))
                except Exception:
                    pass
            title = area.attrib.get("title", "")
            out.append(
                {
                    "shape": shape,
                    "coords": coords,
                    "file": file,
                    "line": line,
                    "title": title,
                }
            )
        return out

    def _rtl_hotspot_hit(self, scene_x: float, scene_y: float) -> Optional[dict]:
        for spot in reversed(self.rtl_structure_hotspots):
            shape = spot.get("shape", "")
            coords = spot.get("coords", [])
            if shape == "rect" and len(coords) >= 4:
                x1, y1, x2, y2 = coords[:4]
                if x1 <= scene_x <= x2 and y1 <= scene_y <= y2:
                    return spot
            elif shape == "circle" and len(coords) >= 3:
                cx, cy, r = coords[:3]
                if (scene_x - cx) ** 2 + (scene_y - cy) ** 2 <= r * r:
                    return spot
            elif shape == "poly" and len(coords) >= 6:
                xs = coords[0::2]
                ys = coords[1::2]
                if min(xs) <= scene_x <= max(xs) and min(ys) <= scene_y <= max(ys):
                    return spot
        return None

    def _rtl_node_box_hit(self, scene_x: float, scene_y: float) -> Optional[dict]:
        for item in reversed(self.rtl_structure_node_boxes):
            x0, y0, x1, y1 = item["rect"]
            if x0 <= scene_x <= x1 and y0 <= scene_y <= y1:
                return item
        return None

    def _dive_into_instance_from_rtl_box(self, box: dict) -> bool:
        if str(box.get("node_type", "")) != "instance":
            return False
        inst_name = str(box.get("name", "")).strip()
        if not inst_name and ":" in str(box.get("label", "")):
            inst_name = str(box.get("label", "")).split(":", 1)[0].strip()
        if not inst_name:
            return False
        parent = (self.current_hier_path or "").strip()
        if not parent:
            return False
        target_hier = f"{parent}.{inst_name}"
        if target_hier not in self.design.hier:
            self.set_status(f"Instance hier not found: {target_hier}")
            return False
        item = self.hier_path_to_item.get(target_hier)
        if item is None:
            self.set_status(f"Hierarchy item not found: {target_hier}")
            return False
        self.hier_tree.setCurrentItem(item)
        self.hier_tree.scrollToItem(item)
        self.set_status(f"Dive hierarchy: {target_hier}")
        return True

    def on_rtl_structure_double_click(self, view_x: float, view_y: float, ctrl_pressed: bool) -> None:
        if self.rtl_structure_elk_layout and self.rtl_structure_node_boxes:
            pt = self.rtl_view.mapToScene(int(view_x), int(view_y))
            box = self._rtl_node_box_hit(float(pt.x()), float(pt.y()))
            if ctrl_pressed and box is not None and self._dive_into_instance_from_rtl_box(box):
                return
        self.on_rtl_structure_click(view_x, view_y)

    def on_rtl_structure_click(self, view_x: float, view_y: float) -> None:
        if self.rtl_structure_elk_layout and self.rtl_structure_node_boxes:
            pt = self.rtl_view.mapToScene(int(view_x), int(view_y))
            item = self._rtl_node_box_hit(float(pt.x()), float(pt.y()))
            if item is not None:
                file = item.get("file") or ""
                line = int(item.get("line") or 0)
                if file and line > 0:
                    self.show_file(file, line)
                    self.right_tabs.setCurrentIndex(0)
                    self.set_status(f"RTL structure src: {file}:{line}")
                return
            return
        if not self.rtl_structure_hotspots:
            return
        pt = self.rtl_view.mapToScene(int(view_x), int(view_y))
        spot = self._rtl_hotspot_hit(float(pt.x()), float(pt.y()))
        if not spot:
            return
        file = spot.get("file", "")
        line = int(spot.get("line", 1))
        title = spot.get("title", "")
        if file:
            self.show_file(file, line)
            self.right_tabs.setCurrentIndex(0)
            self.set_status(f"RTL structure src: {file}:{line}")
        elif title:
            self.set_status(f"RTL structure: {title}")

    def adjust_rtl_structure_zoom(self, factor: float) -> None:
        self.rtl_structure_zoom = max(0.1, min(8.0, self.rtl_structure_zoom * factor))
        self._apply_rtl_structure_zoom()

    def fit_rtl_structure(self) -> None:
        try:
            if self.rtl_structure_elk_layout:
                viewport = self.rtl_view.viewport().size()
                if viewport.width() <= 64 or viewport.height() <= 64 or self.rtl_scene.sceneRect().isNull():
                    self._apply_rtl_structure_zoom()
                    return
                self.rtl_view.fitInView(self.rtl_scene.sceneRect(), Qt.KeepAspectRatio)
                self.rtl_structure_zoom = self.rtl_view.transform().m11()
                self._update_rtl_structure_text_visibility()
                return
            if not self.rtl_structure_png_bytes:
                return
            pixmap = QPixmap()
            if not pixmap.loadFromData(self.rtl_structure_png_bytes, "PNG"):
                self.rtl_scene.clear()
                self.rtl_scene.addText("RTL structure PNG render failed.")
                return
            viewport = self.rtl_view.viewport().size()
            if pixmap.width() <= 0 or pixmap.height() <= 0:
                return
            if viewport.width() <= 64 or viewport.height() <= 64:
                self._apply_rtl_structure_zoom()
                return
            self._apply_rtl_structure_zoom()
            self.rtl_view.fitInView(self.rtl_scene.sceneRect(), Qt.KeepAspectRatio)
            self.rtl_structure_zoom = self.rtl_view.transform().m11()
        except Exception as e:
            self.rtl_scene.clear()
            self.rtl_scene.addText(f"RTL structure fit failed: {e}")

    def _parse_schematic_net_overlays(
        self, svg_path: str, json_path: str = "", module_name: str = ""
    ) -> tuple[List[dict], List[dict]]:
        if not svg_path or not os.path.isfile(svg_path):
            return [], []
        try:
            svg_text = Path(svg_path).read_text(encoding="utf-8", errors="ignore")
            svg_text = self._repair_schematic_svg_annotation_syntax(svg_text)
            root = ET.fromstring(svg_text)
        except Exception:
            return [], []
        net_annotations = self._load_schematic_net_annotations(json_path, module_name, svg_path=svg_path)

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        def num(v) -> float:
            try:
                return float(str(v).replace("px", ""))
            except Exception:
                return 0.0

        def parse_translate(v: str) -> tuple[float, float]:
            m = re.search(r"translate\(\s*([0-9.+-]+)\s*,\s*([0-9.+-]+)\s*\)", str(v or ""))
            if not m:
                return 0.0, 0.0
            return num(m.group(1)), num(m.group(2))

        def parse_points(v: str) -> List[tuple[float, float]]:
            out: List[tuple[float, float]] = []
            txt = str(v or "").strip()
            if not txt:
                return out
            for part in re.split(r"\s+", txt):
                if "," not in part:
                    continue
                xs, ys = part.split(",", 1)
                out.append((num(xs), num(ys)))
            return out

        segments: List[dict] = []
        centers: dict[str, dict] = {}

        def remember_point(net_key: str, label: str, src: str, x: float, y: float) -> None:
            if not net_key or not label:
                return
            slot = centers.get(net_key)
            if slot is None:
                slot = {"label": label, "src": src, "sum_x": 0.0, "sum_y": 0.0, "count": 0}
                centers[net_key] = slot
            if label and not str(slot.get("label", "")).strip():
                slot["label"] = label
            if src and not str(slot.get("src", "")).strip():
                slot["src"] = src
            slot["sum_x"] = float(slot.get("sum_x", 0.0)) + float(x)
            slot["sum_y"] = float(slot.get("sum_y", 0.0)) + float(y)
            slot["count"] = int(slot.get("count", 0)) + 1

        def walk(node: ET.Element, off_x: float, off_y: float, net_key: str, label: str, src: str) -> None:
            tag = local(node.tag)
            dx, dy = (0.0, 0.0)
            if tag == "g":
                dx, dy = parse_translate(node.attrib.get("transform", ""))
            ox = off_x + dx
            oy = off_y + dy
            cur_key = str(net_key or "")
            cls = str(node.attrib.get("class", "")).strip()
            if cls:
                for tok in cls.split():
                    if tok.startswith("net_"):
                        cur_key = tok[4:]
                        break
            cur_label = str(node.attrib.get("data-net-label", "") or label or "").strip()
            cur_src = str(node.attrib.get("data-net-src", "") or src or "").strip()
            ann = net_annotations.get(cur_key, {}) if cur_key else {}
            if not cur_label:
                cur_label = str(ann.get("label", "") or "").strip()
            if not cur_src:
                cur_src = str(ann.get("src", "") or "").strip()

            if cur_key and cur_label:
                if tag == "line":
                    x1 = ox + num(node.attrib.get("x1", 0.0))
                    y1 = oy + num(node.attrib.get("y1", 0.0))
                    x2 = ox + num(node.attrib.get("x2", 0.0))
                    y2 = oy + num(node.attrib.get("y2", 0.0))
                    segments.append(
                        {
                            "kind": "line",
                            "net_key": cur_key,
                            "label": cur_label,
                            "src": cur_src,
                            "x1": x1,
                            "y1": y1,
                            "x2": x2,
                            "y2": y2,
                        }
                    )
                    remember_point(cur_key, cur_label, cur_src, (x1 + x2) / 2.0, (y1 + y2) / 2.0)
                elif tag in {"polyline", "polygon"}:
                    pts = [(ox + x, oy + y) for x, y in parse_points(node.attrib.get("points", ""))]
                    if len(pts) >= 2:
                        segments.append(
                            {
                                "kind": "polyline",
                                "net_key": cur_key,
                                "label": cur_label,
                                "src": cur_src,
                                "points": pts,
                            }
                        )
                        sx = sum(p[0] for p in pts) / float(len(pts))
                        sy = sum(p[1] for p in pts) / float(len(pts))
                        remember_point(cur_key, cur_label, cur_src, sx, sy)
                elif tag == "path":
                    nums = [num(v) for v in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", node.attrib.get("d", ""))]
                    if len(nums) >= 4:
                        pts: List[tuple[float, float]] = []
                        for i in range(0, len(nums) - 1, 2):
                            pts.append((ox + nums[i], oy + nums[i + 1]))
                        if len(pts) >= 2:
                            segments.append(
                                {
                                    "kind": "polyline",
                                    "net_key": cur_key,
                                    "label": cur_label,
                                    "src": cur_src,
                                    "points": pts,
                                }
                            )
                            sx = sum(p[0] for p in pts) / float(len(pts))
                            sy = sum(p[1] for p in pts) / float(len(pts))
                            remember_point(cur_key, cur_label, cur_src, sx, sy)

            for ch in list(node):
                walk(ch, ox, oy, cur_key, cur_label, cur_src)

        walk(root, 0.0, 0.0, "", "", "")

        labels: List[dict] = []
        for net_key, info in centers.items():
            count = int(info.get("count", 0) or 0)
            if count <= 0:
                continue
            labels.append(
                {
                    "net_key": net_key,
                    "label": str(info.get("label", "") or ""),
                    "src": str(info.get("src", "") or ""),
                    "x": float(info.get("sum_x", 0.0)) / float(count),
                    "y": float(info.get("sum_y", 0.0)) / float(count),
                }
            )
        return segments, labels

    def _apply_schematic_zoom(self) -> None:
        if self.schematic_view_mode != "svg" or not self.schematic_svg_path or QSvgRenderer is object:
            return
        try:
            svg_text = Path(self.schematic_svg_path).read_text(encoding="utf-8", errors="ignore")
            svg_text = self._repair_schematic_svg_annotation_syntax(svg_text)
            svg_bytes = svg_text.encode("utf-8", errors="ignore")
            renderer = QSvgRenderer()
            renderer.load(QByteArray(svg_bytes))
            if not renderer.isValid():
                self.schematic_view.setText(f"SVG render failed: {self.schematic_svg_path}")
                return
            size = renderer.defaultSize()
            if size.width() <= 0 or size.height() <= 0:
                self.schematic_view.setText(f"SVG size invalid: {self.schematic_svg_path}")
                return
            w = max(100, int(size.width() * self.schematic_zoom))
            h = max(100, int(size.height() * self.schematic_zoom))
            image = QImage(w, h, QImage.Format_ARGB32)
            image.fill(0xFFFFFFFF)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            renderer.render(painter)

            sx_ratio = float(w) / max(1.0, float(size.width()))
            sy_ratio = float(h) / max(1.0, float(size.height()))

            if self.schematic_hotspots:
                search_hit_indices: Set[int] = set()
                for hit in self.schematic_instance_search_hits:
                    try:
                        idx = int(hit.get("spot_index", -1))
                    except Exception:
                        idx = -1
                    if idx >= 0:
                        search_hit_indices.add(idx)
                for idx, spot in enumerate(self.schematic_hotspots):
                    if str(spot.get("node_kind", "")).strip() != "instance":
                        continue
                    sx = float(spot.get("x", 0.0)) * sx_ratio
                    sy = float(spot.get("y", 0.0)) * sy_ratio
                    sw = float(spot.get("w", 0.0)) * sx_ratio
                    sh = float(spot.get("h", 0.0)) * sy_ratio
                    if sw <= 1.0 or sh <= 1.0:
                        continue
                    pad = 1.0
                    sx -= pad
                    sy -= pad
                    sw += pad * 2.0
                    sh += pad * 2.0
                    if idx in search_hit_indices:
                        fill = QColor("#fef08a")
                        fill.setAlpha(120)
                        pen = QPen(QColor("#a16207"))
                        pen.setWidth(2)
                    else:
                        fill = QColor("#fff7cc")
                        fill.setAlpha(60)
                        pen = QPen(QColor("#c7b252"))
                        pen.setWidth(1)
                    painter.setPen(pen)
                    painter.setBrush(QBrush(fill))
                    painter.drawRect(sx, sy, sw, sh)

            if self.schematic_net_segments:
                for seg in self.schematic_net_segments:
                    label = str(seg.get("label", "") or "").strip()
                    rule = self._match_schematic_net_highlight_rule(label)
                    if rule is None:
                        continue
                    color_hex = self._normalize_color_hex(str(rule.get("color", ""))) or "#d73a49"
                    color = QColor(color_hex)
                    color.setAlpha(190)
                    pen = QPen(color)
                    pen.setWidth(2)
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    kind = str(seg.get("kind", "")).strip()
                    if kind == "line":
                        x1 = float(seg.get("x1", 0.0)) * sx_ratio
                        y1 = float(seg.get("y1", 0.0)) * sy_ratio
                        x2 = float(seg.get("x2", 0.0)) * sx_ratio
                        y2 = float(seg.get("y2", 0.0)) * sy_ratio
                        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
                    elif kind == "polyline":
                        pts_raw = seg.get("points", [])
                        if not isinstance(pts_raw, list) or len(pts_raw) < 2:
                            continue
                        poly = QPolygonF()
                        for p in pts_raw:
                            if not isinstance(p, (tuple, list)) or len(p) < 2:
                                continue
                            poly.append(QPointF(float(p[0]) * sx_ratio, float(p[1]) * sy_ratio))
                        if poly.size() >= 2:
                            painter.drawPolyline(poly)

            if self.schematic_show_net_labels and self.schematic_net_labels:
                font = painter.font()
                font.setPointSize(max(8, int(round(9 * max(1.0, min(2.0, self.schematic_zoom * 0.5))))))
                painter.setFont(font)
                for item in self.schematic_net_labels:
                    label = str(item.get("label", "") or "").strip()
                    if not label:
                        continue
                    rule = self._match_schematic_net_highlight_rule(label)
                    x = float(item.get("x", 0.0)) * sx_ratio + 3.0
                    y = float(item.get("y", 0.0)) * sy_ratio - 3.0
                    th = max(10, painter.fontMetrics().height())
                    if y - th < 0.0:
                        y += th + 8.0
                    color_hex = self.ui_colors.get("schem_net_label", "#1e293b")
                    if rule is not None:
                        color_hex = self._normalize_color_hex(str(rule.get("color", ""))) or "#d73a49"
                    txt_color = QColor(color_hex)
                    painter.setPen(QPen(txt_color))
                    painter.drawText(QPointF(x, y), label)

            spot = self._selected_schematic_spot()
            if spot:
                sx = float(spot.get("x", 0.0)) * sx_ratio
                sy = float(spot.get("y", 0.0)) * sy_ratio
                sw = float(spot.get("w", 0.0)) * sx_ratio
                sh = float(spot.get("h", 0.0)) * sy_ratio
                min_w = 24.0
                min_h = 18.0
                if sw > 1.0 and sh > 1.0:
                    pad = 3.0
                    sx -= pad
                    sy -= pad
                    sw += pad * 2.0
                    sh += pad * 2.0
                    if sw < min_w:
                        sx -= (min_w - sw) / 2.0
                        sw = min_w
                    if sh < min_h:
                        sy -= (min_h - sh) / 2.0
                        sh = min_h
                    fill = QColor(self.ui_colors.get("schem_select_fill", "#f7c59f"))
                    fill.setAlpha(96)
                    pen = QPen(QColor(self.ui_colors.get("schem_select_stroke", "#c63b13")))
                    pen.setWidth(3)
                    painter.setPen(pen)
                    painter.setBrush(QBrush(fill))
                    painter.drawRect(sx, sy, sw, sh)
            painter.end()
            self.schematic_view.setPixmap(QPixmap.fromImage(image))
            self.schematic_view.resize(w, h)
        except Exception as e:
            self.schematic_view.setText(f"SVG display failed: {e}")

    def _normalize_schematic_src(self, raw_src: str, fallback: str = "") -> str:
        return _normalize_schematic_src_text(raw_src, fallback=fallback)

    def _repair_schematic_svg_annotation_syntax(self, svg_text: str) -> str:
        txt = str(svg_text or "")
        if not txt:
            return txt
        # Repair malformed attributes generated by older builds:
        #   class="..."/ data-net-label=...
        txt = re.sub(r'"\s*/\s+(data-(?:net-label|net-src|src|port-dir)=)', r'" \1', txt)

        def _fix_empty_tag(name: str, payload: str) -> str:
            body = str(payload or "")
            if body.rstrip().endswith("/"):
                return f"<{name}{body}>"
            return f"<{name}{body}/>"

        # If repaired tags above lost self-closing '/', restore it for known empty SVG tags.
        empty_tags = ("line", "polyline", "polygon", "path", "circle", "ellipse", "rect")
        for t in empty_tags:
            pattern = re.compile(rf"<({t})([^<>]*\sdata-(?:net-label|net-src|src|port-dir)=[^<>]*)>")
            txt = pattern.sub(lambda m: _fix_empty_tag(m.group(1), m.group(2)), txt)
        return txt

    def _normalize_requested_schematic_module(self, module_name: str) -> str:
        req = str(module_name or "").strip()
        if not req:
            return ""
        if req.startswith("hier:"):
            path = req.split(":", 1)[1].strip()
            node = self.design.hier.get(path)
            if node and node.module_name:
                req = str(node.module_name).strip()
        if " @ " in req:
            req = req.split(" @ ", 1)[0].strip()
        if req.startswith("module:"):
            req = req.split(":", 1)[1].strip()
        return req

    def _canonical_schematic_name(self, name: str) -> str:
        return _canonical_schematic_name_text(name)

    def _demangle_paramod_module_name(self, cell_type: str) -> str:
        return _demangle_paramod_module_name_text(cell_type)

    def _classify_schematic_cell_type(self, cell_type: str) -> tuple[str, str]:
        return _classify_schematic_cell_type_text(cell_type)

    def _resolve_child_hier_from_instance_name(self, parent_hier: str, inst_name: str) -> str:
        parent = str(parent_hier or "").strip()
        name = str(inst_name or "").strip()
        if not parent or not name:
            return ""
        candidates: List[str] = [f"{parent}.{name}"]
        if name.startswith("\\"):
            candidates.append(f"{parent}.{name.lstrip('\\')}")
        for cand in candidates:
            if cand in self.design.hier:
                return cand
        parent_node = self.design.hier.get(parent)
        if not parent_node:
            return ""
        name_c = self._canonical_schematic_name(name)
        for child_path in parent_node.children:
            child = self.design.hier.get(child_path)
            if not child:
                continue
            if self._canonical_schematic_name(str(child.inst_name or "")) == name_c:
                return child_path
        return ""

    def _resolve_instance_decl_src(self, parent_hier: str, inst_name: str) -> str:
        parent = str(parent_hier or "").strip()
        name = str(inst_name or "").strip()
        if not parent or not name:
            return ""
        parent_node = self.design.hier.get(parent)
        if not parent_node:
            return ""
        parent_mod = self.design.modules.get(str(parent_node.module_name or "").strip())
        name_c = self._canonical_schematic_name(name)
        if parent_mod and parent_mod.file and os.path.isfile(parent_mod.file):
            for inst in parent_mod.instances:
                if self._canonical_schematic_name(str(inst.name or "")) != name_c:
                    continue
                line = max(1, int(inst.line or 1))
                return f"{os.path.abspath(parent_mod.file)}:{line}"
        return ""

    def _find_schematic_json_module_key_by_svg_ports(self, modules: dict[str, dict], svg_path: str) -> str:
        sp = str(svg_path or "").strip()
        if not sp or not os.path.isfile(sp):
            return ""
        try:
            svg_text = Path(sp).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
        rx = re.compile(r"<g[^>]*\bs:type=\"(?:inputExt|outputExt|inoutExt)\"[^>]*\sid=\"cell_([^\"]+)\"")
        ext_ids: set[str] = set()
        for m in rx.finditer(svg_text):
            name = str(m.group(1) or "").strip()
            if name:
                ext_ids.add(name)
        if not ext_ids:
            return ""
        best_key = ""
        best_score = 0
        for key, mod in modules.items():
            if not isinstance(mod, dict):
                continue
            ports = mod.get("ports", {})
            if not isinstance(ports, dict):
                continue
            score = sum(1 for name in ext_ids if name in ports)
            if score > best_score:
                best_score = score
                best_key = key
        return best_key if best_score > 0 else ""

    def _load_schematic_json_module(
        self, json_path: str, module_name: str, svg_path: str = ""
    ) -> tuple[dict, str]:
        jp = str(json_path or "").strip()
        if not jp or not os.path.isfile(jp):
            return {}, ""
        try:
            data = json.loads(Path(jp).read_text(encoding="utf-8"))
        except Exception:
            return {}, ""
        modules = data.get("modules", {})
        if not isinstance(modules, dict) or not modules:
            return {}, ""

        req = self._normalize_requested_schematic_module(module_name)
        key = req if req in modules else ""
        if not key and req:
            canon = self._canonical_schematic_name(req)
            for k in modules.keys():
                if self._canonical_schematic_name(str(k)) == canon:
                    key = k
                    break
        if not key:
            key = self._find_schematic_json_module_key_by_svg_ports(modules, svg_path)
        if not key:
            key = next(iter(modules.keys()))
        mod = modules.get(key, {})
        if not isinstance(mod, dict):
            return {}, ""
        module_src = self._normalize_schematic_src(str((mod.get("attributes") or {}).get("src", "")))
        return mod, module_src

    def _load_schematic_svg_id_src_map(
        self, json_path: str, module_name: str, svg_path: str = ""
    ) -> tuple[dict[str, str], str, dict[str, dict]]:
        id_to_src: dict[str, str] = {}
        id_to_meta: dict[str, dict] = {}
        mod, module_src = self._load_schematic_json_module(json_path, module_name, svg_path=svg_path)
        if not mod:
            return id_to_src, module_src, id_to_meta

        cells = mod.get("cells", {})
        if isinstance(cells, dict):
            for cell_name, cell in cells.items():
                if not isinstance(cell, dict):
                    continue
                csrc_raw = str((cell.get("attributes") or {}).get("src", ""))
                csrc = self._normalize_schematic_src(csrc_raw, fallback=module_src)
                cid = f"cell_{cell_name}"
                if csrc:
                    id_to_src[cid] = csrc
                ctype_raw = str(cell.get("type", "") or "")
                kind, ctype = self._classify_schematic_cell_type(ctype_raw)
                if kind == "instance":
                    id_to_meta[cid] = {
                        "kind": "instance",
                        "name": str(cell_name),
                        "module_name": ctype,
                    }
                else:
                    id_to_meta[cid] = {
                        "kind": "cell",
                        "name": str(cell_name),
                        "module_name": ctype,
                    }
        ports = mod.get("ports", {})
        if isinstance(ports, dict):
            for port_name, port in ports.items():
                if not isinstance(port, dict):
                    continue
                psrc_raw = str((port.get("attributes") or {}).get("src", ""))
                psrc = self._normalize_schematic_src(psrc_raw, fallback=module_src)
                pid = f"cell_{port_name}"
                if psrc:
                    # Prefer explicit module-port source over synthesized pseudo-cell src.
                    id_to_src[pid] = psrc
                attrs = port.get("attributes", {})
                if not isinstance(attrs, dict):
                    attrs = {}
                pdir = str(attrs.get("rtlens_orig_direction", "") or port.get("direction", "")).strip().lower()
                if pdir not in {"input", "output", "inout"}:
                    pdir = "input"
                id_to_meta[pid] = {
                    "kind": "port",
                    "name": str(port_name),
                    "port_dir": pdir,
                }
        return id_to_src, module_src, id_to_meta

    def _load_schematic_net_annotations(
        self, json_path: str, module_name: str, svg_path: str = ""
    ) -> dict[str, dict]:
        out: dict[str, dict] = {}
        mod, module_src = self._load_schematic_json_module(json_path, module_name, svg_path=svg_path)
        if not mod:
            return out
        netnames = mod.get("netnames", {})
        if not isinstance(netnames, dict):
            return out
        for net_name, net in netnames.items():
            if not isinstance(net, dict):
                continue
            try:
                hide_name = int(net.get("hide_name", 0))
            except Exception:
                hide_name = 0
            if hide_name != 0:
                continue
            bits = net.get("bits", [])
            if not isinstance(bits, list) or not bits:
                continue
            bits_key = ",".join(str(int(b)) if isinstance(b, int) else str(b) for b in bits)
            if not bits_key:
                continue
            nsrc = self._normalize_schematic_src(str((net.get("attributes") or {}).get("src", "")), fallback=module_src)
            m = re.match(r"^(.*?):(\d+)$", nsrc)
            if not m or not os.path.isfile(m.group(1)):
                nsrc = ""
            label = str(net_name or "").strip()
            if not label:
                continue
            out[bits_key] = {
                "label": label,
                "src": nsrc,
            }
        return out

    def _resolve_schematic_src_from_svg_id(self, elem_id: str, id_to_src: dict[str, str], module_src: str) -> str:
        eid = str(elem_id or "").strip()
        if not eid:
            return ""
        if eid in id_to_src:
            return id_to_src[eid]
        if eid.startswith("port_"):
            base = eid[5:].split("~", 1)[0]
            src = id_to_src.get(f"cell_{base}", "")
            if src:
                return src
        if eid.startswith("cell_"):
            return id_to_src.get(eid, module_src)
        return ""

    def _parse_svg_hotspots(
        self, svg_path: str, json_path: str = "", module_name: str = ""
    ) -> tuple[tuple[float, float], List[dict]]:
        try:
            svg_text = Path(svg_path).read_text(encoding="utf-8", errors="ignore")
            svg_text = self._repair_schematic_svg_annotation_syntax(svg_text)
            root = ET.fromstring(svg_text)
        except Exception:
            return (0.0, 0.0), []

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        def num(v) -> float:
            try:
                return float(str(v).replace("px", ""))
            except Exception:
                return 0.0

        def parse_translate(v: str) -> tuple[float, float]:
            m = re.search(r"translate\(\s*([0-9.+-]+)\s*,\s*([0-9.+-]+)\s*\)", str(v or ""))
            if not m:
                return 0.0, 0.0
            return num(m.group(1)), num(m.group(2))

        def parse_points(v: str) -> List[tuple[float, float]]:
            out: List[tuple[float, float]] = []
            txt = str(v or "").strip()
            if not txt:
                return out
            for part in re.split(r"\s+", txt):
                if not part or "," not in part:
                    continue
                xs, ys = part.split(",", 1)
                out.append((num(xs), num(ys)))
            return out

        width = num(root.attrib.get("width", 0))
        height = num(root.attrib.get("height", 0))
        spots: List[dict] = []
        id_to_src, module_src, id_to_meta = self._load_schematic_svg_id_src_map(
            json_path, module_name, svg_path=svg_path
        )
        for elem in root.iter():
            elem_id = str(elem.attrib.get("id", "") or "")
            if not elem_id.startswith("cell_"):
                continue
            meta = id_to_meta.get(elem_id, {})
            node_kind_hint = str(meta.get("kind", "")).strip()
            inst_name_hint = str(meta.get("name", "")).strip() if node_kind_hint == "instance" else ""
            src_attr = self._normalize_schematic_src(str(elem.attrib.get("data-src", "") or ""), fallback=module_src)
            src_resolved = self._normalize_schematic_src(
                self._resolve_schematic_src_from_svg_id(elem_id, id_to_src, module_src),
                fallback=module_src,
            )
            src = src_attr
            # Prefer id-resolved src for known schematic nodes. This also fixes stale
            # cached SVGs that contain coarse module-level data-src values.
            if src_resolved and (not src or elem_id.startswith("cell_") or elem_id.startswith("port_")):
                src = src_resolved
            if not src and node_kind_hint == "instance" and inst_name_hint:
                src = self._resolve_instance_decl_src(self.current_hier_path or "", inst_name_hint)
            if not src and node_kind_hint == "port":
                src = module_src
            if not src and node_kind_hint not in {"instance", "port"}:
                continue
            tag = local(elem.tag)
            if tag != "g":
                continue
            tr = elem.attrib.get("transform", "")
            m = re.search(r"translate\(\s*([0-9.+-]+)\s*,\s*([0-9.+-]+)\s*\)", tr)
            if not m:
                continue
            x = num(m.group(1))
            y = num(m.group(2))
            w = num(elem.attrib.get("{https://github.com/nturley/netlistsvg}width", elem.attrib.get("width", 0)))
            h = num(elem.attrib.get("{https://github.com/nturley/netlistsvg}height", elem.attrib.get("height", 0)))
            if w <= 0 or h <= 0:
                continue
            # Compute an effective local bounding box from child primitives.
            # This gives a closer "instance-shaped" highlight than raw s:width/s:height.
            min_lx = 0.0
            min_ly = 0.0
            max_lx = w
            max_ly = h

            def include_rect(x0: float, y0: float, x1: float, y1: float) -> None:
                nonlocal min_lx, min_ly, max_lx, max_ly
                min_lx = min(min_lx, x0)
                min_ly = min(min_ly, y0)
                max_lx = max(max_lx, x1)
                max_ly = max(max_ly, y1)

            def scan_node(node: ET.Element, off_x: float, off_y: float) -> None:
                tag2 = local(node.tag)
                dx, dy = (0.0, 0.0)
                if tag2 == "g":
                    dx, dy = parse_translate(node.attrib.get("transform", ""))
                off_x += dx
                off_y += dy

                if tag2 == "rect":
                    rx = off_x + num(node.attrib.get("x", 0.0))
                    ry = off_y + num(node.attrib.get("y", 0.0))
                    rw = num(node.attrib.get("width", 0.0))
                    rh = num(node.attrib.get("height", 0.0))
                    if rw > 0 and rh > 0:
                        include_rect(rx, ry, rx + rw, ry + rh)
                elif tag2 == "line":
                    x1 = off_x + num(node.attrib.get("x1", 0.0))
                    y1 = off_y + num(node.attrib.get("y1", 0.0))
                    x2 = off_x + num(node.attrib.get("x2", 0.0))
                    y2 = off_y + num(node.attrib.get("y2", 0.0))
                    include_rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                elif tag2 == "circle":
                    cx = off_x + num(node.attrib.get("cx", 0.0))
                    cy = off_y + num(node.attrib.get("cy", 0.0))
                    r = num(node.attrib.get("r", 0.0))
                    if r > 0.0:
                        include_rect(cx - r, cy - r, cx + r, cy + r)
                elif tag2 == "ellipse":
                    cx = off_x + num(node.attrib.get("cx", 0.0))
                    cy = off_y + num(node.attrib.get("cy", 0.0))
                    rx = num(node.attrib.get("rx", 0.0))
                    ry = num(node.attrib.get("ry", 0.0))
                    if rx > 0.0 and ry > 0.0:
                        include_rect(cx - rx, cy - ry, cx + rx, cy + ry)
                elif tag2 in {"polygon", "polyline"}:
                    pts = parse_points(node.attrib.get("points", ""))
                    if pts:
                        xs = [off_x + p[0] for p in pts]
                        ys = [off_y + p[1] for p in pts]
                        include_rect(min(xs), min(ys), max(xs), max(ys))
                elif tag2 == "text":
                    text = str((node.text or "")).strip()
                    if text:
                        tx = off_x + num(node.attrib.get("x", 0.0))
                        ty = off_y + num(node.attrib.get("y", 0.0))
                        tw = max(10.0, float(len(text)) * 6.5)
                        th = 10.0
                        include_rect(tx - 2.0, ty - th, tx + tw + 2.0, ty + 2.0)

                for ch in list(node):
                    scan_node(ch, off_x, off_y)

            for ch in list(elem):
                scan_node(ch, 0.0, 0.0)

            x += min_lx
            y += min_ly
            w = max(1.0, max_lx - min_lx)
            h = max(1.0, max_ly - min_ly)
            stype = str(
                elem.attrib.get("{https://github.com/nturley/netlistsvg}type", elem.attrib.get("s:type", ""))
            ).strip()
            node_kind = str(meta.get("kind", "")).strip()
            inst_name = str(meta.get("name", "")).strip() if node_kind == "instance" else ""
            inst_mod = str(meta.get("module_name", "")).strip() if node_kind == "instance" else ""
            port_dir = str(meta.get("port_dir", "")).strip() if node_kind == "port" else ""
            if node_kind == "instance" and inst_name:
                decl_src = self._resolve_instance_decl_src(self.current_hier_path or "", inst_name)
                if decl_src:
                    src = decl_src
            rank = 1
            if stype in {"inputExt", "outputExt", "inoutExt"}:
                rank = 2
            if node_kind == "instance":
                rank = 4
            elif stype:
                rank = 3
            spots.append(
                {
                    "id": elem_id,
                    "stype": stype,
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "src": src,
                    "tag": tag,
                    "area": w * h,
                    "pick_rank": rank,
                    "node_kind": node_kind,
                    "instance_name": inst_name,
                    "instance_module": inst_mod,
                    "port_dir": port_dir,
                }
            )
        spots.sort(key=lambda s: (s["area"], s["tag"] != "g"))
        return (width, height), spots

    def _schematic_spot_index_at(self, view_x: float, view_y: float) -> int:
        if self.schematic_view_mode != "svg" or not self.schematic_svg_path or not self.schematic_hotspots:
            return -1
        rendered_w = max(1, self.schematic_view.width())
        rendered_h = max(1, self.schematic_view.height())
        src_w, src_h = self.schematic_svg_size
        if src_w <= 0 or src_h <= 0:
            return -1
        svg_x = view_x * (src_w / rendered_w)
        svg_y = view_y * (src_h / rendered_h)

        def _collect_hits(pad_x: float = 0.0, pad_y: float = 0.0) -> List[tuple[int, dict]]:
            hits: List[tuple[int, dict]] = []
            for idx, spot in enumerate(self.schematic_hotspots):
                if (
                    spot["x"] - pad_x <= svg_x <= spot["x"] + spot["w"] + pad_x
                    and spot["y"] - pad_y <= svg_y <= spot["y"] + spot["h"] + pad_y
                ):
                    hits.append((idx, spot))
            return hits

        def _pick_best(hits: List[tuple[int, dict]]) -> int:
            if not hits:
                return -1
            # Prefer richer objects (cells) and larger regions so users can click
            # around labels without being forced onto tiny pin markers.
            hits.sort(
                key=lambda it: (
                    int(it[1].get("pick_rank", 0)),
                    float(it[1].get("area", 0.0)),
                ),
                reverse=True,
            )
            return int(hits[0][0])

        direct = _pick_best(_collect_hits())
        if direct >= 0:
            return direct
        tol_px = 3.0
        pad_x = tol_px * (src_w / rendered_w)
        pad_y = tol_px * (src_h / rendered_h)
        return _pick_best(_collect_hits(pad_x, pad_y))

    def _selected_schematic_spot(self) -> Optional[dict]:
        idx = int(self.schematic_selected_spot_index)
        if idx < 0 or idx >= len(self.schematic_hotspots):
            return None
        return self.schematic_hotspots[idx]

    def _set_selected_schematic_spot(self, idx: int) -> bool:
        if idx < 0 or idx >= len(self.schematic_hotspots):
            self.schematic_selected_spot_index = -1
            self.schematic_selected_src = ""
            return False
        self.schematic_selected_spot_index = idx
        spot = self.schematic_hotspots[idx]
        self.schematic_selected_src = str(spot.get("src", "") or "")
        return True

    def on_schematic_svg_click(self, view_x: float, view_y: float) -> None:
        idx = self._schematic_spot_index_at(view_x, view_y)
        if idx < 0:
            return
        if not self._set_selected_schematic_spot(idx):
            return
        self._apply_schematic_zoom()
        raw = self.schematic_selected_src
        chunk = raw.split("|", 1)[0].strip() if raw else ""
        self.set_status(f"Schematic selected: {chunk or '(unknown)'}")

    def _dive_into_instance_from_schematic_spot(self, spot: dict) -> bool:
        if str(spot.get("node_kind", "")).strip() != "instance":
            return False
        inst_name = str(spot.get("instance_name", "")).strip()
        if not inst_name:
            return False
        parent = (self.current_hier_path or "").strip()
        if not parent:
            return False
        target_hier = self._resolve_child_hier_from_instance_name(parent, inst_name)
        if not target_hier:
            self.set_status(f"Instance hier not found: {parent}.{inst_name}")
            return False
        item = self.hier_path_to_item.get(target_hier)
        if item is None:
            self.set_status(f"Hierarchy item not found: {target_hier}")
            return False
        self.hier_tree.setCurrentItem(item)
        self.hier_tree.scrollToItem(item)
        self.set_status(f"Dive hierarchy: {target_hier}")
        return True

    def on_schematic_svg_double_click(self, view_x: float, view_y: float, ctrl_pressed: bool = False) -> None:
        idx = self._schematic_spot_index_at(view_x, view_y)
        spot = None
        if idx >= 0:
            self._set_selected_schematic_spot(idx)
            spot = self._selected_schematic_spot()
            self._apply_schematic_zoom()
        if ctrl_pressed and spot is not None and self._dive_into_instance_from_schematic_spot(spot):
            return
        raw = self.schematic_selected_src
        if raw:
            self._jump_to_schematic_src(raw)

    def _jump_to_schematic_src(self, raw: str) -> None:
        if not raw:
            return
        parsed: List[tuple[str, int]] = []
        for chunk in str(raw).split("|"):
            c = chunk.strip()
            if not c:
                continue
            m = re.match(r"^(.*?):(\d+)", c)
            if not m:
                continue
            parsed.append((m.group(1), max(1, int(m.group(2)))))
        if not parsed:
            self.set_status(f"Schematic src: {raw}")
            return
        file, line = parsed[0]
        for pf, pl in parsed:
            if os.path.isfile(pf):
                file, line = pf, pl
                break
        self.show_file(file, line)
        self.right_tabs.setCurrentIndex(0)
        self.set_status(f"Schematic src: {file}:{line}")

    def adjust_schematic_zoom(self, factor: float) -> None:
        if self.schematic_view_mode != "svg":
            return
        self.schematic_zoom = max(0.1, min(8.0, self.schematic_zoom * factor))
        self._apply_schematic_zoom()

    def fit_schematic(self) -> None:
        if self.schematic_view_mode != "svg" or not self.schematic_svg_path or not hasattr(self, "schematic_scroll"):
            return
        try:
            svg_bytes = Path(self.schematic_svg_path).read_bytes()
            renderer = QSvgRenderer()
            renderer.load(QByteArray(svg_bytes))
            if not renderer.isValid():
                self.schematic_view.setText(f"SVG render failed: {self.schematic_svg_path}")
                return
            size = renderer.defaultSize()
            viewport = self.schematic_scroll.viewport().size()
            if size.width() <= 0 or size.height() <= 0:
                return
            fx = max(0.1, (viewport.width() - 24) / size.width())
            fy = max(0.1, (viewport.height() - 24) / size.height())
            self.schematic_zoom = max(0.1, min(8.0, min(fx, fy)))
            self._apply_schematic_zoom()
        except Exception as e:
            self.schematic_view.setText(f"SVG fit failed: {e}")

    def rebuild_schematic_prebuild_top(self) -> None:
        if not self.schematic_tab_enabled:
            self.set_status("Schematic tab is disabled (use --schematic-prebuild-top)")
            return
        title = "Rebuild Prebuild (Top)"
        msg = (
            "This operation reparses RTL and reruns strict top prebuild.\n"
            "It can take a long time.\n\n"
            f"top: {self.schematic_prebuild_top}"
        )
        try:
            ret = QMessageBox.question(self, title, msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                self.set_status("Schematic prebuild rebuild canceled.")
                return
        except Exception:
            pass
        self.set_status(f"Schematic prebuild rebuild started: {self.schematic_prebuild_top}")
        ok = self.reload_rtl(clear_trace=False)
        if not ok:
            self.set_status("Schematic prebuild rebuild skipped (no source target)")
            return
        self.schematic_dirty = True
        self.refresh_schematic_if_dirty()
        self.set_status(f"Schematic prebuild rebuild finished: {self.schematic_prebuild_top}")

    def refresh_schematic(self, manual: bool = False) -> None:
        if not self.schematic_tab_enabled:
            self.set_status("Schematic tab is disabled (use --schematic-prebuild-top)")
            return
        hier_path = str(self.current_hier_path or "").strip()
        mod = self._current_module_for_schematic()
        if not mod:
            self.schematic_info_label.setText("No hierarchy selection.")
            self._set_schematic_log_text("Schematic generation skipped: no hierarchy selection.")
            self.schematic_hotspots = []
            self.schematic_net_segments = []
            self.schematic_net_labels = []
            self.schematic_instance_search_hits = []
            self.schematic_instance_search_index = -1
            self._refresh_schematic_instance_hit_list()
            self.schematic_selected_spot_index = -1
            self.schematic_selected_src = ""
            if hasattr(self.schematic_view, "setHtml"):
                self.schematic_view.setHtml("<html><body><p>No hierarchy selection.</p></body></html>")
            elif hasattr(self.schematic_view, "setText"):
                self.schematic_view.setText("No hierarchy selection.")
            else:
                self.schematic_view.setPlainText("No hierarchy selection.")
            return
        if self._schematic_worker and self._schematic_worker.is_alive():
            self._schematic_pending_refresh = True
            self.set_status(f"Schematic generation already running: {self._schematic_running_module}")
            return
        cache_key = self._schematic_cache_key_for_hier_path(hier_path) or mod
        cached = self._cached_schematic_result_for_key(cache_key, mod)
        if cached is not None:
            self.schematic_info_label.setText(f"Loading cached schematic for {self._schematic_cache_label(cache_key, mod)} ...")
            self.schematic_refresh_btn.setEnabled(False)
            self._schematic_queue.put((cache_key, cached))
            return
        fail_log = self.schematic_prebuild_fail_logs.get(cache_key, "")
        self.schematic_info_label.setText(f"{self._schematic_cache_label(cache_key, mod)}: prebuild cache miss")
        self.schematic_hotspots = []
        self.schematic_net_segments = []
        self.schematic_net_labels = []
        self.schematic_instance_search_hits = []
        self.schematic_instance_search_index = -1
        self._refresh_schematic_instance_hit_list()
        self.schematic_selected_spot_index = -1
        self.schematic_selected_src = ""
        if fail_log:
            self._set_schematic_log_text(fail_log)
        else:
            self._set_schematic_log_text(self._build_schematic_cache_miss_detail(cache_key, mod))
        ttl_remain = self._schematic_failure_ttl_remaining_sec(cache_key)
        if manual and ttl_remain > 0.0:
            self._clear_schematic_failure_entry(cache_key)
            self._save_schematic_cache_index()
            if cache_key in self.schematic_prebuild_fail_logs:
                self.schematic_prebuild_fail_logs.pop(cache_key, None)
            self._schematic_prebuild_emit_progress(
                f"[rtlens] schematic manual retry key={cache_key} module={mod} ttl_remaining={int(round(ttl_remain))}s"
            )
            self.set_status(f"Schematic retry requested: {self._schematic_cache_label(cache_key, mod)}")
        if hasattr(self.schematic_view, "setPlainText"):
            self.schematic_view.setPlainText(
                "Schematic prebuild cache miss.\n"
                "Strict Top-Only mode is enabled, so on-demand generation is disabled.\n"
                "Re-run startup prebuild for the configured top module."
            )
        elif hasattr(self.schematic_view, "setText"):
            self.schematic_view.setText(
                "Schematic prebuild cache miss.\n"
                "Strict Top-Only mode is enabled.\n"
                "Check Schematic log for details."
            )
        self.set_status(f"Schematic cache miss: {self._schematic_cache_label(cache_key, mod)}")
        return

    def refresh_rtl_structure(self) -> None:
        if not self.current_hier_path or self.current_hier_path not in self.design.hier:
            self.rtl_info_label.setText("No hierarchy selection.")
            self.rtl_log.setPlainText("RTL structure generation skipped: no hierarchy selection.")
            self.rtl_scene.clear()
            self.rtl_scene.addText("No hierarchy selection.")
            return
        mod = self.design.hier[self.current_hier_path].module_name or ""
        if not mod:
            self.rtl_info_label.setText("No module selected.")
            self.rtl_log.setPlainText("RTL structure generation skipped: no module selected.")
            self.rtl_scene.clear()
            self.rtl_scene.addText("No module selected.")
            return
        if self._rtl_structure_worker and self._rtl_structure_worker.is_alive():
            self._rtl_structure_pending_refresh = True
            self.set_status(f"RTL structure generation already running: {self._rtl_structure_running_module}")
            return
        self.rtl_info_label.setText(f"Generating RTL structure for {mod} ...")
        self.rtl_refresh_btn.setEnabled(False)
        self.rtl_scene.clear()
        self.rtl_scene.addText("Generating RTL structure...")
        self.rtl_structure_elk_layout = None
        self.rtl_structure_node_boxes = []
        self._rtl_structure_running_module = mod
        hier_path = self.current_hier_path
        dot_cmd = getattr(self.args, "rtl_structure_dot_cmd", "") or "dot"
        rtl_mode = "auto"
        if hasattr(self, "rtl_mode_combo"):
            rtl_mode = str(self.rtl_mode_combo.currentData() or "auto")
        timeout = estimate_rtl_structure_timeout(
            self.design,
            hier_path,
            max(1, int(getattr(self.args, "rtl_structure_timeout", 8))),
        )

        def _worker() -> None:
            try:
                debug_note = ""
                if is_rtl_debug_enabled():
                    try:
                        dump = run_rtl_debug_pipeline(
                            design=self.design,
                            hier_path=hier_path,
                            mode=rtl_mode,
                            node_cmd="node",
                            timeout=timeout,
                            run_layout=False,
                        )
                        debug_note = f"debug dump: {dump['run_dir']}\n"
                    except Exception as debug_exc:
                        debug_note = f"debug dump failed: {debug_exc}\n"
                prep = profile_rtl_structure_elk_graph(self.design, hier_path, mode=rtl_mode)
                graph = build_rtl_structure_elk_graph(self.design, hier_path, mode=rtl_mode)
                elk_t0 = time.perf_counter()
                elk_layout = render_elk_layout(
                    graph,
                    node_cmd="node",
                    timeout=timeout,
                )
                elk_t1 = time.perf_counter()
                log = (
                    "[rtlens] rtl structure\n"
                    f"module: {mod}\n"
                    f"hier_path: {hier_path}\n"
                    "backend: elk\n"
                    f"mode: {rtl_mode}\n"
                    "node command: node\n"
                    f"timeout: {timeout}\n"
                    f"runtime variant: {prep.get('runtime_variant', 'unknown')}\n"
                    f"fast layout: {prep['fast_layout']}\n"
                    f"full graph: nodes={prep['full_nodes']} edges={prep['full_edges']} signals={prep['full_signals']}\n"
                    f"selected graph: nodes={prep['selected_nodes']} edges={prep['selected_edges']} signals={prep['selected_signals']}\n"
                    f"callables: full={prep.get('full_callables', 0)} selected={prep.get('selected_callables', 0)} filtered={prep.get('filtered_callables', 0)}\n"
                    f"elk input: children={prep['graph_children']} edges={prep['graph_edges']}\n"
                    f"{debug_note}"
                    f"timing: build_view={prep['timing_build_view_ms']}ms "
                    f"select_view={prep['timing_select_view_ms']}ms "
                    f"build_graph={prep['timing_build_graph_ms']}ms "
                    f"elk={round((elk_t1 - elk_t0) * 1000, 1)}ms\n"
                )
                self._rtl_structure_queue.put((mod, b"", "", "", log, "", elk_layout))
            except Exception as elk_exc:
                extra_lines: List[str] = []
                prep = None
                try:
                    prep = profile_rtl_structure_elk_graph(self.design, hier_path, mode=rtl_mode)
                    extra_lines.append(
                        f"full graph: nodes={prep['full_nodes']} edges={prep['full_edges']} signals={prep['full_signals']}\n"
                    )
                    extra_lines.append(
                        f"selected graph: nodes={prep['selected_nodes']} edges={prep['selected_edges']} signals={prep['selected_signals']}\n"
                    )
                    extra_lines.append(
                        f"callables: full={prep.get('full_callables', 0)} "
                        f"selected={prep.get('selected_callables', 0)} "
                        f"filtered={prep.get('filtered_callables', 0)}\n"
                    )
                    extra_lines.append(
                        f"elk input: children={prep['graph_children']} edges={prep['graph_edges']}\n"
                    )
                    extra_lines.append(
                        f"timing: build_view={prep['timing_build_view_ms']}ms "
                        f"select_view={prep['timing_select_view_ms']}ms "
                        f"build_graph={prep['timing_build_graph_ms']}ms\n"
                    )
                    if is_rtl_debug_enabled():
                        try:
                            dump = run_rtl_debug_pipeline(
                                design=self.design,
                                hier_path=hier_path,
                                mode=rtl_mode,
                                node_cmd="node",
                                timeout=timeout,
                                run_layout=False,
                            )
                            extra_lines.append(f"debug dump: {dump['run_dir']}\n")
                        except Exception as debug_exc:
                            extra_lines.append(f"debug dump failed: {debug_exc}\n")
                except Exception as prep_exc:
                    extra_lines.append(f"profile failed: {prep_exc}\n")
                extra = "".join(extra_lines) + "\n"
                runtime_variant = prep.get("runtime_variant", "unknown") if isinstance(prep, dict) else "unknown"
                fast_layout = prep.get("fast_layout", "n/a") if isinstance(prep, dict) else "n/a"
                if isinstance(elk_exc, subprocess.TimeoutExpired) or timeout >= 30:
                    log = (
                        "[rtlens] rtl structure\n"
                        f"module: {mod}\n"
                        f"hier_path: {hier_path}\n"
                        "backend: elk\n"
                        f"mode: {rtl_mode}\n"
                        "node command: node\n"
                        f"timeout: {timeout}\n"
                        f"runtime variant: {runtime_variant}\n"
                        f"fast layout: {fast_layout}\n"
                        + extra
                        + "\n"
                        + traceback.format_exc()
                    )
                    self._rtl_structure_queue.put((mod, b"", "", "", log, str(elk_exc), None))
                    return
                try:
                    render = build_rtl_structure_render(self.design, hier_path, dot_cmd=dot_cmd, timeout=timeout)
                    log = (
                        "[rtlens] rtl structure\n"
                        f"module: {mod}\n"
                        f"hier_path: {hier_path}\n"
                        "backend: graphviz-fallback\n"
                        f"dot command: {dot_cmd}\n"
                        f"timeout: {timeout}\n"
                    )
                    self._rtl_structure_queue.put((mod, render.png, render.cmapx, render.svg, log, "", None))
                    return
                except Exception as e:
                    log = (
                        "[rtlens] rtl structure\n"
                        f"module: {mod}\n"
                        f"hier_path: {hier_path}\n"
                        "backend: elk+graphviz-fallback\n"
                        f"dot command: {dot_cmd}\n"
                        f"timeout: {timeout}\n\n"
                        + traceback.format_exc()
                    )
                    self._rtl_structure_queue.put((mod, b"", "", "", log, str(e), None))
                    return

        self._rtl_structure_worker = threading.Thread(target=_worker, daemon=True)
        self._rtl_structure_worker.start()

    def benchmark_rtl_structure(self) -> None:
        if not bool(getattr(self.args, "dev_ui", False)):
            self.set_status("RTL ELK benchmark UI is disabled (use --dev-ui)")
            return
        if not self.current_hier_path or self.current_hier_path not in self.design.hier:
            self.set_status("RTL ELK benchmark skipped: no hierarchy selection")
            return
        if self._rtl_structure_worker and self._rtl_structure_worker.is_alive():
            self.set_status("RTL structure generation is running")
            return
        if self._rtl_bench_worker and self._rtl_bench_worker.is_alive():
            self.set_status("RTL ELK benchmark already running")
            return
        mod = self.design.hier[self.current_hier_path].module_name or ""
        if not mod:
            self.set_status("RTL ELK benchmark skipped: no module selected")
            return
        hier_path = self.current_hier_path
        rtl_mode = "auto"
        if hasattr(self, "rtl_mode_combo"):
            rtl_mode = str(self.rtl_mode_combo.currentData() or "auto")
        bench_timeout_raw = int(getattr(self.args, "rtl_structure_benchmark_timeout", 2400))
        timeout: int | None
        if bench_timeout_raw <= 0:
            timeout = None
        else:
            timeout = max(
                1,
                estimate_rtl_structure_timeout(
                    self.design,
                    hier_path,
                    bench_timeout_raw,
                ),
            )
        self.rtl_bench_btn.setEnabled(False)
        self.rtl_info_label.setText(f"Benchmarking ELK for {mod} ...")
        self.rtl_log.setPlainText("Benchmarking ELK variants...")

        def _worker() -> None:
            try:
                bench = benchmark_rtl_structure_elk_graph(
                    self.design,
                    hier_path,
                    mode=rtl_mode,
                    node_cmd="node",
                    timeout=timeout,
                )
                stats = bench.get("stats", {})
                results = bench.get("results", [])
                lines = [
                    "[rtlens] rtl structure elk bench",
                    f"module: {mod}",
                    f"hier_path: {hier_path}",
                    f"mode: {rtl_mode}",
                    "node command: node",
                    f"timeout: {'none' if timeout is None else timeout}",
                    f"runtime variant: {stats.get('runtime_variant')}",
                    f"runtime auto fast layout: {stats.get('runtime_fast_layout')}",
                    f"benchmark graph uses full view: {stats.get('benchmark_uses_full_graph')}",
                    (
                        "full graph: "
                        f"nodes={stats.get('full_nodes', 0)} "
                        f"edges={stats.get('full_edges', 0)} "
                        f"signals={stats.get('full_signals', 0)}"
                    ),
                    (
                        "selected graph: "
                        f"nodes={stats.get('selected_nodes', 0)} "
                        f"edges={stats.get('selected_edges', 0)} "
                        f"signals={stats.get('selected_signals', 0)}"
                    ),
                    (
                        "callables: "
                        f"full={stats.get('full_callables', 0)} "
                        f"selected={stats.get('selected_callables', 0)} "
                        f"filtered={stats.get('filtered_callables', 0)}"
                    ),
                    (
                        "elk input: "
                        f"children={stats.get('graph_children', 0)} "
                        f"edges={stats.get('graph_edges', 0)}"
                    ),
                    (
                        "timing(prep): "
                        f"build_view={stats.get('timing_build_view_ms', 0)}ms "
                        f"select_view={stats.get('timing_select_view_ms', 0)}ms "
                        f"build_graph={stats.get('timing_build_graph_ms', 0)}ms"
                    ),
                    "",
                    "variants:",
                ]
                for res in results:
                    status = str(res.get("status", "unknown"))
                    name = str(res.get("name", "unknown"))
                    if status == "ok":
                        lines.append(
                            "  - "
                            f"{name}: ok {res.get('elapsed_ms', 0)}ms "
                            f"(children={res.get('children', 0)} edges={res.get('edges', 0)} "
                            f"size={res.get('width', 0)}x{res.get('height', 0)})"
                        )
                    elif status == "timeout":
                        lines.append(
                            "  - "
                            f"{name}: timeout after {res.get('elapsed_ms', (timeout or 0) * 1000)}ms "
                            f"(children={res.get('children', 0)} edges={res.get('edges', 0)})"
                        )
                    else:
                        lines.append(
                            "  - "
                            f"{name}: error {res.get('error', 'unknown')} "
                            f"(children={res.get('children', 0)} edges={res.get('edges', 0)})"
                        )
                self._rtl_bench_queue.put((mod, "\n".join(lines), ""))
            except Exception as e:
                log = (
                    "[rtlens] rtl structure elk bench\n"
                    f"module: {mod}\n"
                    f"hier_path: {hier_path}\n"
                    f"mode: {rtl_mode}\n"
                    "node command: node\n"
                    f"timeout: {'none' if timeout is None else timeout}\n\n"
                    + traceback.format_exc()
                )
                self._rtl_bench_queue.put((mod, log, str(e)))

        self._rtl_bench_worker = threading.Thread(target=_worker, daemon=True)
        self._rtl_bench_worker.start()

    def open_schematic_external(self) -> None:
        if not self.schematic_result or not self.schematic_result.html_path:
            self.set_status("No schematic generated")
            return
        try:
            cache_dir = Path(os.path.expanduser("~/rtlens_schematic"))
            cache_dir.mkdir(parents=True, exist_ok=True)
            inst_targets = self._external_schematic_instance_targets(export_dir=cache_dir)
            prepared_html = self._prepare_external_html_file(
                html_src_path=str(self.schematic_result.html_path),
                svg_src_path=str(self.schematic_result.svg_path or ""),
                json_path=str(self.schematic_result.json_path or ""),
                module_name=str(self.schematic_result.module_name or ""),
                export_dir=cache_dir,
                instance_targets=inst_targets,
            )
            if not prepared_html:
                raise RuntimeError("failed to prepare external schematic html")
            self._open_local_file_external(str(prepared_html))
            self.set_status(f"Opened schematic: {prepared_html}")
        except Exception as e:
            self.set_status(f"open schematic failed: {e}")

    def _open_local_file_external(self, path_text: str) -> None:
        target = Path(str(path_text or "")).resolve()
        if not target.exists():
            raise RuntimeError(f"path not found: {target}")

        if QDesktopServices is not object and QUrl is not object:
            try:
                url = QUrl.fromLocalFile(str(target))
                if QDesktopServices.openUrl(url):
                    return
            except Exception:
                pass

        if os.name == "nt":
            try:
                os.startfile(str(target))  # type: ignore[attr-defined]
                return
            except Exception:
                pass

        if sys.platform == "darwin":
            cmd = ["open", str(target)]
        else:
            cmd = ["xdg-open", str(target)]
        if not shutil.which(cmd[0]):
            raise RuntimeError(
                f"open command not found: {cmd[0]}. "
                "install desktop opener, or ensure Qt desktop services are available."
            )
        subprocess.Popen(cmd, shell=False)

    def _find_cached_schematic_html_by_module(self, module_name: str, exclude_keys: Optional[Set[str]] = None) -> str:
        mod = self._canonical_schematic_name(module_name)
        if not mod:
            return ""
        excludes = set(exclude_keys or set())
        for key, entry in self.schematic_cache_index.items():
            if key in excludes or not isinstance(entry, dict):
                continue
            if str(entry.get("status", "ok")).strip().lower() == "fail":
                continue
            ent_mod = self._canonical_schematic_name(str(entry.get("module_name", "") or ""))
            if ent_mod != mod:
                continue
            html_path = str(entry.get("html_path", "") or "").strip()
            if html_path and os.path.isfile(html_path):
                return html_path
        return ""

    def _to_local_file_uri(self, path_text: str) -> str:
        p = Path(str(path_text or "")).resolve()
        if QUrl is not object:
            try:
                return QUrl.fromLocalFile(str(p)).toString()
            except Exception:
                pass
        return p.as_uri()

    def _prepare_external_html_file(
        self,
        html_src_path: str,
        svg_src_path: str = "",
        json_path: str = "",
        module_name: str = "",
        export_dir: Optional[Path] = None,
        instance_targets: Optional[dict[str, str]] = None,
    ) -> str:
        html_src = Path(str(html_src_path or "")).resolve()
        if not html_src.is_file():
            return ""
        out_dir = export_dir if export_dir is not None else html_src.parent
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        html_dst = (out_dir / html_src.name).resolve()

        svg_text = ""
        svg_src = Path(str(svg_src_path or "")).resolve() if str(svg_src_path or "").strip() else None
        if svg_src is not None and svg_src.is_file():
            try:
                svg_text = svg_src.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                svg_text = ""
            if svg_text and "data-src=" not in svg_text and json_path:
                try:
                    svg_text = _inject_svg_data_src_from_json(
                        svg_text,
                        Path(str(json_path)).resolve(),
                        str(module_name or ""),
                    )
                except Exception:
                    pass
            if svg_text:
                try:
                    svg_text = _inline_svg_styles_for_qt(svg_text)
                except Exception:
                    pass
            svg_dst = (out_dir / svg_src.name).resolve()
            if svg_text:
                try:
                    svg_dst.write_text(svg_text, encoding="utf-8")
                except Exception:
                    pass
            else:
                try:
                    if svg_src != svg_dst:
                        shutil.copy2(svg_src, svg_dst)
                except Exception:
                    pass

        html_text = html_src.read_text(encoding="utf-8", errors="ignore")
        if svg_text and "data-src=" in svg_text:
            html_text = re.sub(
                r"<svg[\s\S]*?</svg>",
                lambda _m: svg_text,
                html_text,
                count=1,
            )
        html_text = self._augment_external_schematic_html(
            html_text,
            instance_targets=instance_targets,
        )
        html_dst.write_text(html_text, encoding="utf-8")
        return str(html_dst)

    def _copy_external_schematic_html(self, html_path: str, export_dir: Path) -> str:
        src = Path(str(html_path or "")).resolve()
        if not src.is_file():
            return ""
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            dst = (export_dir / src.name).resolve()
            if src != dst:
                shutil.copy2(src, dst)
            return str(dst)
        except Exception:
            return str(src)

    def _external_schematic_instance_targets(self, export_dir: Optional[Path] = None) -> dict[str, str]:
        out: dict[str, str] = {}
        cur_path = str(self.current_hier_path or "").strip()
        if not cur_path or not self.schematic_hotspots:
            return out
        current_key = self._schematic_cache_key_for_hier_path(cur_path)
        excludes: Set[str] = set()
        if current_key:
            excludes.add(current_key)
        for spot in self.schematic_hotspots:
            if str(spot.get("node_kind", "")).strip() != "instance":
                continue
            elem_id = str(spot.get("id", "")).strip()
            inst_name = str(spot.get("instance_name", "")).strip()
            inst_mod = str(spot.get("instance_module", "")).strip()
            if not elem_id:
                continue
            html_target = ""
            if inst_name:
                child_path = self._resolve_child_hier_from_instance_name(cur_path, inst_name)
                child_key = self._schematic_cache_key_for_hier_path(child_path) if child_path else ""
                if child_key:
                    res = self._cached_schematic_result_for_key(child_key, inst_mod)
                    if res and res.html_path and os.path.isfile(res.html_path):
                        html_target = str(Path(res.html_path).resolve())
            if not html_target and inst_mod:
                html_target = self._find_cached_schematic_html_by_module(inst_mod, exclude_keys=excludes)
            if html_target:
                try:
                    if export_dir is not None:
                        target_entry = None
                        if inst_name:
                            child_path = self._resolve_child_hier_from_instance_name(cur_path, inst_name)
                            child_key = self._schematic_cache_key_for_hier_path(child_path) if child_path else ""
                            if child_key:
                                cand = self.schematic_cache_index.get(child_key, {})
                                if isinstance(cand, dict):
                                    target_entry = cand
                        if target_entry is None:
                            for _k, _entry in self.schematic_cache_index.items():
                                if not isinstance(_entry, dict):
                                    continue
                                if str(_entry.get("html_path", "")).strip() == str(html_target).strip():
                                    target_entry = _entry
                                    break
                        if isinstance(target_entry, dict):
                            prepared = self._prepare_external_html_file(
                                html_src_path=str(target_entry.get("html_path", "") or html_target),
                                svg_src_path=str(target_entry.get("svg_path", "") or ""),
                                json_path=str(target_entry.get("json_path", "") or ""),
                                module_name=str(target_entry.get("module_name", "") or inst_mod),
                                export_dir=export_dir,
                                instance_targets={},
                            )
                            if prepared:
                                html_target = prepared
                            else:
                                copied = self._copy_external_schematic_html(html_target, export_dir)
                                if copied:
                                    html_target = copied
                        else:
                            copied = self._copy_external_schematic_html(html_target, export_dir)
                            if copied:
                                html_target = copied
                    out[elem_id] = self._to_local_file_uri(html_target)
                except Exception:
                    pass
        return out

    def _external_schematic_net_rules(self) -> List[dict]:
        out: List[dict] = []
        for rule in self.schematic_net_highlights:
            if not isinstance(rule, dict):
                continue
            query = str(rule.get("query", "")).strip()
            if not query:
                continue
            color = self._normalize_color_hex(str(rule.get("color", ""))) or "#d73a49"
            enabled = bool(rule.get("enabled", True))
            out.append({"query": query, "color": color, "enabled": enabled})
        return out

    def _augment_external_schematic_html(
        self,
        html_text: str,
        instance_targets: Optional[dict[str, str]] = None,
    ) -> str:
        src = str(html_text or "")
        if not src:
            return src
        inst_targets = dict(instance_targets or self._external_schematic_instance_targets())
        net_rules = self._external_schematic_net_rules()
        script = (
            "<style id=\"rtlens-external-layout-fix\">\n"
            "html, body { height: 100vh !important; margin: 0 !important; overflow: hidden !important; }\n"
            ".layout { height: 100vh !important; min-height: 0 !important; }\n"
            ".canvas {\n"
            "  height: 100vh !important;\n"
            "  min-height: 0 !important;\n"
            "  overflow: scroll !important;\n"
            "  scrollbar-gutter: stable both-edges;\n"
            "  box-sizing: border-box;\n"
            "}\n"
            ".sidebar {\n"
            "  height: 100vh !important;\n"
            "  max-height: 100vh !important;\n"
            "  overflow: auto !important;\n"
            "  box-sizing: border-box;\n"
            "}\n"
            ".canvas > svg { display: block; }\n"
            "</style>\n"
            "<script>\n"
            "(function(){\n"
            "  const SVVIEW_INST_TARGETS = "
            + json.dumps(inst_targets, ensure_ascii=False)
            + ";\n"
            "  const SVVIEW_NET_RULES = "
            + json.dumps(net_rules, ensure_ascii=False)
            + ";\n"
            "  function pickNetColor(labelRaw){\n"
            "    const label = String(labelRaw || '').toLowerCase();\n"
            "    if (!label) return '';\n"
            "    for (const rule of SVVIEW_NET_RULES){\n"
            "      if (!rule || !rule.enabled) continue;\n"
            "      const q = String(rule.query || '').toLowerCase();\n"
            "      if (!q) continue;\n"
            "      if (label.includes(q)) return String(rule.color || '#d73a49');\n"
            "    }\n"
            "    return '';\n"
            "  }\n"
            "  function applyNetHighlight(){\n"
            "    document.querySelectorAll('svg [data-net-label]').forEach((el) => {\n"
            "      const color = pickNetColor(el.getAttribute('data-net-label'));\n"
            "      if (!color) return;\n"
            "      const tag = (el.tagName || '').toLowerCase();\n"
            "      if (tag === 'text') {\n"
            "        el.style.fill = color;\n"
            "        el.style.stroke = 'none';\n"
            "      } else {\n"
            "        el.style.stroke = color;\n"
            "        el.style.strokeWidth = '2.5';\n"
            "      }\n"
            "    });\n"
            "  }\n"
            "  function setNotice(msg){\n"
            "    const el = document.getElementById('selected-src');\n"
            "    if (el) el.textContent = String(msg || '');\n"
            "  }\n"
            "  let rtlensZoom = 1.0;\n"
            "  function applyZoom(){\n"
            "    const svg = document.querySelector('.canvas svg');\n"
            "    if (!svg) return;\n"
            "    svg.style.transformOrigin = '0 0';\n"
            "    svg.style.transform = 'scale(' + rtlensZoom + ')';\n"
            "  }\n"
            "  function clampZoom(z){\n"
            "    return Math.max(0.1, Math.min(8.0, z));\n"
            "  }\n"
            "  function fitZoom(){\n"
            "    const canvas = document.querySelector('.canvas');\n"
            "    const svg = document.querySelector('.canvas svg');\n"
            "    if (!canvas || !svg || !svg.getBBox) return;\n"
            "    const prev = rtlensZoom;\n"
            "    rtlensZoom = 1.0;\n"
            "    applyZoom();\n"
            "    let bbox;\n"
            "    try { bbox = svg.getBBox(); } catch (_e) { bbox = null; }\n"
            "    if (!bbox || !bbox.width || !bbox.height) {\n"
            "      rtlensZoom = prev;\n"
            "      applyZoom();\n"
            "      return;\n"
            "    }\n"
            "    const zx = (canvas.clientWidth - 24) / bbox.width;\n"
            "    const zy = (canvas.clientHeight - 24) / bbox.height;\n"
            "    rtlensZoom = clampZoom(Math.min(zx, zy));\n"
            "    applyZoom();\n"
            "    updateZoomLabel();\n"
            "  }\n"
            "  function updateZoomLabel(){\n"
            "    const el = document.getElementById('rtlens-zoom-label');\n"
            "    if (el) el.textContent = Math.round(rtlensZoom * 100) + '%';\n"
            "  }\n"
            "  function buildZoomCtl(){\n"
            "    const box = document.createElement('div');\n"
            "    box.id = 'rtlens-zoom-ctl';\n"
            "    box.style.position = 'fixed';\n"
            "    box.style.top = '10px';\n"
            "    box.style.right = '14px';\n"
            "    box.style.zIndex = '9999';\n"
            "    box.style.background = '#f8fafc';\n"
            "    box.style.border = '1px solid #c7d2e0';\n"
            "    box.style.borderRadius = '8px';\n"
            "    box.style.padding = '6px';\n"
            "    box.style.display = 'flex';\n"
            "    box.style.gap = '6px';\n"
            "    const mkBtn = (txt, fn) => {\n"
            "      const b = document.createElement('button');\n"
            "      b.textContent = txt;\n"
            "      b.style.minWidth = '40px';\n"
            "      b.style.cursor = 'pointer';\n"
            "      b.addEventListener('click', fn);\n"
            "      return b;\n"
            "    };\n"
            "    const minus = mkBtn('-', () => { rtlensZoom = clampZoom(rtlensZoom * 0.8); applyZoom(); updateZoomLabel(); });\n"
            "    const plus = mkBtn('+', () => { rtlensZoom = clampZoom(rtlensZoom * 1.25); applyZoom(); updateZoomLabel(); });\n"
            "    const fit = mkBtn('Fit', () => fitZoom());\n"
            "    const lbl = document.createElement('span');\n"
            "    lbl.id = 'rtlens-zoom-label';\n"
            "    lbl.style.minWidth = '52px';\n"
            "    lbl.style.textAlign = 'right';\n"
            "    lbl.style.lineHeight = '30px';\n"
            "    box.appendChild(minus);\n"
            "    box.appendChild(plus);\n"
            "    box.appendChild(fit);\n"
            "    box.appendChild(lbl);\n"
            "    document.body.appendChild(box);\n"
            "    updateZoomLabel();\n"
            "  }\n"
            "  document.addEventListener('dblclick', (event) => {\n"
            "    if (!event.ctrlKey) return;\n"
            "    const host = event.target && event.target.closest ? event.target.closest('[id^=\"cell_\"]') : null;\n"
            "    if (!host) return;\n"
            "    const id = String(host.id || '');\n"
            "    if (!id) return;\n"
            "    const dst = SVVIEW_INST_TARGETS[id] || '';\n"
            "    if (!dst) {\n"
            "      setNotice('No cached child schematic: ' + id);\n"
            "      return;\n"
            "    }\n"
            "    event.preventDefault();\n"
            "    event.stopPropagation();\n"
            "    window.location.assign(dst);\n"
            "  }, true);\n"
            "  buildZoomCtl();\n"
            "  applyZoom();\n"
            "  applyNetHighlight();\n"
            "})();\n"
            "</script>\n"
        )
        if "</body>" in src:
            return src.replace("</body>", script + "</body>", 1)
        return src + script

    def _signal_names_for_file(self, path: str) -> set[str]:
        ap = os.path.abspath(path)
        out: set[str] = set()
        for mod in self.design.modules.values():
            if os.path.abspath(mod.file) != ap:
                continue
            out.update(mod.signals.keys())
            out.update(mod.ports.keys())
        return out

    def _external_wave_enabled(self) -> bool:
        return bool(getattr(self.wave_bridge, "kind", "none") != "none")

    def _has_source_for_signal(self, sig: Optional[str]) -> bool:
        return bool(sig and sig in self.connectivity.signal_to_source)

    def _has_definition_for_signal(self, sig: Optional[str]) -> bool:
        if not sig:
            return False
        return self._find_signal_definition(sig) is not None

    def _defined_macros(self) -> Set[str]:
        out: Set[str] = set()
        args = list(self.loaded_slang_args or [])
        if not args:
            args = self._extra_slang_args_from_cli()
        i = 0
        while i < len(args):
            tok = args[i]
            if tok.startswith("+define+"):
                for d in tok.split("+")[2:]:
                    if not d:
                        continue
                    out.add(d.split("=", 1)[0])
            elif tok == "-D" and i + 1 < len(args):
                d = args[i + 1]
                if d:
                    out.add(d.split("=", 1)[0])
                i += 1
            elif tok.startswith("-D") and len(tok) > 2:
                d = tok[2:]
                out.add(d.split("=", 1)[0])
            i += 1
        return out

    def _inactive_preproc_lines(self, rows: List[str], defined: Set[str]) -> Set[int]:
        inactive: Set[int] = set()
        # frame: parent_active, branch_taken, active
        stack: List[tuple[bool, bool, bool]] = []
        current_active = True
        rx = re.compile(r"^\s*`(ifdef|ifndef|elsif|else|endif)\b(?:\s+([A-Za-z_][A-Za-z0-9_$]*))?")
        for i, raw in enumerate(rows, start=1):
            m = rx.match(raw)
            if not m:
                if not current_active:
                    inactive.add(i)
                continue
            kind = m.group(1)
            name = (m.group(2) or "").strip()
            line_active = current_active
            if kind in {"ifdef", "ifndef"}:
                cond = name in defined
                if kind == "ifndef":
                    cond = not cond
                parent = current_active
                active = parent and cond
                stack.append((parent, cond, active))
                current_active = active
                line_active = active
            elif kind == "elsif":
                if not stack:
                    line_active = current_active
                else:
                    parent, taken, _active = stack[-1]
                    cond = (name in defined)
                    active = parent and (not taken) and cond
                    stack[-1] = (parent, taken or cond, active)
                    current_active = active
                    line_active = active
            elif kind == "else":
                if not stack:
                    line_active = current_active
                else:
                    parent, taken, _active = stack[-1]
                    active = parent and (not taken)
                    stack[-1] = (parent, True, active)
                    current_active = active
                    line_active = active
            elif kind == "endif":
                if not stack:
                    line_active = current_active
                else:
                    # For `endif, color should match the active state *inside* the block.
                    line_active = current_active
                    parent, _taken, _active = stack.pop()
                    current_active = parent
            if not line_active:
                inactive.add(i)
        return inactive

    def _render_code_segment(self, code: str, signal_names: set[str], hit_token: str) -> str:
        parts: List[str] = []
        last = 0
        token_re = re.compile(r"\"(?:\\.|[^\"\\])*\"|\b\d[\w']*\b|\b[A-Za-z_][A-Za-z0-9_$]*\b")
        inst_decl_spans: dict[tuple[int, int], str] = {}
        inst_m = re.match(
            r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\([^;]*\)\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*\(",
            code,
        )
        if inst_m and inst_m.group(1) not in SV_KEYWORDS and inst_m.group(2) not in SV_KEYWORDS:
            inst_decl_spans[(inst_m.start(1), inst_m.end(1))] = "module"
            inst_decl_spans[(inst_m.start(2), inst_m.end(2))] = "instance"
        for m in token_re.finditer(code):
            if m.start() > last:
                parts.append(html.escape(code[last : m.start()]))
            tok = m.group(0)
            esc_tok = html.escape(tok)
            is_inst_port = False
            inst_decl_kind = inst_decl_spans.get((m.start(), m.end()), "")
            if tok and (tok[0].isalpha() or tok[0] == "_"):
                prev = code[: m.start()].rstrip()
                nxt = code[m.end() :]
                if prev.endswith(".") and re.match(r"^\s*\(", nxt):
                    is_inst_port = True
            if tok.startswith('"'):
                frag = f"<span style='color:#b35a00'>{esc_tok}</span>"
            elif tok[0].isdigit():
                frag = f"<span style='color:#7a3f00;font-weight:600'>{esc_tok}</span>"
            elif tok in SV_KEYWORDS:
                frag = f"<span style='color:#0550ae;font-weight:600'>{esc_tok}</span>"
            elif tok in SV_TYPE_KEYWORDS:
                frag = f"<span style='color:#0f766e;font-weight:600'>{esc_tok}</span>"
            elif inst_decl_kind == "module":
                frag = f"<span style='color:#b45309;font-weight:700'>{esc_tok}</span>"
            elif inst_decl_kind == "instance":
                frag = f"<span style='color:#b45309;font-weight:700'>{esc_tok}</span>"
            elif is_inst_port:
                frag = f"<span style='color:#b45309;font-weight:600'>{esc_tok}</span>"
            elif tok in signal_names:
                frag = f"<span style='color:#5b21b6'>{esc_tok}</span>"
            else:
                frag = f"<span style='color:#6d28d9'>{esc_tok}</span>"
            if hit_token and tok == hit_token:
                frag = f"<span style='background:#ffe89a;color:#102030'>{frag}</span>"
            parts.append(frag)
            last = m.end()
        if last < len(code):
            parts.append(html.escape(code[last:]))
        return "".join(parts)

    def _render_source_html(
        self,
        rows: List[str],
        focus_line: int = 1,
        hit_token: str = "",
        bookmark_lines: Optional[set[int]] = None,
        signal_names: Optional[set[str]] = None,
        inactive_lines: Optional[Set[int]] = None,
    ) -> str:
        parts: List[str] = []
        marks = bookmark_lines or set()
        sigs = signal_names or set()
        inactive = inactive_lines or set()

        for i, raw in enumerate(rows, start=1):
            body = raw.rstrip("\n")
            if i in inactive:
                line_html = f"<span style='color:#9aa3af'>{html.escape(body)}</span>"
                if i == focus_line:
                    line_html = f"<span style='background:#d9ecff'>{line_html}</span>"
                num = f"{i:5d}"
                mark = "★" if i in marks else " "
                num_color = "#c88600" if i in marks else "#6b7d94"
                parts.append(
                    f"<a name='L{i}'></a><span style='color:{num_color}'>{mark}{num}</span> <span style='color:#102030'>{line_html}</span>"
                )
                continue
            # comment highlighting on raw source.
            cidx = body.find("//")
            if cidx >= 0:
                lhs = body[:cidx]
                rhs = body[cidx:]
            else:
                lhs = body
                rhs = ""

            line_html = self._render_code_segment(lhs, sigs, hit_token)
            if rhs:
                line_html += f"<span style='color:#2f7d32'>{html.escape(rhs)}</span>"

            if i == focus_line:
                line_html = f"<span style='background:#d9ecff'>{line_html}</span>"
            num = f"{i:5d}"
            mark = "★" if i in marks else " "
            num_color = "#c88600" if i in marks else "#6b7d94"
            parts.append(
                f"<a name='L{i}'></a><span style='color:{num_color}'>{mark}{num}</span> <span style='color:#102030'>{line_html}</span>"
            )

        return "<pre style='font-family:DejaVu Sans Mono, monospace; font-size:12px; margin:0'>" + "\n".join(parts) + "</pre>"

    def show_file(self, path: str, line: int = 1, token: str = "", record_history: bool = True) -> None:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                rows = f.readlines()
        except Exception as e:
            self.set_status(f"failed to open source: {path} ({e})")
            return
        self._current_lines = rows
        self.current_file = path
        focus_line = max(1, min(len(rows), int(line)))
        self.current_line = focus_line
        if hasattr(self, "source_nav_edit"):
            self.source_nav_edit.setText(f"{path}:{focus_line}")
        if record_history:
            self._push_nav(path, focus_line)
            self._append_recent(path, focus_line)
        marks = self._bookmark_lines_for_current_file()
        sigs = self._signal_names_for_file(path)
        inactive = self._inactive_preproc_lines(rows, self._defined_macros())
        self.source_text.setHtml(
            self._render_source_html(
                rows,
                focus_line=focus_line,
                hit_token=token,
                bookmark_lines=marks,
                signal_names=sigs,
                inactive_lines=inactive,
            )
        )
        self._scroll_to_line(focus_line)
        self.set_status(f"{path}:{line}")

    def _scroll_to_line(self, line: int) -> None:
        line = max(1, int(line))
        anchor = f"L{line}"
        self.source_text.scrollToAnchor(anchor)

        # Fallback: move QTextCursor to the exact line-number token.
        # This is more stable across some Qt/font/rendering environments.
        needle = f"{line:5d} "
        cur = self.source_text.document().find(needle)
        if cur and not cur.isNull():
            self.source_text.setTextCursor(cur)
            self.source_text.ensureCursorVisible()

    def _line_from_source_cursor(self) -> int:
        try:
            block = self.source_text.textCursor().block().text()
            m = re.match(r"\s*(\d+)\s", block)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return int(self.current_line or 1)

    def open_external_editor(self) -> None:
        if not self.current_file:
            self.set_status("No source file selected")
            return
        line = self._line_from_source_cursor()
        cmd_tpl = str(self._effective_editor_cmd or "")
        try:
            argv = build_editor_argv(cmd_tpl, self.current_file, line)
        except ValueError as e:
            self.set_status(f"Invalid editor command template: {e}")
            QMessageBox.critical(self, "RTLens", f"invalid editor command template: {e}")
            return
        try:
            subprocess.Popen(argv, shell=False)
            self.set_status(f"Opened editor: {self.current_file}:{line}")
        except Exception as e:
            QMessageBox.critical(self, "RTLens", f"failed to start editor: {e}")

    def _cleanup_wave_name(self, name: str) -> str:
        return _cleanup_wave_name_text(name)

    def _extract_wave_name_candidates(self, text: str) -> List[str]:
        return _extract_wave_name_candidates_text(text)

    def _resolve_wave_name_to_design(self, wave_name: str) -> Optional[str]:
        cleaned = self._cleanup_wave_name(wave_name)
        if not cleaned:
            return None
        if cleaned in self.connectivity.signal_to_source:
            return cleaned
        if "." not in cleaned:
            r = self.resolve_signal_query(cleaned)
            if r:
                return r
        tail = "." + cleaned.split(".")[-1]
        cands = [s for s in self.connectivity.signal_to_source.keys() if s.endswith(tail)]
        if not cands:
            return None
        return sorted(cands, key=len)[0]

    def import_wave_selection(self) -> None:
        clips: List[tuple[str, str]] = []
        cb = QApplication.clipboard()
        txt = cb.text().strip()
        if txt:
            clips.append(("CLIPBOARD", txt))
        if getattr(self.args, "wave_import_primary", False):
            try:
                txt2 = cb.text(mode=cb.Mode.Selection).strip()
                if txt2:
                    clips.append(("PRIMARY", txt2))
            except Exception:
                pass
        if not clips:
            self.set_status("No clipboard selection found (CLIPBOARD/PRIMARY)")
            return

        picked: Optional[str] = None
        picked_src = ""
        picked_raw = ""
        for src, clip in clips:
            for cand in self._extract_wave_name_candidates(clip):
                picked = self._resolve_wave_name_to_design(cand)
                if picked:
                    picked_src = src
                    picked_raw = cand
                    break
            if picked:
                break
        if not picked:
            raw = clips[0][1].replace("\n", " ")
            if len(raw) > 80:
                raw = raw[:80] + "..."
            self.set_status(f"Could not resolve wave signal from {clips[0][0]}: {raw}")
            return
        self.signal_entry.setText(picked)
        self.search_signal()
        loc = self.connectivity.signal_to_source.get(picked)
        if loc:
            token = picked.split(".")[-1]
            self.show_file(loc.file, loc.line, token=token)
        self.set_status(f"Imported wave signal ({picked_src}): {picked_raw} -> {picked}")

    def open_wave(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open wave", "", "Wave (*.vcd *.fst);;All (*)")
        if not path:
            return
        self.loaded_wave_path = os.path.abspath(path)
        self.load_wave_file(self.loaded_wave_path)

    def load_wave_file(self, path: str) -> None:
        try:
            # Qt pane only needs signal names for cross-probe; avoid full VCD expansion.
            self.wave = load_wave(path, parse_changes=False)
            self.wave_signals = sorted(self.wave.signals.keys())
            self.refresh_wave_list()
            self.wave_file_label.setText(path)
            self.set_status(f"Wave loaded: {path} (signals={len(self.wave_signals)})")
            self.right_tabs.setCurrentIndex(5)
        except Exception as e:
            self.wave = None
            self.wave_signals = []
            self.refresh_wave_list()
            self.wave_file_label.setText("(no wave loaded)")
            QMessageBox.critical(self, "RTLens", str(e))

    def refresh_wave_list(self) -> None:
        filt = self.wave_filter_entry.text().strip().lower()
        self.wave_list.clear()
        for s in self.wave_signals:
            if filt and filt not in s.lower():
                continue
            self.wave_list.addItem(s)

    def on_wave_select(self) -> None:
        item = self.wave_list.currentItem()
        if not item:
            return
        sig = self._resolve_wave_name_to_design(item.text())
        if not sig:
            return
        self.signal_entry.setText(sig)

    def on_wave_jump(self, item) -> None:
        raw = item.text()
        sig = self._resolve_wave_name_to_design(raw)
        if not sig:
            self.set_status(f"wave signal unresolved: {raw}")
            return
        loc = self.connectivity.signal_to_source.get(sig)
        if not loc:
            self.set_status(f"source not found: {sig}")
            return
        token = sig.split(".")[-1]
        self.show_file(loc.file, loc.line, token=token)
        self.right_tabs.setCurrentIndex(0)

    def on_wave_context_menu(self, pos) -> None:
        item = self.wave_list.itemAt(pos)
        self._wave_ctx_signal = None
        if item:
            self._wave_ctx_signal = self._resolve_wave_name_to_design(item.text())
        menu = QMenu(self)
        a_set = menu.addAction("Use as current signal")
        a_jump = menu.addAction("Jump to source")
        a_def = menu.addAction("Show definition")
        a_add = menu.addAction("Add to external wave")
        sig_enabled = self._wave_ctx_signal is not None
        a_set.setEnabled(sig_enabled)
        a_jump.setEnabled(self._has_source_for_signal(self._wave_ctx_signal))
        a_def.setEnabled(self._has_definition_for_signal(self._wave_ctx_signal))
        a_add.setEnabled(sig_enabled and self._external_wave_enabled())
        chosen = menu.exec(self.wave_list.mapToGlobal(pos))
        if chosen == a_set:
            self.ctx_wave_set_signal()
        elif chosen == a_jump:
            self.ctx_wave_jump_source()
        elif chosen == a_def:
            self.ctx_wave_show_definition()
        elif chosen == a_add:
            self.ctx_wave_add_to_wave()

    def ctx_wave_set_signal(self) -> None:
        if not self._wave_ctx_signal:
            return
        self.signal_entry.setText(self._wave_ctx_signal)
        self.set_status(f"Current signal: {self._wave_ctx_signal}")

    def ctx_wave_jump_source(self) -> None:
        if not self._wave_ctx_signal:
            return
        loc = self.connectivity.signal_to_source.get(self._wave_ctx_signal)
        if not loc:
            self.set_status(f"source not found: {self._wave_ctx_signal}")
            return
        token = self._wave_ctx_signal.split(".")[-1]
        self.show_file(loc.file, loc.line, token=token)
        self.right_tabs.setCurrentIndex(0)

    def ctx_wave_show_definition(self) -> None:
        if not self._wave_ctx_signal:
            return
        self.signal_entry.setText(self._wave_ctx_signal)
        self.show_signal_definition()

    def ctx_wave_add_to_wave(self) -> None:
        if not self._wave_ctx_signal:
            return
        try:
            ok = self.wave_bridge.add_signal(self._wave_ctx_signal)
        except WaveBridgeError as e:
            QMessageBox.critical(self, "RTLens", str(e))
            return
        if ok:
            self.set_status(f"Added to external wave: {self._wave_ctx_signal}")
        else:
            self.set_status("External wave viewer is disabled")

    def open_external_wave(self) -> None:
        path = self.loaded_wave_path or getattr(self.args, "wave", "")
        if not path:
            self.set_status("No wave file selected")
            return
        try:
            if self.wave_bridge.open(path):
                mode = getattr(self.wave_bridge, "kind", "external")
                self.set_status(f"Opened {mode}: {path}")
            else:
                self.set_status("External wave viewer is disabled")
        except WaveBridgeError as e:
            QMessageBox.critical(self, "RTLens", str(e))

    def reload_wave(self) -> bool:
        path = self.loaded_wave_path or getattr(self.args, "wave", "")
        if not path or not os.path.isfile(path):
            self.wave_file_label.setText("(no wave loaded)")
            self.set_status("Wave reload skipped (no wave target)")
            return False
        self.load_wave_file(path)
        try:
            ok = self.wave_bridge.reload()
            if not ok:
                ok = self.wave_bridge.open(path)
        except WaveBridgeError as e:
            QMessageBox.critical(self, "RTLens", str(e))
            return False
        if ok:
            self.set_status("Wave reloaded")
            return True
        self.set_status("External wave viewer is disabled")
        return False

    def reload_all(self) -> None:
        rtl_ok = self.reload_rtl()
        wave_ok = self.reload_wave()
        if rtl_ok and wave_ok:
            self.set_status("Reloaded RTL + Wave")
        elif rtl_ok:
            self.set_status("Reloaded RTL")
        elif wave_ok:
            self.set_status("Reloaded Wave")
        else:
            self.set_status("Reload skipped (no targets)")

    def _poll_wave_bridge_events(self) -> None:
        try:
            events = self.wave_bridge.poll_events()
        except Exception:
            events = []
        for ev, payload in events:
            self._handle_wave_bridge_event(ev, payload)

    def _handle_wave_bridge_event(self, ev: str, payload: str) -> None:
        if ev == "waveforms_loaded":
            if payload:
                self.set_status(f"External wave loaded: {payload}")
            return
        if ev in {"goto_declaration", "add_drivers", "add_loads"}:
            sig = self._resolve_wave_name_to_design(payload)
            if not sig:
                self.set_status(f"External event {ev}: unresolved {payload}")
                return
            self.signal_entry.setText(sig)
            if ev == "goto_declaration":
                self.show_signal_definition()
            else:
                self.search_signal()

    def _include_target_from_context(self, block_text: str, src_col: int) -> Optional[str]:
        body = block_text[6:] if len(block_text) >= 6 else ""
        m = re.match(r"\s*`include\s+(?:\"([^\"]+)\"|<([^>]+)>)", body)
        if not m:
            return None
        if m.group(1) is not None:
            s, e = m.start(1), m.end(1)
            target = m.group(1)
        else:
            s, e = m.start(2), m.end(2)
            target = m.group(2)
        if s <= src_col <= e:
            return target
        return None

    def _include_search_dirs(self) -> List[str]:
        dirs: List[str] = []
        seen = set()
        if self.current_file:
            d0 = os.path.abspath(os.path.dirname(self.current_file))
            dirs.append(d0)
            seen.add(d0)
        args = list(self.loaded_slang_args or [])
        i = 0
        while i < len(args):
            tok = args[i]
            if tok.startswith("+incdir+"):
                for p in tok.split("+")[2:]:
                    if not p:
                        continue
                    ap = os.path.abspath(os.path.expandvars(p))
                    if ap not in seen:
                        seen.add(ap)
                        dirs.append(ap)
            elif tok == "-I" and i + 1 < len(args):
                ap = os.path.abspath(os.path.expandvars(args[i + 1]))
                if ap not in seen:
                    seen.add(ap)
                    dirs.append(ap)
                i += 1
            elif tok.startswith("-I") and len(tok) > 2:
                ap = os.path.abspath(os.path.expandvars(tok[2:]))
                if ap not in seen:
                    seen.add(ap)
                    dirs.append(ap)
            i += 1
        return dirs

    def _resolve_include_path(self, target: str) -> Optional[str]:
        t = os.path.expandvars((target or "").strip())
        if not t:
            return None
        if os.path.isabs(t) and os.path.isfile(t):
            return t
        for d in self._include_search_dirs():
            cand = os.path.abspath(os.path.join(d, t))
            if os.path.isfile(cand):
                return cand
        if os.path.isfile(t):
            return os.path.abspath(t)
        return None

    def on_source_context_menu(self, pos) -> None:
        cursor = self.source_text.cursorForPosition(pos)
        cursor.select(cursor.SelectionType.WordUnderCursor)
        word = cursor.selectedText().strip()
        self._source_ctx_word = word
        try:
            block = cursor.block().text()
            m = re.match(r"\s*(\d+)\s", block)
            self._source_ctx_line = int(m.group(1)) if m else self.current_line
        except Exception:
            self._source_ctx_line = self.current_line
        self._source_ctx_signal = self._resolve_signal_query_from_source_token(word) if word else None
        try:
            block_text = cursor.block().text()
            src_col = max(0, int(cursor.positionInBlock()) - 6)
            inc_target = self._include_target_from_context(block_text, src_col)
        except Exception:
            inc_target = None
        self._source_ctx_include_path = self._resolve_include_path(inc_target or "") if inc_target else None
        self._source_ctx_instance_path = self._resolve_module_instance_fullpath_from_site(
            self.current_file or "", self._source_ctx_line, self._source_ctx_word
        )
        self._source_ctx_selected_signals = self._resolve_signals_from_source_selection()
        callable_key = self._resolve_callable_key_from_site(self.current_file or "", self._source_ctx_line, word)
        callable_def_key = self._resolve_callable_key_for_definition_site(
            self.current_file or "", self._source_ctx_line, word
        )
        callable_kind = self.design.callable_kinds.get(callable_key, "") if callable_key else ""
        callable_is_fn_task = callable_kind in {"function", "task"}
        callable_def_is_fn_task = False
        if callable_def_key and self.current_file:
            site_file = self.current_file or ""
            for tok in token_variants(word):
                if callable_def_key in self.design.callable_def_sites.get((site_file, int(self._source_ctx_line), tok), []):
                    callable_def_is_fn_task = self.design.callable_kinds.get(callable_def_key, "") in {"function", "task"}
                    break
        menu = QMenu(self)
        a_set = menu.addAction("Set as current signal")
        a_copy = menu.addAction("Copy signal fullpath")
        a_copy_inst = menu.addAction("Copy instance fullpath")
        a_find = menu.addAction("Find drivers/loads")
        a_def = menu.addAction("Show definition")
        a_inc = menu.addAction("Show include file")
        a_refs = menu.addAction("Find references")
        a_add = menu.addAction("Add to external wave")
        sig_enabled = self._source_ctx_signal is not None
        callable_site_is_fn_task = callable_is_fn_task
        if callable_def_is_fn_task:
            for a in (a_set, a_copy, a_copy_inst, a_find, a_inc, a_add):
                a.setEnabled(False)
            a_def.setEnabled(True)
            a_refs.setEnabled(True)
        else:
            a_set.setEnabled(sig_enabled and not callable_site_is_fn_task)
            a_copy.setEnabled(sig_enabled)
            a_find.setEnabled(sig_enabled and not callable_is_fn_task)
            a_add.setEnabled(
                (not callable_site_is_fn_task)
                and (sig_enabled or bool(self._source_ctx_selected_signals))
                and self._external_wave_enabled()
            )
            a_copy_inst.setEnabled(self._source_ctx_instance_path is not None)
            a_def.setEnabled(self._has_definition_for_signal(self._source_ctx_signal) or callable_key is not None)
            a_inc.setEnabled(self._source_ctx_include_path is not None)
            a_refs.setEnabled(callable_key is not None)
        chosen = menu.exec(self.source_text.mapToGlobal(pos))
        if chosen == a_set:
            self.ctx_source_set_signal()
        elif chosen == a_copy:
            self.ctx_source_copy_fullpath()
        elif chosen == a_copy_inst:
            self.ctx_source_copy_instance_fullpath()
        elif chosen == a_find:
            self.ctx_source_search_signal()
        elif chosen == a_def:
            self.ctx_source_show_definition()
        elif chosen == a_inc:
            self.ctx_source_show_include()
        elif chosen == a_refs:
            self.ctx_source_find_references()
        elif chosen == a_add:
            self.ctx_source_add_to_wave()

    def _resolve_signal_query_from_source_token(self, token: str) -> Optional[str]:
        if not token:
            return None
        if not self.current_file:
            return self.resolve_signal_query(token)
        file_abs = os.path.abspath(self.current_file)
        tail = "." + token
        cands = [
            sig
            for sig, loc in self.connectivity.signal_to_source.items()
            if sig.endswith(tail) and os.path.abspath(loc.file) == file_abs
        ]
        if not cands:
            return self.resolve_signal_query(token)

        if len(cands) == 1:
            return cands[0]

        hier = (self.current_hier_path or "").strip()
        if hier:
            sub = [s for s in cands if s.startswith(hier + ".")]
            if len(sub) == 1:
                return sub[0]
            if sub:
                cands = sub

            hparts = hier.split(".")
            best_sig: Optional[str] = None
            best_score = (-1, -1)
            for sig in cands:
                inst = sig.rsplit(".", 1)[0]
                iparts = inst.split(".")
                common = 0
                for a, b in zip(hparts, iparts):
                    if a != b:
                        break
                    common += 1
                subtree = 1 if inst.startswith(hier + ".") else 0
                score = (subtree, common)
                if score > best_score:
                    best_score = score
                    best_sig = sig
            if best_sig and best_score[1] >= 2:
                return best_sig
            if best_sig and len(cands) == 1:
                return best_sig
            return None

        return sorted(cands, key=len)[0]

    def _resolve_signals_from_source_selection(self) -> List[str]:
        try:
            cur = self.source_text.textCursor()
            if not cur.hasSelection():
                return []
            txt = cur.selectedText().replace("\u2029", "\n")
        except Exception:
            return []
        if not txt:
            return []
        out: List[str] = []
        seen = set()
        for raw_line in txt.splitlines():
            line = re.sub(r"^\s*\d+\s", "", raw_line)
            for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_$]*", line):
                s = self.resolve_signal_query(m.group(0))
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        return out

    def ctx_source_set_signal(self) -> None:
        if not self._source_ctx_signal:
            return
        self.signal_entry.setText(self._source_ctx_signal)
        self._follow_hierarchy_for_signal(self._source_ctx_signal)
        self.set_status(f"Current signal: {self._source_ctx_signal}")

    def ctx_source_copy_fullpath(self) -> None:
        if not self._source_ctx_signal:
            return
        try:
            QApplication.clipboard().setText(self._source_ctx_signal)
            self.set_status(f"Copied signal fullpath: {self._source_ctx_signal}")
        except Exception as e:
            self.set_status(f"clipboard error: {e}")

    def ctx_source_copy_instance_fullpath(self) -> None:
        if not self._source_ctx_instance_path:
            return
        try:
            QApplication.clipboard().setText(self._source_ctx_instance_path)
            self.set_status(f"Copied instance fullpath: {self._source_ctx_instance_path}")
        except Exception as e:
            self.set_status(f"clipboard error: {e}")

    def ctx_source_search_signal(self) -> None:
        if not self._source_ctx_signal:
            return
        self.signal_entry.setText(self._source_ctx_signal)
        self.search_signal()

    def ctx_source_show_definition(self) -> None:
        key = self._resolve_callable_key_from_site(self.current_file or "", self._source_ctx_line, self._source_ctx_word)
        if key and self.design.callable_kinds.get(key, "") in {"function", "task"}:
            self._open_callable_definition(key)
            return
        if self._source_ctx_signal:
            self.signal_entry.setText(self._source_ctx_signal)
            self.show_signal_definition()
            return
        if not key:
            return
        self._open_callable_definition(key)

    def ctx_source_show_include(self) -> None:
        if not self._source_ctx_include_path:
            self.set_status("include file not found")
            return
        self.show_file(self._source_ctx_include_path, 1)

    def ctx_source_find_references(self) -> None:
        key = self._resolve_callable_key_for_definition_site(
            self.current_file or "", self._source_ctx_line, self._source_ctx_word
        )
        if not key:
            key = self._resolve_callable_key_from_site(self.current_file or "", self._source_ctx_line, self._source_ctx_word)
        if not key:
            self.set_status("references not found for selected token")
            return
        self._trace_callable_references(key)

    def ctx_source_add_to_wave(self) -> None:
        targets = list(self._source_ctx_selected_signals)
        if not targets and self._source_ctx_signal:
            targets = [self._source_ctx_signal]
        if not targets:
            return
        added = 0
        for sig in targets:
            try:
                ok = self.wave_bridge.add_signal(sig)
            except WaveBridgeError as e:
                QMessageBox.critical(self, "RTLens", str(e))
                return
            if ok:
                added += 1
        if added == 0:
            self.set_status("External wave viewer is disabled")
        elif added == 1:
            self.set_status(f"Added to external wave: {targets[0]}")
        else:
            self.set_status(f"Added to external wave: {added} signals")

    def resolve_signal_query(self, q: str) -> Optional[str]:
        if q in self.connectivity.signal_to_source:
            return q
        if "." not in q and self.current_hier_path:
            scoped = f"{self.current_hier_path}.{q}"
            if scoped in self.connectivity.signal_to_source:
                return scoped
        tail = "." + q.split(".")[-1]
        cands = [s for s in self.connectivity.signal_to_source.keys() if s.endswith(tail)]
        if not cands:
            return None
        if self.current_hier_path:
            scoped = [s for s in cands if s.startswith(self.current_hier_path + ".")]
            if len(scoped) == 1:
                return scoped[0]
            if scoped:
                cands = scoped
        return sorted(cands, key=len)[0]

    def _find_signal_definition(self, abs_sig: str) -> Optional[tuple[str, int, str]]:
        token = abs_sig.split(".")[-1]
        loc = self.connectivity.signal_to_source.get(abs_sig)
        if not loc:
            return None
        path = loc.file
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            return None

        idtok = re.escape(token)
        decls = [
            re.compile(rf"\b(input|output|inout)\b([^;]*?)\b{idtok}\b"),
            re.compile(rf"\b(wire|logic|reg|tri|uwire|bit|var)\b([^;]*?)\b{idtok}\b"),
            re.compile(rf"\b(parameter|localparam)\b([^;]*?)\b{idtok}\b"),
            # Function/task local declarations (best effort)
            re.compile(
                rf"\b(?:automatic|static|const|var\s+)?"
                rf"(byte|shortint|int|longint|integer|time|realtime|real|shortreal|string|chandle|event|bit|logic|reg)\b"
                rf"([^;]*?)\b{idtok}\b"
            ),
        ]
        for i, raw in enumerate(lines, start=1):
            body = raw.split("//", 1)[0]
            for d in decls:
                m = d.search(body)
                if m:
                    head = m.group(1)
                    tail = m.group(2).strip()
                    typ = (head + " " + tail).strip()
                    return path, i, typ
        return path, loc.line, "implicit-wire-or-generated (best effort)"

    def show_signal_definition(self) -> None:
        q = self.signal_entry.text().strip()
        if not q:
            self.set_status("Signal path is empty")
            return
        resolved = self.resolve_signal_query(q)
        if not resolved:
            self.set_status(f"signal not found: {q}")
            return
        info = self._find_signal_definition(resolved)
        if not info:
            self.set_status(f"definition not found: {resolved}")
            return
        path, line, typ = info
        token = resolved.split(".")[-1]
        self.show_file(path, line, token=token)
        self.set_status(f"Definition: {resolved} => {typ} @ {path}:{line}")
        self.right_tabs.setCurrentIndex(0)
        self._append_trace(
            f"def {resolved} @ {path}:{line}",
            {"type": "goto", "file": path, "line": line, "token": token},
        )

    def search_signal(self) -> None:
        q = self.signal_entry.text().strip()
        if not q:
            return
        resolved = self.resolve_signal_query(q)
        if not resolved:
            self.set_status(f"signal not found: {q}")
            return
        self.signal_entry.setText(resolved)
        self._follow_hierarchy_for_signal(resolved)
        include_control = bool(self.include_control_check.isChecked())
        include_clock = bool(self.include_clock_check.isChecked())
        include_ports = bool(self.include_ports_check.isChecked())
        drivers, loads = query_signal(
            self.connectivity,
            resolved,
            recursive=False,
            include_control=include_control,
            include_clock=include_clock,
            include_ports=include_ports,
        )
        port_hint = ""
        if (not include_ports) and (not drivers) and (not loads):
            port_drivers, port_loads = query_signal(
                self.connectivity,
                resolved,
                recursive=False,
                include_control=include_control,
                include_clock=include_clock,
                include_ports=True,
            )
            if port_drivers or port_loads:
                port_hint = " | hint: enable 'Include port sites'"
        self.driver_list.clear()
        self.load_list.clear()
        for sig, loc in drivers:
            self.driver_list.addItem(f"{sig} -> {loc.file}:{loc.line}")
        for sig, loc in loads:
            self.load_list.addItem(f"{sig} -> {loc.file}:{loc.line}")
        ctrl = "with-control" if include_control else "data-only"
        clk = "with-clock" if include_clock else "no-clock"
        ports = "with-ports" if include_ports else "no-ports"
        self.set_status(
            f"Drivers: {len(drivers)}, Loads: {len(loads)} (direct, {ctrl}, {clk}, {ports}){port_hint}"
        )
        self._append_trace(
            f"trace {resolved} ({ctrl}, {clk}, {ports})",
            {
                "type": "signal-trace",
                "signal": resolved,
                "include_control": include_control,
                "include_clock": include_clock,
                "include_ports": include_ports,
            },
        )

    def search_signal_if_any(self) -> None:
        if self.signal_entry.text().strip():
            self.search_signal()

    def _jump_item(self, text: str) -> None:
        parsed = self._parse_jump_item(text)
        if not parsed:
            return
        file, line, token, sig = parsed
        self._jump_to_source_or_callable(file=file, line=line, token=token, focus_signal=sig)

    def _parse_jump_item(self, text: str) -> Optional[tuple[str, int, str, str]]:
        return _parse_jump_item_text(text)

    def _preview_selected_item(self, is_driver: bool) -> None:
        lw = self.driver_list if is_driver else self.load_list
        item = lw.currentItem()
        if not item:
            return
        parsed = self._parse_jump_item(item.text())
        if not parsed:
            return
        file, line, token, sig = parsed
        self._jump_to_source_or_callable(file=file, line=line, token=token, focus_signal=sig)
        kind = "Driver" if is_driver else "Load"
        self.set_status(f"{kind}: {sig} @ {file}:{line}")

    def on_driver_jump(self, item) -> None:
        self._jump_item(item.text())

    def on_load_jump(self, item) -> None:
        self._jump_item(item.text())

    def on_driver_select(self) -> None:
        self._preview_selected_item(is_driver=True)

    def on_load_select(self) -> None:
        self._preview_selected_item(is_driver=False)

    def on_trace_context_menu(self, pos, is_driver: bool) -> None:
        lw = self.driver_list if is_driver else self.load_list
        item = lw.itemAt(pos)
        self._trace_ctx_signal = None
        if item:
            parsed = self._parse_jump_item(item.text())
            if parsed:
                _file, _line, _token, sig = parsed
                self._trace_ctx_signal = sig
        menu = QMenu(self)
        a_set = menu.addAction("Use as current signal")
        a_jump = menu.addAction("Jump to source")
        a_def = menu.addAction("Show definition")
        a_add = menu.addAction("Add to external wave")
        sig_enabled = self._trace_ctx_signal is not None
        a_set.setEnabled(sig_enabled)
        a_jump.setEnabled(self._has_source_for_signal(self._trace_ctx_signal))
        a_def.setEnabled(self._has_definition_for_signal(self._trace_ctx_signal))
        a_add.setEnabled(sig_enabled and self._external_wave_enabled())
        chosen = menu.exec(lw.mapToGlobal(pos))
        if chosen == a_set:
            self.ctx_trace_set_signal()
        elif chosen == a_jump:
            self.ctx_trace_jump_source()
        elif chosen == a_def:
            self.ctx_trace_show_definition()
        elif chosen == a_add:
            self.ctx_trace_add_to_wave()

    def ctx_trace_set_signal(self) -> None:
        if not self._trace_ctx_signal:
            return
        self.signal_entry.setText(self._trace_ctx_signal)
        self.set_status(f"Current signal: {self._trace_ctx_signal}")

    def ctx_trace_jump_source(self) -> None:
        if not self._trace_ctx_signal:
            return
        loc = self.connectivity.signal_to_source.get(self._trace_ctx_signal)
        if not loc:
            self.set_status(f"source not found: {self._trace_ctx_signal}")
            return
        token = self._trace_ctx_signal.split(".")[-1]
        self._jump_to_source_or_callable(
            file=loc.file,
            line=loc.line,
            token=token,
            focus_signal=self._trace_ctx_signal,
        )
        self.right_tabs.setCurrentIndex(0)

    def ctx_trace_show_definition(self) -> None:
        if not self._trace_ctx_signal:
            return
        self.signal_entry.setText(self._trace_ctx_signal)
        self.show_signal_definition()

    def ctx_trace_add_to_wave(self) -> None:
        if not self._trace_ctx_signal:
            return
        try:
            ok = self.wave_bridge.add_signal(self._trace_ctx_signal)
        except WaveBridgeError as e:
            QMessageBox.critical(self, "RTLens", str(e))
            return
        if ok:
            self.set_status(f"Added to external wave: {self._trace_ctx_signal}")
        else:
            self.set_status("External wave viewer is disabled")

    def _resolve_callable_key_from_site(self, file: str, line: int, word: str) -> Optional[str]:
        return resolve_callable_key_from_site(
            self.design,
            file=file,
            line=int(line),
            word=word,
            current_hier_path=(self.current_hier_path or ""),
        )

    def _resolve_module_instance_fullpath_from_site(self, file: str, line: int, word: str) -> Optional[str]:
        if not file or not word:
            return None
        keys = []
        for tok in token_variants(word):
            keys.extend(self.design.callable_ref_sites.get((file, int(line), tok), []))
        keys = sorted(set(keys))
        if not keys:
            return None
        target_modules = {
            self.design.callable_names.get(k, "").strip()
            for k in keys
            if self.design.callable_kinds.get(k, "") == "module"
        }
        if not target_modules:
            return None
        hit: List[str] = []
        for p, node in self.design.hier.items():
            if node.inst_name == word and node.module_name in target_modules:
                hit.append(p)
        if not hit:
            return None
        if len(hit) == 1:
            return hit[0]
        hier = (self.current_hier_path or "").strip()
        if hier:
            scoped = [h for h in hit if h.startswith(hier + ".") or hier.startswith(h + ".") or h == hier]
            if len(scoped) == 1:
                return scoped[0]
            if scoped:
                hit = scoped
        return sorted(hit, key=len)[0]

    def _resolve_callable_key_for_definition_site(self, file: str, line: int, word: str) -> Optional[str]:
        return resolve_callable_key_for_definition_site(
            self.design,
            file=file,
            line=int(line),
            word=word,
            current_hier_path=(self.current_hier_path or ""),
        )

    def _resolve_callable_key_any_site(self, file: str, line: int) -> Optional[str]:
        return resolve_callable_key_any_site(
            self.design,
            file=file,
            line=int(line),
            current_hier_path=(self.current_hier_path or ""),
            prefer_kinds=("function", "task"),
        )

    def _jump_to_source_or_callable(self, file: str, line: int, token: str, focus_signal: str) -> None:
        key = self._resolve_callable_key_any_site(file=file, line=line)
        if key and self.design.callable_kinds.get(key, "") in {"function", "task"}:
            self._open_callable_definition(key)
            return
        self.show_file(file, line, token=token)

    def _open_callable_definition(self, key: str) -> None:
        loc = self.design.callable_defs.get(key)
        if not loc:
            self.set_status(f"definition not found: {key}")
            return
        token = self.design.callable_names.get(key, key.split(":", 1)[-1].split(".")[-1])
        self.show_file(loc.file, loc.line, token=token)
        self.set_status(f"Definition: {key} @ {loc.file}:{loc.line}")
        self.right_tabs.setCurrentIndex(0)
        self._append_trace(
            f"def {key} @ {loc.file}:{loc.line}",
            {"type": "goto", "file": loc.file, "line": loc.line, "token": token},
        )

    def _trace_callable_references(self, key: str) -> None:
        refs = self.design.callable_refs.get(key, [])
        name = self.design.callable_names.get(key, key.split(":", 1)[-1])
        if not refs:
            self.set_status(f"No references: {key}")
            self._append_trace(f"refs {name}: 0", {"type": "callable-refs", "key": key})
            self.right_tabs.setCurrentIndex(3)
            return
        self._append_trace(f"refs {name}: {len(refs)}", {"type": "callable-refs", "key": key})
        token = name.split(".")[-1]
        for loc in refs:
            self._append_trace(
                f"  {loc.file}:{loc.line}",
                {"type": "goto", "file": loc.file, "line": loc.line, "token": token},
            )
        self.set_status(f"References: {name} -> {len(refs)}")
        self.right_tabs.setCurrentIndex(3)

    def _append_trace(self, label: str, action: dict) -> None:
        self.trace_actions.append(action)
        self.trace_list.insertItem(self.trace_list.count(), label)

    def clear_trace_log(self) -> None:
        self.trace_actions.clear()
        self.trace_list.clear()

    def on_trace_jump(self, item) -> None:
        row = self.trace_list.row(item)
        if row < 0 or row >= len(self.trace_actions):
            return
        action = self.trace_actions[row]
        self._run_trace_action(action)

    def _run_trace_action(self, action: dict) -> None:
        t = action.get("type", "")
        if t == "goto":
            file = action.get("file", "")
            line = int(action.get("line", 1))
            token = action.get("token", "")
            if file:
                self.show_file(file, line, token=token)
                self.right_tabs.setCurrentIndex(0)
            return
        if t == "signal-trace":
            sig = action.get("signal", "")
            if sig:
                self.signal_entry.setText(sig)
                self.include_control_check.setChecked(bool(action.get("include_control", False)))
                self.include_clock_check.setChecked(bool(action.get("include_clock", True)))
                self.include_ports_check.setChecked(bool(action.get("include_ports", False)))
                self.search_signal()
            return
        if t == "callable-refs":
            key = action.get("key", "")
            if key:
                self._trace_callable_references(key)
            return

    def on_hier_select(self) -> None:
        if self._suppress_hier_select:
            return
        sel = self.hier_tree.selectedItems()
        if not sel:
            return
        item = sel[0]
        path = self.hier_to_path.get(id(item))
        if not path:
            return
        self.current_hier_path = path
        node = self.design.hier[path]
        mod = self.design.modules.get(node.module_name)
        self.schematic_dirty = True
        self.rtl_structure_dirty = True
        if self.right_tabs.currentIndex() == self._schematic_tab_index():
            self.refresh_schematic_if_dirty()
        if self.right_tabs.currentIndex() == self._rtl_structure_tab_index():
            self.refresh_rtl_structure_if_dirty()
        if not mod:
            return
        self.show_file(mod.file, mod.start_line)

    def _hier_path_from_signal(self, abs_sig: str) -> Optional[str]:
        if not abs_sig:
            return None
        if "." not in abs_sig:
            return abs_sig if abs_sig in self.design.hier else None
        inst = abs_sig.rsplit(".", 1)[0]
        while inst:
            if inst in self.design.hier:
                return inst
            if "." not in inst:
                break
            inst = inst.rsplit(".", 1)[0]
        return None

    def _follow_hierarchy_for_signal(self, abs_sig: str) -> None:
        path = self._hier_path_from_signal(abs_sig)
        if not path:
            return
        self.current_hier_path = path
        item = self.hier_path_to_item.get(path)
        if not item:
            return
        self._suppress_hier_select = True
        try:
            self.hier_tree.setCurrentItem(item)
            self.hier_tree.scrollToItem(item)
        finally:
            self._suppress_hier_select = False

    def on_hier_context_menu(self, pos) -> None:
        item = self.hier_tree.itemAt(pos)
        if item is not None:
            self.hier_tree.setCurrentItem(item)
        sel = self.hier_tree.selectedItems()
        self._hier_ctx_path = None
        if sel:
            self._hier_ctx_path = self.hier_to_path.get(id(sel[0]))
        menu = QMenu(self)
        a_copy = menu.addAction("Copy instance fullpath")
        a_copy.setEnabled(self._hier_ctx_path is not None)
        chosen = menu.exec(self.hier_tree.viewport().mapToGlobal(pos))
        if chosen == a_copy:
            self.ctx_hier_copy_fullpath()

    def ctx_hier_copy_fullpath(self) -> None:
        if not self._hier_ctx_path:
            return
        try:
            QApplication.clipboard().setText(self._hier_ctx_path)
            self.set_status(f"Copied instance fullpath: {self._hier_ctx_path}")
        except Exception as e:
            self.set_status(f"clipboard error: {e}")

    def open_filelist(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open filelist")
        if not path:
            return
        try:
            files, slang_args = self._read_multiple_filelists([path])
            self.loaded_filelist_path = path
            self.loaded_filelist_paths = [path]
            self.loaded_dir_path = ""
            self._parse_files(files, slang_args)
        except Exception as e:
            QMessageBox.critical(self, "RTLens", str(e))

    def open_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open RTL directory")
        if not path:
            return
        files = discover_sv_files(path)
        self.loaded_dir_path = path
        self.loaded_filelist_path = ""
        self.loaded_filelist_paths = []
        self._parse_files(files, [])

    def reload_rtl(self, clear_trace: bool = True) -> bool:
        if self.loaded_filelist_paths:
            valid = [p for p in self.loaded_filelist_paths if os.path.isfile(p)]
            if valid:
                files, slang_args = self._read_multiple_filelists(valid)
                self._parse_files(files, slang_args)
                self.set_status("RTL reloaded")
                if clear_trace:
                    self.clear_trace_log()
                return True
        if self.loaded_filelist_path and os.path.isfile(self.loaded_filelist_path):
            files, slang_args = self._read_multiple_filelists([self.loaded_filelist_path])
            self._parse_files(files, slang_args)
            self.set_status("RTL reloaded")
            if clear_trace:
                self.clear_trace_log()
            return True
        if self.loaded_rtl_files:
            valid = [p for p in self.loaded_rtl_files if os.path.isfile(p)]
            if valid:
                self.loaded_filelist_paths = []
                self.loaded_filelist_path = ""
                self.loaded_dir_path = ""
                self._parse_files(valid, [])
                self.set_status("RTL reloaded")
                if clear_trace:
                    self.clear_trace_log()
                return True
        arg_filelists = [p for p in self._arg_filelists() if os.path.isfile(p)]
        if arg_filelists:
            self.loaded_filelist_paths = arg_filelists
            self.loaded_filelist_path = arg_filelists[0]
            files, slang_args = self._read_multiple_filelists(arg_filelists)
            self._parse_files(files, slang_args)
            self.set_status("RTL reloaded")
            if clear_trace:
                self.clear_trace_log()
            return True
        arg_rtl_files = [p for p in self._arg_rtl_files() if os.path.isfile(p)]
        if arg_rtl_files:
            self.loaded_filelist_paths = []
            self.loaded_filelist_path = ""
            self.loaded_dir_path = ""
            self._parse_files(arg_rtl_files, [])
            self.set_status("RTL reloaded")
            if clear_trace:
                self.clear_trace_log()
            return True
        if self.loaded_dir_path and os.path.isdir(self.loaded_dir_path):
            self._parse_files(discover_sv_files(self.loaded_dir_path), [])
            self.set_status("RTL reloaded")
            if clear_trace:
                self.clear_trace_log()
            return True
        self.set_status("RTL reload skipped (no source target)")
        return False

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if Qt is object:
            super().keyPressEvent(event)
            return
        mods = event.modifiers() if hasattr(event, "modifiers") else 0
        ctrl_mask = self._enum_to_int(getattr(Qt, "ControlModifier", 0))
        ctrl_pressed = bool(self._enum_to_int(mods) & ctrl_mask)
        handled = False
        combo = self._shortcut_combo_for_event(event)
        if self._run_qt_shortcut_action(combo):
            handled = True
        if (not handled) and ctrl_pressed and hasattr(event, "key"):
            key = self._enum_to_int(event.key())
            tab = self._current_right_tab_name()
            plus_keys = {
                self._enum_to_int(getattr(Qt, "Key_Equal", 0)),
                self._enum_to_int(getattr(Qt, "Key_Plus", 0)),
            }
            plus_keys.discard(0)
            minus_keys = {self._enum_to_int(getattr(Qt, "Key_Minus", 0))}
            if hasattr(Qt, "Key_Underscore"):
                minus_keys.add(self._enum_to_int(getattr(Qt, "Key_Underscore", 0)))
            minus_keys.discard(0)
            if key in plus_keys:
                if tab == "RTL Structure":
                    self.adjust_rtl_structure_zoom(1.25)
                    handled = True
                elif tab == "Schematic":
                    self.adjust_schematic_zoom(1.25)
                    handled = True
            elif key in minus_keys:
                if tab == "RTL Structure":
                    self.adjust_rtl_structure_zoom(0.8)
                    handled = True
                elif tab == "Schematic":
                    self.adjust_schematic_zoom(0.8)
                    handled = True
            elif key == self._enum_to_int(getattr(Qt, "Key_0", 0)):
                if tab == "RTL Structure":
                    self.fit_rtl_structure()
                    handled = True
                elif tab == "Schematic":
                    self.fit_schematic()
                    handled = True
        if handled:
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_session()
        super().closeEvent(event)


def run_qt(args: argparse.Namespace) -> None:
    """Launch the Qt GUI with the provided parsed CLI arguments."""
    if QApplication is None:
        raise RuntimeError("PySide6 is not installed. install with: pip install PySide6")
    if getattr(args, "schematic_view", "external") == "webengine":
        os.environ.setdefault("QT_OPENGL", "software")
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
        extra_flags = "--disable-gpu --disable-gpu-compositing --disable-software-rasterizer"
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " " + extra_flags).strip()
        app_attr = getattr(Qt, "ApplicationAttribute", None)
        if app_attr is not None and hasattr(app_attr, "AA_UseSoftwareOpenGL"):
            QApplication.setAttribute(app_attr.AA_UseSoftwareOpenGL, True)
    app = QApplication([])
    w = SvViewQtWindow(args)
    # Maximize explicitly; enum path differs by Qt bindings/version.
    max_state = getattr(Qt, "WindowMaximized", None)
    if max_state is None and hasattr(Qt, "WindowState"):
        max_state = Qt.WindowState.WindowMaximized
    if max_state is not None:
        w.setWindowState(w.windowState() | max_state)
    w.show()
    app.exec()


def main() -> None:
    """Console-script entry point for the dedicated ``rtlens-qt`` launcher."""
    # Reuse the common parser so rtlens/rtlens-qt stay option-compatible.
    from .app_cli import build_arg_parser

    parser = build_arg_parser()
    # rtlens-qt always runs Qt even though the shared parser exposes --ui.
    parser.set_defaults(ui="qt")
    args = parser.parse_args()
    if getattr(args, "ui", "qt") != "qt":
        warnings.warn("rtlens-qt always uses the Qt backend; --ui tk is ignored", stacklevel=1)
        setattr(args, "ui", "qt")
    run_qt(args)
