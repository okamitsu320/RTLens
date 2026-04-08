from __future__ import annotations

from collections import defaultdict
from urllib.parse import quote
from typing import Dict, List

from .rtl_ir import (
    IRAlwaysBlockNode,
    IRAssignBlockNode,
    IRCallableBlockNode,
    IRInstanceNode,
    IRModulePortNode,
    RTLStructureView,
)


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _node_attrs(attrs: Dict[str, str]) -> str:
    parts = []
    for key, value in attrs.items():
        if value == "":
            continue
        if key == "label" and value.startswith("<") and value.endswith(">"):
            parts.append(f"{key}={value}")
        else:
            parts.append(f"{key}={_q(value)}")
    return "[" + ", ".join(parts) + "]"


def _edge_attrs(attrs: Dict[str, str]) -> str:
    return _node_attrs(attrs)


def _signal_name_map(view: RTLStructureView) -> Dict[str, str]:
    return {sig.id: sig.name for sig in view.signals}


def _signal_tags_map(view: RTLStructureView) -> Dict[str, set[str]]:
    return {sig.id: set(sig.tags) for sig in view.signals}


def _record_escape(value: str) -> str:
    out = value.replace("\\", "\\\\")
    for ch in "{}|<>":
        out = out.replace(ch, "\\" + ch)
    return out.replace('"', '\\"')


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _source_url(file: str, line: int) -> str:
    return f"rtlens://source?file={quote(file)}&line={int(line)}"


def _source_tooltip(file: str, line: int, label: str) -> str:
    return f"{label} @ {file}:{line}"


def _base_node_attrs(node_id: str, label: str, file: str, line: int) -> Dict[str, str]:
    return {
        "id": node_id,
        "URL": _source_url(file, line),
        "tooltip": _source_tooltip(file, line, label),
        "target": "_top",
    }


def _port_node_shape(direction: str) -> tuple[str, str]:
    if direction == "input":
        return "polygon", "#e8f1ff"
    if direction == "output":
        return "box", "#ffe9e8"
    return "hexagon", "#e8fff1"


def _port_node_extra_attrs(direction: str) -> Dict[str, str]:
    if direction == "input":
        return {
            "regular": "false",
            "sides": "5",
            "orientation": "270",
            "distortion": "-0.55",
            "skew": "0.0",
        }
    return {}


def _record_ports(fields: List[str]) -> str:
    if not fields:
        return " "
    return "|".join(_record_escape(f) for f in fields[:12])


def _dot_port_id(name: str) -> str:
    safe = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            safe.append(ch)
        else:
            safe.append("_")
    out = "".join(safe).strip("_")
    return out or "p"


def _signal_port_id(signal_id: str, side: str) -> str:
    return f"{_dot_port_id(signal_id)}__{side}"


def _record_port_fields(ports: List[str]) -> str:
    if not ports:
        return " "
    return "|".join(f"<{_dot_port_id(name)}>{_record_escape(name)}" for name in ports[:12])


def _record_named_port_fields(ports: List[tuple[str, str]]) -> str:
    if not ports:
        return " "
    return "|".join(f"<{_dot_port_id(port_id)}>{_record_escape(name)}" for port_id, name in ports[:12])


def _instance_record_label(node: IRInstanceNode) -> str:
    left: List[tuple[str, str]] = []
    right: List[tuple[str, str]] = []
    for port in node.ports:
        base = _dot_port_id(port.name)
        if port.direction == "input":
            left.append((f"{base}__w", port.name))
        elif port.direction == "output":
            right.append((f"{base}__e", port.name))
        else:
            left.append((f"{base}__w", port.name))
            right.append((f"{base}__e", port.name))
    center = _record_escape(node.label)
    return "{ {" + _record_named_port_fields(left) + "} | " + center + " | {" + _record_named_port_fields(right) + "} }"


def _instance_html_label(node: IRInstanceNode) -> str:
    left: List[tuple[str, str]] = []
    right: List[tuple[str, str]] = []
    for port in node.ports:
        base = _dot_port_id(port.name)
        if port.direction == "input":
            left.append((f"{base}__w", port.name))
        elif port.direction == "output":
            right.append((f"{base}__e", port.name))
        else:
            left.append((f"{base}__w", port.name))
            right.append((f"{base}__e", port.name))

    rows = max(len(left), len(right), 1)
    left += [("", "")] * (rows - len(left))
    right += [("", "")] * (rows - len(right))
    center = _html_escape(node.label)
    parts = [
        '<',
        '<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4" BGCOLOR="#fff4db" COLOR="#8a6d3b">',
    ]
    for idx in range(rows):
        lport, lname = left[idx]
        rport, rname = right[idx]
        parts.append('<TR>')
        if lport:
            parts.append(f'<TD PORT="{_html_escape(lport)}" ALIGN="LEFT">{_html_escape(lname)}</TD>')
        else:
            parts.append('<TD BORDER="0"></TD>')
        if idx == 0:
            parts.append(
                f'<TD ROWSPAN="{rows}" ALIGN="CENTER" VALIGN="MIDDLE" BGCOLOR="#fff4db"><B>{center}</B></TD>'
            )
        if rport:
            parts.append(f'<TD PORT="{_html_escape(rport)}" ALIGN="LEFT">{_html_escape(rname)}</TD>')
        else:
            parts.append('<TD BORDER="0"></TD>')
        parts.append('</TR>')
    parts.append('</TABLE>')
    parts.append('>')
    return "".join(parts)


def _block_record_label(title: str, _summary: str, inputs: List[str], outputs: List[str]) -> str:
    center = _record_escape(title)
    return "{ {" + _record_ports(inputs) + "} | " + center + " | {" + _record_ports(outputs) + "} }"


def _block_html_label(title: str, inputs: List[tuple[str, str]], outputs: List[tuple[str, str]], fillcolor: str) -> str:
    rows = max(len(inputs), len(outputs), 1)
    inputs = inputs + [("", "")] * (rows - len(inputs))
    outputs = outputs + [("", "")] * (rows - len(outputs))
    parts = [
        '<',
        f'<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4" BGCOLOR="{fillcolor}">',
    ]
    for idx in range(rows):
        in_port, in_name = inputs[idx]
        out_port, out_name = outputs[idx]
        parts.append('<TR>')
        if in_port:
            parts.append(f'<TD PORT="{_html_escape(in_port)}" ALIGN="LEFT">{_html_escape(in_name)}</TD>')
        else:
            parts.append('<TD BORDER="0"></TD>')
        if idx == 0:
            parts.append(
                f'<TD ROWSPAN="{rows}" ALIGN="CENTER" VALIGN="MIDDLE" BGCOLOR="{fillcolor}"><B>{_html_escape(title)}</B></TD>'
            )
        if out_port:
            parts.append(f'<TD PORT="{_html_escape(out_port)}" ALIGN="LEFT">{_html_escape(out_name)}</TD>')
        else:
            parts.append('<TD BORDER="0"></TD>')
        parts.append('</TR>')
    parts.append('</TABLE>')
    parts.append('>')
    return "".join(parts)


def _port_sort_key(node: IRModulePortNode) -> tuple[int, str]:
    side_rank = 0 if node.layout_hint.side == "left" else 1
    return (side_rank, node.name.lower())


def _node_group_name(node) -> str:
    if isinstance(node, IRModulePortNode):
        return "ports_left" if node.layout_hint.side in {"left", None} else "ports_right"
    if isinstance(node, IRInstanceNode):
        return "instances"
    if isinstance(node, IRAssignBlockNode):
        return "combinational"
    if isinstance(node, IRCallableBlockNode):
        return "combinational"
    if isinstance(node, IRAlwaysBlockNode):
        return "sequential" if node.always_kind in {"always_ff", "always_latch"} else "combinational"
    return "misc"


def rtl_ir_to_dot(view: RTLStructureView) -> str:
    signal_names = _signal_name_map(view)
    signal_tags = _signal_tags_map(view)
    ports: List[IRModulePortNode] = []
    instances: List[IRInstanceNode] = []
    assigns: List[IRAssignBlockNode] = []
    callables: List[IRCallableBlockNode] = []
    always_blocks: List[IRAlwaysBlockNode] = []

    for node in view.nodes:
        if isinstance(node, IRModulePortNode):
            ports.append(node)
        elif isinstance(node, IRInstanceNode):
            instances.append(node)
        elif isinstance(node, IRAssignBlockNode):
            assigns.append(node)
        elif isinstance(node, IRCallableBlockNode):
            callables.append(node)
        elif isinstance(node, IRAlwaysBlockNode):
            always_blocks.append(node)

    ports.sort(key=_port_sort_key)
    instances.sort(key=lambda n: n.name.lower())
    assigns.sort(key=lambda n: n.name.lower())
    callables.sort(key=lambda n: n.name.lower())
    always_blocks.sort(key=lambda n: n.name.lower())

    lines: List[str] = []
    graph_name = f"rtl_{view.module.name}"
    lines.append(f"digraph {graph_name} {{")
    lines.append(
        "  graph [rankdir=LR, splines=ortho, compound=true, fontname=\"Helvetica\", "
        "nodesep=0.9, ranksep=1.3, overlap=false, outputorder=edgesfirst];"
    )
    lines.append("  node [fontname=\"Helvetica\", fontsize=10, shape=box];")
    lines.append("  edge [fontname=\"Helvetica\", fontsize=9];")
    lines.append("")
    lines.append("  __rank_ports_left [shape=point, width=0.01, height=0.01, label=\"\", style=invis];")
    lines.append("  __rank_comb [shape=point, width=0.01, height=0.01, label=\"\", style=invis];")
    lines.append("  __rank_inst [shape=point, width=0.01, height=0.01, label=\"\", style=invis];")
    lines.append("  __rank_seq [shape=point, width=0.01, height=0.01, label=\"\", style=invis];")
    lines.append("  __rank_ports_right [shape=point, width=0.01, height=0.01, label=\"\", style=invis];")
    lines.append(
        "  __rank_ports_left -> __rank_comb -> __rank_inst -> __rank_seq -> __rank_ports_right "
        "[style=invis, weight=100, minlen=2];"
    )
    lines.append("")

    lines.append("  subgraph cluster_module {")
    lines.append(f"    label={_q(f'{view.module.name} ({view.module.hier_path})')};")
    lines.append("    color=\"#cccccc\";")
    lines.append("    style=\"rounded\";")

    if ports:
        left_ports = [p for p in ports if p.layout_hint.side in {"left", None}]
        right_ports = [p for p in ports if p.layout_hint.side == "right"]
        if left_ports:
            lines.append("    { rank=same;")
            for node in left_ports:
                lines.append(
                    f"      {node.id} "
                    + _node_attrs(
                        {
                            **_base_node_attrs(node.id, node.label, node.source.file, node.source.line_start),
                            **_port_node_extra_attrs(node.direction),
                            "shape": _port_node_shape(node.direction)[0],
                            "style": "filled",
                            "fillcolor": _port_node_shape(node.direction)[1],
                            "label": " ",
                            "xlabel": node.label,
                            "fixedsize": "true",
                            "width": "0.55",
                            "height": "0.40",
                        }
                    )
                    + ";"
                )
                lines.append(f"      __rank_ports_left -> {node.id} [style=invis, weight=30];")
            lines.append("    }")
        for node in instances:
            lines.append(
                f"    {node.id} "
                + _node_attrs(
                    {
                        **_base_node_attrs(node.id, node.label, node.source.file, node.source.line_start),
                        "shape": "plain",
                        "label": _instance_html_label(node),
                    }
                )
                + ";"
            )
            lines.append(f"    __rank_inst -> {node.id} [style=invis, weight=20];")
        for node in assigns:
            input_ports = [(_signal_port_id(sig, "w"), signal_names.get(sig, sig)) for sig in node.input_signals]
            output_ports = [(_signal_port_id(sig, "e"), signal_names.get(sig, sig)) for sig in node.output_signals]
            lines.append(
                f"    {node.id} "
                + _node_attrs(
                    {
                        **_base_node_attrs(node.id, node.label, node.source.file, node.source.line_start),
                        "shape": "plain",
                        "label": _block_html_label(node.label, input_ports, output_ports, "#eef7e8"),
                    }
                )
                + ";"
            )
            lines.append(f"    __rank_comb -> {node.id} [style=invis, weight=20];")
        for node in callables:
            input_ports = [(_signal_port_id(sig, "w"), signal_names.get(sig, sig)) for sig in node.input_signals]
            output_ports = [(_signal_port_id(sig, "e"), signal_names.get(sig, sig)) for sig in node.output_signals]
            title = node.label
            lines.append(
                f"    {node.id} "
                + _node_attrs(
                    {
                        **_base_node_attrs(node.id, title, node.source.file, node.source.line_start),
                        "shape": "plain",
                        "label": _block_html_label(title, input_ports, output_ports, "#eaf1ff"),
                    }
                )
                + ";"
            )
            lines.append(f"    __rank_comb -> {node.id} [style=invis, weight=20];")
        for node in always_blocks:
            fill = "#f7e8f5" if node.always_kind in {"always_ff", "always_latch"} else "#f3f3f3"
            input_ports = [(_signal_port_id(sig, "w"), signal_names.get(sig, sig)) for sig in node.input_signals]
            output_ports = [(_signal_port_id(sig, "e"), signal_names.get(sig, sig)) for sig in node.output_signals]
            lines.append(
                f"    {node.id} "
                + _node_attrs(
                    {
                        **_base_node_attrs(node.id, node.label, node.source.file, node.source.line_start),
                        "shape": "plain",
                        "label": _block_html_label(node.label, input_ports, output_ports, fill),
                    }
                )
                + ";"
            )
            anchor = "__rank_seq" if node.always_kind in {"always_ff", "always_latch"} else "__rank_comb"
            lines.append(f"    {anchor} -> {node.id} [style=invis, weight=20];")
        if right_ports:
            lines.append("    { rank=same;")
            for node in right_ports:
                lines.append(
                    f"      {node.id} "
                    + _node_attrs(
                        {
                            **_base_node_attrs(node.id, node.label, node.source.file, node.source.line_start),
                            **_port_node_extra_attrs(node.direction),
                            "shape": _port_node_shape(node.direction)[0],
                            "style": "filled",
                            "fillcolor": _port_node_shape(node.direction)[1],
                            "label": " ",
                            "xlabel": node.label,
                            "fixedsize": "true",
                            "width": "0.55",
                            "height": "0.40",
                        }
                    )
                    + ";"
                )
                lines.append(f"      __rank_ports_right -> {node.id} [style=invis, weight=30];")
            lines.append("    }")
    lines.append("  }")
    lines.append("")

    instance_nodes = {node.id: node for node in instances}
    assign_nodes = {node.id: node for node in assigns}
    callable_nodes = {node.id: node for node in callables}
    always_nodes = {node.id: node for node in always_blocks}
    node_group = {node.id: _node_group_name(node) for node in view.nodes}
    group_anchor = {
        "ports_left": "__rank_ports_left",
        "combinational": "__rank_comb",
        "instances": "__rank_inst",
        "sequential": "__rank_seq",
        "ports_right": "__rank_ports_right",
    }

    def endpoint_ref(node_id: str, signal_id: str, role: str) -> str:
        inst = instance_nodes.get(node_id)
        if inst:
            for port in inst.ports:
                if signal_id not in port.signal_ids:
                    continue
                port_id = _dot_port_id(port.name)
                if role == "input":
                    return f"{node_id}:{port_id}__w"
                if role == "output":
                    return f"{node_id}:{port_id}__e"
        assign = assign_nodes.get(node_id)
        if assign:
            if role == "input" and signal_id in assign.input_signals:
                return f"{node_id}:{_signal_port_id(signal_id, 'w')}"
            if role == "output" and signal_id in assign.output_signals:
                return f"{node_id}:{_signal_port_id(signal_id, 'e')}"
        callable_node = callable_nodes.get(node_id)
        if callable_node:
            if role == "input" and signal_id in callable_node.input_signals:
                return f"{node_id}:{_signal_port_id(signal_id, 'w')}"
            if role == "output" and signal_id in callable_node.output_signals:
                return f"{node_id}:{_signal_port_id(signal_id, 'e')}"
        block = always_nodes.get(node_id)
        if block:
            if role == "input" and signal_id in block.input_signals:
                return f"{node_id}:{_signal_port_id(signal_id, 'w')}"
            if role == "output" and signal_id in block.output_signals:
                return f"{node_id}:{_signal_port_id(signal_id, 'e')}"
        return node_id

    edges_by_signal: Dict[str, List[str]] = defaultdict(list)
    relay_added: set[str] = set()
    for edge in view.edges:
        label = signal_names.get(edge.signal_id, edge.signal_id)
        relay_id = f"__sig_{_dot_port_id(edge.signal_id)}"
        relay_anchor = group_anchor.get(node_group.get(edge.to_ep.node_id, "instances"), "__rank_inst")
        if relay_id not in relay_added:
            relay_added.add(relay_id)
            lines.append(
                f"  {relay_id} "
                + _node_attrs(
                    {
                        "shape": "point",
                        "width": "0.01",
                        "height": "0.01",
                        "label": "",
                        "xlabel": label,
                        "color": "#777777",
                        "fontcolor": "#444444",
                    }
                )
                + ";"
            )
            lines.append(f"  {relay_anchor} -> {relay_id} [style=invis, weight=10];")
        attrs = {
            "color": "#4c4c4c",
            "tooltip": label,
            "tailport": "e",
            "headport": "w",
        }
        if "clock" in signal_tags.get(edge.signal_id, set()):
            attrs["color"] = "#356ae6"
            attrs["penwidth"] = "2"
        elif "reset" in signal_tags.get(edge.signal_id, set()):
            attrs["color"] = "#d94841"
            attrs["penwidth"] = "2"
        elif "bus" in signal_tags.get(edge.signal_id, set()):
            attrs["penwidth"] = "2"
        edges_by_signal[edge.signal_id].append(
            f"  {endpoint_ref(edge.from_ep.node_id, edge.signal_id, edge.from_ep.role)}"
            f" -> {relay_id} "
            + _edge_attrs(attrs)
            + ";"
        )
        edges_by_signal[edge.signal_id].append(
            f"  {relay_id}"
            f" -> {endpoint_ref(edge.to_ep.node_id, edge.signal_id, edge.to_ep.role)} "
            + _edge_attrs(attrs)
            + ";"
        )

    for signal_id in sorted(edges_by_signal, key=lambda sid: signal_names.get(sid, sid).lower()):
        for line in edges_by_signal[signal_id]:
            lines.append(line)

    lines.append("}")
    return "\n".join(lines)
