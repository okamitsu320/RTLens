from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Type, TypeVar, Union


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


@dataclass
class IRSource:
    file: str
    line_start: int
    line_end: int
    column_start: Optional[int] = None
    column_end: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return _drop_none(asdict(self))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRSource":
        return cls(
            file=data["file"],
            line_start=int(data["line_start"]),
            line_end=int(data["line_end"]),
            column_start=_maybe_int(data.get("column_start")),
            column_end=_maybe_int(data.get("column_end")),
        )


@dataclass
class IRLayoutHint:
    group: Optional[str] = None
    side: Optional[str] = None
    rank: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return _drop_none(asdict(self))

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "IRLayoutHint":
        data = data or {}
        return cls(
            group=data.get("group"),
            side=data.get("side"),
            rank=_maybe_int(data.get("rank")),
        )


@dataclass
class IRModule:
    name: str
    file: str
    line_start: int
    line_end: int
    hier_path: str

    def to_dict(self) -> Dict[str, Any]:
        return _drop_none(asdict(self))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRModule":
        return cls(
            name=data["name"],
            file=data["file"],
            line_start=int(data["line_start"]),
            line_end=int(data["line_end"]),
            hier_path=data["hier_path"],
        )


@dataclass
class IRSignal:
    id: str
    name: str
    kind: str
    width: Optional[int]
    declared_in: str
    source: IRSource
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out = _drop_none(asdict(self))
        out["source"] = self.source.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRSignal":
        return cls(
            id=data["id"],
            name=data["name"],
            kind=data["kind"],
            width=_maybe_int(data.get("width")),
            declared_in=data["declared_in"],
            source=IRSource.from_dict(data["source"]),
            tags=list(data.get("tags", [])),
        )


@dataclass
class IRNodeBase:
    id: str
    type: str
    name: str
    label: str
    source: IRSource
    layout_hint: IRLayoutHint = field(default_factory=IRLayoutHint)

    def _base_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "label": self.label,
            "source": self.source.to_dict(),
            "layout_hint": self.layout_hint.to_dict(),
        }


@dataclass
class IRModulePortNode(IRNodeBase):
    direction: str = "input"
    signal_ids: List[str] = field(default_factory=list)

    def __init__(
        self,
        id: str,
        name: str,
        label: str,
        direction: str,
        signal_ids: List[str],
        source: IRSource,
        layout_hint: Optional[IRLayoutHint] = None,
    ) -> None:
        super().__init__(
            id=id,
            type="module_port",
            name=name,
            label=label,
            source=source,
            layout_hint=layout_hint or IRLayoutHint(),
        )
        self.direction = direction
        self.signal_ids = list(signal_ids)

    def to_dict(self) -> Dict[str, Any]:
        out = self._base_dict()
        out.update(
            {
                "direction": self.direction,
                "signal_ids": list(self.signal_ids),
            }
        )
        return _drop_none(out)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRModulePortNode":
        return cls(
            id=data["id"],
            name=data["name"],
            label=data["label"],
            direction=data["direction"],
            signal_ids=list(data.get("signal_ids", [])),
            source=IRSource.from_dict(data["source"]),
            layout_hint=IRLayoutHint.from_dict(data.get("layout_hint")),
        )


@dataclass
class IRInstancePort:
    name: str
    direction: str
    signal_ids: List[str] = field(default_factory=list)
    dangling_kind: str = ""
    expr: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "signal_ids": list(self.signal_ids),
            "dangling_kind": self.dangling_kind,
            "expr": self.expr,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRInstancePort":
        return cls(
            name=data["name"],
            direction=data["direction"],
            signal_ids=list(data.get("signal_ids", [])),
            dangling_kind=str(data.get("dangling_kind", "")),
            expr=str(data.get("expr", "")),
        )


@dataclass
class IRInstanceNode(IRNodeBase):
    module_name: str = ""
    parameters: Dict[str, str] = field(default_factory=dict)
    parameter_positional: List[str] = field(default_factory=list)
    ports: List[IRInstancePort] = field(default_factory=list)

    def __init__(
        self,
        id: str,
        name: str,
        label: str,
        module_name: str,
        parameters: Dict[str, str],
        parameter_positional: List[str],
        ports: List[IRInstancePort],
        source: IRSource,
        layout_hint: Optional[IRLayoutHint] = None,
    ) -> None:
        super().__init__(
            id=id,
            type="instance",
            name=name,
            label=label,
            source=source,
            layout_hint=layout_hint or IRLayoutHint(),
        )
        self.module_name = module_name
        self.parameters = dict(parameters)
        self.parameter_positional = list(parameter_positional)
        self.ports = list(ports)

    def to_dict(self) -> Dict[str, Any]:
        out = self._base_dict()
        out.update(
            {
                "module_name": self.module_name,
                "parameters": dict(self.parameters),
                "parameter_positional": list(self.parameter_positional),
                "ports": [p.to_dict() for p in self.ports],
            }
        )
        return _drop_none(out)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRInstanceNode":
        return cls(
            id=data["id"],
            name=data["name"],
            label=data["label"],
            module_name=data["module_name"],
            parameters=dict(data.get("parameters", {})),
            parameter_positional=list(data.get("parameter_positional", [])),
            ports=[IRInstancePort.from_dict(p) for p in data.get("ports", [])],
            source=IRSource.from_dict(data["source"]),
            layout_hint=IRLayoutHint.from_dict(data.get("layout_hint")),
        )


@dataclass
class IRAssignBlockNode(IRNodeBase):
    input_signals: List[str] = field(default_factory=list)
    output_signals: List[str] = field(default_factory=list)
    expr_summary: str = ""

    def __init__(
        self,
        id: str,
        name: str,
        label: str,
        input_signals: List[str],
        output_signals: List[str],
        expr_summary: str,
        source: IRSource,
        layout_hint: Optional[IRLayoutHint] = None,
    ) -> None:
        super().__init__(
            id=id,
            type="assign_block",
            name=name,
            label=label,
            source=source,
            layout_hint=layout_hint or IRLayoutHint(),
        )
        self.input_signals = list(input_signals)
        self.output_signals = list(output_signals)
        self.expr_summary = expr_summary

    def to_dict(self) -> Dict[str, Any]:
        out = self._base_dict()
        out.update(
            {
                "input_signals": list(self.input_signals),
                "output_signals": list(self.output_signals),
                "expr_summary": self.expr_summary,
            }
        )
        return _drop_none(out)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRAssignBlockNode":
        return cls(
            id=data["id"],
            name=data["name"],
            label=data["label"],
            input_signals=list(data.get("input_signals", [])),
            output_signals=list(data.get("output_signals", [])),
            expr_summary=data.get("expr_summary", ""),
            source=IRSource.from_dict(data["source"]),
            layout_hint=IRLayoutHint.from_dict(data.get("layout_hint")),
        )


@dataclass
class IRAlwaysBlockNode(IRNodeBase):
    always_kind: str = "always"
    input_signals: List[str] = field(default_factory=list)
    output_signals: List[str] = field(default_factory=list)
    clock_signals: List[str] = field(default_factory=list)
    reset_signals: List[str] = field(default_factory=list)
    stmt_summary: List[str] = field(default_factory=list)

    def __init__(
        self,
        id: str,
        name: str,
        label: str,
        always_kind: str,
        input_signals: List[str],
        output_signals: List[str],
        clock_signals: List[str],
        reset_signals: List[str],
        stmt_summary: List[str],
        source: IRSource,
        layout_hint: Optional[IRLayoutHint] = None,
    ) -> None:
        super().__init__(
            id=id,
            type="always_block",
            name=name,
            label=label,
            source=source,
            layout_hint=layout_hint or IRLayoutHint(),
        )
        self.always_kind = always_kind
        self.input_signals = list(input_signals)
        self.output_signals = list(output_signals)
        self.clock_signals = list(clock_signals)
        self.reset_signals = list(reset_signals)
        self.stmt_summary = list(stmt_summary)

    def to_dict(self) -> Dict[str, Any]:
        out = self._base_dict()
        out.update(
            {
                "always_kind": self.always_kind,
                "input_signals": list(self.input_signals),
                "output_signals": list(self.output_signals),
                "clock_signals": list(self.clock_signals),
                "reset_signals": list(self.reset_signals),
                "stmt_summary": list(self.stmt_summary),
            }
        )
        return _drop_none(out)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRAlwaysBlockNode":
        return cls(
            id=data["id"],
            name=data["name"],
            label=data["label"],
            always_kind=data.get("always_kind", "always"),
            input_signals=list(data.get("input_signals", [])),
            output_signals=list(data.get("output_signals", [])),
            clock_signals=list(data.get("clock_signals", [])),
            reset_signals=list(data.get("reset_signals", [])),
            stmt_summary=list(data.get("stmt_summary", [])),
            source=IRSource.from_dict(data["source"]),
            layout_hint=IRLayoutHint.from_dict(data.get("layout_hint")),
        )


@dataclass
class IRCallableBlockNode(IRNodeBase):
    callable_kind: str = "function"
    callable_key: str = ""
    input_signals: List[str] = field(default_factory=list)
    output_signals: List[str] = field(default_factory=list)
    stmt_summary: str = ""

    def __init__(
        self,
        id: str,
        name: str,
        label: str,
        callable_kind: str,
        callable_key: str,
        input_signals: List[str],
        output_signals: List[str],
        stmt_summary: str,
        source: IRSource,
        layout_hint: Optional[IRLayoutHint] = None,
    ) -> None:
        super().__init__(
            id=id,
            type="callable_block",
            name=name,
            label=label,
            source=source,
            layout_hint=layout_hint or IRLayoutHint(),
        )
        self.callable_kind = callable_kind
        self.callable_key = callable_key
        self.input_signals = list(input_signals)
        self.output_signals = list(output_signals)
        self.stmt_summary = stmt_summary

    def to_dict(self) -> Dict[str, Any]:
        out = self._base_dict()
        out.update(
            {
                "callable_kind": self.callable_kind,
                "callable_key": self.callable_key,
                "input_signals": list(self.input_signals),
                "output_signals": list(self.output_signals),
                "stmt_summary": self.stmt_summary,
            }
        )
        return _drop_none(out)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IRCallableBlockNode":
        return cls(
            id=data["id"],
            name=data["name"],
            label=data["label"],
            callable_kind=data.get("callable_kind", "function"),
            callable_key=data.get("callable_key", ""),
            input_signals=list(data.get("input_signals", [])),
            output_signals=list(data.get("output_signals", [])),
            stmt_summary=data.get("stmt_summary", ""),
            source=IRSource.from_dict(data["source"]),
            layout_hint=IRLayoutHint.from_dict(data.get("layout_hint")),
        )


IRNode = Union[IRModulePortNode, IRInstanceNode, IRAssignBlockNode, IRAlwaysBlockNode, IRCallableBlockNode]


@dataclass
class IREndpoint:
    node_id: str
    role: str

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "role": self.role}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IREndpoint":
        return cls(node_id=data["node_id"], role=data["role"])


@dataclass
class IREdge:
    id: str
    signal_id: str
    from_ep: IREndpoint
    to_ep: IREndpoint

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "signal_id": self.signal_id,
            "from": self.from_ep.to_dict(),
            "to": self.to_ep.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IREdge":
        return cls(
            id=data["id"],
            signal_id=data["signal_id"],
            from_ep=IREndpoint.from_dict(data["from"]),
            to_ep=IREndpoint.from_dict(data["to"]),
        )


NODE_TYPE_MAP: Dict[str, Type[IRNode]] = {
    "module_port": IRModulePortNode,
    "instance": IRInstanceNode,
    "assign_block": IRAssignBlockNode,
    "always_block": IRAlwaysBlockNode,
    "callable_block": IRCallableBlockNode,
}


@dataclass
class RTLStructureView:
    schema_version: str
    view_type: str
    module: IRModule
    signals: List[IRSignal] = field(default_factory=list)
    nodes: List[IRNode] = field(default_factory=list)
    edges: List[IREdge] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "view_type": self.view_type,
            "module": self.module.to_dict(),
            "signals": [s.to_dict() for s in self.signals],
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RTLStructureView":
        nodes: List[IRNode] = []
        for node_data in data.get("nodes", []):
            node_type = node_data["type"]
            node_cls = NODE_TYPE_MAP[node_type]
            nodes.append(node_cls.from_dict(node_data))
        return cls(
            schema_version=data["schema_version"],
            view_type=data["view_type"],
            module=IRModule.from_dict(data["module"]),
            signals=[IRSignal.from_dict(s) for s in data.get("signals", [])],
            nodes=nodes,
            edges=[IREdge.from_dict(e) for e in data.get("edges", [])],
        )


def make_empty_rtl_structure_view(module: IRModule) -> RTLStructureView:
    return RTLStructureView(
        schema_version="0.1",
        view_type="rtl_structure",
        module=module,
        signals=[],
        nodes=[],
        edges=[],
    )


def _maybe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)
