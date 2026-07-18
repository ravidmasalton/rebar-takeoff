"""
Rebar quantity takeoff from vector structural-drawing PDFs.

All processing is in-memory and stateless. `Analysis(pdf_bytes)` parses the
PDF once (an optional `progress` callback receives `(phase, percent)` so the
caller can stream progress to a client); `.response()` returns a
JSON-serialisable dict with two sections: `items` (one row per rebar callout)
and `summary` (aggregates and flags for manual verification).

Two expensive steps are deliberately *not* part of parsing, because profiling
showed they dominate wall time while contributing nothing to counts/weights:

* `.shapes()` — bar-shape classification (~60% of a naive analyze: O(n²)
  connected-components over the vector linework near every callout). Computed
  once on first call and cached.
* `.render_evidence(item_id)` — rasterises the evidence crop for a single
  item on demand (crops are never bulk-rendered — a large drawing can have
  dozens of callouts and holding every PNG at once exhausts memory on small
  servers).

`analyze_pdf(pdf_bytes)` remains as a convenience wrapper returning just the
response dict.

Domain notes
------------
* Callouts follow the pattern ``[n]Ø<dia>[@<spacing>]`` with a nearby
  ``L=<length>`` token. In the source PDF the diameter symbol is the byte
  ``ö`` (it is drawn as the diameter glyph Ø). All linear units are cm; the
  bar diameter is mm.
* RTL / bidi PDFs emit visually-reversed tokens, e.g. ``02@01ö`` for
  ``ö10@20`` and ``056=L`` for ``L=650``. We detect this per token by testing
  both the token and its reverse against the known patterns.
"""

from __future__ import annotations

import io
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from statistics import median
from typing import Callable, Optional

import pdfplumber

logger = logging.getLogger(__name__)

# --- Physical constants ----------------------------------------------------

# Weight per metre of a round steel bar: kg/m = 0.006165 * d^2, with d in mm.
STEEL_KG_PER_M_COEFF = 0.006165

# The only diameters (mm) used in practice. A parsed diameter outside this set
# is a signal that a bidi-reversed token was read in the wrong direction.
VALID_DIAMETERS = {8, 10, 12, 14, 16, 20, 25}

# Flags that warrant a manual-verification warning in the summary. Quantity
# flags only: shape resolution has no bearing on the totals, and listing it
# floods the panel on drawings with hard-to-chain linework.
SUMMARY_WARN_FLAGS = ("estimated_width", "unresolved_width", "missing_length")

# --- Geometry / heuristic thresholds ---------------------------------------

# A distributed callout's zone width must be at least this many cm to be a
# plausible distribution span (filters out stray small dimension numbers).
MIN_ZONE_WIDTH_CM = 60.0

# Max gap (in PDF points, along the reading direction) between a callout and
# its L= token. ``nöd@s L=len`` is one printed phrase, so the two tokens sit
# on the same printed line a bounded gap apart.
LENGTH_MATCH_RADIUS = 160.0

# Perpendicular offset tolerance for "same printed line", as a fraction of
# the larger of the two glyph heights.
LINE_PERP_TOL = 0.8

# Max distance (in PDF points) to associate a standalone width number with a
# distributed callout.
WIDTH_MATCH_RADIUS = 260.0

# Radius (in PDF points) around a callout in which we look for its bar polyline.
SHAPE_SEARCH_RADIUS = 220.0

# Two segment endpoints closer than this (points) are treated as connected.
JOINT_TOLERANCE = 3.0

# A circled element-mark number sits inside a roughly square vector shape no
# larger than this (points) on a side.
MARK_CIRCLE_MAX_SIDE = 42.0

# --- Evidence-crop rendering -----------------------------------------------

# DPI at which the evidence crop is rasterised. High enough that the numbers
# read like a zoomed screenshot; only the crop region is rendered, never the
# full page.
RENDER_DPI = 200

# Margin (points) added around the tight bounding box of the evidence tokens.
CROP_MARGIN_PT = 40.0

# Hard cap (points) on either side of the crop box, centred on the callout. A
# mispaired far-away width token would otherwise blow the region — and the
# rendered bitmap — up to a large slice of the page.
CROP_MAX_BOX_PT = 700.0

# Longest side (px) the stored crop is downscaled to, to keep the JSON light.
CROP_MAX_PX = 1600

# Max distance (points) from the width number to its dimension-arrow line.
ARROW_SEARCH_RADIUS = 80.0

# A crop counts as blank when fewer than this fraction of its pixels are
# non-white before overlays are drawn.
CROP_MIN_INK_FRACTION = 0.01

# --- Token patterns --------------------------------------------------------

# Accept the source byte 'ö' plus the real diameter glyphs, in case a given
# PDF encodes the symbol directly.
_DIA_SYMBOLS = "öÖøØⵁ⌀∅"
_SYM = f"[{_DIA_SYMBOLS}]"

# [n]Ø<dia>[@<spacing>] optionally followed by an inline L=<length>.
CALLOUT_RE = re.compile(
    rf"^(?P<n>\d+)?{_SYM}(?P<dia>\d+)(?:@(?P<sp>\d+))?"
    r"(?:\s*L\s*=\s*(?P<len>\d+(?:\.\d+)?))?$"
)
LENGTH_RE = re.compile(r"^L\s*=\s*(?P<len>\d+(?:\.\d+)?)$")
NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


# --- Data model ------------------------------------------------------------


@dataclass
class Token:
    """A normalised word from the PDF with its page position."""

    text: str            # normalised (de-reversed) text
    raw: str             # original extracted text
    x0: float
    x1: float
    top: float
    bottom: float
    page: int

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass
class Segment:
    """A straight vector line segment with its stroke width."""

    x0: float
    y0: float
    x1: float
    y1: float
    linewidth: float
    page: int

    @property
    def length(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)

    @property
    def midx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def midy(self) -> float:
        return (self.y0 + self.y1) / 2.0


@dataclass
class Item:
    """One rebar callout result row."""

    id: int
    diameter: int
    spacing: Optional[int]
    length_cm: Optional[float]
    zone_width_cm: Optional[float]
    count: int
    # Filled by the separate /shapes pass, not by /analyze (see module doc).
    shape: Optional[str] = None
    shape_segments_pts: list = field(default_factory=list)
    total_m: float = 0.0
    weight_kg: float = 0.0
    source: str = "explicit"          # explicit | measured_width | estimated
    counted: bool = True              # included in the summary totals?
    position: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)


# --- Bidi / normalisation --------------------------------------------------


def normalize_token(raw: str) -> str:
    """
    Return the reading-order form of a callout/length token.

    RTL PDFs reverse whole tokens. Because the callout/length patterns are
    strongly anchored (Ø, @, ``L=``), we can tell orientation by which of the
    token or its reverse matches. Reversing the *whole* token also restores
    the embedded numbers (``02@01ö`` -> ``ö10@20``). Plain number tokens are
    left untouched: bidi keeps standalone digit runs LTR, and a bare number is
    ambiguous under reversal.

    Diameter sanity resolves the remaining ambiguity: both a token and its
    reverse can match the callout pattern (``41ö2`` <-> ``2ö14``), so the
    orientation whose diameter is a real bar size wins.
    """
    stripped = raw.strip()
    if NUMBER_RE.match(stripped):
        return stripped
    reversed_text = stripped[::-1]

    # Prefer a callout orientation with a valid diameter; fall back to any
    # callout match, then to a length match.
    callout_fallback: Optional[str] = None
    for cand in (stripped, reversed_text):
        m = CALLOUT_RE.match(cand)
        if not m:
            continue
        if int(m.group("dia")) in VALID_DIAMETERS:
            return cand
        if callout_fallback is None:
            callout_fallback = cand
    if callout_fallback is not None:
        return callout_fallback

    for cand in (stripped, reversed_text):
        if LENGTH_RE.match(cand):
            return cand
    return stripped


# --- Extraction ------------------------------------------------------------


def _extract_tokens(page, page_number: int) -> list[Token]:
    tokens: list[Token] = []
    for w in page.extract_words(use_text_flow=False, keep_blank_chars=False):
        raw = w["text"]
        tokens.append(
            Token(
                text=normalize_token(raw),
                raw=raw,
                x0=w["x0"],
                x1=w["x1"],
                top=w["top"],
                bottom=w["bottom"],
                page=page_number,
            )
        )
    return tokens


def _extract_segments(page, page_number: int) -> list[Segment]:
    """Collect straight line segments from lines, rect edges and curve pts."""
    segments: list[Segment] = []

    for ln in page.lines:
        segments.append(
            Segment(
                x0=ln["x0"], y0=ln["top"], x1=ln["x1"], y1=ln["bottom"],
                linewidth=ln.get("linewidth", 1.0) or 1.0, page=page_number,
            )
        )

    # Curves (bezier / polyline) are stored as a list of points; treat each
    # consecutive pair as a straight segment. Good enough for shape topology.
    for cv in page.curves:
        pts = cv.get("pts") or []
        lw = cv.get("linewidth", 1.0) or 1.0
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            segments.append(
                Segment(x0=ax, y0=ay, x1=bx, y1=by, linewidth=lw, page=page_number)
            )

    return segments


# --- Circled element-mark detection ----------------------------------------


def _circle_boxes(page) -> list[tuple[float, float, float, float]]:
    """Bounding boxes of small, roughly-square vector shapes (element marks)."""
    boxes: list[tuple[float, float, float, float]] = []
    for obj in list(page.curves) + list(page.rects):
        x0, x1 = obj["x0"], obj["x1"]
        top, bottom = obj["top"], obj["bottom"]
        w, h = x1 - x0, bottom - top
        if w <= 0 or h <= 0:
            continue
        if max(w, h) <= MARK_CIRCLE_MAX_SIDE and 0.6 <= (w / h) <= 1.6:
            boxes.append((x0, top, x1, bottom))
    return boxes


def _is_circled(tok: Token, boxes: list[tuple[float, float, float, float]]) -> bool:
    for x0, top, x1, bottom in boxes:
        if x0 <= tok.cx <= x1 and top <= tok.cy <= bottom:
            return True
    return False


# --- Shape classification --------------------------------------------------


def _chain_segments(segs: list[Segment]) -> Optional[list[Segment]]:
    """
    Order a set of segments into a single open polyline by joining endpoints.

    Returns the ordered chain, or None if the segments do not form one simple
    open path (branching, disconnected, or closed loop -> ambiguous).
    """
    if not segs:
        return None
    if len(segs) == 1:
        return list(segs)

    def close(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1]) <= JOINT_TOLERANCE

    # Represent each segment by its two endpoints; greedily walk the path.
    remaining = list(segs)
    # Start from a segment whose one endpoint is unshared (a path end).
    endpoints = []
    for s in remaining:
        endpoints.append((s.x0, s.y0))
        endpoints.append((s.x1, s.y1))

    def degree(pt):
        return sum(1 for e in endpoints if close(pt, e))

    start_seg = None
    start_pt = None
    for s in remaining:
        for pt in ((s.x0, s.y0), (s.x1, s.y1)):
            if degree(pt) == 1:  # this endpoint only belongs to this segment
                start_seg, start_pt = s, pt
                break
        if start_seg:
            break
    if start_seg is None:
        return None  # closed loop or fully shared -> ambiguous

    chain = [start_seg]
    remaining.remove(start_seg)
    cur = (start_seg.x1, start_seg.y1) if close(start_pt, (start_seg.x0, start_seg.y0)) \
        else (start_seg.x0, start_seg.y0)

    while remaining:
        nxt = None
        for s in remaining:
            if close(cur, (s.x0, s.y0)):
                nxt, cur = s, (s.x1, s.y1)
                break
            if close(cur, (s.x1, s.y1)):
                nxt, cur = s, (s.x0, s.y0)
                break
        if nxt is None:
            return None  # disconnected -> ambiguous
        chain.append(nxt)
        remaining.remove(nxt)

    return chain


def _classify_shape(chain: Optional[list[Segment]]) -> tuple[str, list[float]]:
    """Classify an ordered polyline into straight / L / U / Z."""
    if not chain:
        return "needs review", []

    seg_lengths = [round(s.length, 1) for s in chain]
    n = len(chain)

    if n == 1:
        return "straight", seg_lengths
    if n == 2:
        return "L", seg_lengths
    if n == 3:
        # Distinguish U (both bends the same way) from Z (opposite bends) using
        # the sign of the cross product at each interior vertex.
        turns = []
        for i in range(len(chain) - 1):
            a, b = chain[i], chain[i + 1]
            v1 = (a.x1 - a.x0, a.y1 - a.y0)
            v2 = (b.x1 - b.x0, b.y1 - b.y0)
            cross = v1[0] * v2[1] - v1[1] * v2[0]
            turns.append(cross)
        if all(abs(t) < 1e-6 for t in turns):
            return "straight", seg_lengths
        same_dir = (turns[0] > 0) == (turns[1] > 0)
        return ("U" if same_dir else "Z"), seg_lengths

    return "needs review", seg_lengths


def _connected_components(segs: list[Segment]) -> list[list[Segment]]:
    """Group segments into connected components by shared endpoints."""
    parent = list(range(len(segs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    def touches(a: Segment, b: Segment) -> bool:
        ends_a = ((a.x0, a.y0), (a.x1, a.y1))
        ends_b = ((b.x0, b.y0), (b.x1, b.y1))
        return any(
            math.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= JOINT_TOLERANCE
            for pa in ends_a for pb in ends_b
        )

    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if touches(segs[i], segs[j]):
                union(i, j)

    groups: dict[int, list[Segment]] = {}
    for i, s in enumerate(segs):
        groups.setdefault(find(i), []).append(s)
    return list(groups.values())


def _bold_threshold(widths: list[float]) -> float:
    """
    Lower bound of the "bold" (bar) stroke-width class.

    Drawings use two stroke classes near a callout: thin annotation (grid,
    leaders, dimension lines) and bold rebar. Splitting them at the largest
    *relative* gap between adjacent distinct widths adapts to whatever
    widths a given drawing uses — unlike a median cut (thin linework
    outnumbers the bar, so the median lands in the thin class and lets every
    stray line pollute the component) or a fraction-of-max cut (a thicker
    neighbouring bar inside the search radius pushes the cut above this
    callout's own bar). When no clearly separated class exists (ratio <
    1.5), everything is a candidate and chaining decides.
    """
    uniq = sorted(set(widths))
    if len(uniq) == 1:
        return uniq[0]
    best_ratio, best_split = 0.0, uniq[0]
    for a, b in zip(uniq, uniq[1:]):
        ratio = b / max(a, 1e-6)
        if ratio > best_ratio:
            best_ratio, best_split = ratio, b
    return best_split if best_ratio >= 1.5 else uniq[0]


def _detect_shape(
    tok: Token, segments: list[Segment]
) -> tuple[str, list[float], Optional[list[Segment]]]:
    """Find the bold bar polyline nearest a callout and classify it.

    Returns (shape, segment_lengths, chain) where `chain` is the ordered
    polyline used for the evidence overlay (None when nothing usable is found).
    """
    near = [
        s for s in segments
        if s.page == tok.page
        and math.hypot(s.midx - tok.cx, s.midy - tok.cy) <= SHAPE_SEARCH_RADIUS
    ]
    if not near:
        return "needs review", [], None

    # Rebar is drawn bold: drop the thin annotation class, keep every bar-
    # class stroke (see _bold_threshold), then let proximity pick the right
    # bar among the surviving components.
    thresh = _bold_threshold([s.linewidth for s in near])
    bold = [s for s in near if s.linewidth >= thresh and s.length > 1.0]
    if not bold:
        return "needs review", [], None

    # A callout belongs to a single bar (one connected polyline). Pick the
    # component nearest the callout so adjacent bars don't corrupt the chain.
    components = _connected_components(bold)
    best = min(
        components,
        key=lambda comp: min(
            math.hypot(s.midx - tok.cx, s.midy - tok.cy) for s in comp
        ),
    )
    chain = _chain_segments(best)
    shape, seg_pts = _classify_shape(chain)
    return shape, seg_pts, chain


# --- Callout parsing -------------------------------------------------------


@dataclass
class _Callout:
    tok: Token
    n: Optional[int]
    diameter: int
    spacing: Optional[int]
    inline_length: Optional[float]


def _parse_callouts(tokens: list[Token]) -> list[_Callout]:
    callouts: list[_Callout] = []
    for tok in tokens:
        m = CALLOUT_RE.match(tok.text)
        if not m:
            continue
        # normalize_token already picked the orientation with a valid diameter
        # when one exists; anything still outside the whitelist is noise
        # (mis-parsed marks / decorations) and must never become a Ø2/Ø3 row.
        if int(m.group("dia")) not in VALID_DIAMETERS:
            continue
        callouts.append(
            _Callout(
                tok=tok,
                n=int(m.group("n")) if m.group("n") else None,
                diameter=int(m.group("dia")),
                spacing=int(m.group("sp")) if m.group("sp") else None,
                inline_length=float(m.group("len")) if m.group("len") else None,
            )
        )
    return callouts


def _token_orientation(tok: Token) -> str:
    """'h' for horizontally-set text, 'v' for vertical (rotated) text."""
    return "h" if (tok.x1 - tok.x0) >= (tok.bottom - tok.top) else "v"


def _interval_gap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Distance between intervals [a0,a1] and [b0,b1]; 0 when they overlap."""
    return max(0.0, max(a0, b0) - min(a1, b1))


def _pair_lengths(
    callouts: list[_Callout], tokens: list[Token]
) -> dict[int, tuple[float, Token]]:
    """
    Globally assign L= tokens to the callouts of one page.

    ``nöd@s L=len`` is one printed phrase: a callout's L= token shares its
    text orientation and sits on the same printed line (small perpendicular
    offset), a bounded gap away along the reading direction. Naive
    per-callout nearest-distance matching steals a neighbouring callout's
    L= whenever a callout has none of its own — so instead every valid
    (callout, L=) pair is scored by collinearity (perpendicular offset
    dominates, then gap) and assigned greedily best-first, each L= token
    consumed by exactly one callout. A callout with no valid candidate gets
    no length: a wrong token is never taken silently.

    Returns {callout_index: (length_cm, token)}.
    """
    length_toks = [t for t in tokens if LENGTH_RE.match(t.text)]
    candidates: list[tuple[float, int, int]] = []
    for ci, c in enumerate(callouts):
        if c.inline_length is not None:
            continue
        ori = _token_orientation(c.tok)
        for ti, t in enumerate(length_toks):
            if _token_orientation(t) != ori:
                continue
            if ori == "h":
                perp = abs(t.cy - c.tok.cy)
                perp_tol = LINE_PERP_TOL * max(c.tok.bottom - c.tok.top,
                                               t.bottom - t.top)
                gap = _interval_gap(c.tok.x0, c.tok.x1, t.x0, t.x1)
            else:
                perp = abs(t.cx - c.tok.cx)
                perp_tol = LINE_PERP_TOL * max(c.tok.x1 - c.tok.x0,
                                               t.x1 - t.x0)
                gap = _interval_gap(c.tok.top, c.tok.bottom, t.top, t.bottom)
            if perp > perp_tol or gap > LENGTH_MATCH_RADIUS:
                continue
            candidates.append((perp * 3.0 + gap, ci, ti))

    candidates.sort(key=lambda x: x[0])
    assigned: dict[int, tuple[float, Token]] = {}
    taken: set[int] = set()
    for _score, ci, ti in candidates:
        if ci in assigned or ti in taken:
            continue
        tok = length_toks[ti]
        assigned[ci] = (float(LENGTH_RE.match(tok.text).group("len")), tok)
        taken.add(ti)
    return assigned


def _find_zone_width(
    callout: _Callout,
    number_tokens: list[Token],
    used: set[int],
) -> Optional[tuple[float, int]]:
    """
    Nearest unused standalone width number >= MIN_ZONE_WIDTH_CM that shares
    the callout's text orientation (a zone's dimension number is printed
    parallel to its callout, like the rest of the annotation).
    """
    best_idx = None
    best_dist = WIDTH_MATCH_RADIUS
    ori = _token_orientation(callout.tok)
    for idx, tok in enumerate(number_tokens):
        if idx in used or tok.page != callout.tok.page:
            continue
        if _token_orientation(tok) != ori:
            continue
        value = float(tok.text)
        if value < MIN_ZONE_WIDTH_CM:
            continue
        d = math.hypot(tok.cx - callout.tok.cx, tok.cy - callout.tok.cy)
        if d <= best_dist:
            best_dist = d
            best_idx = idx
    if best_idx is None:
        return None
    return float(number_tokens[best_idx].text), best_idx


# --- Intermediate row ------------------------------------------------------


@dataclass
class _Row:
    """Mutable working row, resolved across two passes before becoming an Item."""

    id: int
    callout: _Callout
    length_cm: Optional[float]
    length_tok: Optional[Token]
    width_tok: Optional[Token]
    zone_width_cm: Optional[float]
    count: Optional[int]           # None until the estimation pass resolves it
    source: str
    flags: list


# --- Estimation (fills distributed callouts that had no measured width) -----


def _estimate_missing_widths(rows: list[_Row]) -> None:
    """
    Resolve distributed callouts that had no nearby width number.

    Estimate the zone width from the median measured width of the same
    (dia, spacing, length) group, else the same (dia, spacing) group. If no
    peer measurements exist, leave count=0 and flag it so it is shown but
    excluded from the totals.
    """
    by_dsl: dict[tuple, list[float]] = defaultdict(list)  # (dia, spacing, len)
    by_ds: dict[tuple, list[float]] = defaultdict(list)   # (dia, spacing)
    for r in rows:
        if r.source == "measured_width" and r.zone_width_cm is not None:
            len_key = round(r.length_cm) if r.length_cm is not None else None
            by_dsl[(r.callout.diameter, r.callout.spacing, len_key)].append(r.zone_width_cm)
            by_ds[(r.callout.diameter, r.callout.spacing)].append(r.zone_width_cm)

    for r in rows:
        # Only unresolved distributed callouts reach here (count still None).
        if r.count is not None:
            continue
        spacing = r.callout.spacing
        len_key = round(r.length_cm) if r.length_cm is not None else None
        key3 = (r.callout.diameter, spacing, len_key)
        key2 = (r.callout.diameter, spacing)

        if by_dsl.get(key3):
            est = median(by_dsl[key3])
        elif by_ds.get(key2):
            est = median(by_ds[key2])
        else:
            est = None

        if est is not None:
            r.zone_width_cm = round(est, 1)
            r.count = math.ceil(est / spacing) + 1
            r.flags.append("estimated_width")
        else:
            # No basis to estimate: keep it visible but out of the totals.
            r.zone_width_cm = None
            r.count = 0
            r.flags.append("unresolved_width")


# --- Evidence-crop rendering -----------------------------------------------
#
# Coordinate model (verified empirically against a shifted-mediabox PDF):
# pdfplumber reports word `x` as RAW PDF x (not shifted by the mediabox
# origin) and `top` as `page.height - pdf_y`, which IGNORES the mediabox
# y-offset. Its `page.bbox` lives in that same top-down space: the page's
# left edge is at bbox[0] (= mediabox x0) and its top edge is at bbox[1]
# (NOT necessarily 0). A bitmap rendered by pdfium therefore maps as
#   px = (x   - bbox[0]) * dpi/72
#   py = (top - bbox[1]) * dpi/72
# Never assume the origin is (0, 0) — always subtract page.bbox.


def _open_renderer(pdf_bytes: bytes):
    """Open the PDF in pdfium. Returns the document or None if unavailable."""
    try:
        import pypdfium2 as pdfium  # optional dependency; feature degrades if absent
    except Exception:
        return None
    return pdfium.PdfDocument(pdf_bytes)


def _ink_fraction(image) -> float:
    """Fraction of pixels that are non-white (before overlays are drawn)."""
    hist = image.convert("L").histogram()
    total = sum(hist)
    if total == 0:
        return 0.0
    non_white = sum(hist[:245])  # everything darker than near-white
    return non_white / total


def _render_evidence_crop(
    doc, page_bbox: tuple, row: _Row, arrow_seg: Optional[Segment]
) -> Optional[bytes]:
    """
    Render a tight, zoomed crop of the evidence for one callout as PNG bytes.

    The crop is the bounding box of {callout token, its L= token, the paired
    width number and its dimension-arrow line} plus CROP_MARGIN_PT, capped at
    CROP_MAX_BOX_PT per side, rendered at RENDER_DPI via pdfium's region
    rendering (the full page is never rasterised). Overlays: red callout,
    blue width number, orange L=.
    """
    try:
        from PIL import ImageDraw
    except Exception:
        return None

    callout = row.callout.tok

    # Tight bounding box of the evidence, in pdfplumber coordinates.
    boxes: list[tuple[float, float, float, float]] = [
        (callout.x0, callout.top, callout.x1, callout.bottom)
    ]
    for tok in (row.length_tok, row.width_tok):
        if tok is not None:
            boxes.append((tok.x0, tok.top, tok.x1, tok.bottom))
    if arrow_seg is not None:
        s = arrow_seg
        boxes.append((min(s.x0, s.x1), min(s.y0, s.y1),
                      max(s.x0, s.x1), max(s.y0, s.y1)))

    rx0 = min(b[0] for b in boxes) - CROP_MARGIN_PT
    ry0 = min(b[1] for b in boxes) - CROP_MARGIN_PT
    rx1 = max(b[2] for b in boxes) + CROP_MARGIN_PT
    ry1 = max(b[3] for b in boxes) + CROP_MARGIN_PT

    # Cap the box size, keeping the callout centred: evidence past the cap is
    # cut off rather than letting the bitmap grow unbounded.
    half = CROP_MAX_BOX_PT / 2.0
    rx0, rx1 = max(rx0, callout.cx - half), min(rx1, callout.cx + half)
    ry0, ry1 = max(ry0, callout.cy - half), min(ry1, callout.cy + half)

    # Clamp to the page. page_bbox is pdfplumber's page.bbox: the page spans
    # [bbox0, bbox2] horizontally and [bbox1, bbox3] in top-down coordinates.
    bx0, by0, bx1, by1 = page_bbox
    rx0, ry0 = max(bx0, rx0), max(by0, ry0)
    rx1, ry1 = min(bx1, rx1), min(by1, ry1)
    if rx1 - rx0 < 1 or ry1 - ry0 < 1:
        logger.warning(
            "evidence crop #%d degenerate: region=(%.1f,%.1f,%.1f,%.1f) bbox=%s",
            row.id, rx0, ry0, rx1, ry1, page_bbox,
        )
        return None

    # pdfium renders a sub-region via cut amounts from each page edge (points).
    scale = RENDER_DPI / 72.0
    cut_left = rx0 - bx0
    cut_top = ry0 - by0
    cut_right = bx1 - rx1
    cut_bottom = by1 - ry1

    page = doc[callout.page - 1]
    crop = page.render(
        scale=scale, crop=(cut_left, cut_bottom, cut_right, cut_top)
    ).to_pil().convert("RGB")

    # Validate BEFORE drawing overlays (overlays would always add ink).
    ink = _ink_fraction(crop)
    if ink < CROP_MIN_INK_FRACTION:
        logger.warning(
            "evidence crop #%d blank (ink=%.4f): region_pt=(%.1f,%.1f,%.1f,%.1f) "
            "pixel_box=(%d,%d) page.bbox=%s cuts=(l=%.1f,b=%.1f,r=%.1f,t=%.1f)",
            row.id, ink, rx0, ry0, rx1, ry1,
            crop.width, crop.height, page_bbox,
            cut_left, cut_bottom, cut_right, cut_top,
        )
        return None

    draw = ImageDraw.Draw(crop)

    def to_local(x_pt: float, top_pt: float) -> tuple[float, float]:
        # Local pixel = (pdfplumber coord - crop origin) * scale.
        return (x_pt - rx0) * scale, (top_pt - ry0) * scale

    def rect(tok: Token, color: tuple[int, int, int]) -> None:
        x0, y0 = to_local(tok.x0, tok.top)
        x1, y1 = to_local(tok.x1, tok.bottom)
        pad = 6
        draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], outline=color, width=5)

    rect(callout, (220, 30, 30))                       # callout — red
    if row.width_tok is not None:
        rect(row.width_tok, (0, 90, 230))              # paired width — blue
    if row.length_tok is not None:
        rect(row.length_tok, (230, 140, 0))            # length — orange

    # Keep the JSON light: cap the long side.
    long_side = max(crop.width, crop.height)
    if long_side > CROP_MAX_PX:
        ratio = CROP_MAX_PX / long_side
        crop = crop.resize((max(1, int(crop.width * ratio)),
                            max(1, int(crop.height * ratio))))

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue()


# --- Public API ------------------------------------------------------------


# Progress callback: receives (phase, percent). Phases: extract / detect /
# compute. Percent is monotonic across the whole parse.
ProgressFn = Callable[[str, float], None]


class Analysis:
    """
    Parsed takeoff for one uploaded PDF.

    Keeps the parsed rows, the per-page vector segments and the original
    bytes so shapes and evidence crops can be produced on demand; no rendered
    image is ever held beyond the one being produced. The pdfium document is
    opened lazily on the first crop and reused (pypdfium2 closes it when the
    Analysis is garbage-collected).
    """

    def __init__(self, pdf_bytes: bytes, progress: Optional[ProgressFn] = None):
        self._pdf_bytes = pdf_bytes
        self._rows, self._page_bboxes, self._segments_by_page = _parse_rows(
            pdf_bytes, progress
        )
        self._doc = None                                  # lazy pdfium handle
        self._shapes: Optional[dict[int, dict]] = None    # lazy shape cache

    def response(self) -> dict:
        """The JSON takeoff (items + summary) — no images, no shapes."""
        return _build_response(_rows_to_items(self._rows))

    def shapes(self) -> dict[int, dict]:
        """
        Classify the bar shape for every item; computed once and cached.

        This is the O(n²)-per-callout pass over the vector linework that
        dominates a naive analyze, which is why it is not part of parsing.
        """
        if self._shapes is None:
            shapes: dict[int, dict] = {}
            for r in self._rows:
                segs = self._segments_by_page.get(r.callout.tok.page, [])
                shape, seg_pts, _chain = _detect_shape(r.callout.tok, segs)
                shapes[r.id] = {"shape": shape, "segments_pts": seg_pts}
            self._shapes = shapes
        return self._shapes

    def render_evidence(self, item_id: int) -> Optional[bytes]:
        """PNG bytes of the evidence crop for one item, or None."""
        row = next((r for r in self._rows if r.id == item_id), None)
        if row is None:
            return None
        bbox = self._page_bboxes.get(row.callout.tok.page)
        if bbox is None:
            return None
        if self._doc is None:
            self._doc = _open_renderer(self._pdf_bytes)
        if self._doc is None:
            return None
        arrow_seg = None
        if row.width_tok is not None:
            arrow_seg = _find_arrow_segment(
                row.width_tok, self._segments_by_page.get(row.callout.tok.page, [])
            )
        try:
            return _render_evidence_crop(self._doc, bbox, row, arrow_seg)
        except Exception:
            logger.exception("evidence crop #%d failed", row.id)
            return None


def analyze_pdf(pdf_bytes: bytes) -> dict:
    """Run the full takeoff on a PDF and return the response dict."""
    return Analysis(pdf_bytes).response()


def _parse_rows(
    pdf_bytes: bytes, progress: Optional[ProgressFn] = None
) -> tuple[list[_Row], dict[int, tuple], dict[int, list[Segment]]]:
    """
    Parse the PDF into resolved rows, plus per-page page.bbox and segments.

    Segments are returned (not just used) so the lazy shape / evidence passes
    can run later without re-opening the PDF with pdfplumber.
    """
    rows: list[_Row] = []
    page_bboxes: dict[int, tuple] = {}  # pdfplumber page.bbox per page number
    segments_by_page: dict[int, list[Segment]] = {}
    next_id = 1

    def report(phase: str, percent: float) -> None:
        if progress is not None:
            progress(phase, round(min(percent, 99.0), 1))

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        n_pages = max(1, len(pdf.pages))
        page_share = 93.0 / n_pages  # 2% open + 93% pages + 5% final compute
        for page_index, page in enumerate(pdf.pages, start=1):
            page_base = 2.0 + (page_index - 1) * page_share
            report("extract", page_base)
            page_bboxes[page_index] = tuple(float(v) for v in page.bbox)
            tokens = _extract_tokens(page, page_index)
            segments = _extract_segments(page, page_index)
            segments_by_page[page_index] = segments
            circle_boxes = _circle_boxes(page)

            report("detect", page_base + page_share * 0.7)
            callouts = _parse_callouts(tokens)
            length_pairs = _pair_lengths(callouts, tokens)

            # Standalone width-candidate numbers: pure numbers that are not
            # part of a callout/length token and are not circled element marks.
            number_tokens = [
                t for t in tokens
                if NUMBER_RE.match(t.text)
                and not _is_circled(t, circle_boxes)
            ]
            used_widths: set[int] = set()

            for ci, callout in enumerate(callouts):
                if callout.inline_length is not None:
                    length_cm, length_tok = callout.inline_length, None
                else:
                    length_cm, length_tok = length_pairs.get(ci, (None, None))
                flags: list[str] = []
                width_tok: Optional[Token] = None
                zone_width: Optional[float] = None

                if callout.n is not None:
                    # Explicit callout: the count is given directly.
                    count: Optional[int] = callout.n
                    source = "explicit"
                elif callout.spacing:
                    # Distributed: measure the zone width and derive the count.
                    match = _find_zone_width(callout, number_tokens, used_widths)
                    if match is not None:
                        zone_width, idx = match
                        used_widths.add(idx)
                        width_tok = number_tokens[idx]
                        count = math.ceil(zone_width / callout.spacing) + 1
                        source = "measured_width"
                    else:
                        # Defer to the estimation pass (needs all measured
                        # widths across pages first).
                        count = None
                        source = "estimated"
                else:
                    # No count and no spacing: single bar assumption.
                    count = 1
                    source = "estimated"
                    flags.append("estimated_count")

                rows.append(
                    _Row(
                        id=next_id, callout=callout,
                        length_cm=length_cm, length_tok=length_tok,
                        width_tok=width_tok,
                        zone_width_cm=zone_width,
                        count=count, source=source, flags=flags,
                    )
                )
                next_id += 1

        # Pass 2: resolve distributed callouts that had no measured width.
        report("compute", 95.0)
        _estimate_missing_widths(rows)

    return rows, page_bboxes, segments_by_page


def _find_arrow_segment(width_tok: Token, segments: list[Segment]) -> Optional[Segment]:
    """The dimension-arrow line the width number annotates: nearest segment."""
    best: Optional[Segment] = None
    best_dist = ARROW_SEARCH_RADIUS
    for s in segments:
        if s.page != width_tok.page:
            continue
        d = math.hypot(s.midx - width_tok.cx, s.midy - width_tok.cy)
        if d <= best_dist:
            best_dist = d
            best = s
    return best


def _rows_to_items(rows: list[_Row]) -> list[Item]:
    items: list[Item] = []
    for r in rows:
        count = r.count or 0
        flags = list(r.flags)
        if r.length_cm is None:
            flags.append("missing_length")

        kg_per_m = STEEL_KG_PER_M_COEFF * (r.callout.diameter ** 2)
        total_m = (count * r.length_cm / 100.0) if r.length_cm else 0.0
        weight_kg = total_m * kg_per_m

        items.append(
            Item(
                id=r.id,
                diameter=r.callout.diameter,
                spacing=r.callout.spacing,
                length_cm=r.length_cm,
                zone_width_cm=r.zone_width_cm,
                count=count,
                total_m=round(total_m, 3),
                weight_kg=round(weight_kg, 3),
                source=r.source,
                counted=count > 0,
                position={
                    "page": r.callout.tok.page,
                    "x": round(r.callout.tok.cx, 1),
                    "y": round(r.callout.tok.cy, 1),
                },
                flags=flags,
            )
        )
    return items


def _build_response(items: list[Item]) -> dict:
    """Assemble the items + summary response from computed rows."""
    by_diameter: dict[int, dict] = {}
    source_counts: dict[str, int] = {}
    grand_total_kg = 0.0
    flagged: list[dict] = []

    for it in items:
        # Rows with no resolvable count are shown but kept out of the totals.
        if it.counted:
            d = by_diameter.setdefault(
                it.diameter, {"diameter": it.diameter, "bars": 0, "meters": 0.0, "kg": 0.0}
            )
            d["bars"] += it.count
            d["meters"] += it.total_m
            d["kg"] += it.weight_kg
            grand_total_kg += it.weight_kg

        source_counts[it.source] = source_counts.get(it.source, 0) + 1

        warn = [f for f in it.flags if f in SUMMARY_WARN_FLAGS]
        if warn:
            flagged.append({"id": it.id, "flags": warn})

    per_diameter = [
        {
            "diameter": v["diameter"],
            "bars": v["bars"],
            "meters": round(v["meters"], 3),
            "kg": round(v["kg"], 3),
        }
        for v in sorted(by_diameter.values(), key=lambda x: x["diameter"])
    ]

    return {
        "items": [asdict(it) for it in items],
        "summary": {
            "per_diameter": per_diameter,
            "grand_total_kg": round(grand_total_kg, 3),
            "source_counts": source_counts,
            "flagged_items": flagged,
            "item_count": len(items),
            "excluded_count": sum(1 for it in items if not it.counted),
        },
    }
