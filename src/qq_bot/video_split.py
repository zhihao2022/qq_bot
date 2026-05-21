#!/usr/bin/env python3
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


BYTES_PER_MB = 1024 * 1024
DEFAULT_SPLIT_TARGET_RATIO = 0.95
DEFAULT_MIN_SEGMENT_SECONDS = 0.25
DEFAULT_MAX_SPLIT_ATTEMPTS = 6
RETRY_SHRINK_FACTOR = 0.85
REENCODE_BITRATE_HEADROOM = 0.90
DEFAULT_REENCODE_AUDIO_KBPS = 96
MIN_REENCODE_VIDEO_KBPS = 150


@dataclass
class SplitResult:
    paths: List[Path]
    duration: float
    segment_seconds: float
    target_bytes: int
    limit_bytes: int
    attempts: int
    mode: str


def mb_to_bytes(value: float) -> int:
    return int(max(0.0, float(value)) * BYTES_PER_MB)


def bytes_to_mb(value: int) -> float:
    return value / BYTES_PER_MB


def resolve_split_target_bytes(limit_bytes: int, target_mb: Optional[float]) -> int:
    if limit_bytes <= 0:
        raise ValueError("limit_bytes必须大于0")

    if target_mb is None or float(target_mb) <= 0:
        return max(1, int(limit_bytes * DEFAULT_SPLIT_TARGET_RATIO))

    requested_bytes = mb_to_bytes(float(target_mb))
    if requested_bytes >= limit_bytes:
        return max(1, int(limit_bytes * DEFAULT_SPLIT_TARGET_RATIO))
    return max(1, requested_bytes)


def probe_video_duration(ffprobe_bin: str, file_path: Path) -> float:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe退出码={proc.returncode}: {output}")
    try:
        return float(output)
    except ValueError as exc:
        raise RuntimeError(f"ffprobe返回了无效时长: {output}") from exc


def estimate_segment_seconds(
    duration: float,
    file_size_bytes: int,
    target_bytes: int,
    max_segment_seconds: Optional[float] = None,
    min_segment_seconds: float = DEFAULT_MIN_SEGMENT_SECONDS,
) -> float:
    if duration <= 0:
        raise ValueError("duration必须大于0")
    if file_size_bytes <= 0:
        raise ValueError("file_size_bytes必须大于0")
    if target_bytes <= 0:
        raise ValueError("target_bytes必须大于0")

    avg_bytes_per_second = file_size_bytes / duration
    seconds = target_bytes / avg_bytes_per_second
    if max_segment_seconds is not None and max_segment_seconds > 0:
        seconds = min(seconds, max_segment_seconds)
    return max(min_segment_seconds, seconds)


def split_mp4_by_size(
    ffmpeg_bin: str,
    ffprobe_bin: str,
    split_work_dir: Path,
    file_path: Path,
    limit_bytes: int,
    target_bytes: int,
    max_segment_seconds: Optional[float] = None,
    min_segment_seconds: float = DEFAULT_MIN_SEGMENT_SECONDS,
    max_attempts: int = DEFAULT_MAX_SPLIT_ATTEMPTS,
    reencode_on_oversize: bool = True,
) -> SplitResult:
    duration = probe_video_duration(ffprobe_bin, file_path)
    if duration <= 0:
        raise RuntimeError("无法读取有效视频时长")

    file_size_bytes = file_path.stat().st_size
    target_bytes = max(1, min(target_bytes, limit_bytes))
    initial_segment_seconds = estimate_segment_seconds(
        duration,
        file_size_bytes,
        target_bytes,
        max_segment_seconds=max_segment_seconds,
        min_segment_seconds=min_segment_seconds,
    )
    segment_seconds = initial_segment_seconds

    split_work_dir.mkdir(parents=True, exist_ok=True)
    last_largest_bytes = 0
    copy_error = None

    for attempt in range(1, max(max_attempts, 1) + 1):
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{file_path.stem}_", dir=str(split_work_dir)))
        try:
            split_paths = split_mp4_copy(ffmpeg_bin, file_path, temp_dir, duration, segment_seconds)
            oversized = [p for p in split_paths if p.stat().st_size > limit_bytes]
            if not oversized:
                return SplitResult(
                    paths=split_paths,
                    duration=duration,
                    segment_seconds=segment_seconds,
                    target_bytes=target_bytes,
                    limit_bytes=limit_bytes,
                    attempts=attempt,
                    mode="copy",
                )

            last_largest_bytes = max(p.stat().st_size for p in oversized)
        except Exception as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if reencode_on_oversize:
                copy_error = exc
                break
            raise

        shutil.rmtree(temp_dir, ignore_errors=True)
        if segment_seconds <= min_segment_seconds + 0.001:
            break

        shrink_by_size = (target_bytes / last_largest_bytes) * 0.95 if last_largest_bytes > 0 else RETRY_SHRINK_FACTOR
        shrink = min(RETRY_SHRINK_FACTOR, max(0.10, shrink_by_size))
        next_segment_seconds = max(min_segment_seconds, segment_seconds * shrink)
        if abs(next_segment_seconds - segment_seconds) < 0.001:
            break
        segment_seconds = next_segment_seconds

    if reencode_on_oversize:
        return split_mp4_reencode_by_size(
            ffmpeg_bin,
            file_path,
            split_work_dir,
            duration,
            initial_segment_seconds,
            limit_bytes,
            target_bytes,
            min_segment_seconds=min_segment_seconds,
            max_attempts=max_attempts,
        )

    largest_mb = bytes_to_mb(last_largest_bytes) if last_largest_bytes else 0.0
    copy_error_text = f"；copy失败: {copy_error}" if copy_error else ""
    raise RuntimeError(
        f"按目标{bytes_to_mb(target_bytes):.2f}MB自动拆分后仍有片段超过"
        f"{bytes_to_mb(limit_bytes):.2f}MB限制，最大片段{largest_mb:.2f}MB；"
        f"可能需要重新编码降低码率{copy_error_text}"
    )


def split_mp4_copy(
    ffmpeg_bin: str,
    file_path: Path,
    temp_dir: Path,
    duration: float,
    segment_seconds: float,
) -> List[Path]:
    segment_seconds = max(DEFAULT_MIN_SEGMENT_SECONDS, segment_seconds)
    parts = max(1, math.ceil(duration / segment_seconds))
    split_paths: List[Path] = []

    for index in range(parts):
        start = segment_seconds * index
        length = min(segment_seconds, duration - start)
        out_path = temp_dir / f"{file_path.stem}_part{index + 1:02d}of{parts:02d}.mp4"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(file_path),
            "-t",
            f"{max(length, 0.001):.3f}",
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(out_path),
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            output = (proc.stdout or "").strip()
            raise RuntimeError(f"ffmpeg退出码={proc.returncode}: {output}")
        if not out_path.exists() or out_path.stat().st_size <= 0:
            raise RuntimeError(f"ffmpeg未生成有效分段: {out_path.name}")
        split_paths.append(out_path)

    return split_paths


def split_mp4_reencode_by_size(
    ffmpeg_bin: str,
    file_path: Path,
    split_work_dir: Path,
    duration: float,
    segment_seconds: float,
    limit_bytes: int,
    target_bytes: int,
    min_segment_seconds: float = DEFAULT_MIN_SEGMENT_SECONDS,
    max_attempts: int = DEFAULT_MAX_SPLIT_ATTEMPTS,
) -> SplitResult:
    last_largest_bytes = 0

    for attempt in range(1, max(max_attempts, 1) + 1):
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{file_path.stem}_reencode_", dir=str(split_work_dir)))
        try:
            split_paths = split_mp4_reencode(ffmpeg_bin, file_path, temp_dir, duration, segment_seconds, target_bytes)
            oversized = [p for p in split_paths if p.stat().st_size > limit_bytes]
            if not oversized:
                return SplitResult(
                    paths=split_paths,
                    duration=duration,
                    segment_seconds=segment_seconds,
                    target_bytes=target_bytes,
                    limit_bytes=limit_bytes,
                    attempts=attempt,
                    mode="reencode",
                )

            last_largest_bytes = max(p.stat().st_size for p in oversized)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        shutil.rmtree(temp_dir, ignore_errors=True)
        if segment_seconds <= min_segment_seconds + 0.001:
            break

        shrink_by_size = (target_bytes / last_largest_bytes) * 0.95 if last_largest_bytes > 0 else RETRY_SHRINK_FACTOR
        shrink = min(RETRY_SHRINK_FACTOR, max(0.10, shrink_by_size))
        next_segment_seconds = max(min_segment_seconds, segment_seconds * shrink)
        if abs(next_segment_seconds - segment_seconds) < 0.001:
            break
        segment_seconds = next_segment_seconds

    largest_mb = bytes_to_mb(last_largest_bytes) if last_largest_bytes else 0.0
    raise RuntimeError(
        f"重新编码后仍有片段超过{bytes_to_mb(limit_bytes):.2f}MB限制，"
        f"最大片段{largest_mb:.2f}MB"
    )


def split_mp4_reencode(
    ffmpeg_bin: str,
    file_path: Path,
    temp_dir: Path,
    duration: float,
    segment_seconds: float,
    target_bytes: int,
) -> List[Path]:
    segment_seconds = max(DEFAULT_MIN_SEGMENT_SECONDS, segment_seconds)
    parts = max(1, math.ceil(duration / segment_seconds))
    split_paths: List[Path] = []

    for index in range(parts):
        start = segment_seconds * index
        length = min(segment_seconds, duration - start)
        out_path = temp_dir / f"{file_path.stem}_part{index + 1:02d}of{parts:02d}.mp4"
        video_kbps, audio_kbps = estimate_reencode_bitrates(target_bytes, max(length, 0.001))
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(file_path),
            "-t",
            f"{max(length, 0.001):.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{video_kbps * 2}k",
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_kbps}k",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            output = (proc.stdout or "").strip()
            raise RuntimeError(f"ffmpeg重新编码退出码={proc.returncode}: {output}")
        if not out_path.exists() or out_path.stat().st_size <= 0:
            raise RuntimeError(f"ffmpeg未生成有效分段: {out_path.name}")
        split_paths.append(out_path)

    return split_paths


def estimate_reencode_bitrates(target_bytes: int, segment_seconds: float) -> tuple:
    total_kbps = int((target_bytes * 8 / segment_seconds) / 1000 * REENCODE_BITRATE_HEADROOM)
    audio_kbps = min(DEFAULT_REENCODE_AUDIO_KBPS, max(32, total_kbps // 5))
    video_kbps = max(MIN_REENCODE_VIDEO_KBPS, total_kbps - audio_kbps)
    return video_kbps, audio_kbps
