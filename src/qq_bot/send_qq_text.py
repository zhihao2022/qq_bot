#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import urllib.error

APP_ID = os.environ.get("QQ_APP_ID", "").strip()
APP_SECRET = os.environ.get("QQ_APP_SECRET", "").strip()

# 你的 openid，默认写死；后面也可以用环境变量覆盖
DEFAULT_OPENID = os.environ.get(
    "QQ_TARGET_OPENID",
    "E32EA2B123E0181AD53925E0B20ED304"
).strip()

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"


def get_access_token(app_id: str, app_secret: str) -> str:
    payload = json.dumps({
        "appId": app_id,
        "clientSecret": app_secret
    }).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    return data["access_token"]


def send_text(openid: str, content: str, access_token: str) -> dict:
    url = f"{API_BASE}/v2/users/{openid}/messages"
    body = {
        "content": content,
        "msg_type": 0
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"QQBot {access_token}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode("utf-8")
        return json.loads(text) if text else {}


def main():
    if not APP_ID or not APP_SECRET:
        print("错误：请先设置环境变量 QQ_APP_ID 和 QQ_APP_SECRET", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 2:
        print("用法: python src/qq_bot/send_qq_text.py 你要发送的文本", file=sys.stderr)
        sys.exit(1)

    content = " ".join(sys.argv[1:]).strip()
    if not content:
        print("错误：消息内容不能为空", file=sys.stderr)
        sys.exit(1)

    openid = DEFAULT_OPENID
    try:
        token = get_access_token(APP_ID, APP_SECRET)
        result = send_text(openid, content, token)
        print("发送成功")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTPError: {e.code}", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"发送失败: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
