# ForeContract 本地应用宿主（APP-T01 至 APP-T05 前端操作台、离线持久会话与本地 Skill / MCP 子集）

本目录提供 ForeContract 的本机可视化宿主与静态前端（原生 HTML / CSS / ES Module），用于驱动原生 `Core RuntimeEngine` / `LLMPlanner` / `forecontract_coding` 在**受控示例项目**与**只读起步的已有本机项目**上执行有界编码、持久多轮会话与验证闭环，并在 APP-T05 中补充**本地 Skill 目录**与 **MCP `2025-11-25` tools 客户端子集**。

## 启动方式

### 通用源码启动（用户 checkout，无需特定验收路径）

从任意位置的 checkout 与已备好的 Python 环境组装四个源码根，通过 `PYTHONPATH` 直接启动。路径含空格请保留引号；`$data` / `$workspaces` / `$projects` 必须为互不重叠的绝对目录：

```powershell
$checkout = 'C:\path with spaces\ForeContract'  # 你的 checkout 根目录
$python   = 'C:\Python312\python.exe'            # Python >= 3.12 解释器绝对路径
$deps     = 'C:\ForeContractDeps'                # 已备只读第三方依赖目录（uvicorn/fastapi/pydantic/starlette 等）
$env:PYTHONPATH = @(
  "$checkout\apps\local",
  "$checkout\packages\core\src",
  "$checkout\packages\planners\llm\src",
  "$checkout\packages\adapters\coding\src",
  $deps
) -join ';'
$env:PIP_NO_INDEX = '1'
$data       = 'C:\ForeContractData\data'
$workspaces = 'C:\ForeContractData\workspaces'
$projects   = 'C:\ForeContractData\projects'
New-Item -ItemType Directory -Force $data, $workspaces, $projects | Out-Null
& $python -B -m forecontract_app --data-root $data --workspace-parent $workspaces --project-root $projects --port 8765 --offline
```

- 上述源码启动已在本机 **Windows / NTFS / TxF + Python 3.12** 离线实测；未在其他 Python 版本或操作系统实测。安装步骤、离线/真实模型分界与故障排查见 [docs/LOCAL_DELIVERY.zh-CN.md](../../docs/LOCAL_DELIVERY.zh-CN.md)。
- 固定第三方依赖目录为可复用只读环境（不重装或修改依赖环境）。

- `--extension-config` 为可选显式参数：指向本机 UTF-8 JSON 文件（上限 64 KiB），格式为 `{"version": 1, "servers": [...]}`（最多 8 个服务器）：
  - `stdio`：`id`（ASCII `[A-Za-z0-9._-]{1,64}`）、`transport: "stdio"`、`executable`（绝对非重解析路径，禁止 shell 拼接）、`argv`（最多 64 项 / 总 ≤ 16 KiB）、`cwd`（已存在绝对目录），启动时核对文件与配置 SHA-256，不继承 API Key / 凭据环境变量；
  - `http`：`id`、`transport: "http"`、`url`（仅允许 `http://127.0.0.1:<port>...` 或 `http://[::1]:<port>...`，显式端口 `1..65535`，禁止 DNS/外网/redirect/proxy/`trust_env`/userinfo/query/fragment）。
  - 禁止 `env`/`headers`/`token`/`api_key` 等未知或凭据字段；配置修改仅重启生效，列表 `GET` 绝不启动进程或网络。

## 本地 Skill 与基础 MCP 边界（APP-T05）

- **本地 Skill 目录**（`<project_root>/.forecontract/skills/<id>/SKILL.md`）：
  - 单层 ASCII 目录枚举（最多 64 个），按 `id` 排序；发现阶段只列元数据与 SHA-256，按需分页读取（每页 ≤ 8 KiB，UTF-8 安全边界，单文件 ≤ 64 KiB，续读须带 `expected_sha256` 防变更）。
  - 每次启动已有项目运行最多勾选 4 个 Skill 且总原文与来源 ≤ 16 KiB（含 `skill_ids` 幂等指纹）；不存在“Skill 隐式执行”后门，Skill 中脚本仍须走既有 `read_file` / `propose_command` / `start_command` 受信任审批。Skill 与 MCP 返回结果均作为不可信内容（不高于用户约束 / `AGENTS.md` / 权限批准）。
- **MCP `2025-11-25` tools 客户端子集**：
  - 仅在 `skill_ids` 非空或显式配置 `--extension-config` 时开放运行内 6 个扩展动作（`list_skills` / `read_skill` / `list_mcp_servers` / `connect_mcp` / `list_mcp_tools` / `call_mcp`）；默认未启用时严格维持原 13 个项目动作合同。
  - 每次运行内首次 `connect_mcp` 与每次 `call_mcp` 均须在前端/API 显式批准（绑定 `(project_id, session_id, run_id, action_id)`、`config_sha256`、`executable_sha256`、`schema_sha256`、`args_sha256` 与 120 秒不可刷新 TTL），明示**受信任本机执行，非 OS 沙箱**；不宣称 OAuth、远程服务、`resources`/`prompts`/`sampling`/`elicitation`/`tasks` 或自动重连重放。
  - 每 Run 最多 8 连接、每连接单在途调用、最多 32 次调用与 1 MiB 运行内调用数据上限（每页展示 ≤ 8 KiB）；输入 Schema 仅接受深度 ≤ 8 的受控子集（不支持关键字标 `unsupported_schema` 并拒绝调用）。
  - `stdio` 通过 Windows Job Object 管理生命周期（挂起创建→入 Job→恢复），取消或关服按总 2 秒上限关闭 stdin 并终止所属 Job（按 Job 校验余量 0，禁止按进程名扫杀）；`tools/call` 成功不等于项目验证通过，仍须执行真实 `verify_project` 并满足最终修订与 oracle 校验；中断或清理不明时状态保留 `unknown` 并锁住工作区。

- `--project-root` 为可重复显式参数：仅当已有目录规范真实路径与启动参数声明的某个 `--project-root` **精确相等**（无 `..`、无 UNC、无 symlink/junction/reparse 祖先、不与 `data_root`/`workspace_parent` 重叠），`POST /api/v1/projects/register` 才允许以只读起步登记。
- 启动时随机生成至少 256-bit 的本地访问 token，仅在终端一次性输出带 `#token=...` fragment 的本机 URL；页面读取后立即清除地址栏 fragment 并存入 `sessionStorage`（无非 token 流绕过，接口始终经 `Authorization: Bearer <token>` 鉴权）。
- 测试/宿主可控停止开关 `--control-stdin` 仅在显式启用时读取自身标准输入的一行 `stop` 触发受控关服与 lifespan 收尾。
- CLI **无 `--live` 参数**；传入未识别参数将直接非零退出。

## 前端工作台操作路径（APP-T04 / UI-T01 六页工作台）

1. **项目、会话与响应式六页导航**：
   - 左侧导航栏提供“项目”、“审批收件箱”、“运行历史”、“扩展”、“当前会话”和“设置”六页；桌面端双栏常驻，窄屏默认收起并通过 `navigation-toggle` 展开/关闭（支持 `Esc` 关闭并归还焦点）。顶部栏实时显示当前项目（`current-project`）、当前会话（`current-session`）与连接状态（`connection-state`，取值为 `idle` / `loading` / `connected` / `reconnecting` / `disconnected` / `error`）；所有控件具备文本可访问名称与可见键盘焦点。
   - 点击项目时会话列表按该项目过滤并清除旧项目的活动运行、文件/Git 审阅状态与可操作卡片；所有异步读取与变更均受选择代次与捕获身份校验保护，过期回执不会覆盖新会话视图，且在请求未决期间锁定对应按钮防重复提交（不自动重发 POST/PUT）。
   - 流断开后按 `1s / 2s / 4s` 退避执行 `GET` 重连，连续三次失败后停止自动重连并展示显式重连按钮（`reconnect-stream`）；连接断开不等同于 Run 失败或成功。
2. **上下文、草稿与运行历史回看**：
   - `goal` 与未保存固定约束按 session 暂存在页面内存；切换会话可恢复，但**草稿未保存，刷新会丢失**（页面 `draft-status` 与设置页明确标明）。保存遇到 `409` 版本冲突时保留用户输入且不自动改 revision 重试。
   - 历史列表逐页游标拉取（“运行历史”页支持按已加载记录筛选与切换查看）；当分页失败或游标未推进时明确标注不完整并停止继续读取。通过 `run-history-select` 可切换查看当前活动或已终态历史运行（`changed_paths`、`diff-file-select` / `diff-detail` 与 `coverage-summary`）；历史或已终态运行只读展示，不提供活动审批、问答或停止按钮。
3. **运行审批、审批收件箱、文件与只读 Git 审阅**：
   - “审批收件箱”聚合当前项目各活动运行的待审批提案（范围/验证/命令/扩展），以真实已加载提案计数并支持跳回原会话；部分加载失败时显式提示不完整。仅当前活动运行中的待定提问与提案开放交互：自由文本回答仅发往 `/answer`（不构成任何写/命令批准）；`command` 提案逐条独立勾选 `trusted_local_execution`（明确提示**非 OS 沙箱，进程可访问本机文件与网络**）。
   - 停止命令收到 `202` 时标为停止中等待真实退出回执，不伪造 `cancelled`；命令输出与 SSE 事件严格按 `(project_id, session_id, run_id, seq)` 去重有序展示。
   - 会话右栏区分“运行差异”（持久 Run diff）、“文件”（`GET /api/v1/projects/{pid}/files` 与 `/file`，仅限已登记 `existing` 项目当前磁盘）与“Git”（`GET /api/v1/projects/{pid}/git/status` 与 `/git/diff`，区分 `area=index` 暂存区与 `area=worktree` 工作树，仅支持项目根即为工作树根的本地仓库，无写入/远程操作且不轮询）。
   - “设置”页支持本浏览器外观主题/密度偏好（不存凭据），并按实际宿主返回展示模式、连接说明、会话约束、执行权限与真实存储用量，不展示测试限额或示例计费数字。

## Windows 本地事件循环兼容说明

- 在 Windows 平台上，本宿主 CLI 与测试 TCP server 线程仅通过局部 `asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)` 驱动自身 `serve()` 协程，保留单 worker、`127.0.0.1` 回环、无 reload、8 秒优雅关服及原 stdin stop / 文件锁语义。
- 不设置全局 `asyncio.set_event_loop_policy`，不修改 Windows / Winsock / 防火墙 / 代理或 Playwright 事件循环。该策略仅为本应用的局部兼容性规避；Windows 默认 Proactor 下偶发 `WinError 10014` 的底层系统根因仍为 `unknown`（不宣称已解决系统根因，亦不代替功能独立验收）。

## 离线执行模式、已有项目两阶段授权与真实边界

- **受控示例项目**（`POST /api/v1/projects` + `offline-metrics-v1`）：保持 APP-T01 范围，仅操作工作区相对路径 `metrics.py` 与外部只读验收测试 `test_metrics_acceptance.py`。
- **已有本机项目**（`POST /api/v1/projects/register` + `offline-project-v1`）：
  1. 登记时只校验与建立精确只读工作区身份（`root_identity` + `read_grant_id`），绝不在项目目录写入演示或标记文件；
  2. 原生 `ProjectCodingAdapter` 提供固定 13 动作契约与有界探索：`list_directory`（单页 `limit<=100`，目录游标带快照哈希检测并发变更返回 `409`）、`find_paths` / `search_text`（每批 200 文件 / 单批内容读取严格不超过 4 MiB / 50 命中 / 16 KiB 有界游标续扫）、`read_file`（最大 8 MiB 文件，单块 <=16 KiB，支持 UTF-8 / CRLF 安全换行与字节偏移续读）、固定排除敏感与二进制文件（`.git`、`.env`、`.env.*`、密钥文件等，不解析 arbitrary `.gitignore` 规则），并按目标路径祖先链与作用域目录绑定校验根及多层 `AGENTS.md` 指令链（单文件 <=16 KiB，全链 <=64 KiB）；
  3. 写入、验证与命令执行采用结构化分阶段审批与 120 秒不可刷新 TTL（批准不续期，取消会同时吊销 `pending` 与未消费的 `approved` 授权）：`propose_scope` 绑定路径前缀/精确路径/操作/受保护路径，`propose_verification` 锁定非空 `VerificationPlan`（支持 `junit_exact` 与明确标注仅查退出码的 `exit_code`），`start_command`（含 `diagnostic`）与 `verify_project` 在 Core 前置条件与启动最后边界均强制校验有效已批准 `VerificationPlan`，`verify_project` 按计划逐项申请 `propose_command`（无 `env`/`env_keys` 透传，人工审批放行后、实际启动前重读磁盘指令与受保护文件并复算可执行文件 SHA-256，必须显式确认 `trusted_local_execution=true`，属受信任本机命令而非 OS 沙箱）；
  4. 原子单文件提交继续复用 `forecontract_coding.workspace.commit_file_atomic`（Windows TxF 独占锁、65,536 字节严格上限校验及落盘读回校验；延续 COD-T01 L01 原生 TxF 兼容性边界，不静默降级为截断写入）；
  5. 进程控制通过 Windows Job Object 增量输出回调实时流式推送（每流 1 MiB 环形与 16 KiB 分页游标，支持 `start_command` / `poll_command` / `stop_command` 与命令运行期间写/启停互斥 `COMMAND_IN_PROGRESS`）；终态状态 `state` 与 `verification` 分别如实展示（不将 `exit_code == 0` 充当单元测试全部通过，也不将被排除路径算作已覆盖）；期间发生外部修改、指令变更、清理未确认或范围超限则显式判为 `unknown`（并保持工作区锁定）或拒绝。
- 页面模型下拉框支持保存 `deepseek` / `deepseek-flash` / `thinking=disabled` 的非敏感候选设置，但在离线模式下启动运行必定以 `409 LIVE_DISABLED` 拒绝；无 API Key 输入框、无凭据持久化、无网络外呼；子进程环境不继承凭据环境变量。宿主可显式以 `--live-deepseek`（与 `--offline` 互斥，省略仍离线）启用实时 DeepSeek 提供方：凭据仅由宿主独占读取（环境变量 `DEEPSEEK_API_KEY`），前端只获模式、允许模型与就绪状态，实际运行仍经既有 scope / verification / command 审批；缺配置返回稳定错误且零传输。

## 持久会话、容量上限、重启恢复边界与离线用量

- **单库有界持久化**（`<data_root>/sessions.sqlite3`）：使用 Python 标准库 `sqlite3`（`schema_version=1`，固定页大小 `4096`、`max_page_count=32768`，主库上限 128 MiB，有界 rollback journal，拒绝 symlink/reparse 目标与损坏/未知高版本库），同步事务提交已登记项目非敏感配置、会话及固定约束、Run 状态/幂等键、单向 `HistoryItem`、有界提问（`QuestionView`）、有界用量快照与每 Run 持久事件尾（至多 64 条 / 512 KiB，超限时仅裁事件尾并推进 `truncated_before`，不删除用户历史）。`HostConfig`、访问 token、实时 SSE 队列、进程句柄与完整 `ModelRequest` 仅驻留内存，绝不入库。
- **有界上下文与显式续跑**：同一会话显式调用 `POST /api/v1/sessions/{sid}/runs`（新 `client_request_id`）发起下一轮，绝不自动重放旧 Run 或自动延续上一轮写范围/验证计划/命令授权；已有项目模式通过 `build_context_frame` 精确按 UTF-8 JSON 字节计量（默认上限 16 KiB），原样携带 `constraints` 并按 `seq` 由新到旧安全截断历史正文（保留原始完整 `sha256` 与 `source_ids`），再接受观测 64 KiB 上限裁决。
- **运行时提问（opt-in `ask_user`）**：仅当启用 `ConversationRuntime` 且携带 `QuestionBroker` 时开放第 14 个受控合同动作 `ask_user`（每 Run 最多 1 个 `pending`、总计最多 8 题、120 秒单调 TTL 受剩余 Run 预算约束），经由原生 `forecast -> prepare_contract -> precondition -> execute -> step_verification` 执行；回答仅传回文本，不更改 `PermissionBroker` 或授权状态。
- **服务重启与恢复边界**：同 `data_root` 重启会恢复已提交项目/会话/终态 Run 与幂等键（不重置已消费工作区也不重复执行受控项目初始化）；若重启前存在 `queued`/`running`/`cancelling` 或收尾未确认的 `unknown` Run，启动协调事务将其标为 `unknown / HOST_RESTARTED`、关闭 `pending` 提问并持久阻断该工作区（`409 WORKSPACE_BUSY`）；若当前 `HostConfig` 移除了原 `project_root`、`workspace_parent` 变更或出现 reparse，历史仍可读，但新启动返回 `403 PROJECT_BINDING_INVALID`。
- **离线用量、容量展示与无自动删除**：`GET /api/v1/sessions/{sid}/runs/{rid}/usage` 如实统计 `calls_started`/`calls_completed`、`usage_known_calls`/`usage_unknown_calls`、`known_input_tokens`/`known_output_tokens`、`steps_used`/`elapsed_ms`（存在未知调用时总 token 为 `null` 并保留已知小计）；离线固定返回 `estimated_cost_cny=null`、`billed_cost_cny=null`、`cost_status="offline_not_billed"`（不计费，不虚构 ¥0）。页面仅展示主库已用/限额，不提供删除、自动 GC 或 `VACUUM` 操作，用户项目/会话持久数据与开发/验收临时证据严格分离；截图不作为行为验收必需条件。
- **同进程断线重连与锁**：同一 `data_root` 通过 `.host.lock` 持有 Windows 文件独占锁；同进程刷新或重启后重新打开浏览器，可通过 `/snapshot`、`/history`、`/context`、`/questions` 与 `/usage` 同步状态并从 `cursor` 续接 SSE，绝不重复提交已决定提案、回答或启动命令。

## 访问安全边界

- 仅监听并放行精确 `Host: 127.0.0.1:<port>`，无 CORS；API 全量校验 `Authorization: Bearer <token>`（不接受 URL query 传参）、`Origin` 与 `Sec-Fetch-Site`，请求体上限 64 KiB。
- 静态路由仅暴露 `/`、`/app.js`、`/style.css`，禁用 `/docs` 与 `/openapi.json`；严格 CSP（`frame-ancestors 'none'`，禁止外链），所有动态文本、代码、文件路径、命令输出与差异摘要仅使用 `textContent` 渲染（绝不使用 `innerHTML`）。
- `data_root`、`workspace_parent` 与所有 `--project-root` 必须为互不重叠的本机绝对路径，禁止任何祖先路径上的 symlink / junction / reparse point。


## 离线只读连续对话与原执行入口（CHAT-T01）

- **只读对话入口**：在已登记项目的当前会话中，可通过主会话底部输入栏直接发送只读问题（`POST /api/v1/sessions/{sid}/chat/turns`），无需勾选写范围确认、不创建 `run_dir`、不占用工作区写锁。
- **白名单只读工具**：仅允许 `list_directory`、`find_paths`、`read_file`、`search_text`，单轮最多 8 次工具调用；用户输入与助手回复按 UTF-8 8 KiB 有界，模型请求上下文按 16 KiB 有界。
- **持久化与标题投影**：对话轮持久化在当前 SQLite 的附加表 `chat_turns` 中，保留原 `history_items`、`runs` 与 `store_meta/user_version=1` 不变；会话列表标题由最早普通用户消息首行派生（`GET /api/v1/projects/{pid}/chat/session-titles`）。
- **原执行入口保留**：需要修改工作区或执行受信任本机命令时，仍通过独立的“启动运行”入口并经过 `confirmed_scope` 与逐提案审批。
