"""Recover explicit rectangular PDF clips for caption-crossing vector lines.

pdfminer reports the entire mathematical line, even when the PDF clips it to a
plot frame. Only use a matching top-level straight path with an explicit native
rectangle clip. Unknown transforms/curved clips are left unchanged, never guessed.
"""
from __future__ import annotations

import ctypes


def _line_key(points):
    return tuple(sorted(tuple(round(float(v), 2) for v in point) for point in points))


def _rectangle(points):
    if len(points) == 5 and _line_key([points[0]]) == _line_key([points[-1]]):
        points = points[:-1]
    if len(points) != 4:
        return None
    xs, ys = {round(x, 2) for x, _ in points}, {round(y, 2) for _, y in points}
    if len(xs) != 2 or len(ys) != 2:
        return None
    if any(abs(a[0] - b[0]) > .01 and abs(a[1] - b[1]) > .01
           for a, b in zip(points, points[1:] + points[:1])):
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _native_clips(pdf_path, page_index, keys, page_w, page_h):
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    def point(segment):
        x, y = ctypes.c_float(), ctypes.c_float()
        if not raw.FPDFPathSegment_GetPoint(segment, x, y):
            raise ValueError('Cannot read PDF path point')
        return x.value, y.value

    document = pdfium.PdfDocument(str(pdf_path))
    page = None
    found = {}
    try:
        page = document[page_index]
        if (page.get_rotation() or any(abs(a-b) > .01 for a, b in zip(page.get_size(), (page_w, page_h)))
                or any(abs(a-b) > .01 for a, b in zip(page.get_bbox(), (0, 0, page_w, page_h)))):
            return {}
        for obj in page.get_objects(max_depth=1):
            if obj.type != raw.FPDF_PAGEOBJ_PATH or raw.FPDFPath_CountSegments(obj) != 2:
                continue
            segments = [raw.FPDFPath_GetPathSegment(obj, i) for i in range(2)]
            if [raw.FPDFPathSegment_GetType(s) for s in segments] != [raw.FPDF_SEGMENT_MOVETO, raw.FPDF_SEGMENT_LINETO]:
                continue
            matrix = obj.get_matrix()
            points = [matrix.on_point(*point(s)) for s in segments]
            key = _line_key([(x, page_h-y) for x, y in points])
            if key not in keys:
                continue
            matches = found.setdefault(key, set())
            clip = raw.FPDFPageObj_GetClipPath(obj)
            if not clip or raw.FPDFClipPath_CountPaths(clip) != 1:
                matches.add(None)
                continue
            count = raw.FPDFClipPath_CountPathSegments(clip, 0)
            if count not in (4, 5):
                matches.add(None)
                continue
            segments = [raw.FPDFClipPath_GetPathSegment(clip, 0, i) for i in range(count)]
            if any(raw.FPDFPathSegment_GetType(s) not in (raw.FPDF_SEGMENT_MOVETO, raw.FPDF_SEGMENT_LINETO)
                   for s in segments):
                matches.add(None)
                continue
            bounds = _rectangle([point(s) for s in segments])
            if bounds:
                left, bottom, right, top = bounds
                matches.add((left, page_h-top, right, page_h-bottom))
            else:
                matches.add(None)
        return {key: next(iter(values)) for key, values in found.items() if len(values) == 1 and None not in values}
    finally:
        if page is not None:
            page.close()
        document.close()


def clip_caption_lines(pdf_path, page_index, graphics, caption_tops, page_w, page_h):
    targets = [g for g in graphics if g.get('object_type') == 'line' and len(g.get('pts', [])) == 2
               and any(float(g['top']) < top < float(g['bottom']) for top in caption_tops)]
    if not targets:
        return graphics
    target_ids = {id(g) for g in targets}
    keys = {_line_key(g['pts']) for g in targets}
    clips = _native_clips(pdf_path, page_index, keys, page_w, page_h)
    result = []
    for graphic in graphics:
        bounds = clips.get(_line_key(graphic['pts'])) if id(graphic) in target_ids else None
        if bounds is None:
            result.append(graphic)
            continue
        left, top = max(graphic['x0'], bounds[0]), max(graphic['top'], bounds[1])
        right, bottom = min(graphic['x1'], bounds[2]), min(graphic['bottom'], bounds[3])
        if left <= right and top <= bottom:
            result.append({**graphic, 'x0': left, 'top': top, 'x1': right, 'bottom': bottom,
                           'clip_evidence': 'native_pdf_rectangle'})
    return result
