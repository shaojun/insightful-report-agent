"""Bounded Feishu REST calls. Neither provider payloads nor credentials appear in errors."""

from __future__ import annotations

import json
import re
import threading
import time
from io import BytesIO
from typing import Any
from zipfile import ZipFile, ZipInfo

import httpx

DOMAIN = "https://open.feishu.cn"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_FOLDER_ENTRIES = 100
MAX_FOLDER_DEPTH = 16
_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class FeishuError(Exception):
    """A safe error code, without response text, URLs or request headers."""


class FolderAttachmentError(FeishuError):
    """A folder could not be read completely."""


def identifier(value: str) -> str:
    if not _ID.fullmatch(value):
        raise FeishuError("invalid_identifier")
    return value


def _folder_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or value in {"", ".", ".."}
        or len(value.encode("utf-8", errors="surrogatepass")) > 240
        or re.search(r"[/\\:\x00-\x1f\x7f\ud800-\udfff]", value)
    ):
        raise FolderAttachmentError("invalid_folder_entry_name")
    return value


def _folder_count(value: object) -> int:
    # The API catalog describes counts as strings, while live responses also use numbers.
    if (type(value) is int and value >= 0) or (
        isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value)
    ):
        return int(value)
    raise FolderAttachmentError("invalid_folder_entry_count")


class FeishuClient:
    def __init__(
        self, app_id: str, secret: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._app_id, self._secret = app_id, secret
        self._http = httpx.Client(
            base_url=DOMAIN, transport=transport, timeout=60, follow_redirects=False
        )
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        if not response.is_success:
            raise FeishuError(f"http_{response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise FeishuError("invalid_response") from None
        if not isinstance(body, dict):
            raise FeishuError("invalid_response")
        code = body.get("code")
        if code != 0:
            safe = str(code) if isinstance(code, int) else "invalid_response"
            raise FeishuError(safe)
        return body

    def _authorization(self) -> dict[str, str]:
        with self._lock:
            if time.monotonic() >= self._expires:
                body = self._body(
                    self._http.post(
                        "/open-apis/auth/v3/tenant_access_token/internal",
                        json={"app_id": self._app_id, "app_secret": self._secret},
                    )
                )
                token, expiry = body.get("tenant_access_token"), body.get("expire")
                if not isinstance(token, str) or not isinstance(expiry, int) or expiry <= 0:
                    raise FeishuError("invalid_token_response")
                self._token = token
                self._expires = time.monotonic() + max(0, expiry - 60)
            return {"Authorization": f"Bearer {self._token}"}

    def call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            body = self._body(
                self._http.request(method, path, headers=self._authorization(), **kwargs)
            )
        except httpx.HTTPError:
            raise FeishuError("transport_error") from None
        return body

    def identity(self) -> str:
        body = self.call("GET", "/open-apis/bot/v3/info")
        bot = body.get("bot") or {}
        return identifier(str(bot.get("open_id") or ""))

    def send(
        self, chat: str, root: str, kind: str, content: dict[str, Any], *, uuid: str = ""
    ) -> str:
        fields: dict[str, Any] = {
            "msg_type": kind,
            "content": json.dumps(content, ensure_ascii=False),
        }
        if uuid:
            fields["uuid"] = uuid
        if root != "dm":
            fields["reply_in_thread"] = True
            path = f"/open-apis/im/v1/messages/{identifier(root)}/reply"
            body = self.call("POST", path, json=fields)
        else:
            fields["receive_id"] = identifier(chat)
            body = self.call(
                "POST",
                "/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json=fields,
            )
        message = (body.get("data") or {}).get("message_id")
        return identifier(str(message or ""))

    def edit(self, message: str, text: str) -> None:
        self.call(
            "PUT",
            f"/open-apis/im/v1/messages/{identifier(message)}",
            json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        )

    def react(self, message: str, emoji_type: str = "Get") -> None:
        self.call(
            "POST",
            f"/open-apis/im/v1/messages/{identifier(message)}/reactions",
            json={"reaction_type": {"emoji_type": emoji_type}},
        )

    def edit_card(self, message: str, card: dict[str, Any]) -> None:
        self.call(
            "PATCH",
            f"/open-apis/im/v1/messages/{identifier(message)}",
            json={"content": json.dumps(card, ensure_ascii=False)},
        )

    def folder_zip(self, message: str, key: str, name: str) -> tuple[str, bytes]:
        """Resolve a chat folder and package its file bytes for the existing ZIP intake."""
        try:
            message = identifier(message)
            root = _folder_name(name)
            path = f"/open-apis/im/v1/files/{identifier(key)}/folder"
            body = self.call(
                "GET", path, params={"srctype": "message", "srcid": message, "recursive": "true"}
            )
            data = body.get("data")
            if not isinstance(data, dict):
                raise FolderAttachmentError("invalid_folder_response")
            files: list[tuple[str, str]] = []
            seen: set[str] = set()

            def collect(items: object, parent: str, depth: int) -> int:
                if not isinstance(items, list) or depth > MAX_FOLDER_DEPTH:
                    raise FolderAttachmentError("invalid_folder_tree")
                start = len(seen)
                for item in items:
                    if not isinstance(item, dict) or type(item.get("is_folder")) is not bool:
                        raise FolderAttachmentError("invalid_folder_entry")
                    relative = f"{parent}/{_folder_name(item.get('name'))}"
                    if relative in seen or len(seen) >= MAX_FOLDER_ENTRIES:
                        raise FolderAttachmentError("duplicate_path_or_too_many_folder_entries")
                    seen.add(relative)
                    if item["is_folder"]:
                        count = _folder_count(item.get("children_count", 0))
                        descendants = collect(item.get("children", []), relative, depth + 1)
                        if count > descendants:
                            raise FolderAttachmentError("incomplete_folder_tree")
                    else:
                        files.append((relative, identifier(str(item.get("file_key") or ""))))
                return len(seen) - start

            count = collect(data.get("items", []), root, 1)
            # With recursive=true the live API counts the root itself in all_count.
            if _folder_count(data.get("all_count", count + 1)) > count + 1:
                raise FolderAttachmentError("incomplete_folder_tree")
            if not files:
                raise FolderAttachmentError("empty_folder")
            output = BytesIO()
            with ZipFile(output, "w") as archive:
                for relative, child in sorted(files):
                    content = self.download(
                        message, child, "file", max_bytes=MAX_DOWNLOAD_BYTES - output.tell()
                    )
                    # A fixed member timestamp keeps identical re-uploads content-addressable.
                    archive.writestr(ZipInfo(relative), content)
            if output.tell() > MAX_DOWNLOAD_BYTES:
                raise FolderAttachmentError("folder_too_large")
            return f"{root}.zip", output.getvalue()
        except FolderAttachmentError:
            raise
        except FeishuError as error:
            raise FolderAttachmentError(str(error)) from None

    def download(
        self, message: str, key: str, kind: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES
    ) -> bytes:
        path = f"/open-apis/im/v1/messages/{identifier(message)}/resources/{identifier(key)}"
        try:
            with self._http.stream(
                "GET", path, params={"type": kind}, headers=self._authorization()
            ) as response:
                if response.status_code != 200:
                    raise FeishuError(f"download_http_{response.status_code}")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > max_bytes:
                        raise FeishuError("attachment_too_large")
                    content.extend(chunk)
                if (
                    "application/json" in response.headers.get("content-type", "")
                    and "content-disposition" not in response.headers
                ):
                    try:
                        body = json.loads(content)
                    except ValueError:
                        body = None
                    if (
                        isinstance(body, dict)
                        and isinstance(body.get("code"), int)
                        and body["code"] != 0
                        and "msg" in body
                    ):
                        raise FeishuError(f"resource_{body['code']}")
                return bytes(content)
        except httpx.HTTPError:
            raise FeishuError("transport_error") from None

    def upload(self, name: str, content: bytes) -> str:
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise FeishuError("file_must_be_between_1_byte_and_30_MiB")
        body = self.call(
            "POST",
            "/open-apis/im/v1/files",
            data={"file_type": "stream", "file_name": name},
            files={"file": (name, content, "application/octet-stream")},
        )
        return identifier(str((body.get("data") or {}).get("file_key") or ""))

    def send_file(self, chat: str, root: str, name: str, content: bytes, *, uuid: str) -> None:
        key = self.upload(name, content)
        self.send(chat, root, "file", {"file_key": key}, uuid=uuid)

    def close(self) -> None:
        self._http.close()
