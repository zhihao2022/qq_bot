#!/usr/bin/env python3
import argparse
import hashlib
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


MANIFEST_TYPE = "qq_bot_video_split_manifest"
MANIFEST_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CandidateFile:
    path: Path
    size: int
    mtime: float


@dataclass
class MatchResult:
    matched: Dict[int, Path]
    missing: List[Dict[str, Any]]
    errors: List[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据 qq_bot split manifest 自动匹配并合并 QQ 接收的视频分片")
    parser.add_argument("--dir", default="", help="包含 QQ 接收视频和 manifest 的目录；不传则交互输入")
    parser.add_argument("--output-dir", default="", help="输出目录，默认 <dir>/merged")
    parser.add_argument("--recursive", dest="recursive", action="store_true", help="递归搜索（默认）")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="只搜索目录第一层")
    parser.add_argument("--time-tolerance", type=float, default=86400, help="mtime 粗筛容忍秒数，默认 86400")
    parser.add_argument("--size-tolerance", type=int, default=0, help="文件大小容忍字节数，默认 0")
    parser.add_argument("--dry-run", action="store_true", help="只显示匹配结果，不调用 ffmpeg 合并")
    parser.add_argument("--keep-list-file", action="store_true", help="合并成功后保留 ffmpeg concat list 文件")
    parser.add_argument("--overwrite", action="store_true", help="输出文件已存在时覆盖")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 可执行文件路径，默认 ffmpeg")
    parser.set_defaults(recursive=True)
    return parser.parse_args()


def get_root_dir(args: argparse.Namespace) -> Optional[Path]:
    raw_dir = args.dir.strip()
    if not raw_dir:
        raw_dir = input("请输入包含 QQ 接收视频和 manifest 的目录：").strip()
    if not raw_dir:
        print("ERROR: 未提供扫描目录")
        return None

    root_dir = Path(raw_dir).expanduser().resolve()
    if not root_dir.exists() or not root_dir.is_dir():
        print(f"ERROR: 目录不存在或不是目录: {root_dir}")
        return None
    return root_dir


def scan_files(root_dir: Path, recursive: bool) -> List[Path]:
    files: List[Path] = []
    iterator = root_dir.rglob("*") if recursive else root_dir.iterdir()
    for path in iterator:
        try:
            if path.is_file():
                files.append(path)
        except OSError as exc:
            print(f"WARN: 无法读取文件，已跳过: {path} ({exc})")
    return files


def find_manifests(all_files: List[Path]) -> List[Tuple[Path, Dict[str, Any]]]:
    manifests: List[Tuple[Path, Dict[str, Any]]] = []
    for path in all_files:
        if path.suffix.lower() != ".json":
            continue
        try:
            if path.stat().st_size > MANIFEST_MAX_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"WARN: JSON 文件无法读取或解析，已跳过: {path} ({exc})")
            continue

        if isinstance(data, dict) and data.get("type") == MANIFEST_TYPE:
            manifests.append((path, data))
        else:
            print(f"WARN: 文件看起来像 JSON，但不是 qq_bot_video_split_manifest，已跳过: {path}")
    return manifests


def find_video_candidates(all_files: List[Path]) -> List[CandidateFile]:
    candidates: List[CandidateFile] = []
    for path in all_files:
        if path.suffix.lower() != ".mp4":
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            print(f"WARN: 无法读取视频候选，已跳过: {path} ({exc})")
            continue
        candidates.append(CandidateFile(path=path, size=stat.st_size, mtime=stat.st_mtime))
    return candidates


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def get_sha256_cached(candidate: CandidateFile, cache: Dict[Tuple[str, int, float], str]) -> str:
    key = (str(candidate.path.resolve()), candidate.size, candidate.mtime)
    if key not in cache:
        cache[key] = sha256_file(candidate.path)
    return cache[key]


def part_label(part: Dict[str, Any]) -> str:
    index = int(part.get("index", 0))
    total = int(part.get("total", part.get("split_total", 0)) or 0)
    width = max(2, len(str(total)))
    if index > 0 and total > 0:
        return f"part{index:0{width}d}of{total:0{width}d}"
    return f"part{index}"


def manifest_parts(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("manifest.parts 缺失或为空")

    normalized: List[Dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("manifest.parts 中存在非对象条目")
        for field in ("index", "total", "name", "size", "sha256", "mtime"):
            if field not in part:
                raise ValueError(f"manifest part 缺少字段: {field}")
        normalized.append(part)
    return sorted(normalized, key=lambda item: int(item["index"]))


def match_parts(
    manifest: Dict[str, Any],
    video_candidates: List[CandidateFile],
    size_tolerance: int,
    time_tolerance: float,
) -> MatchResult:
    matched: Dict[int, Path] = {}
    missing: List[Dict[str, Any]] = []
    errors: List[str] = []
    hash_cache: Dict[Tuple[str, int, float], str] = {}
    selected_paths: Dict[str, int] = {}

    for part in manifest_parts(manifest):
        index = int(part["index"])
        expected_size = int(part["size"])
        expected_mtime = float(part["mtime"])
        expected_sha256 = str(part["sha256"]).lower()
        label = part_label(part)

        size_candidates = [
            candidate
            for candidate in video_candidates
            if abs(candidate.size - expected_size) <= size_tolerance
        ]
        time_candidates = [
            candidate
            for candidate in size_candidates
            if abs(candidate.mtime - expected_mtime) <= time_tolerance
        ]
        hash_candidates = time_candidates if time_candidates else size_candidates
        used_fallback = not time_candidates and bool(size_candidates)

        sha_matches: List[CandidateFile] = []
        for candidate in hash_candidates:
            try:
                if get_sha256_cached(candidate, hash_cache) == expected_sha256:
                    sha_matches.append(candidate)
            except OSError as exc:
                print(f"WARN: 计算 SHA256 失败，已跳过: {candidate.path} ({exc})")

        fallback_text = "，time 无候选已退化为仅 size" if used_fallback else ""
        if sha_matches:
            print(
                f"[match] {label}: size 候选 {len(size_candidates)} 个，"
                f"time 候选 {len(time_candidates)} 个{fallback_text}，sha256 匹配成功"
            )
        else:
            print(
                f"[match] {label}: size 候选 {len(size_candidates)} 个，"
                f"time 候选 {len(time_candidates)} 个{fallback_text}，sha256 未匹配"
            )
            missing.append(part)
            continue

        if len(sha_matches) > 1:
            sha_matches.sort(key=lambda item: item.mtime, reverse=True)
            print(f"WARN: {label} 匹配到多个相同文件，已选择最近修改的一个: {sha_matches[0].path}")

        chosen = sha_matches[0]
        resolved = str(chosen.path.resolve())
        if resolved in selected_paths:
            other_index = selected_paths[resolved]
            errors.append(
                f"多个分片匹配到同一个本地文件: part{other_index} 和 part{index} -> {chosen.path}"
            )
            continue

        matched[index] = chosen.path
        selected_paths[resolved] = index

    return MatchResult(matched=matched, missing=missing, errors=errors)


def output_name_for_manifest(manifest: Dict[str, Any]) -> str:
    merge = manifest.get("merge") if isinstance(manifest.get("merge"), dict) else {}
    output_name = str(merge.get("output_name") or "").strip()
    if output_name:
        return Path(output_name).name

    original = manifest.get("original") if isinstance(manifest.get("original"), dict) else {}
    stem = str(original.get("stem") or Path(str(original.get("name") or "merged")).stem)
    return f"{stem}_merged.mp4"


def unique_output_path(output_dir: Path, output_name: str, overwrite: bool) -> Path:
    base_path = output_dir / output_name
    if overwrite or not base_path.exists():
        return base_path

    stem = base_path.stem
    suffix = base_path.suffix or ".mp4"
    for number in range(1, 1000):
        candidate = output_dir / f"{stem}_{number:03d}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不重名输出文件: {base_path}")


def ffmpeg_concat_escape(path: Path) -> str:
    value = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{value}'"


def check_ffmpeg(ffmpeg_bin: str) -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        print("ERROR: 未找到 ffmpeg。请先安装 ffmpeg，并确保 ffmpeg 在 PATH 中。")
        return False
    except Exception as exc:
        print(f"ERROR: 检查 ffmpeg 失败: {exc}")
        return False

    if proc.returncode != 0:
        print(f"ERROR: ffmpeg 不可用，退出码={proc.returncode}")
        return False
    return True


def merge_parts(
    manifest: Dict[str, Any],
    matched_parts: Dict[int, Path],
    output_dir: Path,
    overwrite: bool,
    keep_list_file: bool,
    ffmpeg_bin: str,
) -> Optional[Path]:
    if not check_ffmpeg(ffmpeg_bin):
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = unique_output_path(output_dir, output_name_for_manifest(manifest), overwrite)
    concat_list_path = output_path.with_suffix(output_path.suffix + ".concat.txt")
    ordered_paths = [matched_parts[int(part["index"])] for part in manifest_parts(manifest)]

    with concat_list_path.open("w", encoding="utf-8") as f:
        for path in ordered_paths:
            f.write(ffmpeg_concat_escape(path) + "\n")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list_path),
        "-c",
        "copy",
        str(output_path),
    ]
    print(f"[merge] 输出: {output_path}")
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if proc.returncode != 0:
        stderr_lines = (proc.stderr or proc.stdout or "").splitlines()
        tail = "\n".join(stderr_lines[-20:])
        print(f"ERROR: ffmpeg 合并失败，return code={proc.returncode}")
        print(f"ERROR: ffmpeg 命令: {shlex.join(cmd)}")
        print(f"ERROR: concat list: {concat_list_path}")
        if tail:
            print("[ffmpeg stderr tail]")
            print(tail)
        return None

    if not output_path.exists() or output_path.stat().st_size <= 0:
        print(f"ERROR: ffmpeg 返回成功，但输出文件无效: {output_path}")
        return None

    original = manifest.get("original") if isinstance(manifest.get("original"), dict) else {}
    original_sha256 = str(original.get("sha256") or "").strip().lower()
    if original_sha256:
        merged_sha256 = sha256_file(output_path)
        if merged_sha256 == original_sha256:
            print("[verify] 原视频 SHA256 校验通过")
        else:
            print("[verify] WARN: 合并文件 SHA256 与 manifest.original.sha256 不一致")

    if not keep_list_file:
        try:
            concat_list_path.unlink()
        except OSError as exc:
            print(f"WARN: 删除 concat list 失败: {concat_list_path} ({exc})")

    print("[done] 合并完成")
    return output_path


def process_manifest(
    manifest_path: Path,
    manifest: Dict[str, Any],
    video_candidates: List[CandidateFile],
    output_dir: Path,
    args: argparse.Namespace,
) -> bool:
    try:
        parts = manifest_parts(manifest)
    except ValueError as exc:
        print(f"ERROR: manifest 字段不完整，已跳过: {manifest_path} ({exc})")
        return False

    original = manifest.get("original") if isinstance(manifest.get("original"), dict) else {}
    original_name = str(original.get("name") or manifest.get("video_id") or manifest_path.name)
    print(f"[manifest] 找到 manifest: {manifest_path}")
    print(f"[task] 原视频: {original_name}")
    print(f"[task] 分片总数: {len(parts)}")

    result = match_parts(
        manifest=manifest,
        video_candidates=video_candidates,
        size_tolerance=max(0, int(args.size_tolerance)),
        time_tolerance=max(0.0, float(args.time_tolerance)),
    )

    if result.errors:
        for error in result.errors:
            print(f"ERROR: {error}")
        return False

    if result.missing:
        print(f"[wait] 尚未找齐分片: {original_name}")
        for part in result.missing:
            print(
                f"[missing] {part_label(part)}, expected_size={part.get('size')}, "
                f"expected_sha256={part.get('sha256')}"
            )
        print("[hint] 请确认该分片已经从 QQ 下载/另存到当前目录或其子目录")
        return False

    print(f"[merge] 已找齐全部 {len(result.matched)} 个分片")
    if args.dry_run:
        output_path = unique_output_path(output_dir, output_name_for_manifest(manifest), args.overwrite)
        print(f"[dry-run] 将输出: {output_path}")
        return True

    return merge_parts(
        manifest=manifest,
        matched_parts=result.matched,
        output_dir=output_dir,
        overwrite=args.overwrite,
        keep_list_file=args.keep_list_file,
        ffmpeg_bin=args.ffmpeg,
    ) is not None


def main() -> int:
    args = parse_args()
    root_dir = get_root_dir(args)
    if root_dir is None:
        return 2

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else root_dir / "merged"

    print(f"[scan] 扫描目录: {root_dir}")
    all_files = scan_files(root_dir, recursive=args.recursive)
    print(f"[scan] {'递归' if args.recursive else '非递归'}搜索文件数: {len(all_files)}")

    manifests = find_manifests(all_files)
    if not manifests:
        print("没有找到可用的 split manifest")
        return 0

    video_candidates = find_video_candidates(all_files)
    print(f"[scan] 视频候选 .mp4 文件数: {len(video_candidates)}")

    success_count = 0
    for manifest_path, manifest in manifests:
        print("")
        try:
            if process_manifest(manifest_path, manifest, video_candidates, output_dir, args):
                success_count += 1
        except Exception as exc:
            print(f"ERROR: 处理 manifest 失败，已继续下一个: {manifest_path} ({exc})")

    print("")
    print(f"[summary] manifest={len(manifests)} success={success_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
