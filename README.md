# astrbot_plugin_whale_social

让鲸鱼娘在群聊里像**普通群友**一样自然地潜水、观察，偶尔主动说一句。

> 大多数时候潜水，偶尔参与；有兴趣才说话；说完不会连续刷屏；没人接话时会自然沉默。

本插件**只做主动层**：被 @ / 唤醒的消息一律交给 AstrBot 默认 agent，插件只观察、不回复、不拦截事件。主动发言只在**发送成功后**尝试写回 AstrBot 会话记忆，让后续正常回复也能看到它刚才说过的话。

- 目标平台：AstrBot 4.x（`astrbot_version: ">=4.5.7,<5"`）
- 当前版本：`0.2.4`
- 设计文档（本地 `docs/`，不随仓库发布）：`docs/astrbot_plugin_whale_social_PLAN.md`、`docs/astrbot_plugin_whale_social_PLAN_v2.md`（V2 会话线程 / Debounce / 群级决策，已在 `0.2.1` 落地）

---

## 特性

- **克制的主动参与**：本地规则先过滤，绝大多数消息直接被丢弃，只有少量进入 LLM 决策。
- **三态决策**：`IGNORE` / `WAIT` / `SPEAK`，强制 JSON 输出，带括号平衡容错解析。
- **会话线程（V2）**：把群消息按「引用 → @ → 关键词 → 同人续聊 → 新会话」聚成会话，每次只挑一个最值得参与的会话，而不是逐条消息反应。
- **防抖决策（V2）**：群聊刷屏时先静默等待稳定（`debounce_seconds`），最多等到 `debounce_max_wait_seconds`，避免在别人打字中途插话。
- **群级决策（V2）**：LLM 输出 `thread_id` 与 `target`；`target.type=USER` 时可选 @ 对方（默认关闭）。
- **重连去重（第一层基础设施）**：以 `umo:message_id` 为键、TTL + 容量上限的全局去重缓存，服务重连重放的事件在进入 Collector / 线程 / LLM 之前就被丢弃；无 ID 时的指纹兜底默认关闭，避免误伤连续相同发言。
- **群级任务互斥**：一个群同一时刻只有一个 debounce 定时器 + 一个在途决策，重复事件不会产生第二个 LLM 任务或重复回复。
- **分档冷却**：普通 / 刚参与 / 连续发言 / 上次被忽略，四档随机冷却，持久化到磁盘，重启后依然有效。
- **回应窗口**：主动发言后开启窗口；只有**与机器人相关**的消息（@ 机器人 / 引用机器人消息 / 同一会话续聊）才算“有人接话”，无关闲聊不会错误地清除“被忽略”标记。
- **活跃度反向调节**：群越刷屏，越不插话。
- **兴趣关键词**：支持负向词与每群覆盖，命中兴趣提升意愿。
- **延迟 + 复检**：发言前等待 2–8 秒，并复检冷却、会话是否仍活跃、是否被 @，以及**话题是否已被新消息切走**（`revision` 复检），任何变化都可能取消。
- **出站即入库**：发送成功后立即把机器人消息写入本地会话与线程，不依赖平台回推；若平台回推自身消息，按短期指纹去重，避免重复计数。
- **非文本归一**：图片 / 语音 / 视频 / 文件 / 表情在入口处归一为 `kind` + `[占位文本]`，保留事件语义而非当作空消息丢弃。
- **决策模型失败退避**：Provider 缺失、网络失败或无效 JSON 与“模型拒绝参与”区分处理，前者进入独立短退避并计数，避免对坏 Provider 反复调用。
- **记忆回写与注入**：主动消息写回会话历史；正常 LLM 请求时以临时动态上下文注入群情，不污染 system prompt。
- **流控与韧性**：每群 + 全局令牌桶、每群 + 全局每日上限、发送失败指数退避、决策失败退避、时区感知的跨日重置与静默时段、状态 TTL/数量淘汰。
- **安全默认**：白名单**默认关闭**（空 = 不启用任何群）；支持 `dry_run`；解析 / Provider / 记忆失败均安静降级。
- **管理命令**：`/ws status|enable|disable|reset|why`（管理员）。
- **纯净核心**：`core/` 与 `storage/` 不依赖 AstrBot，可直接跑单元测试（无需 LLM 或真实实例）。

---

## 安装

**前置要求**：AstrBot 4.5.7+（`astrbot_version: ">=4.5.7,<5"`）。插件依赖只有 `tzdata`（Windows / 精简镜像上 `zoneinfo` 需要），AstrBot 加载插件时会按 `requirements.txt` 自动安装，无需手动操作。

### 方式一：WebUI 从仓库安装（推荐）

1. 打开 AstrBot WebUI → **插件管理**，选择「安装插件 / 从仓库安装」。
2. 填入仓库地址：

   ```
   https://github.com/h3n5/astrbot_plugin_whale_social
   ```

3. 安装完成后，在插件管理中确认「鲸鱼娘社交引擎」已启用；如未自动启用请手动启用（或重启 AstrBot）。

### 方式二：手动安装

1. 将本仓库放入 AstrBot 插件目录，目录名保持 `astrbot_plugin_whale_social`：

   ```bash
   cd <AstrBot>/data/plugins
   git clone https://github.com/h3n5/astrbot_plugin_whale_social
   ```

   也可以下载仓库 zip 后解压为同名目录。

2. 重启 AstrBot，在 WebUI → 插件管理中确认插件已加载并启用。

### 安装后配置

1. 打开插件配置：
   - 建议先开启 `dry_run`（演练模式）观察；
   - 在 `group_allowlist` 中填入要启用的群 **UMO**（可用 `/sid` 获取）；
   - 确认 `provider_id` 留空即可复用当前会话模型，或显式指定。

2. 观察日志 / `/ws status` 一段时间，确认决策符合预期后，再关闭 `dry_run`。

> **运营前提**：目标群应处于“被 @ / 唤醒才回复”模式（AstrBot 默认）。若开启“回复所有群消息”，默认 agent 会与本插件抢发消息。

---

## 配置项

WebUI 配置文件为 [`_conf_schema.json`](./_conf_schema.json)，全部默认值如下：

| key | type | default | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 全局开关（kill switch） |
| `dry_run` | bool | `false` | 只记录决策与日志，不发送、不改状态 |
| `group_allowlist` | list | `[]` | **默认关闭**；空 = 不启用任何群；每项为 UMO |
| `min_message_length` | int | `2` | 过短消息不触发判断 |
| `context_message_limit` | int | `20` | 入窗并发送给 LLM 的最近消息条数 |
| `incoming_rate_limit` | int | `30` | 30 秒内人类消息超过此值视为刷屏 |
| `min_cooldown_seconds` | int | `900` | 主动发言冷却下限 |
| `max_cooldown_seconds` | int | `1800` | 主动发言冷却上限 |
| `reply_window_seconds` | int | `120` | 回应窗口；超时无回应标记“被忽略” |
| `base_speak_probability` | float | `0.08` | 主动发言基础概率（× SpeakScore，上限 0.8） |
| `high_interest_bonus` | float | `1.8` | 高兴趣话题的参与倍率上限 |
| `energy_initial` | float | `0.6` | 初始社交能量 |
| `energy_max` | float | `1.0` | 社交能量上限 |
| `daily_proactive_cap` | int | `20` | 每群每日主动上限；`0` = 不限制 |
| `global_daily_proactive_cap` | int | `100` | 全局每日主动上限；`0` = 不限制 |
| `group_token_bucket_capacity` | int | `3` | 每群突发令牌上限；`0` = 不限制 |
| `group_token_refill_seconds` | int | `900` | 每群每恢复一个令牌所需秒数 |
| `global_token_bucket_capacity` | int | `10` | 全局突发令牌上限；`0` = 不限制 |
| `global_token_refill_seconds` | int | `300` | 全局每恢复一个令牌所需秒数 |
| `send_failure_backoff_seconds` | int | `300` | 发送/Provider 失败后的初始退避秒数 |
| `max_failure_backoff_seconds` | int | `3600` | 指数退避上限 |
| `llm_failure_backoff_seconds` | int | `60` | 决策模型调用失败 / 无效 JSON 后的退避秒数；`0` = 不退避 |
| `llm_timeout_seconds` | int | `60` | 等待决策模型响应的最长秒数，超时按失败退避；`0` = 不限制 |
| `timezone` | string | `Asia/Shanghai` | 每日配额、跨日重置与静默时段使用的 IANA 时区；留空用本机时区 |
| `max_group_states` | int | `100` | 最多追踪群数，超出按最近活跃保留；`0` = 不限制 |
| `state_ttl_seconds` | int | `604800` | 长期不活跃群状态的淘汰秒数（7 天）；`0` = 不淘汰 |
| `active_hours` | string | `08:00-23:59` | 允许主动的时段，支持跨午夜（如 `22:00-06:00`） |
| `debounce_seconds` | float | `3` | 群聊防抖静默时长：新消息把决策推后 |
| `debounce_max_wait_seconds` | float | `8` | 防抖最长等待；`0` = 不限制 |
| `thread_window_seconds` | int | `180` | 会话活跃窗口，超时视为结束 |
| `thread_join_time_gap_seconds` | int | `120` | 无引用/@关系时，并入同一会话的最大时间间隔 |
| `max_threads` | int | `3` | 每群同时追踪的会话上限 |
| `min_thread_messages` | int | `1` | 触发判断所需的会话最少消息数 |
| `thread_selection` | string | `most_active` | 会话选择策略：`most_active` / `interest` |
| `thread_time_continuity_merge` | bool | `true` | 单一近期会话 + 无关键词消息时，按时间连续性并入该会话（低置信度聚类兜底） |
| `reply_mention_user` | bool | `false` | `target.type=USER` 时是否 @ 对方 |
| `extract_message_segments` | bool | `true` | 解析引用/@ 消息段用于会话归并 |
| `dedup_ttl_seconds` | float | `300` | 同一 `message_id` 重复推送在该秒数内直接丢弃 |
| `dedup_max_entries` | int | `10000` | 去重缓存容量上限；`0` = 不限制 |
| `dedup_fallback_seconds` | float | `0` | 无 `message_id` 时的指纹兜底窗口；`0` = 关闭 |
| `provider_id` | string | `""` | 留空使用当前会话模型 |
| `interest_keywords` | text | 游戏/副本/… | 每行一个兴趣关键词 |
| `negative_keywords` | text | `""` | 每行一个，命中则直接放弃本次参与 |
| `group_keyword_overrides` | list | `[]` | 每项 `UMO=关键词1,关键词2`，覆盖该群的兴趣关键词 |
| `output_blocklist` | text | `""` | 每行一个，回复命中则丢弃 |
| `max_reply_length` | int | `200` | 主动回复的最大字符数，超出截断；`0` = 不限制 |
| `use_astrbot_memory` | bool | `true` | 预留：读取 AstrBot 会话历史作为上下文（见「已知限制」） |
| `memory_writeback` | bool | `true` | 主动发言成功后写回会话记忆 |
| `inject_group_context` | bool | `true` | 正常 LLM 请求时注入简短群情 |
| `persona_prompt` | text | 鲸鱼娘人格 | 只负责“她是谁 / 怎么说话” |
| `decision_prompt` | text | `""` | 留空使用内置决策 Prompt；只负责“是否参与” |

---

## 管理命令

需管理员权限：

| 命令 | 说明 |
|---|---|
| `/ws status` | 查看本群启用状态、冷却剩余、能量、今日主动数、上次决策 |
| `/ws enable` | **临时**启用本群（仅本次运行有效，持久请改 WebUI） |
| `/ws disable` | **临时**停用本群 |
| `/ws reset` | 清除本群社交状态 |
| `/ws why` | 查看上次决策、冷却、连续发言、被忽略状态与当前时段 |

---

## 工作流程

```
群消息事件
   │
   ├─ 被 @ / 唤醒 ────────────────► AstrBot 默认 agent 回复（插件不参与）
   │                                  插件仅：提升能量、标记 @、开启冷却让位
   │
   └─ 普通消息
        ▼
   Dedup       umo:message_id 精确去重（TTL + 容量上限，第一关）
        ▼
   Collector   入窗 / message_id 去重 / 滑动速率 / 能量
        ▼
   Threads     引用→@→关键词→同人续聊→新会话（每群最多 N 个）
        ▼
   Debounce    静默 debounce_seconds，最多等 debounce_max_wait_seconds
        ▼
   Thread 选择  most_active / interest，只挑一个会话
        ▼
   Gate        白名单 / 冷却 / 刷屏 / 连续发言 / 令牌桶 / 发送失败退避 / 决策失败退避 / 静默时段 / 每日上限
        ▼
   Scorer      TopicInterest × Activity × Energy × Ignored × Streak → [0, 3]
        ▼
   概率 = clamp(base_probability × score, 0, 0.8)
        ▼
   Decision LLM ── IGNORE（记录）/ WAIT（记录话题）/ SPEAK（含 thread_id、target）
        ▼
   延迟 2–8s + 复检（冷却 / 会话是否仍活跃 / 是否被 @ / revision 是否已切话题）
        ▼
   流控预占令牌 ── send_message（可选 @ 目标用户）
        ├─ 失败：退还令牌 + 指数退避
        └─ 成功：分档冷却 + 回应窗口 + 能量下降 + 每日计数 + 本地入库 + 记忆回写
```

分档冷却（在配置的 `min/max_cooldown_seconds` 基础上缩放）：

| 场景 | 倍率 |
|---|---|
| 普通主动发言 | ×1.0 |
| 刚参与过讨论（本进程内有 bot 发言） | min×⅓ / max×½ |
| 连续主动发言 | ×2.0 |
| 上次完全静默（无人回应） | min×2.0 / max×3.0 |

---

## 记忆

- **写回**：主动发送成功后，以「触发消息 → 主动回复」一对写入当前会话（`conversation_manager.add_message_pair`）。
- **注入**：在 `@filter.on_llm_request` 中把群情摘要作为 `extra_user_content_parts` 注入，并尝试 `mark_as_temp()`（该 API 需 ≥ 4.24.0，缺失时降级）。
- **降级**：发送或回写失败只记录脱敏日志，不影响默认回复管线。
- **验收**：上线前请在目标平台确认主动回写使用的 CID 与后续 @ 回复读取的 CID 一致；若不一致，请关闭 `memory_writeback`。

---

## 安全默认与边界

- 白名单默认关闭；空列表 = 全群静默。
- 被 @ 时**永不** `stop_event()`、**不 yield**，绝不重复回复。
- 群聊内容视为不可信数据，作为数据段传给决策模型，并限制回复长度、过滤禁用词。
- 只持久化节奏/计数类字段；**不持久化**消息窗口与速率桶（时间戳会过期污染上下文）。
- `state.json` 使用临时文件 + `os.replace` 原子替换，并带 `schema_version` 版本守卫。
- 平台风控：每群/全局令牌桶 + 每群/全局每日上限 + 静默时段 + 有上限的指数退避；发送失败**不**计入成功配额、**不**回写记忆。
- V1 明确只支持**单实例**运行。

---

## 项目结构

```
main.py                 # 事件入口、生命周期、AstrBot 适配、/ws 命令
metadata.yaml
_conf_schema.json
requirements.txt        # 仅 tzdata（Windows 时区数据库）
core/                   # 纯 Python 策略层（无 AstrBot 依赖）
  config.py             # PluginConfig + 时段解析
  models.py             # ChatMessage / GroupState
  collector.py          # 入窗 / 去重 / 速率 / 能量 / 出场记账
  content.py            # 非文本消息归一（kind + 占位文本）
  gate.py               # Signal Gate
  scorer.py             # SpeakScore
  topic.py              # 关键词兴趣 / 负向词
  cooldown.py           # 分档冷却 + 回应窗口
  flow.py               # 令牌桶 / 每日配额 / 失败退避 / 全局状态
  timeutil.py           # 时区与跨日 day key
  threads.py            # 会话聚类 / 活跃度 / 选择
  debounce.py           # 防抖截止时间策略
  dedup.py              # 重连重放去重（TTL + 容量上限）
  decision.py           # Prompt 构建 + JSON 容错解析（V2: thread_id/target）
  reply.py              # 回复清洗 / 过滤 / 截断
  memory.py             # 群情 hint 与回写 payload
  engine.py             # 异步编排（依赖注入，便于测试）
storage/
  state_store.py        # 原子持久化 + schema 守卫（含全局流控状态）
tests/                  # pytest + 假 LLM / 假发送，无需 AstrBot
data/
  state.json            # 运行时状态（自动生成，勿提交真实群数据）
```

---

## 测试

测试不调用 LLM、不依赖 AstrBot，全部用 mock 边界：

```bash
python -m pytest -q
```

覆盖：冷却分档与持久化、回应窗口结算（含“无关消息不关窗”）、去重（含重连重放 TTL/容量/指纹）、窗口截断、速率、SpeakScore、Gate 各分支、JSON 容错解析（含 V2 `thread_id`/`target`）、回复过滤、会话聚类与活跃度（含时间连续合并）、防抖截止时间、非文本归一、原子写与版本守卫、以及引擎端到端（发言/忽略/被 @/dry_run/冷却/竞态取消/延迟期间话题切换/同线程续聊仍发送/出站本地入库与回显去重/决策失败退避/shutdown/防抖合并/线程选择/@ 目标）。

> 测试需要 `pytest`（仅开发依赖，插件运行本身无第三方依赖）。

---

## 已知限制 / 路线图

当前 `0.2.2` 已实现核心主动链路、全部 P0 修正、V1.1 的流控/韧性、V2.0 的会话线程 / 防抖 / 群级决策、重连重放的第一层去重，以及一轮代码审查的 P0/P1/P2 修正（revision 发送前复检、时区化静默时段、回应窗口相关性判定、出站消息本地入库 + 回显去重、决策模型失败退避、非文本归一、时间连续聚类兜底）；以下为尚未落地的部分：

- `use_astrbot_memory`：配置项已预留，**尚未**读取 AstrBot 会话历史做额外上下文。
- 决策与回复目前为**一次** LLM 调用（同一次 JSON 同时给出 `action`、`thread_id`、`target` 与 `reply`），计划中的“两阶段分离”尚未拆分。
- 未实现审计日志与影子模式指标。
- 发送 reservation 目前为轻量实现：`pending` 群级互斥 + 发送前 `revision`/话题复检，尚未做持久化预留。
- V2 剩余：话题 embedding、群/用户画像、每群人格、WebUI 会话视图。详见本地设计文档 `docs/astrbot_plugin_whale_social_PLAN_v2.md`（不随仓库发布）。

> `timezone` 依赖 IANA 时区数据库；Windows 等环境由 `requirements.txt` 自动安装 `tzdata`。若无法解析，插件会安静回退到本机时区。

---

## 许可与合规

- 不主动声明自己是 AI；平台与法规合规责任由运营方自行评估。
- 请勿提交凭据、真实群 UMO 或真实聊天记录。
- `metadata.yaml` 中的 `repo` 为占位地址，发布前请替换。
# astrbot_plugin_whale_social
