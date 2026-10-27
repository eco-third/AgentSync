"""全局记忆（规则）双向同步。

三份活文件 + 一份留档 + 一份权威副本：
    Zcode 侧        ~/.zcode/AGENTS.md
                    ← UserPromptSubmit hook 每条消息实时注入，改完立即生效
    WorkBuddy 侧    ~/.workbuddy/user-<账号>-personal/MEMORY.md
                    ← WorkBuddy 程序 IdentityContextSection 读取，会话首轮注入，跨所有工作区生效
    工作区规则      <workspace>/.codebuddy/CODEBUDDY.md
                    ← ProjectContextSection 读取（stage=first_turn，8KB 上限），只对该工作区生效
    留档镜像        ~/.workbuddy/AGENTS.md
                    ← WorkBuddy 并不读取该路径，仅保留以便人肉查看/旧工具兼容
    权威副本        <项目>/state/memory/GLOBAL.md

注意：WorkBuddy 的两条通道都只在「会话首轮」注入，没有 UserPromptSubmit 之类
的每轮钩子，因此改完必须新开会话才生效；这与 Zcode 的实时注入有本质差别。

规则（对应需求 4：双方都适用、双方都能改、双方都能备份）：
    · 只有一侧改过 -> 该侧内容成为权威，推给另一侧
    · 两侧都改过   -> 按「## 小节」合并，冲突小节取文件 mtime 较新者，并留冲突副本
    · 谁都没改     -> 什么都不做
    · 每次写入前先备份到 state/backups/memory/<时间戳>/
"""
import hashlib
import os
import re
import shutil
import time

from . import paths, state


def h(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16] if text is not None else None


def read_text(p):
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8-sig") as f:
            return f.read()
    except Exception:
        return None


def write_text(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, p)


# ---------------------------------------------------------------- 解析
SEC_RE = re.compile(r"^(#{1,2})\s+(.*)$")


def split_sections(text):
    """按 # / ## 标题切小节，返回 [(key, 原文片段)]，key 为标题（无标题的引言段 key=''）。"""
    if not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    out, cur_key, buf = [], "", []
    for ln in lines:
        m = SEC_RE.match(ln)
        if m:
            out.append((cur_key, "\n".join(buf).strip("\n")))
            cur_key = m.group(2).strip()
            buf = [ln]
        else:
            buf.append(ln)
    out.append((cur_key, "\n".join(buf).strip("\n")))
    return [(k, v) for k, v in out if v.strip()]


def merge_sections(a, b, prefer):
    """a/b 为两侧正文，prefer ∈ {'zcode','wb'} 决定冲突小节取谁。"""
    sa, sb = split_sections(a), split_sections(b)
    ka = [k for k, _ in sa]
    kb = [k for k, _ in sb]
    da, db = dict(sa), dict(sb)
    order = ka + [k for k in kb if k not in da]
    parts, conflicts = [], []
    for k in order:
        va, vb = da.get(k), db.get(k)
        if va is not None and vb is not None:
            if va.strip() == vb.strip():
                parts.append(va)
            else:
                conflicts.append(k)
                parts.append(vb if prefer == "wb" else va)
        else:
            parts.append(va if va is not None else vb)
    return "\n\n".join(p.strip("\n") for p in parts if p and p.strip()) + "\n", conflicts


def merge_three_way(base, a, b, prefer):
    """三方合并：base 是上次同步时的权威内容。

    逐个小节判断「谁改的」：只有一侧相对 base 变了就取那一侧；两侧都变了才算冲突。
    这样「两侧各改不同小节」能同时保留，而不是整篇被一侧覆盖。
    """
    sbase = dict(split_sections(base))
    sa, sb = dict(split_sections(a)), dict(split_sections(b))
    order = list(sbase.keys())
    for k in list(sa.keys()) + list(sb.keys()):
        if k not in order:
            order.append(k)
    parts, conflicts = [], []
    for k in order:
        va, vb, vo = sa.get(k), sb.get(k), sbase.get(k)
        if va == vb:
            chosen = va
        elif va == vo:              # 只有 b 改过
            chosen = vb
        elif vb == vo:              # 只有 a 改过
            chosen = va
        else:                       # 都改过 -> 冲突
            conflicts.append(k)
            chosen = vb if prefer == "wb" else va
        if chosen is not None and chosen.strip():
            parts.append(chosen)
    return "\n\n".join(p.strip("\n") for p in parts if p.strip()) + "\n", conflicts


def diff_summary(a, b):
    sa = {k: v for k, v in split_sections(a)}
    sb = {k: v for k, v in split_sections(b)}
    added = [k for k in sb if k not in sa]
    removed = [k for k in sa if k not in sb]
    changed = [k for k in sa if k in sb and sa[k].strip() != sb[k].strip()]
    return {"added": added, "removed": removed, "changed": changed}


# ---------------------------------------------------------------- 备份
def backup(tag=None, extra_files=None):
    tag = tag or time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(paths.BACKUP_DIR, "memory", tag)
    os.makedirs(d, exist_ok=True)
    cfg = paths.load_config()
    cfgp = paths.cfg_paths(cfg)
    saved = []
    for label, p in (("zcode-AGENTS.md", cfgp["zcode_rules"]),
                     ("workbuddy-MEMORY.md", cfgp["wb_rules"]),
                     ("workbuddy-mirror-AGENTS.md", cfgp.get("wb_mirror") or ""),
                     ("canonical-GLOBAL.md", os.path.join(paths.MEMORY_DIR, "GLOBAL.md"))):
        if p and os.path.exists(p):
            dst = os.path.join(d, label)
            shutil.copy2(p, dst)
            saved.append(dst)
    for p in (extra_files or []):
        if p and os.path.exists(p):
            name = "workspace-" + hashlib.sha1(p.encode()).hexdigest()[:8] + "-" + os.path.basename(p)
            shutil.copy2(p, os.path.join(d, name))
            saved.append(os.path.join(d, name))
    return d, saved


# ---------------------------------------------------------------- 主流程
def sync(prefer=None, dry_run=False, verbose=True, project_workspaces=None):
    """返回 dict: {action, canonical_hash, pushed:[...], conflicts:[...], backup}"""
    cfg = paths.load_config()
    cfgp = paths.cfg_paths(cfg)
    canon_path = os.path.join(paths.MEMORY_DIR, "GLOBAL.md")

    z_text = read_text(cfgp["zcode_rules"])
    w_text = read_text(cfgp["wb_rules"])
    c_text = read_text(canon_path)
    st = state.get_memory_state(state.connect())

    zh, wh, ch = h(z_text), h(w_text), h(c_text)
    z_changed = z_text is not None and zh != st.get("z_hash")
    w_changed = w_text is not None and wh != st.get("w_hash")
    c_changed = c_text is not None and ch != st.get("canonical_hash")

    res = {"action": "", "conflicts": [], "pushed": [], "backup": None,
           "z_changed": z_changed, "w_changed": w_changed, "c_changed": c_changed}

    # 首次运行：没有任何历史
    first = all(st.get(k) is None for k in ("canonical_hash", "z_hash", "w_hash"))

    if first:
        if z_text and w_text and h(z_text) != h(w_text):
            base, conflicts = merge_sections(z_text, w_text, prefer or "zcode")
            res["action"] = "首次运行：两侧内容不同，已按小节合并"
            res["conflicts"] = conflicts
        else:
            base = z_text or w_text or c_text or ""
            res["action"] = "首次运行：以现有内容为基线"
    elif c_changed and not z_changed and not w_changed:
        base = c_text
        res["action"] = "权威副本被手工修改 -> 下发到两侧"
    elif z_changed and not w_changed:
        base = z_text
        res["action"] = "Zcode 侧有改动 -> 推送到 WorkBuddy"
    elif w_changed and not z_changed:
        base = w_text
        res["action"] = "WorkBuddy 侧有改动 -> 推送到 Zcode"
    elif z_changed and w_changed:
        prefer = prefer or _newer_side(cfgp)
        if c_text and c_text.strip():
            base, conflicts = merge_three_way(c_text, z_text, w_text, prefer)
            res["action"] = f"两侧都有改动 -> 三方合并（冲突小节取 {prefer} 侧）"
        else:
            base, conflicts = merge_sections(z_text, w_text, prefer)
            res["action"] = f"两侧都有改动且没有历史基线 -> 按小节合并（冲突取 {prefer} 侧）"
        res["conflicts"] = conflicts
    else:
        base = c_text or z_text or w_text or ""
        res["action"] = "无需变更"

    if base is None:
        base = ""
    base = base.replace("\r\n", "\n").strip() + "\n"

    plan = []
    if z_text is None or h(z_text) != h(base):
        plan.append(("zcode", cfgp["zcode_rules"], z_text))
    if w_text is None or h(w_text) != h(base):
        plan.append(("workbuddy", cfgp["wb_rules"], w_text))
    if c_text is None or h(c_text) != h(base):
        plan.append(("canonical", canon_path, c_text))

    if verbose:
        print(f"  {res['action']}")
        if res["conflicts"]:
            print(f"  冲突小节（{len(res['conflicts'])}）: {', '.join(res['conflicts'][:5])}")
        for side, p, _ in plan:
            print(f"  → 将写入 {side}: {p}")

    if dry_run:
        res["plan"] = [p for _, p, _ in plan]
        return res

    if plan:
        d, saved = backup(extra_files=[p for _, p, _ in plan if p and os.path.exists(p)])
        res["backup"] = d

    for side, p, old in plan:
        write_text(p, base)
        res["pushed"].append(side)

    # WorkBuddy 真正加载的项目级规则
    if project_workspaces:
        rel = cfg["workbuddy"].get("project_rules_relpath", ".codebuddy/CODEBUDDY.md")
        limit = cfg["memory"].get("max_rules_bytes", 8000)
        payload = base
        if len(payload.encode("utf-8")) > limit:
            payload = payload.encode("utf-8")[:limit - 60].decode("utf-8", "ignore") + \
                      "\n\n（规则超长，已截断；请精简全局记忆）\n"
            if verbose:
                print(f"  ! 规则超过 {limit} 字节，写入 WorkBuddy 项目上下文时已截断")
        for ws in project_workspaces:
            if not ws or not os.path.isdir(ws):
                continue
            target = os.path.join(ws, rel)
            cur = read_text(target)
            if cur is not None and h(cur) != h(payload) and h(cur) != st.get("w_hash"):
                # 已有且不是我们上次写的：不覆盖，避免踩到用户自己的规则
                if verbose:
                    print(f"  ! 跳过 {target}（已存在且非本工具写入）")
                continue
            if cur is None or h(cur) != h(payload):
                write_text(target, payload)
                res["pushed"].append("project:" + ws)

    # 留档镜像：WorkBuddy 不读取该路径，仅为便于人肉查看与旧工具兼容。
    # 失败不影响主流程（目录可能不存在）。
    mirror = cfgp.get("wb_mirror") or ""
    if mirror:
        try:
            cur_m = read_text(mirror)
            if cur_m is None or h(cur_m) != h(base):
                write_text(mirror, base)
                res["pushed"].append("mirror")
        except Exception:
            pass

    c = state.connect()
    state.set_memory_state(c, canonical_hash=h(base), z_hash=h(base), w_hash=h(base))
    c.commit()
    c.close()
    return res


def _newer_side(cfgp):
    z, w = cfgp["zcode_rules"], cfgp["wb_rules"]
    try:
        return "zcode" if os.path.getmtime(z) >= os.path.getmtime(w) else "wb"
    except Exception:
        return "zcode"


def status(verbose=True):
    cfg = paths.load_config()
    cfgp = paths.cfg_paths(cfg)
    canon_path = os.path.join(paths.MEMORY_DIR, "GLOBAL.md")
    z = read_text(cfgp["zcode_rules"])
    w = read_text(cfgp["wb_rules"])
    ctext = read_text(canon_path)
    mirror = cfgp.get("wb_mirror") or ""
    mtext = read_text(mirror) if mirror else None
    st = state.get_memory_state(state.connect())
    rows = [("Zcode", cfgp["zcode_rules"], z, st.get("z_hash"), "每条消息实时注入"),
            ("WorkBuddy", cfgp["wb_rules"], w, st.get("w_hash"), "用户级·跨工作区·首轮注入"),
            ("权威副本", canon_path, ctext, st.get("canonical_hash"), "仅存放，不下发"),
            ("留档镜像", mirror, mtext, None, "WorkBuddy 不加载该路径")]
    out = []
    if verbose:
        print("  全局规则文件：")
    for name, p, text, last, note in rows:
        if not p:
            if verbose:
                print("    ✗ %-10s 未定位到路径（检查 ~/.workbuddy/user-*-personal）" % name)
            out.append({"side": name, "path": "", "exists": False, "bytes": 0,
                        "changed": "缺路径", "note": note})
            continue
        changed = "首次" if last is None else ("已改动" if (text is not None and h(text) != last) else "未变")
        size = len(text.encode("utf-8")) if text is not None else 0
        out.append({"side": name, "path": p, "exists": text is not None,
                    "bytes": size, "changed": changed, "note": note})
        if verbose:
            flag = "✓" if text is not None else "✗"
            print("    %s %-10s %5dB  %-6s %s" % (flag, name, size, changed, p))
            print("      %-10s %6s  %s" % ("", "", note))
    if z is not None and w is not None:
        d = diff_summary(z, w)
        if verbose:
            if not any(d.values()):
                print("  Zcode 与 WorkBuddy 内容一致。")
            else:
                print("  差异：新增 %d / 删除 %d / 修改 %d 小节" % (
                    len(d['added']), len(d['removed']), len(d['changed'])))
    return out
