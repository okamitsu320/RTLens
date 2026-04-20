from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import warnings
from typing import Dict, List, Optional, Tuple

from .callable_resolver import (
    explain_callable_resolution,
    resolve_callable_key_any_site,
    resolve_callable_key_for_definition_site,
    resolve_callable_key_from_site,
    token_variants,
)
from .connectivity import build_connectivity, build_hierarchy, query_signal
from .editor_cmd import build_editor_argv
from .model import ConnectivityDB, DesignDB
from .slang_backend import SlangBackendError, load_design_with_slang
from .sv_parser import discover_sv_files, parse_sv_files, read_filelist_with_args
from .wave import WaveDB, load_wave
from .wave_bridge import WaveBridgeError, create_wave_bridge

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ModuleNotFoundError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None

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


class SvViewApp:
    """Tk-based legacy GUI application.

    This backend is deprecated and kept temporarily for transition. Prefer the
    Qt UI (`--ui qt` or `rtlens-qt`) for new usage.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        if tk is None:
            raise RuntimeError("tkinter is not installed. install python3-tk package")
        warnings.warn(
            "Tk UI backend is deprecated and will be removed in a future release; use --ui qt or rtlens-qt",
            FutureWarning,
            stacklevel=2,
        )
        self.args = args
        self.root = tk.Tk()
        self.root.title("RTLens")
        self.root.geometry("1700x950")

        self.design = DesignDB()
        self.connectivity = ConnectivityDB()
        self.wave: Optional[WaveDB] = None

        self.current_file: Optional[str] = None
        self.current_line: int = 1
        self.current_hier_path: Optional[str] = None
        self.file_cache: Dict[str, List[str]] = {}
        self.hier_to_tree_item: Dict[str, str] = {}
        self._suppress_hier_select: bool = False
        self.wave_signals: List[str] = []
        self.loaded_filelist_path: str = ""
        self.loaded_filelist_paths: List[str] = []
        self.loaded_dir_path: str = ""
        self.loaded_wave_path: str = ""
        self.loaded_slang_args: List[str] = []
        self.compile_log: str = "Compile log is empty."
        self.wave_bridge = create_wave_bridge(args.wave_viewer, args.gtkwave_cmd, args.surfer_cmd)
        self._source_ctx_signal: Optional[str] = None
        self._source_ctx_word: str = ""
        self._source_ctx_line: int = 1
        self._source_ctx_include_path: Optional[str] = None
        self._source_ctx_instance_path: Optional[str] = None
        self._source_ctx_selected_signals: List[str] = []
        self._wave_ctx_signal: Optional[str] = None
        self._hier_ctx_path: Optional[str] = None
        self.loaded_rtl_files: List[str] = []
        self.search_hits: List[Tuple[str, int, str]] = []
        self.trace_actions: List[dict] = []
        self.current_wave_name: str = ""
        self.current_wave_changes: List[Tuple[int, str]] = []
        self.wave_total_t0: int = 0
        self.wave_total_t1: int = 1
        self.wave_view_t0: int = 0
        self.wave_view_t1: int = 1
        self.nav_history: List[Tuple[str, int]] = []
        self.nav_index: int = -1

        self._build_ui()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        toolbar_row1 = ttk.Frame(toolbar)
        toolbar_row1.pack(side=tk.TOP, fill=tk.X)
        toolbar_row2 = ttk.Frame(toolbar)
        toolbar_row2.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar_row1, text="Open filelist", command=self.open_filelist).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(toolbar_row1, text="Open dir", command=self.open_dir).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(toolbar_row1, text="Load wave", command=self.open_wave).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(toolbar_row1, text="Open External Wave", command=self.open_external_wave).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(toolbar_row1, text="Import Wave Sel", command=self.import_wave_selection).pack(side=tk.LEFT, padx=4, pady=2)

        ttk.Button(toolbar_row2, text="Reload RTL", command=self.reload_rtl).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(toolbar_row2, text="Reload Wave", command=self.reload_wave).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(toolbar_row2, text="Reload All", command=self.reload_all).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(toolbar_row2, text="Reset Layout", command=self._setup_initial_layout).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Button(toolbar_row2, text="Open in editor", command=self.open_external_editor).pack(side=tk.LEFT, padx=4, pady=2)
        ttk.Separator(toolbar_row2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(toolbar_row2, text="Status:").pack(side=tk.LEFT, padx=2)
        ttk.Label(toolbar_row2, textvariable=self.status_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)


        outer = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True)
        self.outer_pane = outer

        left = ttk.Panedwindow(outer, orient=tk.VERTICAL)
        outer.add(left, weight=1)
        self.left_pane = left

        hier_frame = ttk.LabelFrame(left, text="Hierarchy")
        self.hier_tree = ttk.Treeview(hier_frame, show="tree")
        self.hier_tree.heading("#0", text="Instance")
        self.hier_tree.column("#0", width=320, stretch=True)
        self.hier_tree.bind("<<TreeviewSelect>>", self.on_hier_select)
        self.hier_tree.bind("<Button-3>", self.on_hier_context_menu)

        self.hier_tree.pack(in_=hier_frame, side=tk.LEFT, fill=tk.BOTH, expand=True)
        y = ttk.Scrollbar(hier_frame, command=self.hier_tree.yview)
        y.pack(side=tk.RIGHT, fill=tk.Y)
        self.hier_tree.configure(yscrollcommand=y.set)
        left.add(hier_frame, weight=3)

        lower = ttk.Panedwindow(left, orient=tk.HORIZONTAL)

        sig_left = ttk.Frame(lower)
        ttk.Label(sig_left, text="Signals in selected hierarchy").pack(anchor=tk.W, padx=4, pady=4)
        self.scope_signal_filter = tk.StringVar()
        scope_filter = ttk.Entry(sig_left, textvariable=self.scope_signal_filter)
        scope_filter.pack(fill=tk.X, padx=4)
        scope_filter.bind("<KeyRelease>", lambda _e: self.refresh_scope_signal_list())
        self.scope_signal_list = tk.Listbox(sig_left, height=18)
        self.scope_signal_list.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.scope_signal_list.bind("<Double-Button-1>", self.on_scope_signal_select)
        lower.add(sig_left, weight=1)

        sig_right = ttk.Frame(lower)
        self.signal_entry = ttk.Entry(sig_right)
        self.signal_entry.bind("<Return>", lambda _e: self.search_signal())
        ttk.Label(sig_right, text="Signal path (abs or local in selected hierarchy):").pack(
            anchor=tk.W, padx=4, pady=4
        )
        self.signal_entry.pack(fill=tk.X, padx=4)
        self.include_control_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            sig_right, text="Include control deps", variable=self.include_control_var, command=self.search_signal_if_any
        ).pack(
            anchor=tk.W, padx=4, pady=2
        )
        self.include_clock_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            sig_right, text="Include clock deps", variable=self.include_clock_var, command=self.search_signal_if_any
        ).pack(
            anchor=tk.W, padx=4, pady=2
        )
        self.include_port_sites_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            sig_right, text="Include port sites", variable=self.include_port_sites_var, command=self.search_signal_if_any
        ).pack(
            anchor=tk.W, padx=4, pady=2
        )
        ttk.Button(sig_right, text="Search load/drive", command=self.search_signal).pack(anchor=tk.W, padx=4, pady=4)
        ttk.Button(sig_right, text="Show definition", command=self.show_signal_definition).pack(anchor=tk.W, padx=4, pady=2)

        self.driver_list = tk.Listbox(sig_right, height=8)
        self.load_list = tk.Listbox(sig_right, height=8)
        ttk.Label(sig_right, text="Drivers").pack(anchor=tk.W, padx=4)
        self.driver_list.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)
        ttk.Label(sig_right, text="Loads").pack(anchor=tk.W, padx=4)
        self.load_list.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)
        self.driver_list.bind("<Double-Button-1>", self.on_driver_jump)
        self.load_list.bind("<Double-Button-1>", self.on_load_jump)
        lower.add(sig_right, weight=2)
        left.add(lower, weight=2)

        right = ttk.Panedwindow(outer, orient=tk.VERTICAL)
        outer.add(right, weight=3)
        self.right_pane = right

        top_tabs = ttk.Notebook(right)
        self.top_tabs = top_tabs

        src_frame = ttk.Frame(top_tabs)
        src_nav = ttk.Frame(src_frame)
        src_nav.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)
        self.source_back_btn = ttk.Button(src_nav, text="◀", command=self.go_back)
        self.source_back_btn.pack(side=tk.LEFT, padx=2)
        self.source_forward_btn = ttk.Button(src_nav, text="▶", command=self.go_forward)
        self.source_forward_btn.pack(side=tk.LEFT, padx=2)
        self.source_nav_var = tk.StringVar(value="(no source)")
        ttk.Label(src_nav, text="Current:").pack(side=tk.LEFT, padx=(10, 2))
        ttk.Label(src_nav, textvariable=self.source_nav_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self._update_nav_buttons()

        src_body = ttk.Frame(src_frame)
        src_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.source_text = tk.Text(src_body, wrap=tk.NONE, font=("DejaVu Sans Mono", 11))
        self.source_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.source_text.bind("<Double-Button-1>", self.on_source_double_click)
        self.source_text.bind("<Button-3>", self.on_source_context_menu)
        ys = ttk.Scrollbar(src_body, command=self.source_text.yview)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        xs = ttk.Scrollbar(src_body, orient=tk.HORIZONTAL, command=self.source_text.xview)
        xs.pack(side=tk.BOTTOM, fill=tk.X)
        self.source_text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.source_text.tag_configure("kw", foreground="#0055aa")
        self.source_text.tag_configure("comment", foreground="#6a737d")
        self.source_text.tag_configure("hit", background="#fff2a8")
        self.source_text.tag_configure("focusline", background="#cfe8ff")
        top_tabs.add(src_frame, text="Source")

        log_frame = ttk.Frame(top_tabs)
        self.compile_log_text = tk.Text(log_frame, wrap=tk.NONE, font=("DejaVu Sans Mono", 10))
        self.compile_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ys_log = ttk.Scrollbar(log_frame, command=self.compile_log_text.yview)
        ys_log.pack(side=tk.RIGHT, fill=tk.Y)
        xs_log = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.compile_log_text.xview)
        xs_log.pack(side=tk.BOTTOM, fill=tk.X)
        self.compile_log_text.configure(yscrollcommand=ys_log.set, xscrollcommand=xs_log.set)
        top_tabs.add(log_frame, text="Compile Log")

        search_frame = ttk.Frame(top_tabs)
        search_ctl = ttk.Frame(search_frame)
        search_ctl.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)
        ttk.Label(search_ctl, text="Pattern").pack(side=tk.LEFT)
        self.search_pattern_var = tk.StringVar()
        self.search_pattern_entry = ttk.Entry(search_ctl, textvariable=self.search_pattern_var, width=40)
        self.search_pattern_entry.pack(side=tk.LEFT, padx=4)
        self.search_pattern_entry.bind("<Return>", lambda _e: self.run_text_search())
        ttk.Label(search_ctl, text="Scope").pack(side=tk.LEFT, padx=6)
        self.search_scope_var = tk.StringVar(value="Current file")
        self.search_scope_combo = ttk.Combobox(
            search_ctl,
            textvariable=self.search_scope_var,
            values=("Current file", "Design files"),
            state="readonly",
            width=14,
        )
        self.search_scope_combo.pack(side=tk.LEFT)
        self.search_regex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(search_ctl, text="Regex", variable=self.search_regex_var).pack(side=tk.LEFT, padx=8)
        ttk.Button(search_ctl, text="Search", command=self.run_text_search).pack(side=tk.LEFT, padx=4)

        self.search_result_list = tk.Listbox(search_frame)
        self.search_result_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.search_result_list.bind("<Double-Button-1>", self.on_search_result_jump)
        ys_s = ttk.Scrollbar(search_frame, command=self.search_result_list.yview)
        ys_s.pack(side=tk.RIGHT, fill=tk.Y)
        self.search_result_list.configure(yscrollcommand=ys_s.set)
        top_tabs.add(search_frame, text="Search")

        trace_frame = ttk.Frame(top_tabs)
        trace_ctl = ttk.Frame(trace_frame)
        trace_ctl.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)
        ttk.Button(trace_ctl, text="Clear Trace", command=self.clear_trace_log).pack(side=tk.LEFT, padx=4)
        self.trace_list = tk.Listbox(trace_frame)
        self.trace_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.trace_list.bind("<Double-Button-1>", self.on_trace_jump)
        ys_t = ttk.Scrollbar(trace_frame, command=self.trace_list.yview)
        ys_t.pack(side=tk.RIGHT, fill=tk.Y)
        self.trace_list.configure(yscrollcommand=ys_t.set)
        top_tabs.add(trace_frame, text="Trace Log")
        right.add(top_tabs, weight=3)
        self._refresh_compile_log_view()

        wave_frame = ttk.Panedwindow(right, orient=tk.HORIZONTAL)

        left_wave = ttk.Frame(wave_frame)
        self.wave_filter_var = tk.StringVar()
        wf = ttk.Entry(left_wave, textvariable=self.wave_filter_var)
        wf.pack(fill=tk.X, padx=4, pady=2)
        wf.bind("<KeyRelease>", lambda _e: self.refresh_wave_list())

        self.wave_list = tk.Listbox(left_wave)
        self.wave_list.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)
        self.wave_list.bind("<<ListboxSelect>>", self.on_wave_select)
        self.wave_list.bind("<Button-3>", self.on_wave_context_menu)
        wave_frame.add(left_wave, weight=1)

        right_wave = ttk.Frame(wave_frame)
        wave_ctl1 = ttk.Frame(right_wave)
        wave_ctl1.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)
        self.wave_file_var = tk.StringVar(value="(no wave loaded)")
        self.wave_range_var = tk.StringVar(value="t=[0,1] width=1 center=0")
        self.wave_center_var = tk.StringVar(value="0")
        ttk.Label(wave_ctl1, text="Wave file:").pack(side=tk.LEFT, padx=4)
        ttk.Label(wave_ctl1, textvariable=self.wave_file_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        wave_ctl2 = ttk.Frame(right_wave)
        wave_ctl2.pack(side=tk.TOP, fill=tk.X, padx=4, pady=2)
        ttk.Button(wave_ctl2, text="Zoom +", command=lambda: self.zoom_wave(0.5)).pack(side=tk.LEFT, padx=2)
        ttk.Button(wave_ctl2, text="Zoom -", command=lambda: self.zoom_wave(2.0)).pack(side=tk.LEFT, padx=2)
        ttk.Button(wave_ctl2, text="Fit", command=self.fit_wave).pack(side=tk.LEFT, padx=2)
        ttk.Separator(wave_ctl2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(wave_ctl2, text="<<", command=lambda: self.shift_wave_center(-1.0)).pack(side=tk.LEFT, padx=1)
        ttk.Button(wave_ctl2, text="<", command=lambda: self.shift_wave_center(-0.25)).pack(side=tk.LEFT, padx=1)
        ttk.Button(wave_ctl2, text=">", command=lambda: self.shift_wave_center(0.25)).pack(side=tk.LEFT, padx=1)
        ttk.Button(wave_ctl2, text=">>", command=lambda: self.shift_wave_center(1.0)).pack(side=tk.LEFT, padx=1)
        ttk.Separator(wave_ctl2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Label(wave_ctl2, text="Center").pack(side=tk.LEFT, padx=4)
        ttk.Entry(wave_ctl2, textvariable=self.wave_center_var, width=14).pack(side=tk.LEFT)
        ttk.Button(wave_ctl2, text="Set", command=self.set_wave_center).pack(side=tk.LEFT, padx=2)

        wave_ctl3 = ttk.Frame(right_wave)
        wave_ctl3.pack(side=tk.TOP, fill=tk.X, padx=4, pady=1)
        ttk.Label(wave_ctl3, textvariable=self.wave_range_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        self.wave_canvas = tk.Canvas(right_wave, bg="#101820")
        self.wave_canvas.pack(fill=tk.BOTH, expand=True)
        self.wave_canvas.bind("<Configure>", lambda _e: self.redraw_wave())
        wave_frame.add(right_wave, weight=2)

        right.add(wave_frame, weight=2)

        self.source_ctx_menu = tk.Menu(self.root, tearoff=0)
        self.source_ctx_menu.add_command(label="Set as current signal", command=self.ctx_source_set_signal)
        self.source_ctx_menu.add_command(label="Copy signal fullpath", command=self.ctx_source_copy_fullpath)
        self.source_ctx_menu.add_command(label="Copy instance fullpath", command=self.ctx_source_copy_instance_fullpath)
        self.source_ctx_menu.add_command(label="Find drivers/loads", command=self.ctx_source_search_signal)
        self.source_ctx_menu.add_command(label="Show definition", command=self.ctx_source_show_definition)
        self.source_ctx_menu.add_command(label="Show include file", command=self.ctx_source_show_include)
        self.source_ctx_menu.add_command(label="Find references", command=self.ctx_source_find_references)
        self.source_ctx_menu.add_command(label="Add to external wave", command=self.ctx_source_add_to_wave)

        self.hier_ctx_menu = tk.Menu(self.root, tearoff=0)
        self.hier_ctx_menu.add_command(label="Copy instance fullpath", command=self.ctx_hier_copy_fullpath)

        self.wave_ctx_menu = tk.Menu(self.root, tearoff=0)
        self.wave_ctx_menu.add_command(label="Use as current signal", command=self.ctx_wave_set_signal)
        self.wave_ctx_menu.add_command(label="Jump to source", command=self.ctx_wave_jump_source)
        self.wave_ctx_menu.add_command(label="Show definition", command=self.ctx_wave_show_definition)
        self.wave_ctx_menu.add_command(label="Add to external wave", command=self.ctx_wave_add_to_wave)

        status_bar = tk.Frame(self.root, bd=1, relief=tk.SUNKEN, height=24)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Label(status_bar, textvariable=self.status_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=2)

    def set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _update_nav_buttons(self) -> None:
        if not hasattr(self, "source_back_btn"):
            return
        self.source_back_btn.configure(state=(tk.NORMAL if self.nav_index > 0 else tk.DISABLED))
        can_fwd = self.nav_index >= 0 and self.nav_index < len(self.nav_history) - 1
        self.source_forward_btn.configure(state=(tk.NORMAL if can_fwd else tk.DISABLED))

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

    def _poll_wave_bridge_events(self) -> None:
        try:
            events = self.wave_bridge.poll_events()
        except Exception:
            events = []
        for ev, payload in events:
            self._handle_wave_bridge_event(ev, payload)
        self.root.after(250, self._poll_wave_bridge_events)

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
            self.signal_entry.delete(0, tk.END)
            self.signal_entry.insert(0, sig)
            if ev == "goto_declaration":
                self.show_signal_definition()
                return
            # For add_drivers/add_loads, show connectivity list for the signal.
            self.search_signal()
            return

    def _append_trace(self, label: str, action: dict) -> None:
        self.trace_actions.append(action)
        self.trace_list.insert(tk.END, label)

    def clear_trace_log(self) -> None:
        self.trace_actions.clear()
        self.trace_list.delete(0, tk.END)

    def on_trace_jump(self, _event=None) -> None:
        sel = self.trace_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < 0 or idx >= len(self.trace_actions):
            return
        self._run_trace_action(self.trace_actions[idx])

    def _run_trace_action(self, action: dict) -> None:
        t = action.get("type", "")
        if t == "goto":
            file = action.get("file", "")
            line = int(action.get("line", 1))
            token = action.get("token", "")
            if file:
                self.show_file(file, line, token=token, token_line=line, focus_line=line, focus_label=token)
                self.top_tabs.select(0)
            return
        if t == "signal-trace":
            sig = action.get("signal", "")
            if not sig:
                return
            self.signal_entry.delete(0, tk.END)
            self.signal_entry.insert(0, sig)
            self.include_control_var.set(bool(action.get("include_control", False)))
            self.include_clock_var.set(bool(action.get("include_clock", True)))
            self.include_port_sites_var.set(bool(action.get("include_ports", False)))
            self.search_signal()
            return
        if t == "callable-refs":
            key = action.get("key", "")
            if key:
                self._trace_callable_references(key)
            return

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

    def _read_multiple_filelists(self, paths: List[str]) -> Tuple[List[str], List[str]]:
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
        self.file_cache.clear()
        self.loaded_rtl_files = [os.path.abspath(f) for f in files]
        merged_slang_args = list(slang_args or []) + self._extra_slang_args_from_cli()
        self.loaded_slang_args = merged_slang_args
        try:
            self.design, self.connectivity, self.compile_log = load_design_with_slang(
                files, self.args.top, merged_slang_args
            )
            self._refresh_compile_log_view()
            self.refresh_hierarchy()
            self.set_status(
                "Parsed by slang: "
                f"modules={len(self.design.modules)} hier={len(self.design.hier)} files={len(files)} "
                f"slang_args={len(merged_slang_args)} "
                "(Hierarchy: left-top pane)"
            )
            return
        except SlangBackendError as e:
            self.compile_log = str(e)
            self._refresh_compile_log_view()
            self.set_status(f"slang backend unavailable, fallback parser used: {e}")

        self.design = parse_sv_files(files)
        build_hierarchy(self.design, self.args.top)
        self.connectivity = build_connectivity(self.design)
        self.compile_log += (
            "\n\n[rtlens] fallback parser summary\n"
            f"modules={len(self.design.modules)} hier={len(self.design.hier)} files={len(files)}"
        )
        self._refresh_compile_log_view()
        self.refresh_hierarchy()

    def _refresh_compile_log_view(self) -> None:
        self.compile_log_text.configure(state=tk.NORMAL)
        self.compile_log_text.delete("1.0", tk.END)
        self.compile_log_text.insert("1.0", self.compile_log)
        self.compile_log_text.configure(state=tk.DISABLED)

    def _design_search_files(self) -> List[str]:
        files: List[str] = []
        seen = set()
        for f in self.loaded_rtl_files:
            af = os.path.abspath(f)
            if af in seen:
                continue
            if os.path.isfile(af):
                files.append(af)
                seen.add(af)
        for mod in self.design.modules.values():
            af = os.path.abspath(mod.file)
            if af in seen:
                continue
            if os.path.isfile(af):
                files.append(af)
                seen.add(af)
        return files

    def run_text_search(self) -> None:
        pat = self.search_pattern_var.get()
        if not pat:
            self.set_status("Search pattern is empty")
            return
        use_regex = bool(self.search_regex_var.get())
        scope = self.search_scope_var.get().strip()
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
        self.search_result_list.delete(0, tk.END)
        for path in files:
            try:
                if path not in self.file_cache:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        self.file_cache[path] = f.readlines()
                lines = self.file_cache[path]
            except Exception:
                continue
            for i, raw in enumerate(lines, start=1):
                body = raw.rstrip("\n")
                ok = False
                if rx is not None:
                    ok = rx.search(body) is not None
                else:
                    ok = pat in body
                if not ok:
                    continue
                snippet = body.strip()
                if len(snippet) > 140:
                    snippet = snippet[:137] + "..."
                self.search_hits.append((path, i, snippet))
                self.search_result_list.insert(tk.END, f"{os.path.basename(path)}:{i}: {snippet}")

        self.set_status(f"Search hits: {len(self.search_hits)} ({scope}, {'regex' if use_regex else 'plain'})")

    def on_search_result_jump(self, _event=None) -> None:
        sel = self.search_result_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx < 0 or idx >= len(self.search_hits):
            return
        path, line, _snippet = self.search_hits[idx]
        self.show_file(path, line, token=None, token_line=None, focus_line=line, focus_label="search-hit")

    def _find_signal_definition(self, abs_sig: str) -> Optional[Tuple[str, int, str]]:
        token = abs_sig.split(".")[-1]
        loc = self.connectivity.signal_to_source.get(abs_sig)
        if not loc:
            return None
        path = loc.file
        try:
            if path not in self.file_cache:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    self.file_cache[path] = f.readlines()
            lines = self.file_cache[path]
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

        # Best effort fallback: likely implicit net / generated signal.
        return path, loc.line, "implicit-wire-or-generated (best effort)"

    def show_signal_definition(self) -> None:
        q = self.signal_entry.get().strip()
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
        self.show_file(path, line, token=token, token_line=line, focus_line=line, focus_label=token, focus_signal=resolved)
        self.set_status(f"Definition: {resolved} => {typ} @ {path}:{line}")
        self._append_trace(
            f"def {resolved} @ {path}:{line}",
            {"type": "goto", "file": path, "line": line, "token": token},
        )

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
        keys: List[str] = []
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

    def _open_callable_definition(self, key: str) -> None:
        loc = self.design.callable_defs.get(key)
        if not loc:
            self.set_status(f"definition not found: {key}")
            return
        token = self.design.callable_names.get(key, key.split(":", 1)[-1].split(".")[-1])
        self.show_file(loc.file, loc.line, token=token, token_line=loc.line, focus_line=loc.line, focus_label=token)
        self.set_status(f"Definition: {key} @ {loc.file}:{loc.line}")
        self.top_tabs.select(0)
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
            self.top_tabs.select(3)
            return
        self._append_trace(f"refs {name}: {len(refs)}", {"type": "callable-refs", "key": key})
        token = name.split(".")[-1]
        for loc in refs:
            self._append_trace(
                f"  {loc.file}:{loc.line}",
                {"type": "goto", "file": loc.file, "line": loc.line, "token": token},
            )
        self.set_status(f"References: {name} -> {len(refs)}")
        self.top_tabs.select(3)

    def _jump_to_source_or_callable(self, file: str, line: int, token: str, focus_signal: str) -> None:
        key = self._resolve_callable_key_any_site(file=file, line=line)
        if key and self.design.callable_kinds.get(key, "") in {"function", "task"}:
            self._open_callable_definition(key)
            return
        self.show_file(
            file,
            line,
            token,
            line,
            focus_line=line,
            focus_label=token,
            focus_signal=focus_signal,
        )

    def open_filelist(self) -> None:
        path = filedialog.askopenfilename(title="Open filelist")
        if not path:
            return
        try:
            files, slang_args = self._read_multiple_filelists([path])
            self.loaded_filelist_path = path
            self.loaded_filelist_paths = [path]
            self.loaded_dir_path = ""
            self._parse_files(files, slang_args)
        except Exception as e:
            messagebox.showerror("RTLens", str(e))

    def open_dir(self) -> None:
        path = filedialog.askdirectory(title="Open RTL directory")
        if not path:
            return
        files = discover_sv_files(path)
        self.loaded_dir_path = path
        self.loaded_filelist_path = ""
        self.loaded_filelist_paths = []
        self._parse_files(files, [])

    def open_wave(self) -> None:
        path = filedialog.askopenfilename(title="Open wave", filetypes=[("Wave", "*.vcd *.fst"), ("All", "*")])
        if not path:
            return
        self.loaded_wave_path = path
        self.load_wave_file(path)

    def load_wave_file(self, path: str) -> None:
        try:
            self.wave = load_wave(path)
            self.wave_signals = sorted(self.wave.signals.keys())
            self.refresh_wave_list()
            self.wave_file_var.set(os.path.basename(path))
            if self.wave and self.wave.times:
                self.wave_total_t0 = self.wave.times[0]
                self.wave_total_t1 = self.wave.times[-1] if self.wave.times[-1] > self.wave.times[0] else self.wave.times[0] + 1
            else:
                self.wave_total_t0 = 0
                self.wave_total_t1 = 1
            self.wave_view_t0 = self.wave_total_t0
            self.wave_view_t1 = self.wave_total_t1
            self.wave_center_var.set(str((self.wave_view_t0 + self.wave_view_t1) // 2))
            self._update_wave_range_label()
            self.current_wave_name = ""
            self.current_wave_changes = []
            self.wave_canvas.delete("all")
            try:
                self.wave_bridge.open(path)
            except WaveBridgeError as e:
                self.set_status(f"Wave loaded (external viewer unavailable: {e})")
                return
            self.set_status(f"Wave loaded: {path}")
        except Exception as e:
            messagebox.showerror("RTLens", str(e))

    def open_external_wave(self) -> None:
        path = self.loaded_wave_path or self.args.wave
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
            messagebox.showerror("RTLens", str(e))

    def _cleanup_wave_name(self, name: str) -> str:
        s = name.strip()
        if not s:
            return s
        s = s.strip("'\"")
        # normalize common hierarchy separators from external viewers
        s = s.replace("/", ".")
        while ".." in s:
            s = s.replace("..", ".")
        # remove leading separators used by some viewers
        while s.startswith(".") or s.startswith("/"):
            s = s[1:]
        # strip all bit/part select suffixes for matching
        s = re.sub(r"\[[^\]]+\]", "", s)
        return s

    def _extract_wave_name_candidates(self, text: str) -> List[str]:
        t = text.strip()
        if not t:
            return []
        out: List[str] = []

        # 1) line-first tokens
        for line in t.splitlines():
            s = line.strip()
            if not s:
                continue
            token = s.split()[0]
            if "." in token or "/" in token or re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", token):
                out.append(token)

        # 2) generic path-like scan fallback
        for m in re.finditer(r"[A-Za-z0-9_$./\\\[\]:]+", t):
            token = m.group(0)
            if "." in token or "/" in token or re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", token):
                out.append(token)

        # keep order, drop dup
        uniq: List[str] = []
        seen = set()
        for x in out:
            if x in seen:
                continue
            seen.add(x)
            uniq.append(x)
        return uniq

    def _resolve_wave_name_to_design(self, wave_name: str) -> Optional[str]:
        cleaned = self._cleanup_wave_name(wave_name)
        if not cleaned:
            return None
        if cleaned in self.connectivity.signal_to_source:
            return cleaned
        # Allow bare signal names copied from wave tools.
        if "." not in cleaned:
            r = self.resolve_signal_query(cleaned)
            if r:
                return r
        return self.best_wave_to_design_match(cleaned)

    def import_wave_selection(self) -> None:
        clips: List[Tuple[str, str]] = []
        try:
            clips.append(("CLIPBOARD", self.root.clipboard_get()))
        except Exception:
            pass
        if getattr(self.args, "wave_import_primary", False):
            try:
                clips.append(("PRIMARY", self.root.selection_get(selection="PRIMARY")))
            except Exception:
                pass
        if not clips:
            self.set_status("No clipboard selection found (CLIPBOARD/PRIMARY)")
            return

        picked: Optional[str] = None
        picked_src = ""
        picked_raw = ""
        for src, clip in clips:
            for cand in self._extract_wave_name_candidates(str(clip)):
                picked = self._resolve_wave_name_to_design(cand)
                if picked:
                    picked_src = src
                    picked_raw = cand
                    break
            if picked:
                break
        if not picked:
            raw = clips[0][1].strip().replace("\n", " ")
            if len(raw) > 80:
                raw = raw[:80] + "..."
            self.set_status(f"Could not resolve wave signal from {clips[0][0]}: {raw}")
            return
        self.signal_entry.delete(0, tk.END)
        self.signal_entry.insert(0, picked)
        self.search_signal()
        loc = self.connectivity.signal_to_source.get(picked)
        if loc:
            token = picked.split(".")[-1]
            self.show_file(loc.file, loc.line, token, loc.line, focus_line=loc.line, focus_label=token, focus_signal=picked)
        self.set_status(f"Imported wave signal ({picked_src}): {picked_raw} -> {picked}")

    def refresh_hierarchy(self) -> None:
        self.hier_tree.delete(*self.hier_tree.get_children())
        self.hier_to_tree_item.clear()

        def add_node(path: str, parent: str = "") -> None:
            node = self.design.hier[path]
            item = self.hier_tree.insert(parent, "end", text=f"{node.inst_name} : {node.module_name}", open=True)
            self.hier_to_tree_item[path] = item
            for c in node.children:
                add_node(c, item)

        for r in self.design.roots:
            add_node(r)

        # Show something immediately after load.
        if self.design.roots:
            root_path = self.design.roots[0]
            item = self.hier_to_tree_item.get(root_path)
            if item:
                self.hier_tree.selection_set(item)
                self.hier_tree.focus(item)
                self.on_hier_select()
                self.set_status(
                    f"Hierarchy loaded: roots={len(self.design.roots)} nodes={len(self.design.hier)} "
                    f"(left-top pane)"
                )
        else:
            self.set_status("Hierarchy is empty. Check filelist/options.")
        self.root.after(20, lambda: self._setup_initial_layout(retries=2))

    def on_hier_select(self, _event=None) -> None:
        if self._suppress_hier_select:
            return
        sel = self.hier_tree.selection()
        if not sel:
            return
        item = sel[0]
        path = None
        for k, v in self.hier_to_tree_item.items():
            if v == item:
                path = k
                break
        if not path:
            return
        self.current_hier_path = path
        node = self.design.hier[path]
        mod = self.design.modules.get(node.module_name)
        if not mod:
            return
        self.refresh_scope_signal_list()
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
        self.refresh_scope_signal_list()
        item = self.hier_to_tree_item.get(path)
        if not item:
            return
        self._suppress_hier_select = True
        try:
            self.hier_tree.selection_set(item)
            self.hier_tree.focus(item)
            self.hier_tree.see(item)
        finally:
            self._suppress_hier_select = False

    def on_hier_context_menu(self, event) -> None:
        item = self.hier_tree.identify_row(event.y)
        if item:
            self.hier_tree.selection_set(item)
            self.hier_tree.focus(item)
        sel = self.hier_tree.selection()
        self._hier_ctx_path = None
        if sel:
            cur = sel[0]
            for k, v in self.hier_to_tree_item.items():
                if v == cur:
                    self._hier_ctx_path = k
                    break
        self.hier_ctx_menu.entryconfigure(
            "Copy instance fullpath", state=(tk.NORMAL if self._hier_ctx_path else tk.DISABLED)
        )
        self.hier_ctx_menu.tk_popup(event.x_root, event.y_root)

    def ctx_hier_copy_fullpath(self) -> None:
        if not self._hier_ctx_path:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._hier_ctx_path)
            self.set_status(f"Copied instance fullpath: {self._hier_ctx_path}")
        except Exception as e:
            self.set_status(f"clipboard error: {e}")

    def show_file(
        self,
        path: str,
        line: int = 1,
        token: Optional[str] = None,
        token_line: Optional[int] = None,
        focus_line: Optional[int] = None,
        focus_label: Optional[str] = None,
        focus_signal: Optional[str] = None,
        record_history: bool = True,
    ) -> None:
        try:
            if path not in self.file_cache:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    self.file_cache[path] = f.readlines()
        except Exception as e:
            self.set_status(f"failed to open source: {path} ({e})")
            return

        lines = self.file_cache[path]
        self.current_file = path
        self.current_line = max(1, line)
        if hasattr(self, "source_nav_var"):
            self.source_nav_var.set(f"{path}:{self.current_line}")
        if record_history:
            self._push_nav(path, self.current_line)
        self.source_text.delete("1.0", tk.END)

        for i, raw in enumerate(lines, start=1):
            self.source_text.insert(tk.END, f"{i:5d} {raw}")

        self.source_text.tag_remove("kw", "1.0", tk.END)
        self.source_text.tag_remove("comment", "1.0", tk.END)
        self.source_text.tag_remove("hit", "1.0", tk.END)
        self.source_text.tag_remove("focusline", "1.0", tk.END)

        for i, raw in enumerate(lines, start=1):
            cpos = raw.find("//")
            if cpos >= 0:
                start = f"{i}.{6 + cpos}"
                end = f"{i}.end"
                self.source_text.tag_add("comment", start, end)

            for kw in SV_KEYWORDS:
                start_idx = 0
                while True:
                    p = raw.find(kw, start_idx)
                    if p < 0:
                        break
                    b = raw[p - 1] if p > 0 else " "
                    a = raw[p + len(kw)] if p + len(kw) < len(raw) else " "
                    if not (b.isalnum() or b == "_") and not (a.isalnum() or a == "_"):
                        s = f"{i}.{6 + p}"
                        e = f"{i}.{6 + p + len(kw)}"
                        self.source_text.tag_add("kw", s, e)
                    start_idx = p + len(kw)

        if token:
            token_re = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(token)}(?![A-Za-z0-9_$])")
            line_iter = range(1, len(lines) + 1) if token_line is None else [max(1, token_line)]
            for i in line_iter:
                raw = lines[i - 1]
                for m in token_re.finditer(raw):
                    s = f"{i}.{6 + m.start()}"
                    e = f"{i}.{6 + m.end()}"
                    self.source_text.tag_add("hit", s, e)

        if focus_line is not None:
            fl = max(1, min(len(lines), focus_line))
            self.source_text.tag_add("focusline", f"{fl}.0", f"{fl}.end")

        self.source_text.see(f"{line}.0")
        if focus_label and focus_signal:
            self.set_status(f"{focus_label} ({focus_signal}) @ {path}:{line}")
        elif focus_label:
            self.set_status(f"{focus_label} @ {path}:{line}")
        else:
            self.set_status(f"{path}:{line}")

    def search_signal(self) -> None:
        q = self.signal_entry.get().strip()
        if not q:
            return
        resolved = self.resolve_signal_query(q)
        if not resolved:
            self.set_status(f"signal not found: {q}")
            return
        q = resolved
        self.signal_entry.delete(0, tk.END)
        self.signal_entry.insert(0, q)
        self._follow_hierarchy_for_signal(q)

        include_control = bool(self.include_control_var.get())
        include_clock = bool(self.include_clock_var.get())
        include_ports = bool(self.include_port_sites_var.get())
        drivers, loads = query_signal(
            self.connectivity,
            q,
            recursive=False,
            include_control=include_control,
            include_clock=include_clock,
            include_ports=include_ports,
        )
        port_hint = ""
        if (not include_ports) and (not drivers) and (not loads):
            port_drivers, port_loads = query_signal(
                self.connectivity,
                q,
                recursive=False,
                include_control=include_control,
                include_clock=include_clock,
                include_ports=True,
            )
            if port_drivers or port_loads:
                port_hint = " | hint: enable 'Include port sites'"
        self.driver_list.delete(0, tk.END)
        self.load_list.delete(0, tk.END)

        for sig, loc in drivers:
            self.driver_list.insert(tk.END, f"{sig} -> {loc.file}:{loc.line}")
        for sig, loc in loads:
            self.load_list.insert(tk.END, f"{sig} -> {loc.file}:{loc.line}")

        ctrl = "with-control" if include_control else "data-only"
        clk = "with-clock" if include_clock else "no-clock"
        ports = "with-ports" if include_ports else "no-ports"
        self.set_status(f"Drivers: {len(drivers)}, Loads: {len(loads)} (direct, {ctrl}, {clk}, {ports}){port_hint}")
        self._append_trace(
            f"trace {q} ({ctrl}, {clk}, {ports})",
            {
                "type": "signal-trace",
                "signal": q,
                "include_control": include_control,
                "include_clock": include_clock,
                "include_ports": include_ports,
            },
        )

    def search_signal_if_any(self) -> None:
        if self.signal_entry.get().strip():
            self.search_signal()

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

    def _parse_jump_item(self, s: str) -> Optional[Tuple[str, int, str]]:
        if " -> " not in s:
            return None
        sig, loc = s.split(" -> ", 1)
        if ":" not in loc:
            return None
        file, line_s = loc.rsplit(":", 1)
        try:
            line = int(line_s)
        except ValueError:
            return None
        token = sig.split(".")[-1]
        return file, line, token

    def on_driver_jump(self, _event=None) -> None:
        sel = self.driver_list.curselection()
        if not sel:
            return
        s = self.driver_list.get(sel[0])
        p = self._parse_jump_item(s)
        if p:
            file, line, token = p
            self._jump_to_source_or_callable(
                file=file,
                line=line,
                token=token,
                focus_signal=s.split(" -> ", 1)[0],
            )

    def on_load_jump(self, _event=None) -> None:
        sel = self.load_list.curselection()
        if not sel:
            return
        s = self.load_list.get(sel[0])
        p = self._parse_jump_item(s)
        if p:
            file, line, token = p
            self._jump_to_source_or_callable(
                file=file,
                line=line,
                token=token,
                focus_signal=s.split(" -> ", 1)[0],
            )

    def refresh_scope_signal_list(self) -> None:
        self.scope_signal_list.delete(0, tk.END)
        if not self.current_hier_path:
            return
        filt = self.scope_signal_filter.get().strip().lower()
        pref = self.current_hier_path + "."
        names: List[str] = []
        for sig in self.connectivity.signal_to_source.keys():
            if not sig.startswith(pref):
                continue
            tail = sig[len(pref) :]
            if "." in tail:
                continue
            if filt and filt not in tail.lower():
                continue
            names.append(tail)
        for n in sorted(names):
            self.scope_signal_list.insert(tk.END, n)

    def on_scope_signal_select(self, _event=None) -> None:
        sel = self.scope_signal_list.curselection()
        if not sel or not self.current_hier_path:
            return
        name = self.scope_signal_list.get(sel[0])
        self.signal_entry.delete(0, tk.END)
        self.signal_entry.insert(0, f"{self.current_hier_path}.{name}")
        self.search_signal()

    def on_source_double_click(self, _event=None) -> None:
        index = self.source_text.index(tk.INSERT)
        abs_sig = self.resolve_signal_from_source_index(index)
        if abs_sig:
            self.signal_entry.delete(0, tk.END)
            self.signal_entry.insert(0, abs_sig)
            self.search_signal()

    def resolve_signal_from_source_index(self, index: str) -> Optional[str]:
        line_s, col_s = index.split(".")
        line = int(line_s)
        col = int(col_s)
        txt = self.source_text.get(f"{line}.0", f"{line}.end")
        if len(txt) < 7:
            return None
        body = txt[6:]
        src_col = max(0, col - 6)
        for m in re.finditer(r"[a-zA-Z_][a-zA-Z0-9_$]*", body):
            if m.start() <= src_col <= m.end():
                return self._resolve_signal_query_from_source_token(m.group(0))
        return None

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
            # Prefer candidates in / near the selected hierarchy subtree.
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
                # Prefer real subtree hit, then deeper/common path.
                subtree = 1 if inst.startswith(hier + ".") else 0
                score = (subtree, common)
                if score > best_score:
                    best_score = score
                    best_sig = sig
            # Reject clearly unrelated candidates (only root matched).
            if best_sig and best_score[1] >= 2:
                return best_sig
            if best_sig and len(cands) == 1:
                return best_sig
            return None

        # No hierarchy context: deterministic fallback.
        return sorted(cands, key=len)[0]

    def on_source_context_menu(self, event) -> None:
        index = self.source_text.index(f"@{event.x},{event.y}")
        self.source_text.mark_set(tk.INSERT, index)
        line_s, _col_s = index.split(".")
        self._source_ctx_line = int(line_s)
        self._source_ctx_word = self._word_from_source_index(index)
        self._source_ctx_signal = self.resolve_signal_from_source_index(index)
        inc_target = self._include_target_from_source_index(index)
        self._source_ctx_include_path = self._resolve_include_path(inc_target or "") if inc_target else None
        self._source_ctx_instance_path = self._resolve_module_instance_fullpath_from_site(
            self.current_file or "", self._source_ctx_line, self._source_ctx_word
        )
        self._source_ctx_selected_signals = self._resolve_signals_from_source_selection()
        callable_key = self._resolve_callable_key_from_site(self.current_file or "", self._source_ctx_line, self._source_ctx_word)
        callable_def_key = self._resolve_callable_key_for_definition_site(
            self.current_file or "", self._source_ctx_line, self._source_ctx_word
        )
        callable_kind = self.design.callable_kinds.get(callable_key, "") if callable_key else ""
        callable_is_fn_task = callable_kind in {"function", "task"}
        callable_def_is_fn_task = False
        if callable_def_key and self.current_file:
            site_file = self.current_file or ""
            for tok in token_variants(self._source_ctx_word):
                if callable_def_key in self.design.callable_def_sites.get((site_file, int(self._source_ctx_line), tok), []):
                    callable_def_is_fn_task = self.design.callable_kinds.get(callable_def_key, "") in {"function", "task"}
                    break
        state = tk.NORMAL if self._source_ctx_signal else tk.DISABLED
        find_state = tk.NORMAL if (self._source_ctx_signal and not callable_is_fn_task) else tk.DISABLED
        if callable_def_is_fn_task:
            for label in (
                "Set as current signal",
                "Copy signal fullpath",
                "Copy instance fullpath",
                "Find drivers/loads",
                "Show include file",
                "Add to external wave",
            ):
                self.source_ctx_menu.entryconfigure(label, state=tk.DISABLED)
            self.source_ctx_menu.entryconfigure("Show definition", state=tk.NORMAL)
            self.source_ctx_menu.entryconfigure("Find references", state=tk.NORMAL)
        else:
            self.source_ctx_menu.entryconfigure(
                "Set as current signal", state=(tk.NORMAL if (self._source_ctx_signal and not callable_is_fn_task) else tk.DISABLED)
            )
            self.source_ctx_menu.entryconfigure("Copy signal fullpath", state=state)
            self.source_ctx_menu.entryconfigure(
                "Copy instance fullpath", state=(tk.NORMAL if self._source_ctx_instance_path else tk.DISABLED)
            )
            self.source_ctx_menu.entryconfigure("Find drivers/loads", state=find_state)
            self.source_ctx_menu.entryconfigure(
                "Show definition", state=(tk.NORMAL if (self._source_ctx_signal or callable_key) else tk.DISABLED)
            )
            self.source_ctx_menu.entryconfigure(
                "Show include file", state=(tk.NORMAL if self._source_ctx_include_path else tk.DISABLED)
            )
            self.source_ctx_menu.entryconfigure("Find references", state=(tk.NORMAL if callable_key else tk.DISABLED))
            add_enabled = state == tk.NORMAL or bool(self._source_ctx_selected_signals)
            if callable_is_fn_task:
                add_enabled = False
            self.source_ctx_menu.entryconfigure("Add to external wave", state=(tk.NORMAL if add_enabled else tk.DISABLED))
        self.source_ctx_menu.tk_popup(event.x_root, event.y_root)

    def _resolve_signals_from_source_selection(self) -> List[str]:
        try:
            sel = self.source_text.get("sel.first", "sel.last")
        except Exception:
            return []
        if not sel:
            return []
        found: List[str] = []
        seen = set()
        for raw_line in sel.splitlines():
            line = re.sub(r"^\s*\d+\s", "", raw_line)
            for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_$]*", line):
                s = self.resolve_signal_query(m.group(0))
                if s and s not in seen:
                    seen.add(s)
                    found.append(s)
        return found

    def _word_from_source_index(self, index: str) -> str:
        line_s, col_s = index.split(".")
        line = int(line_s)
        col = int(col_s)
        txt = self.source_text.get(f"{line}.0", f"{line}.end")
        if len(txt) < 7:
            return ""
        body = txt[6:]
        src_col = max(0, col - 6)
        for m in re.finditer(r"[a-zA-Z_][a-zA-Z0-9_$]*", body):
            if m.start() <= src_col <= m.end():
                return m.group(0)
        return ""

    def _include_target_from_source_index(self, index: str) -> Optional[str]:
        line_s, col_s = index.split(".")
        line = int(line_s)
        col = int(col_s)
        txt = self.source_text.get(f"{line}.0", f"{line}.end")
        if len(txt) < 7:
            return None
        body = txt[6:]
        src_col = max(0, col - 6)
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

    def ctx_source_set_signal(self) -> None:
        if not self._source_ctx_signal:
            return
        self.signal_entry.delete(0, tk.END)
        self.signal_entry.insert(0, self._source_ctx_signal)
        self._follow_hierarchy_for_signal(self._source_ctx_signal)
        self.set_status(f"Current signal: {self._source_ctx_signal}")

    def ctx_source_copy_fullpath(self) -> None:
        if not self._source_ctx_signal:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._source_ctx_signal)
            self.set_status(f"Copied signal fullpath: {self._source_ctx_signal}")
        except Exception as e:
            self.set_status(f"clipboard error: {e}")

    def ctx_source_copy_instance_fullpath(self) -> None:
        if not self._source_ctx_instance_path:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._source_ctx_instance_path)
            self.set_status(f"Copied instance fullpath: {self._source_ctx_instance_path}")
        except Exception as e:
            self.set_status(f"clipboard error: {e}")

    def ctx_source_search_signal(self) -> None:
        if not self._source_ctx_signal:
            return
        self.signal_entry.delete(0, tk.END)
        self.signal_entry.insert(0, self._source_ctx_signal)
        self.search_signal()

    def ctx_source_show_definition(self) -> None:
        key = self._resolve_callable_key_from_site(self.current_file or "", self._source_ctx_line, self._source_ctx_word)
        if key and self.design.callable_kinds.get(key, "") in {"function", "task"}:
            self._open_callable_definition(key)
            return
        if self._source_ctx_signal:
            self.signal_entry.delete(0, tk.END)
            self.signal_entry.insert(0, self._source_ctx_signal)
            self.show_signal_definition()
            return
        if key:
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
                messagebox.showerror("RTLens", str(e))
                return
            if ok:
                added += 1
        if added == 0:
            self.set_status("External wave viewer is disabled")
        elif added == 1:
            self.set_status(f"Added to external wave: {targets[0]}")
        else:
            self.set_status(f"Added to external wave: {added} signals")

    def refresh_wave_list(self) -> None:
        self.wave_list.delete(0, tk.END)
        f = self.wave_filter_var.get().strip().lower()
        for s in self.wave_signals:
            if f and f not in s.lower():
                continue
            self.wave_list.insert(tk.END, s)

    def on_wave_select(self, _event=None) -> None:
        if not self.wave:
            return
        sel = self.wave_list.curselection()
        if not sel:
            return
        name = self.wave_list.get(sel[0])
        ws = self.wave.signals.get(name)
        if not ws:
            return
        self.current_wave_name = name
        self.current_wave_changes = ws.changes
        self.redraw_wave()

        picked = self.best_wave_to_design_match(name)
        if picked:
            try:
                self.wave_bridge.add_signal(picked)
            except WaveBridgeError:
                pass
            self.signal_entry.delete(0, tk.END)
            self.signal_entry.insert(0, picked)
            self.search_signal()
            loc = self.connectivity.signal_to_source.get(picked)
            if loc:
                token = picked.split(".")[-1]
                self.show_file(
                    loc.file, loc.line, token, loc.line, focus_line=loc.line, focus_label=token, focus_signal=picked
                )

    def _select_wave_list_index(self, index: int) -> Optional[str]:
        if index < 0:
            return None
        if index >= self.wave_list.size():
            return None
        self.wave_list.selection_clear(0, tk.END)
        self.wave_list.selection_set(index)
        self.wave_list.activate(index)
        return self.wave_list.get(index)

    def on_wave_context_menu(self, event) -> None:
        idx = self.wave_list.nearest(event.y)
        name = self._select_wave_list_index(idx)
        if not name:
            return
        self._wave_ctx_signal = self.best_wave_to_design_match(name)
        state = tk.NORMAL if self._wave_ctx_signal else tk.DISABLED
        self.wave_ctx_menu.entryconfigure("Use as current signal", state=state)
        self.wave_ctx_menu.entryconfigure("Jump to source", state=state)
        self.wave_ctx_menu.entryconfigure("Show definition", state=state)
        self.wave_ctx_menu.entryconfigure("Add to external wave", state=state)
        self.wave_ctx_menu.tk_popup(event.x_root, event.y_root)

    def ctx_wave_set_signal(self) -> None:
        if not self._wave_ctx_signal:
            return
        self.signal_entry.delete(0, tk.END)
        self.signal_entry.insert(0, self._wave_ctx_signal)
        self.set_status(f"Current signal: {self._wave_ctx_signal}")

    def ctx_wave_jump_source(self) -> None:
        if not self._wave_ctx_signal:
            return
        self.signal_entry.delete(0, tk.END)
        self.signal_entry.insert(0, self._wave_ctx_signal)
        self.search_signal()
        loc = self.connectivity.signal_to_source.get(self._wave_ctx_signal)
        if not loc:
            return
        token = self._wave_ctx_signal.split(".")[-1]
        self.show_file(
            loc.file, loc.line, token, loc.line, focus_line=loc.line, focus_label=token, focus_signal=self._wave_ctx_signal
        )

    def ctx_wave_show_definition(self) -> None:
        if not self._wave_ctx_signal:
            return
        self.signal_entry.delete(0, tk.END)
        self.signal_entry.insert(0, self._wave_ctx_signal)
        self.show_signal_definition()

    def ctx_wave_add_to_wave(self) -> None:
        if not self._wave_ctx_signal:
            return
        try:
            ok = self.wave_bridge.add_signal(self._wave_ctx_signal)
        except WaveBridgeError as e:
            messagebox.showerror("RTLens", str(e))
            return
        if ok:
            self.set_status(f"Added to external wave: {self._wave_ctx_signal}")
        else:
            self.set_status("External wave viewer is disabled")

    def best_wave_to_design_match(self, wave_name: str) -> Optional[str]:
        wave_parts = wave_name.split(".")
        best_sig = None
        best_score = -1
        for sig in self.connectivity.signal_to_source.keys():
            sig_parts = sig.split(".")
            score = 0
            i = 1
            while i <= len(wave_parts) and i <= len(sig_parts):
                if wave_parts[-i] != sig_parts[-i]:
                    break
                score += 1
                i += 1
            if score == 0:
                continue
            if self.current_hier_path and sig.startswith(self.current_hier_path + "."):
                score += 1
            if score > best_score:
                best_score = score
                best_sig = sig
        return best_sig

    def _update_wave_range_label(self) -> None:
        c = (self.wave_view_t0 + self.wave_view_t1) // 2
        self.wave_range_var.set(
            f"t=[{self.wave_view_t0},{self.wave_view_t1}] width={self.wave_view_t1 - self.wave_view_t0} center={c}"
        )
        self.wave_center_var.set(str(c))

    def fit_wave(self) -> None:
        self.wave_view_t0 = self.wave_total_t0
        self.wave_view_t1 = self.wave_total_t1
        self._update_wave_range_label()
        self.redraw_wave()

    def zoom_wave(self, factor: float) -> None:
        if factor <= 0:
            return
        center = (self.wave_view_t0 + self.wave_view_t1) // 2
        span = max(1, self.wave_view_t1 - self.wave_view_t0)
        new_span = max(1, int(span * factor))
        t0 = center - new_span // 2
        t1 = t0 + new_span
        if t0 < self.wave_total_t0:
            t0 = self.wave_total_t0
            t1 = t0 + new_span
        if t1 > self.wave_total_t1:
            t1 = self.wave_total_t1
            t0 = max(self.wave_total_t0, t1 - new_span)
        if t1 <= t0:
            t1 = t0 + 1
        self.wave_view_t0 = t0
        self.wave_view_t1 = t1
        self._update_wave_range_label()
        self.redraw_wave()

    def shift_wave_center(self, ratio: float) -> None:
        span = max(1, self.wave_view_t1 - self.wave_view_t0)
        center = (self.wave_view_t0 + self.wave_view_t1) // 2
        center += int(span * ratio)
        t0 = center - span // 2
        t1 = t0 + span
        if t0 < self.wave_total_t0:
            t0 = self.wave_total_t0
            t1 = t0 + span
        if t1 > self.wave_total_t1:
            t1 = self.wave_total_t1
            t0 = max(self.wave_total_t0, t1 - span)
        self.wave_view_t0 = t0
        self.wave_view_t1 = max(t0 + 1, t1)
        self._update_wave_range_label()
        self.redraw_wave()

    def set_wave_center(self) -> None:
        try:
            c = int(self.wave_center_var.get().strip())
        except ValueError:
            self.set_status("Wave center must be integer time")
            return
        span = max(1, self.wave_view_t1 - self.wave_view_t0)
        t0 = c - span // 2
        t1 = t0 + span
        if t0 < self.wave_total_t0:
            t0 = self.wave_total_t0
            t1 = t0 + span
        if t1 > self.wave_total_t1:
            t1 = self.wave_total_t1
            t0 = max(self.wave_total_t0, t1 - span)
        self.wave_view_t0 = t0
        self.wave_view_t1 = max(t0 + 1, t1)
        self._update_wave_range_label()
        self.redraw_wave()

    def redraw_wave(self) -> None:
        self.draw_wave(self.current_wave_changes)

    def _format_wave_value(self, v: str) -> str:
        s = (v or "").strip()
        if not s:
            return s
        if len(s) == 1:
            return s.lower()
        if re.fullmatch(r"[01]+", s):
            return format(int(s, 2), "x")
        if re.fullmatch(r"[01xXzZ]+", s):
            # Unknowns present; keep compact binary
            return "b" + s.lower()
        return s

    def draw_wave(self, changes: List[Tuple[int, str]]) -> None:
        self.wave_canvas.delete("all")
        if not changes:
            self.wave_canvas.create_text(20, 20, text="No wave selected", fill="#7ef29a", anchor="w")
            return

        w = max(self.wave_canvas.winfo_width(), 600)
        h = max(self.wave_canvas.winfo_height(), 200)
        margin = 45
        self.wave_canvas.create_rectangle(0, 0, w, h, fill="#101820", outline="")
        t0 = self.wave_view_t0
        t1 = self.wave_view_t1 if self.wave_view_t1 > self.wave_view_t0 else self.wave_view_t0 + 1
        self._update_wave_range_label()
        self.wave_canvas.create_text(8, 12, text=self.current_wave_name, fill="#7ef29a", anchor="w")

        def x_of(t: int) -> float:
            return margin + (w - 2 * margin) * (t - t0) / (t1 - t0)
        # Determine if this is 1-bit like signal.
        onebit = True
        if "[" in self.current_wave_name and "]" in self.current_wave_name:
            onebit = False
        for _t, v in changes[: min(len(changes), 200)]:
            vv = v.strip()
            if len(vv) != 1 or vv not in {"0", "1", "x", "X", "z", "Z", "u", "U", "h", "H", "l", "L", "-"}:
                onebit = False
                break

        vis: List[Tuple[int, str]] = []
        prev = changes[0]
        for cur in changes:
            if cur[0] <= t0:
                prev = cur
                continue
            vis.append(cur)
        vis.insert(0, (t0, prev[1]))
        end_t = t1

        if onebit:
            y_hi = h * 0.30
            y_lo = h * 0.78
            y_xz = (y_hi + y_lo) / 2.0
            def y_of(v: str) -> float:
                if v in {"1", "h", "H"}:
                    return y_hi
                if v in {"0", "l", "L"}:
                    return y_lo
                return y_xz

            def color_of(v: str) -> str:
                if v in {"1", "h", "H", "0", "l", "L"}:
                    return "#7ef29a"
                return "#ffb86c"

            self.wave_canvas.create_text(10, y_hi, text="1", fill="#7ef29a", anchor="w")
            self.wave_canvas.create_text(10, y_lo, text="0", fill="#7ef29a", anchor="w")
            self.wave_canvas.create_text(12, y_xz, text="XZ", fill="#ffb86c", anchor="w")
            last_t, last_v = vis[0]
            last_y = y_of(last_v)
            for t, v in vis[1:]:
                if t < t0:
                    continue
                if t > t1:
                    break
                x0 = x_of(last_t)
                x1 = x_of(t)
                y = y_of(last_v)
                c = color_of(last_v)
                self.wave_canvas.create_line(x0, y, x1, y, fill=c, width=2)
                ny = y_of(v)
                c2 = color_of(v)
                self.wave_canvas.create_line(x1, y, x1, ny, fill=c2, width=2)
                last_t, last_v, last_y = t, v, ny
            self.wave_canvas.create_line(
                x_of(last_t), last_y, x_of(end_t), last_y, fill=color_of(last_v), width=2
            )
        else:
            y_top = h * 0.35
            y_bot = h * 0.72
            y_mid = (y_top + y_bot) / 2.0
            self.wave_canvas.create_line(margin, y_top, w - margin, y_top, fill="#3c5662", width=1)
            self.wave_canvas.create_line(margin, y_bot, w - margin, y_bot, fill="#3c5662", width=1)
            last_t, last_v = vis[0]
            for t, v in vis[1:]:
                if t < t0:
                    continue
                if t > t1:
                    break
                x0 = x_of(last_t)
                x1 = x_of(t)
                self.wave_canvas.create_rectangle(x0, y_top, x1, y_bot, outline="#7ef29a")
                if x1 - x0 > 36:
                    shown_raw = self._format_wave_value(last_v)
                    shown = shown_raw if len(shown_raw) <= 18 else (shown_raw[:16] + "..")
                    self.wave_canvas.create_text((x0 + x1) / 2.0, y_mid, text=shown, fill="#7ef29a")
                self.wave_canvas.create_line(x1, y_top, x1, y_bot, fill="#7ef29a", width=2)
                last_t, last_v = t, v
            x0 = x_of(last_t)
            x1 = x_of(end_t)
            self.wave_canvas.create_rectangle(x0, y_top, x1, y_bot, outline="#7ef29a")
            if x1 - x0 > 36:
                shown_raw = self._format_wave_value(last_v)
                shown = shown_raw if len(shown_raw) <= 18 else (shown_raw[:16] + "..")
                self.wave_canvas.create_text((x0 + x1) / 2.0, y_mid, text=shown, fill="#7ef29a")

    def open_external_editor(self) -> None:
        if not self.current_file:
            return

        line = self.current_line
        if self.source_text.focus_displayof() is not None:
            index = self.source_text.index(tk.INSERT)
            try:
                line = int(index.split(".")[0])
            except Exception:
                pass

        cmd_tpl = self.args.editor_cmd
        try:
            argv = build_editor_argv(cmd_tpl, self.current_file, line)
        except ValueError as e:
            messagebox.showerror("RTLens", f"invalid --editor-cmd template: {e}")
            return
        try:
            subprocess.Popen(argv, shell=False)
        except Exception as e:
            messagebox.showerror("RTLens", f"failed to start editor: {e}")

    def reload_rtl(self) -> bool:
        reloaded = False
        if self.loaded_filelist_paths:
            valid = [p for p in self.loaded_filelist_paths if os.path.isfile(p)]
            if valid:
                files, slang_args = self._read_multiple_filelists(valid)
                self._parse_files(files, slang_args)
                reloaded = True
        elif self.loaded_filelist_path and os.path.isfile(self.loaded_filelist_path):
            files, slang_args = self._read_multiple_filelists([self.loaded_filelist_path])
            self._parse_files(files, slang_args)
            reloaded = True
        elif self.loaded_dir_path and os.path.isdir(self.loaded_dir_path):
            self._parse_files(discover_sv_files(self.loaded_dir_path), [])
            reloaded = True
        elif self.args.dir and os.path.isdir(self.args.dir):
            self._parse_files(discover_sv_files(self.args.dir), [])
            reloaded = True
        else:
            arg_filelists = [p for p in self._arg_filelists() if os.path.isfile(p)]
            if arg_filelists:
                self.loaded_filelist_paths = arg_filelists
                self.loaded_filelist_path = arg_filelists[0]
                files, slang_args = self._read_multiple_filelists(arg_filelists)
                self._parse_files(files, slang_args)
                reloaded = True
        if reloaded:
            self.clear_trace_log()
            self.set_status("RTL reloaded")
        else:
            self.set_status("RTL reload skipped (no source target)")
        return reloaded

    def reload_wave(self) -> bool:
        wave = self.loaded_wave_path or self.args.wave
        reloaded = False
        if wave and os.path.isfile(wave):
            self.load_wave_file(wave)
            try:
                self.wave_bridge.reload()
            except WaveBridgeError:
                pass
            reloaded = True
        if reloaded:
            self.set_status("Wave reloaded")
        else:
            self.set_status("Wave reload skipped (no wave target)")
        return reloaded

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

    def run(self) -> None:
        arg_filelists = self._arg_filelists()
        valid_filelists = [p for p in arg_filelists if os.path.isfile(p)]
        if valid_filelists:
            self.loaded_filelist_paths = valid_filelists
            self.loaded_filelist_path = valid_filelists[0]
            files, slang_args = self._read_multiple_filelists(valid_filelists)
            self._parse_files(files, slang_args)
        elif self.args.dir and os.path.isdir(self.args.dir):
            self.loaded_dir_path = self.args.dir
            self._parse_files(discover_sv_files(self.args.dir), [])
        elif arg_filelists:
            self.set_status(f"filelist not found: {arg_filelists[0]}")

        if self.args.wave and os.path.isfile(self.args.wave):
            self.loaded_wave_path = self.args.wave
            self.load_wave_file(self.args.wave)

        self.root.after(250, self._poll_wave_bridge_events)
        # PanedWindow layout can race with WM map timing on some environments.
        self.root.after(80, lambda: self._setup_initial_layout(retries=8))
        self.root.after(600, lambda: self._setup_initial_layout(retries=2))
        self.root.mainloop()

    def _setup_initial_layout(self, retries: int = 0) -> None:
        # Keep hierarchy/source/wave panes visible even when map timing is late.
        try:
            w = max(self.root.winfo_width(), 1200)
            h = max(self.root.winfo_height(), 800)
            # Enforce visible hierarchy pane on the left / top.
            outer = max(420, int(w * 0.32))
            left_top = max(240, int(h * 0.40))
            right_top = max(300, int(h * 0.58))
            self.outer_pane.sashpos(0, outer)
            self.left_pane.sashpos(0, left_top)
            self.right_pane.sashpos(0, right_top)
        except Exception:
            if retries > 0:
                self.root.after(120, lambda: self._setup_initial_layout(retries=retries - 1))
