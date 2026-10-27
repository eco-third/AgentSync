# AGENTS.md

给在本仓库工作的 AI 协作者（以及人）看的约定。**动手前请先读完"不可违反的约束"一节**，那里每一条都是线上事故换来的。

---

## 项目是什么

Windows 上的本地同步工具：把 Zcode 和 WorkBuddy 两个 AI 客户端的**对话历史**与**全局记忆规则**双向拉平。
它会直接读写这两个客户端的 SQLite / JSONL 数据，**写错会让用户的会话打不开**，所以保守优先。

- 语言：Python 3.8+，**只用标准库**，不许加第三方依赖（打包体积、离线可用性都是硬要求）
- 平台：只支持 Windows（路径归一化、控制台编码、注册表按 Windows 写）
- 注释与文档：中文

---

## 不可违反的约束

### 1. 工具结果的状态字段是「会话能不能打开」的开关

Zcode 重放历史时走 `buildToolOutput(e, t)`，第一行就是 `Buffer.byteLength(e.content, "utf8")`：

- `status === "completed"` → `content` 取 `state.output`
- `status === "error"` → `content` 取 `state.error`

任一侧是 `undefined` → 抛 `ERR_INVALID_ARG_TYPE` → **整个 `v4/conversation/subscribe` 失败** →
会话点开空白并弹 `The "string" argument must be of type string…Received undefined`。

所以：**写工具结果时 `status != "completed"` 必须带非空字符串 `state.error`**，
`completed` 必须带字符串 `state.output`。相关函数：`zcode._new_tool_state` / `zcode._apply_tool_result`。

### 2. 归一化必须在事件入口，不能只在写入时

`"null"` / `"undefined"` / `"None"` 字面量、控制字符的清洗都放在 `Event.__init__`（`model.norm_output`）。
只在写入侧清洗会导致「源算哈希 → 写入清洗 → 读回不等」，内容哈希漂移，每次同步都判定"对方又更新了"而反复重写。
`selftest.py` 的 S9 / S12 专门抓这个。

### 3. 幂等性靠两处归一化，别动

- **工具段按 `call_id` 聚合比较**：WorkBuddy 是 `callA callB resultA resultB`，Zcode 的 part 结构天生是 `callA resultA callB resultB`，线性顺序永远不同
- **`content_hash` 先按 `ts` 排序**：WorkBuddy 的 JSONL 行序 ≠ 时间序（压缩摘要消息位置完全错开）
- JSON 参数按 sorted keys 比较；空 status 与 `completed` 同义

### 4. 写 Zcode 的 message 必须重建完整信封

`message.data` 是**严格信封**，resume 时直接读 `tokens.input`、`semantics`、`anchor`、`modelSelection`，
少一个就抛 `Cannot read properties of undefined (reading 'input')`，界面同样表现为空白。
字段全集见 `zcode._build_rows`，并且必须同时重建 `session_entry` 三条运行时状态
（`runtime/model_selection`、`runtime/execution_state`、`runtime/bash_shell_selection`）。

### 5. 新会话要写 tasks 索引

Zcode 侧边栏的数据源是 `~/.zcode/v2/tasks-index.sqlite` 的 `tasks` 表（列名 `task_id`，另有 `archived` / `deleted`）。
不在索引里的会话：db 里有，界面看不到。归档状态**只**记在那两列（`session.time_archived` 实测全空）。

### 6. 根目录不能用 `__file__` 推

单文件 exe 会被 PyInstaller 解压到 `%TEMP%`，`__file__` 指向那里 —— 配置和备份会写进临时目录，退出即丢。
一律用 `paths.PROJECT_DIR`（exe 模式取 `sys.executable` 所在目录；源码模式向上找 `config.json` 或 `.agentsync-root`）。

### 7. Windows 控制台一律 UTF-8；`.bat` 一律 ASCII + CRLF

`_WindowsConsoleIO` 只接受 UTF-8 字节再转 UTF-16，喂 GBK 必乱码。`.bat` 里的中文和行尾混用会直接执行失败，
改动请用 Python 写文件（`newline="\r\n"`），不要用编辑器随手改。

---

## 模块地图

| 文件 | 职责 | 什么时候改它 |
|---|---|---|
| `开发/asynclib/paths.py` | 根目录定位、默认配置、路径展开 | 新增配置项、改目录规则 |
| `开发/asynclib/model.py` | `Conversation` / `Event`、指纹、UUIDv7、输出归一化、`strip_harness_noise` | 改事件模型、改清洗规则 |
| `开发/asynclib/state.py` | 配对映射、内容哈希基线、墓碑 | 改配对/删除逻辑 |
| `开发/asynclib/zcode.py` | Zcode 读写 + 体检修复（`audit_tool_state` / `fix_tool_state` / `audit_orphan_tasks`） | 改 Zcode 侧任何东西 |
| `开发/asynclib/workbuddy.py` | WorkBuddy 读写（JSONL 解析、用量提取） | 改 WorkBuddy 侧 |
| `开发/asynclib/sync.py` | 配对 → 冲突判定 → 计划 → 执行 | 改同步策略 |
| `开发/asynclib/memory.py` | 规则双向同步、按 `##` 小节三方合并 | 改记忆同步 |
| `开发/launcher.py` | exe 菜单 | 改交互 |
| `开发/run.py` | 命令行（菜单复用它的实现） | 新增子命令 |

## 想加一个功能，先看这里

- **新增同步规则** → `sync.py` 的 `build_plan` / `decide`，并在 `selftest.py` 加对应断言
- **新增体检项** → `zcode.audit_*`，同时挂进 `run.py doctor` 与菜单第 4 项
- **新增配置项** → `paths.DEFAULTS` + `config.example.json`，两处都要改
- **改 Zcode 写入** → 先跑 `python 开发/selftest.py`，S9 会验证「写入后读回哈希与源一致」

---

## 验证（改完必须做）

```bash
python 开发/selftest.py        # 42 项，跑在 state/selftest 的库副本上
python 开发/run.py doctor      # 真实环境体检：路径 / 进程 / 会话数 / 工具结果形状 / 索引孤儿
python 开发/run.py sync --dry-run   # 只看计划，不写任何东西
```

- **自测必须全绿**才提交。有任何一项红，先查是不是自己的改动破坏了幂等（S9 / S12 最常见）。
- 沙盒必须覆盖 `tasks_index`（S10 守这条）—— 否则自测建的测试会话会直接出现在用户真实的 Zcode 列表里。
- **不要在真实数据上做实验**。要复现就复制数据库到 `state/selftest/` 或临时目录再动；
  验证写入器请挑**已经结束**的会话，拿正在聊的对话当基准，往返永远不一致。

## 打包

```bash
开发\重新打包.bat              # 需要 PyInstaller；产物是仓库根目录的 AgentSync.exe
```

打包前记得：`--onefile --console`，并且**不要把 exe 提交进仓库**（`.gitignore` 已排除）。

---

## 禁止事项

- 不要提交 `state/`、`logs/`、`config.json`（都含本机路径与个人数据，已在 `.gitignore`）
- 不要引入 pip 依赖
- 不要用「删掉所有尖括号块」这类宽泛正则清洗正文 —— 用户会真的在正文里写 `<div>` 或讨论 `<user_memory>`。
  `strip_harness_noise` 只处理确定的 harness 标记：整块删 `<system-reminder ...>`、剥壳保内容 `<user_query>…</user_query>`、摘要标签只删标签
- 不要在 Zcode 运行时写它的库（`sync.require_zcode_closed` 默认 `true`）
- 不要为了"让测试通过"去改断言 —— 断言红了通常意味着真的坏了一个不变量
