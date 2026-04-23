# AGENTS.md

## 项目概览
这是一个 QQ Bot 自动通知与文件接收项目。源码在 `src/qq_bot/`，配置在 `config/`，运行状态在 `var/`，日志在 `logs/`，接收文件在 `data/received_files/`。

## 运行方式
- 先设置环境变量 `QQ_APP_ID`、`QQ_APP_SECRET`，可选 `QQ_TARGET_OPENID`。
- 安装依赖：`pip install -r requirements.txt`
- 监听发送：`python src/qq_bot/watch_and_send_qq.py`
- 接收保存：`python src/qq_bot/receive_qq_files.py`
- 单发视频：`python src/qq_bot/send_qq_video.py /path/to/video.mp4 [说明文字]`
- 单发文字：`python src/qq_bot/send_qq_text.py "消息内容"`

## 维护注意
- `config/config.json` 放稳定配置；`config/tasks.json` 放任务列表，运行中可动态增删任务，监听进程会按 `tasks_reload_seconds` 热更新。
- `receive_qq_files.py` 会保存可下载文件；没有可下载 URL 时保存原始 `message.json`，然后回复保存路径。
- `var/`、`logs/`、`data/` 是运行产物目录，默认不要提交；新增用法请同步更新 `README.md`。
