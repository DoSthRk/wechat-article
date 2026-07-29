# Journal Logo Filter Design

## Goal

Remove detached publisher logos, journal names, article labels, and citation headers
from extracted scientific figures without changing the dashboard or article pipeline.

## Scope

- Reuse PDF geometry already available through `pdfplumber`.
- Trim only short detached bands at the outer top or bottom of a page.
- Require a visible whitespace gap between the band and the main figure.
- Apply the same sanitizer to caption, figure-legend, heuristic, and vision crops.
- Do not erase or inpaint pixels inside a scientific figure.
- Do not add OCR, OpenCV, model calls, or new runtime dependencies.

## Data Flow

1. An extractor calculates its candidate figure rectangle.
2. The shared sanitizer inspects graphic-element occupancy inside that rectangle.
3. A short detached band in the outer page margin is excluded.
4. The extractor renders and saves the sanitized rectangle as before.

If no qualifying detached band exists, the candidate rectangle is returned unchanged.

## Acceptance

- CellPress/Immunity sample page 4 moves its crop top from about 42 pt to 104 pt.
- Molecular Therapy sample page 2 moves its crop top from about 1 pt to 104 pt.
- A normal figure at the top of a page is not trimmed without a qualifying detached band.
- Existing extraction and dashboard tests continue to pass.
