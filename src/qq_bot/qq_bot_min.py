#!/usr/bin/env python3
import asyncio
import json
import os
import signal
import ssl
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import websockets

APP_ID = os.environ.get("QQ_APP_ID", "").strip()
APP_SECRET = os.environ.get("QQ_APP_SECRET", "").strip()

if not APP_ID or not APP_SECRET:
    print("请先设置环境变量 QQ_APP_ID 和 QQ_APP_SECRET", file=sys.stderr)
    sys.exit(1)

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parents[1]
MSG_FILE = PROJECT_DIR / "data" / "messages.jsonl"
USER_FILE = PROJECT_DIR / "data" / "users.jsonl"

STOP = False


def now_ts() -> int:
    return int(time.time())


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


class TokenManager:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = None
        self.expire_at = 0

    def get_token(self) -> str:
        # 官方说明：access_token 生命周期默认约 7200 秒，接近过期时需要自行刷新
        if self.access_token and time.time() < self.expire_at - 120:
            return self.access_token

        payload = json.dumps({
            "appId": self.app_id,
            "clientSecret": self.app_secret
        }).encode("utf-8")

        req = urllib.request.Request(
            TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        token = data["access_token"]
        expires_in = int(data.get("expires_in", 7200))
        self.access_token = token
        self.expire_at = int(time.time()) + expires_in
        return token


class QQBot:
    def __init__(self):
        self.token_mgr = TokenManager(APP_ID, APP_SECRET)
        self.seq = None
        self.seen_msg_ids = set()

    def auth_header(self) -> dict:
        token = self.token_mgr.get_token()
        return {"Authorization": f"QQBot {token}"}

    def http_get_json(self, url: str) -> dict:
        req = urllib.request.Request(
            url,
            headers=self.auth_header(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def http_post_json(self, url: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                **self.auth_header(),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}

    def get_gateway_url(self) -> str:
        data = self.http_get_json(f"{API_BASE}/gateway")
        return data["url"]

    def send_ok_reply(self, user_openid: str, msg_id: str) -> None:
        # 被动回复：带 msg_id
        body = {
            "content": "好的",
            "msg_type": 0,
            "msg_id": msg_id,
            "msg_seq": 1,
        }
        self.http_post_json(f"{API_BASE}/v2/users/{user_openid}/messages", body)

    async def heartbeat_loop(self, ws, interval_ms: int):
        interval = max(interval_ms / 1000.0, 1.0)
        while not STOP:
            await asyncio.sleep(interval)
            payload = {"op": 1, "d": self.seq}
            await ws.send(json.dumps(payload))

    async def handle_dispatch(self, payload: dict):
        event_type = payload.get("t")
        data = payload.get("d", {})
        self.seq = payload.get("s", self.seq)

        if event_type == "READY":
            print(f"[READY] session established, user={data.get('user', {}).get('username')}", flush=True)
            return

        if event_type == "FRIEND_ADD":
            openid = data.get("openid")
            record = {
                "ts": now_ts(),
                "event_type": event_type,
                "openid": openid,
                "raw": data,
            }
            append_jsonl(USER_FILE, record)
            print(f"[FRIEND_ADD] openid={openid}", flush=True)
            return

        if event_type == "C2C_MSG_RECEIVE":
            openid = data.get("openid")
            record = {
                "ts": now_ts(),
                "event_type": event_type,
                "openid": openid,
                "raw": data,
            }
            append_jsonl(USER_FILE, record)
            print(f"[C2C_MSG_RECEIVE] openid={openid}", flush=True)
            return

        if event_type == "C2C_MESSAGE_CREATE":
            msg_id = data.get("id")
            author = data.get("author", {})
            user_openid = author.get("user_openid")
            content = data.get("content", "")
            timestamp = data.get("timestamp")

            # 官方文档提到极端情况下同一 msg_id 可能重复推送；这里做当前进程内去重
            if msg_id in self.seen_msg_ids:
                print(f"[DUPLICATE] msg_id={msg_id}", flush=True)
                return
            self.seen_msg_ids.add(msg_id)

            msg_record = {
                "ts": now_ts(),
                "event_type": event_type,
                "msg_id": msg_id,
                "openid": user_openid,
                "content": content,
                "timestamp": timestamp,
                "raw": data,
            }
            append_jsonl(MSG_FILE, msg_record)

            user_record = {
                "ts": now_ts(),
                "event_type": "USER_SEEN_FROM_MESSAGE",
                "openid": user_openid,
            }
            append_jsonl(USER_FILE, user_record)

            print(f"[MESSAGE] openid={user_openid} content={content!r}", flush=True)

            try:
                self.send_ok_reply(user_openid, msg_id)
                print(f"[REPLY] ok -> {user_openid}", flush=True)
            except Exception as e:
                print(f"[REPLY_ERROR] {e}", file=sys.stderr, flush=True)

    async def run_once(self):
        gateway_url = self.get_gateway_url()
        token = self.token_mgr.get_token()

        ssl_context = ssl.create_default_context()

        async with websockets.connect(
            gateway_url,
            ssl=ssl_context,
            ping_interval=None,
            max_size=8 * 1024 * 1024,
            open_timeout=20,
            close_timeout=10,
        ) as ws:
            # 1) 接收 Hello
            raw = await ws.recv()
            hello = json.loads(raw)
            if hello.get("op") != 10:
                raise RuntimeError(f"unexpected hello payload: {hello}")
            heartbeat_interval = int(hello["d"]["heartbeat_interval"])

            # 2) Identify
            identify = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": 1 << 25,   # 单聊 / 群聊消息相关事件
                    "shard": [0, 1],
                    "properties": {
                        "$os": "linux",
                        "$browser": "python-minimal-bot",
                        "$device": "python-minimal-bot",
                    },
                },
            }
            await ws.send(json.dumps(identify))

            hb_task = asyncio.create_task(self.heartbeat_loop(ws, heartbeat_interval))
            try:
                while not STOP:
                    raw = await ws.recv()
                    payload = json.loads(raw)

                    op = payload.get("op")
                    if op == 11:
                        # Heartbeat ACK
                        continue

                    if op == 0:
                        await self.handle_dispatch(payload)
            finally:
                hb_task.cancel()
                with contextlib.suppress(Exception):
                    await hb_task

    async def run_forever(self):
        backoff = 3
        while not STOP:
            try:
                await self.run_once()
                backoff = 3
            except Exception as e:
                print(f"[RECONNECT] {e}", file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def handle_stop(signum, frame):
    global STOP
    STOP = True
    print(f"[STOP] signal={signum}", flush=True)


# 避免为 hb_task.cancel 的 suppress 再额外引入一大段 try
import contextlib

def main():
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    bot = QQBot()
    asyncio.run(bot.run_forever())


if __name__ == "__main__":
    main()
