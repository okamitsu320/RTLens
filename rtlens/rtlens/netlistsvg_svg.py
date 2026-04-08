"""SVG / Qt transformation utilities and yosys JSON module-key helpers.

This module contains:
- SVG annotation helpers (inject data-src attributes, inline styles for Qt)
- HTML output builder (_build_html)
- yosys JSON module-key lookup helpers (_canonical_module_name, _find_json_module_key)

These functions have no subprocess or pipeline dependencies and can be
imported independently of the full netlistsvg pipeline.
"""
from __future__ import annotations

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set

SVG_NS = "http://www.w3.org/2000/svg"
NETLISTSVG_NS = "https://github.com/nturley/netlistsvg"
ET.register_namespace("", SVG_NS)
ET.register_namespace("s", NETLISTSVG_NS)
_RTLENS_TMP_MARKERS = ("rtlens_schematic_prebuild_", "rtlens_netlistsvg_")


def _parse_src_entries(src: str) -> List[Dict[str, object]]:
    """Parse a pipe-separated src string into a list of {raw, file, line} dicts."""
    out: List[Dict[str, object]] = []
    for chunk in (src or "").split("|"):
        chunk = chunk.strip()
        m = re.match(r"^(.*?):(\d+)", chunk)
        if not m:
            out.append({"raw": chunk, "file": "", "line": 1})
            continue
        out.append({"raw": chunk, "file": m.group(1), "line": int(m.group(2))})
    return out


def _is_rtlens_generated_tmp_src(path_text: str) -> bool:
    """Return True when a source path points to rtlens-generated temp files."""
    text = str(path_text or "")
    if not text:
        return False
    return any(marker in text for marker in _RTLENS_TMP_MARKERS)


def _normalize_svg_src_for_ui(raw_src: str, fallback: str = "") -> str:
    """Return a cleaned 'file:line' src string suitable for UI display.

    Filters out temp-directory paths and returns fallback when the file does
    not exist on disk.
    """
    src = str(raw_src or "").strip()
    fb = str(fallback or "").strip()
    if not src:
        return fb
    m = re.match(r"^(.*?):(\d+)", src)
    if not m:
        return fb or src
    f = m.group(1)
    line = m.group(2)
    if _is_rtlens_generated_tmp_src(f):
        return fb or ""
    if os.path.isfile(f):
        return f"{f}:{line}"
    return fb or src


def _normalize_requested_module_for_json_lookup(name: str) -> str:
    """Strip scope prefixes (e.g. 'module:' or ' @ instance') from a module name."""
    req = str(name or "").strip()
    if not req:
        return ""
    if " @ " in req:
        req = req.split(" @ ", 1)[0].strip()
    if req.startswith("module:"):
        req = req.split(":", 1)[1].strip()
    return req


def _extract_svg_external_port_ids(svg_text: str) -> Set[str]:
    """Return the set of cell IDs that correspond to external ports in the SVG."""
    ids: Set[str] = set()
    if not svg_text:
        return ids
    rx = re.compile(r"<g[^>]*\bs:type=\"(?:inputExt|outputExt|inoutExt)\"[^>]*\sid=\"cell_([^\"]+)\"")
    for m in rx.finditer(svg_text):
        name = str(m.group(1) or "").strip()
        if name:
            ids.add(name)
    return ids


def _find_json_module_key_by_svg_ports(modules: Dict[str, dict], svg_text: str) -> str:
    """Find the yosys JSON module key that best matches the external ports in svg_text."""
    ext_ids = _extract_svg_external_port_ids(svg_text)
    if not ext_ids:
        return ""
    best_key = ""
    best_score = 0
    for key, mod in modules.items():
        if not isinstance(mod, dict):
            continue
        ports = mod.get("ports", {})
        if not isinstance(ports, dict):
            continue
        score = sum(1 for name in ext_ids if name in ports)
        if score > best_score:
            best_score = score
            best_key = key
    return best_key if best_score > 0 else ""


def _canonical_module_name(name: str) -> str:
    """Strip leading backslash escapes from a yosys module/cell name."""
    s = str(name or "").strip()
    while s.startswith("\\"):
        s = s[1:]
    return s


def _find_json_module_key(modules: Dict[str, dict], requested: str) -> str:
    """Return the exact key in *modules* matching *requested*, using canonical comparison."""
    req = str(requested or "").strip()
    if not req:
        return ""
    if req in modules:
        return req
    canon_req = _canonical_module_name(req)
    for key in modules.keys():
        if _canonical_module_name(key) == canon_req:
            return key
    return ""


def _inject_svg_data_src_from_json(svg_text: str, json_path: Path, module_name: str) -> str:
    """Annotate SVG elements with data-src / data-port-dir attributes from yosys JSON.

    Reads the yosys JSON at *json_path*, resolves the module matching *module_name*,
    and injects ``data-src``, ``data-port-dir``, ``data-net-label``, and
    ``data-net-src`` attributes on matching ``<g>`` elements.  Returns the
    annotated SVG string (unchanged if annotation is not possible).
    """
    # Repair malformed attributes from older cached SVGs.
    svg_text = re.sub(r'"\s*/\s+(data-(?:net-label|net-src|src|port-dir)=)', r'" \1', str(svg_text or ""))

    def _fix_empty_svg_tag(m: re.Match[str]) -> str:
        tag = m.group(1)
        attrs = m.group(2)
        body = str(attrs or "")
        if body.rstrip().endswith("/"):
            return f"<{tag}{body}>"
        return f"<{tag}{body}/>"

    for t in ("line", "polyline", "polygon", "path", "circle", "ellipse", "rect"):
        svg_text = re.sub(
            rf"<({t})([^<>]*\sdata-(?:net-label|net-src|src|port-dir)=[^<>]*)>",
            _fix_empty_svg_tag,
            svg_text,
        )

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return svg_text
    modules = data.get("modules", {})
    if not isinstance(modules, dict) or not modules:
        return svg_text

    req = _normalize_requested_module_for_json_lookup(module_name)
    key = _find_json_module_key(modules, req)
    if not key:
        key = _find_json_module_key_by_svg_ports(modules, svg_text)
    if not key:
        key = next(iter(modules.keys()))
    mod = modules.get(key, {})
    if not isinstance(mod, dict):
        return svg_text

    module_src = _normalize_svg_src_for_ui(str((mod.get("attributes") or {}).get("src", "")))
    module_src_is_real = bool(module_src and re.match(r"^(.*?):(\d+)$", module_src) and os.path.isfile(module_src.split(":", 1)[0]))
    id_to_src: Dict[str, str] = {}
    id_to_port_dir: Dict[str, str] = {}
    bit_to_label: Dict[str, str] = {}
    bit_to_src: Dict[str, str] = {}

    def _append_unique(dst: List[str], value: str) -> None:
        v = str(value or "").strip()
        if v and v not in dst:
            dst.append(v)

    def _is_real_src(src: str) -> bool:
        m = re.match(r"^(.*?):(\d+)$", str(src or "").strip())
        return bool(m and os.path.isfile(m.group(1)))

    def _bits_key(bits: object) -> str:
        if not isinstance(bits, list):
            return ""
        vals: List[str] = []
        for b in bits:
            try:
                vals.append(str(int(b)))
            except Exception:
                vals.append(str(b))
        return ",".join(vals)

    netnames = mod.get("netnames", {})
    if isinstance(netnames, dict):
        for nname, net in netnames.items():
            if not isinstance(net, dict):
                continue
            try:
                hide_name = int(net.get("hide_name", 0))
            except Exception:
                hide_name = 0
            if hide_name != 0:
                continue
            bits_key = _bits_key(net.get("bits", []))
            if not bits_key:
                continue
            nsrc = _normalize_svg_src_for_ui(str((net.get("attributes") or {}).get("src", "")), fallback=module_src)
            if not _is_real_src(nsrc):
                continue
            label = str(nname or "").strip()
            if not label:
                continue
            bit_to_label[bits_key] = label
            bit_to_src[bits_key] = nsrc

    cells = mod.get("cells", {})
    if isinstance(cells, dict):
        for cell_name, cell in cells.items():
            if not isinstance(cell, dict):
                continue
            csrc_raw = str((cell.get("attributes") or {}).get("src", ""))
            csrc = _normalize_svg_src_for_ui(csrc_raw, fallback=module_src)
            conn_src: List[str] = []
            conns = cell.get("connections", {})
            if isinstance(conns, dict):
                for bits in conns.values():
                    bits_key = _bits_key(bits)
                    if bits_key in bit_to_src:
                        _append_unique(conn_src, bit_to_src[bits_key])
                    if isinstance(bits, list):
                        for b in bits:
                            try:
                                bkey = str(int(b))
                            except Exception:
                                bkey = str(b)
                            if bkey in bit_to_src:
                                _append_unique(conn_src, bit_to_src[bkey])
            merged: List[str] = []
            csrc_is_real = _is_real_src(csrc)
            # If cell src falls back to module-level coarse source, prefer
            # net-derived src candidates first for better source jump accuracy.
            if conn_src and (not csrc_is_real or (module_src_is_real and csrc == module_src)):
                for s in conn_src:
                    _append_unique(merged, s)
            if csrc:
                _append_unique(merged, csrc)
            for s in conn_src:
                _append_unique(merged, s)
            if merged:
                id_to_src[f"cell_{cell_name}"] = "|".join(merged)
    ports = mod.get("ports", {})
    if isinstance(ports, dict):
        for port_name, port in ports.items():
            if not isinstance(port, dict):
                continue
            psrc_raw = str((port.get("attributes") or {}).get("src", ""))
            psrc = _normalize_svg_src_for_ui(psrc_raw, fallback=module_src)
            if psrc:
                # Prefer explicit module-port source over synthesized pseudo-cell src.
                id_to_src[f"cell_{port_name}"] = psrc
            attrs = port.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}
            pdir = str(attrs.get("rtlens_orig_direction", "") or port.get("direction", "")).strip().lower()
            if pdir not in {"input", "output", "inout"}:
                pdir = "input"
            id_to_port_dir[f"cell_{port_name}"] = pdir

    if not id_to_src and not module_src and not id_to_port_dir and not bit_to_label:
        return svg_text

    def _resolve(elem_id: str) -> str:
        eid = str(elem_id or "").strip()
        if not eid:
            return ""
        if eid in id_to_src:
            return id_to_src[eid]
        if eid.startswith("port_"):
            base = eid[5:].split("~", 1)[0]
            src = id_to_src.get(f"cell_{base}", "")
            if src:
                return src
        if eid.startswith("cell_"):
            return id_to_src.get(eid, module_src)
        return ""

    tag_rx = re.compile(r"<([A-Za-z_][\w:.-]*)([^>]*\sid=\"([^\"]+)\"[^>]*)>")
    changed = 0

    def _append_extras_to_attrs(attrs: str, extras: List[str]) -> str:
        if not extras:
            return attrs
        raw = str(attrs or "")
        has_self_close = raw.rstrip().endswith("/")
        if has_self_close:
            core = raw.rstrip()
            core = core[:-1].rstrip()
            return f"{core} {' '.join(extras)} /"
        return f"{raw} {' '.join(extras)}"

    def _repl(m: re.Match[str]) -> str:
        nonlocal changed
        attrs = m.group(2)
        eid = m.group(3)
        extras: List[str] = []

        if " data-src=" not in attrs:
            src = _resolve(eid)
            if src:
                extras.append(f'data-src="{html.escape(src, quote=True)}"')
                changed += 1

        if " data-port-dir=" not in attrs:
            pdir = id_to_port_dir.get(eid, "")
            if pdir:
                extras.append(f'data-port-dir="{html.escape(pdir, quote=True)}"')
                changed += 1

        class_m = re.search(r'\sclass="([^"]+)"', attrs)
        if class_m:
            class_tokens = [tok.strip() for tok in class_m.group(1).split() if tok.strip()]
            net_label = ""
            net_src = ""
            for tok in class_tokens:
                if not tok.startswith("net_"):
                    continue
                bits_key = tok[4:]
                if bits_key in bit_to_label:
                    net_label = bit_to_label[bits_key]
                    net_src = bit_to_src.get(bits_key, "")
                    break
            if net_label and " data-net-label=" not in attrs:
                extras.append(f'data-net-label="{html.escape(net_label, quote=True)}"')
                changed += 1
            if net_src and " data-net-src=" not in attrs:
                extras.append(f'data-net-src="{html.escape(net_src, quote=True)}"')
                changed += 1

        if not extras:
            return m.group(0)
        new_attrs = _append_extras_to_attrs(attrs, extras)
        return f"<{m.group(1)}{new_attrs}>"

    out = tag_rx.sub(_repl, svg_text)
    if bit_to_label:
        class_rx = re.compile(r"<([A-Za-z_][\w:.-]*)([^>]*\sclass=\"([^\"]*net_[^\"]*)\"[^>]*)>")

        def _repl_class(m: re.Match[str]) -> str:
            nonlocal changed
            attrs = m.group(2)
            class_attr = m.group(3)
            class_tokens = [tok.strip() for tok in str(class_attr).split() if tok.strip()]
            net_label = ""
            net_src = ""
            for tok in class_tokens:
                if not tok.startswith("net_"):
                    continue
                bits_key = tok[4:]
                if bits_key in bit_to_label:
                    net_label = bit_to_label[bits_key]
                    net_src = bit_to_src.get(bits_key, "")
                    break
            extras: List[str] = []
            if net_label and " data-net-label=" not in attrs:
                extras.append(f'data-net-label="{html.escape(net_label, quote=True)}"')
                changed += 1
            if net_src and " data-net-src=" not in attrs:
                extras.append(f'data-net-src="{html.escape(net_src, quote=True)}"')
                changed += 1
            if not extras:
                return m.group(0)
            new_attrs = _append_extras_to_attrs(attrs, extras)
            return f"<{m.group(1)}{new_attrs}>"

        out = class_rx.sub(_repl_class, out)
    return out if changed > 0 else svg_text


def _build_html(svg_text: str, source_files: List[str], out_path: Path) -> None:
    """Write a self-contained HTML file embedding *svg_text* with a source-preview panel."""
    source_map: Dict[str, List[str]] = {}
    for path in source_files:
        ap = os.path.abspath(path)
        try:
            source_map[ap] = Path(ap).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
    source_json = json.dumps(source_map)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(out_path.name)}</title>
  <style>
    :root {{
      --bg: #f3efe4;
      --panel: #fffaf0;
      --ink: #1d1b16;
      --muted: #70695a;
      --accent: #0f766e;
      --line: #d7cdb9;
      --highlight: #ffe08a;
    }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Noto Sans JP", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, #fff6d8 0, transparent 22rem),
        linear-gradient(135deg, #efe7d4, var(--bg));
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(22rem, 1fr);
      min-height: 100vh;
    }}
    .canvas {{
      overflow: auto;
      padding: 1rem;
      border-right: 1px solid var(--line);
    }}
    .sidebar {{
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      padding: 1rem;
      min-width: 0;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 0.9rem 1rem;
      box-shadow: 0 10px 24px rgba(53, 42, 15, 0.08);
    }}
    .label {{
      font-size: 0.78rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 0.4rem;
    }}
    .value {{
      word-break: break-all;
      font-size: 0.95rem;
    }}
    .src-list button {{
      display: block;
      width: 100%;
      margin: 0.35rem 0 0;
      text-align: left;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: white;
      padding: 0.5rem 0.65rem;
      cursor: pointer;
      font: inherit;
    }}
    .src-list button:hover {{
      border-color: var(--accent);
    }}
    .src-list button.active {{
      border-color: var(--accent);
      box-shadow: inset 0 0 0 1px rgba(15, 118, 110, 0.28);
      background: #f2fffd;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "Iosevka Fixed", "Noto Sans Mono", monospace;
      font-size: 0.88rem;
      line-height: 1.45;
    }}
    .line {{
      display: grid;
      grid-template-columns: 4rem 1fr;
      gap: 0.6rem;
      padding: 0 0.35rem;
      border-radius: 6px;
    }}
    .line.current {{
      background: var(--highlight);
    }}
    .ln {{
      color: var(--muted);
      text-align: right;
      user-select: none;
    }}
    .hint {{
      color: var(--muted);
      font-size: 0.9rem;
    }}
    svg [data-src] {{
      cursor: pointer;
    }}
    svg [data-src]:hover {{
      filter: drop-shadow(0 0 4px rgba(15, 118, 110, 0.45));
    }}
    svg [data-src].selected {{
      filter: drop-shadow(0 0 5px rgba(198, 59, 19, 0.58));
      stroke: #c63b13 !important;
      stroke-width: 2 !important;
    }}
    svg [data-port-dir="input"] path,
    svg [data-port-dir="input"] polygon,
    svg [data-port-dir="input"] polyline,
    svg [data-port-dir="input"] rect,
    svg [data-port-dir="input"] circle,
    svg [data-port-dir="input"] ellipse {{
      stroke: #1d4ed8 !important;
    }}
    svg [data-port-dir="input"] text {{
      fill: #1d4ed8 !important;
    }}
    svg [data-port-dir="output"] path,
    svg [data-port-dir="output"] polygon,
    svg [data-port-dir="output"] polyline,
    svg [data-port-dir="output"] rect,
    svg [data-port-dir="output"] circle,
    svg [data-port-dir="output"] ellipse {{
      stroke: #b45309 !important;
    }}
    svg [data-port-dir="output"] text {{
      fill: #b45309 !important;
    }}
    svg [data-port-dir="inout"] path,
    svg [data-port-dir="inout"] polygon,
    svg [data-port-dir="inout"] polyline,
    svg [data-port-dir="inout"] rect,
    svg [data-port-dir="inout"] circle,
    svg [data-port-dir="inout"] ellipse {{
      stroke: #0f766e !important;
    }}
    svg [data-port-dir="inout"] text {{
      fill: #0f766e !important;
    }}
  </style>
</head>
<body>
  <div class="layout">
    <div class="canvas">{svg_text}</div>
    <div class="sidebar">
      <div class="card">
        <div class="label">Selected src</div>
        <div class="value" id="selected-src">Click a cell node in the schematic.</div>
      </div>
      <div class="card">
        <div class="label">All src entries</div>
        <div class="src-list" id="src-list"></div>
      </div>
      <div class="card">
        <div class="label">Source preview</div>
        <div id="source-preview" class="hint">No source selected.</div>
      </div>
    </div>
  </div>
  <script>
    const SOURCE_MAP = {source_json};
    const selectedSrcEl = document.getElementById('selected-src');
    const sourcePreviewEl = document.getElementById('source-preview');
    const srcListEl = document.getElementById('src-list');
    let rtlensBridge = null;
    let selectedRaw = '';

    if (window.qt && typeof QWebChannel !== 'undefined') {{
      new QWebChannel(qt.webChannelTransport, function(channel) {{
        rtlensBridge = channel.objects.rtlensBridge;
      }});
    }}

    function parseEntries(raw) {{
      return raw.split('|').map((chunk) => {{
        const text = chunk.trim();
        const match = text.match(/^(.*?):(\\d+)/);
        if (!match) {{
          return {{ raw: text, file: '', line: 1 }};
        }}
        return {{ raw: text, file: match[1], line: Number(match[2]) }};
      }});
    }}

    function escapeHtml(s) {{
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}

    function renderSource(file, line) {{
      const lines = SOURCE_MAP[file];
      if (!lines) {{
        sourcePreviewEl.innerHTML = '<div class="hint">Source not bundled: ' + escapeHtml(file) + '</div>';
        return;
      }}
      const start = Math.max(1, line - 4);
      const end = Math.min(lines.length, line + 4);
      const htmlLines = [];
      for (let i = start; i <= end; i += 1) {{
        const cls = i === line ? 'line current' : 'line';
        htmlLines.push('<div class="' + cls + '"><span class="ln">' + i + '</span><span>' + escapeHtml(lines[i - 1]) + '</span></div>');
      }}
      sourcePreviewEl.innerHTML = '<div class="value" style="margin-bottom:0.6rem">' + escapeHtml(file + ':' + line) + '</div><pre>' + htmlLines.join('') + '</pre>';
    }}

    function openSrc(entry) {{
      renderSource(entry.file, entry.line);
      if (rtlensBridge && rtlensBridge.jumpToSource) {{
        rtlensBridge.jumpToSource(entry.file, entry.line, entry.raw);
      }}
    }}

    function setSelectedSvgTarget(raw) {{
      selectedRaw = raw || '';
      document.querySelectorAll('svg [data-src]').forEach((el) => {{
        if ((el.getAttribute('data-src') || '') === selectedRaw) {{
          el.classList.add('selected');
        }} else {{
          el.classList.remove('selected');
        }}
      }});
    }}

    function selectSrc(raw, chosenIdx = 0, jumpNow = false) {{
      const entries = parseEntries(raw);
      selectedSrcEl.textContent = raw;
      setSelectedSvgTarget(raw);
      srcListEl.innerHTML = '';
      entries.forEach((entry, idx) => {{
        const button = document.createElement('button');
        button.textContent = entry.raw;
        button.addEventListener('click', () => {{
          renderSource(entry.file, entry.line);
          srcListEl.querySelectorAll('button').forEach((b) => b.classList.remove('active'));
          button.classList.add('active');
        }});
        button.addEventListener('dblclick', () => openSrc(entry));
        srcListEl.appendChild(button);
        if (idx === chosenIdx) {{
          button.classList.add('active');
          renderSource(entry.file, entry.line);
        }}
      }});
      if (entries.length && jumpNow) {{
        openSrc(entries[Math.max(0, Math.min(chosenIdx, entries.length - 1))]);
      }}
    }}

    document.addEventListener('click', (event) => {{
      const target = event.target.closest('[data-src]');
      if (!target) {{
        return;
      }}
      selectSrc(target.getAttribute('data-src') || '', 0, false);
    }});

    document.addEventListener('dblclick', (event) => {{
      const target = event.target.closest('[data-src]');
      if (!target) {{
        return;
      }}
      selectSrc(target.getAttribute('data-src') || '', 0, true);
    }});
  </script>
</body>
</html>
"""
    out_path.write_text(html_text, encoding="utf-8")


def _inline_svg_styles_for_qt(svg_text: str) -> str:
    """Inline CSS-like styles into SVG attributes for Qt WebEngine rendering.

    Qt's WebEngine does not support CSS ``[attr]`` selectors on SVG elements,
    so port-direction colour coding and default stroke/fill values must be
    applied directly as XML attributes.  Returns the modified SVG string, or
    the original string unchanged if XML parsing fails.
    """
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return svg_text

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for elem in root.iter():
        tag = local(elem.tag)
        cls = elem.attrib.get("class", "")
        if tag in {"line", "path", "circle", "rect", "polygon", "polyline", "ellipse"}:
            elem.attrib.setdefault("stroke", "#000000")
            elem.attrib.setdefault("fill", "none")
            elem.attrib.setdefault("stroke-width", "1")
        elif tag == "text":
            elem.attrib.setdefault("fill", "#000000")
            elem.attrib.setdefault("stroke", "none")
        if "splitjoinBody" in cls.split():
            elem.attrib["fill"] = "#000000"
            elem.attrib.setdefault("stroke", "none")

    port_palette = {
        "input": {"stroke": "#1d4ed8", "fill": "#dbeafe", "text": "#1d4ed8"},
        "output": {"stroke": "#b45309", "fill": "#ffedd5", "text": "#b45309"},
        "inout": {"stroke": "#0f766e", "fill": "#d1fae5", "text": "#0f766e"},
    }

    def _port_dir(elem: ET.Element) -> str:
        raw = str(elem.attrib.get("data-port-dir", "")).strip().lower()
        if raw in {"input", "output", "inout"}:
            return raw
        stype = str(
            elem.attrib.get(f"{{{NETLISTSVG_NS}}}type", elem.attrib.get("s:type", ""))
        ).strip()
        if stype == "inputExt":
            return "input"
        if stype == "outputExt":
            return "output"
        if stype == "inoutExt":
            return "inout"
        return ""

    def _color_port_group(group: ET.Element, pdir: str) -> None:
        pal = port_palette.get(pdir)
        if not pal:
            return
        for node in group.iter():
            t = local(node.tag)
            if t in {"path", "polygon", "polyline", "rect", "circle", "ellipse", "line"}:
                node.attrib["stroke"] = pal["stroke"]
                old_fill = str(node.attrib.get("fill", "none")).strip().lower()
                if old_fill in {"none", ""} and t in {"path", "polygon", "polyline", "rect"}:
                    node.attrib["fill"] = pal["fill"]
            elif t == "text":
                node.attrib["fill"] = pal["text"]
                node.attrib["stroke"] = "none"

    for elem in root.iter():
        if local(elem.tag) != "g":
            continue
        pdir = _port_dir(elem)
        if pdir:
            _color_port_group(elem, pdir)

    return ET.tostring(root, encoding="unicode")
