# 部署与配置

## 对现有云端的影响

这是独立的源码仓库。Git 提交不会改变现有云端数据、QQ 登录、模型配置或服务状态。不要直接用公开示例覆盖正在运行的 `astrbot-data`，也不要让两套机器人同时登录同一个 QQ 小号。

## 新环境

1. 准备 Linux、Docker 和 Compose，参考 `deploy/compose.yml`。其中目录都相对于该 Compose 文件所在位置，默认仅在服务器回环地址开放管理端口。
2. 将 `plugins/astrbot_plugin_jinli_*` 目录放入新环境的 `astrbot-data/plugins/`。将需要的 `config/examples/*_config.json` 复制到 `astrbot-data/config/` 后，在本地填写真实账号信息。示例的 `10000` 为机器人账号，`10001` 为主人，不能直接照用。
3. 新部署在上述准备完成后才执行 `docker compose -f deploy/compose.yml up -d`。这条命令是部署操作，本次上传代码没有执行它。
4. 通过 SSH 隧道访问 AstrBot（6185）、SnowLuma（5099）、QQ 登录桌面（6081）。不要把这些管理端口直接公开到互联网。
5. 在 SnowLuma 登录准备好的机器人 QQ 小号。创建 OneBot 反向 WebSocket 客户端，地址为 `ws://astrbot:6199/ws`。
6. 在 AstrBot 创建匹配的 aiocqhttp 连接，ID 为 `qq-snowluma`，监听 `0.0.0.0:6199`，路径 `/ws`。两端设置一致且自行生成的访问令牌；令牌不能提交到 Git。
7. 在 AstrBot 界面配置 DeepSeek API Key。聊天模型示例 ID 为 `deepseek-dragon/deepseek-flash`，需要 `text`、`image`、`tool_use` 能力；记忆模型 ID 为 `deepseek-dragon/jinli-memory`。这些是本项目使用的配置 ID，需要与实际创建的模型对应。
8. 新建人格 ID `jinli` 并填写自己的角色设定。每日空间动态插件会读取该人格。真实人格包含私人关系设定，因此没有上传。
9. 插件配置中 `platform_id`、`account_id`、`owner_id` 和 `provider_id` 应与新环境对应，再按需要开启 `enabled`。部分插件的界面默认平台 ID 仍为本机版本 `jinli-personal`，云端应改为 `qq-snowluma`；示例文件已使用云端 ID。

本仓库不包含一键填入所有 AstrBot 主配置的工具，避免把真实模型密钥、权限和历史数据一起打包。主配置需要在界面完成。

## 保持对话体验

群消息缓存、人格、工具调用和主模型配置由 AstrBot 提供。原环境启用群聊上下文和发言人标识，最近消息缓存为 40 条；长期记忆由插件另行存储。仅安装唤醒插件不会自动创建这些主配置。

原环境将消息速率限制的 `count` 设置为 `0`：此前 `10 次/60 秒 + stall` 导致排队几十秒。迁移时不要误把旧的排队设置覆盖回去。联网搜索需要在 AstrBot 中启用相应工具；工具调用能力不能只写在提示词里。

## 本地管理面板（可选）

`tools/bot-manager` 是 Windows 本地工具，不是云端机器人常驻依赖。

复制 `config.example.json` 为同目录 `config.json`，填写自己的服务器地址、SSH 密钥路径和需要转发的端口，然后用 Python 运行 `manager.py`。首次 SSH 主机身份确认应事先手动完成。

关闭该面板只关闭本机隧道和本地界面；云端容器由 Docker 管理。自动恢复功能默认关闭，未提供任何阿里云凭证。新开自动恢复前应了解其重启权限和频率控制。

