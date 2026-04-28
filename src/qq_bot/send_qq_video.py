#!/usr/bin/env python3
import base64
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict

APP_ID = os.environ.get("QQ_APP_ID", "").strip()
APP_SECRET = os.environ.get("QQ_APP_SECRET", "").strip()
TARGET_OPENID = os.environ.get(
    "QQ_TARGET_OPENID",
    "E32EA2B123E0181AD53925E0B20ED304"
).strip()

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

MEDIA_FILE_TYPES: Dict[str, int] = {
    ".jpg": 1,
    ".jpeg": 1,
    ".png": 1,
    ".mp4": 2,
    ".silk": 3,
}
DEFAULT_FILE_TYPE = 4


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


def qq_file_type(file_path: Path) -> int:
    return MEDIA_FILE_TYPES.get(file_path.suffix.lower(), DEFAULT_FILE_TYPE)


def upload_file_with_file_data(
    openid: str,
    file_path: Path,
    access_token: str,
    display_file_name: str = "",
) -> dict:
    raw = file_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    file_type = qq_file_type(file_path)

    # QQ Bot v2 文件上传接口使用 file_type 区分媒体：1 图片，2 视频，
    # 3 语音，4 普通文件。普通文件能力由平台侧开放状态决定。
    body = {
        "file_type": file_type,
        "srv_send_msg": False,
        "file_data": b64,
    }
    if file_type == DEFAULT_FILE_TYPE:
        body["file_name"] = display_file_name or file_path.name

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

    args = sys.argv[1:]
    display_file_name = ""
    cleaned_args = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--file-name":
            if index + 1 >= len(args):
                print("错误：--file-name 需要一个文件名", file=sys.stderr)
                sys.exit(1)
            display_file_name = args[index + 1]
            index += 2
            continue
        cleaned_args.append(arg)
        index += 1

    if len(cleaned_args) < 1:
        print("用法: python src/qq_bot/send_qq_video.py [--file-name 显示文件名] /path/to/file [可选说明文字]", file=sys.stderr)
        sys.exit(1)

    file_path = Path(cleaned_args[0]).expanduser().resolve()
    if not file_path.exists():
        print(f"错误：文件不存在: {file_path}", file=sys.stderr)
        sys.exit(2)

    content = " ".join(cleaned_args[1:]).strip()

    try:
        token = get_access_token(APP_ID, APP_SECRET)

        upload_result = upload_file_with_file_data(
            TARGET_OPENID,
            file_path,
            token,
            display_file_name=display_file_name,
        )
        file_info = upload_result.get("file_info")
        if not file_info:
            print("上传结果里没有 file_info，无法继续发送", file=sys.stderr)
            print(json.dumps(upload_result, ensure_ascii=False, indent=2), file=sys.stderr)
            sys.exit(4)

        send_result = send_media_message(TARGET_OPENID, file_info, token, content=content)

        print("文件发送成功")
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
