from __future__ import annotations

from typing import Dict, List, Optional

from .model import DesignDB


def token_variants(word: str) -> List[str]:
    """Return lookup variants for a source token (strip punctuation, split on :: / -> / .)."""
    raw = (word or "").strip()
    if not raw:
        return []
    base = raw.strip("()[]{};,")
    vals = [raw, base]
    if "::" in base:
        vals.append(base.split("::")[-1])
    if "->" in base:
        vals.append(base.split("->")[-1])
    if "." in base:
        vals.append(base.split(".")[-1])
    out: List[str] = []
    seen = set()
    for v in vals:
        v = (v or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _collect_site_candidates(
    design: DesignDB,
    file: str,
    line: int,
    tokens: List[str],
    include_ref: bool,
    include_def: bool,
) -> Dict[str, int]:
    """Collect callable keys that have a reference or definition site matching (file, line, token).

    Returns a dict mapping callable key → number of site hits.
    """
    counts: Dict[str, int] = {}
    if not file or line <= 0:
        return counts
    for tok in tokens:
        site = (file, int(line), tok)
        if include_ref:
            for key in design.callable_ref_sites.get(site, []):
                counts[key] = counts.get(key, 0) + 1
        if include_def:
            for key in design.callable_def_sites.get(site, []):
                counts[key] = counts.get(key, 0) + 1
    return counts


def _add_name_candidates(design: DesignDB, counts: Dict[str, int], tokens: List[str]) -> None:
    """Seed counts with all keys whose short name matches any token, without overwriting site hits."""
    for tok in tokens:
        for key in sorted(design.callable_name_index.get(tok, set())):
            counts.setdefault(key, 0)


def _pick_callable_key(
    design: DesignDB,
    keys: List[str],
    current_hier_path: str = "",
    file: str = "",
    line: int = 0,
    word: str = "",
    site_counts: Optional[Dict[str, int]] = None,
) -> Optional[str]:
    """Pick the best callable key from candidates using a multi-criteria score tuple.

    Score tuple (higher wins, compared lexicographically):
      site_hits  — number of matching ref/def sites at the call location (most decisive)
      same_file  — 1 if the definition is in the same file as the call site
      line_prox  — negative line distance to definition (0 = same line, more negative = further away)
      scope_hit  — 1 if the definition path is an ancestor/descendant of current_hier_path
      common     — number of leading hierarchy components shared with current_hier_path
      name_exact — 1 if the short callable name exactly matches a token variant of word
      kind_rank  — 1 if the callable is a function or task (preferred over other kinds)
      shorter    — negative full-path length (prefer shorter, less deeply nested names)
    """
    if not keys:
        return None
    uniq = sorted(set(keys))
    if len(uniq) == 1:
        return uniq[0]
    tokens = token_variants(word)
    token_set = set(tokens)
    hier = (current_hier_path or "").strip()
    hparts = hier.split(".") if hier else []
    best_key: Optional[str] = None
    best_score: Optional[tuple[int, int, int, int, int, int, int, int]] = None
    for key in uniq:
        full = key.split(":", 1)[-1]
        kind = design.callable_kinds.get(key, "")
        name = design.callable_names.get(key, full.split(".")[-1])
        loc = design.callable_defs.get(key)
        loc_file = (loc.file if loc else "")
        loc_line = int(loc.line if loc else 0)

        # How many times this key appeared at the exact call site.
        site_hits = int((site_counts or {}).get(key, 0))
        # Bonus when the definition lives in the same file as the call.
        same_file = int(bool(file and loc_file and file == loc_file))
        # Proximity: closer definitions score higher (negative distance).
        if same_file and line > 0 and loc_line > 0:
            line_prox = -abs(line - loc_line)
        else:
            line_prox = -1_000_000

        scope_hit = 0
        common = 0
        if hier:
            fparts = full.split(".")
            for a, b in zip(hparts, fparts):
                if a != b:
                    break
                common += 1
            if full == hier or full.startswith(hier + ".") or hier.startswith(full + "."):
                scope_hit = 1

        # Prefer function/task when there is no stronger site match.
        kind_rank = 1 if kind in {"function", "task"} else 0
        name_exact = 1 if name in token_set else 0
        shorter = -len(full)

        score = (
            site_hits,
            same_file,
            line_prox,
            scope_hit,
            common,
            name_exact,
            kind_rank,
            shorter,
        )
        if best_score is None or score > best_score:
            best_score = score
            best_key = key
    return best_key


def resolve_callable_key_from_site(
    design: DesignDB,
    file: str,
    line: int,
    word: str,
    current_hier_path: str = "",
) -> Optional[str]:
    """Resolve the callable key for a token at a specific reference or definition site.

    Uses both ref and def site indexes, falling back to name-only lookup.
    Returns the best matching key, or None if no candidates exist.
    """
    if not file or not word:
        return None
    toks = token_variants(word)
    if not toks:
        return None
    counts = _collect_site_candidates(design, file, int(line), toks, include_ref=True, include_def=True)
    _add_name_candidates(design, counts, toks)
    return _pick_callable_key(
        design,
        list(counts.keys()),
        current_hier_path=current_hier_path,
        file=file,
        line=int(line),
        word=word,
        site_counts=counts,
    )


def resolve_callable_key_for_definition_site(
    design: DesignDB,
    file: str,
    line: int,
    word: str,
    current_hier_path: str = "",
) -> Optional[str]:
    """Resolve the callable key preferring definition sites over reference sites.

    Used when the cursor is on a declaration/definition line rather than a call site.
    Falls back to name-only lookup if no definition site match is found.
    """
    if not file or not word:
        return None
    toks = token_variants(word)
    if not toks:
        return None
    def_counts = _collect_site_candidates(design, file, int(line), toks, include_ref=False, include_def=True)
    picked = _pick_callable_key(
        design,
        list(def_counts.keys()),
        current_hier_path=current_hier_path,
        file=file,
        line=int(line),
        word=word,
        site_counts=def_counts,
    )
    if picked:
        return picked
    _add_name_candidates(design, def_counts, toks)
    return _pick_callable_key(
        design,
        list(def_counts.keys()),
        current_hier_path=current_hier_path,
        file=file,
        line=int(line),
        word=word,
        site_counts=def_counts,
    )


def resolve_callable_key_any_site(
    design: DesignDB,
    file: str,
    line: int,
    current_hier_path: str = "",
    prefer_kinds: Optional[tuple[str, ...]] = ("function", "task"),
) -> Optional[str]:
    """Resolve any callable key at a given (file, line) without requiring a token word.

    Scans all ref and def sites on the line. If prefer_kinds is set, filters to those
    kinds before scoring. Returns None if the line has no recorded callable sites.
    """
    if not file or line <= 0:
        return None
    line_i = int(line)
    counts: Dict[str, int] = {}
    for (f, l, _tok), keys in design.callable_ref_sites.items():
        if f != file or int(l) != line_i:
            continue
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    for (f, l, _tok), keys in design.callable_def_sites.items():
        if f != file or int(l) != line_i:
            continue
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None

    keys = list(counts.keys())
    if prefer_kinds:
        pset = set(prefer_kinds)
        filtered = [k for k in keys if design.callable_kinds.get(k, "") in pset]
        if filtered:
            keys = filtered
            counts = {k: counts[k] for k in keys}

    return _pick_callable_key(
        design,
        keys,
        current_hier_path=current_hier_path,
        file=file,
        line=line_i,
        word="",
        site_counts=counts,
    )


def explain_callable_resolution(
    design: DesignDB,
    file: str,
    line: int,
    word: str,
    current_hier_path: str = "",
) -> Dict[str, object]:
    """Return a diagnostic dict explaining how a callable is resolved for a given site.

    Includes the token variants tried, all candidate keys with their scores, and the
    final resolved keys for both from-site and for-definition-site strategies.
    Useful for debugging resolution mismatches.
    """
    toks = token_variants(word)
    refdef_counts = _collect_site_candidates(design, file, int(line), toks, include_ref=True, include_def=True)
    def_counts = _collect_site_candidates(design, file, int(line), toks, include_ref=False, include_def=True)
    merged = dict(refdef_counts)
    _add_name_candidates(design, merged, toks)
    picked_from = _pick_callable_key(
        design,
        list(merged.keys()),
        current_hier_path=current_hier_path,
        file=file,
        line=int(line),
        word=word,
        site_counts=merged,
    )
    picked_def = _pick_callable_key(
        design,
        list(def_counts.keys()),
        current_hier_path=current_hier_path,
        file=file,
        line=int(line),
        word=word,
        site_counts=def_counts,
    )
    if not picked_def:
        _add_name_candidates(design, def_counts, toks)
        picked_def = _pick_callable_key(
            design,
            list(def_counts.keys()),
            current_hier_path=current_hier_path,
            file=file,
            line=int(line),
            word=word,
            site_counts=def_counts,
        )
    details = []
    for key in sorted(merged.keys()):
        loc = design.callable_defs.get(key)
        details.append(
            {
                "key": key,
                "kind": design.callable_kinds.get(key, ""),
                "name": design.callable_names.get(key, ""),
                "site_hits": int(merged.get(key, 0)),
                "def_file": loc.file if loc else "",
                "def_line": int(loc.line if loc else 0),
            }
        )
    return {
        "site": {"file": file, "line": int(line), "word": word, "tokens": toks},
        "current_hier_path": current_hier_path,
        "resolved_from_site": picked_from,
        "resolved_for_definition_site": picked_def,
        "candidates": details,
    }
