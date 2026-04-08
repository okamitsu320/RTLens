from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Dict

from .model import DesignDB
from .rtl_dot import rtl_ir_to_dot
from .rtl_elk import elk_benchmark_variants, rtl_ir_to_elk_graph
from .rtl_elk_render import benchmark_elk_layouts, render_elk_layout
from .rtl_extract import extract_module_structure
from .rtl_graphviz import render_dot_to_cmapx, render_dot_to_png, render_dot_to_svg
from .rtl_ir import (
    IRAlwaysBlockNode,
    IRAssignBlockNode,
    IRCallableBlockNode,
    IRInstanceNode,
    IRInstancePort,
    IRModulePortNode,
    RTLStructureView,
)
from .rtl_ir_builder import build_rtl_structure_ir


@dataclass
class RTLStructureRender:
    dot: str
    png: bytes
    svg: str
    cmapx: str


@dataclass
class RTLElkPreparation:
    graph: Dict[str, Any]
    stats: Dict[str, Any]


def build_rtl_structure_view(design: DesignDB, hier_path: str) -> RTLStructureView:
    extracted = extract_module_structure(design, hier_path)
    return build_rtl_structure_ir(extracted)


def _simplify_view_for_elk(view: RTLStructureView) -> RTLStructureView:
    seed_types = {"module_port", "instance"}
    node_map = {n.id: n for n in view.nodes}
    signal_name_map = {s.id: s.name for s in view.signals}
    seed_ids = {n.id for n in view.nodes if n.type in seed_types}
    keep_ids = set(seed_ids)
    complexity = len(view.nodes) + len(view.edges)
    large_mode = complexity > 500

    def _touches_module_port(node_id: str) -> bool:
        for edge in view.edges:
            if edge.from_ep.node_id == node_id:
                other = edge.to_ep.node_id
            elif edge.to_ep.node_id == node_id:
                other = edge.from_ep.node_id
            else:
                continue
            other_node = node_map.get(other)
            if other_node and other_node.type == "module_port":
                return True
        return False

    def _has_struct_member_signal(node_id: str) -> bool:
        for edge in view.edges:
            if edge.from_ep.node_id != node_id and edge.to_ep.node_id != node_id:
                continue
            if "." in signal_name_map.get(edge.signal_id, ""):
                return True
        return False

    def _logic_block_is_important(node_id: str) -> bool:
        if not large_mode:
            return True
        return _touches_module_port(node_id) or _has_struct_member_signal(node_id)

    # Keep only one-hop logic blocks that directly connect to visible module ports
    # or instances. Do not recurse through logic blocks, otherwise large top-level
    # modules quickly expand back to the full graph and ELK loses the benefit of
    # simplification.
    #
    # Retention policy:
    # - logic that CONSUMES a seed output is always kept; otherwise instance/module
    #   outputs look broken in the simplified top-level view.
    # - logic that DRIVES a seed input is only kept when it is "important"
    #   (struct/member handling, module-port related paths, etc.), which keeps
    #   top-level complexity under control.
    for edge in view.edges:
        a = edge.from_ep.node_id
        b = edge.to_ep.node_id
        a_node = node_map.get(a)
        b_node = node_map.get(b)
        if (
            a in seed_ids
            and b not in keep_ids
            and b_node
            and b_node.type in {"assign_block", "always_block", "callable_block"}
        ):
            if edge.from_ep.role == "output":
                keep_ids.add(b)
            elif _logic_block_is_important(b):
                keep_ids.add(b)
        if (
            b in seed_ids
            and a not in keep_ids
            and a_node
            and a_node.type in {"assign_block", "always_block", "callable_block"}
        ):
            if edge.to_ep.role == "input" and _logic_block_is_important(a):
                keep_ids.add(a)

    # One additional hop:
    # if a logic block is already kept, keep the immediate consumer of its outputs.
    # This avoids showing a kept assign/always block with a dangling output even
    # though the very next consumer still exists in the original graph.
    for edge in view.edges:
        a = edge.from_ep.node_id
        b = edge.to_ep.node_id
        if (
            a in keep_ids
            and a not in seed_ids
            and node_map.get(a)
            and node_map[a].type in {"assign_block", "always_block", "callable_block"}
            and edge.from_ep.role == "output"
            and b not in keep_ids
            and node_map.get(b)
            and node_map[b].type in {"assign_block", "always_block", "callable_block", "instance", "module_port"}
        ):
            keep_ids.add(b)

    kept_edges = [e for e in view.edges if e.from_ep.node_id in keep_ids and e.to_ep.node_id in keep_ids]
    used_signal_ids = {e.signal_id for e in kept_edges}
    dangling_output_signal_ids: set[str] = set()
    node_used_inputs: dict[str, set[str]] = {}
    node_used_outputs: dict[str, set[str]] = {}
    for edge in kept_edges:
        node_used_outputs.setdefault(edge.from_ep.node_id, set()).add(edge.signal_id)
        node_used_inputs.setdefault(edge.to_ep.node_id, set()).add(edge.signal_id)
    kept_signals = [s for s in view.signals if s.id in used_signal_ids]

    kept_nodes = []
    for node in view.nodes:
        if node.id not in keep_ids:
            continue
        if isinstance(node, IRModulePortNode):
            sigs = [sig for sig in node.signal_ids if sig in used_signal_ids]
            if not sigs:
                continue
            kept_nodes.append(
                IRModulePortNode(
                    id=node.id,
                    name=node.name,
                    label=node.label,
                    direction=node.direction,
                    signal_ids=sigs,
                    source=node.source,
                    layout_hint=node.layout_hint,
                )
            )
            continue
        if isinstance(node, IRInstanceNode):
            ports = []
            for port in node.ports:
                dangling_kind = str(getattr(port, "dangling_kind", ""))
                sigs = [sig for sig in port.signal_ids if sig in used_signal_ids]
                if not sigs and port.direction in {"output", "inout", "unknown"}:
                    sigs = list(port.signal_ids)
                    for sig in sigs:
                        dangling_output_signal_ids.add(sig)
                if not sigs and not dangling_kind:
                    continue
                ports.append(
                    IRInstancePort(
                        name=port.name,
                        direction=port.direction,
                        signal_ids=sigs,
                        dangling_kind=dangling_kind,
                        expr=str(getattr(port, "expr", "")),
                    )
                )
            if not ports:
                continue
            kept_nodes.append(
                IRInstanceNode(
                    id=node.id,
                    name=node.name,
                    label=node.label,
                    module_name=node.module_name,
                    parameters=node.parameters,
                    parameter_positional=node.parameter_positional,
                    ports=ports,
                    source=node.source,
                    layout_hint=node.layout_hint,
                )
            )
            continue
        if isinstance(node, IRAssignBlockNode):
            ins = [sig for sig in node.input_signals if sig in node_used_inputs.get(node.id, set())]
            used_outs = set(node_used_outputs.get(node.id, set()))
            outs = [sig for sig in node.output_signals if sig in used_outs]
            for sig in node.output_signals:
                if sig not in used_outs:
                    outs.append(sig)
                    dangling_output_signal_ids.add(sig)
            if not ins and not outs:
                continue
            kept_nodes.append(
                IRAssignBlockNode(
                    id=node.id,
                    name=node.name,
                    label=node.label,
                    input_signals=ins,
                    output_signals=outs,
                    expr_summary=node.expr_summary,
                    source=node.source,
                    layout_hint=node.layout_hint,
                )
            )
            continue
        if isinstance(node, IRCallableBlockNode):
            ins = [sig for sig in node.input_signals if sig in node_used_inputs.get(node.id, set())]
            used_outs = set(node_used_outputs.get(node.id, set()))
            outs = [sig for sig in node.output_signals if sig in used_outs]
            for sig in node.output_signals:
                if sig not in used_outs:
                    outs.append(sig)
                    dangling_output_signal_ids.add(sig)
            if not ins and not outs:
                continue
            kept_nodes.append(
                IRCallableBlockNode(
                    id=node.id,
                    name=node.name,
                    label=node.label,
                    callable_kind=node.callable_kind,
                    callable_key=node.callable_key,
                    input_signals=ins,
                    output_signals=outs,
                    stmt_summary=node.stmt_summary,
                    source=node.source,
                    layout_hint=node.layout_hint,
                )
            )
            continue
        if isinstance(node, IRAlwaysBlockNode):
            ins = [sig for sig in node.input_signals if sig in node_used_inputs.get(node.id, set())]
            used_outs = set(node_used_outputs.get(node.id, set()))
            outs = [sig for sig in node.output_signals if sig in used_outs]
            for sig in node.output_signals:
                if sig not in used_outs:
                    outs.append(sig)
                    dangling_output_signal_ids.add(sig)
            clks = [sig for sig in node.clock_signals if sig in used_signal_ids]
            rsts = [sig for sig in node.reset_signals if sig in used_signal_ids]
            if not ins and not outs and not clks and not rsts:
                continue
            kept_nodes.append(
                IRAlwaysBlockNode(
                    id=node.id,
                    name=node.name,
                    label=node.label,
                    always_kind=node.always_kind,
                    input_signals=ins,
                    output_signals=outs,
                    clock_signals=clks,
                    reset_signals=rsts,
                    stmt_summary=node.stmt_summary,
                    source=node.source,
                    layout_hint=node.layout_hint,
                )
            )
            continue
        kept_nodes.append(node)

    kept_signals = [s for s in view.signals if s.id in used_signal_ids or s.id in dangling_output_signal_ids]

    return RTLStructureView(
        schema_version=view.schema_version,
        view_type=view.view_type,
        module=view.module,
        signals=kept_signals,
        nodes=kept_nodes,
        edges=kept_edges,
    )


def _collect_signal_ids_for_node(node: object) -> set[str]:
    ids: set[str] = set()
    if isinstance(node, IRModulePortNode):
        ids.update(node.signal_ids)
    elif isinstance(node, IRInstanceNode):
        for port in node.ports:
            ids.update(port.signal_ids)
    elif isinstance(node, IRAssignBlockNode):
        ids.update(node.input_signals)
        ids.update(node.output_signals)
    elif isinstance(node, IRAlwaysBlockNode):
        ids.update(node.input_signals)
        ids.update(node.output_signals)
        ids.update(node.clock_signals)
        ids.update(node.reset_signals)
    elif isinstance(node, IRCallableBlockNode):
        ids.update(node.input_signals)
        ids.update(node.output_signals)
    return ids


def _trim_view_to_node_ids(view: RTLStructureView, keep_node_ids: set[str]) -> RTLStructureView:
    kept_nodes = [node for node in view.nodes if node.id in keep_node_ids]
    kept_edges = [
        edge for edge in view.edges if edge.from_ep.node_id in keep_node_ids and edge.to_ep.node_id in keep_node_ids
    ]

    used_signal_ids = {edge.signal_id for edge in kept_edges}
    for node in kept_nodes:
        used_signal_ids.update(_collect_signal_ids_for_node(node))

    kept_signals = [sig for sig in view.signals if sig.id in used_signal_ids]
    return RTLStructureView(
        schema_version=view.schema_version,
        view_type=view.view_type,
        module=view.module,
        signals=kept_signals,
        nodes=kept_nodes,
        edges=kept_edges,
    )


def _normalize_rtl_mode(mode: str) -> str:
    return "detailed" if mode == "detailed" else "auto"


def _select_rtl_view_for_mode(view: RTLStructureView, mode: str) -> RTLStructureView:
    normalized_mode = _normalize_rtl_mode(mode)
    if normalized_mode == "detailed":
        return view

    keep_node_ids: set[str] = set()
    for node in view.nodes:
        if not isinstance(node, IRCallableBlockNode):
            keep_node_ids.add(node.id)
            continue
        is_task = node.callable_kind == "task"
        has_output_assignment = bool(node.output_signals)
        if is_task or has_output_assignment:
            keep_node_ids.add(node.id)

    if len(keep_node_ids) == len(view.nodes):
        return view
    return _trim_view_to_node_ids(view, keep_node_ids)


def estimate_rtl_structure_timeout(
    design: DesignDB,
    hier_path: str,
    base_timeout: int = 240,
) -> int:
    view = build_rtl_structure_view(design, hier_path)
    complexity = len(view.nodes) + len(view.edges)
    if complexity <= 80:
        return max(4, base_timeout)
    if complexity <= 180:
        return max(base_timeout, 12)
    if complexity <= 320:
        return max(base_timeout, 30)
    if complexity <= 520:
        return max(base_timeout, 60)
    return max(base_timeout, 240)


def _resolve_fast_layout(mode: str, node_count: int, edge_count: int) -> bool:
    complexity = node_count + edge_count
    if mode == "detailed":
        return False
    return complexity > 260


def _resolve_runtime_variant(mode: str, node_count: int, edge_count: int) -> str:
    if _resolve_fast_layout(mode, node_count, edge_count):
        return "ortho_fast_fixed"
    return "strict_ortho_fixed"


def _prepare_rtl_structure_elk_graph(
    design: DesignDB,
    hier_path: str,
    mode: str = "auto",
    fast_layout: bool = False,
) -> RTLElkPreparation:
    t0 = perf_counter()
    full_view = build_rtl_structure_view(design, hier_path)
    t1 = perf_counter()

    full_nodes = len(full_view.nodes)
    full_edges = len(full_view.edges)
    full_signals = len(full_view.signals)
    full_callables = sum(1 for node in full_view.nodes if isinstance(node, IRCallableBlockNode))

    selected_mode = _normalize_rtl_mode(mode)
    selected_view = _select_rtl_view_for_mode(full_view, selected_mode)
    selected_callables = sum(1 for node in selected_view.nodes if isinstance(node, IRCallableBlockNode))

    t2 = perf_counter()
    graph = rtl_ir_to_elk_graph(selected_view, fast_layout=fast_layout)
    t3 = perf_counter()

    stats = {
        "requested_mode": mode,
        "effective_mode": selected_mode,
        "fast_layout": fast_layout,
        "runtime_variant": _resolve_runtime_variant(mode, len(selected_view.nodes), len(selected_view.edges)),
        "full_nodes": full_nodes,
        "full_edges": full_edges,
        "full_signals": full_signals,
        "full_callables": full_callables,
        "selected_nodes": len(selected_view.nodes),
        "selected_edges": len(selected_view.edges),
        "selected_signals": len(selected_view.signals),
        "selected_callables": selected_callables,
        "filtered_callables": max(0, full_callables - selected_callables),
        "graph_children": len(graph.get("children", [])),
        "graph_edges": len(graph.get("edges", [])),
        "timing_build_view_ms": round((t1 - t0) * 1000, 1),
        "timing_select_view_ms": round((t2 - t1) * 1000, 1),
        "timing_build_graph_ms": round((t3 - t2) * 1000, 1),
    }
    return RTLElkPreparation(graph=graph, stats=stats)


def build_rtl_structure_dict(design: DesignDB, hier_path: str) -> Dict[str, Any]:
    return build_rtl_structure_view(design, hier_path).to_dict()


def build_rtl_structure_elk_graph(design: DesignDB, hier_path: str, mode: str = "auto") -> Dict[str, Any]:
    view = build_rtl_structure_view(design, hier_path)
    selected = _select_rtl_view_for_mode(view, mode)
    fast_layout = _resolve_fast_layout(mode, len(selected.nodes), len(selected.edges))
    return _prepare_rtl_structure_elk_graph(design, hier_path, mode=mode, fast_layout=fast_layout).graph


def profile_rtl_structure_elk_graph(design: DesignDB, hier_path: str, mode: str = "auto") -> Dict[str, Any]:
    view = build_rtl_structure_view(design, hier_path)
    selected = _select_rtl_view_for_mode(view, mode)
    fast_layout = _resolve_fast_layout(mode, len(selected.nodes), len(selected.edges))
    return _prepare_rtl_structure_elk_graph(design, hier_path, mode=mode, fast_layout=fast_layout).stats


def benchmark_rtl_structure_elk_graph(
    design: DesignDB,
    hier_path: str,
    mode: str = "auto",
    node_cmd: str = "node",
    timeout: int = 8,
) -> Dict[str, Any]:
    prep = _prepare_rtl_structure_elk_graph(design, hier_path, mode=mode, fast_layout=False)
    variants = elk_benchmark_variants(prep.graph)
    runtime_fast_layout = _resolve_fast_layout(
        mode,
        int(prep.stats.get("selected_nodes", 0)),
        int(prep.stats.get("selected_edges", 0)),
    )
    runtime_variant = _resolve_runtime_variant(
        mode,
        int(prep.stats.get("selected_nodes", 0)),
        int(prep.stats.get("selected_edges", 0)),
    )
    return {
        "stats": {
            **prep.stats,
            "runtime_fast_layout": runtime_fast_layout,
            "runtime_variant": runtime_variant,
            "benchmark_uses_full_graph": True,
        },
        "results": benchmark_elk_layouts(variants, node_cmd=node_cmd, timeout=timeout),
    }


def build_rtl_structure_elk_layout(
    design: DesignDB,
    hier_path: str,
    node_cmd: str = "node",
    timeout: int = 8,
    mode: str = "auto",
) -> Dict[str, Any]:
    return render_elk_layout(build_rtl_structure_elk_graph(design, hier_path, mode=mode), node_cmd=node_cmd, timeout=timeout)


def build_rtl_structure_dot(design: DesignDB, hier_path: str) -> str:
    return rtl_ir_to_dot(build_rtl_structure_view(design, hier_path))


def build_rtl_structure_svg(
    design: DesignDB,
    hier_path: str,
    dot_cmd: str = "dot",
    timeout: int = 8,
) -> str:
    return render_dot_to_svg(build_rtl_structure_dot(design, hier_path), dot_cmd=dot_cmd, timeout=timeout)


def build_rtl_structure_png(
    design: DesignDB,
    hier_path: str,
    dot_cmd: str = "dot",
    timeout: int = 8,
) -> bytes:
    return render_dot_to_png(build_rtl_structure_dot(design, hier_path), dot_cmd=dot_cmd, timeout=timeout)


def build_rtl_structure_render(
    design: DesignDB,
    hier_path: str,
    dot_cmd: str = "dot",
    timeout: int = 8,
) -> RTLStructureRender:
    dot = build_rtl_structure_dot(design, hier_path)
    return RTLStructureRender(
        dot=dot,
        png=render_dot_to_png(dot, dot_cmd=dot_cmd, timeout=timeout),
        svg=render_dot_to_svg(dot, dot_cmd=dot_cmd, timeout=timeout),
        cmapx=render_dot_to_cmapx(dot, dot_cmd=dot_cmd, timeout=timeout),
    )
