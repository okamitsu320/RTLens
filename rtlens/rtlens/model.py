"""Immutable data-model types shared across rtlens backend modules.

All types are plain ``dataclass`` containers — no business logic lives here.
They are populated by :mod:`rtlens.sv_parser` (regex fallback) or
:mod:`rtlens.slang_backend` (full elaboration) and consumed by
:mod:`rtlens.rtl_extract`, :mod:`rtlens.connectivity`, and the GUI layers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class SignalDef:
    """A signal or port declaration extracted from a module body.

    Attributes:
        name: Identifier as it appears in the source.
        line: 1-based source line number of the declaration.
        kind: Declaration keyword — e.g. ``"input"``, ``"output"``,
            ``"logic"``, ``"wire"``, ``"reg"``, or a user-defined type name.
    """

    name: str
    line: int
    kind: str


@dataclass
class PortDef:
    """A module port (direction-annotated signal at the module boundary).

    Attributes:
        name: Port identifier.
        direction: ``"input"``, ``"output"``, or ``"inout"``.
        line: 1-based source line number of the port declaration.
    """

    name: str
    direction: str
    line: int


@dataclass
class Assignment:
    """A continuous ``assign`` statement or initializer expression.

    Attributes:
        lhs: List of identifiers written on the left-hand side.
        rhs: List of identifiers read from the right-hand side.
        line: 1-based source line number.
        text: Original source text of the assignment (without the trailing
            semicolon), for display purposes.
    """

    lhs: List[str]
    rhs: List[str]
    line: int
    text: str = ""


@dataclass
class AlwaysBlock:
    """A parsed ``always`` / ``always_ff`` / ``always_comb`` / ``always_latch`` block.

    Attributes:
        kind: Procedure keyword: ``"always"``, ``"always_ff"``,
            ``"always_comb"``, ``"always_latch"``, or ``"initial"``.
        line_start: 1-based first line of the block.
        line_end: 1-based last line of the block.
        reads: Identifiers appearing on the right-hand side of assignments
            or in expressions inside the block.
        writes: Identifiers assigned (``=`` or ``<=``) inside the block.
        sensitivity_signals: Identifiers appearing in the event-control
            sensitivity list (for example ``always_ff @(posedge clk or
            negedge rst_n)``).
        clock_signals: Signals heuristically identified as clocks (names
            containing ``"clk"`` or ``"clock"`` in the sensitivity list).
        reset_signals: Signals heuristically identified as resets (names
            containing ``"rst"`` or ``"reset"`` anywhere in the block).
        stmt_summary: First few non-empty source lines, for tooltip display.
    """

    kind: str
    line_start: int
    line_end: int
    reads: List[str]
    writes: List[str]
    sensitivity_signals: List[str] = field(default_factory=list)
    clock_signals: List[str] = field(default_factory=list)
    reset_signals: List[str] = field(default_factory=list)
    stmt_summary: List[str] = field(default_factory=list)


@dataclass
class InstanceDef:
    """A module instantiation inside a parent module.

    Attributes:
        module_type: The instantiated module's name.
        name: The instance name (label).
        connections: Named port map — ``{port_name: connected_signal}``.
        line: 1-based source line of the instantiation statement.
        parameters: Named parameter override map — ``{param_name: value_expr}``.
        parameter_positional: Positional parameter values (when ``#(...)``
            uses positional rather than named association).
        positional: Positional port connections (when ``.(...)`` form is not
            used).
    """

    module_type: str
    name: str
    connections: Dict[str, str]
    line: int
    parameters: Dict[str, str] = field(default_factory=dict)
    parameter_positional: List[str] = field(default_factory=list)
    positional: List[str] = field(default_factory=list)


@dataclass
class ModuleDef:
    """All information extracted from a single ``module … endmodule`` block.

    Attributes:
        name: Module identifier.
        file: Absolute path to the source file.
        start_line: 1-based line of the ``module`` keyword.
        end_line: 1-based line of ``endmodule``.
        ports: Ordered dict of port name → :class:`PortDef`.
        signals: All declared signals (including ports) by name.
        instances: Sub-module instantiations in declaration order.
        assignments: Continuous ``assign`` statements and initializers.
        always_blocks: Procedural blocks in declaration order.
    """

    name: str
    file: str
    start_line: int
    end_line: int
    ports: Dict[str, PortDef] = field(default_factory=dict)
    signals: Dict[str, SignalDef] = field(default_factory=dict)
    instances: List[InstanceDef] = field(default_factory=list)
    assignments: List[Assignment] = field(default_factory=list)
    always_blocks: List[AlwaysBlock] = field(default_factory=list)


@dataclass
class SourceLoc:
    """A file + line reference into the original source.

    Attributes:
        file: Absolute path to the source file.
        line: 1-based line number.
    """

    file: str
    line: int


@dataclass
class HierNode:
    """A node in the elaborated design hierarchy (one per instance path).

    Attributes:
        path: Full dotted hierarchy path, e.g. ``"top.u_core.u_alu"``.
        module_name: The module definition instantiated at this node.
        inst_name: The local instance name (last component of *path*).
        parent: Parent hierarchy path, or ``None`` for a root node.
        children: Sorted list of child hierarchy paths.
    """

    path: str
    module_name: str
    inst_name: str
    parent: Optional[str]
    children: List[str] = field(default_factory=list)


@dataclass
class DesignDB:
    """Top-level container for a parsed / elaborated design.

    Populated by :func:`rtlens.sv_parser.parse_sv_files` or
    :func:`rtlens.slang_backend.load_design_with_slang`.

    Attributes:
        modules: Module definitions keyed by module name.
        roots: Sorted list of root hierarchy paths (typically one entry).
        hier: Hierarchy nodes keyed by dotted instance path.
        top_module: Module name of the first root, or ``None`` if unknown.
        callable_defs: Source location of each callable, keyed by
            ``"<kind>:<name>"`` (e.g. ``"function:top.u_core.alu_op"``).
        callable_kinds: Kind string (``"module"``, ``"function"``, ``"task"``)
            for each callable key.
        callable_names: Short (local) name for each callable key.
        callable_name_index: Maps short name → set of fully-qualified keys.
        callable_refs: Call / instantiation sites for each callable key.
        callable_ref_sites: Maps ``(file, line, token)`` → candidate keys,
            used for go-to-definition from cursor position.
        callable_def_sites: Maps ``(file, line, name)`` → keys defined there.
    """

    modules: Dict[str, ModuleDef] = field(default_factory=dict)
    roots: List[str] = field(default_factory=list)
    hier: Dict[str, HierNode] = field(default_factory=dict)
    top_module: Optional[str] = None
    # key: "module:<name>" | "function:<hier.path>" | "task:<hier.path>"
    callable_defs: Dict[str, SourceLoc] = field(default_factory=dict)
    callable_kinds: Dict[str, str] = field(default_factory=dict)
    callable_names: Dict[str, str] = field(default_factory=dict)
    callable_name_index: Dict[str, Set[str]] = field(default_factory=dict)
    callable_refs: Dict[str, List[SourceLoc]] = field(default_factory=dict)
    # (file, line, token) -> candidate keys
    callable_ref_sites: Dict[Tuple[str, int, str], List[str]] = field(default_factory=dict)
    callable_def_sites: Dict[Tuple[str, int, str], List[str]] = field(default_factory=dict)


@dataclass
class SignalQueryResult:
    """Result of a signal connectivity query.

    Attributes:
        signal: The queried signal name (hierarchy-qualified).
        drivers: Source locations of assignments that drive this signal.
        loads: Source locations where this signal is read.
    """

    signal: str
    drivers: List[SourceLoc]
    loads: List[SourceLoc]


@dataclass
class ConnectivityDB:
    """Signal-level driver/load graph built from elaboration data.

    Populated by :func:`rtlens.slang_backend.load_design_with_slang`.
    All signal names are hierarchy-qualified (e.g. ``"top.u_core.carry"``).

    Attributes:
        drives_data: Data-flow edges: ``{src_signal: {dst_signal, …}}``.
        drives_control: Control-flow edges (e.g. enable, select signals).
        drives_clock: Clock-flow edges from event-control clocks to procedural
            assignments (clock-only dependency graph).
        alias_edges: Port-alias edges introduced by port connections.
        signal_to_source: Maps each signal to its declaration location.
        driver_sites: Non-port assignment locations per signal.
        driver_sites_port: Port-driven assignment locations per signal.
        load_sites_data: Data-read locations per signal.
        load_sites_port: Port-read locations per signal.
        load_sites_control: Control-read locations per signal.
        load_sites_clock: Clock-read locations from event controls.
    """
    # path.signal -> list(path.signal)
    drives_data: Dict[str, Set[str]] = field(default_factory=dict)
    drives_control: Dict[str, Set[str]] = field(default_factory=dict)
    drives_clock: Dict[str, Set[str]] = field(default_factory=dict)
    alias_edges: Dict[str, Set[str]] = field(default_factory=dict)
    signal_to_source: Dict[str, SourceLoc] = field(default_factory=dict)
    driver_sites: Dict[str, List[SourceLoc]] = field(default_factory=dict)
    driver_sites_port: Dict[str, List[SourceLoc]] = field(default_factory=dict)
    load_sites_data: Dict[str, List[SourceLoc]] = field(default_factory=dict)
    load_sites_port: Dict[str, List[SourceLoc]] = field(default_factory=dict)
    load_sites_control: Dict[str, List[SourceLoc]] = field(default_factory=dict)
    load_sites_clock: Dict[str, List[SourceLoc]] = field(default_factory=dict)

    def add_edge(self, src: str, dst: str, kind: str = "data") -> None:
        if kind == "control":
            table = self.drives_control
        elif kind == "clock":
            table = self.drives_clock
        else:
            table = self.drives_data
        table.setdefault(src, set()).add(dst)
        table.setdefault(dst, set())
        # Keep keys visible in all maps for consistent traversal.
        self.drives_data.setdefault(src, set())
        self.drives_data.setdefault(dst, set())
        self.drives_control.setdefault(src, set())
        self.drives_control.setdefault(dst, set())
        self.drives_clock.setdefault(src, set())
        self.drives_clock.setdefault(dst, set())
        self.alias_edges.setdefault(src, set())
        self.alias_edges.setdefault(dst, set())

    def add_alias(self, a: str, b: str) -> None:
        self.alias_edges.setdefault(a, set()).add(b)
        self.alias_edges.setdefault(b, set()).add(a)

    def add_driver_site(self, sig: str, loc: SourceLoc) -> None:
        self.driver_sites.setdefault(sig, []).append(loc)

    def add_driver_site_port(self, sig: str, loc: SourceLoc) -> None:
        self.driver_sites_port.setdefault(sig, []).append(loc)

    def add_load_site(self, sig: str, loc: SourceLoc, kind: str = "data") -> None:
        if kind == "control":
            table = self.load_sites_control
        elif kind == "clock":
            table = self.load_sites_clock
        elif kind == "port":
            table = self.load_sites_port
        else:
            table = self.load_sites_data
        table.setdefault(sig, []).append(loc)
