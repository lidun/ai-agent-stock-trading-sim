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
- Date: 2026-09-06
- Context: 完成 trigger kind 规范化切片后，用户说明后续默认提交
- Instructions:
  - 完成每个开发切片（改动验证通过后）默认直接提交，无需再逐次询问是否提交；提交信息沿用仓库既有风格。

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
  - 本项目 GitHub 远端 origin=https://github.com/lidun/ai-agent-stock-trading-sim.git，默认分支 main；本地分支名 master，推送需 `git push origin HEAD:main`。
  - gh 已以账号 lidun 登录（web 设备流，浏览器一次性码授权）；沙箱内置 git 凭据助手对 github.com 会 500，需 `gh auth setup-git` 后推送走 gh 凭据助手。
  - 历史遗留：远端 pr/smol-dev/zrye5w 分支与 PR #1 仍指向被替换前的旧内容（test 仓库残留），如需干净可删除该分支与 PR。
  - 仓库为 PUBLIC；若需私有改 `gh repo edit lidun/ai-agent-stock-trading-sim --visibility private`。
