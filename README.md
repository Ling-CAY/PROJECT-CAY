# CAYRememberYou（跨群记忆）

> **默认不消耗 Token**：纯本地关键词检索（只读 MaiBot 自己的 SQLite）。
> 向量语义是**可选开关**，只有你手动打开后才会产生 embedding 费用。

让麦麦记得你在**别的群**说过什么，并在合适的时机自然提起。

## 安装

**方式一：插件市场（推荐）**

MaiBot WebUI → 插件 → 插件市场 → 搜索「跨群记忆」→ 安装 → 启用 → 重启。

**方式二：手动安装**

把本仓库内容放进 `数据目录/plugins/CAYRememberYou/`，重启 MaiBot。

> `config.toml` 由 Runner 根据 `PluginConfigBase` 自动生成，**不要手动放在插件目录里**。

## 功能

- **自动注入**：每条消息按内容检索其他群的相关片段，作为系统提示注入（不改你的原话）
- **记忆曲线**：默认按半衰期衰减（越久远越模糊），也可切到固定时间窗
- **模型工具** `search_cross_group_memory`：麦麦可以主动检索
- **命令** `/跨群记忆 <关键词>`、`/跨群记忆 统计`
- **隐私**：私聊是否作为来源、按群黑白名单、昵称打码
- **零侵入**：只读数据库，不建表、不写库

## 两套检索

| 模式 | 成本 | 能力 |
|---|---|---|
| 关键词（默认） | **0 Token** | 字面匹配 + 中文近义词扩展 + 子串模糊 + 时间衰减 |
| 向量语义（可选） | 每次检索 1 次 embedding | 同义、近义表达也能召回 |

## 配置要点

```toml
[memory]
inject_enabled = true
decay_mode = "记忆曲线 / Curve"
lookback_days = 30
min_score = 0.18

[semantic]
enabled = false          # 打开后才有 embedding 消耗
rerank_limit = 20        # 只重排 Top N，越小越省
min_similarity = 0.35
```

## 兼容性

- MaiBot 1.3.0 ~ 1.3.99
- 纯 Python 标准库，无第三方依赖

## 许可

MIT