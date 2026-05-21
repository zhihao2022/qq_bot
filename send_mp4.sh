#!/usr/bin/env bash
set -euo pipefail

# 改这里：要发送的 MP4 文件路径。
VIDEO_PATH="/home/zhihao/Code/save_fire/DEXER0423/src_mzh/runtime/videos/src_mzh_env2_20260520_161134.mp4"

# 改这里：可选说明文字；不需要就留空。
MESSAGE=""

# 通常不需要改。默认使用 config/config.json 里的 10MB、9.5MB目标自动拆分和失败通知配置。
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="/home/zhihao/miniconda3/envs/auto_notify/bin/python"
CONFIG_PATH="${PROJECT_DIR}/config/config.json"

cd "${PROJECT_DIR}"
exec "${PYTHON_BIN}" "${PROJECT_DIR}/src/qq_bot/send_qq_large_mp4.py" \
  --config "${CONFIG_PATH}" \
  "${VIDEO_PATH}" \
  "${MESSAGE}"
