# QQ Bot 自动通知与文件接收

这个项目提供两类能力：

- 监听本地目录中的 `.mp4` 文件，待文件写入稳定后自动通过 QQ Bot 发给目标 openid。
- 监听 QQ 单聊/群聊消息，把消息中的文件或媒体保存到本地，并回复保存路径。

## 目录结构

```text
.
├── config/                  # 稳定配置与可热更新任务列表
│   ├── config.json
│   └── tasks.json
├── src/qq_bot/              # Python 源码
│   ├── watch_and_send_qq.py # 监听目录并发送视频
│   ├── receive_qq_files.py  # 接收 QQ 消息并保存文件
│   ├── send_qq_video.py     # 单发视频
│   ├── send_qq_text.py      # 单发文字
│   └── qq_bot_min.py        # 最小 WebSocket Bot 示例
├── data/                    # 接收文件与消息记录，默认不提交
├── logs/                    # 运行日志，默认不提交
├── var/                     # 状态文件，默认不提交
├── AGENTS.md
├── README.md
└── requirements.txt
```

## 安装

建议在项目根目录执行：

```bash
pip install -r requirements.txt
```

运行前需要配置 QQ Bot 凭据：

```bash
export QQ_APP_ID="你的 app id"
export QQ_APP_SECRET="你的 app secret"
export QQ_TARGET_OPENID="目标 openid"
```

`QQ_TARGET_OPENID` 主要用于主动发送视频和文字；接收脚本会根据收到的消息自动回复。

## 监听目录并发送视频

```bash
python src/qq_bot/watch_and_send_qq.py
```

默认读取：

- 稳定配置：`config/config.json`
- 任务配置：`config/tasks.json`
- 运行状态：`var/state.json`
- 日志文件：`logs/watch_and_send_qq.log`

启动后会先对当前任务做一次初始检查，扫描已有的匹配文件；如果该文件版本还没有记录在 `var/state.json` 中，就会按 `settle_seconds` 等待稳定后自动发送。

也可以显式指定：

```bash
python src/qq_bot/watch_and_send_qq.py \
  --config config/config.json \
  --state var/state.json
```

## 动态修改监听任务

监听任务放在 `config/tasks.json`。运行中可以直接增删或修改 `tasks` 列表，监听进程会按 `config/config.json` 中的 `tasks_reload_seconds` 自动热更新。新增或修改任务后，也会立刻对这些任务执行一次初始检查；因此先生成 `.mp4`、再把目录加入 `tasks.json`，也能自动补发未发送过的文件。

一个任务示例：

```json
{
  "name": "my-videos",
  "local_dir": "~/Videos",
  "recursive": true,
  "send_text": false,
  "include_suffixes": [".mp4"],
  "exclude_globs": ["*.tmp", "*.part"],
  "hash_small_files_only_mb": 16
}
```

如果 `tasks.json` 临时写坏，进程会继续使用上一份有效任务，并在日志里记录错误。

## 接收消息并保存文件

```bash
python src/qq_bot/receive_qq_files.py
```

默认保存到：

- 文件目录：`data/received_files/`
- 消息索引：`logs/received_messages.jsonl`

收到可下载文件时会保存文件；如果消息里没有可下载 URL，也会保存原始消息为 `message.json`。保存后会回复：

```text
收到，消息保存在{filepath}
```

常用参数：

```bash
python src/qq_bot/receive_qq_files.py \
  --save-dir data/received_files \
  --log-file logs/received_messages.jsonl \
  --max-mb 200
```

## 单次发送

```bash
python src/qq_bot/send_qq_video.py /path/to/video.mp4 "可选说明文字"
python src/qq_bot/send_qq_text.py "消息内容"
```

## 注意事项

- `config/config.json` 放稳定配置，`config/tasks.json` 放可热更新任务。
- `var/`、`logs/`、`data/` 是运行产物目录，已在 `.gitignore` 中忽略。
- QQ 平台不同消息类型的文件字段可能有差异；接收脚本会保留完整原始消息，排查时优先看对应目录下的 `message.json`。

## logs

### tmux名称

- auto_notify
- receive_qq_files
