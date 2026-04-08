#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO / "rtlens"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from rtlens.wave import load_wave


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def main() -> int:
    fst = REPO / "vsim" / "fp64_min.fst"
    if not fst.exists():
        _pass("skip: vsim/fp64_min.fst not found")
        return 0

    db = load_wave(str(fst))
    if len(db.signals) == 0:
        return _fail("wave has zero signals")

    cands = [n for n in db.signals.keys() if n.endswith("u_core.u_longlat.counter")]
    if not cands:
        return _fail("missing u_core.u_longlat.counter in wave")

    sig = db.signals[cands[0]]
    if len(sig.changes) == 0:
        return _fail("counter exists but has no changes")

    _pass(f"signals={len(db.signals)} times={len(db.times)} counter_changes={len(sig.changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
