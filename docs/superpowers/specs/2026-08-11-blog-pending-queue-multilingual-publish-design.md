# Blog Pending Queue and Multilingual Manual Publishing Design

**Date:** 2026-08-11  
**Status:** Proposed, supersedes the WeChat-publication-triggered Blog sync design  
**Scope:** `wechat-article` only

## Goal

Keep article production, translation, and publishing independently controllable.
Each successfully generated Chinese article is queued for GeneMedi Blog handling
without depending on a WeChat publication. Administrators can translate selected
articles and manually publish selected Chinese or English versions. Scheduling,
throughput control, and WeChat-to-Blog synchronization are intentionally out of
scope for this phase.

## User Workflow

1. A business user uploads PDFs and generates selected articles in `/operator`.
2. A successful generation creates two Blog version records:
   - Chinese (`zh`): `ready` and Blog distribution `pending`.
   - English (`en`): `pending` translation and Blog distribution `waiting_translation`.
3. An administrator opens `/admin`, selects one or more English rows, and runs
   translation. Translation updates only the English version state.
4. The administrator selects ready Chinese and/or English rows and explicitly
   publishes them to Blog. Publication is a separate action from translation.
5. A result records the Drupal UUID, node ID, final URL, timestamps, and a
   concise error when unsuccessful. Raw logs remain available as expandable
   diagnostics.

`/operator` remains limited to PDF upload and article generation. Blog controls,
translation controls, and full operational statuses belong only in `/admin`.

## State Model

### Article versions

Add `article_versions`, unique on `(job_pk, lang)`:

| Field | Purpose |
| --- | --- |
| `job_pk`, `lang` | Version identity; initial languages are `zh` and `en`. |
| `content_path` | Relative generated Markdown path (`article.md`, `article.en.md`). |
| `translation_status` | `ready`, `pending`, `translating`, `translated`, `failed`. |
| `translation_error`, `translation_model` | Recoverable diagnostic and provenance. |
| `total_tokens`, `prompt_tokens`, `completion_tokens` | Translation usage record. |
| timestamps | State audit trail. |

Chinese original content is `ready`; its translation action is never run. English
is publishable only after it is `translated` and its Markdown file is present.

### Distributions

Continue to use `Distribution` as the per-channel state row, unique on
`(job_pk, platform, account, lang)`. Add generic remote identifiers:

| Field | Purpose |
| --- | --- |
| `external_id` | Drupal JSON:API UUID for Blog; do not overload `wechat_media_id`. |
| `external_node_id` | Drupal internal node ID. |
| `external_url` | Final public Blog URL. |

Blog distribution states are `waiting_translation`, `pending`, `publishing`,
`published`, and `failed`. Existing WeChat rows and behavior stay unchanged.

## Translation Design

The implementation follows `target-running`'s proven separation of source,
translation state, and publication state, but does not import its Python modules
at runtime. Both projects must remain independently deployable.

### Vocabulary policy

The local translator will maintain two explicitly separate inputs:

1. **Academic base glossary:** current-project life-science terminology such as
   AAV and bispecific antibodies. It guides terminology only when the term
   actually appears in the manuscript.
2. **Protected proper nouns:** a reduced, reviewed subset of the `target-running`
   vocabulary: company/brand names, registered product names, and unambiguous
   scientific abbreviations. These are preserved only if present in the source.

Product names such as `PurProX`, `AAVEasy`, and `SOLIDEX` are not injected,
expanded, or used as preferred wording in general-population science articles.
The model prompt instead prioritizes faithful Chinese-to-English scientific
translation, conservative claims, preservation of numerical values, Markdown
structure, DOI/PMID/links, figure placeholders, and author uncertainty.

The local prompt and glossary files are versioned in this repository. Future
terminology improvements can be manually promoted from `target-running` after
review, with no cross-project runtime dependency.

### Provider boundary

All translation code must support injected/mock clients for tests. A real
DeepSeek API key is required only for a live English translation run. It will be
read from a local environment variable, never committed, logged, or copied into
configuration files.

## Images and Covers

Blog articles use original accepted local figures rather than WeChat-hosted
images. Before Blog publication:

1. Resolve accepted figures from the job's existing figure metadata/path rules.
2. Upload each source asset to OSS using a content-hash object key; duplicate
   use reuses the same object.
3. Replace supported image placeholders in the Blog HTML/Markdown with
   `https://img.genemedi.cn/...` URLs.
4. Use the first valid uploaded figure as `field_cover_url`.

Chinese and English versions use the same original image files and CDN URLs.
If no acceptable figure exists, publication proceeds only when the article has
no mandatory image placeholder; otherwise it fails clearly before Drupal is
called. A missing cover is recorded rather than silently substituting a generic
placeholder image.

## Drupal Publication

Use the local `genemedi_blog` client contract with explicit configuration:

- `POST /jsonapi/node/article` for a first manual publication, using
  `status=true`.
- Persist Drupal UUID, node ID, and final URL on success.
- Retry a failed/incomplete publication with `PATCH /jsonapi/node/article/{uuid}`
  if a UUID exists, preserving the URL.
- Never republish a row already marked `published` through the normal action;
  this protects later human Blog edits. A future explicit `force update` action
  can be designed separately.

Before the first live publication, run a read-only/preflight verification of
the Blog language endpoints and accepted Chinese language code. The expected
English code is `en`; the Chinese code is configuration-driven until the Blog
instance confirms its value.

## Admin API and Interface

Add administrator-only endpoints and a compact Blog queue section:

- list queue rows grouped by article and language;
- translate selected English versions;
- publish selected ready versions;
- retry selected failed versions;
- expose status, result URL, error summary, and expandable raw logs.

The selection model is per article-language pair. This prevents publishing an
English version merely because its Chinese source is ready, or the reverse.

## Testing and Rollout

1. Database migration tests for version/distribution uniqueness and transitions.
2. Translation unit tests using a fake client: Chinese source, retained figure
   placeholders, academic glossary matching, and absence of unrelated product
   vocabulary.
3. OSS and Drupal client tests using mocks: image URL insertion, cover choice,
   POST first publish, PATCH retry, and no overwrite after `published`.
4. Admin API tests for batch selection and invalid transitions.
5. Local end-to-end test with mocked provider/OSS/Drupal.
6. After a DeepSeek key is supplied, run one controlled real Chinese-to-English
   translation and inspect its output.
7. With administrator approval, manually publish one Chinese and one English
   Blog article and verify the two final Blog URLs and image rendering.

No scheduler, automated public publishing, WeChat trigger, or rate limiter is
included in this release.
