#!/usr/bin/env python3
import argparse
import fnmatch
import hashlib
import json
import logging
import os
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from video_split import (
    SplitResult,
    bytes_to_mb,
    make_split_manifest_path,
    resolve_split_target_bytes,
    split_mp4_by_size,
    write_split_manifest,
)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    WATCHDOG_IMPORT_ERROR: Optional[Exception] = None
except ModuleNotFoundError as exc:
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_IMPORT_ERROR = exc


STOP = False
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parents[1]

DEFAULT_EXCLUDE_GLOBS = [
    "*.tmp", "*.part", "*.swp", "*.swx", "*.crdownload",
    ".DS_Store", "__pycache__", ".ipynb_checkpoints"
]


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def expand_path(path_str: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(path_str))).resolve())


def normalize_command_path(path_str: str, default: str) -> str:
    raw = str(path_str or default).strip()
    if not raw:
        return default
    if "/" in raw or raw.startswith(".") or raw.startswith("~"):
        return expand_path(raw)
    return raw


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


def expand_path_from(base_dir: Path, path_str: str, default: str) -> str:
    raw = str(path_str or default).strip()
    if not raw:
        raw = default
    expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return str(expanded.resolve())


def file_version(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_mtime_ns, st.st_size


def matches_any_glob(rel_path: str, globs: List[str]) -> bool:
    name = Path(rel_path).name
    return any(fnmatch.fnmatch(rel_path, g) or fnmatch.fnmatch(name, g) for g in globs)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def normalize_task(
    raw_task: Dict[str, Any],
    default_send_text: bool,
    default_text_template: str,
    default_max_send_file_mb: float,
    default_split_large_mp4: bool,
    default_split_large_mp4_segment_seconds: float,
    default_split_large_mp4_target_mb: float,
    default_large_file_failure_template: str,
) -> Dict[str, Any]:
    task = dict(raw_task)
    task["name"] = str(task["name"])
    task["local_dir"] = expand_path(task["local_dir"])
    task["recursive"] = bool(task.get("recursive", True))
    task["send_text"] = bool(task.get("send_text", default_send_text))
    task["text_template"] = str(task.get("text_template", default_text_template))
    task["include_suffixes"] = [str(x).lower() for x in task.get("include_suffixes", [".mp4"])]
    task["max_send_file_mb"] = float(task.get("max_send_file_mb", default_max_send_file_mb))
    task["split_large_mp4"] = bool(task.get("split_large_mp4", default_split_large_mp4))
    task["split_large_mp4_segment_seconds"] = max(
        0.25,
        float(task.get("split_large_mp4_segment_seconds", default_split_large_mp4_segment_seconds)),
    )
    task["split_large_mp4_target_mb"] = float(
        task.get("split_large_mp4_target_mb", default_split_large_mp4_target_mb)
    )
    task["large_file_failure_template"] = str(
        task.get("large_file_failure_template", default_large_file_failure_template)
    )
    task["send_file_retries"] = int(task.get("send_file_retries", 3))
    task["send_file_retry_delay_seconds"] = float(task.get("send_file_retry_delay_seconds", 5))
    exclude_globs = list(DEFAULT_EXCLUDE_GLOBS)
    exclude_globs.extend(task.get("exclude_globs", []))
    task["exclude_globs"] = exclude_globs
    task["hash_small_files_only_mb"] = int(task.get("hash_small_files_only_mb", 16))
    return task


def normalize_tasks(
    raw_tasks: Any,
    default_send_text: bool,
    default_text_template: str,
    default_max_send_file_mb: float,
    default_split_large_mp4: bool,
    default_split_large_mp4_segment_seconds: float,
    default_split_large_mp4_target_mb: float,
    default_large_file_failure_template: str,
) -> List[Dict[str, Any]]:
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("tasks 必须是非空列表")

    tasks = []
    seen_names = set()
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            raise ValueError("每个 task 必须是 JSON 对象")
        if not raw_task.get("name") or not raw_task.get("local_dir"):
            raise ValueError("每个 task 都必须包含 name 和 local_dir")

        task = normalize_task(
            raw_task,
            default_send_text,
            default_text_template,
            default_max_send_file_mb,
            default_split_large_mp4,
            default_split_large_mp4_segment_seconds,
            default_split_large_mp4_target_mb,
            default_large_file_failure_template,
        )
        if task["name"] in seen_names:
            raise ValueError(f"task name 重复: {task['name']}")
        seen_names.add(task["name"])
        tasks.append(task)

    return tasks


class QQVideoWatcher:
    def __init__(self, config_path: str, state_path: str):
        self.config_path = Path(expand_path(config_path))
        self.state_path = Path(expand_path(state_path))
        self.script_dir = SCRIPT_DIR

        raw_config = load_json(self.config_path, {})
        if not isinstance(raw_config, dict):
            raise ValueError("config.json 必须是 JSON 对象")

        self.config = dict(raw_config)
        self.config["settle_seconds"] = int(self.config.get("settle_seconds", 8))
        self.config["tasks_reload_seconds"] = float(self.config.get("tasks_reload_seconds", 2))
        self.config["send_text"] = bool(self.config.get("send_text", False))
        self.config["text_template"] = str(
            self.config.get("text_template", "视频已发送：{filename}")
        )
        self.config["python_bin"] = normalize_command_path(
            str(self.config.get("python_bin", sys.executable)),
            sys.executable,
        )
        self.config["ffmpeg_bin"] = normalize_command_path(
            str(self.config.get("ffmpeg_bin", "ffmpeg")),
            "ffmpeg",
        )
        self.config["ffprobe_bin"] = normalize_command_path(
            str(self.config.get("ffprobe_bin", "ffprobe")),
            "ffprobe",
        )
        self.config["max_send_file_mb"] = float(self.config.get("max_send_file_mb", 10))
        self.config["split_large_mp4"] = bool(self.config.get("split_large_mp4", True))
        self.config["split_large_mp4_segment_seconds"] = max(
            0.25,
            float(self.config.get("split_large_mp4_segment_seconds", 20)),
        )
        self.config["split_large_mp4_target_mb"] = float(
            self.config.get(
                "split_large_mp4_target_mb",
                self.config["max_send_file_mb"] * 0.95,
            )
        )
        self.config["large_file_failure_template"] = str(
            self.config.get(
                "large_file_failure_template",
                "由于{reason}，{filename}文件发送失败",
            )
        )
        self.config["split_work_dir"] = expand_path_from(
            self.config_path.parent,
            str(self.config.get("split_work_dir", PROJECT_DIR / "var" / "split_files")),
            str(PROJECT_DIR / "var" / "split_files"),
        )
        self.config["log_file"] = expand_path_from(
            self.config_path.parent,
            str(self.config.get("log_file", PROJECT_DIR / "logs" / "watch_and_send_qq.log")),
            str(PROJECT_DIR / "logs" / "watch_and_send_qq.log"),
        )

        tasks_file = self.config.get("tasks_file")
        self.tasks_source_is_config = not bool(tasks_file)
        self.tasks_source_path = (
            Path(expand_path_from(self.config_path.parent, str(tasks_file), "tasks.json"))
            if tasks_file
            else self.config_path
        )

        self.send_file_script = self.script_dir / "send_qq_video.py"
        self.send_text_script = self.script_dir / "send_qq_text.py"

        self.state = load_json(self.state_path, {"tasks": {}})
        self.state.setdefault("tasks", {})

        log_file = Path(self.config["log_file"])
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            print(f"warning: 无法写入日志文件 {log_file}: {exc}", file=sys.stderr)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
            handlers=handlers,
        )

        self.lock = threading.Lock()
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.timers: Dict[str, threading.Timer] = {}
        self.tasks_by_name: Dict[str, Dict[str, Any]] = {}
        self.config["tasks"] = []
        self.watches: Dict[Tuple[str, bool], Any] = {}
        self.missing_watch_keys = set()
        self.tasks_source_version = file_version(self.tasks_source_path)
        self.tasks_source_error_version: Any = None
        self.initial_scan_task_names = self.set_tasks(self.load_tasks(), "初始化")

    def save_state(self) -> None:
        save_json_atomic(self.state_path, self.state)

    def load_tasks(self) -> List[Dict[str, Any]]:
        if self.tasks_source_is_config:
            raw_config = load_json(self.config_path, {})
            if not isinstance(raw_config, dict):
                raise ValueError("config.json 必须是 JSON 对象")
            raw_tasks = raw_config.get("tasks", [])
        else:
            raw_doc = load_json(self.tasks_source_path, {})
            raw_tasks = raw_doc.get("tasks", []) if isinstance(raw_doc, dict) else raw_doc

        return normalize_tasks(
            raw_tasks,
            self.config["send_text"],
            self.config["text_template"],
            self.config["max_send_file_mb"],
            self.config["split_large_mp4"],
            self.config["split_large_mp4_segment_seconds"],
            self.config["split_large_mp4_target_mb"],
            self.config["large_file_failure_template"],
        )

    def set_tasks(self, tasks: List[Dict[str, Any]], reason: str) -> List[str]:
        new_by_name = {task["name"]: task for task in tasks}

        with self.lock:
            old_by_name = self.tasks_by_name
            removed_names = {
                name
                for name in old_by_name
                if name not in new_by_name
            }
            changed_or_added = {
                name
                for name, new_task in new_by_name.items()
                if name not in old_by_name or old_by_name[name] != new_task
            }
            self.config["tasks"] = tasks
            self.tasks_by_name = new_by_name
            self.cancel_pending_for_tasks_locked(removed_names | changed_or_added)

        logging.info(
            "任务列表已%s: %s",
            reason,
            ", ".join(new_by_name) if new_by_name else "(empty)",
        )
        return sorted(changed_or_added)

    def cancel_pending_for_tasks_locked(self, task_names) -> None:
        if not task_names:
            return
        for key, item in list(self.pending.items()):
            if item.get("task_name") not in task_names:
                continue
            self.pending.pop(key, None)
            timer = self.timers.pop(key, None)
            if timer:
                timer.cancel()

    def tasks_snapshot(self) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.config["tasks"])

    def reload_tasks_if_changed(self, observer: Any, handler: "ChangeHandler") -> None:
        version = file_version(self.tasks_source_path)
        if version == self.tasks_source_version:
            return

        try:
            tasks = self.load_tasks()
        except Exception as exc:
            error_version = version if version is not None else ("missing", str(self.tasks_source_path))
            if error_version != self.tasks_source_error_version:
                logging.error("任务配置热加载失败，继续使用旧任务: %s", exc)
                self.tasks_source_error_version = error_version
            return

        self.tasks_source_version = version
        self.tasks_source_error_version = None
        changed_or_added = self.set_tasks(tasks, "热更新")
        self.sync_watches(observer, handler)
        self.scan_existing_files(changed_or_added, "任务热更新后初始检查")

    def sync_watches(self, observer: Any, handler: "ChangeHandler") -> None:
        tasks = self.tasks_snapshot()
        desired = {
            (str(Path(task["local_dir"]).resolve()), bool(task.get("recursive", True)))
            for task in tasks
        }

        for key, watch in list(self.watches.items()):
            if key in desired:
                continue
            observer.unschedule(watch)
            self.watches.pop(key, None)
            self.missing_watch_keys.discard(key)
            logging.info("停止监听: %s recursive=%s", key[0], key[1])

        for key in sorted(desired):
            if key in self.watches:
                continue
            watch_dir = Path(key[0])
            if not watch_dir.exists():
                if key not in self.missing_watch_keys:
                    logging.warning("监听目录不存在，暂不监听: %s", watch_dir)
                    self.missing_watch_keys.add(key)
                continue

            watch = observer.schedule(handler, str(watch_dir), recursive=key[1])
            self.watches[key] = watch
            self.missing_watch_keys.discard(key)
            task_names = [
                task["name"]
                for task in tasks
                if str(Path(task["local_dir"]).resolve()) == key[0]
                and bool(task.get("recursive", True)) == key[1]
            ]
            logging.info("开始监听: %s -> tasks=%s", watch_dir, ",".join(task_names))

    def file_sig(self, task: Dict[str, Any], path: Path) -> Dict[str, Any]:
        st = path.stat()
        meta = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        }
        hash_limit_bytes = int(task["hash_small_files_only_mb"]) * 1024 * 1024
        if hash_limit_bytes > 0 and st.st_size <= hash_limit_bytes:
            meta["sha256"] = sha256_file(path)
        return meta

    def task_state(self, task_name: str) -> Dict[str, Any]:
        task_state = self.state["tasks"].setdefault(task_name, {})
        task_state.setdefault("sent", {})
        return task_state

    def already_sent(self, task_name: str, rel_path: str, sig: Dict[str, Any]) -> bool:
        sent = self.task_state(task_name).setdefault("sent", {})
        return sent.get(rel_path, {}).get("meta") == sig

    def mark_sent(self, task_name: str, rel_path: str, sig: Dict[str, Any]) -> None:
        task_state = self.task_state(task_name)
        task_state["sent"][rel_path] = {
            "meta": sig,
            "sent_at": now_str(),
        }
        task_state["last_send_time"] = now_str()
        self.save_state()

    def mark_checked(self, task_name: str) -> None:
        task_state = self.task_state(task_name)
        task_state["last_check_time"] = now_str()
        self.save_state()

    def match_task_for_path(self, task: Dict[str, Any], path: Path) -> Optional[str]:
        try:
            rel_path = path.resolve().relative_to(Path(task["local_dir"]).resolve()).as_posix()
        except ValueError:
            return None

        if not task.get("recursive", True) and "/" in rel_path:
            return None
        if matches_any_glob(rel_path, task.get("exclude_globs", [])):
            return None
        include_suffixes = task.get("include_suffixes", [])
        if include_suffixes and path.suffix.lower() not in include_suffixes:
            return None
        return rel_path

    def matching_items(self, path: Path) -> List[Tuple[Dict[str, Any], str]]:
        matched: List[Tuple[Dict[str, Any], str]] = []
        for task in self.tasks_snapshot():
            rel_path = self.match_task_for_path(task, path)
            if rel_path is not None:
                matched.append((task, rel_path))
        return matched

    def iter_task_files(self, task: Dict[str, Any]) -> Iterator[Path]:
        local_dir = Path(task["local_dir"])
        if not local_dir.exists():
            logging.warning("[%s] 初始检查跳过，目录不存在: %s", task["name"], local_dir)
            return
        if not local_dir.is_dir():
            logging.warning("[%s] 初始检查跳过，不是目录: %s", task["name"], local_dir)
            return

        try:
            iterator = local_dir.rglob("*") if task.get("recursive", True) else local_dir.iterdir()
            for path in iterator:
                if STOP:
                    return
                try:
                    if path.is_file():
                        yield path
                except OSError as exc:
                    logging.warning("[%s] 初始检查无法读取文件: %s (%s)", task["name"], path, exc)
        except OSError as exc:
            logging.warning("[%s] 初始检查无法扫描目录: %s (%s)", task["name"], local_dir, exc)

    def schedule_for_task(self, task: Dict[str, Any], path: Path, rel_path: str, source: str) -> bool:
        try:
            sig = self.file_sig(task, path)
        except FileNotFoundError:
            return False

        key = f"{task['name']}::{rel_path}"
        with self.lock:
            self.pending[key] = {
                "task_name": task["name"],
                "path": str(path),
                "rel_path": rel_path,
                "last_seen_sig": sig,
                "last_event_time": time.time(),
            }

            old_timer = self.timers.get(key)
            if old_timer:
                old_timer.cancel()

            timer = threading.Timer(self.config["settle_seconds"], self.try_send, args=(key,))
            timer.daemon = True
            self.timers[key] = timer
            timer.start()

        logging.info("[%s] %s，已安排发送检查: %s", task["name"], source, rel_path)
        return True

    def scan_existing_files(self, task_names: List[str], reason: str) -> None:
        if not task_names:
            return

        with self.lock:
            tasks = [
                self.tasks_by_name[name]
                for name in task_names
                if name in self.tasks_by_name
            ]

        for task in tasks:
            if STOP:
                return
            scheduled_count = 0
            checked_count = 0
            logging.info("[%s] 开始%s: %s", task["name"], reason, task["local_dir"])
            for path in self.iter_task_files(task):
                rel_path = self.match_task_for_path(task, path)
                if rel_path is None:
                    continue
                checked_count += 1
                if self.schedule_for_task(task, path.resolve(), rel_path, reason):
                    scheduled_count += 1
            self.mark_checked(task["name"])
            logging.info(
                "[%s] %s完成，匹配文件=%s，安排检查=%s",
                task["name"],
                reason,
                checked_count,
                scheduled_count,
            )

    def schedule(self, path_str: str) -> None:
        if not path_str:
            return

        try:
            path = Path(path_str).resolve()
        except Exception:
            return

        if not path.exists() or path.is_dir():
            return

        for task, rel_path in self.matching_items(path):
            self.schedule_for_task(task, path, rel_path, "检测到变化")

    def clear_pending(self, key: str) -> None:
        with self.lock:
            self.pending.pop(key, None)
            timer = self.timers.pop(key, None)
            if timer:
                timer.cancel()

    def try_send(self, key: str) -> None:
        if STOP:
            return

        with self.lock:
            item = self.pending.get(key)
            if not item:
                return

        task_name = item["task_name"]
        path = Path(item["path"])
        rel_path = item["rel_path"]
        with self.lock:
            task = self.tasks_by_name.get(task_name)
        if not task:
            logging.info("[%s] 任务已移除，跳过待发送文件: %s", task_name, rel_path)
            self.mark_checked(task_name)
            self.clear_pending(key)
            return

        if not path.exists() or path.is_dir():
            logging.info("[%s] 文件不存在或不是普通文件，跳过: %s", task_name, rel_path)
            self.mark_checked(task_name)
            self.clear_pending(key)
            return

        try:
            current_sig = self.file_sig(task, path)
        except FileNotFoundError:
            self.mark_checked(task_name)
            self.clear_pending(key)
            return

        with self.lock:
            pending_item = self.pending.get(key)
            if not pending_item:
                return
            old_sig = pending_item["last_seen_sig"]

        if current_sig != old_sig:
            logging.info("[%s] 文件仍在写入，延后发送: %s", task_name, rel_path)
            with self.lock:
                if key not in self.pending:
                    return
                self.pending[key]["last_seen_sig"] = current_sig
                timer = threading.Timer(self.config["settle_seconds"], self.try_send, args=(key,))
                timer.daemon = True
                self.timers[key] = timer
                timer.start()
            return

        if self.already_sent(task_name, rel_path, current_sig):
            logging.info("[%s] 该版本文件已发送过，跳过: %s", task_name, rel_path)
            self.mark_checked(task_name)
            self.clear_pending(key)
            return

        ok = self.send_file(task, path)
        if ok and task.get("send_text", False):
            self.send_text_notice(task, path, rel_path)

        if ok:
            self.mark_sent(task_name, rel_path, current_sig)
        self.mark_checked(task_name)
        self.clear_pending(key)

    def send_file(self, task: Dict[str, Any], path: Path) -> bool:
        max_mb = float(task.get("max_send_file_mb", 0))
        if max_mb <= 0:
            return self.send_file_direct(task, path)

        try:
            size_bytes = path.stat().st_size
        except FileNotFoundError:
            return False

        limit_bytes = int(max_mb * 1024 * 1024)
        if size_bytes <= limit_bytes:
            return self.send_file_direct(task, path)

        size_mb = size_bytes / 1024 / 1024
        if path.suffix.lower() == ".mp4" and task.get("split_large_mp4", True):
            return self.send_large_mp4(task, path, size_bytes, limit_bytes, max_mb)

        reason = f"文件大小{size_mb:.2f}MB超过{max_mb:.2f}MB限制"
        logging.warning("[%s] %s，跳过发送: %s", task["name"], reason, path)
        self.send_large_file_failure_notice(task, path, reason, size_bytes=size_bytes, limit_mb=max_mb)
        return False

    def send_large_mp4(
        self,
        task: Dict[str, Any],
        path: Path,
        size_bytes: int,
        limit_bytes: int,
        limit_mb: float,
    ) -> bool:
        max_segment_seconds = max(0.25, float(task.get("split_large_mp4_segment_seconds", 20)))
        target_mb = float(task.get("split_large_mp4_target_mb", limit_mb * 0.95))
        target_bytes = resolve_split_target_bytes(limit_bytes, target_mb)
        size_mb = size_bytes / 1024 / 1024
        logging.info(
            "[%s] MP4超过大小限制，准备按目标大小优先用最少段数拆分发送: %s size=%.2fMB limit=%.2fMB target=%.2fMB",
            task["name"],
            path,
            size_mb,
            limit_mb,
            bytes_to_mb(target_bytes),
        )

        split_paths: List[Path] = []
        manifest_path: Optional[Path] = None
        try:
            try:
                split_result = self.split_mp4(path, max_segment_seconds, limit_bytes, target_bytes)
                split_paths = split_result.paths
                logging.info(
                    "[%s] MP4自动拆分完成: parts=%s segment_seconds=%.2f mode=%s attempts=%s",
                    task["name"],
                    len(split_paths),
                    split_result.segment_seconds,
                    split_result.mode,
                    split_result.attempts,
                )
            except Exception as exc:
                reason = f"文件大小{size_mb:.2f}MB超过{limit_mb:.2f}MB限制，且ffmpeg拆分失败: {exc}"
                logging.error("[%s] %s", task["name"], reason)
                self.send_large_file_failure_notice(task, path, reason, size_bytes=size_bytes, limit_mb=limit_mb)
                return False

            oversized = [p for p in split_paths if p.stat().st_size > limit_bytes]
            if oversized:
                largest_mb = max(p.stat().st_size for p in oversized) / 1024 / 1024
                reason = (
                    f"文件大小{size_mb:.2f}MB超过{limit_mb:.2f}MB限制，"
                    f"按目标{bytes_to_mb(target_bytes):.2f}MB拆分后仍有片段超过限制，最大片段{largest_mb:.2f}MB"
                )
                logging.warning("[%s] %s", task["name"], reason)
                self.send_large_file_failure_notice(task, path, reason, size_bytes=size_bytes, limit_mb=limit_mb)
                return False

            try:
                manifest_path = make_split_manifest_path(path, split_paths)
                manifest = write_split_manifest(
                    original_video=path,
                    part_paths=split_paths,
                    split_result=split_result,
                    manifest_path=manifest_path,
                    source={
                        "host": socket.gethostname(),
                        "watch_name": task["name"],
                    },
                    output_name=f"{path.stem}_merged.mp4",
                )
                logging.info(
                    "[%s] split manifest 已生成: %s parts=%s",
                    task["name"],
                    manifest_path,
                    len(manifest.get("parts", [])),
                )
            except Exception as exc:
                reason = f"文件已拆分，但生成 split manifest 失败: {exc}"
                logging.error("[%s] %s", task["name"], reason)
                self.send_large_file_failure_notice(task, path, reason, size_bytes=size_bytes, limit_mb=limit_mb)
                return False

            for index, split_path in enumerate(split_paths, start=1):
                display_name = f"{path.stem}_part{index:02d}of{len(split_paths):02d}{path.suffix.lower()}"
                if not self.send_file_direct(task, split_path, display_name=display_name):
                    reason = f"文件拆分为{len(split_paths)}段后，第{index}段发送失败"
                    self.send_large_file_failure_notice(task, path, reason, size_bytes=size_bytes, limit_mb=limit_mb)
                    return False

            if not manifest_path or not self.send_file_direct(task, manifest_path, display_name=manifest_path.name):
                reason = "文件分片已发送，但 split manifest 发送失败"
                logging.error("[%s] %s: %s", task["name"], reason, path.name)
                self.send_large_file_failure_notice(task, path, reason, size_bytes=size_bytes, limit_mb=limit_mb)
                return False

            logging.info("[%s] 大MP4已拆分，所有分片和 manifest 均发送成功: %s", task["name"], path.name)
            return True
        finally:
            self.cleanup_split_files(split_paths)

    def split_mp4(
        self,
        path: Path,
        max_segment_seconds: float,
        limit_bytes: int,
        target_bytes: int,
    ) -> SplitResult:
        return split_mp4_by_size(
            self.config["ffmpeg_bin"],
            self.config["ffprobe_bin"],
            Path(self.config["split_work_dir"]),
            path,
            limit_bytes,
            target_bytes,
            max_segment_seconds=max_segment_seconds,
        )

    def cleanup_split_files(self, split_paths: List[Path]) -> None:
        if not split_paths:
            return
        split_dir = split_paths[0].parent
        try:
            shutil.rmtree(split_dir, ignore_errors=True)
        except OSError as exc:
            logging.warning("清理视频分段临时目录失败: %s (%s)", split_dir, exc)

    def send_file_direct(self, task: Dict[str, Any], path: Path, display_name: str = "") -> bool:
        cmd = [self.config["python_bin"], str(self.send_file_script), str(path)]
        if display_name:
            cmd[2:2] = ["--file-name", display_name]
        logging.info("[%s] 开始发送文件: %s", task["name"], path)
        logging.info("执行命令: %s", " ".join(cmd))

        attempts = max(int(task.get("send_file_retries", 3)), 1)
        retry_delay = max(float(task.get("send_file_retry_delay_seconds", 5)), 0)
        for attempt in range(1, attempts + 1):
            proc = subprocess.run(
                cmd,
                cwd=str(self.script_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )

            output = proc.stdout or ""
            if output.strip():
                logging.info("文件发送输出:\n%s", output.rstrip())

            if proc.returncode == 0:
                logging.info("[%s] 文件发送成功: %s", task["name"], path.name)
                return True

            if attempt < attempts:
                logging.warning(
                    "[%s] 文件发送失败，退出码=%s，%s 秒后重试 %s/%s: %s",
                    task["name"],
                    proc.returncode,
                    retry_delay,
                    attempt + 1,
                    attempts,
                    path.name,
                )
                time.sleep(retry_delay)

        logging.error("[%s] 文件发送失败，已重试 %s 次: %s", task["name"], attempts, path.name)
        return False

    def send_large_file_failure_notice(
        self,
        task: Dict[str, Any],
        path: Path,
        reason: str,
        size_bytes: int,
        limit_mb: float,
    ) -> bool:
        template = task.get("large_file_failure_template") or "由于{reason}，{filename}文件发送失败"
        content = template.format(
            task_name=task["name"],
            filename=path.name,
            abs_path=str(path.resolve()),
            reason=reason,
            size_mb=size_bytes / 1024 / 1024,
            limit_mb=limit_mb,
        )
        return self.send_text_content(task, content, "大文件失败通知")

    def send_text_notice(self, task: Dict[str, Any], path: Path, rel_path: str) -> bool:
        content = task["text_template"].format(
            task_name=task["name"],
            filename=path.name,
            rel_path=rel_path,
            abs_path=str(path.resolve()),
        )
        return self.send_text_content(task, content, "文字通知")

    def send_text_content(self, task: Dict[str, Any], content: str, label: str) -> bool:
        cmd = [self.config["python_bin"], str(self.send_text_script), content]
        logging.info("[%s] 开始发送%s: %s", task["name"], label, content)

        proc = subprocess.run(
            cmd,
            cwd=str(self.script_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

        output = proc.stdout or ""
        if output.strip():
            logging.info("%s输出:\n%s", label, output.rstrip())

        if proc.returncode != 0:
            logging.error("[%s] %s发送失败，退出码=%s", task["name"], label, proc.returncode)
            return False

        logging.info("[%s] %s发送成功", task["name"], label)
        return True

    def run_daemon(self) -> None:
        if Observer is None:
            raise RuntimeError(
                "运行监听模式需要先安装 watchdog，例如: pip install watchdog"
            ) from WATCHDOG_IMPORT_ERROR

        observer = Observer()
        handler = ChangeHandler(self)

        self.sync_watches(observer, handler)

        observer.start()
        logging.info("文件稳定等待秒数: %s", self.config["settle_seconds"])
        logging.info("状态文件: %s", self.state_path)
        logging.info("任务配置来源: %s", self.tasks_source_path)
        self.scan_existing_files(self.initial_scan_task_names, "启动后初始检查")

        try:
            while not STOP:
                self.reload_tasks_if_changed(observer, handler)
                self.sync_watches(observer, handler)
                time.sleep(self.config["tasks_reload_seconds"])
        finally:
            observer.stop()
            observer.join()
            logging.info("监听器已退出")


class ChangeHandler(FileSystemEventHandler):
    def __init__(self, app: QQVideoWatcher):
        super().__init__()
        self.app = app

    def _handle(self, event) -> None:
        if getattr(event, "is_directory", False):
            return

        src = getattr(event, "src_path", None)
        if src:
            self.app.schedule(src)

        dest = getattr(event, "dest_path", None)
        if dest:
            self.app.schedule(dest)

    def on_created(self, event):
        self._handle(event)

    def on_modified(self, event):
        self._handle(event)

    def on_moved(self, event):
        self._handle(event)

    def on_closed(self, event):
        self._handle(event)


def handle_stop(signum, frame) -> None:
    del frame
    global STOP
    STOP = True
    logging.info("收到停止信号: %s", signum)


def main() -> int:
    parser = argparse.ArgumentParser(description="监听配置里的目录，并通过 QQ 发送匹配文件")
    parser.add_argument("--config", default=str(PROJECT_DIR / "config" / "config.json"))
    parser.add_argument("--state", default=str(PROJECT_DIR / "var" / "state.json"))
    args = parser.parse_args()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    app = QQVideoWatcher(args.config, args.state)
    app.run_daemon()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
