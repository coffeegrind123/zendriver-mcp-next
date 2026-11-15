"""Screenshots and PDFs, taken without the page being able to tell.

Full-page screenshots do NOT use ``captureBeyondViewport``. Measured 2026-11-15
on Chrome 157: Chrome enlarges the viewport for it, the page receives a
``resize`` event and its ResizeObserver sees the document's height jump. The
page is instead scrolled a viewport at a time with trusted wheel gestures, as a
person reads it, captured, stitched, and scrolled back. Fixed or sticky
elements (a header) therefore appear in every slice.

An element screenshot is a clip of the visible viewport: a crop, which the page
cannot observe.
"""

from __future__ import annotations

import base64
import io

from PIL import Image as PILImage
from zendriver import cdp

from zendriver_mcp.dom import Dom, Handle
from zendriver_mcp.errors import ElementHiddenError

# Full-page captures stop at this many CSS pixels of height, so an endless feed
# cannot run forever or build a gigantic image.
MAX_FULL_PAGE_HEIGHT = 20000


async def viewport_png(dom: Dom) -> bytes:
    data = await dom.tab.send(cdp.page.capture_screenshot(format_="png"))
    return base64.b64decode(data)


async def element_png(dom: Dom, handle: Handle) -> tuple[bytes, bool]:
    """The element, scrolled into view and clipped to it; and whether the viewport cut it."""
    await dom.center(handle)
    frame = handle.frame
    try:
        model = await frame.session.send(
            cdp.dom.get_box_model(backend_node_id=handle.backend_node_id)
        )
    except Exception as exc:
        raise ElementHiddenError(handle.label) from exc
    ox, oy = await dom._session_offset(frame)
    q = model.border
    xs, ys = [q[i] + ox for i in (0, 2, 4, 6)], [q[i] + oy for i in (1, 3, 5, 7)]
    sx, sy, vw, vh, _content = await dom.viewport()
    left, top = max(0.0, min(xs)), max(0.0, min(ys))
    right, bottom = min(vw, max(xs)), min(vh, max(ys))
    if right <= left or bottom <= top:
        raise ElementHiddenError(handle.label)
    cut = (left, top, right, bottom) != (min(xs), min(ys), max(xs), max(ys))
    # The clip is in document coordinates: the viewport rectangle plus the scroll.
    clip = cdp.page.Viewport(
        x=left + sx, y=top + sy, width=right - left, height=bottom - top, scale=1
    )
    data = await dom.tab.send(cdp.page.capture_screenshot(format_="png", clip=clip))
    return base64.b64decode(data), cut


async def full_page_png(dom: Dom) -> tuple[bytes, bool]:
    """The whole page, scrolled through and stitched; and whether it was cut at the cap."""
    _x, start, vw, vh, _content = await dom.viewport()
    y = await dom.scroll_by(-start) if start else 0.0
    slices: list[tuple[float, PILImage.Image]] = []
    try:
        while True:
            png = await viewport_png(dom)
            slices.append((y, PILImage.open(io.BytesIO(png)).convert("RGB")))
            content = (await dom.viewport())[4]
            if y + vh >= min(content, MAX_FULL_PAGE_HEIGHT) - 1:
                break
            nxt = await dom.scroll_by(vh)
            if nxt <= y:
                break  # the page will not scroll further
            y = nxt
    finally:
        now = (await dom.viewport())[1]
        if now != start:
            await dom.scroll_by(start - now)
    content = (await dom.viewport())[4]
    height = min(content, MAX_FULL_PAGE_HEIGHT, slices[-1][0] + vh)
    first = slices[0][1]
    # Device pixels per CSS pixel. Not image width / viewport width: the image
    # includes the scrollbar and the CSS viewport does not.
    metrics = await dom.tab.send(cdp.page.get_layout_metrics())
    scale = metrics[0].client_width / metrics[3].client_width if metrics[3].client_width else 1.0
    # Each slice shows the scrollbar with its thumb somewhere else; keep the
    # page's own width only.
    width = min(first.width, round(vw * scale))
    canvas = PILImage.new("RGB", (width, round(height * scale)), "white")
    for top, img in slices:
        canvas.paste(img.crop((0, 0, width, img.height)), (0, round(top * scale)))
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue(), content > MAX_FULL_PAGE_HEIGHT


async def pdf(dom: Dom, *, landscape: bool, background: bool) -> bytes:
    data, _stream = await dom.tab.send(
        cdp.page.print_to_pdf(landscape=landscape, print_background=background)
    )
    return base64.b64decode(data)
