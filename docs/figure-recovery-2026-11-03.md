# Independent recovery of draft figures

SOLIDEX 7-2 and 7-3 had generated bodies but empty editorial figure maps.
Preparation previously required a complete legacy image package, then marked
the preparation task done even when that package was blocked.

## Extraction fixes

- Reject wrapped panel references and continuation lines ending in `related to`
  as main-figure legends. True duplicate figures remain ambiguous and blocked.
- Honor an exact native rectangular PDF clip for rectangle paths as well as
  straight lines. Never clamp unknown graphics to an inferred legend boundary.
- Recover a large preceding-page figure under a top-of-page legend, excluding
  bottom prose; require a substantial graphics region with no competing legend.
- Keep crop padding outside captions without shrinking the detected figure.

## Draft behavior

Missing figures are resolved independently from the draft's immutable PDF.
Valid candidates survive an unrelated extraction failure. Existing figures,
body, captions, title, digest and cover selection remain untouched. Source
identity is checked before and after extraction, and the final revision is
saved with compare-and-swap to protect concurrent edits.

Incomplete preparations end as `needs_images`; the list derives counts from
the current draft instead of the historical task status. Complete drafts say
待审核. The authenticated, line-scoped, CSRF-protected recovery action queues
only a prepare task. Publication still requires complete figures and explicit
human confirmation. No legacy image-package approval is inferred.

## Validation

Original-PDF recovery reproduced the current four slots in each article:

| Article | Figure | PDF page |
|---|---|---|
| 7-2 | 1, 2, 5, 6 | 2, 4, 8, 9 |
| 7-3 | 1, 3, 4, 5 | 3, 6, 8, 9 |

Isolated recovery compared all document nodes, title, digest, cover selection
and source identity before/after and created no publish task. Crops were also
visually compared with original pages. Regression tests cover partial failure,
manual-image preservation, concurrent editing, changed PDFs, missing-figure
publication rejection, API authorization, and native clipping provenance.
