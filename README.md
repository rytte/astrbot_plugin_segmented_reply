# 分段回复

将 AstrBot 完整回复中的 `Plain` 消息按标点或自定义规则拆分，再按随机或对数间隔逐段发送。适用于希望把 AstrBot 原生分段回复单独配置、维护的场景。

## 安装

将本目录放入 `AstrBot/data/plugins/astrbot_plugin_segmented_reply`，在插件管理中启用并配置。此插件没有额外依赖。

启用插件配置中的“启用分段回复”后，请关闭 AstrBot 全局设置 `platform_settings.segmented_reply.enable` 并重启 AstrBot，使插件接管分段发送。若全局原生开关仍开启，插件会跳过处理，避免重复切分和间隔叠加。

插件只使用公开的事件钩子和发送接口，不修改 AstrBot 核心源码。分段发生在发送前，核心后续仍负责回复前缀、引用、@、TTS、转图片和转发等处理；启用 TTS 或其他消息段时，发送顺序以当前 AstrBot 版本的响应阶段行为为准。

## 配置

“省略号拆分优化”“跳过首段回复的延迟等待”和“启用分段回复”是独立开关，位于“分段回复设置”组外；其余配置项都在该组中。

- `only_llm_result`：仅对模型生成结果分段，默认开启；模型运行器错误结果也属于模型结果。
- `interval_method`：随机间隔按 `interval` 的最小、最大秒数抽样；对数间隔按 ASCII 单词数或非 ASCII 字母数字字符数计算。
- `acceleration_ratio`：新消息到达时的加速比例，范围 `0～1`，默认 `1` 不加速。同一会话的新消息进入 LLM 会话锁等待前，正在发送的回复会将当前尚未等待完的时间及后续分段间隔乘以该比例。例如设为 `0.7`，后续原本等待 `3` 秒的分段改为等待 `2.1` 秒；若当前等待还剩 `2` 秒，则缩短为 `1.4` 秒。重复到达的新消息不会叠乘；`0` 表示取消剩余人为等待。只对当前回复生效，不影响其他会话或下一次回复；超出范围或非有限数值会直接报错。
- `words_count_threshold`：单条 `Plain` 文本长度超过上限时不拆分，默认 `300`；判断长度与 AstrBot 原生实现一致，使用字符数。
- `split_mode`：正则模式使用 `re.findall`；默认规则不把 `…` 当作正则分隔符。“省略号拆分优化”开启时，只有省略号前后都有字母或数字，且省略号前最近的非空白字符不是标点时才拆分。因此 `（123）……456` 不会在省略号后拆分。关闭时省略号不触发拆分。分段词模式按 `split_words` 中任意词匹配，匹配词会从段尾移除。
- `content_cleanup_rule`：默认移除句号 `。`；对拆出的每段执行 `re.sub(rule, "", text)`，然后去除首尾空白。清空此项可关闭过滤。
- `skip_first_delay`：默认开启，首段回复立即发送，后续段仍按所选间隔算法等待；关闭后首段也会等待。

流式结果不处理；`qq_official_webhook`、`weixin_official_account` 和 `dingtalk` 与 AstrBot 原生实现一样不进行分段。回复前缀、引用、@、TTS、转图片和转发等后续处理仍由 AstrBot 核心完成。

本插件按 AstrBot 核心分段回复的行为实现。参考：[AstrBot `ResultDecorateStage`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/pipeline/result_decorate/stage.py) 和 [`RespondStage`](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/pipeline/respond/stage.py)。
