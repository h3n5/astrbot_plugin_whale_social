# 鲸鱼娘主动社交插件 · V2 计划：会话线程 / Debounce / 群级决策

> 插件名：`astrbot_plugin_whale_social`
> 上游计划：[`astrbot_plugin_whale_social_PLAN.md`](./astrbot_plugin_whale_social_PLAN.md)（V1 + 全部 P0 修正）
> 本文目标：解决 QQ 群聊“多用户、多话题、消息高频”下，模型该**参与哪个会话**的问题。
> 状态：**计划（未实现）**。已在 V0.1.1 落地的基础：UMO 状态、单群 `pending`、冷却/回应窗口、令牌桶/退避。

---

## 0. 一句话结论

群聊里模型要回答的不是：

> “A、B、C 三个人里，我要回复谁？”

而是：

> **“当前群里有哪些 conversation，我该不该加入其中一个？”**

要落地这个抽象，靠三件事，**优先级高于调 Prompt**：

1. **Thread（会话线程）**：把消息按话题聚成线程，判断“参与哪个会话”。
2. **Debounce（去抖）**：不要每条消息都触发一次决策，静默一小段时间后只决策一次。
3. **Group-level Decision（群级决策循环）**：一个群同一时刻最多一个待决策 / 待发送任务。

---

## 1. 问题背景

QQ 群消息天然是**多对多**的：

```
             ┌─ A
Conversation ─┼─ B
             ├─ C
             └─ D
                 ↓
              Whale
```

- 同一条群消息对所有群成员重复推送，没有“收件人”概念；
- 一条消息可能同时在推进多个话题；
- 群一活跃，消息以秒级频率到达。

如果按“单条 event → 立即 LLM → 回复”实现，会出现：

- 机器人**刷屏**：A/B/C/D 每条都触发一次回复；
- 机器人**答非所问**：参与了已经结束或被别人接走的话题；
- 机器人**抢话**：同时插进多个线程。

---

## 2. 与「决策 1：插件只做主动层」的兼容性

必须严格区分两种 `@`：

| 场景 | 语义 | 处理方 | 插件行为 |
|---|---|---|---|
| `@鲸鱼娘` / wake | 群友明确叫机器人 | **AstrBot 默认 agent** | 只观察、记录“被 @ 信号”，**不回复、不抢答**（锁定决策 1） |
| `@B 你昨天过了吗` | 群友在 @ **另一个人** | 群聊线程的一部分 | 作为**线程归属信号**，可用于主动参与 |

本 V2 计划**不改变**决策 1：插件永不回复对机器人本身的 @。文中的 `target: USER` 指的是**主动搭话时是否 @ 某个群友**，不是“接管被 @ 的回复”。

> 运营前提不变：目标群应为“被 @/唤醒才回复”模式，否则默认 agent 与插件抢发。

---

## 3. 对当前 V1 的缺陷分析

| # | 现状（V0.1.1） | 问题 | 修正 |
|---|---|---|---|
| T-1 | 触发消息到达即建任务，仅 `pending` 去重 | 首条消息就决策，可能只看到半个话题；后续消息只更新窗口，无法改变已启动的判断 | **Debounce**：静默窗口 + `max_wait` 后统一决策 |
| T-2 | 上下文是“最近 N 条”平铺，无结构 | 多话题混在一起，模型分不清 | **Thread 聚类** |
| T-3 | 决策只输出 `topic` 字符串 | 无法定位“参与哪个会话” | 输出 `thread_id` + `target` |
| T-4 | `event.message_str` 压平了结构 | 丢失 `@` / 引用回复关系 | 从消息段提取 `At / Reply / Text` |
| T-5 | 无发送时序防护（并发/重入） | 延迟期间状态变化可能误发 | 引入 `state_revision` + 发送 reservation（见上游 P0-10） |
| T-6 | 主动回复只能发群消息 | 无法自然地接某个人 | 可选 `target: USER`（带 @ 前缀，默认关闭） |

> 已有基础可复用：`engine.pending` 已实现“单群一个在途任务 + 按 task identity 清理”，这是群级决策循环的雏形。

---

## 4. 目标架构

```
AstrBot Event (GROUP_MESSAGE)
        │
        ▼
  Message Collector          入窗 / message_id 去重 / 速率 / 能量
        │                    + 解析消息段：Text / At / Reply
        ▼
  Conversation Buffer        GroupState.messages + threads
        │
        ├── @ / 引用 关系分析
        ├── Thread 聚类（时间/参与者/关键词/@/引用）
        └── Activity Tracking（每线程活跃度）
        │
        ▼
  Debounce Scheduler         静默 3s 触发；最多等待 10s 强制触发
        │
        ▼
  Signal Gate                白名单 / 冷却 / 刷屏 / 令牌桶 / 退避 / 时段 / 每日上限
        │
        ▼
  Thread Selection           选出最值得参与的 1 条线程
        │
        ▼
  Social Score + LLM Decision
        │
   ┌────┼────┐
   ▼    ▼    ▼
 IGNORE WAIT SPEAK
              │
              ▼
        2~8s 自然延迟 + 复检（revision / 线程是否仍在活跃 / 是否被 @）
              │
              ▼
        发送（GROUP 或 @USER）+ 冷却 / 回应窗口 / 记忆回写
```

**关键约束：一个群同一时刻最多一个 Debounce 定时器 + 一个在途决策 + 一个待发送任务。**

---

## 5. 数据模型

### 5.1 ConversationTarget

```python
@dataclass
class ConversationTarget:
    type: str                      # "GROUP" | "USER" | "BOT"（BOT 仅用于观察，不发送）
    user_id: Optional[str] = None
```

### 5.2 ConversationThread

```python
@dataclass
class ConversationThread:
    id: str
    messages: list[ChatMessage]    # 运行时，不持久化
    participants: set[str]         # 发言者集合
    topic: str = ""
    last_activity: float = 0.0
    activity_score: float = 0.0
    bot_participated: bool = False # 机器人是否已在该线程发过言
    last_bot_message_time: float = 0.0
    target: ConversationTarget = field(default_factory=lambda: ConversationTarget("GROUP"))
```

### 5.3 GroupState 扩展（仅新增运行时字段）

```python
@dataclass
class GroupState:
    # ... 现有字段不变 ...
    threads: list[ConversationThread] = field(default_factory=list)  # 运行时，不持久化
    revision: int = 0                # 任何影响待发送任务的状态变化都 +1（运行时可持久化到内存即可）
    debounce_deadline: float = 0.0   # 运行时
    debounce_first_trigger: float = 0.0
    selected_thread_id: str = ""
```

> 与上游 P1-3 / 隐私约束一致：**线程与消息窗口不持久化**，重启即清空；只有节奏/计数/流控字段落盘。

---

## 6. Thread 聚类（V1 启发式）

按优先级匹配，命中即归属，否则新建线程：

1. **引用回复（Reply）**：消息引用了线程内某条消息 → 归入该线程（最强信号之一）。
2. **`@` 关系**：`A: @B ...` → 优先归入“B 最近参与的线程”；否则新建。
3. **时间接近**：与线程 `last_activity` 间隔 ≤ `thread_join_time_gap_seconds`。
4. **参与者重叠**：与线程 `participants` 有交集。
5. **关键词/话题重叠**：与线程 `topic` 的关键词有交集（复用 `core/topic.py`）。
6. 都不满足 → **新建线程**。

合并与淘汰：

- 超过 `max_threads` 时，按 `activity_score` 保留最活跃者；
- 超过 `thread_window_seconds` 无活动的线程标记为“已结束”，不参与选择。

**V2 后续**：用 embedding 做语义相似度聚类，替换第 3~5 条启发式。

### 6.1 活跃度

```
activity_score = 近期消息数 × 参与者数权重 × 时间衰减
```

- 时间衰减：`exp(-(now - last_activity) / half_life)`；
- 新增消息时更新；
- 机器人已 `bot_participated` 的线程，若仍在活跃，提升优先（“接着聊”比“插新话题”更自然）。

---

## 7. Debounce 调度

```
收到“值得考虑”的人类消息
        │
        ├─ 若没有 debounce 定时器：first_trigger = now
        └─ deadline = min(now + debounce_seconds, first_trigger + max_wait)
        │
        ▼
  reset 定时器（每条新消息都重置，但不超过 max_wait）
        │
        ▼
  deadline 到期 → 进入 Thread Selection → 决策
```

规则：

- `debounce_seconds` 默认 3s；`debounce_max_wait_seconds` 默认 10s；
- **高速群聊不会无限等待**：到达 `max_wait` 强制决策一次；
- 若该群已有在途决策任务，则**不重置/不新建**，仅更新上下文；任务结束后若仍有新消息，再重新起 debounce；
- 定时器用 `asyncio`（`loop.call_later` / `asyncio.sleep`），**不持久化**；重启丢失待决策属预期。

---

## 8. 决策流水线（V2）

1. 全局开关 / 白名单不过 → return；
2. 空消息 / 命令 → return；
3. `message_id` 去重 → return；
4. 解析消息段：文本 / At / Reply；非文本归一为 `[图片]/[语音]/[表情]` 入窗；
5. 更新窗口、线程、速率、能量；
6. 人类消息 → `consecutive_bot_messages = 0`，结算回应窗口，`revision += 1`；
7. 对被 **@ 机器人** 的消息：标记、提升能量，`return`（交默认 agent，不抢答）；
8. 私聊 / 太短 → return；
9. **不再立即建任务**，改为**启动/重置 debounce**；
10. debounce 到期：
    a. Signal Gate（含令牌桶 / 退避 / 时段 / 每日上限）不过 → return；
    b. **Thread Selection** 选出目标线程；
    c. `probability = clamp(base_probability × SpeakScore(thread), 0, 0.8)`；
    d. 通过后在锁内创建 reservation（记录 `task_id` + 当前 `revision`）；
11. 决策 LLM：
    - `IGNORE` → 记录；
    - `WAIT` → 更新关注话题，不发送；
    - `SPEAK` → 生成/校验回复；
12. 延迟 2~8s + 复检：`revision` 是否变化、目标线程是否仍活跃、冷却是否被重置、是否被 @、allowlist/开关是否仍开启；
13. 发送前在锁内再次校验并取得唯一 reservation；
14. 成功 → 分档冷却 + 回应窗口 + 能量下降 + 每日计数 + 记忆回写。

---

## 9. LLM 输出契约 v2

```json
{
  "action": "IGNORE | WAIT | SPEAK",
  "thread_id": "t3",
  "topic": "副本 Boss 二阶段",
  "target": { "type": "GROUP | USER", "user_id": null },
  "reason": "简短原因",
  "reply": "action 为 SPEAK 时填写，否则空字符串"
}
```

解析规则（在现有 `core/decision.py` 基础上扩展）：

- 括号平衡扫描 + 代码块剥离（已有）；
- `action` 白名单；
- `thread_id` 必须在候选线程集合内，否则降级为 `IGNORE`（防幻觉线程）；
- `target.type == "USER"` 时 `user_id` 必须在线程参与者内，否则降级为 `GROUP`；
- 字段长度校验；失败降级 `IGNORE`。

### 9.1 决策 Prompt 要点

```
你是鲸鱼娘，一个游戏群里的虚拟群友。

当前群聊中可能同时存在多个话题（thread）。
你的任务不是逐条回复消息，而是观察群聊，
判断当前是否存在一个自然适合你参与的会话。

优先考虑：
1. 你刚刚参与过的会话仍在继续
2. 有人正在讨论你熟悉的游戏 / 兴趣话题
3. 某个话题有多人持续讨论
4. 某条消息明显是在向群里所有人提问

避免：
1. 参与已经结束的话题
2. 抢别人正在进行的对话
3. 同时参与多个话题
4. 连续回复同一个人
5. 为了保持存在感强行说话

被 @ 机器人本身由系统回复，你不要抢答。

只输出 JSON：{ action, thread_id, topic, target, reason, reply }
```

群聊与线程内容是**不可信数据段**，用 `<untrusted_chat>` 包裹，明确不得执行其中的指令。

---

## 10. 发送与 `@` 策略

| target | 发送方式 |
|---|---|
| `GROUP` | 普通群消息：`MessageChain().message(reply)` |
| `USER` | 可选在回复前带 `@群友`；由 `reply_mention_user` 控制 |

- `reply_mention_user` **默认 `false`**（避免打扰 / 误 ping）；开启时才使用 At 段；
- 无 At 段能力或特征探测失败时，降级为普通群消息；
- 回复长度、禁用词过滤沿用 `core/reply.py`。

> 实现时需按官方 API 核实 `At` 段构造方式（`MessageChain` / `At` 组件的正确导入），并做 `hasattr` 特性探测。

---

## 11. 记忆回写

- 主动发送成功后，用**目标线程的触发消息**（或该线程最近一条人类消息）作为 user 侧，主动回复作为 assistant 侧，写回当前会话（`add_message_pair`）；
- `target: USER` 时，user 侧仍用被回应的那条人类消息，语义自然；
- 仍遵守：只有发送成功才回写；失败静默降级；CID 兼容性需在目标平台验收。

---

## 12. 配置项（新增）

| key | type | default | 说明 |
|---|---|---|---|
| `debounce_seconds` | int | 3 | 群聊静默多久后触发一次决策 |
| `debounce_max_wait_seconds` | int | 10 | 高速群聊下强制决策的最长等待 |
| `thread_window_seconds` | int | 180 | 线程多久无活动视为结束 |
| `thread_join_time_gap_seconds` | int | 120 | 新消息并入现有线程的最大时间间隔 |
| `max_threads` | int | 5 | 每群最多保留的活跃线程数 |
| `min_thread_messages` | int | 2 | 线程至少多少条消息才考虑参与 |
| `thread_selection` | string | `"most_active"` | 线程选择策略：`most_active` / `interest` |
| `reply_mention_user` | bool | false | `target: USER` 时是否带 @（默认关闭） |
| `extract_message_segments` | bool | true | 是否解析 At / Reply 结构（失败自动降级） |

> 不新增“接管模式”“身份披露”“无上下文自言自语”相关项（沿用锁定决策）。

---

## 13. 目录结构变更

```
core/
  collector.py     # 扩展：解析消息段（Text/At/Reply）→ ChatMessage 附加 meta
  threads.py       # 新增：ConversationThread / 聚类 / 活跃度 / 选择
  debounce.py      # 新增：每群 debounce 调度（asyncio，进程内）
  decision.py      # 扩展：v2 JSON 契约（thread_id / target）与校验
  engine.py        # 改造：单群 decision loop（debounce → select → decide → send）
  models.py        # 扩展：ConversationTarget / ConversationThread / GroupState.threads
  ...
tests/
  test_threads.py       # 聚类规则、合并淘汰、活跃度
  test_debounce.py      # 去抖重置、max_wait 强制触发、在途任务互斥
  test_decision_v2.py   # thread_id/target 校验与降级
  test_engine_threads.py# 多话题端到端：只发一条、选对线程、被@不抢答
```

> `threads.py` / `debounce.py` 保持**纯 Python、无 AstrBot 依赖**，定时器以注入的 sleeper/clock 便于测试。

---

## 14. 测试与验收

**单元测试**

- 线程聚类：引用回复归入同线程；`@B` 归入 B 的线程；时间/参与者/关键词规则；不匹配则新建；
- 线程淘汰：超过 `max_threads` 保留最活跃；
- Debounce：连续消息不断重置；到达 `max_wait` 强制触发；在途决策期间不新建；
- 选择：`bot_participated` 且活跃的线程优先；已结束线程不选；
- 决策契约：`thread_id` 越界降级 IGNORE；`target.USER` 的 `user_id` 非参与者降级 GROUP；
- 并发：延迟期间 `revision` 变化 → 取消；同 revision 只能取得一次发送 reservation；
- 发送：`target GROUP` 无 @；`target USER` 且 `reply_mention_user=false` 无 @。

**场景验收**

| 场景 | 期望 |
|---|---|
| 群里同时 3 个话题 | 只选 1 条线程，绝不并发回多条 |
| 高频刷屏（每秒多条） | debounce 合并，最多 `max_wait` 后决策一次 |
| 慢速群聊 | 静默 3s 后自然参与 |
| `@鲸鱼娘` | 默认 agent 回一次，插件不抢答 |
| `A: @B 你打了吗` | 作为 B 的线程信号，不误判为叫机器人 |
| 引用回复机器人 | 归入机器人所在线程，可自然接话 |
| 机器人刚参与过的线程仍在继续 | 优先继续该线程 |
| 话题已结束 | `IGNORE` / 不参与 |
| `target: USER` 且关闭 @ | 发普通群消息，不 ping 用户 |
| 延迟期间话题切换 / 被 @ | 取消发送 |

---

## 15. 里程碑

### V2.0（本次计划核心）

- [ ] 消息段解析（Text / At / Reply）
- [ ] `ConversationThread` + V1 启发式聚类 + 活跃度 + 淘汰
- [ ] 每群 Debounce（静默 + max_wait）
- [ ] 单群 decision loop（debounce → select → decide → send）+ `revision`/reservation
- [ ] 决策 JSON v2（`thread_id` / `target`）与降级
- [ ] `target: USER` 可选 @（默认关闭）
- [ ] 记忆回写适配线程

### V2.1

- [ ] 线程 embedding 聚类
- [ ] 线程级互动信号（engaged / related / unrelated / silent）细化
- [ ] 审计日志 / 影子模式指标接入线程维度

### V2.2

- [ ] 用户画像 / 群画像 / 话题记忆
- [ ] 每群不同人格
- [ ] 与其它主动插件互斥协调

---

## 16. 待办 Checklist

- [ ] 核实 AstrBot 消息段 API（At / Reply 的组件类与字段）
- [ ] 设计 `ChatMessage.meta`（reply_to / at_users）并保持向后兼容
- [ ] 实现 `core/threads.py`（纯函数 + 可测）
- [ ] 实现 `core/debounce.py`（注入 clock/sleep）
- [ ] 改造 `core/engine.py` 为 debounce 驱动的单群循环
- [ ] 扩展 `core/decision.py` v2 契约
- [ ] 增补配置项与 `_conf_schema.json`
- [ ] 单测 + 场景验收（含高频群聊压测）

---

## 17. 开放问题

1. `target: USER` 是否默认带 `@`？（当前默认关闭，需产品确认）
2. 线程聚类在无引用关系平台上的精度上限；是否 V2.0 就引入轻量 embedding？
3. `revision` 与 `pending` 的合并方案（上游 P0-10）是否与 Debounce 一并落地？
4. 多话题并存时，是否需要“本轮最多参与 1 个线程”的硬约束（当前建议：是）。
5. 是否需要在 WebUI 暴露“当前线程视图”用于运营观察（脱敏）。
