# 本机短剧生产路由

这是本地 `drama-skills-local` 的运行规则，不属于通用上游协议。凡是在这台 Mac 上执行短剧媒体生产，先读取本文件，再读取对应 Skill 的 `SKILL.md`。

## 固定路由

| 媒体 | 默认供应商/模型 | 运行要求 |
|---|---|---|
| 图片、角色/场景/道具资产、冻结起始帧 | RunningHub 国际站 G2 | 使用已登记的 G2 图生图入口，并绑定项目实际参考图 |
| 视频、对白、环境声、背景声 | MiniMax H3 | 使用已登记的 H3 音视频提示词与参考图绑定 |

## 凭证继承

供应商凭证只从导演工作台根目录读取：

```text
/Users/mac/Documents/ChatGPT/AI短视频导演工作台/.env.local
```

RunningHub 国际站使用以下字段，字段名必须保持不变；这里只记录字段名，不记录密钥值：

```text
RUNNINGHUB_INTL_BASE_URL
RUNNINGHUB_INTL_API_KEY
RUNNINGHUB_G2_PATH
```

禁止从旧的 OpenClaw 配置、临时缓存、项目目录、聊天记录或历史 adapter 配置读取密钥。也禁止把密钥复制到本仓库、项目文件、批次 JSON、Markdown、日志或回复中。

本机 RunningHub G2 的长期 adapter 配置位于：

```text
/Users/mac/Documents/ChatGPT/drama-skills-local/local/production-adapters.json
```

它只保存可执行入口和超时，不保存凭证；入口脚本位于
`skills/short-drama-produce/scripts/runninghub_g2_workbench_adapter.py`，运行时读取上述工作台
`.env.local`。不要再把它替换成 `/tmp` 下的一次性 adapter。

## 执行前检查

1. 确认上述 `.env.local` 存在，并且 adapter 明确加载它。
2. 运行供应商连通性检查，确认使用的是国际站配置；检查失败时停止，不切换到旧配置，也不提交任务。
3. 生产仍必须遵守 `prepare -> explicit confirm -> run`；确认失败、配置变化或任务内容变化后必须重新确认。
4. adapter 可以放在项目外，但不能只依赖 `/tmp` 临时文件。长期复用的入口、说明和加载逻辑必须能从本仓库找到。
5. 供应商成功返回后，继续核对项目内实际输出文件、媒体格式和绑定记录；任务成功不等于画面质量通过。

## 本次错误的修正

曾有一次临时 RunningHub adapter 误读旧 OpenClaw 配置，导致无效 Key。以后以本文件和导演工作台根目录 `.env.local` 为唯一配置继承规则，不再重新寻找或猜测账号来源。
