from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple

from .rtl_ir import (
    IRAlwaysBlockNode,
    IRAssignBlockNode,
    IRCallableBlockNode,
    IRInstanceNode,
    IRModulePortNode,
    RTLStructureView,
)


def _port_side_for_direction(direction: str, io_role: str) -> str:
    if io_role == "input":
        return "WEST"
    if io_role == "output":
        return "EAST"
    if direction == "input":
        return "WEST"
    if direction == "output":
        return "EAST"
    return "WEST"


def _label_width(text: str, min_width: float = 60.0, char_px: float = 7.0) -> float:
    return max(min_width, 16.0 + len(text) * char_px)


def _node_size(node) -> Tuple[float, float]:
    if isinstance(node, IRModulePortNode):
        return (22.0, 18.0)
    if isinstance(node, IRInstanceNode):
        left_labels = [p.name for p in node.ports if p.direction in {"input", "inout", "unknown"}]
        right_labels = [p.name for p in node.ports if p.direction in {"output", "inout", "unknown"}]
        rows = max(len(left_labels), len(right_labels), 1)
        left_w = max((_label_width(x, min_width=58.0, char_px=6.5) for x in left_labels), default=58.0)
        right_w = max((_label_width(x, min_width=58.0, char_px=6.5) for x in right_labels), default=58.0)
        param_parts = [f"{k}={v}" for k, v in node.parameters.items()]
        param_parts.extend(node.parameter_positional)
        param_text = ", ".join(param_parts)
        center_w = max(
            _label_width(node.label, min_width=190.0, char_px=7.8),
            _label_width(param_text, min_width=70.0, char_px=6.2) if param_text else 0.0,
        )
        return (left_w + center_w + right_w + 18.0, max(54.0, 24.0 * rows + 18.0))
    if isinstance(node, (IRAssignBlockNode, IRAlwaysBlockNode, IRCallableBlockNode)):
        left_labels = list(node.input_signals)
        right_labels = list(node.output_signals)
        rows = max(len(left_labels), len(right_labels), 1)
        left_w = max((_label_width(x, min_width=68.0, char_px=6.8) for x in left_labels), default=68.0)
        right_w = max((_label_width(x, min_width=68.0, char_px=6.8) for x in right_labels), default=68.0)
        center_w = _label_width(node.label, min_width=170.0, char_px=7.6)
        return (left_w + center_w + right_w + 16.0, max(44.0, 22.0 * rows + 16.0))
    return (120.0, 40.0)


def _base_node_payload(node, width: float, height: float) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": node.id,
        "width": width,
        "height": height,
        "labels": [{"id": f"{node.id}_label", "text": node.label}],
        "layoutOptions": {
            "elk.portConstraints": "FIXED_ORDER",
        },
        "rtlensNodeType": getattr(node, "type", ""),
        "rtlensName": getattr(node, "name", ""),
        "rtlensSource": {
            "file": node.source.file,
            "line": node.source.line_start,
        },
    }
    if isinstance(node, IRModulePortNode):
        payload["layoutOptions"]["elk.layered.layering.layerConstraint"] = (
            "LAST" if node.direction == "output" else "FIRST"
        )
        payload["rtlensDirection"] = node.direction
    return payload


def _make_port(
    owner_id: str,
    port_id: str,
    label: str,
    side: str,
    index: int,
    signal_name: str = "",
    dangling_kind: str = "",
    expr: str = "",
) -> Dict[str, Any]:
    out = {
        "id": f"{owner_id}.{port_id}",
        "width": 10.0,
        "height": 10.0,
        "labels": [{"id": f"{owner_id}.{port_id}.label", "text": label}],
        "layoutOptions": {
            "elk.port.side": side,
            "elk.port.index": str(index),
        },
        "rtlensPortLabel": label,
        "rtlensPortSide": side,
        "rtlensSignalName": signal_name,
        "rtlensIsStructMember": bool(signal_name and "." in signal_name),
    }
    if dangling_kind:
        out["rtlensDanglingKind"] = dangling_kind
    if expr:
        out["rtlensExpr"] = expr
    return out


def rtl_ir_to_elk_graph(view: RTLStructureView, fast_layout: bool = False) -> Dict[str, Any]:
    layout_options = {
        "elk.algorithm": "layered",
        "elk.direction": "RIGHT",
        "elk.spacing.nodeNode": "50",
        "elk.layered.spacing.nodeNodeBetweenLayers": "90",
        "elk.spacing.edgeNode": "28",
        "elk.padding": "[top=30,left=30,bottom=30,right=30]",
    }
    if fast_layout:
        layout_options.update(
            {
                "elk.edgeRouting": "ORTHOGONAL",
                "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
                "elk.layered.thoroughness": "1",
            }
        )
    else:
        layout_options.update(
            {
                "elk.edgeRouting": "ORTHOGONAL",
                "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
                "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
            }
        )

    graph: Dict[str, Any] = {
        "id": f"rtl_{view.module.name}",
        "layoutOptions": layout_options,
        "children": [],
        "edges": [],
        "rtlensModule": {
            "name": view.module.name,
            "file": view.module.file,
            "line_start": view.module.line_start,
            "line_end": view.module.line_end,
            "hier_path": view.module.hier_path,
        },
    }

    node_map = {node.id: node for node in view.nodes}
    signal_names = {sig.id: sig.name for sig in view.signals}
    signal_meta = {sig.id: sig for sig in view.signals}
    connected_out_pairs: set[tuple[str, str]] = set()
    for edge in view.edges:
        if edge.from_ep.role == "output":
            connected_out_pairs.add((edge.from_ep.node_id, edge.signal_id))
    port_counts: Dict[str, int] = {}

    def next_index(node_id: str, side: str) -> int:
        key = f"{node_id}:{side}"
        cur = port_counts.get(key, 0)
        port_counts[key] = cur + 1
        return cur

    children: List[Dict[str, Any]] = []
    port_ref: Dict[Tuple[str, str, str], List[str]] = {}

    def _add_port_ref(key: Tuple[str, str, str], port_id: str) -> None:
        bucket = port_ref.setdefault(key, [])
        if port_id not in bucket:
            bucket.append(port_id)

    for node in view.nodes:
        width, height = _node_size(node)
        payload = _base_node_payload(node, width, height)
        ports: List[Dict[str, Any]] = []

        if isinstance(node, IRModulePortNode):
            side = _port_side_for_direction(node.direction, "output" if node.direction in {"input", "inout"} else "input")
            sig_id = node.signal_ids[0] if node.signal_ids else node.id
            p = _make_port(node.id, "p", node.label, side, next_index(node.id, side), signal_names.get(sig_id, node.label))
            ports.append(p)
            if node.direction in {"input", "inout"}:
                _add_port_ref((node.id, sig_id, "output"), p["id"])
            if node.direction in {"output", "inout"}:
                _add_port_ref((node.id, sig_id, "input"), p["id"])
            payload["rtlensShape"] = "module_port"

        elif isinstance(node, IRInstanceNode):
            for port in node.ports:
                port_dangling_kind = str(getattr(port, "dangling_kind", ""))
                label_signal_name = next(
                    (signal_names.get(sig_id, "") for sig_id in port.signal_ids if signal_names.get(sig_id, "")),
                    "",
                )
                port_expr = str(getattr(port, "expr", ""))
                west_id = None
                east_id = None
                if port.direction in {"input", "inout", "unknown"}:
                    pid = f"{port.name}__w"
                    p = _make_port(
                        node.id,
                        pid,
                        port.name,
                        "WEST",
                        next_index(node.id, "WEST"),
                        label_signal_name or port.name,
                        dangling_kind=port_dangling_kind,
                        expr=port_expr,
                    )
                    ports.append(p)
                    west_id = p["id"]
                if port.direction in {"output", "inout", "unknown"}:
                    pid = f"{port.name}__e"
                    p = _make_port(
                        node.id,
                        pid,
                        port.name,
                        "EAST",
                        next_index(node.id, "EAST"),
                        label_signal_name or port.name,
                        dangling_kind=port_dangling_kind,
                        expr=port_expr,
                    )
                    ports.append(p)
                    east_id = p["id"]
                for sig_id in port.signal_ids:
                    if west_id:
                        _add_port_ref((node.id, sig_id, "input"), west_id)
                    if east_id:
                        _add_port_ref((node.id, sig_id, "output"), east_id)
            payload["rtlensShape"] = "instance"
            payload["rtlensModuleName"] = node.module_name
            payload["rtlensParams"] = dict(node.parameters)
            payload["rtlensParamsPositional"] = list(node.parameter_positional)

        elif isinstance(node, IRAssignBlockNode):
            for sig_id in node.input_signals:
                p = _make_port(node.id, f"{sig_id}__w", signal_names.get(sig_id, sig_id), "WEST", next_index(node.id, "WEST"), signal_names.get(sig_id, sig_id))
                ports.append(p)
                _add_port_ref((node.id, sig_id, "input"), p["id"])
            for sig_id in node.output_signals:
                p = _make_port(node.id, f"{sig_id}__e", signal_names.get(sig_id, sig_id), "EAST", next_index(node.id, "EAST"), signal_names.get(sig_id, sig_id))
                ports.append(p)
                _add_port_ref((node.id, sig_id, "output"), p["id"])
            payload["rtlensShape"] = "assign_block"

        elif isinstance(node, IRAlwaysBlockNode):
            for sig_id in node.input_signals:
                p = _make_port(node.id, f"{sig_id}__w", signal_names.get(sig_id, sig_id), "WEST", next_index(node.id, "WEST"), signal_names.get(sig_id, sig_id))
                ports.append(p)
                _add_port_ref((node.id, sig_id, "input"), p["id"])
            for sig_id in node.output_signals:
                dangling_kind = ""
                if (node.id, sig_id) not in connected_out_pairs:
                    sig = signal_meta.get(sig_id)
                    if sig and sig.kind in {"reg", "logic"}:
                        dangling_kind = "internal_state"
                p = _make_port(
                    node.id,
                    f"{sig_id}__e",
                    signal_names.get(sig_id, sig_id),
                    "EAST",
                    next_index(node.id, "EAST"),
                    signal_names.get(sig_id, sig_id),
                    dangling_kind=dangling_kind,
                )
                ports.append(p)
                _add_port_ref((node.id, sig_id, "output"), p["id"])
            payload["rtlensShape"] = "always_block"
            payload["rtlensAlwaysKind"] = node.always_kind

        elif isinstance(node, IRCallableBlockNode):
            for sig_id in node.input_signals:
                p = _make_port(
                    node.id,
                    f"{sig_id}__w",
                    signal_names.get(sig_id, sig_id),
                    "WEST",
                    next_index(node.id, "WEST"),
                    signal_names.get(sig_id, sig_id),
                )
                ports.append(p)
                _add_port_ref((node.id, sig_id, "input"), p["id"])
            for sig_id in node.output_signals:
                p = _make_port(
                    node.id,
                    f"{sig_id}__e",
                    signal_names.get(sig_id, sig_id),
                    "EAST",
                    next_index(node.id, "EAST"),
                    signal_names.get(sig_id, sig_id),
                )
                ports.append(p)
                _add_port_ref((node.id, sig_id, "output"), p["id"])
            payload["rtlensShape"] = "callable_block"
            payload["rtlensCallableKind"] = node.callable_kind
            payload["rtlensCallableKey"] = node.callable_key

        if ports:
            payload["ports"] = ports
        children.append(payload)

    graph["children"] = children

    signal_fanout: Dict[str, int] = {}
    for edge in view.edges:
        signal_fanout[edge.signal_id] = signal_fanout.get(edge.signal_id, 0) + 1

    edges: List[Dict[str, Any]] = []
    for edge in view.edges:
        srcs = list(port_ref.get((edge.from_ep.node_id, edge.signal_id, edge.from_ep.role), []))
        dsts = list(port_ref.get((edge.to_ep.node_id, edge.signal_id, edge.to_ep.role), []))
        if not srcs or not dsts:
            continue
        sig = signal_meta.get(edge.signal_id)
        pairs: List[Tuple[str, str]] = []
        if len(srcs) == 1 and len(dsts) == 1:
            pairs = [(srcs[0], dsts[0])]
        elif len(srcs) == 1:
            pairs = [(srcs[0], d) for d in dsts]
        elif len(dsts) == 1:
            pairs = [(s, dsts[0]) for s in srcs]
        else:
            span = max(len(srcs), len(dsts))
            pairs = [(srcs[i % len(srcs)], dsts[i % len(dsts)]) for i in range(span)]
        seen_pairs: set[Tuple[str, str]] = set()
        for idx, (src, dst) in enumerate(pairs):
            if (src, dst) in seen_pairs:
                continue
            seen_pairs.add((src, dst))
            eid = edge.id if idx == 0 else f"{edge.id}__{idx+1}"
            edges.append(
                {
                    "id": eid,
                    "sources": [src],
                    "targets": [dst],
                    "labels": [{"id": f"{eid}_label", "text": signal_names.get(edge.signal_id, edge.signal_id)}],
                    "rtlensSignalId": edge.signal_id,
                    "rtlensSignalName": signal_names.get(edge.signal_id, edge.signal_id),
                    "rtlensIsBus": bool(sig and (sig.width and sig.width > 1 or "bus" in sig.tags)),
                    "rtlensWidth": int(sig.width or 1) if sig else 1,
                    "rtlensIsStructMember": bool(signal_names.get(edge.signal_id, edge.signal_id).find(".") >= 0),
                    "rtlensFanout": int(signal_fanout.get(edge.signal_id, 1)),
                }
            )
    graph["edges"] = edges
    return graph


def elk_benchmark_variants(graph: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    def _clone() -> Dict[str, Any]:
        return deepcopy(graph)

    def _drop_fixed_order(g: Dict[str, Any]) -> None:
        for child in g.get("children", []):
            lo = child.get("layoutOptions")
            if isinstance(lo, dict):
                lo.pop("elk.portConstraints", None)

    variants: List[Tuple[str, Dict[str, Any]]] = []

    g = _clone()
    g.setdefault("layoutOptions", {}).update(
        {
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
            "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
        }
    )
    variants.append(("strict_ortho_fixed", g))

    g = _clone()
    g.setdefault("layoutOptions", {}).update(
        {
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
            "elk.layered.thoroughness": "1",
        }
    )
    g["layoutOptions"].pop("elk.layered.nodePlacement.strategy", None)
    g["layoutOptions"].pop("elk.layered.crossingMinimization.strategy", None)
    variants.append(("ortho_fast_fixed", g))

    g = _clone()
    g.setdefault("layoutOptions", {}).update(
        {
            "elk.edgeRouting": "ORTHOGONAL",
            "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
            "elk.layered.thoroughness": "1",
        }
    )
    g["layoutOptions"].pop("elk.layered.nodePlacement.strategy", None)
    g["layoutOptions"].pop("elk.layered.crossingMinimization.strategy", None)
    _drop_fixed_order(g)
    variants.append(("ortho_fast_freeports", g))

    g = _clone()
    g.setdefault("layoutOptions", {}).update(
        {
            "elk.edgeRouting": "POLYLINE",
            "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
            "elk.layered.thoroughness": "1",
        }
    )
    g["layoutOptions"].pop("elk.layered.nodePlacement.strategy", None)
    g["layoutOptions"].pop("elk.layered.crossingMinimization.strategy", None)
    variants.append(("fast_polyline_fixed", g))

    g = _clone()
    g.setdefault("layoutOptions", {}).update(
        {
            "elk.edgeRouting": "POLYLINE",
            "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
            "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
        }
    )
    g["layoutOptions"].pop("elk.layered.considerModelOrder.strategy", None)
    g["layoutOptions"].pop("elk.layered.thoroughness", None)
    variants.append(("polyline_fullopt_fixed", g))

    g = _clone()
    g.setdefault("layoutOptions", {}).update(
        {
            "elk.edgeRouting": "POLYLINE",
            "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
            "elk.layered.thoroughness": "1",
        }
    )
    g["layoutOptions"].pop("elk.layered.nodePlacement.strategy", None)
    g["layoutOptions"].pop("elk.layered.crossingMinimization.strategy", None)
    _drop_fixed_order(g)
    variants.append(("fast_polyline_freeports", g))

    return variants
