#!/usr/bin/env python3
import argparse
import asyncio
import contextlib
import json
import mimetypes
import os
import re
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import websockets
    WEBSOCKETS_IMPORT_ERROR: Optional[Exception] = None
except ModuleNotFoundError as exc:
    websockets = None  # type: ignore[assignment]
    WEBSOCKETS_IMPORT_ERROR = exc


APP_ID = os.environ.get("QQ_APP_ID", "").strip()
APP_SECRET = os.environ.get("QQ_APP_SECRET", "").strip()
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parents[1]

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_SAVE_DIR = Path(os.environ.get("QQ_SAVE_DIR", str(PROJECT_DIR / "data" / "received_files")))
DEFAULT_LOG_FILE = Path(os.environ.get("QQ_RECEIVE_LOG", str(PROJECT_DIR / "logs" / "received_messages.jsonl")))

STOP = False

MEDIA_CONTEXT_KEYS = {
    "attachment", "attachments", "file", "files", "file_info", "image", "images",
    "video", "videos", "audio", "audios", "media", "resources", "resource",
}
DOWNLOAD_URL_KEYS = {
    "url", "file_url", "download_url", "media_url", "image_url", "video_url",
    "audio_url", "src", "file",
}
NAME_KEYS = ("filename", "file_name", "name", "title", "display_name")
TYPE_KEYS = ("type", "file_type", "content_type", "mime_type")


def now_ts() -> int:
    return int(time.time())


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def expand_path(path: Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)


def safe_filename(name: Optional[str], fallback: str) -> str:
    raw = str(name or fallback).strip()
    raw = urllib.parse.unquote(raw.split("?", 1)[0].split("#", 1)[0])
    raw = Path(raw).name
    raw = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", raw).strip(" .")
    return (raw or fallback)[:180]


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 10000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不冲突的文件名: {path}")


def is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def first_string(obj: Dict[str, Any], keys) -> Optional[str]:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def guess_kind(obj: Dict[str, Any], path: str) -> str:
    raw_type = first_string(obj, TYPE_KEYS) or ""
    value = raw_type.lower()
    if value in {"1", "image"} or "image" in value:
        return "image"
    if value in {"2", "video"} or "video" in value:
        return "video"
    if value in {"3", "audio", "voice"} or "audio" in value or "voice" in value:
        return "audio"
    if value in {"4", "file"}:
        return "file"

    lowered_path = path.lower()
    for kind in ("image", "video", "audio", "file"):
        if kind in lowered_path:
            return kind
    return "file"


def guess_ext(kind: str, content_type: str) -> str:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    ext = mimetypes.guess_extension(content_type) if content_type else None
    if ext:
        return ext
    return {
        "image": ".jpg",
        "video": ".mp4",
        "audio": ".mp3",
        "file": ".bin",
    }.get(kind, ".bin")


def iter_file_candidates(obj: Any, path: str = "$", in_media_context: bool = False):
    if isinstance(obj, dict):
        lowered_keys = {str(key).lower() for key in obj}
        context = in_media_context or bool(lowered_keys & MEDIA_CONTEXT_KEYS)

        url = None
        for key, value in obj.items():
            if str(key).lower() in DOWNLOAD_URL_KEYS and is_http_url(value):
                url = value
                break

        if not url and context:
            for value in obj.values():
                if is_http_url(value):
                    url = value
                    break

        has_file_info = context and (
            url is not None
            or bool(lowered_keys & set(NAME_KEYS))
            or bool(lowered_keys & {"size", "file_size", "file_info", "content_type", "mime_type"})
        )
        if has_file_info:
            name = first_string(obj, NAME_KEYS)
            if not name and url:
                name = Path(urllib.parse.urlparse(url).path).name
            yield {
                "path": path,
                "url": url,
                "name": name,
                "kind": guess_kind(obj, path),
                "raw": obj,
            }

        for key, value in obj.items():
            child_context = context or str(key).lower() in MEDIA_CONTEXT_KEYS
            yield from iter_file_candidates(value, f"{path}.{key}", child_context)

    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            yield from iter_file_candidates(item, f"{path}[{index}]", in_media_context)


def extract_file_candidates(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = []
    seen = set()
    for candidate in iter_file_candidates(data):
        raw_key = json.dumps(candidate.get("raw", {}), ensure_ascii=False, sort_keys=True, default=str)
        key = (candidate.get("url"), candidate.get("name"), raw_key)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


class TokenManager:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = None
        self.expire_at = 0

    def get_token(self) -> str:
        if self.access_token and time.time() < self.expire_at - 120:
            return self.access_token

        payload = json.dumps({
            "appId": self.app_id,
            "clientSecret": self.app_secret,
        }).encode("utf-8")

        req = urllib.request.Request(
            TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        self.access_token = data["access_token"]
        self.expire_at = int(time.time()) + int(data.get("expires_in", 7200))
        return self.access_token


class QQFileReceiver:
    def __init__(self, save_dir: Path, log_file: Path, max_bytes: int):
        self.save_dir = expand_path(save_dir)
        self.log_file = expand_path(log_file)
        self.max_bytes = max_bytes
        self.token_mgr = TokenManager(APP_ID, APP_SECRET)
        self.seq = None
        self.seen_msg_ids = set()

    def auth_header(self) -> Dict[str, str]:
        return {"Authorization": f"QQBot {self.token_mgr.get_token()}"}

    def http_get_json(self, url: str) -> Dict[str, Any]:
        req = urllib.request.Request(url, headers=self.auth_header(), method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def http_post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", **self.auth_header()},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}

    def get_gateway_url(self) -> str:
        return self.http_get_json(f"{API_BASE}/gateway")["url"]

    def reply_text(self, event_type: str, data: Dict[str, Any], content: str) -> None:
        msg_id = data.get("id")
        body = {
            "content": content,
            "msg_type": 0,
            "msg_id": msg_id,
            "msg_seq": 1,
        }

        if event_type == "C2C_MESSAGE_CREATE":
            author = data.get("author", {})
            user_openid = author.get("user_openid")
            if not user_openid:
                raise RuntimeError("C2C 消息缺少 author.user_openid，无法回复")
            self.http_post_json(f"{API_BASE}/v2/users/{user_openid}/messages", body)
            return

        if event_type == "GROUP_AT_MESSAGE_CREATE":
            group_openid = data.get("group_openid") or data.get("group_id")
            if not group_openid:
                raise RuntimeError("群消息缺少 group_openid，无法回复")
            self.http_post_json(f"{API_BASE}/v2/groups/{group_openid}/messages", body)
            return

        raise RuntimeError(f"暂不支持回复事件类型: {event_type}")

    def download_candidate(self, candidate: Dict[str, Any], msg_dir: Path, msg_id: str, index: int) -> Path:
        url = candidate.get("url")
        if not url:
            raise ValueError("候选文件没有可下载 URL")

        fallback_name = f"{msg_id}_{index}"
        filename = safe_filename(candidate.get("name"), fallback_name)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "qq-file-receiver/1.0"},
            method="GET",
        )

        msg_dir.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=120) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if not Path(filename).suffix:
                filename = f"{filename}{guess_ext(candidate.get('kind', 'file'), content_type)}"

            content_length = int(resp.headers.get("Content-Length") or 0)
            if content_length and content_length > self.max_bytes:
                raise ValueError(f"文件过大: {content_length} bytes > {self.max_bytes} bytes")

            path = unique_path(msg_dir / filename)
            written = 0
            try:
                with path.open("wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise ValueError(f"文件超过大小限制: {self.max_bytes} bytes")
                        f.write(chunk)
            except Exception:
                if path.exists():
                    path.unlink()
                raise
        return path

    def save_message(self, event_type: str, data: Dict[str, Any]) -> List[Path]:
        msg_id = str(data.get("id") or f"no_id_{now_stamp()}")
        msg_dir = self.save_dir / time.strftime("%Y%m%d") / safe_filename(msg_id, "message")
        candidates = extract_file_candidates(data)
        saved_paths: List[Path] = []
        errors = []

        for index, candidate in enumerate(candidates, start=1):
            if not candidate.get("url"):
                continue
            try:
                saved_paths.append(self.download_candidate(candidate, msg_dir, msg_id, index))
            except Exception as exc:
                errors.append({
                    "candidate_path": candidate.get("path"),
                    "url": candidate.get("url"),
                    "error": str(exc),
                })

        metadata_path = msg_dir / "message.json"
        save_json(metadata_path, {
            "ts": now_ts(),
            "event_type": event_type,
            "msg_id": msg_id,
            "saved_paths": [str(path) for path in saved_paths],
            "download_errors": errors,
            "file_candidates": candidates,
            "raw": data,
        })

        if not saved_paths:
            saved_paths.append(metadata_path)

        append_jsonl(self.log_file, {
            "ts": now_ts(),
            "event_type": event_type,
            "msg_id": msg_id,
            "saved_paths": [str(path) for path in saved_paths],
            "metadata_path": str(metadata_path),
            "candidate_count": len(candidates),
            "download_error_count": len(errors),
        })
        return saved_paths

    async def heartbeat_loop(self, ws, interval_ms: int) -> None:
        interval = max(interval_ms / 1000.0, 1.0)
        while not STOP:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": 1, "d": self.seq}))

    async def handle_message_event(self, event_type: str, data: Dict[str, Any]) -> None:
        msg_id = data.get("id")
        if msg_id and msg_id in self.seen_msg_ids:
            print(f"[DUPLICATE] msg_id={msg_id}", flush=True)
            return
        if msg_id:
            self.seen_msg_ids.add(msg_id)

        saved_paths = self.save_message(event_type, data)
        filepath = str(saved_paths[0])
        print(f"[SAVED] {event_type} msg_id={msg_id} -> {filepath}", flush=True)

        try:
            self.reply_text(event_type, data, f"收到，消息保存在{filepath}")
            print(f"[REPLY] {filepath}", flush=True)
        except Exception as exc:
            print(f"[REPLY_ERROR] {exc}", file=sys.stderr, flush=True)

    async def handle_dispatch(self, payload: Dict[str, Any]) -> None:
        event_type = payload.get("t")
        data = payload.get("d", {})
        self.seq = payload.get("s", self.seq)

        if event_type == "READY":
            print(f"[READY] user={data.get('user', {}).get('username')}", flush=True)
            return

        if event_type in {"C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"}:
            await self.handle_message_event(event_type, data)
            return

        append_jsonl(self.log_file, {
            "ts": now_ts(),
            "event_type": event_type,
            "raw": data,
        })
        print(f"[EVENT] {event_type}", flush=True)

    async def run_once(self) -> None:
        gateway_url = self.get_gateway_url()
        token = self.token_mgr.get_token()
        ssl_context = ssl.create_default_context()

        async with websockets.connect(
            gateway_url,
            ssl=ssl_context,
            ping_interval=None,
            max_size=32 * 1024 * 1024,
            open_timeout=20,
            close_timeout=10,
        ) as ws:
            raw = await ws.recv()
            hello = json.loads(raw)
            if hello.get("op") != 10:
                raise RuntimeError(f"unexpected hello payload: {hello}")
            heartbeat_interval = int(hello["d"]["heartbeat_interval"])

            identify = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": 1 << 25,
                    "shard": [0, 1],
                    "properties": {
                        "$os": "linux",
                        "$browser": "python-file-receiver",
                        "$device": "python-file-receiver",
                    },
                },
            }
            await ws.send(json.dumps(identify))

            hb_task = asyncio.create_task(self.heartbeat_loop(ws, heartbeat_interval))
            try:
                while not STOP:
                    payload = json.loads(await ws.recv())
                    op = payload.get("op")
                    if op == 11:
                        continue
                    if op == 0:
                        await self.handle_dispatch(payload)
            finally:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task

    async def run_forever(self) -> None:
        backoff = 3
        while not STOP:
            try:
                await self.run_once()
                backoff = 3
            except Exception as exc:
                print(f"[RECONNECT] {exc}", file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def handle_stop(signum, frame) -> None:
    del frame
    global STOP
    STOP = True
    print(f"[STOP] signal={signum}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="监听 QQ 消息，保存文件或原始消息，并回复保存路径")
    parser.add_argument("--save-dir", default=str(DEFAULT_SAVE_DIR), help="收到的文件保存目录")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE), help="消息索引 JSONL")
    parser.add_argument("--max-mb", type=int, default=200, help="单个下载文件大小上限，单位 MB")
    args = parser.parse_args()

    if not APP_ID or not APP_SECRET:
        print("错误：请先设置环境变量 QQ_APP_ID 和 QQ_APP_SECRET", file=sys.stderr)
        return 1
    if websockets is None:
        print("错误：运行接收脚本需要先安装 websockets，例如: pip install websockets", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    receiver = QQFileReceiver(
        save_dir=Path(args.save_dir),
        log_file=Path(args.log_file),
        max_bytes=args.max_mb * 1024 * 1024,
    )
    asyncio.run(receiver.run_forever())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
