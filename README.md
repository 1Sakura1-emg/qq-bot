# 龙娘 QQ Bot（烬璃）

基于 AstrBot、DeepSeek 和 SnowLuma 的龙娘 QQ 机器人定制源码。插件来自云端运行版本的只读导出；公开版本已替换真实账号、移除私人数据，配置示例默认关闭。

**这个仓库用于源码管理，不与现有服务器自动同步。提交代码不会部署、重启或停止云端服务。** 仓库没有自动部署工作流，也不包含连接现有服务器的密钥。

## 内容

| 目录 | 用途 |
| --- | --- |
| `plugins/` | 9 个定制 AstrBot 插件 |
| `config/examples/` | 脱敏后的插件配置示例 |
| `deploy/` | 云端 Compose 和 SnowLuma/noVNC 启动修复文件 |
| `tools/bot-manager/` | Windows 本地管理面板及 SSH 自动重连源码 |
| `tests/` | 模拟接口的离线插件回归测试 |
| `docs/` | 安装、配置、数据边界和已知限制 |

## 已实现功能

- `jinli_clean`：整理回复空行。
- `jinli_friends`：核对小号身份后自动通过好友申请。
- `jinli_media`：QQ 语音转文字桥接、图片和表情包提示处理。语音识别有已知故障，详见下文。
- `jinli_memory`：长期记忆。各群共享公开记忆，私聊按真实 QQ 号隔离；支持 `/记住`、`/记忆`、`/忘记`。
- `jinli_poke`：别人戳小号时触发回复，包含冷却限制。
- `jinli_search`：搜索工具接入与链接核验，过滤没有搜索依据的模型链接。
- `jinli_wake`：群内叫「龙娘」唤醒；之后 60 秒按「群 + 发言人」继续对话，其他人不能借用窗口。
- `jinli_qzone`：每天北京时间 19:45 生成并发布一条纯文字动态，当天不盲目重试。
- `jinli_qzone_social`：回复自己动态的新评论；评论「空友爱看」推荐动态，每天最多 5 条、至少间隔 2 小时。发送结果不确定时记录后不重发。

## 运行版本与限制

源码快照日期：2026-11-10（北京时间）。运行环境：AstrBot **v4.29.3**、SnowLuma **v2.2.1**、Linux Docker；聊天模型和长期记忆模型在 AstrBot 中分别配置。

语音目前依赖 QQ 原生转文字接口，该接口曾返回错误码 9 或超时。未启用的本地离线识别实验没有纳入本仓库，也没有将其描述为可用功能。图片理解需要聊天模型具备图像能力，插件本身不是视觉模型。

QQ 空间功能依赖当前 SnowLuma 接口与 QQ 登录状态，平台变更可能使其失效。「自己动态下的新评论回复」仍需要手机端端到端实测；公共推荐评论曾取得实际接口成功结果。

## 使用

请先阅读 [部署与配置](docs/setup.md) 和 [数据与维护](docs/maintenance.md)。复用源码到新环境需要自行配置模型密钥、QQ 登录、账号身份和人格；这些真实配置不在公开仓库中。

### 离线测试

在已安装 AstrBot v4.29.3 依赖的 Python 环境中，将该版本 AstrBot 源码路径加入 `PYTHONPATH`，然后运行：

```powershell
$env:PYTHONPATH = 'C:\path\to\AstrBot'
python -m unittest discover -s tests -p 'test_*.py'
python tests/test_keyword_wake.py
python tests/test_followup_window.py
python -m unittest discover -s tools/bot-manager -p 'test_cloud_recovery.py'
```

测试使用模拟接口与临时数据库，不发送真实 QQ 消息或空间评论。测试不依赖个人的 AstrBot 配置和聊天历史。

## 上游项目

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [SnowLuma](https://github.com/SnowLuma/SnowLuma)
- [noVNC](https://github.com/novnc/noVNC)

第三方文件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

