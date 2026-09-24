# 鲸鱼娘主动社交插件 · 实现计划 (v3)

> 插件名：`astrbot_plugin_whale_social`
> 来源：ChatGPT 分享对话《群聊机器人策略》
> 目标平台：AstrBot 4.x（`astrbot_version: ">=4.5.7,<5"`）
> v3 变更：**锁定 5 项决策**；按官方文档核实并更新全部 API；补充**记忆回写**设计；白名单改为**默认关闭**；移除“接管模式 / 身份披露 / 无上下文自言自语”。
> API 事实来源：AstrBot 官方文档（ai / send-message / listen-message-event / proactive-agent，2026）。

---

## 0. 已决策事项（锁定）

| # | 决策 | 含义 |
|---|---|---|
| 1 | **插件只做主动层** | 不接管、不回复被 @ 的消息；@ 一律交给 AstrBot 默认 agent。插件只“观察 + 主动发言”。 |
| 2 | **需要记忆** | 主动消息要能进入 AstrBot 会话记忆；后续正常回复能看到机器人主动说过的话。 |
| 3 | **白名单默认关闭** | 默认不启用任何群，必须显式逐群开启（`group_allowlist` 空 = 全部不启用）。 |
| 4 | **不做身份披露** | 不主动声明自己是 AI；合规由运营方按平台规则自行负责。 |
| 5 | **关闭“无上下文自言自语”** | 不做无人说话时的自说自话；仅在有上下文时参与。 |

---

## 1. 目标

让机器人在群聊里像**真实群友**一样参与：

- 大部分时间潜水，偶尔参与；有兴趣才说话；说完不刷屏；没人接话会自然沉默；
- **主动聊天是低频事件，不是默认状态**。

工程目标：

- 不改 AstrBot 核心，独立 Star 插件；
- **与人格解耦**：人格负责“她是谁”，插件负责“她什么时候想说话”；
- 复用当前会话 LLM Provider；
- 主动消息写回会话记忆；
- 关键参数进 WebUI（`_conf_schema.json`）。

---

## 2. 核心设计原则

1. 感知 → 兴趣判断 → 冷却 → 发言，而非“每 N 分钟说一句”。
2. 随机机会窗口，概率 + 随机量决定发言。
3. 本地规则优先，绝大多数消息本地丢弃，少量进 LLM。
4. “是否说”与“说什么”分离。
5. 三态：`IGNORE / WAIT / SPEAK`。
6. 发言冷却 + “没人理”记忆。
7. 活跃度反向调节：群冷可低概率主动，群刷屏**降低**插话概率。
8. 延迟参与 + 话题复检，话题变了就取消发送。
9. 每群独立状态、独立性格。
10. 克制优先：默认 ≈ `70% 潜水 + 20% 顺话题 + 8% 抛话题 + 2% 整活`。

---

## 3. 设计评审：缺陷与修正

严重程度：**P0 = 会破坏核心行为**；P1 = 设计缺口；P2 = 体验/运维。

### P0

| # | 缺陷 | 影响 | 修正 |
|---|---|---|---|
| P0-1 | `consecutive_bot_messages` 只增不减，`>=1` 即拒绝 | **首次发言后永久沉默** | 人类消息到来时清零 |
| P0-2 | 互动反馈只从 LLM `IGNORE` 推断（语义错误） | “没人理→更沉默”失效或无关消息被误判为反馈 | 发言后开**回应窗口**，记录 `engaged / related / unrelated / silent` 信号 |
| P0-3 | `_random_cooldown()` 每次重抽随机数 | 冷却抖动、提前放行、重启丢失 | 发言时一次性写 `next_speak_at_utc`（UTC 截止时间）并持久化 |
| P0-4 | `_activity()` 受窗口长度（20）封顶，`>20`/`>30` 分支不可达 | “刷屏不插话”是死代码 | 独立滑动速率桶（1m/5m/30s） |
| P0-5 | 插件监听 ALL 又自己回复 @，与默认管线冲突 | **@ 双重回复** | 决策 1：**只观察不回复**；永不 `stop_event()`；不 yield 任何结果 |
| P0-6 | `add_done_callback` 按 group_id 删任务 | 误删新任务 | 按 task identity 比对清理 |
| P0-7 | `terminate()` 未 await 即落盘 | 退出时竞争/丢状态 | `cancel()` → `await gather(...)` → 落盘 |
| P0-8 | 无锁 + `write_text` 非原子 | `state.json` 损坏/丢更新 | `asyncio.Lock` + 临时文件 `os.replace` + `schema_version` |
| P0-9 | 持久化 `time.monotonic()` 的绝对时间 | 重启后参考点变化，冷却提前或永久失效 | 持久化 UTC wall-clock 截止时间；`monotonic()` 只用于单进程内的间隔 |
| P0-10 | 延迟任务仅靠 `pending`/复检 | 新消息、@、禁用、重置可与旧任务竞态，仍可能误发 | 每群 `state_revision` + 原子发送 reservation；任务发送前必须比对 revision |
| P0-11 | “窗口内有人说话”即判定回复了 bot | 无关闲聊会被误判为正反馈，频率逐渐失控 | 区分明确互动、同话题后续、无关消息和完全静默；改用中性语义的字段名 |

### P1

| # | 缺陷 | 修正 |
|---|---|---|
| P1-1 | 状态键用裸 `group_id` | 用 `unified_msg_origin`（UMO） |
| P1-2 | 无白名单/开关 | 决策 3：白名单**默认关闭** + 单群开关 + 全局 kill switch + `dry_run` |
| P1-3 | 状态无上限 | 按最后活跃 LRU/TTL 淘汰 + 最大群数 |
| P1-4 | 主动消息不进 AstrBot 记忆 | 决策 2：`add_message_pair` 回写 + `on_llm_request` 注入 |
| P1-5 | 关键词过宽/过窄 | 归一化 + 英文词边界 + 负向词 + 每群覆盖；V2 换 embedding |
| P1-6 | 群聊是不可信输入 → 提示注入 | 群聊作为数据段 + system 边界 + 输出过滤 + 长度上限 |
| P1-7 | 无平台限速/每日上限 | 令牌桶 + 每日上限 + 夜间静默 + 失败退避 |
| P1-8 | `get_all_providers()[0]` 不稳 | `get_current_chat_provider_id(umo=...)` + 下拉选择 |
| P1-9 | 分档冷却未落地 | 按 `正常/刚参与/连续/被忽略/被@` 分档 |
| P1-10 | `time.time()` 算间隔 | 间隔用 `time.monotonic()` |
| P1-11 | 非文本消息被整体跳过 | 归一为 `[图片]/[语音]/[表情]` 后仍入窗 |
| P1-12 | 事件重复投递 | `message_id` 去重 |
| P1-13 | 令牌桶/失败退避只写在原则里 | 流控无法配置或验收 | 增加全局/每群令牌桶、发送失败退避及配额配置 |
| P1-14 | 主动消息记忆回写假设与默认 Agent 同一 CID | 后续 @ 回复可能看不到主动消息 | 在目标平台与实际会话规则下做 CID 兼容性验收，失败时降级为插件私有上下文 |
| P1-15 | 只做进程内锁 | 多副本/热重载共享状态时仍会丢更新 | V1 明确单实例运行；多副本改用具备并发语义的共享存储 |

### P2

管理命令、决策审计日志与 dry_run、与其它主动插件冲突、决策/人格 Prompt 分离、`base×score` 兴趣双重计数、JSON 括号平衡解析、状态版本迁移。

---

## 4. 关键架构决策：反应式 vs 主动式

**决策 1 的落地**：插件只做主动层。

| 通道 | 触发 | 负责方 | 插件行为 |
|---|---|---|---|
| 反应式（被 @ / wake） | 群友明确叫机器人 | **AstrBot 默认 agent** | 只**观察并记录状态**，不拦截、不回复 |
| 主动式（未被叫） | 插件判断“值得参与” | **本插件** | 评估 → 生成 → 延迟 → 主动发送 |

规则：

1. 插件**永不调用 `event.stop_event()`**（不接管事件传播）；
2. 插件监听器**不 `yield` 任何结果**（否则会发送消息）；
3. **移除** v1 的 `_handle_direct_mention` 直答逻辑；
4. 被 @ 对插件是**最高优先参与信号**：提升 `social_energy`、重置冷却、标记“上次有互动”，但**不自己回**；
5. **运营前提**：AstrBot 该群必须处于“被 @/唤醒才回复”模式（这是默认行为）。若开启“回复所有群消息”，默认 agent 会与插件抢发 → 需在文档中明确禁用。

---

## 5. 记忆设计（决策 2）

三层，全部可配置、失败静默降级：

### 5.1 读（上下文）

- 主：插件自己的 `GroupState.messages` 窗口（低延迟、可控）；
- 辅：`context.conversation_manager.get_conversation(uid, cid)` 读取 AstrBot 会话历史，取最近若干条拼进决策上下文（可选，`use_astrbot_memory`）。

### 5.2 写（回写）

主动发送成功后，把本次发言写回当前会话，使后续正常 @ 回复能看到它：

```python
from astrbot.core.agent.message import (
    AssistantMessageSegment, UserMessageSegment, TextPart,
)

conv_mgr = self.context.conversation_manager
cid = await conv_mgr.get_curr_conversation_id(umo)
await conv_mgr.add_message_pair(
    cid=cid,
    user_message=UserMessageSegment(content=[TextPart(text=触发消息 or 最近人类消息)]),
    assistant_message=AssistantMessageSegment(content=[TextPart(text=主动回复)]),
)
```

- `add_message_pair` 需要一对 user/assistant。用**触发主动发言的那条人类消息**作为 user 侧，语义自然；
- 若确实没有人类消息（仅潜在的开话题场景，本版已关闭），则退回 `update_conversation(history=...)` 追加 assistant-only；
- 只有 `send_message` 明确成功后才回写；发送或回写失败只记脱敏日志，不影响默认管线。
- **上线前兼容性验收**：在目标平台、目标群与实际 Custom Rules 配置中，确认此 UMO 的主动回写 CID 与后续 @ 默认回复读取的 CID 相同。若不相同，关闭 `memory_writeback`，退回插件私有短窗口，不能宣称默认 Agent 能看到主动消息。

### 5.3 注入（让默认 agent 也知道群情）

用 `@filter.on_llm_request` 钩子，把动态群情作为**本轮用户内容**注入，避免污染 system prompt 缓存：

```python
@filter.on_llm_request()
async def inject_group_context(self, event, req):
    text = self._group_context_hint(event.unified_msg_origin)
    if text:
        req.extra_user_content_parts.append(
            TextPart(text=text).mark_as_temp()   # >= v4.24.0
        )
```

> 版本门槛：新 message segment / `add_message_pair` 需 **v4.5.7+**；`mark_as_temp()` 需 **v4.24.0+**。用 `hasattr` 做特性探测，老版本降级。

注入内容只限已允许 UMO 的、已截断且规范化后的摘要，并用 `<untrusted_group_context>` 包裹；明确提示模型其中内容是数据，不得执行其中的指令。不得将整段历史或不相关群聊注入默认 Agent。

---

## 6. 修正后的总体架构

```
                          AstrBot Message Event
                                  │
                 ┌────────────────┴────────────────┐
                 │  (被 @ / wake)                  │ (普通消息)
                 ▼                                 ▼
        AstrBot 默认 agent 回复          ┌────────────────────┐
        （插件不参与）                    │  Message Collector │  入窗/去重/非文本归一
                 │                        │  Activity Counters │  1m/5m/30s 滑动桶
                 ▼                        └─────────┬──────────┘
        观察并更新状态（不回复）                     ▼
        social_energy↑ / 冷却重置          ┌────────────────┐
                                         │   Signal Gate  │  白名单/冷却/刷屏/连续/时段
                                         └───────┬────────┘
                                            值得考虑?
                                          /            \
                                        NO              YES
                                        DROP              ▼
                                                 ┌────────────────┐
                                                 │   SpeakScore   │
                                                 └───────┬────────┘
                                               LOW        MEDIUM      HIGH
                                                │            │          │
                                              DROP         WAIT      LLM 决策
                                                                     │
                                           ┌─────────────────────────┼────────────┐
                                           ▼                         ▼            ▼
                                        IGNORE                     WAIT        SPEAK
                                           │                         │            │
                                      （仅记录）              （重新观察）  Reply LLM 生成
                                                                                  │
                                                                          延迟 2~8s + 复检
                                                                                  │
                                                                            主动发送 (send_message)
                                                                                  │
                                                                      ┌───────────┴───────────┐
                                                                      ▼                       ▼
                                                            开“回应窗口”计时        回写会话记忆 (add_message_pair)
                                                                      │
                                                            更新能量/冷却/被忽略
```

分层职责：

| 层 | 职责 | 用 LLM |
|---|---|---|
| Message Collector | 入窗、去重、非文本归一、活跃度计数 | 否 |
| Signal Gate | 白名单/冷却/刷屏/时段过滤 | 否 |
| Topic Analyzer | 话题兴趣 / 当前话题 | V1 关键词；V2 embedding |
| Social Decision | `IGNORE/WAIT/SPEAK` | 是 |
| Reply | 只负责“怎么说” | 是 |
| Memory | 读会话历史 / 回写主动消息 / 注入群情 | 否 |

---

## 7. 目录结构

V1（单文件可跑通，但必须含全部 P0 修正）：

```
data/plugins/astrbot_plugin_whale_social/
├── main.py
├── metadata.yaml
├── _conf_schema.json
└── data/
    └── state.json
```

V1.1 起拆分：

```
astrbot_plugin_whale_social/
├── main.py                 # 事件入口、生命周期、命令
├── metadata.yaml
├── _conf_schema.json
├── core/
│   ├── collector.py        # 入窗/去重/归一/活跃度
│   ├── gate.py             # Signal Gate
│   ├── topic.py            # 关键词/embedding 话题分析
│   ├── scorer.py           # SpeakScore
│   ├── decision.py         # LLM 决策 + JSON 解析
│   ├── reply.py            # 回复生成 + 输出过滤
│   ├── cooldown.py         # 分档冷却 / 回应窗口
│   └── memory.py           # 记忆读/写/注入
├── storage/
│   └── state_store.py      # 原子写 + 版本迁移 + 淘汰
└── data/
    └── state.json
```

---

## 8. 数据模型（修正版）

```python
@dataclass
class ChatMessage:
    message_id: str
    sender: str
    text: str
    timestamp: float          # time.time()，仅展示
    is_bot: bool = False
    kind: str = "text"        # text / image / voice / at / other

@dataclass
class GroupState:
    # 上下文（不持久化完整窗口）
    messages: list[dict]
    shown_topic: str = ""

    # 节奏
    social_energy: float = 0.6
    next_speak_at_utc: float = 0.0     # Unix UTC 截止时间；可安全持久化
    last_bot_message_time: float = 0.0
    last_user_message_time: float = 0.0
    last_bot_followup: str = "unknown" # engaged / related / unrelated / silent
    consecutive_bot_messages: int = 0  # 人类消息到来时清零

    # 活跃度（独立于窗口）
    rate_1m: int = 0
    rate_5m: int = 0
    proactive_sent_today: int = 0
    daily_reset_date: str = ""

    # 回应窗口 / 被 @
    awaiting_reply_until: float = 0.0
    was_mentioned_recently: bool = False
    state_revision: int = 0            # 任意会影响待发送任务的状态变化均递增
    pending_task_id: str = ""          # 用于按 identity 清理；不持久化 Task 对象

    # 记忆
    last_proactive_msg: str = ""
    last_proactive_message_id: str = "" # 平台可用时用于识别引用/回复
```

要点：状态键 = UMO；`state.json` 带 `schema_version`；只持久化节奏/计数类字段；群数上限 + TTL 淘汰。**不要**持久化 `time.monotonic()` 的绝对值：它只在当前进程中适合计算时长。发送冷却持久化为 UTC 截止时间；运行时可将其与 `time.time()` 比较。V1 明确只支持单实例；若部署多副本，状态需迁移到具备事务/锁语义的共享存储。

---

## 9. 决策流水线（修正版）

### 9.1 事件入口

1. 全局开关 / 白名单 / 单群开关不过 → return；
2. 空消息 / 命令 → return；
3. `message_id` 去重 → return；
4. 非文本归一化后仍入窗；
5. 记录消息、更新滑动速率、能量；
6. **人类消息 → `consecutive_bot_messages = 0`**，结算回应窗口并递增 `state_revision`；
7. 被 @ / wake → 标记、提升能量，然后 **return（交默认 agent，插件不回复）**；
8. 私聊（本版不处理）→ return；
9. 太短 → return；
10. 该群已有 pending → return；
11. Signal Gate 不过 → return；
12. `probability = clamp(base_probability × SpeakScore, 0, 0.8)`，随机决定；
13. 在锁内创建发送 reservation（记录 `task_id` 与当前 `state_revision`）后起异步任务；回调按 task identity 清理。

### 9.2 Signal Gate

```python
def should_consider(state, now) -> bool:
    if umo not in allowlist or not group_enabled: return False   # 默认关闭
    if now_utc < state.next_speak_at_utc: return False           # 分档冷却
    if state.rate_30s > incoming_limit: return False             # 刷屏（独立计数）
    if state.consecutive_bot_messages >= 1: return False
    if not within_active_hours(now): return False                # 夜间静默
    if state.proactive_sent_today >= daily_cap: return False
    return True
```

### 9.3 SpeakScore

```
SpeakScore =
    TopicInterest × ActivityFactor × SocialEnergy
  × ConversationFit × RandomFactor × CooldownFactor
```

```python
score = 1.0
score *= topic_interest          # 命中关键词 ×(1+0.7n) 上限 high_interest_bonus；否则 ×0.35
score *= activity_factor         # 高速 ×0.35；中速 ×0.7；冷群 ×1.2
score *= max(social_energy, 0.15)
if last_bot_followup == "silent": score *= 0.35
elif last_bot_followup == "unrelated": score *= 0.85
if consecutive_bot_messages: score *= 0.3
score = clamp(score, 0, 3.0)
```

> P2-5：明确口径——`TopicInterest` 只算一次，`base_probability` 不再隐含兴趣。

### 9.4 LLM 决策

输入：决策 Prompt（与人格分离）+ 最近群聊（不可信数据段）+ 状态摘要 + 显式“当前触发消息”。

输出强制 JSON：

```json
{ "action": "IGNORE|WAIT|SPEAK", "reason": "...", "topic": "...", "confidence": 0.0 }
```

解析：括号平衡扫描 → `action` 白名单 → 字段/长度校验 → 失败降级 `IGNORE`。V1 固定为两阶段：决策器**不**生成 `reply`；只有 `SPEAK` 后才调用 Reply LLM，避免“是否说”和“说什么”职责冲突。若未来为节省成本合并调用，必须同时删除独立 Reply 层与相应重试逻辑。

### 9.5 延迟发送 + 复检

```
自然插话     2~8s
思考/观察    5~15s
```

复检：冷却是否被重置、话题是否变化、是否被 @（被 @ 则取消）、是否突然刷屏、allowlist/全局开关是否仍开启，以及 task 捕获的 `state_revision` 是否仍为当前值。任一不满足 → 取消。发送前在锁内再次校验并取得唯一 reservation；锁外不得持有锁执行 LLM 调用、`sleep` 或网络发送。

### 9.6 回应窗口（被忽略检测）

```
主动发送 → awaiting_reply_until = now + reply_window(90~180s)
  引用 bot 消息、@ bot、直接回复（平台支持时） → `engaged`
  窗口内同话题的后续讨论                 → `related`
  窗口内仅不相关的人类消息               → `unrelated`（中性）
  窗口超时且无任何群消息                 → `silent`
```

不要将“有人类消息”笼统解释为“机器人没有被忽略”。若平台不能提供回复/引用关系，只能将信号标记为 `related`、`unrelated` 或 `silent`，不得作出强互动判断。

### 9.7 分档冷却

| 场景 | 冷却（随机） |
|---|---|
| 普通主动发言 | 15~30 min |
| 刚参与过讨论 | 5~15 min |
| 连续主动发言 | 30~60 min |
| 上次完全静默 | 30~90 min |
| 被 @ | 不占用主动冷却（且提升能量） |

---

## 10. 配置项草案（`_conf_schema.json`）

| key | type | default | 说明 |
|---|---|---|---|
| `enabled` | bool | true | 全局开关 |
| `dry_run` | bool | false | 只记日志不发送 |
| `group_allowlist` | list | **[]** | **默认关闭**；空 = 不启用任何群；每项为 UMO |
| `min_message_length` | int | 2 | 太短不触发 |
| `context_message_limit` | int | 20 | 上下文条数 |
| `incoming_rate_limit` | int | 30 | 30s 超限视为刷屏 |
| `min_cooldown_seconds` | int | 900 | 冷却下限 |
| `max_cooldown_seconds` | int | 1800 | 冷却上限 |
| `reply_window_seconds` | int | 120 | 回应窗口 |
| `base_speak_probability` | float | 0.08 | 主动基础概率 |
| `high_interest_bonus` | float | 1.8 | 高兴趣倍率 |
| `energy_initial` | float | 0.6 | 初始社交能量 |
| `energy_max` | float | 1.0 | 能量上限 |
| `daily_proactive_cap` | int | 20 | 每群每日主动上限 |
| `global_daily_proactive_cap` | int | 100 | 所有群合计每日主动上限 |
| `group_token_bucket_capacity` | int | 3 | 每群短时突发上限 |
| `group_token_refill_seconds` | int | 900 | 每群恢复一个主动发送令牌的时间 |
| `global_token_bucket_capacity` | int | 10 | 全局短时突发上限 |
| `global_token_refill_seconds` | int | 300 | 全局恢复一个令牌的时间 |
| `send_failure_backoff_seconds` | int | 300 | 平台发送或 Provider 失败后的初始退避 |
| `max_failure_backoff_seconds` | int | 3600 | 失败退避上限 |
| `timezone` | string | "Asia/Shanghai" | 每日配额、夜间静默和跨日重置使用的 IANA 时区 |
| `active_hours` | string | "08:00-23:59" | 静默时段外不主动 |
| `provider_id` | select | "" | 留空用当前会话模型 |
| `interest_keywords` | text | 游戏/副本/Boss/…/笑死 | 每行一个 |
| `negative_keywords` | text | "" | 命中直接放弃 |
| `group_keyword_overrides` | object | {} | 每群自定义关键词 |
| `output_blocklist` | text | "" | 回复禁止词/链接 |
| `use_astrbot_memory` | bool | true | 读 AstrBot 会话历史做上下文 |
| `memory_writeback` | bool | true | 主动消息回写会话记忆 |
| `inject_group_context` | bool | true | 用 `on_llm_request` 注入群情 |
| `max_context_characters` | int | 6000 | 发送给决策/回复 LLM 的不可信群聊最大字符数 |
| `audit_log_retention_days` | int | 14 | 脱敏决策日志保留天数；`0` 表示关闭落盘日志 |
| `persona_prompt` | text | 鲸鱼娘人格 | 说话风格 |
| `decision_prompt` | text | 内置 | 决策器 Prompt |

> 无 `takeover_mode`（决策 1）；无“无上下文自言自语”开关（决策 5）；无身份披露相关项（决策 4）。令牌桶与每日配额同时生效；发送失败不消耗“已成功主动发言”的每日计数，也不得进行记忆回写。

---

## 11. AstrBot 4.x API 事实（已核实）

来源：官方文档 `dev/star/guides/{ai,send-message,listen-message-event}` 与 `use/proactive-agent`。

| 用途 | 正确写法 | 版本/备注 |
|---|---|---|
| 事件过滤 | `@filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)` | 枚举为 `PRIVATE_MESSAGE/GROUP_MESSAGE`；示例里的 `ALL` 需核实 |
| 文本 | `event.message_str` | ✔ |
| UMO | `event.unified_msg_origin` | ✔，状态键与发送都用它 |
| 发送者 | `event.get_sender_id()` / `event.get_sender_name()` | ✔ |
| 群/消息 ID | `event.message_obj.group_id` / `event.message_obj.message_id` | ✔ |
| 机器人自身 ID | `event.message_obj.self_id` | 用于判断是否 bot 消息 |
| 停止传播 | `event.stop_event()` | ✔（**本插件禁用**，决策 1） |
| 调用 LLM | `await self.context.get_current_chat_provider_id(umo=umo)` 再 `context.llm_generate(chat_provider_id=pid, prompt=...)` → `.completion_text` | v4.5.7+ |
| 主动发送 | `from astrbot.api.event import MessageChain`；`await context.send_message(umo, MessageChain().message(text))` | ✔（**v1 的 `astrbot.api.message` 导入是错的**） |
| 当前 Provider | `context.get_current_chat_provider_id(umo=umo)` | v4.5.7+，替代 `get_all_providers()[0]` |
| 读记忆 | `context.conversation_manager.get_curr_conversation_id(uid)` / `get_conversation(uid, cid)` | ✔ |
| 写记忆 | `conv_mgr.add_message_pair(cid=, user_message=UserMessageSegment(...), assistant_message=AssistantMessageSegment(...))` | `from astrbot.core.agent.message import ...` |
| 覆盖历史 | `conv_mgr.update_conversation(uid, cid, history=[...])` | 用于 assistant-only 追加 |
| 注入群情 | `@filter.on_llm_request()` → `req.extra_user_content_parts.append(TextPart(text=...))` | `.mark_as_temp()` 需 v4.24.0+ |
| 管理命令 | `@filter.command_group("ws")` + `@filter.permission_type(filter.PermissionType.ADMIN)` | ✔ |
| 生命周期 | `@filter.on_astrbot_loaded()` | v3.4.34+ |

**相关既有能力（避免重复造轮子 / 需明确边界）**

- AstrBot 内置**主动型 Agent**（Proactive Agent，v4.14.0，实验性）：基于 Cron 的“未来任务”，是“定时主动执行任务”，**不是**群聊氛围参与，与本插件正交；本插件不依赖它。
- 社区插件 `Zxin-Pro/astrbot_plugin_proactive_chat`：目标高度重合（沉默检测/上下文感知/欲望驱动/冷却/多会话隔离），其 README 已按 master 源码核对 API。实现时应对照它，并明确互斥建议，避免双重主动发言。

---

## 12. 里程碑（修正）

### V1（含全部 P0 修正 + 5 项决策）

- [ ] 事件监听（GROUP_MESSAGE，只观察不回复、不 stop_event）
- [ ] 状态键 = UMO；白名单**默认关闭**
- [ ] 入窗 / 去重 / 非文本归一 / 独立滑动速率
- [ ] 分档冷却 + `next_speak_at_utc` 持久化
- [ ] 关键词兴趣（含负向词）+ SpeakScore
- [ ] `IGNORE/WAIT/SPEAK` + JSON 容错
- [ ] 延迟发送 + 复检
- [ ] 回应窗口 / 被忽略记忆
- [ ] 主动发送 + **记忆回写（决策 2）**
- [ ] 原子持久化 + 静默时段 + 每日上限
- [ ] UTC 冷却截止时间 + 单实例运行边界
- [ ] revision/reservation 机制，保证延迟任务不能在状态变化后误发
- [ ] 发送成功才记忆回写；目标平台 CID 兼容性验收

### V1.1

- [ ] 拆 `core/ + storage/`，含 `memory.py`
- [ ] `on_llm_request` 注入群情（`mark_as_temp` 特性探测）
- [ ] Provider 下拉 + 当前会话 Provider
- [ ] 管理命令 `/ws status|enable|disable|reset|why`
- [ ] 结构化决策日志 + `dry_run`
- [ ] 输出过滤 / prompt-injection 防护
- [ ] 全局/每群令牌桶、失败退避与跨时区日配额重置
- [ ] 影子模式指标与 7~14 天灰度验收

### V2

- [ ] 话题 embedding / 小分类器
- [ ] 完整 `socialEnergy` + 每日性格
- [ ] 群画像 / 用户画像 / 话题记忆
- [ ] 每群不同人格
- [ ] 与其它主动插件互斥协调

---

## 13. 待办 Checklist

- [ ] 确认本地 AstrBot ≥ 4.5.7（记忆/新 API），并特性探测 4.24.0
- [ ] 骨架：`metadata.yaml` + `_conf_schema.json` + `main.py`
- [ ] `GroupState` + 持久化（锁/原子写/版本）
- [ ] Collector / Gate / Scorer / Decision / Reply / Memory
- [ ] 回应窗口 + 分档冷却
- [ ] 管理命令 + 审计日志 + dry_run
- [ ] 单测 + 场景验收
- [ ] 拆分 `core/ + storage/`
- [ ] V2 话题分类与画像

---

## 14. 测试与验收

**单元测试**

- SpeakScore：普通闲聊 ≈ 0.01；高兴趣显著更高；负向词直接 0；
- 冷却：`next_speak_at_utc` 前不放行；进程重启后依然有效，且不依赖 `monotonic()` 的旧参考点；
- **连续发言复位**：bot 发言→人类发言→计数归零；
- 回应窗口：明确回复/@ 记为 `engaged`；相关后续为 `related`；无关消息为中性 `unrelated`；完全静默为 `silent`；
- 滑动速率：窗口长度变化不影响刷屏判定（P0-4 回归）；
- JSON 解析：包裹/噪声/缺字段/多对象均安全降级；
- 原子写：写入异常不破坏旧文件；版本不匹配安全重置；
- 并发：同群多消息不重复 pending / 不误删任务；
- 并发：任务在延迟、LLM 调用、发送前的任一阶段遇到 revision 变化都会取消；同一 revision 只能取得一次发送 reservation；
- 记忆：回写失败不影响发送；`mark_as_temp` 不可用时降级。
- 记忆：仅发送成功后回写；在真实 UMO/Custom Rules 下验证主动回写与后续 @ 回复使用同一 CID。
- 流控：全局与每群令牌桶、每日配额、失败指数退避和跨日时区重置均可预测地生效。

**场景验收**

| 场景 | 期望 |
|---|---|
| 未在白名单的群 | 完全不动作（默认关闭） |
| 普通闲聊 | 潜水 |
| 游戏话题 | 观察后自然插一句 |
| 群刷屏 | 潜水（该分支可达） |
| 被 @ | **由默认 agent 回复一次**，插件不重复 |
| 上一句没人理 | 明显降低再次主动概率 |
| 延迟期间换话题 | 取消发送 |
| 延迟期间被 @ | 取消主动发送，让默认 agent 回 |
| 主动发言后正常 @ | 默认 agent 的上下文里**能看到**刚才的主动消息 |
| 夜间/超每日上限 | 不主动 |
| dry_run | 只记日志不发送 |
| 影子模式连续 7~14 天 | 记录候选、取消原因、预测频率和成本；不发送消息 |
| 发送失败 | 不回写记忆、不计入成功日配额，并进入有限指数退避 |
| Custom Rules 改变会话模型/人格 | 仍使用实际 UMO 的当前 Provider，且验证回写 CID 一致 |

---

## 15. 安全、合规与运营

- **提示注入**：群聊是数据不是指令；以 `<untrusted_chat>` 明确隔离，规范化控制字符/@/URL，限制每条和总上下文长度；决策与回复 Prompt 均明确不得执行聊天内容中的指令。输出过滤链接/命令/敏感词。
- **平台风控**：全局及每群令牌桶 + 每日上限 + 夜间静默 + 有上限的指数退避；发送成功才消耗成功配额与触发记忆回写。
- **运营前提**：该群必须为“被 @ 才回复”模式，否则默认 agent 与插件抢发。
- **隐私**：只存必要群状态；`/ws reset` 可清除；消息窗口不持久化。审计日志只存脱敏的决策摘要，不存原始群聊；按 `audit_log_retention_days` 自动清理。不得提交 UMO、真实群数据或凭据。
- **可观测**：每次决策记录脱敏 score/概率/取消原因/发送结果；按群聚合主动发送数、候选→发送转化率、发送后互动信号、LLM 调用数和估算成本。
- **灰度**：新群先以 `dry_run`/影子模式运行 7~14 天；评估频率、取消率和成本后再由管理员显式启用。建议首周设更低的每群成功发送上限（如 3 条/日）。
- **降级**：Provider 缺失 / LLM 失败 / 记忆写失败 → 安静失败，不影响 AstrBot 默认功能。
- **身份披露**：按决策 4 不做主动披露；平台与法规责任由运营方自行评估。

---

## 16. 结论

最关键的不是公式，而是让机器人拥有**克制**：

> 大多数时候潜水，偶尔参与；有兴趣才说话；说完不会连续刷屏；没人接话时会自然沉默。

本插件**只做主动层**，与 AstrBot 默认的被动回复管线分工明确；主动消息仅在发送成功后尝试写回会话记忆。只有通过目标平台/CID 兼容性验收时，才承诺后续默认对话能看到这段主动上下文。
