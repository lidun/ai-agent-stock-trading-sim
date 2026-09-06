# User Instruction Memory

This file records user instructions, preferences, and teachings for reference in future interactions.

## Format

### User Instruction Entry
User instruction entries should follow this format:

[User Instruction Summary]
- Date: [YYYY-MM-DD]
- Context: [Mentioned scenario or time]
- Instructions:
  - [Content of user teaching or instruction, described line by line]

### Project Knowledge Entry
Entries discovered by the Agent during task execution should follow this format:

[Project Knowledge Summary]
- Date: [YYYY-MM-DD]
- Context: Discovered by Agent while performing [specific task description]
- Category: [Operations & Deployment|Build Methods|Testing Methods|Troubleshooting & Debugging|Workflow & Collaboration|Environment Configuration]
- Instructions:
  - [Specific knowledge points, described line by line]

## Deduplication Strategy
- Before adding a new entry, check for similar or identical instructions.
- If a duplicate is found, skip the new entry or merge it with the existing one.
- When merging, update the context or date information.
- This helps avoid redundant entries and keeps the memory file tidy.

## Entries

[Project Knowledge Summary]
- Date: 2026-09-06
- Context: Discovered by Agent while implementing 腾讯系行情适配器与 EOD 结算编排（settle_day 切片）
- Category: Environment Configuration / Build Methods
- Instructions:
  - 本沙箱出网为白名单制：东财 push2/push2his 与 akshare 系被墙（HTTP 000），腾讯系可达（qt.gtimg.cn 实时快照、web.ifzq.gtimg.cn 日K/分时）；行情适配 core/quotes_tencent.py 默认走腾讯单源，spec-03 异族抽检第二源尚未配置。
  - 腾讯分时接口只给最新一个完整交易会话（09:30–15:00 逐分钟 + 15:06–15:30 冻结续段），续段属 spec-03 is_extended 须剔除；历史日分钟不可得，历史日补跑需 L2 档（引擎未支持则显式 gap）。
  - 测试运行：`python3 -m pytest core/tests -q`（conftest 自布置隔离 CORE_DATA_DIR/测试口令，无需网络）；行情解析单测固定读 core/tests/fixtures/ 下录制样本，禁止依赖网络。
  - 实盘/冒烟运行库为 core/data/aat.db（.gitignore 忽略）；用隔离数据目录做回放冒烟时设 `CORE_DATA_DIR=<tmp>` 且 `CORE_SINGLE_INSTANCE_LOCK=0`（或直接调用 core.settle_day.run_day 不带 app 锁）。
  - 结算编排入口：`python3 -m core.settle_day --date YYYY-MM-DD [--account …]`；未显式给日期时用腾讯快照 ts 推断最近会话日。
