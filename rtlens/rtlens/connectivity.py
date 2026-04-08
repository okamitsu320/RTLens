from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Set, Tuple

from .model import ConnectivityDB, DesignDB, HierNode, SourceLoc


def build_hierarchy(db: DesignDB, top_module: Optional[str]) -> None:
    """Populate db.hier and db.roots by BFS from the top module.

    If top_module is None or not found in db.modules, the root is inferred as the
    module that is not instantiated by any other module.  Mutates db in-place.
    """
    if not db.modules:
        return

    if top_module and top_module in db.modules:
        top = top_module
    else:
        instantiated = set()
        for mod in db.modules.values():
            for inst in mod.instances:
                instantiated.add(inst.module_type)
        roots = [m for m in db.modules.keys() if m not in instantiated]
        top = roots[0] if roots else next(iter(db.modules.keys()))

    db.top_module = top
    root_path = top
    db.hier = {
        root_path: HierNode(path=root_path, module_name=top, inst_name=top, parent=None, children=[])
    }
    db.roots = [root_path]

    q = deque([root_path])
    while q:
        path = q.popleft()
        node = db.hier[path]
        mod = db.modules.get(node.module_name)
        if not mod:
            continue
        for inst in mod.instances:
            if inst.module_type not in db.modules:
                continue
            child_path = f"{path}.{inst.name}"
            db.hier[child_path] = HierNode(
                path=child_path,
                module_name=inst.module_type,
                inst_name=inst.name,
                parent=path,
                children=[],
            )
            node.children.append(child_path)
            q.append(child_path)


def _abs(path: str, sig: str) -> str:
    """Return the absolute signal name by joining hierarchy path and signal name."""
    return f"{path}.{sig}"


def build_connectivity(db: DesignDB) -> ConnectivityDB:
    """Build a ConnectivityDB from an elaborated DesignDB.

    Traverses the hierarchy, records signal source locations, and adds directed
    edges for continuous assignments and port connections.  Port direction determines
    edge direction: input → parent-to-child, output → child-to-parent, inout → both.

    Returns a new ConnectivityDB; does not mutate db.
    """
    cdb = ConnectivityDB()

    for path, node in db.hier.items():
        mod = db.modules.get(node.module_name)
        if not mod:
            continue

        for sig_name, sig in mod.signals.items():
            cdb.signal_to_source[_abs(path, sig_name)] = SourceLoc(file=mod.file, line=sig.line)

        for a in mod.assignments:
            src_loc = SourceLoc(file=mod.file, line=a.line)
            lhs_abs_list: List[str] = []
            for lhs in a.lhs:
                lhs_abs = _abs(path, lhs)
                lhs_abs_list.append(lhs_abs)
                cdb.signal_to_source.setdefault(lhs_abs, src_loc)
                cdb.add_driver_site(lhs_abs, src_loc)
            for rhs in a.rhs:
                rhs_abs = _abs(path, rhs)
                cdb.signal_to_source.setdefault(rhs_abs, src_loc)
                cdb.add_load_site(rhs_abs, src_loc, kind="data")
                for lhs_abs in lhs_abs_list:
                    cdb.add_edge(rhs_abs, lhs_abs, kind="data")

        for blk in mod.always_blocks:
            blk_loc = SourceLoc(file=mod.file, line=blk.line_start)
            write_abs_list: List[str] = []
            for w in blk.writes:
                w_abs = _abs(path, w)
                write_abs_list.append(w_abs)
                cdb.signal_to_source.setdefault(w_abs, blk_loc)
                cdb.add_driver_site(w_abs, blk_loc)
            for r in blk.reads:
                r_abs = _abs(path, r)
                cdb.signal_to_source.setdefault(r_abs, blk_loc)
                cdb.add_load_site(r_abs, blk_loc, kind="data")
                for w_abs in write_abs_list:
                    cdb.add_edge(r_abs, w_abs, kind="data")
            control_reads = list(blk.clock_signals) + list(blk.reset_signals)
            for c in control_reads:
                c_abs = _abs(path, c)
                cdb.signal_to_source.setdefault(c_abs, blk_loc)
                cdb.add_load_site(c_abs, blk_loc, kind="control")
                for w_abs in write_abs_list:
                    cdb.add_edge(c_abs, w_abs, kind="control")

        for inst in mod.instances:
            child_path = f"{path}.{inst.name}"
            child = db.modules.get(inst.module_type)
            if not child or child_path not in db.hier:
                continue

            conn_loc = SourceLoc(file=mod.file, line=inst.line)
            for port_name, parent_sig in inst.connections.items():
                p = child.ports.get(port_name)
                if not p:
                    continue
                parent_abs = _abs(path, parent_sig)
                child_abs = _abs(child_path, port_name)
                cdb.signal_to_source.setdefault(parent_abs, conn_loc)
                cdb.signal_to_source.setdefault(child_abs, conn_loc)
                if p.direction == "input":
                    cdb.add_driver_site_port(child_abs, conn_loc)
                    cdb.add_load_site(parent_abs, conn_loc, kind="port")
                    cdb.add_edge(parent_abs, child_abs)
                elif p.direction == "output":
                    cdb.add_driver_site_port(parent_abs, conn_loc)
                    cdb.add_load_site(child_abs, conn_loc, kind="port")
                    cdb.add_edge(child_abs, parent_abs)
                else:
                    cdb.add_driver_site_port(parent_abs, conn_loc)
                    cdb.add_driver_site_port(child_abs, conn_loc)
                    cdb.add_load_site(parent_abs, conn_loc, kind="port")
                    cdb.add_load_site(child_abs, conn_loc, kind="port")
                    cdb.add_edge(parent_abs, child_abs)
                    cdb.add_edge(child_abs, parent_abs)

    return cdb


def _neighbor_map(cdb: ConnectivityDB, include_control: bool) -> Dict[str, Set[str]]:
    """Build an adjacency map (signal → set of driven signals) for graph traversal."""
    out: Dict[str, Set[str]] = {}
    for s, ds in cdb.drives_data.items():
        out.setdefault(s, set()).update(ds)
    if include_control:
        for s, ds in cdb.drives_control.items():
            out.setdefault(s, set()).update(ds)
    return out


def _alias_closure(cdb: ConnectivityDB, start: str) -> Set[str]:
    """Return all signals reachable from start via alias edges (BFS)."""
    seen: Set[str] = set([start])
    q = deque([start])
    while q:
        cur = q.popleft()
        for nxt in cdb.alias_edges.get(cur, set()):
            if nxt in seen:
                continue
            seen.add(nxt)
            q.append(nxt)
    return seen


def _collect_forward(cdb: ConnectivityDB, start: str, include_control: bool) -> Set[str]:
    """Return all signals reachable from start by following drive edges forward (BFS)."""
    neigh = _neighbor_map(cdb, include_control)
    seen: Set[str] = set()
    q = deque([start])
    seen.add(start)
    out: Set[str] = set()
    while q:
        cur = q.popleft()
        for nxt in neigh.get(cur, set()):
            if nxt in seen:
                continue
            seen.add(nxt)
            out.add(nxt)
            q.append(nxt)
    return out


def _collect_reverse(cdb: ConnectivityDB, start: str, include_control: bool) -> Set[str]:
    """Return all signals that can reach start by following drive edges in reverse (BFS)."""
    rev = _reverse_map(cdb, include_control)

    seen: Set[str] = set()
    q = deque([start])
    seen.add(start)
    out: Set[str] = set()
    while q:
        cur = q.popleft()
        for prv in rev.get(cur, set()):
            if prv in seen:
                continue
            seen.add(prv)
            out.add(prv)
            q.append(prv)
    return out


def _reverse_map(cdb: ConnectivityDB, include_control: bool) -> Dict[str, Set[str]]:
    """Build the transpose of _neighbor_map (driven signal → set of drivers)."""
    neigh = _neighbor_map(cdb, include_control)
    rev: Dict[str, Set[str]] = {}
    for s, dsts in neigh.items():
        for d in dsts:
            rev.setdefault(d, set()).add(s)
        rev.setdefault(s, set())
    return rev


def query_signal(
    cdb: ConnectivityDB,
    abs_signal: str,
    recursive: bool = False,
    include_control: bool = False,
    include_ports: bool = False,
) -> Tuple[List[Tuple[str, SourceLoc]], List[Tuple[str, SourceLoc]]]:
    """Find the drivers and loads of a signal.

    Args:
        cdb:             ConnectivityDB built by build_connectivity().
        abs_signal:      Fully qualified signal name (e.g. "top.u_core.data_out").
        recursive:       If True, follow the entire transitive drive graph (BFS).
                         If False, return only direct driver/load sites from the DB.
        include_control: Include control-flow drive edges (clock, enable, reset)
                         in addition to data edges.
        include_ports:   Include port driver/load sites in non-recursive mode.

    Returns:
        (drivers, loads) where each element is a sorted list of
        (absolute_signal_name, SourceLoc) tuples.
    """
    if not recursive:
        def uniq_pairs(items: List[Tuple[str, SourceLoc]]) -> List[Tuple[str, SourceLoc]]:
            seen = set()
            out: List[Tuple[str, SourceLoc]] = []
            for sig, loc in items:
                key = (sig, loc.file, loc.line)
                if key in seen:
                    continue
                seen.add(key)
                out.append((sig, loc))
            out.sort(key=lambda x: (x[0], x[1].file, x[1].line))
            return out

        aliases = _alias_closure(cdb, abs_signal)
        dsrc: List[Tuple[str, SourceLoc]] = []
        lsrc: List[Tuple[str, SourceLoc]] = []
        for s in aliases:
            dsrc.extend([(s, loc) for loc in cdb.driver_sites.get(s, [])])
            if include_ports:
                dsrc.extend([(s, loc) for loc in cdb.driver_sites_port.get(s, [])])
            lsrc.extend([(s, loc) for loc in cdb.load_sites_data.get(s, [])])
            if include_ports:
                lsrc.extend([(s, loc) for loc in cdb.load_sites_port.get(s, [])])
            if include_control:
                lsrc.extend([(s, loc) for loc in cdb.load_sites_control.get(s, [])])

        drivers = uniq_pairs(dsrc)
        loads = uniq_pairs(lsrc)
        return drivers, loads

    neigh = _neighbor_map(cdb, include_control)
    if recursive:
        drivers_abs = sorted(_collect_reverse(cdb, abs_signal, include_control))
        loads_abs = sorted(_collect_forward(cdb, abs_signal, include_control))
    else:
        drivers_abs = []
        loads_abs = []

    drivers: List[Tuple[str, SourceLoc]] = []
    loads: List[Tuple[str, SourceLoc]] = []

    for s in drivers_abs:
        if s == abs_signal:
            continue
        loc = cdb.signal_to_source.get(s)
        if loc:
            drivers.append((s, loc))
    for s in loads_abs:
        if s == abs_signal:
            continue
        loc = cdb.signal_to_source.get(s)
        if loc:
            loads.append((s, loc))

    return drivers, loads
