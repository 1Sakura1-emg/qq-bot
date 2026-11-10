# 烬璃每日空间动态

为当前龙娘小号提供单一用途的 QQ 空间定时发布能力。每天北京时间 19:45，使用 AstrBot 已配置的 DeepSeek 模型和 `jinli` 人格生成一条纯文字说说，尝试按好友可见发布。

在 AstrBot 的插件配置中可修改发布时间、内容要求或关闭自动发布。时间使用 24 小时制 `HH:MM`。保存配置后重载本插件，使新的时间生效。

插件从指定 OneBot 连接获取 QQ 空间登录状态，每次请求前核对账号，不把登录凭据保存到文件或发给模型。生成内容只使用角色提示词、日期和最近七条本插件发布的内容，不读取私人聊天记录。不提供聊天指令或模型工具来发布、删帖、评论和点赞。

`data/plugin_data/astrbot_plugin_jinli_qzone/status.json` 保存只读连接检查结果与未发布的预览；`state.json` 保存发布尝试日期、结果和最近七条正文。不要在当天删除 `state.json` 后重载插件，以免丢失去重记录。

每次启动只注册之后的定时点，不补发停机时错过的动态。网络超时可能发生在发布已经成功之后，因此同一天不自动重试发布。若平台要求重新登录、开通空间或验证，请在手机 QQ 上自行完成，再重载插件检查。

此实现使用 QQ 空间网页接口，平台调整可能影响功能；只读检查通过不能代替首次真实发布验证。凭据仅发送给 QQ 空间的 HTTPS 地址。

发布字段及登录接入方式参考：

- https://github.com/Zhalslar/astrbot_plugin_qzone/tree/f474c50c9b517655d8475df34ad0c129b70193d4/core/qzone
- https://github.com/Gu-Heping/onebot-qzone/blob/main/doc/emotion-api.md
