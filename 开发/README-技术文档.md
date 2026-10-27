# AgentSync — Zcode ⇄ WorkBuddy 双向同步器

把两个 AI 客户端里的**对话历史**和**全局记忆规则**保持一致的本地工具。纯 Python 标准库，没有第三方依赖。

> **目录说明**
> 面向普通用户的入口是根目录的 **`AgentSync.exe`**（双击弹菜单，见 [`../使用说明.md`](../使用说明.md)）。
> 本文件是给维护者看的技术文档，源码都在 `开发/` 下。
> 早期排查期间的一次性脚本（relink / rollback / repair / revert 修复 / 各类 verify）已随问题结案删除；
> 其中仍在使用的能力已并入产品：工具结果形状体检与修复、侧边栏索引孤儿清理
> → `AgentSync.exe` 菜单第 4 项（实现见 `asynclib/zcode.py` 的 `audit_*` / `fix_*`）。

```
python 开发/run.py status           # 看两边各有多少对话、怎么配对的
python 开发/run.py sync             # 双向同步（自动备份）
python 开发/run.py sync --dry-run   # 只看计划，不写任何东西
```

---

## 1. 为什么需要它

Zcode 和 WorkBuddy 的数据结构完全不同：

| | Zcode | WorkBuddy |
|---|---|---|
| 位置 | `~/.zcode/cli/db/db.sqlite` | `~/.workbuddy/workbuddy.db` + `~/.workbuddy/projects/<slug>/<id>.jsonl` |
| 结构 | `session` / `message` / `part` 三层 | 一行 `sessions` 记录 + 一个 jsonl 事件流 |
| 正文 | `part.data` 里按 `type` 分（text / reasoning / tool） | 每行一个事件（message / reasoning / function_call…） |
| 排序 | `message.sequence` + `part.sequence` | UUIDv7 的 `id` + `parentId` 链 |
| 全局规则 | `~/.zcode/AGENTS.md`，由 hook **每条消息**注入 | `user-<账号>-personal/MEMORY.md` + 工作区 `.codebuddy/CODEBUDDY.md`（均**仅首轮**）；装了 `wb-plugin` 后同样做到**每条消息**注入 |

AgentSync 在中间做一层归一化：两边都读成同一套 `Conversation / Event` 模型，再按规则写回去。

---

## 2. 四条需求怎么落地的

| 需求 | 实现 |
|---|---|
| **① 按时间顺序同步；双方都有同一对话时，较新的一方覆盖较旧的一方** | 对话按 `created` 升序处理；配对后比较「最后活动时间」，新的一侧整段覆盖旧的一侧（含正文、思考过程、工具调用与结果）。覆盖 Zcode 侧时按原生信封重建 message，写完再读回校验（详见第 8 节） |
| **② 工作区文件夹名相同 = 同一工作空间** | 取路径 basename 归一化比较；并且**两侧各自只保留一个标准目录**（见下），否则 App 里会出现「两个都叫 日常项目」的分组 |
| **③ 一方有、另一方没有 -> 补齐** | 未配对的会话直接复制到对侧，事件时间戳原样保留 |
| **④ 全局规则双方都适用、都能改、都能备份** | 三份文件 + 一份权威副本做双向同步；单侧改动直接推送，两侧都改按小节三方合并；`memory backup` 一键备份 |

### 写回 Zcode 的关键约束

Zcode 的 `message.data` 是一层**严格信封**，不是「role + text」。它的运行时 resume 时直接读
`tokens.input`、`semantics`、`anchor`、`modelSelection`，少一个就抛
`Cannot read properties of undefined (reading 'input')`，界面表现为会话空白。

所以写入器（`asynclib/zcode.py::_build_rows`）按真实库统计出的字段全集构造：

```
assistant: role time{created,completed} parentID modelId providerId mode planEnabled
           agent path{cwd,root} cost tokens{input,output,reasoning,cache,total}
           finish semantics{origin,kind,uiVisibility,providerVisibility,transcriptVisibility}
           anchor{turnId,historyRoundCount,orderedMessageIds,boundaryMessageId,goalBoundary}
user:      role time{created} agent modelSelection contextSnapshot semantics anchor tools
           metadata{inputIntent} model
```

另外两件配套的事：

- **`session_entry` 必须重建**（`runtime/model_selection`、`runtime/execution_state`、
  `runtime/bash_shell_selection`）。原生会话都有，缺了 resume 同样失败。
- **`tasks-index.sqlite` 的 `tasks` 表要写一行**，否则会话在 db 里但侧边栏看不到。

写入前会把目标会话的 `session` / `message` / `part` / `session_entry` **全部原始行**导出到
`state/backups/<时间戳>/zcode-raw-rows/<sessionId>.json`，可原样回滚（`zcode.restore_session`）。


### 同名工作区必须落到同一个目录（否则「合并」只是看起来合并）

两个 App 的历史列表是**按完整路径分组**的，不是按文件夹名。所以即使两边都叫「日常项目」，
只要路径不同（`D:\你的目录\日常项目` vs `C:\Users\<你>\WorkBuddy\日常项目`），
界面上就仍然是两个分组——这正是「名字一样却没合并」的原因。

所以 AgentSync 给每个工作区定一套**标准目录**：

```json
"workspaces": {
  "日常项目": {
    "zcode": "D:\\你的目录\\日常项目",
    "wb":    "C:/Users/<你>/WorkBuddy/日常项目"
  }
}
```

- 复制对话时一律写进该侧的标准目录，不再沿用源路径
- 已有对话如果落在别的目录，会做一次 `统一目录`（只搬位置，内容一个字不动）
- 路径比较用归一化形式（斜杠方向不影响），写回时保留各 App 原生的写法
  （Zcode 原生是反斜杠，WorkBuddy 是正斜杠）
- 不配置也能跑：自动取该侧「最近活跃会话」所在目录；配了更稳
- 不想统一：`sync --keep-paths`

### 怎么判断「两边是同一个对话」

1. **显式映射**（最可靠）：`state/state.db` 里记着上次同步建立的 `Zcode id ↔ WorkBuddy id`
2. **内容指纹**：前 3 条用户消息（去掉注入噪音）+ 创建时间到分钟，哈希后比对，**在同一工作区内**匹配
3. **跨工作区指纹**：仅当一侧只有唯一候选且创建时间相差 < 10 分钟（历史迁移产物常见）

判定不了就不强行配对——会在计划里标成「待人工」，宁可多一条也不合并错。

### 时间相同但内容不同怎么办

先看上次同步时记录的内容哈希，哪一侧变了就取哪一侧；两边都动过才标记为「待人工」，不覆盖任何东西。

### 删除也会同步（但默认只报告、不动手）

WorkBuddy 用软删除（`deleted_at`），Zcode 是物理删除。这块最容易造成数据损失，所以默认**保守**：

- 配对过的对话，一侧被删 → 计划里标成「对侧已删除」，**默认不删另一侧**，只提示
- 确认要严格一致，加 `--delete` 才会真的把另一侧也删掉
- **墓碑**：对侧已删的对话不会被规则 ③ 又复制回来（除非 `--revive`）；即使 WorkBuddy 把历史行物理清掉，墓碑依然有效

> 这条是踩坑踩出来的：早期版本空计划时没落墓碑，结果下一次同步就把用户删掉的 11 条历史又搬回来了；后来又发现默认自动传播删除太危险（在 WB 里清理几条，Zcode 原件就没了），于是改成默认只报告。

---

## 3. 全局记忆（规则）的同步

```
~/.zcode/AGENTS.md                                ← Zcode 侧；UserPromptSubmit hook 每条消息注入，改完立即生效
~/.workbuddy/user-<账号>-personal/MEMORY.md       ← WorkBuddy 侧【真正生效】用户级记忆，跨所有工作区
<工作区>/.codebuddy/CODEBUDDY.md                  ← WorkBuddy 工作区级规则（8KB 上限），只对该工作区生效
~/.workbuddy/AGENTS.md                            ← 留档镜像（WorkBuddy 不加载此路径，仅便于人肉查看）
state/memory/GLOBAL.md                            ← 权威副本，同时是三方合并的基线
```

> **两端加载机制不一样，这是踩过的坑**：
> - Zcode 装了 `UserPromptSubmit` hook（`~/.zcode/hooks/global-memory.py`），**每条消息前**重新注入，改完下一条消息即生效。
> - WorkBuddy 的对话由 `cli/bin/codebuddy`（`codebuddy-lite-wb.mjs`）执行，hook 引擎**在那里**，支持
>   `UserPromptSubmit`；但 Electron 主进程 `app.asar` 里搜不到该字符串，所以：
>   - `~/.workbuddy/settings.json` 里写 `hooks` **无效**——主进程既不读也不透传；
>   - **唯一入口是插件**：`<pluginRoot>/hooks/hooks.json`（代码里写死的相对路径）。
> - 因此 WorkBuddy 侧要做到"每轮注入"，必须挂一个本地插件 —— 见下节。不挂插件时才回落到首轮注入：
>   - `MemoryCollector.getUserLocalMemoryContent()` 读 `~/.workbuddy/user-<账号>-personal/MEMORY.md`，
>     包成 `<user_memory>` 注入 —— **只有这条路径跨工作区生效**；
>   - `ProjectContextSection` 只按 `cwd` 拼 `CODEBUDDY.md` / `.codebuddy/CODEBUDDY.md` / `AGENTS.md`，
>     **不读家目录**，所以 `~/.workbuddy/AGENTS.md` 放在那里等于没放。

### WorkBuddy 每轮实时注入（wb-plugin）

```
wb-plugin/                                        ← 本地插件市场（type=directory）
  .codebuddy-plugin/marketplace.json
  plugins/global-memory-inject/
    .codebuddy-plugin/plugin.json
    hooks/hooks.json                              ← UserPromptSubmit → node hooks/global-memory.mjs
    hooks/global-memory.mjs                       ← 读盘 + 输出 hookSpecificOutput.additionalContext
```

hook 的输出去向（从 CLI 实测）：`hookSpecificOutput.additionalContext` → `hookSpecificOutput.hookEventName`
匹配后包成 `<system-reminder data-role="hook">`，用 `addSystemReminder(..., "last")` **追加在 prompt 末尾**。

脚本每次调用都重读磁盘，并取「多份副本里 mtime 最新的那份」（内容相同则标注权威副本），所以：

- 在 Zcode 或 WorkBuddy 任一侧改规则 → **下一句话立即生效**，不用重开会话；
- 任何异常静默 exit 0，绝不阻塞对话；
- 每次调用都记一行到 `state/logs/wb-hook.log`，便于确认是否真的跑了。

```bash
python install_wb_hook.py            # 注册/更新插件（改注册表前自动备份）
python install_wb_hook.py --dry-run  # 只看要改什么
python install_wb_hook.py --uninstall
```

注册要落三处，缺一不生效：

| 文件 | 作用 |
|---|---|
| `plugins/known_marketplaces.json` | 市场来源（`type=directory` 指向 `wb-plugin`） |
| `plugins/installed_plugins.json` | 安装记录（`installPath` 指向插件根） |
| `settings.json` → `enabledPlugins` | `"global-memory-inject@wb-local-hooks": true` |

> 注册完成后**必须重启 WorkBuddy**（插件在进程启动时加载）。之后改规则不再需要重启。

**WorkBuddy 会把插件复制到缓存再运行**（踩过的坑）：

```
源目录   <项目根>\wb-plugin\plugins\global-memory-inject
运行时   ~\.workbuddy\plugins\cache\wb-local-hooks\global-memory-inject\1.0.0\
```

所以脚本里**不能**用「上四级目录」推算 AgentSync 根目录——缓存副本算出来是 `plugins\cache`，
结果是权威副本找不到、日志写进 `plugins\cache\state\logs`、来源标成留档镜像。
现在的做法是逐层验证「有 `state/memory/GLOBAL.md` 才算数」，并额外写三处标记文件：

| 标记 | 位置 |
|---|---|
| 插件内标记 | `<插件>/hooks/agentsync-root.txt`（随副本一起被复制） |
| 家目录标记 | `~\.workbuddy\agentsync-root.txt` |

`install_wb_hook.py` 还会把当前 `global-memory.mjs` 直接覆盖到缓存副本 —— hook 是**每次调用才起进程读文件**，
所以改脚本不用重启；验证时**源目录和缓存副本都要各跑一遍**，只验前者会漏掉这类问题。


同步逻辑：

| 情况 | 行为 |
|---|---|
| 只有一侧改过 | 该侧内容成为权威，推给另一侧 |
| 两侧都改过，但改的是不同小节 | **三方合并**，双方改动都保留 |
| 两侧改了同一小节 | 冲突：按文件 mtime 取较新一侧，并打印冲突小节名 |
| 权威副本被手工改 | 下发到两侧 |
| 谁都没改 | 不写任何文件 |

合并以 `##` 小节为单位，所以「Zcode 里加一条规则、WorkBuddy 里改另一条」不会互相覆盖。

```bash
python run.py memory status         # 四个位置现状（含是否可被加载）+ 差异统计
python run.py memory diff           # 列出新增/删除/修改的小节
python run.py memory sync           # 双向同步（--prefer wb 指定冲突优先侧）
python run.py memory backup         # 带时间戳备份
python install_wb_hook.py           # 注册/更新 WorkBuddy 每轮注入插件（详见上节）
```

---

## 4. 命令一览

```bash
python run.py doctor              # 体检：路径、进程、两边会话数
python run.py status [-v]         # 工作区分布 / 配对明细 / 单侧独有的会话
python run.py sync [--dry-run]    # 双向同步（默认会问一次确认）
        --yes                      # 不问，直接执行
        --workspace 日常项目        # 只同步某个工作区
        --revive                   # 忽略墓碑，把对侧删过的恢复回来
        --delete                   # 严格一致：一侧删了，另一侧也删（默认只报告不删）
        --no-delete                # 显式关闭删除传播（与默认行为一致）
        --no-memory                # 只同步对话，不动规则
        --project-rules            # 同时写 <工作区>/.codebuddy/CODEBUDDY.md
        --keep-paths               # 不统一工作区目录（默认会统一）
        --force-open               # Zcode 运行中也强行写它的库（默认拒绝）
python run.py memory [status|diff|sync|backup|paths]
python run.py backup              # 立刻快照两侧数据库 + 三份规则文件
python run.py backups             # 列出历史备份
python run.py reset-baseline      # 清空哈希基线（升级比较算法后跑一次）
```

不方便开终端的话，直接双击根目录的 **`AgentSync.exe`**，按回车即等同于 `python run.py sync`。

建议日常就用一条：

```bash
python run.py sync --yes
```

同步前会**自动**用 sqlite 在线备份接口给两侧数据库取一致快照（WAL 模式下直接拷文件会撕裂），写入目标会话前还会把被覆盖的对话导出成 JSON，放在 `state/backups/<时间戳>/`。

---

## 5. 目录结构

```
AgentSync/
├── AgentSync.exe          exe 入口（双击弹菜单；不入库，需自行打包）
├── 使用说明.md             面向普通用户的操作说明
├── config.example.json    配置模板（复制成 config.json 后按需修改）
├── .agentsync-root        根目录标记（源码模式靠它定位项目根）
├── wb-plugin/             WorkBuddy 本地插件市场（type=directory）
│   ├── .codebuddy-plugin/marketplace.json
│   └── plugins/global-memory-inject/{.codebuddy-plugin/plugin.json, hooks/}
├── state/                 运行时自动生成
│   ├── state.db           配对与墓碑（删了等于失忆，会重新全量配对）
│   ├── memory/GLOBAL.md   规则权威副本
│   ├── logs/wb-hook.log   WB 每轮注入 hook 的调用记录
│   └── backups/           数据库快照与规则备份（默认保留最近 5 份）
├── logs/
└── 开发/
    ├── run.py             命令行入口
    ├── launcher.py        exe 的中文菜单入口
    ├── selftest.py        42 项自测，全部跑在数据库副本上
    ├── install_wb_hook.py 注册 WorkBuddy 每轮注入插件（自动备份注册表）
    ├── 重新打包.bat        一键重建 exe
    ├── README-技术文档.md   本文件
    └── asynclib/
        ├── paths.py       路径与配置
        ├── console.py     Windows 控制台编码（控制台强制 utf-8，否则必乱码）
        ├── model.py       统一模型（Conversation / Event / 指纹 / UUIDv7）
        ├── state.py       状态库（配对映射、内容哈希、墓碑、运行记录）
        ├── zcode.py       Zcode 读写适配器（含体检与修复）
        ├── workbuddy.py   WorkBuddy 读写适配器
        ├── sync.py        配对 + 冲突判定 + 计划 + 执行
        └── memory.py      全局规则双向同步 + 三方合并 + 备份
```

**运行环境**：Python 3.8+（只用标准库，无需 pip 安装任何东西）。已验证 3.8 与 3.13 都能跑。

---

## 6. 已知限制（重要）

1. **同步后需要重启对应客户端**：两边都把会话列表缓存在内存里，直接写库后不重启看不到新会话（数据本身是好的）。
2. **WorkBuddy 有云端同步**：新建的会话会被它 import 到云端；在 UI 里删除也会走云端接口。AgentSync 只操作本地库，删除传播依赖本地状态，云端一侧由 WorkBuddy 自己处理。
3. **WorkBuddy 规则有长度上限**：工作区 `.codebuddy/CODEBUDDY.md` 在 8000 字节处截断（`memory.max_rules_bytes` 可调），用户级 `MEMORY.md` 在 4000 字符处截断；超限会提示精简。当前全局记忆约 2.7KB，余量充足。
4. **正在进行的会话：只读不写**。WorkBuddy 里 `pending/working/planning` 状态的会话
   **可以作为来源**（读一份快照推到 Zcode，这样聊到一半也能同步过去），
   但**永远不会往它里面写**（那会打断正在生成的回答）。这一侧在线时以它为准，避免时间戳抖动导致来回覆盖。
5. **子代理会话默认不迁**：Zcode 的 `subagent_child` 是父会话内的派生上下文，`include_subagents: true` 可打开。
6. **已归档/已删除的会话不参与同步**（用户明确要求）：Zcode 侧的归档状态**只记在
   `tasks-index.sqlite` 的 `archived`/`deleted` 列**（`session.time_archived` 实测全为空），
   读取时按它过滤；WorkBuddy 侧看 `sessions.deleted_at`。归档的会话同时会被记为墓碑，
   避免对侧把它当成「缺失」重新复制过去（否则会出现「一个对话变成两个」）。
   在 Zcode 里取消归档后会自动恢复同步。
7. **写 Zcode 必须先退出它**：`sync.require_zcode_closed` 默认 `true`。Zcode 持有连接与内存状态，
   运行中写它的库可能被退出时覆盖回去。要强行写入加 `--force-open`（风险自负）。
8. **token 用量优先取真实记录，取下不到才估算**：WorkBuddy 的 jsonl 在 `message.usage` /
   `providerData.rawUsage` 里带真实用量（含 `cache_read_input_tokens`、
   `completion_tokens_details.reasoning_tokens`），统一归一到 Zcode 的
   `tokens{input,output,reasoning,cache.read,total}`；模型名取 `providerData.requestModelId`。
   两者都没有时才按内容估算（`zcode.estimate_tokens` 可关成 0），且**不累加历史上下文**
   ——累加会把上下文虚报成几十万，UI 上会显示「上下文将满」。
   覆盖 Zcode 原生会话时，它自己的用量记录会被换成 WorkBuddy 的值；原始行整份落在
   `state/backups/<时间戳>/zcode-raw-rows/`，可回滚。
9. **`selftest.py` 用的是真实库快照**：真实环境里会话增减会让断言的会话数跟着变化，但用例本身是确定的
   （沙盒里已放开 `allow_overwrite` 与 `require_zcode_closed`）。**沙盒必须覆盖 `tasks_index`**，
   否则自测建的测试会话会直接出现在用户真实的 Zcode 列表里（S10 就是守这条的）。


---

## 7. 自测

```bash
python selftest.py          # 42 项，跑在 state/selftest 的库副本上；失败时保留沙盒便于排查
```

`selftest.py` 覆盖：单侧补齐（两个方向）、较新覆盖较旧（两个方向）、幂等、删除传播、墓碑防复活、
规则单侧改动推送、规则两侧改动合并、工作区规则落位、同名工作区目录统一，以及
**S9 写入器回归**（信封完整性 / `tokens.input` 是数字 / `session_entry` 重建 / 写入后读回哈希与源一致 /
正文逐条无编码损坏 / 覆盖前快照可回滚 / token 用量原样取自源记录 / modelId 取真实模型名 / 覆盖仍幂等），
**S12 工具结果形状回归**（写出的数据必须能通过 `audit_tool_state` / error 状态必须带字符串 error /
`"null"` 字面量必须被归一化）。

---

## 8. 踩过的坑（都已在代码里修掉）

| 现象 | 根因 | 修法 |
|---|---|---|
| **Zcode 列表里出现 `S1-仅Zcode有` / `S2-仅WorkBuddy有`，点开是空白或报 `The "string" argument must be of type string…`** | 自测的 `cfg` 没写 `zcode.tasks_index`，`load_config()` 与 DEFAULTS 合并后落到了**真实路径**，于是每次跑 selftest 都往真 Zcode 侧边栏索引里插测试会话（累计 33 条）。索引里有 task、session 表里没有 → 点开读不到 session，渲染时把 `undefined` 当 string 用 | 沙盒 cfg 显式写 `tasks_index` 并复制快照；新增 S10 断言「所有落地路径必须在 state/selftest 下」；`clean_tasks_index.py` 清理历史孤儿（只删 task 存在但 session 不存在的行） |
| **用户发言被扭曲成 `<user_query>修</user_query>` 一大串** | `strip_harness_noise` 只认 `system-reminder`，没处理 `user_query` / `cb_summary` / `previous_*` 这些**包裹用户输入**的标签，原样写进对侧就成了正文 | 区分两类：包裹类**剥壳保内容**（`<user_query>修</user_query>` → `修`），摘要结构类**只删标签**；同时**不做**宽泛的"删掉所有尖括号块"——用户会在正文里写 `<td>`/`<div>` 或讨论 `<user_memory>` 这种标签，宽泛匹配会把真实内容吃掉 |
| **一个对话变成好几个** | 归档的会话被排除后，对侧那条被判为「缺失」而重新补齐；加上孤儿 task 残留，列表里同标题出现十几条 | `only_w` 分支也查墓碑（归档会话的指纹同样记墓碑）；`upsert_task_index` 不再强制把 `archived` 置 0 |
| **写入后 UI 显示「上下文 56 万 / 将满」** | 早期按「累计所有历史消息的字节数 / 3」估算 input，越到后面越离谱 | 改为优先读 WorkBuddy 的真实用量（`message.usage` / `providerData.rawUsage`）；没有时才估算，且不再累加历史 |
| **同步过去的对话满是 `�`** | **不是写入造成的**：WorkBuddy 的 jsonl 里本来就有（该会话 1227 处），源头是当年 PowerShell 输出被按错编码解码后原样存进了对话记录，U+FFFD 不可逆 | 无解，只能等这条对话被新内容覆盖；已确认写入链路 UTF-8 往返无损（S9 断言正文逐条一致） |
| **Zcode 历史会话打开是空白 / `Cannot read properties of undefined (reading 'input')`** | 写入器只写 `{role, time, agent}` 四个字段就当成 message 存了。Zcode 的 `message.data` 是**严格信封**，resume 时直接读 `tokens.input`、`semantics`、`anchor`、`modelSelection`，缺一个就崩 | 按真实库统计出的字段全集重建信封：`tokens{input,output,reasoning,cache,total}`、`cost`、`path{cwd,root}`、`finish`、`semantics{origin,kind,...}`、`anchor{turnId,origin}`、`modelSelection`、`contextSnapshot`；并重建 `session_entry` 三条运行时状态；覆盖前留行级全量快照 |
| **每次同步都判定「对方更新」，反复重写** | 两侧对同一内容的**线性顺序必然不同**：WB 是 `callA callB resultA resultB`，Zcode 的 part 结构天生是 `callA resultA callB resultB`；另外 WB 的 jsonl 行序 ≠ 时间序（会话压缩插入的摘要消息尤其明显） | `content_hash()` 先按 `ts` 排序，再把连续工具段**按 call_id 聚合**后比较；JSON 参数按语义（sorted keys）比较；空 status 与 `completed` 视为同义 |
| **新同步过去的会话在 Zcode 侧边栏看不到** | 列表数据源是 `~/.zcode/v2/tasks-index.sqlite` 的 `tasks` 表，不在那里就不显示 | 新增 `upsert_task_index()`，写内容后同步维护索引行 |
| 双击运行满屏乱码 | 把 stdout 的 `encoding` 改成了 `cp936`（跟随代码页）。Windows 控制台流 `_WindowsConsoleIO` **只接受 UTF-8 字节**再转 UTF-16 输出，喂 GBK 必然乱码 | 控制台一律用 `utf-8`；只有重定向到文件时才跟随系统编码。`.bat` 也清掉了会破坏控制台直写的 `PYTHONIOENCODING` |
| 正在聊的这个对话同步不过去 | 早期把「进行中」的会话整条跳过，连「读出来复制」也一起禁了 | 区分方向：可读不可写。并从源头消除「刚写完时间戳反而更新」的抖动 |
| 两边同名工作区「没合并」 | 复制时沿用**源**路径，导致 WB 里出现 `C:\...\日常项目` 与 `D:\...\日常项目` 两个同名分组 | 新增「工作区标准目录」，复制/迁移后统一落位 |
| 反复判定「对方更新」 | Zcode 同一时刻多个工具调用，结果时间戳 `+1ms` 导致读回顺序抖动 → 内容哈希不稳 | 结果与调用同时间戳，排序靠稳定排序 |
| 删掉的历史又冒出来 | 空计划时提前 return，墓碑没落库 | 进入执行函数先落墓碑 |
| 覆盖已有 Zcode 会话必失败 | `part` 插入 7 列只写了 6 个 `?`，异常还被吞 | 修好占位符并回显错误 |
