"""微信公众号 API 客户端 —— wechat-article Phase 0。

实现的端点（最小集，够 Phase 0 用）：
- ``get_access_token()``：拿 / 复用 access_token，2 小时缓存，单飞
- ``create_draft(payload)``：``/cgi-bin/draft/add``，返回 ``media_id``
- ``update_draft(media_id, index, payload)``：``/cgi-bin/draft/update``，重发用
- ``upload_image(local_path)``：``/cgi-bin/media/uploadimg``，返回 mmbiz URL（图片专用，永久）

- ``add_permanent_material(local_path, type)``：``/cgi-bin/material/add_material``，
  返回永久素材 ``media_id``（可作草稿封面 ``thumb_media_id``；与 uploadimg 不同）

不实现：freepublish、消息群发。

access_token 缓存策略：内存 + 文件，**按账户隔离** ``runtime/wechat_token_{account}.json``。
多账户（AAV / 免疫客）各自一个 token 文件，避免互相覆盖（微信每个账户同一时刻只允许
一个有效 token，新拿一个旧的就失效）；多进程通过同一文件共享同账户的 token。
凭据按账户读 ``WECHAT_{ACCOUNT}_APP_ID/SECRET``（回落通用 ``WECHAT_APP_ID/SECRET``）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from utils.logger import setup_logger

logger = setup_logger("wechat_client")

WECHAT_API_BASE = "https://api.weixin.qq.com"
TOKEN_REFRESH_MARGIN_SECONDS = 300  # 提前 5 分钟视为过期


class WeChatAPIError(Exception):
    """微信 API 调用失败（凭据缺失 / 网络异常 / errcode 非 0 等）。"""

    def __init__(self, message: str, errcode: Optional[int] = None, raw: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.errcode = errcode
        self.raw = raw or {}


class WeChatClient:
    """公众号 API 客户端。线程安全的 access_token 刷新。"""

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        account: str = "default",
        token_cache_path: Optional[str] = None,
        timeout: float = 20.0,
    ):
        self.account = (account or "default").strip() or "default"
        acc = self.account.upper()
        # 凭据优先级：显式参数 > 账户命名空间 env（WECHAT_{ACCOUNT}_*）> 通用 env（WECHAT_*）
        self.app_id = (
            app_id
            or os.getenv(f"WECHAT_{acc}_APP_ID", "")
            or os.getenv("WECHAT_APP_ID", "")
        ).strip()
        self.app_secret = (
            app_secret
            or os.getenv(f"WECHAT_{acc}_APP_SECRET", "")
            or os.getenv("WECHAT_APP_SECRET", "")
        ).strip()
        if not (self.app_id and self.app_secret):
            raise WeChatAPIError(
                f"账户 '{self.account}' 凭据未配置（WECHAT_{acc}_APP_ID / WECHAT_{acc}_APP_SECRET）"
            )
        self.timeout = float(timeout)
        # token 按账户隔离：微信每个账户同一时刻只允许一个有效 token，多账户各用各的文件
        default_cache = (
            Path(__file__).resolve().parent.parent / "runtime" / f"wechat_token_{self.account}.json"
        )
        self.token_cache_path = Path(token_cache_path) if token_cache_path else default_cache
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    # ----------------- access_token -----------------

    def get_access_token(self, force_refresh: bool = False) -> str:
        """拿 access_token。命中内存缓存优先，再命中文件缓存，最后调 API。"""
        now = time.time()
        # 1. 内存缓存
        if not force_refresh and self._token and self._token_expires_at > now + TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        # 2. 文件缓存（跨进程共享）
        with self._token_lock:
            # 双检：进了锁之后再看一次内存（其它线程可能刚刷过）
            if not force_refresh and self._token and self._token_expires_at > now + TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token
            if not force_refresh:
                cached = self._read_token_file()
                if cached:
                    token, expires_at = cached
                    if expires_at > now + TOKEN_REFRESH_MARGIN_SECONDS:
                        self._token = token
                        self._token_expires_at = expires_at
                        return token
            # 3. 调 API 刷新
            return self._refresh_token()

    def _refresh_token(self) -> str:
        params = {
            "grant_type": "client_credential",
            "appid": self.app_id,
            "secret": self.app_secret,
        }
        url = f"{WECHAT_API_BASE}/cgi-bin/token?{urlencode(params)}"
        status, body = self._http_get(url)
        data = self._parse_json(body)
        if status != 200 or "access_token" not in data:
            raise WeChatAPIError(
                f"refresh access_token failed: {data}",
                errcode=data.get("errcode"),
                raw=data,
            )
        token = str(data["access_token"])
        expires_in = int(data.get("expires_in") or 7200)
        self._token = token
        self._token_expires_at = time.time() + expires_in
        self._write_token_file(token, self._token_expires_at)
        logger.info("access_token refreshed, expires in %ds", expires_in)
        return token

    def _read_token_file(self) -> Optional[Tuple[str, float]]:
        try:
            with self.token_cache_path.open("r", encoding="utf-8") as f:
                d = json.load(f)
            return str(d["access_token"]), float(d["expires_at"])
        except (FileNotFoundError, KeyError, ValueError, OSError):
            return None

    def _write_token_file(self, token: str, expires_at: float) -> None:
        try:
            with self.token_cache_path.open("w", encoding="utf-8") as f:
                json.dump({"access_token": token, "expires_at": expires_at}, f)
        except OSError as exc:
            logger.warning("write token cache failed: %s", exc)

    # ----------------- draft / image -----------------

    def create_draft(self, articles: list[Dict[str, Any]]) -> str:
        """POST /cgi-bin/draft/add。articles 至少 1 篇，多篇时即"多图文"。

        每篇 article 必填字段：``title, author, digest, content, content_source_url,
        thumb_media_id, need_open_comment, only_fans_can_comment``。
        ``content`` 是 HTML 字符串（公众号会原样渲染，但有 < img/style/script 等限制）。

        Returns:
            media_id（草稿的唯一标识，重发时用）。
        """
        if not articles:
            raise WeChatAPIError("create_draft: articles must be non-empty")
        token = self.get_access_token()
        url = f"{WECHAT_API_BASE}/cgi-bin/draft/add?access_token={token}"
        payload = {"articles": articles}
        status, body = self._http_post_json(url, payload)
        data = self._parse_json(body)
        if status != 200 or "media_id" not in data:
            self._maybe_invalidate_token(data)
            raise WeChatAPIError(f"create_draft failed: {data}", errcode=data.get("errcode"), raw=data)
        media_id = str(data["media_id"])
        logger.info("draft created media_id=%s articles=%d", media_id, len(articles))
        return media_id

    def update_draft(self, media_id: str, index: int, article: Dict[str, Any]) -> None:
        """PATCH 已存在草稿的某一篇（index 从 0 起）。复刻 target-running 的 PATCH 思路。"""
        token = self.get_access_token()
        url = f"{WECHAT_API_BASE}/cgi-bin/draft/update?access_token={token}"
        payload = {"media_id": media_id, "index": int(index), "articles": article}
        status, body = self._http_post_json(url, payload)
        data = self._parse_json(body)
        if status != 200 or data.get("errcode", 0) != 0:
            self._maybe_invalidate_token(data)
            raise WeChatAPIError(f"update_draft failed: {data}", errcode=data.get("errcode"), raw=data)
        logger.info("draft updated media_id=%s index=%d", media_id, index)

    def get_draft(self, media_id: str) -> Dict[str, Any]:
        """GET /cgi-bin/draft/get —— 取已存草稿内容（重投 PATCH 时复用其封面 thumb_media_id）。"""
        token = self.get_access_token()
        url = f"{WECHAT_API_BASE}/cgi-bin/draft/get?access_token={token}"
        status, body = self._http_post_json(url, {"media_id": media_id})
        data = self._parse_json(body)
        if status != 200 or "news_item" not in data:
            self._maybe_invalidate_token(data)
            raise WeChatAPIError(f"get_draft failed: {data}", errcode=data.get("errcode"), raw=data)
        return data

    def upload_image(self, local_path: str) -> str:
        """POST /cgi-bin/media/uploadimg。仅供公众号图文正文里 <img src> 用。

        返回的 URL 形如 ``https://mmbiz.qpic.cn/...``，**永久有效**（与 5000/日配额）。
        Phase 0 不调；Phase 1 起 image_provider 调它。
        """
        path = Path(local_path)
        if not path.exists():
            raise WeChatAPIError(f"image not found: {local_path}")
        token = self.get_access_token()
        url = f"{WECHAT_API_BASE}/cgi-bin/media/uploadimg?access_token={token}"
        status, body = self._http_post_multipart(url, path)
        data = self._parse_json(body)
        if status != 200 or "url" not in data:
            self._maybe_invalidate_token(data)
            raise WeChatAPIError(f"upload_image failed: {data}", errcode=data.get("errcode"), raw=data)
        mmbiz_url = str(data["url"])
        logger.info("image uploaded: %s -> %s", path.name, mmbiz_url)
        return mmbiz_url

    def add_permanent_material(self, local_path: str, material_type: str = "image") -> str:
        """POST /cgi-bin/material/add_material?type=image —— 上传永久素材，返回 media_id。

        与 ``upload_image``（media/uploadimg，只供正文 <img src> 用、返回 URL 不返回 media_id）
        不同：永久素材返回的 ``media_id`` 可直接作草稿封面 ``thumb_media_id``。
        注意永久图片素材有数量上限（公众号后台可查），调用方应做去重缓存。
        """
        path = Path(local_path)
        if not path.exists():
            raise WeChatAPIError(f"material not found: {local_path}")
        token = self.get_access_token()
        url = (
            f"{WECHAT_API_BASE}/cgi-bin/material/add_material"
            f"?access_token={token}&type={material_type}"
        )
        status, body = self._http_post_multipart(url, path)
        data = self._parse_json(body)
        if status != 200 or "media_id" not in data:
            self._maybe_invalidate_token(data)
            raise WeChatAPIError(f"add_material failed: {data}", errcode=data.get("errcode"), raw=data)
        media_id = str(data["media_id"])
        logger.info("permanent material added: %s -> media_id=%s", path.name, media_id)
        return media_id

    def batchget_freepublish(self, offset: int = 0, count: int = 20, no_content: int = 0) -> Dict[str, Any]:
        """POST /cgi-bin/freepublish/batchget —— 拉取已发布图文列表（默认含正文 content）。

        用于从已发布文章里抽取人工做好的内容（如文末产品模块）。``count`` ≤ 20；
        ``no_content=0`` 返回正文 HTML。返回原始 dict（item[].content.news_item[]）。
        """
        token = self.get_access_token()
        url = f"{WECHAT_API_BASE}/cgi-bin/freepublish/batchget?access_token={token}"
        payload = {"offset": int(offset), "count": int(count), "no_content": int(no_content)}
        status, body = self._http_post_json(url, payload)
        data = self._parse_json(body)
        if status != 200 or "item" not in data:
            self._maybe_invalidate_token(data)
            raise WeChatAPIError(f"freepublish batchget failed: {data}", errcode=data.get("errcode"), raw=data)
        return data

    # ----------------- HTTP plumbing -----------------

    def _http_get(self, url: str) -> Tuple[int, str]:
        req = urllib_request.Request(url, method="GET")
        return self._send(req)

    def _http_post_json(self, url: str, payload: Dict[str, Any]) -> Tuple[int, str]:
        # 注意：微信要求 UTF-8 + Content-Type=application/json；中文不能被转 \u 转义否则后台显示乱码
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        return self._send(req)

    def _http_post_multipart(self, url: str, path: Path) -> Tuple[int, str]:
        boundary = f"----wechatupload{int(time.time() * 1000)}"
        body_parts: list[bytes] = []
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(
            f'Content-Disposition: form-data; name="media"; filename="{path.name}"\r\n'.encode()
        )
        body_parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        body_parts.append(path.read_bytes())
        body_parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(body_parts)
        req = urllib_request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        return self._send(req)

    def _send(self, req: urllib_request.Request) -> Tuple[int, str]:
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return exc.code, body
        except URLError as exc:
            raise WeChatAPIError(f"network failure: {exc}") from exc

    @staticmethod
    def _parse_json(body: str) -> Dict[str, Any]:
        try:
            return json.loads(body) if body else {}
        except (ValueError, TypeError):
            return {"_raw_body": body}

    def _maybe_invalidate_token(self, data: Dict[str, Any]) -> None:
        """40001/42001/40014 这几个 errcode 都意味着 token 失效，下次调用强刷。"""
        code = data.get("errcode")
        if code in (40001, 42001, 40014):
            logger.warning("token errcode=%s, will refresh next call", code)
            self._token = None
            self._token_expires_at = 0.0
            try:
                self.token_cache_path.unlink(missing_ok=True)
            except OSError:
                pass
