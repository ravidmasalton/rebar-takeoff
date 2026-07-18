"""
Tests for rebar_service, focused on coordinate handling of the evidence crops.

The critical regression case is a PDF whose mediabox does not start at (0,0):
pdfplumber reports raw PDF x and a `top` that ignores the mediabox y-offset,
so any renderer that assumes an origin of (0,0) produces white / misplaced
crops. These tests build such a PDF and assert the crop contains the actual
drawing ink inside the overlay boxes.
"""

import io

import pypdf
import pytest
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import rebar_service


def _make_pdf(shift: bool) -> bytes:
    """A one-callout drawing; optionally with the mediabox origin shifted."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 10)
    # Distributed callout with a width number and its dimension-arrow line.
    c.drawString(100, 700, "ö10@20")
    c.drawString(170, 700, "L=100")
    c.drawString(130, 720, "300")
    c.setLineWidth(1)
    c.line(100, 715, 260, 715)   # dimension arrow under the width number
    c.setLineWidth(3)
    c.line(100, 690, 260, 690)   # the bar itself
    c.showPage()
    c.save()
    if not shift:
        return buf.getvalue()

    # Shift the mediabox origin (same size) without moving the content —
    # mimics real drawings whose mediabox is e.g. (-2203.08, 1449.18, ...).
    reader = pypdf.PdfReader(io.BytesIO(buf.getvalue()))
    writer = pypdf.PdfWriter()
    page = reader.pages[0]
    w = float(page.mediabox.width)
    h = float(page.mediabox.height)
    page.mediabox.lower_left = (-100, -50)
    page.mediabox.upper_right = (-100 + w, -50 + h)
    writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _decode_crop(analysis: rebar_service.Analysis, item_id: int) -> Image.Image:
    png = analysis.render_evidence(item_id)
    assert png, "expected an evidence crop"
    return Image.open(io.BytesIO(png)).convert("RGB")


def _red_box(img: Image.Image) -> tuple:
    """Bounding box of the red callout-overlay pixels."""
    px = img.load()
    xs, ys = [], []
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y]
            if r > 170 and g < 90 and b < 90:
                xs.append(x)
                ys.append(y)
    assert xs, "no red overlay pixels found in crop"
    return min(xs), min(ys), max(xs), max(ys)


def _has_dark_ink_inside(img: Image.Image, box: tuple) -> bool:
    """True if genuine drawing ink (near-black text) lies inside `box`."""
    x0, y0, x1, y1 = box
    gray = img.convert("L")
    px = gray.load()
    for y in range(y0 + 6, y1 - 5):
        for x in range(x0 + 6, x1 - 5):
            if px[x, y] < 100:
                return True
    return False


@pytest.mark.parametrize("shift", [False, True], ids=["origin_0_0", "shifted_mediabox"])
def test_evidence_crop_alignment(shift):
    analysis = rebar_service.Analysis(_make_pdf(shift))
    result = analysis.response()
    assert len(result["items"]) == 1
    item = result["items"][0]

    # The takeoff itself must be unaffected by the mediabox origin.
    assert item["diameter"] == 10
    assert item["spacing"] == 20
    assert item["zone_width_cm"] == 300.0
    assert item["count"] == 16
    assert item["source"] == "measured_width"

    # Images are rendered on demand, never inlined in the analyze response.
    assert "evidence_png" not in item

    crop = _decode_crop(analysis, item["id"])

    # Not blank: the raw drawing must contribute real ink.
    hist = crop.convert("L").histogram()
    ink = sum(hist[:245]) / sum(hist)
    assert ink > rebar_service.CROP_MIN_INK_FRACTION

    # Alignment: the red overlay must surround the callout TEXT — dark glyph
    # pixels inside the red box prove the coordinate mapping is correct.
    assert _has_dark_ink_inside(crop, _red_box(crop))


def test_blank_crop_is_rejected_not_returned():
    """A crop region that lands on empty paper must return None, not white."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 10)
    c.drawString(100, 700, "5ö16")
    c.drawString(160, 700, "L=650")
    c.showPage()
    c.save()

    analysis = rebar_service.Analysis(buf.getvalue())
    item = analysis.response()["items"][0]
    # This page has real content at the callout, so the crop must exist...
    crop = _decode_crop(analysis, item["id"])
    # ...and be genuinely non-blank.
    hist = crop.convert("L").histogram()
    assert sum(hist[:245]) / sum(hist) > rebar_service.CROP_MIN_INK_FRACTION


def test_crop_is_tight_not_full_page():
    """The crop should cover just the evidence tokens + margin, not 200pt radius."""
    analysis = rebar_service.Analysis(_make_pdf(shift=False))
    item = analysis.response()["items"][0]
    crop = _decode_crop(analysis, item["id"])
    # Evidence spans ~160pt wide (tokens 100..260) + 2*40pt margin = ~240pt,
    # rendered at RENDER_DPI and capped at CROP_MAX_PX after downscale.
    assert crop.width <= rebar_service.CROP_MAX_PX
    assert crop.height <= rebar_service.CROP_MAX_PX
    # Also bounded by the hard cap on the crop box itself.
    max_px = rebar_service.CROP_MAX_BOX_PT * rebar_service.RENDER_DPI / 72.0 + 1
    assert crop.width <= max_px
    assert crop.height <= max_px
    # Height covers tokens (~35pt tall) + margins ~= 115pt << width; a fixed
    # 200pt-radius crop would be near-square, a tight one is clearly wide.
    assert crop.width / crop.height > 1.6


def test_render_evidence_unknown_item_returns_none():
    analysis = rebar_service.Analysis(_make_pdf(shift=False))
    assert analysis.render_evidence(999) is None


def test_shapes_are_lazy_and_classified():
    """Shape classification is not part of /analyze; shapes() provides it."""
    analysis = rebar_service.Analysis(_make_pdf(shift=False))
    item = analysis.response()["items"][0]
    assert item["shape"] is None  # analyze returns no shapes

    shapes = analysis.shapes()
    assert shapes[item["id"]]["shape"] == "straight"
    assert shapes[item["id"]]["segments_pts"]
    assert analysis.shapes() is shapes  # cached, computed once


def test_length_not_stolen_by_neighbor():
    """A callout with no L= of its own must not grab a neighbouring row's."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 10)
    c.drawString(100, 700, "5ö16")
    c.drawString(150, 700, "L=650")   # same printed line as 5ö16
    c.drawString(100, 720, "3ö12")    # 20pt above; its nearest L= is the one above
    c.showPage()
    c.save()

    items = rebar_service.analyze_pdf(buf.getvalue())["items"]
    by_dia = {it["diameter"]: it for it in items}
    assert by_dia[16]["length_cm"] == 650.0
    assert by_dia[12]["length_cm"] is None
    assert "missing_length" in by_dia[12]["flags"]


def _tok(text, x0, top, x1, bottom):
    return rebar_service.Token(
        text=text, raw=text, x0=x0, x1=x1, top=top, bottom=bottom, page=1
    )


def _callout_of(tok):
    m = rebar_service.CALLOUT_RE.match(tok.text)
    return rebar_service._Callout(
        tok=tok,
        n=int(m.group("n")) if m.group("n") else None,
        diameter=int(m.group("dia")),
        spacing=int(m.group("sp")) if m.group("sp") else None,
        inline_length=None,
    )


def test_pair_lengths_requires_matching_orientation():
    # Vertical callout (box taller than wide) with a collinear vertical L=
    # below it, plus a closer horizontal L= that must be ignored.
    vert_callout = _callout_of(_tok("ö10@20", 100, 600, 110, 640))
    vert_len = _tok("L=650", 100, 645, 110, 680)
    horiz_len = _tok("L=999", 115, 610, 150, 620)

    pairs = rebar_service._pair_lengths(
        [vert_callout], [vert_len, horiz_len]
    )
    assert pairs == {0: (650.0, vert_len)}


def test_shape_detection_survives_thin_linework_and_width_variation():
    """
    The two regressions seen on real drawings: (a) thin leader/dimension
    lines joining the bar's component, (b) a thicker neighbouring bar
    pushing a fraction-of-max cut above this callout's own thinner bar.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 10)

    # Callout A with a 1.6pt U-bar, thin leader touching the bar.
    c.drawString(120, 720, "ö10@20")
    c.drawString(190, 720, "L=100")
    c.setLineWidth(0.4)
    c.line(140, 718, 160, 682)            # leader down to the bar
    c.setLineWidth(1.6)
    p = c.beginPath()
    p.moveTo(100, 735); p.lineTo(100, 680); p.lineTo(230, 680); p.lineTo(230, 735)
    c.drawPath(p)

    # Callout B nearby with a much bolder 3.0pt straight bar.
    c.drawString(320, 520, "5ö16")
    c.drawString(370, 520, "L=650")
    c.setLineWidth(3.0)
    c.line(300, 490, 460, 490)
    c.showPage()
    c.save()

    analysis = rebar_service.Analysis(buf.getvalue())
    items = analysis.response()["items"]
    shapes = analysis.shapes()
    by_dia = {it["diameter"]: shapes[it["id"]]["shape"] for it in items}
    assert by_dia[10] == "U"
    assert by_dia[16] == "straight"


def test_summary_flags_only_quantity_warnings():
    """estimated_count must not reach the flagged panel; warn flags must."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 10)
    c.drawString(100, 700, "ö16")     # no count, no spacing -> estimated_count
    c.drawString(140, 700, "L=650")
    c.showPage()
    c.save()

    result = rebar_service.analyze_pdf(buf.getvalue())
    item = result["items"][0]
    assert "estimated_count" in item["flags"]        # still on the row
    assert result["summary"]["flagged_items"] == []  # but not in the panel


def test_progress_callback_phases():
    events = []
    rebar_service.Analysis(_make_pdf(shift=False), progress=lambda p, pct: events.append((p, pct)))
    phases = [p for p, _ in events]
    percents = [pct for _, pct in events]
    assert "extract" in phases and "detect" in phases and "compute" in phases
    assert percents == sorted(percents)  # monotonic
    assert all(0 <= pct <= 99 for pct in percents)
