from __future__ import annotations

from collections import defaultdict

from .rtl_extract import ExtractedModuleStructure
from .rtl_ir import (
    IRAssignBlockNode,
    IRAlwaysBlockNode,
    IRCallableBlockNode,
    IREdge,
    IREndpoint,
    IRInstanceNode,
    IRInstancePort,
    IRLayoutHint,
    IRModule,
    IRModulePortNode,
    IRSignal,
    IRSource,
    RTLStructureView,
    make_empty_rtl_structure_view,
)


def _ir_source(file: str, line: int) -> IRSource:
    return IRSource(file=file, line_start=line, line_end=line)


def _uniq_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _append_endpoint(table: dict[str, list[IREndpoint]], signal_id: str, node_id: str, role: str) -> None:
    table[signal_id].append(IREndpoint(node_id=node_id, role=role))


def build_rtl_structure_ir(extracted: ExtractedModuleStructure) -> RTLStructureView:
    module = IRModule(
        name=extracted.module_name,
        file=extracted.module_file,
        line_start=extracted.line_start,
        line_end=extracted.line_end,
        hier_path=extracted.hier_path,
    )
    view = make_empty_rtl_structure_view(module)

    for sig in extracted.signals:
        view.signals.append(
            IRSignal(
                id=sig.id,
                name=sig.name,
                kind=sig.kind,
                width=sig.width,
                declared_in=sig.declared_in,
                source=_ir_source(sig.source.file, sig.source.line),
                tags=list(sig.tags),
            )
        )

    for port in extracted.module_ports:
        side = "left" if port.direction in {"input", "inout"} else "right"
        view.nodes.append(
            IRModulePortNode(
                id=port.id,
                name=port.name,
                label=port.name,
                direction=port.direction,
                signal_ids=list(port.signal_ids),
                source=_ir_source(port.source.file, port.source.line),
                layout_hint=IRLayoutHint(group="ports", side=side),
            )
        )

    for inst in extracted.instances:
        view.nodes.append(
            IRInstanceNode(
                id=inst.id,
                name=inst.name,
                label=inst.label,
                module_name=inst.module_name,
                parameters=dict(inst.parameters),
                parameter_positional=list(inst.parameter_positional),
                ports=[
                    IRInstancePort(
                        name=p.name,
                        direction=p.direction,
                        signal_ids=list(p.signal_ids),
                        dangling_kind=str(getattr(p, "dangling_kind", "")),
                        expr=str(getattr(p, "expr", "")),
                    )
                    for p in inst.ports
                ],
                source=_ir_source(inst.source.file, inst.source.line),
                layout_hint=IRLayoutHint(group="instances"),
            )
        )

    for assign in extracted.assigns:
        view.nodes.append(
            IRAssignBlockNode(
                id=assign.id,
                name=assign.name,
                label=assign.label,
                input_signals=_uniq_keep_order(list(assign.input_signals)),
                output_signals=_uniq_keep_order(list(assign.output_signals)),
                expr_summary=assign.expr_summary,
                source=_ir_source(assign.source.file, assign.source.line),
                layout_hint=IRLayoutHint(group="combinational"),
            )
        )

    for block in extracted.always_blocks:
        group = "sequential" if block.always_kind in {"always_ff", "always_latch"} else "combinational"
        view.nodes.append(
            IRAlwaysBlockNode(
                id=block.id,
                name=block.name,
                label=block.label,
                always_kind=block.always_kind,
                input_signals=_uniq_keep_order(list(block.input_signals)),
                output_signals=_uniq_keep_order(list(block.output_signals)),
                clock_signals=_uniq_keep_order(list(block.clock_signals)),
                reset_signals=_uniq_keep_order(list(block.reset_signals)),
                stmt_summary=list(block.stmt_summary),
                source=_ir_source(block.source.file, block.source.line),
                layout_hint=IRLayoutHint(group=group),
            )
        )

    for callable_ref in extracted.callables:
        view.nodes.append(
            IRCallableBlockNode(
                id=callable_ref.id,
                name=callable_ref.name,
                label=callable_ref.label,
                callable_kind=callable_ref.callable_kind,
                callable_key=callable_ref.callable_key,
                input_signals=_uniq_keep_order(list(callable_ref.input_signals)),
                output_signals=_uniq_keep_order(list(callable_ref.output_signals)),
                stmt_summary=callable_ref.stmt_summary,
                source=_ir_source(callable_ref.source.file, callable_ref.source.line),
                layout_hint=IRLayoutHint(group="combinational"),
            )
        )

    known_signals = {sig.id for sig in extracted.signals}
    producers: dict[str, list[IREndpoint]] = defaultdict(list)
    consumers: dict[str, list[IREndpoint]] = defaultdict(list)

    for port in extracted.module_ports:
        if not port.signal_ids:
            continue
        signal_id = port.signal_ids[0]
        if signal_id not in known_signals:
            continue
        if port.direction in {"input", "inout"}:
            _append_endpoint(producers, signal_id, port.id, "output")
        if port.direction in {"output", "inout"}:
            _append_endpoint(consumers, signal_id, port.id, "input")

    for assign in extracted.assigns:
        for sig_id in _uniq_keep_order(list(assign.input_signals)):
            if sig_id in known_signals:
                _append_endpoint(consumers, sig_id, assign.id, "input")
        for sig_id in _uniq_keep_order(list(assign.output_signals)):
            if sig_id in known_signals:
                _append_endpoint(producers, sig_id, assign.id, "output")

    for block in extracted.always_blocks:
        for sig_id in _uniq_keep_order(list(block.input_signals)):
            if sig_id in known_signals:
                _append_endpoint(consumers, sig_id, block.id, "input")
        for sig_id in _uniq_keep_order(list(block.output_signals)):
            if sig_id in known_signals:
                _append_endpoint(producers, sig_id, block.id, "output")

    for callable_ref in extracted.callables:
        for sig_id in _uniq_keep_order(list(callable_ref.input_signals)):
            if sig_id in known_signals:
                _append_endpoint(consumers, sig_id, callable_ref.id, "input")
        for sig_id in _uniq_keep_order(list(callable_ref.output_signals)):
            if sig_id in known_signals:
                _append_endpoint(producers, sig_id, callable_ref.id, "output")

    for inst in extracted.instances:
        for port in inst.ports:
            for sig_id in port.signal_ids:
                if sig_id not in known_signals:
                    continue
                if port.direction in {"input", "unknown"}:
                    _append_endpoint(consumers, sig_id, inst.id, "input")
                if port.direction in {"output", "inout", "unknown"}:
                    _append_endpoint(producers, sig_id, inst.id, "output")

    edge_index = 0
    seen_edges: set[tuple[str, str, str]] = set()
    for signal_id in sorted(known_signals):
        src_eps = producers.get(signal_id, [])
        dst_eps = consumers.get(signal_id, [])
        for src_ep in src_eps:
            for dst_ep in dst_eps:
                if src_ep.node_id == dst_ep.node_id:
                    continue
                key = (signal_id, src_ep.node_id, dst_ep.node_id)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edge_index += 1
                view.edges.append(
                    IREdge(
                        id=f"edge_{edge_index}",
                        signal_id=signal_id,
                        from_ep=src_ep,
                        to_ep=dst_ep,
                    )
                )

    return view
