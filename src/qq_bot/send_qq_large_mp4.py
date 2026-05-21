#!/usr/bin/env python3
import argparse
import json
import shutil
import sys
import urllib.error
from pathlib import Path
from typing import Any, Dict, List

from send_qq_text import send_text
from send_qq_video import (
    APP_ID,
    APP_SECRET,
    TARGET_OPENID,
    get_access_token,
    send_media_message,
    upload_file_with_file_data,
)
from video_split import (
    SplitResult,
    bytes_to_mb,
    resolve_split_target_bytes,
    split_mp4_by_size,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parents[1]
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "config.json"


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return data


def expand_config_path(config_path: Path, raw: str, default: Path) -> Path:
    value = str(raw or default).strip()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def send_one_mp4(
    file_path: Path,
    token: str,
    content: str = "",
    display_name: str = "",
) -> dict:
    upload_result = upload_file_with_file_data(
        TARGET_OPENID,
        file_path,
        token,
        display_file_name=display_name,
    )
    file_info = upload_result.get("file_info")
    if not file_info:
        raise RuntimeError(
            "上传结果里没有 file_info，无法继续发送: "
            + json.dumps(upload_result, ensure_ascii=False)
        )
    send_result = send_media_message(TARGET_OPENID, file_info, token, content=content)
    return {
        "upload_result": upload_result,
        "send_result": send_result,
    }


def send_failure_notice(token: str, template: str, file_path: Path, reason: str, size_mb: float, limit_mb: float) -> None:
    content = template.format(
        filename=file_path.name,
        abs_path=str(file_path.resolve()),
        reason=reason,
        size_mb=size_mb,
        limit_mb=limit_mb,
    )
    send_text(TARGET_OPENID, content, token)


def split_mp4(
    ffmpeg_bin: str,
    ffprobe_bin: str,
    split_work_dir: Path,
    file_path: Path,
    max_segment_seconds: float,
    limit_bytes: int,
    target_bytes: int,
) -> SplitResult:
    return split_mp4_by_size(
        ffmpeg_bin,
        ffprobe_bin,
        split_work_dir,
        file_path,
        limit_bytes,
        target_bytes,
        max_segment_seconds=max_segment_seconds,
    )


def cleanup_split_files(split_paths: List[Path]) -> None:
    if split_paths:
        shutil.rmtree(split_paths[0].parent, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="单独发送指定 MP4，超过大小限制时自动拆成多段发送")
    parser.add_argument("file", help="要发送的 .mp4 文件")
    parser.add_argument("content", nargs="*", help="可选说明文字，只随第一段发送")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--max-mb", type=float, default=None, help="覆盖最大单文件大小，默认读 config/config.json")
    parser.add_argument(
        "--segment-seconds",
        type=float,
        default=None,
        help="覆盖每段视频秒数上限，实际会按目标大小自动缩短",
    )
    parser.add_argument(
        "--target-mb",
        type=float,
        default=None,
        help="覆盖拆分目标大小，默认读 config/config.json，例如 9.5",
    )
    parser.add_argument("--no-split", action="store_true", help="超过大小限制时不拆分，直接发送失败文字")
    return parser.parse_args()


def main() -> int:
    if not APP_ID or not APP_SECRET:
        print("错误：请先设置环境变量 QQ_APP_ID 和 QQ_APP_SECRET", file=sys.stderr)
        return 1

    args = parse_args()
    file_path = Path(args.file).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        print(f"错误：文件不存在或不是普通文件: {file_path}", file=sys.stderr)
        return 2
    if file_path.suffix.lower() != ".mp4":
        print(f"错误：该脚本只处理 .mp4 文件: {file_path}", file=sys.stderr)
        return 3

    config_path = Path(args.config).expanduser().resolve()
    config = load_json(config_path, {})
    max_mb = args.max_mb if args.max_mb is not None else float(config.get("max_send_file_mb", 10))
    max_segment_seconds = max(
        0.25,
        args.segment_seconds
        if args.segment_seconds is not None
        else float(config.get("split_large_mp4_segment_seconds", 20)),
    )
    target_mb = args.target_mb if args.target_mb is not None else float(config.get("split_large_mp4_target_mb", max_mb * 0.95))
    split_enabled = not args.no_split and bool(config.get("split_large_mp4", True))
    failure_template = str(config.get("large_file_failure_template", "由于{reason}，{filename}文件发送失败"))
    ffmpeg_bin = str(config.get("ffmpeg_bin", "ffmpeg"))
    ffprobe_bin = str(config.get("ffprobe_bin", "ffprobe"))
    split_work_dir = expand_config_path(
        config_path,
        str(config.get("split_work_dir", PROJECT_DIR / "var" / "split_files")),
        PROJECT_DIR / "var" / "split_files",
    )

    content = " ".join(args.content).strip()
    size_bytes = file_path.stat().st_size
    size_mb = size_bytes / 1024 / 1024
    limit_bytes = int(max_mb * 1024 * 1024)
    target_bytes = resolve_split_target_bytes(limit_bytes, target_mb) if limit_bytes > 0 else 0

    split_paths: List[Path] = []
    try:
        token = get_access_token(APP_ID, APP_SECRET)

        if max_mb <= 0 or size_bytes <= limit_bytes:
            result = send_one_mp4(file_path, token, content=content)
            print("文件发送成功")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if not split_enabled:
            reason = f"文件大小{size_mb:.2f}MB超过{max_mb:.2f}MB限制"
            send_failure_notice(token, failure_template, file_path, reason, size_mb, max_mb)
            print(reason, file=sys.stderr)
            return 4

        print(
            f"文件大小{size_mb:.2f}MB超过{max_mb:.2f}MB，"
            f"开始按目标{bytes_to_mb(target_bytes):.2f}MB自动计算分段时长"
        )
        try:
            split_result = split_mp4(
                ffmpeg_bin,
                ffprobe_bin,
                split_work_dir,
                file_path,
                max_segment_seconds,
                limit_bytes,
                target_bytes,
            )
            split_paths = split_result.paths
            print(
                f"已按{split_result.segment_seconds:.2f}秒/段切为{len(split_paths)}段"
                f"（模式{split_result.mode}，尝试{split_result.attempts}次）"
            )
        except Exception as exc:
            reason = f"文件大小{size_mb:.2f}MB超过{max_mb:.2f}MB限制，且ffmpeg拆分失败: {exc}"
            send_failure_notice(token, failure_template, file_path, reason, size_mb, max_mb)
            print(reason, file=sys.stderr)
            return 5

        oversized = [p for p in split_paths if p.stat().st_size > limit_bytes]
        if oversized:
            largest_mb = max(p.stat().st_size for p in oversized) / 1024 / 1024
            reason = (
                f"文件大小{size_mb:.2f}MB超过{max_mb:.2f}MB限制，"
                f"按目标{bytes_to_mb(target_bytes):.2f}MB拆分后仍有片段超过限制，最大片段{largest_mb:.2f}MB"
            )
            send_failure_notice(token, failure_template, file_path, reason, size_mb, max_mb)
            print(reason, file=sys.stderr)
            return 6

        results = []
        for index, split_path in enumerate(split_paths, start=1):
            part_name = f"{file_path.stem}_part{index:02d}of{len(split_paths):02d}.mp4"
            part_content = content if index == 1 else ""
            print(f"发送第{index}/{len(split_paths)}段: {part_name}")
            try:
                results.append(send_one_mp4(split_path, token, content=part_content, display_name=part_name))
            except Exception as exc:
                reason = f"文件拆分为{len(split_paths)}段后，第{index}段发送失败: {exc}"
                send_failure_notice(token, failure_template, file_path, reason, size_mb, max_mb)
                print(reason, file=sys.stderr)
                return 7

        print("文件已拆分并全部发送成功")
        print(json.dumps({"parts": len(results), "results": results}, ensure_ascii=False, indent=2))
        return 0

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTPError: {exc.code}", file=sys.stderr)
        print(body, file=sys.stderr)
        return 10
    except Exception as exc:
        print(f"发送失败: {exc}", file=sys.stderr)
        return 11
    finally:
        cleanup_split_files(split_paths)


if __name__ == "__main__":
    raise SystemExit(main())
