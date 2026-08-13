# GeneMedi CMS 自动发布接口

这个目录可以整体复制到另一个项目。发布器只依赖 Python 3 标准库，不依赖
`target-running` 的数据库、任务调度器或其他模块。

## 接口地址

Drupal Article 的 JSON:API 资源地址：

```text
创建文章：POST  https://hub.genemedi.net/jsonapi/node/article
更新文章：PATCH https://hub.genemedi.net/jsonapi/node/article/{drupal_uuid}
读取文章：GET   https://hub.genemedi.net/jsonapi/node/article/{drupal_uuid}
```

必须使用 HTTPS。当前 Drupal 可能在响应的 `links.self.href` 中返回 HTTP 地址，
调用方不应据此降级，后续请求仍应使用上面的 HTTPS 地址。

## 鉴权和请求头

接口使用 HTTP Basic Auth。当前临时用户名为 `admin`；密码由项目负责人通过
环境变量或密钥管理工具提供，不要写入代码、JSON 文件或 Git。

```http
Authorization: Basic base64(username:password)
Accept: application/vnd.api+json
Content-Type: application/vnd.api+json
```

## 创建文章

新文章可创建成草稿，即 `status: false`；本项目管理员面板的“自动发布”动作会显式提交 `status: true`：

```json
{
  "data": {
    "type": "node--article",
    "attributes": {
      "title": "Example GeneMedi Product Article",
      "status": false,
      "langcode": "en",
      "body": {
        "value": "<p>This is the article body.</p>",
        "format": "full_html"
      },
      "field_product_series": "AAV Vector Production",
      "field_cover_url": "https://cdn.example.com/images/example-cover.jpg",
      "field_slug": "example-genemedi-product-article"
    }
  }
}
```

字段说明：

| Drupal 字段 | 类型 | 说明 |
| --- | --- | --- |
| `title` | 文本 | 文章标题，创建时必填 |
| `status` | 布尔 | `false` 是草稿，`true` 是公开发布 |
| `langcode` | 文本 | 英文使用 `en` |
| `body.value` | 文本 | 正文，创建时必填 |
| `body.format` | 文本 | 使用 `full_html` 或 `markdown` |
| `field_product_series` | 文本 | 产品类型 |
| `field_cover_url` | 文本 | 封面图片的 HTTP(S) 公网地址，推荐 HTTPS |
| `field_slug` | 文本 | 可选 URL slug |

这个 Blog 没有 `field_target_id`，不要从 Target 项目的载荷中复制该字段；也不要
复制 Target 项目的 `path`、产品 ID 或多语言路径逻辑。

成功创建返回 HTTP `201`。关键返回字段：

```json
{
  "data": {
    "type": "node--article",
    "id": "Drupal UUID",
    "attributes": {
      "drupal_internal__nid": 123,
      "path": {
        "alias": "/blog/example-genemedi-product-article"
      }
    }
  }
}
```

调用方必须保存 `data.id`，后续更新使用的是 UUID，不是数字 NID。

## 更新文章

更新地址：

```text
PATCH https://hub.genemedi.net/jsonapi/node/article/{drupal_uuid}
```

`data.id` 必须与 URL 中的 UUID 相同。只传需要修改的字段，未传字段会保持原值：

```json
{
  "data": {
    "type": "node--article",
    "id": "987f910f-0b6d-4514-9a9b-43d3a802db58",
    "attributes": {
      "field_product_series": "Updated Product Series",
      "field_cover_url": "https://cdn.example.com/images/new-cover.jpg"
    }
  }
}
```

更新成功返回 HTTP `200`。如果不希望改变公开状态，不要在 PATCH 中发送
`status`。要发布已有草稿，发送 `"status": true`；要撤回为草稿，发送
`"status": false`。

## 环境变量

脚本读取以下环境变量：

| 变量 | 必填 | 默认值 |
| --- | --- | --- |
| `GENEMEDI_BLOG_BASE_URL` | 否 | `https://hub.genemedi.net` |
| `GENEMEDI_BLOG_USER` | 是 | 无 |
| `GENEMEDI_BLOG_PASSWORD` | 是 | 无 |
| `GENEMEDI_BLOG_TIMEOUT` | 否 | `30` 秒 |
| `GENEMEDI_BLOG_VERIFY_SSL` | 否 | `true` |

PowerShell：

```powershell
$env:GENEMEDI_BLOG_BASE_URL = "https://hub.genemedi.net"
$env:GENEMEDI_BLOG_USER = "admin"
$env:GENEMEDI_BLOG_PASSWORD = "<由项目负责人提供>"
```

Linux/macOS：

```bash
export GENEMEDI_BLOG_BASE_URL='https://hub.genemedi.net'
export GENEMEDI_BLOG_USER='admin'
export GENEMEDI_BLOG_PASSWORD='<由项目负责人提供>'
```

不要把真实密码填写到 `.env.example` 后提交。生产接入建议改用专用发布账号，
并只授予 Article 读取、创建和更新权限。

## 命令行用法

先复制并修改 `article.example.json`。

只生成请求、不访问网络，也不需要账号密码：

```powershell
python genemedi_blog.py create article.example.json --dry-run
```

创建草稿：

```powershell
python genemedi_blog.py create article.example.json
```

确认草稿内容后公开创建：

```powershell
python genemedi_blog.py create article.example.json --publish
```

更新已有文章但不改变发布状态：

```powershell
python genemedi_blog.py update <drupal_uuid> article.example.json
```

更新并发布，或者更新并改回草稿：

```powershell
python genemedi_blog.py update <drupal_uuid> article.example.json --publish
python genemedi_blog.py update <drupal_uuid> article.example.json --draft
```

只读检查鉴权、Article 资源、POST 方法和新增字段：

```powershell
python genemedi_blog.py probe
```

脚本成功时向 stdout 输出 JSON，失败时向 stderr 输出 JSON 并返回非零退出码。

## 在 Python 项目中调用

把当前目录加入项目后，可以直接导入：

```python
from genemedi_blog import BlogConfig, GeneMediBlogClient

config = BlogConfig.from_env()
client = GeneMediBlogClient(config)

created = client.create(
    {
        "title": "Example GeneMedi Product Article",
        "body": "<p>This is the article body.</p>",
        "body_format": "full_html",
        "langcode": "en",
        "product_series": "AAV Vector Production",
        "cover_url": "https://cdn.example.com/images/example-cover.jpg",
        "slug": "example-genemedi-product-article",
    },
    publish=False,
)

updated = client.update(
    created["uuid"],
    {"cover_url": "https://cdn.example.com/images/new-cover.jpg"},
)
```

`client.create(..., publish=False)` 默认创建草稿。`client.update()` 的 `publish`
默认是 `None`，因此不会改变已有文章的发布状态。

## curl 原始调用示例

假设请求体保存在 `drupal-payload.json`：

```bash
curl --fail-with-body \
  --user "$GENEMEDI_BLOG_USER:$GENEMEDI_BLOG_PASSWORD" \
  --request POST \
  --header 'Accept: application/vnd.api+json' \
  --header 'Content-Type: application/vnd.api+json' \
  --data-binary @drupal-payload.json \
  'https://hub.genemedi.net/jsonapi/node/article'
```

PATCH 只需要替换方法和 URL，并在 JSON 的 `data` 中加入相同 UUID 的 `id`。

## 常见响应

| HTTP 状态 | 含义 |
| --- | --- |
| `200` | GET、OPTIONS 或 PATCH 成功 |
| `201` | POST 创建成功 |
| `400` | JSON:API 结构错误或缺少 `data.type` |
| `401` | 没有提供有效 Basic Auth |
| `403` | 账号没有创建或更新权限 |
| `409` | `data.type` 或 PATCH 的 UUID 与接口不匹配 |
| `422` | 字段值、必填字段或正文格式未通过 Drupal 校验 |

遇到失败时优先读取 JSON:API 响应中的 `errors[].title` 和 `errors[].detail`。
