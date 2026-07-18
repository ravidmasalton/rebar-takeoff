# Rebar Quantity Takeoff

Extracts rebar (steel reinforcement) quantities from **vector** structural-drawing
PDFs. FastAPI backend + single-page React frontend. Everything is processed in
memory; nothing is written to disk or a database. The most recent analysis is
kept in a single in-memory slot so evidence crops can be rendered on demand.

## Run

```bash
cd backend
pip install -r requirements.txt
python main.py          # serves API + frontend on http://127.0.0.1:8000
```

Open http://127.0.0.1:8000, drag a PDF onto the drop zone.

## How it works

All logic lives in [backend/rebar_service.py](backend/rebar_service.py).

**Extraction** (pdfplumber, words with coordinates)
- Callout pattern `[n]Ø<dia>[@<spacing>]` with a nearby `L=<length>` token.
  In the source PDF the diameter symbol is the byte `ö` (drawn as Ø). Units: cm.
- **Bidi/RTL** reversed tokens (`02@01ö` → `ö10@20`, `056=L` → `L=650`) are
  detected per token by matching the token and its reverse against the patterns.
- **Diameter whitelist** `{8,10,12,14,16,20,25}`: when both a token and its
  reverse parse as a callout (`41ö2` ↔ `2ö14`), the orientation with a real bar
  size wins. Tokens that are invalid both ways are dropped — never a Ø2/Ø3 row.

**Counts**
- *Explicit* (`5ö16 L=650`): count = `n`, source `explicit`.
- *Distributed* (`ö10@20 L=100`): paired with the nearest standalone width number
  (≥ 60 cm, not a circled element mark, each used once); count = `ceil(width/spacing)+1`,
  source `measured_width`.
- *Distributed with no width*: width is **estimated** from the median measured
  width of the same `(dia, spacing, length)` group, else the same `(dia, spacing)`
  group (source `estimated`, flagged `estimated_width`). If no peer measurements
  exist, the row is kept with `count=0`, flagged `unresolved_width`, and
  **excluded from the totals** (`counted=false`).

**Weights**: `kg/m = 0.006165·d²` (d in mm); `total_m = count·L/100`;
aggregated per diameter + grand total.

**Shape**: bold vector polyline nearest the callout is chained and classified
`straight / L / U / Z` (U vs Z by bend direction). Ambiguous → `needs review`,
never guessed.

**Evidence crop**: each callout gets a tight, zoomed PNG crop — the bounding box
of {callout token, `L=` token, paired width number + its dimension-arrow line}
plus 40 pt margin, capped at 700 pt per side, rendered at 200 DPI via pdfium
*region* rendering (the full page is never rasterised). Crops are **not**
inlined in the `/analyze` response — a large drawing can have dozens of
callouts, and bulk-rendering every crop exhausts memory on small servers.
Instead `GET /evidence/{item_id}?analysis_id=…` renders a single crop on
demand from the cached analysis (one in-memory slot holding the parsed rows +
PDF bytes; a new upload replaces it, and a stale `analysis_id` gets a 404).
Overlays: callout (red), width number (blue), `L=` (orange), so
`count = ceil(width/spacing)+1` can be checked against the exact numbers it came
from. Coordinates are mapped through pdfplumber's `page.bbox` — **never** assuming
a (0,0) origin — because real drawings ship shifted mediaboxes (e.g.
`(-2203.08, 1449.18, …)`); pdfplumber reports raw PDF x but a `top` that ignores
the mediabox y-offset, so `px=(x−bbox[0])·dpi/72`, `py=(top−bbox[1])·dpi/72`.
Rendered crops are validated for non-blankness (>1 % ink) before overlays; blank
crops are logged with the computed pixel box + mediabox and 404 instead of
returning white. Rendering is optional — if `pypdfium2` is missing the takeoff
still runs and `/evidence` returns 404.
Regression tests: [backend/test_rebar_service.py](backend/test_rebar_service.py).

**Response**: `analysis_id` (key for `/evidence` requests) + `items` (one row
per callout) + `summary` (per-diameter totals, grand total, source counts,
`excluded_count`, flagged items for manual verification).

## Frontend

Hebrew RTL SPA ([frontend/index.html](frontend/index.html), React via CDN).
Drag & drop upload, editable table (width/count) with live recalculation, a
per-row evidence thumbnail that opens a full-size verification modal, and a summary
panel with flagged items. Thumbnails load lazily (`loading="lazy"`) — the
browser fetches each crop only as its row scrolls into view. No persistence —
refresh clears everything.
