# Journal Logo Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove detached journal header and logo bands from every PDF figure extraction strategy.

**Architecture:** Add one dependency-free geometry sanitizer that works on `pdfplumber` element boxes and a candidate crop rectangle. Each extractor calls it immediately before rendering, so crop behavior is consistent without changing strategy selection.

**Tech Stack:** Python 3.11, pdfplumber, pypdfium2, unittest

---

### Task 1: Shared Geometry Sanitizer

**Files:**
- Create: `utils/figure_crop_geometry.py`
- Create: `tests/test_figure_crop_geometry.py`

- [ ] Write tests for detached top and bottom bands and non-detached figure content.
- [ ] Run `python -m unittest tests.test_figure_crop_geometry -v` and confirm the missing module/function failure.
- [ ] Implement `is_rule_line` and `trim_detached_edge_bands`.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Extractor Integration

**Files:**
- Modify: `utils/caption_figures.py`
- Modify: `utils/pdf_figure_extractor.py`
- Modify: `utils/vision_figures.py`
- Modify: `tests/test_caption_figures.py`
- Modify: `tests/test_pdf_figure_extractor.py`

- [ ] Add failing tests showing caption and legend regions exclude a detached header band.
- [ ] Run the focused tests and confirm the new assertions fail.
- [ ] Sanitize candidate regions in caption, legend, heuristic, and vision paths.
- [ ] Keep `_is_rule_line` as a compatibility wrapper for existing tests.
- [ ] Run all figure extraction tests.

### Task 3: Real-PDF Regression

**Files:**
- No production file changes.

- [ ] Re-extract the two known contaminated pages without using cached images.
- [ ] Confirm the calculated top boundary is 104 pt for both samples.
- [ ] Render the new crops and inspect them visually.
- [ ] Run the complete test suite and inspect `git diff --check`.
