# WeChat Published Article to GeneMedi Blog Sync Design

## Goal

Use a public WeChat article as the sole approval gate for GeneMedi Blog. After a
configured WeChat official account publishes a new article, publish the final
WeChat title, body, images, and cover to GeneMedi Blog. The workflow must not
require an additional business-user action.

## Scope and Decisions

- Monitor the configured AAV and 免疫客 official accounts.
- Start with articles published after the monitor is enabled. Do not backfill
  existing history.
- Use the final public WeChat article as the content source, including edits
  made in the WeChat draft box.
- Publish to GeneMedi Blog only after every body image and the cover have been
  copied to OSS and converted to `https://img.genemedi.cn/...` URLs.
- Create the Blog article publicly (`status: true`), rather than as a Blog
  draft. WeChat publication is the review and approval step.
- Do not delete, unpublish, or update a Blog article when the corresponding
  WeChat article is later changed, deleted, or returned to draft. Reverse and
  post-publication synchronization are explicitly out of scope for this phase.
- Keep the business operator page unchanged: upload PDF and generate selected
  files remain its only actions.

## Chosen Architecture

The public WeChat API service runs a five-minute systemd timer. It uses the
official `freepublish/batchget` endpoint for each configured account, then
uses `freepublish/getarticle` for each new item. Polling is the primary
trigger because business users publish through the WeChat backend. The
`PUBLISHJOBFINISH` callback is only an optional future accelerator for API
submitted publishing and is not required for correctness.

Each published WeChat news item is handled independently. A multi-item WeChat
article therefore produces one Blog article per item. The immutable source key
is `(wechat_account, wechat_article_id, article_index)`.

```text
WeChat manual publish
        |
        v
five-minute sync timer on public API service
        |
        +--> published-list scan and deduplication
        |
        +--> fetch final WeChat article HTML
        |
        +--> download cover and body images
        |
        +--> upload images to private OSS, receive CDN URLs
        |
        +--> create public GeneMedi Blog article
        |
        v
sync record, log, and administrator status
```

This runs on the public API service, not the intranet resource server. It
already has the necessary WeChat outbound access and IP-whitelist position;
the final public WeChat article provides all content needed for Blog sync.

## Image and HTML Handling

The sync reads the final WeChat HTML without regenerating or rewriting the
article text. It parses the HTML structurally, finds each `img` element, and
prefers its `data-src` URL with `src` as a fallback. Each image is downloaded,
validated as an image response, and uploaded with its detected content type to
the private OSS bucket.

OSS object keys are content-addressed:

```text
article-assets/wechat/{account}/{sha256}.{extension}
```

The returned CDN URL replaces the element `src`; obsolete WeChat `data-src`
attributes are removed. The WeChat cover URL is processed through the same
store and sent as Blog `field_cover_url`. Existing image bytes therefore reuse
the same OSS object across articles while Blog never depends on a WeChat
`mmbiz` URL.

If downloading, validating, uploading, or replacing any required image fails,
the Blog article is not created. The sync record remains retryable and records
the precise failure. It is preferable to delay a Blog post to publishing an
article with broken or missing images.

## Persistent State and Idempotency

Add two tables independent of the existing PDF-job `distributions` table,
because manually created WeChat articles have no local PDF job:

1. `wechat_sync_checkpoints`: one row per account containing the latest
   observed `update_time` and article IDs at that timestamp. Its first run
   stores the current watermark and publishes nothing, which prevents
   historical backfill.
2. `wechat_publication_syncs`: one row per account/article/item source key,
   containing source title and URL, WeChat update time, `pending`,
   `publishing`, `published`, or `failed` state, attempt timing, error text,
   Blog UUID, and Blog URL.

The worker creates a deterministic Blog slug from the source key and checks
for an existing Blog record with that slug before retrying a previous uncertain
write. This protects against duplicate Blog posts if the Blog API created an
article but its response was lost. A successfully synced source key is never
published again by later scans.

## Failure Handling and Visibility

- A per-item failure never stops scanning or publishing other newly published
  WeChat items.
- Failed items retry after 5, 15, 60 minutes, then every 6 hours until they
  succeed or an administrator resolves the cause.
- Logs identify the WeChat account, article ID, item index, source URL, and
  the failed image or Blog API operation.
- The existing administrator page adds a read-only Blog-sync section with
  published, pending, and failed counts plus the latest failure message.
- The business operator page remains intentionally free of this operational
  detail.

## Configuration

No credential is committed. The public service environment supplies:

- existing WeChat account credentials and account list;
- `ALIYUN_OSS_ACCESS_KEY_ID`, `ALIYUN_OSS_ACCESS_KEY_SECRET`, bucket, endpoint,
  object prefix, and CDN base URL;
- `GENEMEDI_BLOG_BASE_URL`, `GENEMEDI_BLOG_USER`, and
  `GENEMEDI_BLOG_PASSWORD`;
- account-to-product-series mapping for Blog metadata.

The previously exposed OSS access key must be rotated before enabling the
timer. The implementation will validate configuration at startup and leave the
timer inactive when any required setting is absent.

## Verification

Automated tests will cover checkpoint initialization without backfill, new-item
detection, multi-item handling, source-key deduplication, retry scheduling,
final HTML image replacement, cover upload, image failure isolation, and Blog
payload creation. An integration smoke test will use a non-production WeChat
publication or an explicitly selected live article, verify every generated CDN
image returns HTTP 200, and verify the resulting Blog article is public with
the expected final content.
