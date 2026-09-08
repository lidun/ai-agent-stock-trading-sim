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

[User Instruction Summary]
- Date: 2026-09-06（2026-09-08 演进记忆/能力域切片后用户重申并明确：继续）
- Context: 完成多个开发切片后，用户补充推进方式约定；本次再强调"以后不用问先做哪个，按你的建议直接做"
- Instructions:
  - 后续切片推进时无需询问用户"先做哪个"；由 Agent 自行按规格依赖与风险排序决定，并持续自主推进直至整个项目完成。
  - 每完成一个切片只需用一段话汇报交付与验证结果，可顺带列出后续候选但不提供选择菜单。

[User Instruction Summary]
- Date: 2026-09-06
- Context: 用户先约定切片完成后默认提交，后续进一步明确所有更改直接提交并推送（time 定时单切片完成时重申）
- Instructions:
  - 完成每个开发切片或任何改动（验证通过后）默认直接 git 提交并推送到远端，无需再逐次询问是否提交/推送；提交信息沿用仓库既有风格。
  - 本仓库推送方式：本地分支 master → `git push origin HEAD:main`（见下方远端条目）。

[Project Knowledge Summary]
- Date: 2026-09-06
- Context: Discovered by Agent while implementing 腾讯系行情适配器与 EOD 结算编排（settle_day 切片）
- Category: Environment Configuration / Build Methods
- Instructions:
  - 本沙箱出网为白名单制：东财 push2/push2his 与 akshare 系被墙（HTTP 000），腾讯系可达（qt.gtimg.cn 实时快照、web.ifzq.gtimg.cn 日K/分时）；行情适配 core/quotes_tencent.py 默认走腾讯单源，spec-03 异族抽检第二源尚未配置。
  - 腾讯分时接口只给最新一个完整交易会话（09:30–15:00 逐分钟 + 15:06–15:30 冻结续段），续段属 spec-03 is_extended 须剔除；历史日分钟不可得 → 历史日补跑自动回退 L2 日线区间档（settle_day._build_feeds 对 replay_day 缺口调 quotes_tencent.replay_l2，引擎按 l2_map 官方收盘价成交）；两档皆缺才显式 gap 并报账户 error。
  - 测试运行：`python3 -m pytest core/tests -q`（conftest 自布置隔离 CORE_DATA_DIR/测试口令，无需网络）；行情解析单测固定读 core/tests/fixtures/ 下录制样本，禁止依赖网络。
  - 实盘/冒烟运行库为 core/data/aat.db（.gitignore 忽略）；用隔离数据目录做回放冒烟时设 `CORE_DATA_DIR=<tmp>` 且 `CORE_SINGLE_INSTANCE_LOCK=0`（或直接调用 core.settle_day.run_day 不带 app 锁）。
  - 结算编排入口：`python3 -m core.settle_day --date YYYY-MM-DD [--account …]`；未显式给日期时用腾讯快照 ts 推断最近会话日。

[Project Knowledge Summary]
- Date: 2026-09-06
- Context: Discovered by Agent while implementing EOD 结算自动触发（settle_scheduler 切片，spec-04 §2.2 第 2 项）
- Category: Operations & Deployment
- Instructions:
  - 结算自动触发默认关闭；常驻启用需设 `CORE_EOD_AUTO_SETTLE=1`，可配 `CORE_EOD_SETTLE_TICK_S`（默认 60）/`CORE_EOD_SETTLE_EARLIEST`（默认 15:35）/`CORE_EOD_SETTLE_RETRY_UNTIL`（默认 16:35）。
  - 触发判定是本地 SQL+腾讯快照（零日历表）：非交易日/未开盘快照 ts 与当日不符即跳过；测试一律注入 feed（tests/_feedkit.py FakeFeed/ReadySessionFeed/GapReplayFeed 系），禁止网络。
  - 进程内 _done_dates 仅节流，跨进程幂等靠 DB settle_key；单测回归命令见上条。

[Project Knowledge Summary]
- Date: 2026-09-06
- Context: Discovered by Agent while binding 本仓库到 GitHub（用户账号 lidun，原 lidun/test 重命名为 lidun/ai-agent-stock-trading-sim 并清空绑定）
- Category: Operations & Deployment
- Instructions:
  - 本项目 GitHub 远端 origin=https://github.com/lidun/ai-agent-stock-trading-sim.git，默认分支 main；本地分支名 master，推送需 `git push origin HEAD:main`；按用户约定完成每个开发切片后默认提交并推送。
  - gh 已以账号 lidun 登录（web 设备流，浏览器一次性码授权）；沙箱内置 git 凭据助手对 github.com 会 500，需 `gh auth setup-git` 后推送走 gh 凭据助手。
  - 仓库为 PRIVATE（已私有化）；原 test 仓库残留的 PR #1 与 pr/smol-dev/zrye5w 分支已清理（closed，refs/pull/1/head 属正常残留可忽略）。

[Project Knowledge Summary]
- Date: 2026-09-07
- Context: Discovered by Agent while running core/tests/test_reproducibility.py 脚本调试金标准 diff
- Category: Troubleshooting & Debugging / Environment Configuration
- Instructions:
  - 不要在开发服运行期间用裸 `python3` 直接 import/执行 core/tests 下测试模块：`core/app.py` 模块级 `app = create_app()` 会撞默认库 core/data 的单实例锁并 sys.exit(1)；必须用 `python3 -m pytest`（conftest 会在 import 前覆写 CORE_DATA_DIR/CORE_SINGLE_INSTANCE_LOCK=0 等）。若确需脚本方式，先设 `CORE_DATA_DIR=<tmp> CORE_SINGLE_INSTANCE_LOCK=0`。
  - 结算日报引擎数据段以 settlement_log.positions_snapshot 为唯一数据源（spec-04 §5.2 零 token 直读）；结算/快照 schema 变更后须 `UPDATE_GOLDEN=1 python3 -m pytest core/tests/test_reproducibility.py` 重写 core/tests/golden/repro_full_normalized.txt 并人工审阅 diff 后随代码一起提交。

[Project Knowledge Summary]
- Date: 2026-09-06
- Context: Discovered by Agent while debugging ST/新股拦截引擎测试（eodengine.py 快速多次源码修改后出现"修改不生效"假象）
- Category: Troubleshooting & Debugging / Testing Methods
- Instructions:
  - 引擎测试 helper core/tests/test_eodengine.py `_insert_order` 的 created 默认是 "2026-09-07T09:00:00"；结算日不是 09-07 时新用例必须显式传 created=结算日时间，否则引擎终态循环按"非本日单（#38）"跳过状态写盘——订单会被撮合/成交/结算但状态不更新（settlement_log 照写），表现为断言 status 一直为初始 active/invalid_reason 空。
  - 同一秒内连续多次改写 .py 源文件时，Python 的 pyc mtime 以秒为粒度，可能复用陈旧字节码导致"代码改了没生效"的假象；连续改源后重跑测试前执行 `find core -name '__pycache__' -type d -prune -exec rm -rf {} +` 或 touch 源文件避开同秒，防止用 DBG 探针排查时被误导。
