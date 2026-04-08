from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Set

from .model import DesignDB
from .rtl_elk import rtl_ir_to_elk_graph
from .rtl_elk_render import render_elk_layout
from .rtl_extract import extract_module_structure
from .rtl_ir import IRAlwaysBlockNode, IRAssignBlockNode, IRCallableBlockNode, IRInstanceNode
from .rtl_ir_builder import build_rtl_structure_ir

ENV_RTL_DEBUG = "SVVIEW_RTL_DEBUG"
ENV_RTL_DEBUG_DIR = "SVVIEW_RTL_DEBUG_DIR"


def is_rtl_debug_enabled() -> bool:
    return os.environ.get(ENV_RTL_DEBUG, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_fast_layout(mode: str, node_count: int, edge_count: int) -> bool:
    if mode == "detailed":
        return False
    return (node_count + edge_count) > 260


def _resolve_runtime_variant(mode: str, node_count: int, edge_count: int) -> str:
    if _resolve_fast_layout(mode, node_count, edge_count):
        return "ortho_fast_fixed"
    return "strict_ortho_fixed"


def _safe_name(value: str, max_len: int = 96) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in {"_", "-", "."}) else "_" for ch in value)
    cleaned = cleaned.strip("._") or "unknown"
    return cleaned[:max_len]


def _default_debug_root() -> Path:
    import tempfile
    env = os.environ.get(ENV_RTL_DEBUG_DIR, "").strip()
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "rtlens_rtl_debug"


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not json serializable: {type(value)!r}")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True, default=_json_default)
        f.write("\n")


def _role_signals_from_ports(ports: list[Any], role: str) -> Set[str]:
    out: Set[str] = set()
    for port in ports:
        direction = getattr(port, "direction", "unknown")
        signal_ids = list(getattr(port, "signal_ids", []) or [])
        if role == "input" and direction in {"input", "inout", "unknown"}:
            out.update(signal_ids)
        if role == "output" and direction in {"output", "inout", "unknown"}:
            out.update(signal_ids)
    return out


def _connected_signals(view) -> tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    connected_in: Dict[str, Set[str]] = {}
    connected_out: Dict[str, Set[str]] = {}
    for edge in view.edges:
        connected_out.setdefault(edge.from_ep.node_id, set()).add(edge.signal_id)
        connected_in.setdefault(edge.to_ep.node_id, set()).add(edge.signal_id)
    return connected_in, connected_out


def _summarize_mismatches(extracted, view) -> Dict[str, Any]:
    connected_in, connected_out = _connected_signals(view)
    incoming_map, _ = _build_edge_maps(view)
    signal_names = {sig.id: sig.name for sig in view.signals}

    ir_instances = {node.id: node for node in view.nodes if isinstance(node, IRInstanceNode)}
    ir_assigns = {node.id: node for node in view.nodes if isinstance(node, IRAssignBlockNode)}
    ir_callables = {node.id: node for node in view.nodes if isinstance(node, IRCallableBlockNode)}
    ir_always = {node.id: node for node in view.nodes if isinstance(node, IRAlwaysBlockNode)}
    ir_assign_labels = {node.id: node.label for node in view.nodes if isinstance(node, IRAssignBlockNode)}

    instance_anomalies: List[Dict[str, Any]] = []
    declared_lanes = {"input": 0, "output": 0}
    emitted_lanes = {"input": 0, "output": 0}
    connected_lanes = {"input": 0, "output": 0}
    expr_cov = {
        "total": 0,
        "const_expr": 0,
        "signal_expr": 0,
        "connected": 0,
        "via_expr_assign": 0,
    }
    expr_anomalies: List[Dict[str, Any]] = []

    for inst in extracted.instances:
        expected_in = _role_signals_from_ports(inst.ports, "input")
        expected_out = _role_signals_from_ports(inst.ports, "output")
        declared_lanes["input"] += len(expected_in)
        declared_lanes["output"] += len(expected_out)

        ir_inst = ir_instances.get(inst.id)
        emitted_in: Set[str] = set()
        emitted_out: Set[str] = set()
        if ir_inst:
            emitted_in = _role_signals_from_ports(ir_inst.ports, "input")
            emitted_out = _role_signals_from_ports(ir_inst.ports, "output")
        emitted_lanes["input"] += len(emitted_in)
        emitted_lanes["output"] += len(emitted_out)

        conn_in = set(connected_in.get(inst.id, set()))
        conn_out = set(connected_out.get(inst.id, set()))
        connected_lanes["input"] += len(conn_in)
        connected_lanes["output"] += len(conn_out)

        missing_emit_in = sorted(expected_in - emitted_in)
        missing_emit_out = sorted(expected_out - emitted_out)
        missing_conn_in = sorted(expected_in - conn_in)
        missing_conn_out = sorted(expected_out - conn_out)
        extra_conn_in = sorted(conn_in - expected_in)
        extra_conn_out = sorted(conn_out - expected_out)

        if missing_emit_in or missing_emit_out or missing_conn_in or missing_conn_out or extra_conn_in or extra_conn_out:
            instance_anomalies.append(
                {
                    "id": inst.id,
                    "label": inst.label,
                    "module": inst.module_name,
                    "missing_emitted_input": [signal_names.get(sig, sig) for sig in missing_emit_in],
                    "missing_emitted_output": [signal_names.get(sig, sig) for sig in missing_emit_out],
                    "missing_connected_input": [signal_names.get(sig, sig) for sig in missing_conn_in],
                    "missing_connected_output": [signal_names.get(sig, sig) for sig in missing_conn_out],
                    "extra_connected_input": [signal_names.get(sig, sig) for sig in extra_conn_in],
                    "extra_connected_output": [signal_names.get(sig, sig) for sig in extra_conn_out],
                }
            )

        for port in inst.ports:
            expr = str(getattr(port, "expr", "") or "").strip()
            direction = str(getattr(port, "direction", "unknown"))
            if not expr or direction not in {"input", "inout", "unknown"}:
                continue
            expr_cov["total"] += 1
            sig_ids = list(getattr(port, "signal_ids", []) or [])
            dangling_kind = str(getattr(port, "dangling_kind", ""))
            if dangling_kind == "const_expr_input" and not sig_ids:
                expr_cov["const_expr"] += 1
                continue

            expr_cov["signal_expr"] += 1
            is_connected = bool(sig_ids) and all(sig in conn_in for sig in sig_ids)
            if is_connected:
                expr_cov["connected"] += 1

            has_expr_assign = False
            if sig_ids:
                for edge in incoming_map.get(inst.id, []):
                    if edge.signal_id not in sig_ids:
                        continue
                    src_id = edge.from_ep.node_id
                    src_label = ir_assign_labels.get(src_id, "")
                    src_sig_name = signal_names.get(edge.signal_id, "")
                    if src_label.startswith("expr_") or src_sig_name.startswith("expr_"):
                        has_expr_assign = True
                        break
            if has_expr_assign:
                expr_cov["via_expr_assign"] += 1

            if not is_connected:
                expr_anomalies.append(
                    {
                        "instance": inst.label,
                        "instance_id": inst.id,
                        "port": port.name,
                        "expr": expr,
                        "reason": "missing_connected_input",
                        "signal_names": [signal_names.get(sig, sig) for sig in sig_ids],
                    }
                )
            elif not has_expr_assign and any(signal_names.get(sig, "").startswith("expr_") for sig in sig_ids):
                expr_anomalies.append(
                    {
                        "instance": inst.label,
                        "instance_id": inst.id,
                        "port": port.name,
                        "expr": expr,
                        "reason": "expr_signal_without_expr_assign_producer",
                        "signal_names": [signal_names.get(sig, sig) for sig in sig_ids],
                    }
                )

    block_anomalies: List[Dict[str, Any]] = []
    block_totals = {
        "assign": {"expected_input": 0, "expected_output": 0, "emitted_input": 0, "emitted_output": 0},
        "callable": {"expected_input": 0, "expected_output": 0, "emitted_input": 0, "emitted_output": 0},
        "always": {"expected_input": 0, "expected_output": 0, "emitted_input": 0, "emitted_output": 0},
    }

    for assign in extracted.assigns:
        expected_in = set(assign.input_signals)
        expected_out = set(assign.output_signals)
        emitted_in = set(ir_assigns.get(assign.id).input_signals if assign.id in ir_assigns else [])
        emitted_out = set(ir_assigns.get(assign.id).output_signals if assign.id in ir_assigns else [])
        conn_in = set(connected_in.get(assign.id, set()))
        conn_out = set(connected_out.get(assign.id, set()))
        block_totals["assign"]["expected_input"] += len(expected_in)
        block_totals["assign"]["expected_output"] += len(expected_out)
        block_totals["assign"]["emitted_input"] += len(emitted_in)
        block_totals["assign"]["emitted_output"] += len(emitted_out)
        missing_emit_in = sorted(expected_in - emitted_in)
        missing_emit_out = sorted(expected_out - emitted_out)
        missing_conn_in = sorted(expected_in - conn_in)
        missing_conn_out = sorted(expected_out - conn_out)
        if missing_emit_in or missing_emit_out or missing_conn_in or missing_conn_out:
            block_anomalies.append(
                {
                    "kind": "assign",
                    "id": assign.id,
                    "label": assign.label,
                    "missing_emitted_input": [signal_names.get(sig, sig) for sig in missing_emit_in],
                    "missing_emitted_output": [signal_names.get(sig, sig) for sig in missing_emit_out],
                    "missing_connected_input": [signal_names.get(sig, sig) for sig in missing_conn_in],
                    "missing_connected_output": [signal_names.get(sig, sig) for sig in missing_conn_out],
                }
            )

    for callable_ref in extracted.callables:
        expected_in = set(callable_ref.input_signals)
        expected_out = set(callable_ref.output_signals)
        emitted_in = set(ir_callables.get(callable_ref.id).input_signals if callable_ref.id in ir_callables else [])
        emitted_out = set(ir_callables.get(callable_ref.id).output_signals if callable_ref.id in ir_callables else [])
        conn_in = set(connected_in.get(callable_ref.id, set()))
        conn_out = set(connected_out.get(callable_ref.id, set()))
        block_totals["callable"]["expected_input"] += len(expected_in)
        block_totals["callable"]["expected_output"] += len(expected_out)
        block_totals["callable"]["emitted_input"] += len(emitted_in)
        block_totals["callable"]["emitted_output"] += len(emitted_out)
        missing_emit_in = sorted(expected_in - emitted_in)
        missing_emit_out = sorted(expected_out - emitted_out)
        missing_conn_in = sorted(expected_in - conn_in)
        missing_conn_out = sorted(expected_out - conn_out)
        if missing_emit_in or missing_emit_out or missing_conn_in or missing_conn_out:
            block_anomalies.append(
                {
                    "kind": "callable",
                    "id": callable_ref.id,
                    "label": callable_ref.label,
                    "callable_kind": callable_ref.callable_kind,
                    "missing_emitted_input": [signal_names.get(sig, sig) for sig in missing_emit_in],
                    "missing_emitted_output": [signal_names.get(sig, sig) for sig in missing_emit_out],
                    "missing_connected_input": [signal_names.get(sig, sig) for sig in missing_conn_in],
                    "missing_connected_output": [signal_names.get(sig, sig) for sig in missing_conn_out],
                }
            )

    for block in extracted.always_blocks:
        expected_in = set(block.input_signals) | set(block.clock_signals) | set(block.reset_signals)
        expected_out = set(block.output_signals)
        emitted = ir_always.get(block.id)
        emitted_in = set()
        emitted_out = set()
        if emitted:
            emitted_in = set(emitted.input_signals) | set(emitted.clock_signals) | set(emitted.reset_signals)
            emitted_out = set(emitted.output_signals)
        conn_in = set(connected_in.get(block.id, set()))
        conn_out = set(connected_out.get(block.id, set()))
        block_totals["always"]["expected_input"] += len(expected_in)
        block_totals["always"]["expected_output"] += len(expected_out)
        block_totals["always"]["emitted_input"] += len(emitted_in)
        block_totals["always"]["emitted_output"] += len(emitted_out)
        missing_emit_in = sorted(expected_in - emitted_in)
        missing_emit_out = sorted(expected_out - emitted_out)
        missing_conn_in = sorted(expected_in - conn_in)
        missing_conn_out = sorted(expected_out - conn_out)
        if missing_emit_in or missing_emit_out or missing_conn_in or missing_conn_out:
            block_anomalies.append(
                {
                    "kind": "always",
                    "id": block.id,
                    "label": block.label,
                    "always_kind": block.always_kind,
                    "missing_emitted_input": [signal_names.get(sig, sig) for sig in missing_emit_in],
                    "missing_emitted_output": [signal_names.get(sig, sig) for sig in missing_emit_out],
                    "missing_connected_input": [signal_names.get(sig, sig) for sig in missing_conn_in],
                    "missing_connected_output": [signal_names.get(sig, sig) for sig in missing_conn_out],
                }
            )

    return {
        "instance_ports": {
            "declared": declared_lanes,
            "emitted": emitted_lanes,
            "connected": connected_lanes,
            "anomalies": instance_anomalies,
        },
        "expr_input_ports": {
            "coverage": expr_cov,
            "anomalies": expr_anomalies,
        },
        "logic_blocks": {
            "totals": block_totals,
            "anomalies": block_anomalies,
        },
    }


def _signal_name_list(signal_ids: Set[str] | List[str], signal_names: Dict[str, str]) -> List[str]:
    return [signal_names.get(sig, sig) for sig in signal_ids]


def _build_edge_maps(view) -> tuple[Dict[str, List[Any]], Dict[str, List[Any]]]:
    incoming: Dict[str, List[Any]] = {}
    outgoing: Dict[str, List[Any]] = {}
    for edge in view.edges:
        outgoing.setdefault(edge.from_ep.node_id, []).append(edge)
        incoming.setdefault(edge.to_ep.node_id, []).append(edge)
    return incoming, outgoing


def _edge_rows(
    edges: List[Any],
    incoming: bool,
    signal_names: Dict[str, str],
    node_labels: Dict[str, str],
    node_types: Dict[str, str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for edge in edges:
        if incoming:
            peer_id = edge.from_ep.node_id
            peer_role = edge.from_ep.role
            self_role = edge.to_ep.role
        else:
            peer_id = edge.to_ep.node_id
            peer_role = edge.to_ep.role
            self_role = edge.from_ep.role
        rows.append(
            {
                "edge_id": edge.id,
                "signal_id": edge.signal_id,
                "signal": signal_names.get(edge.signal_id, edge.signal_id),
                "self_role": self_role,
                "peer_node_id": peer_id,
                "peer_label": node_labels.get(peer_id, peer_id),
                "peer_type": node_types.get(peer_id, "unknown"),
                "peer_role": peer_role,
            }
        )
    rows.sort(key=lambda r: (r["signal"], r["peer_label"], r["edge_id"]))
    return rows


def _build_node_details(extracted, view) -> Dict[str, Any]:
    signal_names = {sig.id: sig.name for sig in view.signals}
    node_labels = {node.id: node.label for node in view.nodes}
    node_types = {node.id: node.type for node in view.nodes}
    connected_in, connected_out = _connected_signals(view)
    incoming_map, outgoing_map = _build_edge_maps(view)

    instance_details: List[Dict[str, Any]] = []
    for inst in extracted.instances:
        conn_in = connected_in.get(inst.id, set())
        conn_out = connected_out.get(inst.id, set())
        ports: List[Dict[str, Any]] = []
        for port in inst.ports:
            sig_rows = []
            for sig in port.signal_ids:
                sig_rows.append(
                    {
                        "signal_id": sig,
                        "signal": signal_names.get(sig, sig),
                        "connected_input": sig in conn_in,
                        "connected_output": sig in conn_out,
                    }
                )
            ports.append(
                {
                    "name": port.name,
                    "direction": port.direction,
                    "dangling_kind": str(getattr(port, "dangling_kind", "")),
                    "expr": str(getattr(port, "expr", "")),
                    "signal_ids": list(port.signal_ids),
                    "signals": _signal_name_list(port.signal_ids, signal_names),
                    "connected_input_signals": _signal_name_list([s for s in port.signal_ids if s in conn_in], signal_names),
                    "connected_output_signals": _signal_name_list([s for s in port.signal_ids if s in conn_out], signal_names),
                    "signal_rows": sig_rows,
                }
            )
        instance_details.append(
            {
                "id": inst.id,
                "label": inst.label,
                "name": inst.name,
                "module_name": inst.module_name,
                "ports": ports,
                "incoming_edges": _edge_rows(incoming_map.get(inst.id, []), True, signal_names, node_labels, node_types),
                "outgoing_edges": _edge_rows(outgoing_map.get(inst.id, []), False, signal_names, node_labels, node_types),
            }
        )

    assign_details: List[Dict[str, Any]] = []
    for assign in extracted.assigns:
        conn_in = connected_in.get(assign.id, set())
        conn_out = connected_out.get(assign.id, set())
        assign_details.append(
            {
                "id": assign.id,
                "label": assign.label,
                "name": assign.name,
                "expected_input_signals": _signal_name_list(assign.input_signals, signal_names),
                "expected_output_signals": _signal_name_list(assign.output_signals, signal_names),
                "connected_input_signals": _signal_name_list(sorted(conn_in), signal_names),
                "connected_output_signals": _signal_name_list(sorted(conn_out), signal_names),
                "incoming_edges": _edge_rows(incoming_map.get(assign.id, []), True, signal_names, node_labels, node_types),
                "outgoing_edges": _edge_rows(outgoing_map.get(assign.id, []), False, signal_names, node_labels, node_types),
            }
        )

    callable_details: List[Dict[str, Any]] = []
    for callable_ref in extracted.callables:
        conn_in = connected_in.get(callable_ref.id, set())
        conn_out = connected_out.get(callable_ref.id, set())
        callable_details.append(
            {
                "id": callable_ref.id,
                "label": callable_ref.label,
                "name": callable_ref.name,
                "callable_kind": callable_ref.callable_kind,
                "callable_key": callable_ref.callable_key,
                "expected_input_signals": _signal_name_list(callable_ref.input_signals, signal_names),
                "expected_output_signals": _signal_name_list(callable_ref.output_signals, signal_names),
                "connected_input_signals": _signal_name_list(sorted(conn_in), signal_names),
                "connected_output_signals": _signal_name_list(sorted(conn_out), signal_names),
                "incoming_edges": _edge_rows(incoming_map.get(callable_ref.id, []), True, signal_names, node_labels, node_types),
                "outgoing_edges": _edge_rows(outgoing_map.get(callable_ref.id, []), False, signal_names, node_labels, node_types),
            }
        )

    always_details: List[Dict[str, Any]] = []
    for block in extracted.always_blocks:
        conn_in = connected_in.get(block.id, set())
        conn_out = connected_out.get(block.id, set())
        expected_in_ids = set(block.input_signals) | set(block.clock_signals) | set(block.reset_signals)
        expected_out_ids = set(block.output_signals)
        unconnected_out_ids = expected_out_ids - conn_out
        internal_state_ids = expected_out_ids & set(block.input_signals)
        unconnected_internal_state_ids = unconnected_out_ids & internal_state_ids
        unconnected_external_ids = unconnected_out_ids - unconnected_internal_state_ids
        always_details.append(
            {
                "id": block.id,
                "label": block.label,
                "name": block.name,
                "always_kind": block.always_kind,
                "expected_input_signals": _signal_name_list(block.input_signals, signal_names),
                "expected_output_signals": _signal_name_list(block.output_signals, signal_names),
                "clock_signals": _signal_name_list(block.clock_signals, signal_names),
                "reset_signals": _signal_name_list(block.reset_signals, signal_names),
                "connected_input_signals": _signal_name_list(sorted(conn_in), signal_names),
                "connected_output_signals": _signal_name_list(sorted(conn_out), signal_names),
                "unconnected_output_signals": _signal_name_list(sorted(unconnected_out_ids), signal_names),
                "internal_state_signals": _signal_name_list(sorted(internal_state_ids), signal_names),
                "unconnected_internal_state_signals": _signal_name_list(sorted(unconnected_internal_state_ids), signal_names),
                "unconnected_external_output_signals": _signal_name_list(sorted(unconnected_external_ids), signal_names),
                "incoming_edges": _edge_rows(incoming_map.get(block.id, []), True, signal_names, node_labels, node_types),
                "outgoing_edges": _edge_rows(outgoing_map.get(block.id, []), False, signal_names, node_labels, node_types),
            }
        )

    return {
        "instances": instance_details,
        "assigns": assign_details,
        "callables": callable_details,
        "always_blocks": always_details,
    }


def _summary_text(summary: Dict[str, Any]) -> str:
    meta = summary["meta"]
    stages = summary["stages"]
    inst = summary["mismatch"]["instance_ports"]
    expr_ports = summary["mismatch"].get("expr_input_ports", {})
    blocks = summary["mismatch"]["logic_blocks"]
    lines = [
        "[rtlens] rtl debug summary",
        f"module: {meta['module']}",
        f"hier_path: {meta['hier_path']}",
        f"mode: {meta['mode']}",
        f"node command: {meta['node_cmd']}",
        f"timeout: {meta['timeout']}",
        f"runtime variant: {meta['runtime_variant']}",
        f"fast layout: {meta['fast_layout']}",
        f"run dir: {meta['run_dir']}",
        "",
        "stage counts:",
        (
            f"  parser: signals={stages['parser']['signals']} ports={stages['parser']['ports']} "
            f"instances={stages['parser']['instances']} assigns={stages['parser']['assigns']} "
            f"always={stages['parser']['always']} callables={stages['parser'].get('callables', 0)}"
        ),
        (
            f"  extract: signals={stages['extract']['signals']} ports={stages['extract']['ports']} "
            f"instances={stages['extract']['instances']} assigns={stages['extract']['assigns']} "
            f"always={stages['extract']['always']} callables={stages['extract'].get('callables', 0)}"
        ),
        (
            f"  ir: nodes={stages['ir']['nodes']} edges={stages['ir']['edges']} signals={stages['ir']['signals']} "
            f"callables={stages['ir'].get('callables', 0)}"
        ),
        f"  elk_graph: children={stages['elk_graph']['children']} edges={stages['elk_graph']['edges']}",
    ]
    extract_debug = stages.get("extract_debug", {})
    if extract_debug:
        lines.extend(
            [
                "  extract_debug: "
                f"declared={extract_debug.get('declared_signal_count', 0)} "
                f"synthetic_member={extract_debug.get('synthetic_member_count', 0)} "
                f"synthetic_undeclared={extract_debug.get('synthetic_undeclared_count', 0)} "
                f"allowed={extract_debug.get('allowed_signal_count', 0)}",
            ]
        )
        member_names = extract_debug.get("synthetic_member_names", []) or []
        undeclared_names = extract_debug.get("synthetic_undeclared_names", []) or []
        if member_names:
            lines.append(f"    member_names={member_names[:40]}")
        if undeclared_names:
            lines.append(f"    undeclared_names={undeclared_names[:40]}")
    if "elk_layout" in stages:
        lines.append(f"  elk_layout: width={stages['elk_layout']['width']} height={stages['elk_layout']['height']}")
    if "elapsed_ms" in stages:
        ems = stages["elapsed_ms"]
        lines.append(
            "timing(ms): "
            f"parser={ems['parser']} extract={ems['extract']} ir={ems['ir']} graph={ems['elk_graph']} "
            f"layout={ems.get('elk_layout', 'n/a')}"
        )
    lines.extend(
        [
            "",
            "instance port coverage:",
            (
                f"  declared(in/out)={inst['declared']['input']}/{inst['declared']['output']} "
                f"emitted(in/out)={inst['emitted']['input']}/{inst['emitted']['output']} "
                f"connected(in/out)={inst['connected']['input']}/{inst['connected']['output']}"
            ),
            f"  anomalies={len(inst['anomalies'])}",
        ]
    )
    for row in inst["anomalies"][:40]:
        lines.append(
            f"    - {row['label']}: "
            f"miss_emit_in={len(row['missing_emitted_input'])} "
            f"miss_emit_out={len(row['missing_emitted_output'])} "
            f"miss_conn_in={len(row['missing_connected_input'])} "
            f"miss_conn_out={len(row['missing_connected_output'])}"
        )

    cov = expr_ports.get("coverage", {})
    if cov:
        lines.extend(
            [
                "",
                "expr input coverage:",
                (
                    f"  total={cov.get('total', 0)} const_expr={cov.get('const_expr', 0)} "
                    f"signal_expr={cov.get('signal_expr', 0)} connected={cov.get('connected', 0)} "
                    f"via_expr_assign={cov.get('via_expr_assign', 0)}"
                ),
                f"  anomalies={len(expr_ports.get('anomalies', []))}",
            ]
        )
        for row in (expr_ports.get("anomalies", []) or [])[:40]:
            lines.append(
                f"    - {row.get('instance')}::{row.get('port')} "
                f"reason={row.get('reason')} expr={row.get('expr')!r}"
            )

    lines.extend(
        [
            "",
            "always/assign coverage:",
            (
                f"  assign expected(in/out)={blocks['totals']['assign']['expected_input']}/"
                f"{blocks['totals']['assign']['expected_output']} emitted(in/out)="
                f"{blocks['totals']['assign']['emitted_input']}/{blocks['totals']['assign']['emitted_output']}"
            ),
            (
                f"  callable expected(in/out)={blocks['totals'].get('callable', {}).get('expected_input', 0)}/"
                f"{blocks['totals'].get('callable', {}).get('expected_output', 0)} emitted(in/out)="
                f"{blocks['totals'].get('callable', {}).get('emitted_input', 0)}/"
                f"{blocks['totals'].get('callable', {}).get('emitted_output', 0)}"
            ),
            (
                f"  always expected(in/out)={blocks['totals']['always']['expected_input']}/"
                f"{blocks['totals']['always']['expected_output']} emitted(in/out)="
                f"{blocks['totals']['always']['emitted_input']}/{blocks['totals']['always']['emitted_output']}"
            ),
            f"  anomalies={len(blocks['anomalies'])}",
        ]
    )
    for row in blocks["anomalies"][:80]:
        lines.append(
            f"    - {row['kind']} {row['label']}: "
            f"miss_emit_in={len(row['missing_emitted_input'])} "
            f"miss_emit_out={len(row['missing_emitted_output'])} "
            f"miss_conn_in={len(row['missing_connected_input'])} "
            f"miss_conn_out={len(row['missing_connected_output'])}"
        )

    details = summary.get("details", {})
    lines.extend(["", "per-node detail:"])

    for inst in details.get("instances", []):
        lines.append(
            f"  [instance] {inst['label']} ({inst['name']}:{inst['module_name']}) "
            f"ports={len(inst['ports'])} in_edges={len(inst['incoming_edges'])} out_edges={len(inst['outgoing_edges'])}"
        )
        for port in inst["ports"]:
            lines.append(
                f"    port {port['name']} dir={port['direction']} sig={port['signals']} expr={port.get('expr', '')!r} "
                f"conn_in={port['connected_input_signals']} conn_out={port['connected_output_signals']}"
            )
        for edge in inst["incoming_edges"]:
            lines.append(
                f"    edge(in) {edge['signal']} <= {edge['peer_label']}[{edge['peer_role']}] "
                f"(self:{edge['self_role']})"
            )
        for edge in inst["outgoing_edges"]:
            lines.append(
                f"    edge(out) {edge['signal']} => {edge['peer_label']}[{edge['peer_role']}] "
                f"(self:{edge['self_role']})"
            )

    for assign in details.get("assigns", []):
        lines.append(
            f"  [assign] {assign['label']} "
            f"in_edges={len(assign['incoming_edges'])} out_edges={len(assign['outgoing_edges'])}"
        )
        lines.append(f"    expected_in={assign['expected_input_signals']}")
        lines.append(f"    expected_out={assign['expected_output_signals']}")
        lines.append(f"    connected_in={assign['connected_input_signals']}")
        lines.append(f"    connected_out={assign['connected_output_signals']}")
        for edge in assign["incoming_edges"]:
            lines.append(
                f"    edge(in) {edge['signal']} <= {edge['peer_label']}[{edge['peer_role']}] "
                f"(self:{edge['self_role']})"
            )
        for edge in assign["outgoing_edges"]:
            lines.append(
                f"    edge(out) {edge['signal']} => {edge['peer_label']}[{edge['peer_role']}] "
                f"(self:{edge['self_role']})"
            )

    for callable_ref in details.get("callables", []):
        lines.append(
            f"  [callable] {callable_ref['label']} kind={callable_ref['callable_kind']} "
            f"in_edges={len(callable_ref['incoming_edges'])} out_edges={len(callable_ref['outgoing_edges'])}"
        )
        lines.append(f"    expected_in={callable_ref['expected_input_signals']}")
        lines.append(f"    expected_out={callable_ref['expected_output_signals']}")
        lines.append(f"    connected_in={callable_ref['connected_input_signals']}")
        lines.append(f"    connected_out={callable_ref['connected_output_signals']}")
        for edge in callable_ref["incoming_edges"]:
            lines.append(
                f"    edge(in) {edge['signal']} <= {edge['peer_label']}[{edge['peer_role']}] "
                f"(self:{edge['self_role']})"
            )
        for edge in callable_ref["outgoing_edges"]:
            lines.append(
                f"    edge(out) {edge['signal']} => {edge['peer_label']}[{edge['peer_role']}] "
                f"(self:{edge['self_role']})"
            )

    for block in details.get("always_blocks", []):
        lines.append(
            f"  [always] {block['label']} kind={block['always_kind']} "
            f"in_edges={len(block['incoming_edges'])} out_edges={len(block['outgoing_edges'])}"
        )
        lines.append(f"    expected_in={block['expected_input_signals']}")
        lines.append(f"    expected_out={block['expected_output_signals']}")
        lines.append(f"    clocks={block['clock_signals']} resets={block['reset_signals']}")
        lines.append(f"    connected_in={block['connected_input_signals']}")
        lines.append(f"    connected_out={block['connected_output_signals']}")
        lines.append(f"    unconnected_out={block['unconnected_output_signals']}")
        lines.append(f"    internal_state={block['internal_state_signals']}")
        lines.append(f"    unconnected_internal_state={block['unconnected_internal_state_signals']}")
        lines.append(f"    unconnected_external_out={block['unconnected_external_output_signals']}")
        for edge in block["incoming_edges"]:
            lines.append(
                f"    edge(in) {edge['signal']} <= {edge['peer_label']}[{edge['peer_role']}] "
                f"(self:{edge['self_role']})"
            )
        for edge in block["outgoing_edges"]:
            lines.append(
                f"    edge(out) {edge['signal']} => {edge['peer_label']}[{edge['peer_role']}] "
                f"(self:{edge['self_role']})"
            )
    return "\n".join(lines) + "\n"


def run_rtl_debug_pipeline(
    design: DesignDB,
    hier_path: str,
    mode: str = "auto",
    node_cmd: str = "node",
    timeout: int | None = 240,
    debug_root: str | Path | None = None,
    run_layout: bool = True,
) -> Dict[str, Any]:
    if hier_path not in design.hier:
        raise KeyError(f"hier path not found: {hier_path}")
    hier_node = design.hier[hier_path]
    module_name = hier_node.module_name
    module = design.modules.get(module_name)
    if module is None:
        raise KeyError(f"module not found: {module_name}")
    module_file_abs = os.path.abspath(module.file)
    parser_callable_refs = 0
    for (site_file, site_line, _token), keys in design.callable_ref_sites.items():
        if not site_file:
            continue
        if os.path.abspath(site_file) != module_file_abs:
            continue
        if site_line < module.start_line or site_line > module.end_line:
            continue
        if any(design.callable_kinds.get(k, "") in {"function", "task"} for k in keys):
            parser_callable_refs += 1

    root = Path(debug_root) if debug_root else _default_debug_root()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_hash = hashlib.sha1(hier_path.encode("utf-8")).hexdigest()[:10]
    run_dir = root / f"{stamp}_{_safe_name(module_name)}_{run_hash}"
    run_dir.mkdir(parents=True, exist_ok=True)

    t0 = perf_counter()
    parser_stage = {
        "hier": asdict(hier_node),
        "module": asdict(module),
    }
    _write_json(run_dir / "01_parser.json", parser_stage)
    t1 = perf_counter()

    extracted = extract_module_structure(design, hier_path)
    _write_json(run_dir / "02_extract.json", asdict(extracted))
    t2 = perf_counter()

    view = build_rtl_structure_ir(extracted)
    _write_json(run_dir / "03_ir.json", view.to_dict())
    t3 = perf_counter()

    fast_layout = _resolve_fast_layout(mode, len(view.nodes), len(view.edges))
    runtime_variant = _resolve_runtime_variant(mode, len(view.nodes), len(view.edges))
    graph = rtl_ir_to_elk_graph(view, fast_layout=fast_layout)
    _write_json(run_dir / "04_elk_graph.json", graph)
    t4 = perf_counter()

    layout: Dict[str, Any] | None = None
    layout_error: str = ""
    if run_layout:
        try:
            layout = render_elk_layout(graph, node_cmd=node_cmd, timeout=timeout)
            _write_json(run_dir / "05_elk_layout.json", layout)
        except Exception as e:
            layout_error = str(e)
            _write_json(run_dir / "05_elk_layout.error.json", {"error": layout_error})
    t5 = perf_counter()

    mismatch = _summarize_mismatches(extracted, view)
    details = _build_node_details(extracted, view)

    summary = {
        "meta": {
            "module": module_name,
            "hier_path": hier_path,
            "mode": mode,
            "node_cmd": node_cmd,
            "timeout": timeout,
            "runtime_variant": runtime_variant,
            "fast_layout": fast_layout,
            "run_dir": str(run_dir),
            "layout_error": layout_error,
        },
        "stages": {
            "parser": {
                "signals": len(module.signals),
                "ports": len(module.ports),
                "instances": len(module.instances),
                "assigns": len(module.assignments),
                "always": len(getattr(module, "always_blocks", []) or []),
                "callables": parser_callable_refs,
            },
            "extract": {
                "signals": len(extracted.signals),
                "ports": len(extracted.module_ports),
                "instances": len(extracted.instances),
                "assigns": len(extracted.assigns),
                "always": len(extracted.always_blocks),
                "callables": len(extracted.callables),
            },
            "extract_debug": dict(getattr(extracted, "debug", {}) or {}),
            "ir": {
                "nodes": len(view.nodes),
                "edges": len(view.edges),
                "signals": len(view.signals),
                "callables": len([n for n in view.nodes if isinstance(n, IRCallableBlockNode)]),
            },
            "elk_graph": {
                "children": len(graph.get("children", [])),
                "edges": len(graph.get("edges", [])),
            },
            "elapsed_ms": {
                "parser": round((t1 - t0) * 1000, 1),
                "extract": round((t2 - t1) * 1000, 1),
                "ir": round((t3 - t2) * 1000, 1),
                "elk_graph": round((t4 - t3) * 1000, 1),
                "elk_layout": round((t5 - t4) * 1000, 1),
            },
        },
        "mismatch": mismatch,
        "details": details,
    }
    if layout is not None:
        summary["stages"]["elk_layout"] = {
            "width": round(float(layout.get("width", 0.0)), 1),
            "height": round(float(layout.get("height", 0.0)), 1),
        }

    _write_json(run_dir / "summary.json", summary)
    summary_text = _summary_text(summary)
    (run_dir / "summary.log").write_text(summary_text, encoding="utf-8")

    return {
        "run_dir": str(run_dir),
        "summary_path": str(run_dir / "summary.log"),
        "summary": summary,
        "summary_text": summary_text,
        "graph": graph,
        "layout": layout,
        "layout_error": layout_error,
    }
