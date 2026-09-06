# spec-02 记忆与上下文规格 v0.5（Web 部署版适配：通道与承载表述同步）

> 版本：v0.5（2026-09-06，Web 部署版适配轮·随总纲 v0.20 提交，待用户终审：承载形态由桌面壳 + sidecar 重构为**浏览器前端 + Nginx 反代 + core 常驻（systemd）**——本文件仅做通道与承载表述同步：messages.delivered_via `desktop`→`web`（站内通知 web）、桌面通知兜底→站内通知（web）兜底、Windows/开机等桌面语义改 Linux/进程启动语义、导出/导入"换机迁移"改跨主机迁移；存储/schema/隔离/备份/向量/审计/checkpoint 等机制与决策不变；v0.4 全部内容保留）
> **状态：⏳ Web 部署版适配稿（随总纲 v0.20 一并以 Web 部署版 P1 基线提交，待用户终审定稿）——定稿后 v0.5 替代 v0.4 成为 Web 部署版开发基线（机制同 v0.4）。**
> 依据：总纲 v0.20（Web 部署版，业务决策承 v0.18）§9 记忆系统（9.1-9.5）、§10.1 时效分级、§12.8 决策表；联动 spec-01 v0.7（§2.8 试运行复位、§8.1 exit_trackings）、spec-04 v0.7（任务级执行预算）
> **受约束决策**：#23（成本四条之记忆卡片/分层摘要） #25 #28 #30 #31 #44 #50 #51 #54 #55（知识库统计侧归 spec-05）
> **§16 勾销**：#9 #17 #20 #35（见文末勾销表；#9 存储侧本 spec、流程侧移交 spec-05）
> 与总纲冲突时以总纲为准并同步本文件。

---

## 1. 模块定位与边界

- **职责**：分级记忆的存储与检索（原文 + 调用卡片 + 分层摘要）、每 Agent 独立记忆空间与隔离、上下文装配（Context Assembly）的输入模板与预算、策略版本化存储、会话与消息存储、向量库（中文 embedding）、**审计日志（audit_logs）存储与统一写入口（§5.1，v0.3 归口声明）**、**LangGraph 会话 checkpoint 持久化（§6.1，v0.3 归口声明）**、存储层备份/恢复/重建、性能与费用记录表。
- **不负责**：知识库（正向概念+反向避坑）条目与统计（spec-05）；能力/大纲存储（spec-05）；任务表与调度（spec-04，摘要/备份的**调度执行**归 spec-04，**触发谓词定义**归本 spec §4.1）；引擎业务表（spec-01，本 spec 只维护引擎表之外的库级备份关系）；UI（spec-06）。
- **消费方**：编排层执行器（任务收尾"沉淀"调用）、管理 Agent / 子 Agent 工作流（检索与装配）、spec-05（策略版本晋升/回滚、知识库证据流读取演进记忆与信号统计）、spec-04（性能记录消费做容量/成本审查、月度费用对账报告生成）。
- **信任边界**：#33 通道绑定下，记忆读写均以执行器注入的 agent_id 为命名空间；任何 Agent 无权读写他者命名空间（知识库为管理 Agent 可授权共享的例外，其存储归 spec-05）。
- **归口声明（v0.3 拍板）**：
  - **审计归口**：`audit_logs` 表与统一写入口归本 spec（§5.1）；执行器统一盖章（总纲 #33）为唯一写入方；
  - **checkpoint 归口**：LangGraph checkpointer 持久化介质 = 业务库 SQLite，归本 spec（§6.1）；interrupt/恢复的运行时用法归 spec-04。

## 2. 分级记忆模型（落库实现）

| 记忆类型 | 内容 | 表 | 保存策略 |
|---|---|---|---|
| 用户需求记忆 | 原始表述、需求确认单、用户偏好 | memory_entries(type=user_requirement) | 永久，可修订版本（revision 链） |
| 策略记忆 | 策略理念、每次优化 前/后/依据/预期/结果、失败修改与拒绝原因（#25） | memory_entries(type=strategy) + strategy_versions | 永久追加=演进史 |
| 交易决策记忆 | 每笔买卖理由、市场判断、卖出跟踪验证结果 | memory_entries(type=trade_decision) | 永久追加 |
| 市场观察记忆 | 市场状态/板块热点主观记录 | memory_entries(type=market_note) | **滚动类（唯一例外，见下）**：近详远略，保留期可配 |
| 会话/工作记忆 | 任务上下文与产出 | conversations/messages + LangGraph checkpoint（§6.1）+ 任务收尾沉淀 | 短期，任务结束沉淀后转长期 |

- **append-only 范围（v0.3 拍板方案 A，消除与滚动删除的矛盾）**：
  - **永久类**（用户需求/策略/交易决策/会话沉淀出的长期记忆）：只追加、不物理删除（软删 + 撤销可见）；
  - **市场观察为滚动类例外**：远期原文可物理删除——前置条件 = 已完成对应分层摘要且管理 Agent 抽检通过；删除动作全审计；**级联清理见 §4.3**（向量+卡片同步清理）；保留期可配（默认值入 config，P1 确认，建议 180 自然日）。
  - 总纲 §15.2"记忆默认永久"的例外依据 = §9.1 滚动语义（近详远略），已同步总纲 v0.17.3 括注。
- 清理策略（总纲 §15.2：清理动作需用户确认；市场观察滚动删除为例外、按配置自动执行）。

## 3. 记忆双版本：原文 + 调用卡片（成本原则）

### 3.1 存储（v0.3 补幂等约束与 quality 语义）

- `memory_entries`（原文，append-only）：`id(ULID), agent_id, mem_type, ts, body(全文), ref_ids JSON, revision(可修订类型用), source(task_id|msg_id), dedup_key, quality(normal|flagged)`
  - **幂等约束（v0.3 拍板 B1）**：`UNIQUE(agent_id, dedup_key)`。不用 (source, mem_type) 联合唯一——同一任务合法沉淀多条同类型记忆；由 sediment() 调用方（编排层）为每条待沉淀记忆生成**稳定 dedup_key**（如 `{task_id}:{slot}` 或内容哈希），任务重跑生成相同 key → 冲突即跳过（INSERT OR IGNORE 语义），崩溃恢复重跑零重复；
  - **quality 语义（v0.3 定义）**：默认 `normal`；管理 Agent 抽检发现"卡片与原文不符"时标 `flagged` 并**触发该条卡片重生成**（离线可延迟任务）；原文本身不因 quality 变更（append-only）。
- `memory_cards`：`memory_id(主键), agent_id, card(结构化精炼), card_version, model, created_ts, updated_ts`（**token 预算 200-500 区间内由生成模型自定，不按记忆类型分档**——v0.2 用户拍板）
  - **主键与更新语义（v0.3）**：memory_id 一对一，`PRIMARY KEY(memory_id)`；更新卡片时**原地 upsert**（card_version+1、model/updated_ts 刷新），不插新行；生成幂等：卡片已存在且未 flagged 则跳过；
  - 卡片生成时机：记忆落库后由**离线可延迟任务**（#50 空闲窗口）用强模型生成，或任务收尾"沉淀"时同步生成（可配）；
  - **卡片重生成触发（v0.3 补）**：①quality=flagged（抽检不符）；②原文 revision 变更（可修订类型 user_requirement）——原文不动、只重生成卡片；
  - 卡片为"调用时版本"——原文永不更新。
- `memory_summaries`：**幂等约束（v0.3）** `UNIQUE(agent_id, period, period_key)`——补跑覆盖重算（UPSERT）而非重复插入，重算留痕（created_ts/model 刷新 + audit）。

### 3.2 检索协议（防"全量历史"进上下文，#23；v0.3 补空窗兜底）

```
retrieve(agent_id, query, intent, top_k=5) → list[{card|raw, memory_id, score, mem_type, from_card(BOOL)}]
get_full(agent_id, memory_id) → 原文全文        // Agent 需要精确细节（数字/原话）时才调
```
- 检索命中卡片而非原文；卡片相似度用向量（§8），必要时关键词加权（意图路由）；
- **无卡片空窗兜底（v0.3 拍板 A3）**：卡片生成是异步可延迟任务，存在"已落库、无卡片"窗口，且原文不向量化——近期记忆对向量检索不可见。兜底规则：
  1. **兜底检索**：retrieve() 对近 N 个交易日（config `recent_fallback_days`，默认 5）内**无卡片的原文条目**追加结构化查询（mem_type + 时间窗 + 关键词匹配），命中并入候选，标注 `from_card=false`（调用方知其为原文候选）；
  2. **超时补卡**：无卡片条目滞留超过阈值（config，默认 24 小时）→ 触发即时补卡任务（可延迟组**高优先级**，进程启动后首个空闲窗口执行）；
- **卡片只用于导航**：任何写入策略演进/日报的引用必须回原文核对（事实红线 §6.1 联动）——卡片不得成为决策引用的"事实源"（from_card=false 的原文候选同样须 get_full 核对细节）。

## 4. 分层摘要索引（日→周→月，可延迟无时效任务）

### 4.1 周期口径与触发谓词（v0.3 拍板 B2，新增）

- **周期口径：一律按 A 股交易日历（北京时间，总纲 §10.1 时区约定）**：
  - 日 = 交易日；period_key 格式 `YYYY-MM-DD`；
  - 周 = 交易周（自然周周一~周日内发生的交易日集合）；period_key 格式 `YYYY-Www`（ISO 周号）；
  - 月 = 自然月内的交易日集合；period_key 格式 `YYYY-MM`；
  - 周/月"已结束"定义：该周期内最后一个交易日已收盘（15:00 后）。
- **触发谓词（v0.3 补，供 spec-04 调度器实现缺口检测）**——#50 去时点化后，调度器在空闲窗口扫描以下条件，满足则入可延迟队列：
  - 日摘要：前一交易日已收盘 **且** 当日有沉淀条目 **且** 无对应日摘要；
  - 周摘要：上一交易周已结束 **且** 无周摘要；
  - 月摘要：上一自然月已结束 **且** 月内周摘要齐备（缺周摘要先补周再合成月）；
  - 谓词定义归本 spec，扫描与执行归 spec-04。

### 4.2 生成与合成（v0.2 拍板维持）

- 生成：日摘要以当日沉淀条目为输入；周摘要以本周**原文**为输入重算（不从日摘要文本拼接——防链式误差累积）；**月摘要由本月周摘要合成**——合成时继承周摘要的 record_ids 全集，抽检可回溯任一记录到原文（合成精度损失由"管理 Agent 抽检 + 溯源到原文"兜底）；
- 执行时机：#50 可延迟无时效任务——不绑定固定夜间时点；用强模型；管理 Agent 抽检（抽样比例可配，默认 10%）；
- 上下文只装"近期卡片 + 月摘要（或周摘要，按意图）"，历史细节定向 get_full 回原文。

### 4.3 滚动删除与级联清理（v0.3 拍板方案 A，新增）

- 市场观察远期原文滚动删除（保留期可配）**前置条件**：该期分层摘要已生成 **且** 管理 Agent 抽检通过；
- **删除时级联清理（一次事务内完成 + 审计）**：
  1. 删 chroma 向量（按 metadata `agent_id` + `memory_id` 精确删除）；
  2. 删除 memory_cards 中对应卡片（或重指向该期摘要——实现二选一，P1 定，默认删除）；
  3. 删除 memory_entries 原文行；
  4. 写 audit_logs：被删 memory_id 清单 + 所属摘要 period_key + 抽检凭证引用；
- **溯源降级说明**：原文删除后，摘要 record_ids 中对应条目成为"历史索引"——溯源到删除审计记录（何时/依据何摘要/经何抽检删除），不再能取回原文全文；抽检必须在删除**前**完成即为此兜底；
- 永久类记忆无滚动删除（§2）。

## 5. 独立记忆空间与隔离

- 命名空间 = `agent_id`；管理 Agent 一套、每个子 Agent 一套；库表均带 agent_id 列 + 应用层强制过滤（双保险：SQL 过滤 + 检索 API 断言）；
- 隔离测试为必测项（§12）：Agent A 无法经任何 API 检索/读取 B 的条目（含卡片与摘要）；
- 例外：知识库（spec-05，管理 Agent 授权共享）；归档 Agent 记忆转为只读命名空间（§3.5 归档），管理 Agent 可读（经验提取 #31 消费）；
- **归档清理（v0.2 用户拍板）**：归档 Agent 记忆保留期**可配**（默认永久；若启用限期清理，清理前必须完成分层摘要与管理 Agent 抽检，清理记录审计——级联清理同 §4.3）；
- 向量检索的隔离：单 collection 下由统一检索 API 强制注入 `where(agent_id=…)`（§8），与关系库双保险。

### 5.1 审计日志（v0.3 归口声明，新增）

```
audit_logs: id(ULID), ts, actor_type(executor|agent|user|system), agent_id,
  action, target, result(allowed|denied|executed|failed), detail JSON
```

- **归口**：schema 与统一写入口归本 spec；**唯一写入方 = 编排层执行器**（总纲 #33 统一盖章：who/what/when/against/result）；
- 必落审计清单：隔离断言拒绝、跨 Agent 授权查询、白名单拦截转审阅、归档/滚动清理（§4.3）、策略 checkpoint/晋升/回滚（§9）、备份与还原、向量重建与切换、context_templates 演进（§7.0）、**账户复位与试运行归档（spec-01 §2.8，action=account_reset/trial_archive，v0.4）**；
- 完整字段与索引 P1 蓝图定稿，本表为最小集。

## 6. 会话与消息存储（联系人式会话，#28/#31 关联）

### 6.1 LangGraph checkpoint 归口（v0.3 拍板 C6，新增）

- checkpointer 持久化介质 = **业务库 SQLite**（与业务表同库，随每日备份与还原流程走，还原后 checkpoint 随库恢复）；
- checkpointer 表由 LangGraph SQLite saver 生成，**存储归口本 spec**；interrupt/审批超时（#58）/会话恢复的运行时用法归 spec-04。
- **checkpoint 保留策略（v0.4 补）**：checkpoint 随运行持续增长——按 thread（agent+task）保留**最近 K=10 个 checkpoint**（config 可配），图版本升级后的旧 checkpoint 一并清理；清理入可延迟组、留审计；**interrupt 挂起中的 thread 不清理**（恢复依赖）。

### 6.2 会话与消息（v0.3 补消息状态机）

- `conversations`：`id, agent_id(子Agent|manager), conv_type(user_chat|report_direct|sync), created_ts`
- `messages`：`id, conv_id, msg_type(日报/异常上报/审批回执/提问回复/总汇报/系统事件), direction(user→agent|agent→user), body, payload_ref, delivered_via(web|mail|clawbot|wecom), sync_to_manager BOOL, status(delivered|pending_review|failed), delivery_attempts INT, last_error, read_ts, ts`
  - **status 语义（v0.3 拍板 C3）**：
    - `pending_review`：白名单拦截落点——执行器拦截非白名单 msg_type 的 user 直达投递 → 标 pending_review + 入"待管理 Agent 审阅"队列 + audit；审阅放行后转 delivered；
    - `failed`：通道推送失败（delivery_attempts+1、last_error 记录）；补偿路径 = 站内通知（web）兜底（通道限额聚合归 spec-04，#36）；
    - `delivered`：已送达（站内 web/外部通道任一成功，或审阅放行）。
- 规则：子 Agent → 用户消息受**事件白名单**约束（§10.4：日报/异常/审批与申请回执/提问回复；不可闲聊）——白名单在引擎层强制校验（见上 pending_review 流程）；
- 聊天记录永久保存（备份/导出含），任务收尾"沉淀"时由 LLM 从会话提炼关键决策入 memory_entries（source 指向会话消息；幂等由 §3.1 dedup_key 保证）。

## 7. 上下文装配（Context Assembly，§9.5 落地）

### 7.0 装配模板与意图路由表存储（v0.3 拍板 C4，新增）

```
context_templates: id, agent_id, template_version, route_table JSON,
  block_order JSON, active BOOL, created_by(manager), created_ts, superseded_ts
```

- **intent 枚举与路由表同源**：route_table JSON 的 key 即合法 intent（选股/复盘/日报/审批/优化 + fallback）；`retrieve()`/`assemble()` 校验 intent ∈ 该 Agent 当前 active 模板的路由表，未知 intent 走 fallback 路由；
- 演进：新 template_version 插入 + active 切换（旧版本标 superseded_ts 保留），全审计（§5.1）；
- 意图→检索路由内容（v0.2 维持）：选股→市场观察卡片+近期决策；复盘→卖出跟踪+演进记录+失败修改；日报→昨日计划+当日引擎数据段；审批→申请历史（管理 Agent 侧）；优化→演进史+版本链+信号统计摘要（数据在 spec-05 消费端）；
- 装配模板（含路由表与块顺序）由管理 Agent 创建子 Agent 时生成，演进调整记录留痕（§9.5）。

### 7.1 装配流程

```
assemble(agent_id, task_intent, task_payload) → {
  prefix_static,      // 静态前缀：系统提示+大纲关键条款+策略理念/双层结构（版本戳化，见 7.2）
  charter_summary,    // 大纲变更摘要（自上次运行以来的变更，通常为空）
  memory_block,       // 按意图检索：cards + 月/周摘要 + 需引用的原文片段
  data_digest,        // 当日所需数据（工具聚合先行 §8.4，数据不进 prompt 原则 §10.5）
  budget,             // 估算 token（7.3）
}
```

### 7.2 静态前缀固定化

- 不可变文本（系统提示/大纲关键条款/策略理念）置于前缀，携带 `charter_version` 戳；大纲版本更新才整体失效 provider prompt caching（§12.4 联动）；缓存键=agent_id+charter_version+模型；
- **缓存命中现实预期（v0.3 补）**：provider prompt caching 有最小前缀长度与缓存 TTL（各 provider 不同），子 Agent 任务间隔长（每日数任务），实际命中率可能低于理想值——P1 以 performance_records.cached_tokens 实测命中率，回填修正 §7.3 预算估算的成本假设。

### 7.3 装配即预算

- 每任务估算 token（前缀常量 + 检索块实测均值 + 数据块按条数计价表）→ 与预算参考值对照（§12.4 预算联动，超出仅提示）；
- 超限任务标记"高耗任务"写入 performance_records.high_cost_flag，供 §4.5 容量/成本审查（#35 联动）；
- 估算与实测均按**缓存命中/未命中分开计价**（§11 cached_tokens/cache_read_per_1k）。

## 8. 向量库与中文 embedding（§16 #20）

- **chromadb 本地持久化，单 collection + metadata 过滤（v0.2 用户拍板）**：全部记忆向量存**一个 collection**，每条向量 metadata 必带 `agent_id`（另含 mem_type/period 等标签）；
- **检索隔离 = 统一检索 API 强制注入 `where(agent_id=…)`**：所有读取路径必须走统一检索 API，不允许绕过注入的底层查询（配合 §5 应用层断言双保险）；单 collection 的跨 Agent 风险由"纪律 + 注入 + 断言"三层兜底；
- **over-fetch 放大检索（v0.3 拍板 B4-①）**：chroma 的 n_results 与 where 过滤存在"先取后滤"实现差异，跨 Agent 混库时可能过滤后不足 top_k——统一检索 API 规定 `n_results = top_k × over_fetch_ratio`（默认 5，可配）检索，where 过滤后按 score 截断 top_k；过滤后不足 top_k 时**如实返回少量结果，不补噪音**；
- **管理 Agent 聚合检索与 spec-05 证据流**：走显式跨 Agent 查询参数（授权路径，含知识库共享例外），全审计；
- **embedding 模型：中文优先**——默认 `bge-small-zh`（或同档中文模型），**模型名与版本入库记录**（config 表 `key=embedding_model`），重建流程依赖此记录（#51）；
  - **推理运行时（v0.3 补）**：本地推理依赖（sentence-transformers 或 onnxruntime，二选一）与 Linux 部署下 CPU 延迟，P1 冒烟实测确定并固化（§14）；
  - **超长文本截断（v0.3 补）**：bge-small-zh max_seq_len=512——摘要/长市场观察超窗时截断策略（头部截断 vs 分块均值池化）并入 P1 冒烟实测定案；
- 卡片、摘要、市场观察、需求文本向量化；原文全文**不向量化**（体积与噪音），回原文走 get_full；
- **影子重建与原子切换（v0.3 拍板 B4-②）**：模型更换/全量重建流程 = 新建影子 collection（`mem_rebuild_<ts>`）→ 全量向量化灌入（离线可延迟任务）→ 抽样检索质量比对 → **原子切换**（config `active_collection` 单事务更新 + 检索 API 引用刷新）→ 删除旧 collection；运行期常态仍为**单 active collection**（不违反单 collection 拍板）；
- **检索性能目标（v0.3 补）**：统一检索 API P95 ≤ 1s（本地 CPU 含 encode），实测值入 performance_records，持续超限告警。

## 9. 策略版本化存储（#31/§16 #9，存储与 API 本 spec，流程 spec-05 消费）

```
strategy_versions: id, agent_id, version_no, parent_version, status,
  config JSON(执行层参数全量快照), config_diff, basis(诊断证据引用:信号/演进记录 id),
  created_by(strategy_agent|manager), created_ts,
  trial_window(验证期配置:仓位上限/窗口长度), validated_on(晋升日), 
  failure_reason(失败版本标记), rolled_back_to
```

- 不变量：每个 Agent 有且仅有一个 status=active 版本；演进记录（memory_entries type=strategy）逐条挂 version_no；
- API：`checkpoint(agent, config, basis)` / `activate(version_no)` / `rollback(agent, reason)`（回到最近 validated 版本，被回退版本标 rolled_back+failure_reason）——幂等、全审计；
- **rollback 边界语义（v0.3 补）**：无已 validated 版本可回退时（如首版本尚在验证期即出事）——rollback() **拒绝执行并告警**（转管理 Agent 待办），执行层**保持当前版本继续运行**（既定策略不变，等价总纲 §3.5"手动暂停"前的安全自治），等待管理 Agent 处置（手动暂停 / 指定回退目标）；不引入生命周期状态机之外的新状态；
- 消费方 spec-05：验证期参数写入、晋升判定（信号统计）、失败自动回滚调用——勾销表标注分工。

## 10. 备份、恢复与重建（#17/#51；导出/导入 UI 归 spec-06）

- **在线备份**：SQLite backup API（VACUUM INTO/备份接口），**不做"暂停调度"式粗暴备份**；
- **安全点检测机制（v0.3 拍板 B3 补）**：引擎在结算事务期间持有"结算中"标志（进程内结算锁，暴露 `is_settling()` 查询）；备份任务执行前检测——结算中则**推迟重试**（间隔可配，默认 5 分钟），落 audit；检测与重试细则 P1 蓝图细化；
- **每日自动备份（P1 起，#51）**：本地备份 + 保留最近 7 份轮换；备份内容=业务库（引擎表+记忆表+任务表等单一 SQLite 文件集合，**含 LangGraph checkpoint 表**）；
  - **备份优先级（v0.3 拍板 B3）**：每日备份为可延迟无时效组（#50）中的**最高优先级**——进程启动即补、不等待空闲窗口判定；core 长时停机（systemd 停服/断电）跨多日未备份时，下次启动首个安全点立即执行（防备份陈旧）；
  - 本地每日备份明文存储（磁盘安全用户自理）；**导出包默认加密**（总纲 §9.4/§15.2 建议 1）；
- **向量不随备份（#51 不变量）**：chromadb 可由原文重建——备份包只记 embedding 模型与版本（config），还原后触发重建任务；
- **还原流程**：停 tick → 关闭 chroma 引用 → 恢复 SQLite 文件（含 checkpoint）→ 自检（账户对账 #49、记忆可检索抽样、任务表重建状态）→ 重建向量 → 恢复调度；
- **导出/导入（跨主机迁移/重装恢复，P4 完整版）**：存储层提供 export_snapshot()/import_snapshot()（zip+校验清单+冲突策略三选一），UI 入口与交互归 spec-06（Web 端）；含聊天记录与设置偏好（§9.4 内容清单）；**导出口径与每日备份一致：不含向量库文件，导入后由原文重建（v0.3 拍板，总纲 §9.4 已同步 v0.17.3）**；
- 备份调度在"可延迟无时效任务"组（#50，见上最高优先级约定）。

## 11. 性能与费用记录（§16 #35；v0.3 拍板 C1 补缓存计价）

```
performance_records: id, task_id, agent_id, task_type, started_ts, ended_ts,
  duration_ms, mem_peak_mb, llm_calls, tool_calls,
  tokens_in(含缓存命中), cached_tokens(其中缓存命中部分), tokens_out,
  cost_yuan DECIMAL(10,4)(按 provider 单价折算，与账单可对), 
  data_fetch_bytes, result(ok|failed|high_cost), high_cost_flag
```

- 单价表：`provider_pricing(provider, model, input_per_1k, output_per_1k, cache_read_per_1k, updated_ts)`——**单价配置化不写死**（价格变动仅改表）；**cache_read_per_1k（v0.3 新增）**：缓存命中单价列，无缓存机制的 provider 置 NULL（按 input 价计）；
- **费用折算（v0.3）**：`cost_yuan = (tokens_in − cached_tokens)×input_per_1k + cached_tokens×cache_read_per_1k + tokens_out×output_per_1k`——缓存命中与未命中分开计价，消除系统性高估，并使 §7.2 缓存收益**可度量**；
- **tool_calls（v0.4）**：任务收尾节点统计本任务工具调用次数落表，供任务级执行预算（总纲 #60，spec-04 §3.2）监控与高耗任务分析；
- 适配层要求：usage 中的 `prompt_tokens_details.cached_tokens`（OpenAI 兼容格式）须透传落表（无该字段的 provider 记 0 并在适配层标注）；
- 消费：容量评估与成本审查按 **元/天、Agent、任务类型** 三维（§4.5 联动）；月度费用汇总入《策略体检报告》（spec-04/06）；**月度"记录折算 vs provider 账单"对账报告由 spec-04 容量/成本模块生成、spec-06 展示**（本 spec 保证 task 粒度 tokens/cost 留痕可对，v0.3 明确归属）。

## 12. 测试计划（v0.3 补 5 类用例）

- 隔离测试：跨 agent 检索/读取一律拒绝（§5 必测）；
- **over-fetch 检索用例（v0.3）**：单 collection 下构造跨 Agent 高相似干扰向量，验证结果仅含本 Agent 且 top_k 截断正确、无泄漏；
- **幂等用例（v0.3）**：sediment 重跑 / 摘要补跑 / 卡片重生成 → 三表唯一约束下零重复条目；
- **滚动删除级联用例（v0.3）**：原文删 → 向量删 → 卡片删 → 审计留痕 → 摘要 record_ids 可溯源到删除审计记录；
- **白名单拦截用例（v0.3）**：非白名单 msg_type → pending_review + audit，不直达用户；审阅放行后转 delivered；
- **中文检索质量冒烟（v0.3 列入，P1 执行）**：§14 实测项对应，结果固化回 §8；
- 卡片-原文一致性抽测：卡片引用回原文核对（抽样）；
- 分层摘要溯源：摘要 record_ids 可逐条展开到原文（远期市场观察按 §4.3 降级口径验收到审计记录）；
- 版本化：checkpoint/晋升/回滚状态机用例（active 唯一性、回滚幂等、**无 validated 目标时拒绝+告警**）；
- 备份还原：备份→还原→对账一致 + 向量重建完成标志 + checkpoint 随库恢复；
- 检索路由：意图→路由表命中率冒烟用例（含未知 intent 走 fallback）；
- 预算估算：装配 token 估算误差 <±20% 用例（估算函数单测）。

## 13. §16 勾销表（本文件状态）

| # | 事项 | 状态 |
|---|---|---|
| 9 | 策略版本化存储 schema（spec-02/05） | 存储与 API 已写入 §9；验证期/晋升/回滚流程移交 spec-05（状态=引擎侧部分已写入，流程待 spec-05） |
| 17 | 备份一致性：SQLite 在线备份 API + 安全点标志，不粗暴暂停；LLM 调用不等待（按状态机恢复） | 已写入 §10（v0.3 补安全点检测机制 `is_settling()` + 推迟重试 + 备份最高优先级） |
| 20 | 记忆 embedding 中文选型（bge-small-zh 等）、模型与版本入库（#51 重建依赖） | 已写入 §8（v0.3 补推理运行时与超长截断，并入 P1 实测） |
| 35 | 性能监控费用维度：费用字段（元，按 provider 单价折算）、元/天/Agent/任务类型三维展示数据源 | 已写入 §11（v0.3 补 cached_tokens/cache_read_per_1k 缓存计价与对账归属） |

## 14. 评审确认记录

### 第一轮（2026-09-05 用户拍板，v0.2 落实）

1. **卡片 token 档位**：200-500 区间内由生成模型自定，不按记忆类型分档（§3.1）；
2. **月摘要生成**：由本月周摘要**合成**（继承 record_ids 全集、抽检可回溯原文），周摘要仍从原文重算（§4.2）；
3. **向量库划分**：**单 collection + metadata（agent_id 标签）过滤**——统一检索 API 强制注入 where，隔离由"纪律+注入+断言"三层兜底；管理 Agent 聚合检索走显式跨 Agent 授权路径（§8）；
4. **会话沉淀触发**：任务收尾节点**显式调用** memory.sediment()（编排层在 P1 蓝图定义调用点）——确认原稿机制（§6.2）；
5. **归档清理**：归档 Agent 记忆保留期**可配**（默认永久，限期清理前完成摘要+抽检+审计）（§5）。

### 第二轮（2026-09-05 用户拍板，v0.3 落实）

1. **A1+A2 滚动删除**：**方案 A**——市场观察为滚动类例外、远期原文可物理删除，前置条件（摘要完成+抽检通过）+ 级联清理（向量/卡片同步）+ 审计留痕（§2/§4.3）；
2. **A3 空窗兜底**：无卡片近期条目兜底检索（近 N 交易日结构化查询，from_card=false 标注）+ 超时补卡（24h 阈值，可延迟组高优先级）（§3.2）；
3. **B1 幂等键**：memory_entries `UNIQUE(agent_id, dedup_key)`（显式 dedup_key，防同任务多条同类型误伤）/ memory_cards `PRIMARY KEY(memory_id)` 原地 upsert / memory_summaries `UNIQUE(agent_id, period, period_key)` 补跑覆盖重算（§3.1/§4）；
4. **B2 周期口径**：日/周/月摘要一律按 A 股交易日历（北京时间）口径 + 调度触发谓词定义归本 spec（§4.1）；
5. **C1 cached_tokens 落表**：performance_records 加 cached_tokens + provider_pricing 加 cache_read_per_1k，费用折算分缓存命中/未命中计价（§11）；
6. **C5/C6 归口声明**：audit_logs 与 LangGraph checkpoint 存储均归 spec-02（§1/§5.1/§6.1）；
7. **其余按评审建议落实**：B3 安全点检测机制 + 备份可延迟组最高优先级开机即补（§10）；B4 over-fetch（5×放大过滤截断）+ 影子 collection 原子切换（§8）；C2 quality 字段语义定义（flagged 触发卡片重生成，§3.1）；C3 messages 状态机（delivered/pending_review/failed，§6.2）；C4 装配模板与路由表存储 context_templates + intent 枚举同源（§7.0）；C7 导出快照不含向量、导入后重建（§8/§10，总纲 §9.4 同步 v0.17.3）；D1 测试计划补 5 类用例（§12）；D2 rollback 无 validated 目标时拒绝+告警+保持当前版本（§9）；D3 月度费用对账归属 spec-04 生成/spec-06 展示（§11）；D4 embedding 推理运行时与超长截断并入 P1 实测（§8/§14）；D5 检索 P95 ≤ 1s 性能目标（§8）；另 §7.2 补 prompt caching 命中率现实预期（P1 实测校准）。

### 第三轮（2026-09-06，夜间优化轮·用户追认后定稿，落实于 v0.4）

1. **messages.delivered_via 通道名同步**：serverchan → **clawbot**（总纲 v0.17.7 推送通道修订在本文件的遗留不一致，§6.2）；
2. **LangGraph checkpoint 保留策略**：每 thread 保留最近 K=10（可配）、图版本升级清理、interrupt 挂起中不清理（§6.1）——防 checkpoint 无界增长；
3. **performance_records 补 tool_calls 字段**（§11）——支撑任务级执行预算（总纲 #60）监控；
4. **审计清单补账户复位/试运行归档**（§5.1，联动 spec-01 §2.8）；
5. **用户追认（2026-09-06 上午）**：随总纲 v0.18 四项拍板一并定稿（v0.4 为开发基线）。

**剩余待实测项（P1 执行，结果固化回对应章节）**：
1. embedding 中文检索质量冒烟（bge-small-zh）→ 固化回 §8；
2. 推理运行时选型（sentence-transformers vs onnxruntime）与超长文本截断策略 → 固化回 §8；
3. provider prompt caching 实际命中率 → 固化回 §7.2/§7.3 成本假设。

**Web 部署版适配（v0.5，随总纲 v0.20，待用户终审）**：本文件 `delivered_via` 取值 `desktop`→`web`（§6.2，站内通知 web）、桌面兜底→站内通知（web）兜底、Windows CPU 延迟→Linux 部署、开机即补→进程启动即补、换机迁移→跨主机迁移（§3.2/§6.2/§8/§10）；存储 schema/隔离/备份/向量/审计/checkpoint 等机制无变更，无新增行为决策，业务与 v0.4 定稿一致。

## 变更记录

| 版本 | 日期 | 状态 | 要点 |
|---|---|---|---|
| v0.1 | 2026-09-05 | 草稿待评审 | 初稿：分级记忆落库（原文/卡片/分层摘要）、隔离与检索协议、会话存储与事件白名单、上下文装配（前缀固定化/预算）、中文 embedding、策略版本化存储、备份重建、性能费用记录；§16 #9/#17/#20/#35 勾销；待评审项 5 条 |
| v0.2 | 2026-09-05 | 第一轮评审修订 | ①卡片 token 200-500 由模型自定不分档（§3.1）②月摘要改由周摘要合成、周摘要仍从原文重算、record_ids 继承可溯源（§4）③向量库改**单 collection + metadata（agent_id）**，统一检索 API 强制注入 where、三层兜底、跨 Agent 授权路径（§8/§5）④会话沉淀=任务收尾显式调用确认（§6）⑤归档记忆保留期可配清理（§5）⑥§14 改为评审确认记录，剩余实测仅 embedding 冒烟 1 项 |
| v0.3 | 2026-09-05 | 第二轮评审修订（ZCode 深度评审） | ①滚动删除**方案 A**：市场观察例外可物理删+前置条件+向量/卡片级联清理+审计，溯源降级为删除审计记录（§2/§4.3）②无卡片空窗兜底：近期原文结构化检索 + 超时补卡（§3.2）③三表幂等约束：dedup_key/卡片主键 upsert/摘要 UNIQUE（§3.1/§4）④摘要周期按交易日历（北京时间）+ 触发谓词归本 spec（§4.1）⑤cached_tokens + cache_read_per_1k 落表、费用分缓存计价（§11）⑥审计 audit_logs 与 LangGraph checkpoint 归口声明归本 spec（§1/§5.1/§6.1）⑦备份安全点 is_settling() 检测+推迟重试+可延迟组最高优先级开机即补（§10）⑧over-fetch 5×放大截断 + 影子 collection 原子切换（§8）⑨messages 状态机（§6.2）、context_templates 存储与 intent 同源（§7.0）、quality 语义定义（§3.1）、rollback 无目标拒绝告警（§9）、导出不含向量（§8/§10）、测试补 5 类、检索 P95 目标、缓存命中率现实预期（§7.2）；剩余待实测 3 项 |
| **v0.4** | 2026-09-06 | 夜间优化轮（**用户追认定稿**） | ①§6.2 delivered_via 通道名 serverchan→clawbot（总纲 v0.17.7 遗留同步）②§6.1 checkpoint 保留策略（K=10/图版本清理/interrupt 挂起豁免）③§11 performance_records 补 tool_calls（任务级执行预算 #60 支撑）④§5.1 审计清单补账户复位/试运行归档（spec-01 §2.8 联动）；无行为规则变更，存储健壮性与一致性问题收口 |
| **v0.5** | 2026-09-06 | Web 部署版适配（**随总纲 v0.20 提交，待用户终审**；机制同 v0.4，无新增行为决策） | ①§6.2 delivered_via 取值 `desktop`→`web`（站内通知 web，默认通道改写）②消息失败补偿路径与 §3.2 超时补卡时机改写：桌面兜底→站内通知（web）兜底、开机首个空闲窗口→进程启动后首个空闲窗口 ③桌面部署语义清除：Windows CPU 延迟→Linux 部署（§8）、开机即补→进程启动即补（§10）、换机迁移→跨主机迁移/重装恢复（§10）、§10 标题"UI 归 spec-06/桌面"→"归 spec-06" ④依据与交叉引用版本同步（总纲 v0.20；spec-01 v0.7/spec-04 v0.7） |
