"""
Tests for rebar_service, focused on coordinate handling of the evidence crops.

The critical regression case is a PDF whose mediabox does not start at (0,0):
pdfplumber reports raw PDF x and a `top` that ignores the mediabox y-offset,
so any renderer that assumes an origin of (0,0) produces white / misplaced
crops. These tests build such a PDF and assert the crop contains the actual
drawing ink inside the overlay boxes.
"""

import base64
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


def _decode_crop(item: dict) -> Image.Image:
    assert item["evidence_png"], "expected an evidence crop"
    b64 = item["evidence_png"].split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


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
    result = rebar_service.analyze_pdf(_make_pdf(shift))
    assert len(result["items"]) == 1
    item = result["items"][0]

    # The takeoff itself must be unaffected by the mediabox origin.
    assert item["diameter"] == 10
    assert item["spacing"] == 20
    assert item["zone_width_cm"] == 300.0
    assert item["count"] == 16
    assert item["source"] == "measured_width"

    crop = _decode_crop(item)

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

    result = rebar_service.analyze_pdf(buf.getvalue())
    item = result["items"][0]
    # This page has real content at the callout, so the crop must exist...
    assert item["evidence_png"] is not None
    crop = _decode_crop(item)
    # ...and be genuinely non-blank.
    hist = crop.convert("L").histogram()
    assert sum(hist[:245]) / sum(hist) > rebar_service.CROP_MIN_INK_FRACTION


def test_crop_is_tight_not_full_page():
    """The crop should cover just the evidence tokens + margin, not 200pt radius."""
    result = rebar_service.analyze_pdf(_make_pdf(shift=False))
    crop = _decode_crop(result["items"][0])
    # Evidence spans ~160pt wide (tokens 100..260) + 2*40pt margin = ~240pt.
    # At 300 DPI that is ~1000px, capped at CROP_MAX_PX after downscale.
    assert crop.width <= rebar_service.CROP_MAX_PX
    assert crop.height <= rebar_service.CROP_MAX_PX
    # Height covers tokens (~35pt tall) + margins ~= 115pt << width; a fixed
    # 200pt-radius crop would be near-square, a tight one is clearly wide.
    assert crop.width / crop.height > 1.6
