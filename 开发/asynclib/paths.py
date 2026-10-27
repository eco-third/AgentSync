"""路径与配置：所有位置集中在这里，换机/换目录只改这一处。"""
import json
import os
import sys

HOME = os.path.expanduser("~")

#: 根目录标记文件（隐藏文件，用于源码模式定位项目根；exe 模式不依赖它）
ROOT_MARK = ".agentsync-root"


def _root():
    """确定程序根目录。

    · **exe 模式**：exe 所在目录。PyInstaller 单文件包会把代码解压到临时目录，
      `__file__` 指向那里，绝不能拿它当根目录 —— 否则配置和备份会写进临时目录，
      程序一退出就没了。
    · **源码模式**：从本文件逐级向上找带 ``config.json`` 或根标记的目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        if (os.path.exists(os.path.join(d, "config.json"))
                or os.path.exists(os.path.join(d, ROOT_MARK))):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_DIR = _root()
# 允许用环境变量指向另一套配置/状态（自测与多环境用；正常使用不用管）
STATE_DIR = os.environ.get("AGENTSYNC_STATE_DIR") or os.path.join(PROJECT_DIR, "state")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")
MEMORY_DIR = os.path.join(STATE_DIR, "memory")
STATE_DB = os.path.join(STATE_DIR, "state.db")
CONFIG_FILE = os.environ.get("AGENTSYNC_CONFIG") or os.path.join(PROJECT_DIR, "config.json")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")

DEFAULTS = {
    # ---- Zcode 侧 ----
    "zcode": {
        "db": "~/.zcode/cli/db/db.sqlite",
        # 侧边栏列表的数据源（不在这个索引里的会话，db 里有但界面看不到）
        "tasks_index": "~/.zcode/v2/tasks-index.sqlite",
        "rules_file": "~/.zcode/AGENTS.md",
        "include_subagents": False,
        # 允许 WorkBuddy 覆盖 Zcode 侧已有会话。
        # 早期为 true 会写坏信封（丢 tokens/cost/semantics），已修写入器；
        # 仍保留开关，覆盖前会做全量行级快照（见 snapshot_session）。
        "allow_overwrite": True,
        # 是否写入 tasks 索引（关掉的话新会话在 Zcode 列表里看不到）
        "write_task_index": True,
        # 是否按内容估算 token 用量（关掉则一律写 0）
        "estimate_tokens": True,
    },
    # ---- WorkBuddy 侧 ----
    "workbuddy": {
        "db": "~/.workbuddy/workbuddy.db",
        "projects_dir": "~/.workbuddy/projects",
        # 真正生效的用户级规则：~/.workbuddy/user-<账号>-personal/MEMORY.md
        # 留空 = 自动发现（见 resolve_wb_user_memory）；也可写死绝对路径。
        "rules_file": "",
        # 留档镜像：WorkBuddy 不读取该路径（已核实程序内无任何代码加载它），
        # 仅作人肉备份，避免旧版工具链找不到文件。
        "mirror_file": "~/.workbuddy/AGENTS.md",
        "write_project_rules": True,
        "project_rules_relpath": ".codebuddy/CODEBUDDY.md",
    },
    "sync": {
        # 跳过正在运行的会话（避免写坏活跃对话）
        "skip_active_sessions": True,
        # 是否允许跨工作区按指纹配对（历史迁移产物常见）
        "allow_cross_workspace_match": True,
        # 时间戳差异小于该值视为「一样新」，不互相覆盖
        "equal_tolerance_ms": 1000,
        # 删除是否双向传播。默认 False：只报告不删，避免误删另一侧历史；
        # 想严格一致时用 sync --delete 显式开启
        "propagate_delete": False,
        # 数据库快照保留份数
        "backup_keep": 5,
        # 同名工作区是否统一目录（不统一的话 App 里会出现两个同名分组）
        "normalize_workspace_paths": True,
        # Zcode 运行时禁止写它的库：它持有连接与内存状态，退出时可能把改动覆盖掉。
        # 关掉（false）则允许在运行时写入，风险自负。
        "require_zcode_closed": True,
    },
    # 工作区标准目录：文件夹名 -> 两侧各自的实际目录。
    # 留空则自动推断（取该侧最近活跃会话所在的目录）
    "workspaces": {},
    "memory": {
        "backup_on_change": True,
        "max_rules_bytes": 8000,   # WorkBuddy 项目上下文的上限
    },
}


def _expand(p):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p)))


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))          # 深拷贝
    if os.path.exists(CONFIG_FILE):
        try:
            user = json.load(open(CONFIG_FILE, encoding="utf-8"))
        except Exception:
            user = {}
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def ensure_dirs():
    for d in (STATE_DIR, BACKUP_DIR, MEMORY_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


def resolve_wb_user_memory(cfg=None):
    """定位 WorkBuddy 的用户级记忆文件（跨工作区生效）。

    WorkBuddy 程序内 IdentityContextSection / readLocalMemoryFiles 读取的是
    ``~/.workbuddy/user-<账号>-personal/MEMORY.md``，这是唯一能跨工作区生效的
    规则载体。账号目录名带 UUID，因此这里自动发现，而不是写死。
    """
    explicit = ((cfg or {}).get("workbuddy", {}) or {}).get("rules_file") or ""
    if explicit.strip():
        return _expand(explicit)

    base = os.path.join(HOME, ".workbuddy")
    try:
        names = os.listdir(base)
    except OSError:
        return ""
    cands = sorted(n for n in names
                   if n.startswith("user-") and n.endswith("-personal")
                   and os.path.isdir(os.path.join(base, n)))
    if not cands:
        return ""
    # 优先已有 MEMORY.md 的目录；多个时取最近修改的
    with_mem = [n for n in cands if os.path.exists(os.path.join(base, n, "MEMORY.md"))]
    if with_mem:
        with_mem.sort(key=lambda n: os.path.getmtime(os.path.join(base, n, "MEMORY.md")),
                      reverse=True)
        pick = with_mem[0]
    else:
        pick = cands[0]
    return os.path.join(base, pick, "MEMORY.md")


def cfg_paths(cfg):
    """把配置里的路径全部展开成绝对路径。"""
    wb_cfg = cfg.get("workbuddy", {}) or {}
    mirror = wb_cfg.get("mirror_file") or ""
    idx = cfg["zcode"].get("tasks_index") or ""
    return {
        "zcode_db": _expand(cfg["zcode"]["db"]),
        "zcode_tasks_index": _expand(idx) if idx.strip() else "",
        "zcode_rules": _expand(cfg["zcode"]["rules_file"]),
        "wb_db": _expand(cfg["workbuddy"]["db"]),
        "wb_projects": _expand(cfg["workbuddy"]["projects_dir"]),
        "wb_rules": resolve_wb_user_memory(cfg),
        "wb_mirror": _expand(mirror) if mirror.strip() else "",
    }
