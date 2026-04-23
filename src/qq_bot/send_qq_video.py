#!/usr/bin/env python3
import base64
import json
import mimetypes
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

APP_ID = os.environ.get("QQ_APP_ID", "").strip()
APP_SECRET = os.environ.get("QQ_APP_SECRET", "").strip()
TARGET_OPENID = os.environ.get(
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


def upload_video_with_file_data(openid: str, file_path: Path, access_token: str) -> dict:
    raw = file_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")

    # 文档写了 file_type=2 表示视频，格式要求 mp4
    # 这里优先尝试 file_data 直传
    body = {
        "file_type": 2,
        "srv_send_msg": False,
        "file_data": b64
    }

    # 保守起见，若服务端要求 url 字段存在，可尝试把它置空；
    # 如果你的环境因此报参数错误，就需要改成可公网访问的 url 方案。
    body["url"] = ""

    url = f"{API_BASE}/v2/users/{openid}/files"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"QQBot {access_token}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")
        return json.loads(text) if text else {}


def send_media_message(openid: str, file_info: str, access_token: str, content: str = "") -> dict:
    body = {
        "msg_type": 7,
        "media": {
            "file_info": file_info
        }
    }
    if content:
        body["content"] = content

    url = f"{API_BASE}/v2/users/{openid}/messages"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"QQBot {access_token}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
        return json.loads(text) if text else {}


def main():
    if not APP_ID or not APP_SECRET:
        print("错误：请先设置环境变量 QQ_APP_ID 和 QQ_APP_SECRET", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) < 2:
        print("用法: python src/qq_bot/send_qq_video.py /path/to/video.mp4 [可选说明文字]", file=sys.stderr)
        sys.exit(1)

    file_path = Path(sys.argv[1]).expanduser().resolve()
    if not file_path.exists():
        print(f"错误：文件不存在: {file_path}", file=sys.stderr)
        sys.exit(2)

    if file_path.suffix.lower() != ".mp4":
        print("错误：当前脚本只发送 .mp4 视频", file=sys.stderr)
        sys.exit(3)

    content = " ".join(sys.argv[2:]).strip()

    try:
        token = get_access_token(APP_ID, APP_SECRET)

        upload_result = upload_video_with_file_data(TARGET_OPENID, file_path, token)
        file_info = upload_result.get("file_info")
        if not file_info:
            print("上传结果里没有 file_info，无法继续发送", file=sys.stderr)
            print(json.dumps(upload_result, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(4)

        send_result = send_media_message(TARGET_OPENID, file_info, token, content=content)

        print("视频发送成功")
        print(json.dumps({
            "upload_result": upload_result,
            "send_result": send_result
        }, ensure_ascii=False, indent=2))

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTPError: {e.code}", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(10)
    except Exception as e:
        print(f"发送失败: {e}", file=sys.stderr)
        sys.exit(11)


if __name__ == "__main__":
    main()
