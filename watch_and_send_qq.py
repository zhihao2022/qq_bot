#!/usr/bin/env python3
import argparse
import fnmatch
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    WATCHDOG_IMPORT_ERROR: Optional[Exception] = None
except ModuleNotFoundError as exc:
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_IMPORT_ERROR = exc


STOP = False

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


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
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


def normalize_task(raw_task: Dict[str, Any], default_send_text: bool, default_text_template: str) -> Dict[str, Any]:
    task = dict(raw_task)
    task["name"] = str(task["name"])
    task["local_dir"] = expand_path(task["local_dir"])
    task["recursive"] = bool(task.get("recursive", True))
    task["send_text"] = bool(task.get("send_text", default_send_text))
    task["text_template"] = str(task.get("text_template", default_text_template))
    task["include_suffixes"] = [str(x).lower() for x in task.get("include_suffixes", [".mp4"])]
    exclude_globs = list(DEFAULT_EXCLUDE_GLOBS)
    exclude_globs.extend(task.get("exclude_globs", []))
    task["exclude_globs"] = exclude_globs
    task["hash_small_files_only_mb"] = int(task.get("hash_small_files_only_mb", 16))
    return task


class QQVideoWatcher:
    def __init__(self, config_path: str, state_path: str):
        self.config_path = Path(expand_path(config_path))
        self.state_path = Path(expand_path(state_path))
        self.script_dir = Path(__file__).resolve().parent

        raw_config = load_json(self.config_path, {})
        if "tasks" not in raw_config or not raw_config["tasks"]:
            raise ValueError("config.json 里必须至少有一个 task")

        self.config = dict(raw_config)
        self.config["settle_seconds"] = int(self.config.get("settle_seconds", 8))
        self.config["send_text"] = bool(self.config.get("send_text", False))
        self.config["text_template"] = str(
            self.config.get("text_template", "视频已发送：{filename}")
        )
        self.config["python_bin"] = normalize_command_path(
            str(self.config.get("python_bin", sys.executable)),
            sys.executable,
        )
        self.config["log_file"] = expand_path(
            str(self.config.get("log_file", self.script_dir / "watch_and_send_qq.log"))
        )
        self.config["tasks"] = [
            normalize_task(task, self.config["send_text"], self.config["text_template"])
            for task in self.config["tasks"]
        ]

        self.send_video_script = self.script_dir / "send_qq_video.py"
        self.send_text_script = self.script_dir / "send_qq_text.py"
        self.tasks_by_name = {task["name"]: task for task in self.config["tasks"]}

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

    def save_state(self) -> None:
        save_json_atomic(self.state_path, self.state)

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
        for task in self.config["tasks"]:
            rel_path = self.match_task_for_path(task, path)
            if rel_path is not None:
                matched.append((task, rel_path))
        return matched

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
            try:
                sig = self.file_sig(task, path)
            except FileNotFoundError:
                continue

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

            logging.info("[%s] 检测到变化，已安排发送检查: %s", task["name"], rel_path)

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
        task = self.tasks_by_name[task_name]
        path = Path(item["path"])
        rel_path = item["rel_path"]

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

        ok = self.send_video(task, path)
        if ok and task.get("send_text", False):
            self.send_text_notice(task, path, rel_path)

        if ok:
            self.mark_sent(task_name, rel_path, current_sig)
        self.mark_checked(task_name)
        self.clear_pending(key)

    def send_video(self, task: Dict[str, Any], path: Path) -> bool:
        cmd = [self.config["python_bin"], str(self.send_video_script), str(path)]
        logging.info("[%s] 开始发送视频: %s", task["name"], path)
        logging.info("执行命令: %s", " ".join(cmd))

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
            logging.info("视频发送输出:\n%s", output.rstrip())

        if proc.returncode != 0:
            logging.error("[%s] 视频发送失败，退出码=%s", task["name"], proc.returncode)
            return False

        logging.info("[%s] 视频发送成功: %s", task["name"], path.name)
        return True

    def send_text_notice(self, task: Dict[str, Any], path: Path, rel_path: str) -> bool:
        content = task["text_template"].format(
            task_name=task["name"],
            filename=path.name,
            rel_path=rel_path,
            abs_path=str(path.resolve()),
        )
        cmd = [self.config["python_bin"], str(self.send_text_script), content]
        logging.info("[%s] 开始发送文字通知: %s", task["name"], content)

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
            logging.info("文字通知输出:\n%s", output.rstrip())

        if proc.returncode != 0:
            logging.error("[%s] 文字通知发送失败，退出码=%s", task["name"], proc.returncode)
            return False

        logging.info("[%s] 文字通知发送成功", task["name"])
        return True

    def run_daemon(self) -> None:
        if Observer is None:
            raise RuntimeError(
                "运行监听模式需要先安装 watchdog，例如: pip install watchdog"
            ) from WATCHDOG_IMPORT_ERROR

        observer = Observer()
        handler = ChangeHandler(self)

        for task in self.config["tasks"]:
            watch_dir = Path(task["local_dir"])
            if not watch_dir.exists():
                raise SystemExit(f"监听目录不存在: {watch_dir}")
            observer.schedule(handler, str(watch_dir), recursive=task.get("recursive", True))
            logging.info(
                "开始监听: %s -> task=%s, send_text=%s",
                watch_dir,
                task["name"],
                task.get("send_text", False),
            )

        observer.start()
        logging.info("文件稳定等待秒数: %s", self.config["settle_seconds"])
        logging.info("状态文件: %s", self.state_path)

        try:
            while not STOP:
                time.sleep(1)
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
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="监听配置里的目录，并通过 QQ 发送匹配文件")
    parser.add_argument("--config", default=str(base_dir / "config.json"))
    parser.add_argument("--state", default=str(base_dir / "state.json"))
    args = parser.parse_args()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    app = QQVideoWatcher(args.config, args.state)
    app.run_daemon()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
