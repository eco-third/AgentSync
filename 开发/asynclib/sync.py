"""同步引擎：配对 -> 判定谁更新 -> 生成计划 -> 执行（带备份）。

规则落地：
  · 规则 1  按时间顺序同步；两边都有同一对话时，时间较新的一方覆盖较旧的一方
  · 规则 2  工作区文件夹名相同 = 同一个工作空间
  · 规则 3  一方有、另一方没有 -> 复制过去，直到两边都有

判定「同一对话」的顺序：
  1) state.db 里的显式映射（最可靠，上次同步建立的）
  2) 内容指纹（前 3 条用户消息 + 创建时间到分钟）
     交叉工作区仅在 allow_cross_workspace_match=true 时启用（历史迁移产物会这样）
"""
import json
import os
import shutil
import sqlite3
import time

from . import paths, state, workbuddy, zcode
from .model import workspace_name


def now_ms():
    return int(time.time() * 1000)


def norm_path(p):
    """两侧路径写法不同（Zcode 存反斜杠，WorkBuddy 存正斜杠），比较前先归一。"""
    if not p:
        return ""
    q = str(p).replace("\\", "/")
    while len(q) > 2 and q.endswith("/"):
        q = q[:-1]
    return q


def resolve_workspace_paths(cfg, zconvs, wconvs):
    """规则 2 的落地：文件夹名相同 = 同一个工作空间，那就必须落在同一组目录里。

    否则会出现「两个工作区都叫 日常项目，却没合并」——在 App 里其实是两个分组，
    因为分组的依据是完整路径，不是文件夹名。

    返回 {文件夹名: {"zcode": 标准目录, "wb": 标准目录}}，取值顺序：
      1) config.json 的 workspaces 显式指定（推荐，最稳）
      2) 该侧「最近活跃」会话所在的目录（同一工作区里最新的那条说了算）
    """
    def recent(convs):
        best = {}
        for c in convs:
            if not c.workspace_path:
                continue
            cur = best.get(c.workspace)
            if cur is None or c.updated > cur[0]:
                best[c.workspace] = (c.updated, c.workspace_path)
        return {k: v[1] for k, v in best.items()}

    zr, wr = recent(zconvs), recent(wconvs)
    override = cfg.get("workspaces") or {}
    names = set(zr) | set(wr) | set(override)
    out = {}
    for name in names:
        o = override.get(name) or {}
        # 保留配置里的原始写法（Zcode 原生用反斜杠，WorkBuddy 用正斜杠），
        # 比较时才用 norm_path；写回去的路径要和该 App 原生的写法一致
        out[name] = {
            "zcode": o.get("zcode") or zr.get(name) or "",
            "wb": o.get("wb") or wr.get(name) or "",
        }
    return out


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- 备份
def prune_backups(keep=5, verbose=False):
    """只保留最近 N 份数据库快照，避免磁盘被历史备份吃掉。"""
    root = paths.BACKUP_DIR
    if not os.path.isdir(root):
        return []
    items = [d for d in os.listdir(root)
             if os.path.isdir(os.path.join(root, d)) and d[:8].isdigit()]
    items.sort(reverse=True)
    removed = []
    for d in items[keep:]:
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        removed.append(d)
    if verbose and removed:
        print(f"  已清理 {len(removed)} 份旧备份（保留最近 {keep} 份）")
    return removed


def snapshot_dbs(cfgp, tag=None, keep=5):
    """用 sqlite 在线备份接口取一致快照（WAL 下直接拷文件会撕裂）。"""
    tag = tag or _stamp()
    d = os.path.join(paths.BACKUP_DIR, tag)
    os.makedirs(d, exist_ok=True)
    made = []
    for name, src in (("zcode.db", cfgp["zcode_db"]), ("workbuddy.db", cfgp["wb_db"])):
        if not os.path.exists(src):
            continue
        s = sqlite3.connect(src, timeout=15)
        t = sqlite3.connect(os.path.join(d, name))
        with t:
            s.backup(t)
        t.close()
        s.close()
        made.append(os.path.join(d, name))
    prune_backups(keep)
    return d, made


def _dump_conv_json(conv, backup_dir, sub):
    d = os.path.join(backup_dir, sub)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{conv.native_id}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({
            "side": conv.side, "native_id": conv.native_id, "workspace": conv.workspace,
            "workspace_path": conv.workspace_path, "title": conv.title,
            "created": conv.created, "updated": conv.updated,
            "events": [{"kind": e.kind, "ts": e.ts, "text": e.text, "name": e.name,
                        "call_id": e.call_id, "args": e.args, "output": e.output,
                        "status": e.status} for e in conv.events],
        }, f, ensure_ascii=False, indent=1)
    return p


def _dump_zcode_rows(db_path, session_id, backup_dir):
    """覆盖 Zcode 会话前，把原始 session/message/part/session_entry 行整份留底。

    只 dump 事件序列是不够的：信封字段（tokens/cost/semantics/anchor）一旦被
    覆盖就推不回来，而 resume 恰恰依赖它们。
    """
    snap = zcode.snapshot_session(db_path, session_id)
    if not snap:
        return None
    d = os.path.join(backup_dir, "zcode-raw-rows")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{session_id}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    return p


def _z_hash(cfgp, sid):
    rows = zcode.list_sessions(cfgp["zcode_db"], include_subagents=True,
                               tasks_index=cfgp.get("zcode_tasks_index"),
                               include_archived=True)
    for r in rows:
        if r["id"] == sid:
            return zcode.read_conversation(cfgp["zcode_db"], r).content_hash()
    return None


def _wb_hash(cfgp, sid):
    rows = workbuddy.list_sessions(cfgp["wb_db"], include_deleted=True)
    for r in rows:
        if r["id"] == sid:
            return workbuddy.read_conversation(cfgp["wb_projects"], r).content_hash()
    return None



# ---------------------------------------------------------------- 配对
def build_pairs(zconvs, wconvs, pair_rows, cfg):
    """返回 (pairs, only_z, only_w)。pairs 每项 {z, w, how}"""
    zmap = {c.native_id: c for c in zconvs}
    wmap = {c.native_id: c for c in wconvs}
    used_z, used_w = set(), set()
    pairs = []

    # 1) 显式映射
    for p in pair_rows:
        z, w = zmap.get(p["zcode_id"]), wmap.get(p["wb_id"])
        if z and w and z.native_id not in used_z and w.native_id not in used_w:
            pairs.append({"z": z, "w": w, "how": "映射", "state": p})
            used_z.add(z.native_id)
            used_w.add(w.native_id)

    # 2) 同工作区指纹
    by_fp_w = {}
    for w in wconvs:
        if w.native_id in used_w:
            continue
        by_fp_w.setdefault((w.workspace, w.fingerprint()), []).append(w)
    for z in zconvs:
        if z.native_id in used_z:
            continue
        cand = [w for w in by_fp_w.get((z.workspace, z.fingerprint()), [])
                if w.native_id not in used_w]
        if len(cand) == 1:
            pairs.append({"z": z, "w": cand[0], "how": "同工作区指纹", "state": None})
            used_z.add(z.native_id)
            used_w.add(cand[0].native_id)

    # 3) 跨工作区指纹（历史迁移产物）
    if cfg["sync"].get("allow_cross_workspace_match", True):
        fp_w = {}
        for w in wconvs:
            if w.native_id in used_w:
                continue
            fp_w.setdefault(w.fingerprint(), []).append(w)
        for z in zconvs:
            if z.native_id in used_z:
                continue
            cand = [w for w in fp_w.get(z.fingerprint(), []) if w.native_id not in used_w]
            if len(cand) == 1:
                # 时间差太大就不是同一场对话，宁可当成两个
                if abs(cand[0].created - z.created) <= 10 * 60 * 1000:
                    pairs.append({"z": z, "w": cand[0], "how": "跨工作区指纹", "state": None})
                    used_z.add(z.native_id)
                    used_w.add(cand[0].native_id)

    only_z = [c for c in zconvs if c.native_id not in used_z]
    only_w = [c for c in wconvs if c.native_id not in used_w]
    return pairs, only_z, only_w


# ---------------------------------------------------------------- 判定
def decide(z, w, st, tol):
    """返回 (动作, 说明)。动作 ∈ {'in-sync','z2w','w2z','skip'}"""
    zh, wh = z.content_hash(), w.content_hash()
    if zh == wh:
        return "in-sync", "内容一致"
    # 两侧自上次同步后都没再改动 -> 就是同步状态。
    # 这一条必须有：把 WB 的内容写进 Zcode 后，Zcode 读回来的事件序列与源
    # 不可能逐字节相同（信封、part 时间都不同），若只靠 hash 相等判定，
    # 每次同步都会认为「有差异」，然后反复重写。
    if st:
        z_changed = st.get("z_hash") is not None and st["z_hash"] != zh
        w_changed = st.get("w_hash") is not None and st["w_hash"] != wh
        if st.get("z_hash") is not None and st.get("w_hash") is not None \
                and not z_changed and not w_changed:
            return "in-sync", "自上次同步后两侧均未改动"
    if z.updated > w.updated + tol:
        return "z2w", f"Zcode 较新（+{(z.updated - w.updated) / 1000:.0f}s）"
    if w.updated > z.updated + tol:
        return "w2z", f"WorkBuddy 较新（+{(w.updated - z.updated) / 1000:.0f}s）"
    # 时间几乎相同但内容不同：看上次同步后谁动过
    if st:
        z_changed = st.get("z_hash") is not None and st["z_hash"] != zh
        w_changed = st.get("w_hash") is not None and st["w_hash"] != wh
        if z_changed and not w_changed:
            return "z2w", "时间相同，但仅 Zcode 侧有改动"
        if w_changed and not z_changed:
            return "w2z", "时间相同，但仅 WorkBuddy 侧有改动"
    return "skip", "时间相同且内容不同，无法判定（需人工确认）"


# ---------------------------------------------------------------- 计划
def make_plan(cfg, cfgp, only_workspace=None, include_subagents=None, revive=False,
              propagate_delete=None):
    if include_subagents is None:
        include_subagents = cfg["zcode"].get("include_subagents", False)
    if propagate_delete is None:
        propagate_delete = cfg["sync"].get("propagate_delete", False)
    zall, zrows = zcode.read_all(cfgp["zcode_db"], include_subagents,
                                 tasks_index=cfgp.get("zcode_tasks_index"),
                                 include_archived=True)
    wall, wrows = workbuddy.read_all(cfgp["wb_db"], cfgp["wb_projects"], include_deleted=True)

    # 已归档/已删除的会话：不参与同步，也不写到对侧。
    # 同时把它们记成墓碑 —— 否则对侧那条会被当成「这边缺失」而重新复制回来，
    # 用户手动归档过的会话就这么复活了。
    zarchived = [c for c in zall if c.archived]
    zconvs = [c for c in zall if not c.archived]

    if only_workspace:
        key = only_workspace.strip().lower()
        zconvs = [c for c in zconvs if c.workspace == key]
        wall = [c for c in wall if c.workspace == key]

    wdead = [c for c in wall if c.deleted]
    wconvs = [c for c in wall if not c.deleted]

    # 正在进行的会话：**只保护「写入目标」，不禁止当作「来源」**。
    # 从活跃会话读一份快照复制到另一侧是安全的（只读）；往正在写的会话里改内容才危险。
    active_ids = set()
    if cfg["sync"].get("skip_active_sessions", True):
        active_ids = {c.native_id for c in wconvs
                      if (c.status or "").lower() in workbuddy.ACTIVE_STATUS}

    # 工作区的标准目录：同名的文件夹必须落到同一组路径，App 里才会是一个分组
    wpaths = resolve_workspace_paths(cfg, zconvs, wconvs)
    keep_paths = not cfg["sync"].get("normalize_workspace_paths", True)

    c = state.connect()
    pair_rows = state.all_pairs(c)
    tomb = {} if revive else state.tombstones(c)
    # 软删除的会话天然是墓碑：说明用户主动删过，不能从另一边再复制回来
    for d in wdead:
        tomb.setdefault(d.fingerprint(), {"fingerprint": d.fingerprint(), "side": "wb",
                                          "note": "WorkBuddy 侧已删除"})
    # 归档同理：用户主动收起来了，不要被同步翻出来
    if not revive:
        for c_ in zarchived:
            tomb.setdefault(c_.fingerprint(), {"fingerprint": c_.fingerprint(),
                                               "side": "zcode", "note": "Zcode 侧已归档"})

    # --- 删除传播：配对过的一侧没了，另一侧跟着删 ---
    zmap = {x.native_id: x for x in zconvs}
    wmap = {x.native_id: x for x in wconvs}
    wdead_ids = {x.native_id for x in wdead}
    zarch_ids = {c_.native_id for c_ in zarchived}
    del_items, dead_pairs, archived_items = [], [], []
    for p in pair_rows:
        z_here, w_here = p["zcode_id"] in zmap, p["wb_id"] in wmap
        if z_here and (p["wb_id"] in wdead_ids or not w_here):
            del_items.append({"kind": "deleted-wb", "how": "对方已删除", "action": "del-z",
                              "why": "WorkBuddy 侧已删除，同步删除 Zcode 侧",
                              "z": zmap[p["zcode_id"]], "w": None, "state": p,
                              "workspace": zmap[p["zcode_id"]].workspace})
            dead_pairs.append(p)
            zmap.pop(p["zcode_id"], None)
        elif w_here and p["zcode_id"] in zarch_ids:
            # Zcode 侧被用户归档了：不删对侧，也不再同步（归档 ≠ 删除）
            _w = wmap.pop(p["wb_id"], None)
            archived_items.append({"kind": "archived-z", "how": "Zcode 侧已归档",
                                   "action": "skip",
                                   "why": "Zcode 侧已归档，不参与同步（取消归档后自动恢复）",
                                   "z": None, "w": _w, "state": p,
                                   "workspace": _w.workspace if _w else ""})
            dead_pairs.append(p)
        elif w_here and not z_here:
            del_items.append({"kind": "deleted-z", "how": "对方已删除", "action": "del-w",
                              "why": "Zcode 侧已删除，同步删除 WorkBuddy 侧",
                              "z": None, "w": wmap[p["wb_id"]], "state": p,
                              "workspace": wmap[p["wb_id"]].workspace})
            dead_pairs.append(p)
            wmap.pop(p["wb_id"], None)
        elif not z_here and not w_here:
            dead_pairs.append(p)

    pairs, only_z, only_w = build_pairs(list(zmap.values()), list(wmap.values()), pair_rows, cfg)
    tol = cfg["sync"].get("equal_tolerance_ms", 1000)

    skip_del = []
    if not propagate_delete:
        for it in del_items:
            it["action"] = "skip"
            it["why"] = ("对侧已删除；默认不跟着删（要严格一致请加 --delete）")
        skip_del, del_items = del_items, []

    skipped_active = []
    items = list(del_items) + skip_del + archived_items
    for p in pairs:
        z, w = p["z"], p["w"]
        action, why = decide(z, w, p.get("state"), tol)
        want = wpaths.get(z.workspace) or {}
        need_z = (not keep_paths) and bool(want.get("zcode")) and \
            norm_path(z.workspace_path) != norm_path(want["zcode"])
        need_w = (not keep_paths) and bool(want.get("wb")) and \
            norm_path(w.workspace_path) != norm_path(want["wb"])
        # 正在写的是 WorkBuddy 这一侧：它才是「活」的那一份，一律以它为准推给 Zcode。
        # 否则会出现「刚写完 Zcode，Zcode 的时间戳反而比 WB 新 1 秒」导致来回抖。
        if w.native_id in active_ids and action != "in-sync":
            if action != "w2z":
                action, why = "w2z", "WorkBuddy 侧正在进行，以它为准（推一份快照给 Zcode）"
        # 往正在写的 WorkBuddy 会话里覆盖 -> 跳过；从它读取复制则允许
        if action == "z2w" and w.native_id in active_ids:
            skipped_active.append(w)
            items.append({"kind": "pair", "how": p["how"], "action": "skip",
                          "why": "WorkBuddy 侧对话正在进行，写入会打断，稍后再同步",
                          "z": z, "w": w, "state": p.get("state"),
                          "workspace": z.workspace, "fix_z": False, "fix_w": False})
            continue
        if action == "in-sync" and (need_z or need_w):
            # 内容一致但目录不统一 -> 只搬目录，App 里才会合并成一个工作区分组
            action = "fix-path"
            parts = []
            if need_z:
                parts.append(f"Zcode 侧挪到 {want['zcode']}")
            if need_w:
                parts.append(f"WorkBuddy 侧挪到 {want['wb']}")
                if w.native_id in active_ids:
                    need_w = False
                    parts[-1] = "WorkBuddy 侧正在使用，暂不挪"
            why = "统一工作区目录：" + "；".join(parts or ["无需处理"])
        else:
            if action in ("z2w", "w2z") and (need_z or need_w):
                side = "Zcode 侧" if action == "w2z" else "WorkBuddy 侧"
                why += f"（同时统一{side}目录）"
            if w.native_id in active_ids and need_w:
                need_w = False
        items.append({
            "kind": "pair", "how": p["how"], "action": action, "why": why,
            "z": z, "w": w, "state": p.get("state"),
            "workspace": z.workspace,
            "fix_z": need_z, "fix_w": need_w,
        })
    skipped_tomb = []
    for z in only_z:
        if z.fingerprint() in tomb:
            skipped_tomb.append(z)
            items.append({"kind": "tombstone", "how": "曾在对侧被删除", "action": "skip",
                          "why": "对侧已删除过（加 --revive 可强制恢复）", "z": z, "w": None,
                          "state": None, "workspace": z.workspace})
            continue
        items.append({"kind": "only-z", "how": "仅 Zcode 有", "action": "z2w",
                      "why": "另一侧缺失，补齐", "z": z, "w": None, "state": None,
                      "workspace": z.workspace})
    for w in only_w:
        if w.fingerprint() in tomb:
            # 对侧删过或归档过：不要"补齐"，否则会把用户主动收起来的对话
            # 又变成一个全新的会话（表现为「一个对话变成两个」）
            items.append({"kind": "only-w", "how": "对侧曾删除/归档", "action": "skip",
                          "why": "对侧已删除或归档过（加 --revive 可强制恢复）",
                          "z": None, "w": w, "state": None, "workspace": w.workspace})
            continue
        why = "另一侧缺失，补齐"
        if w.native_id in active_ids:
            why = "另一侧缺失，补一份进行中的快照"
        items.append({"kind": "only-w", "how": "仅 WorkBuddy 有", "action": "w2z",
                      "why": why, "z": None, "w": w, "state": None,
                      "workspace": w.workspace})
    items.sort(key=lambda x: ((x["z"] or x["w"]).created, (x["z"] or x["w"]).native_id))
    c.close()
    return {"items": items, "skipped_active": skipped_active, "active_ids": active_ids,
            "paths": wpaths, "zarchived": zarchived,
            "skipped_tombstone": skipped_tomb, "dead_pairs": dead_pairs, "wdead": wdead,
            "stats": {"z_total": len(zconvs), "w_total": len(wconvs),
                      "pairs": len(pairs), "only_z": len(only_z), "only_w": len(only_w),
                      "deleted_wb": len(wdead), "tombstone": len(skipped_tomb),
                      "archived_z": len(zarchived),
                      "active_allowed": len(active_ids)}}


# ---------------------------------------------------------------- 执行
def _move_side(cfgp, conv, new_path):
    """把一条会话搬到该工作区的标准目录，内容一点不动。

    注意：Zcode 侧绝不能调 write_conversation —— 那会整段重写 message/part，
    把 Zcode 运行时的 message 信封（tokens/cost/path/semantics/anchor）削平，
    导致会话打不开。这里只改 session 的 directory/path 两个字段。
    """
    if conv.side == "zcode":
        c = sqlite3.connect(cfgp["zcode_db"], timeout=20)
        try:
            with c:
                c.execute("update session set directory=?, path=? where id=?",
                          (new_path, new_path, conv.native_id))
        finally:
            c.close()
        return conv.native_id, conv.updated, len(conv.events)
    return workbuddy.write_conversation(cfgp["wb_db"], cfgp["wb_projects"], conv, new_path,
                                        existing_id=conv.native_id,
                                        old_cwd=conv.workspace_path)


def apply_plan(cfg, cfgp, plan, dry_run=True, verbose=True):
    # 先把「对侧删过」的会话记成墓碑：即使本次没有写入动作，也要落库，
    # 否则下次又从这边复制回去（这个坑实机自检时踩到过一次）
    if not dry_run:
        c0 = state.connect()
        for d in plan.get("wdead", []):
            state.add_tombstone(c0, d.fingerprint(), "wb", "WorkBuddy 侧已删除")
        c0.commit()
        c0.close()

    todo = [i for i in plan["items"]
            if i["action"] in ("z2w", "w2z", "del-z", "del-w", "fix-path")]
    result = {"written": [], "deleted": [], "errors": [], "backup": None, "dry_run": dry_run}
    if not todo:
        if verbose:
            print("  没有需要写入的会话。")
        return result

    backup_dir = None
    if not dry_run:
        backup_dir, made = snapshot_dbs(cfgp)
        result["backup"] = backup_dir
        if verbose:
            print(f"  已备份两侧数据库 -> {backup_dir}")
        if not made:
            raise RuntimeError("数据库快照失败，已中止（不会在无备份的情况下写入）")

    c = state.connect()
    zcode_open = (cfg.get("sync", {}).get("require_zcode_closed", True)
                  and any(i["action"] in ("w2z", "del-z", "fix-path") for i in todo)
                  and zcode.is_running())
    if zcode_open:
        print("  ! Zcode 正在运行，本次不写它的数据库。")
        print("    它持有连接和内存状态，退出时可能把改动覆盖回去。")
        print("    请完全退出 Zcode（含托盘图标）后重跑；确实要写入可加 --force-open。")
    for it in todo:
        src_side = "zcode" if it["action"] in ("z2w", "del-z") else "wb"
        src = it["z"] if src_side == "zcode" else it["w"]
        dst = it["w"] if src_side == "zcode" else it["z"]

        # Zcode 运行时一律不写它的库（删/覆盖/搬目录都算写）
        writes_z = (it["action"] in ("w2z", "del-z")
                    or (it["action"] == "fix-path" and it.get("fix_z")))
        if zcode_open and writes_z:
            one = it["z"] or it["w"]
            result["errors"].append({"title": one.title if one else "",
                                     "error": "Zcode 正在运行，已跳过（先退出 Zcode 再同步）"})
            if verbose:
                print(f"  ⊘ 跳过（Zcode 运行中）  {(one.title if one else '')[:30]}")
            continue

        # --- 删除传播 ---
        if it["action"] in ("del-z", "del-w"):
            label = f"{src.title[:30]}"
            if verbose:
                print(f"  · 删除 {('Zcode' if it['action'] == 'del-z' else 'WorkBuddy')} 侧  {label}"
                      f"  [{it['why']}]")
            if dry_run:
                result["deleted"].append({"dry": True, "title": src.title})
                continue
            try:
                if it["action"] == "del-z":
                    zcode.delete_conversation(cfgp["zcode_db"], src.native_id)
                    state.add_tombstone(c, src.fingerprint(), "zcode", "同步删除")
                else:
                    workbuddy.soft_delete(cfgp["wb_db"], src.native_id)
                    state.add_tombstone(c, src.fingerprint(), "wb", "同步删除")
                if it.get("state"):
                    state.drop_pair(c, it["state"]["pair_id"])
                c.commit()
                result["deleted"].append({"title": src.title})
            except Exception as e:
                result["errors"].append({"title": src.title, "error": str(e)})
                if verbose:
                    print(f"      ✗ 失败: {e}")
            continue

        # --- 只搬目录（内容一致，但两侧工作区路径没对齐）---
        if it["action"] == "fix-path":
            label = f"{src.title[:30]}"
            if verbose:
                print(f"  · 统一工作区目录  {label}")
            if dry_run:
                result["written"].append({"dry": True, "title": src.title, "action": "fix-path"})
                continue
            try:
                moved = []
                if it.get("fix_z") and it["z"] is not None:
                    _move_side(cfgp, it["z"], plan["paths"][it["z"].workspace]["zcode"])
                    moved.append("Zcode")
                if it.get("fix_w") and it["w"] is not None:
                    _move_side(cfgp, it["w"], plan["paths"][it["w"].workspace]["wb"])
                    moved.append("WorkBuddy")
                result["written"].append({"title": src.title, "action": "fix-path",
                                          "events": len(src.events), "moved": moved})
                if verbose:
                    print(f"      已挪动：{' / '.join(moved) or '无'}")
            except Exception as e:
                result["errors"].append({"title": src.title, "error": str(e)})
                if verbose:
                    print(f"      ✗ 失败: {e}")
            continue

        target_exists = dst is not None

        # 目标目录：统一到该工作区的标准目录（config.workspaces，或该侧最近活跃的目录）。
        # 这样「文件夹名相同的两个工作区」在 App 里才会合并成一个分组。
        want = (plan.get("paths") or {}).get(src.workspace) or {}
        dst_side = "wb" if src_side == "zcode" else "zcode"
        target_path = want.get(dst_side) or (dst.workspace_path if target_exists
                                             else src.workspace_path)
        moved_dir = bool(target_exists) and norm_path(dst.workspace_path) != norm_path(target_path)

        label = f"{src.title[:30]}"
        if verbose:
            arrow = "Zcode → WorkBuddy" if src_side == "zcode" else "WorkBuddy → Zcode"
            print(f"  · {arrow}  {label}  [{it['why']}]")
            if moved_dir:
                print(f"      目录统一到 {target_path}")

        if dry_run:
            result["written"].append({"dry": True, "title": src.title, "action": it["action"]})
            continue

        # 安全闸：默认允许覆盖，但写入器必须能重建完整原生信封。
        # （早版本整段重写会把 tokens/cost/semantics 削平，Zcode resume 崩溃，
        #  所以曾默认禁止。现已按真实库结构重建，覆盖前还有行级全量快照。）
        if (src_side == "wb" and target_exists
                and not cfg.get("zcode", {}).get("allow_overwrite", True)):
            msg = ("已跳过：Zcode 侧已有该会话（config.json 的 zcode.allow_overwrite"
                   " 为 false，覆盖已禁用）")
            result["errors"].append({"title": src.title, "error": msg})
            if verbose:
                print(f"      ⊘ {msg}")
            continue

        try:
            if src_side == "zcode":
                if target_exists:
                    _dump_conv_json(dst, backup_dir, "wb-conversations")
                sid, updated, n = workbuddy.write_conversation(
                    cfgp["wb_db"], cfgp["wb_projects"], src, target_path,
                    existing_id=dst.native_id if target_exists else None,
                    old_cwd=dst.workspace_path if moved_dir else None)
            else:
                if target_exists:
                    _dump_conv_json(dst, backup_dir, "zcode-conversations")
                    _dump_zcode_rows(cfgp["zcode_db"], dst.native_id, backup_dir)
                if not os.path.isdir(target_path):
                    os.makedirs(target_path, exist_ok=True)
                sid, updated, nmsg, npart = zcode.write_conversation(
                    cfgp["zcode_db"], src, target_path,
                    existing_id=dst.native_id if target_exists else None,
                    estimate_tokens=cfg["zcode"].get("estimate_tokens", True))
                n = nmsg
                if cfg["zcode"].get("write_task_index", True):
                    try:
                        zcode.upsert_task_index(
                            cfgp.get("zcode_tasks_index"), sid, target_path, src.title,
                            src.created, updated)
                    except Exception as e:
                        # 索引写不进去不影响内容本身，但要让人看见
                        if verbose:
                            print(f"      ! 界面索引写入失败（内容已写入）: {e}")

            # 存「两侧实际读到的现状」，而不是源侧哈希：写入后目标侧读回来的内容
            # 才是下一次比对的基准，否则每次都会认为有差异
            zid = src.native_id if src_side == "zcode" else sid
            wid = sid if src_side == "zcode" else src.native_id
            zh = wh = src.content_hash()
            try:
                if src_side == "zcode":
                    r = _wb_hash(cfgp, sid)
                    wh = r if r is not None else wh
                else:
                    r = _z_hash(cfgp, sid)
                    zh = r if r is not None else zh
            except Exception:
                pass
            pid = state.upsert_pair(c, zid, wid, src.workspace, src.fingerprint(),
                                    z_hash=zh, w_hash=wh,
                                    z_updated=src.updated, w_updated=src.updated)
            state.update_hashes(c, pid, z_hash=zh, w_hash=wh,
                                z_updated=src.updated, w_updated=src.updated)
            c.commit()
            result["written"].append({"title": src.title, "action": it["action"],
                                      "dst_id": sid, "events": n})
            if verbose:
                print(f"      写入 {n} 个事件")
        except Exception as e:
            result["errors"].append({"title": src.title, "error": str(e)})
            if verbose:
                print(f"      ✗ 失败: {e}")

    state.log_run(c, "apply" if not dry_run else "dry-run",
                  {"written": len(result["written"]), "errors": len(result["errors"])})
    c.close()
    return result


# ---------------------------------------------------------------- 状态汇总
def status(cfg, cfgp):
    zconvs, _ = zcode.read_all(cfgp["zcode_db"], cfg["zcode"].get("include_subagents", False),
                               tasks_index=cfgp.get("zcode_tasks_index"))
    wconvs, wrows = workbuddy.read_all(cfgp["wb_db"], cfgp["wb_projects"])
    c = state.connect()
    pair_rows = state.all_pairs(c)
    pairs, only_z, only_w = build_pairs(zconvs, wconvs, pair_rows, cfg)
    c.close()
    ws = {}
    for conv in zconvs + wconvs:
        ws.setdefault(conv.workspace, {"zcode": 0, "wb": 0})
        ws[conv.workspace]["zcode" if conv.side == "zcode" else "wb"] += 1

    def used(convs):
        d = {}
        for c in convs:
            if c.workspace_path:
                d.setdefault(c.workspace, set()).add(norm_path(c.workspace_path))
        return d

    return {"zconvs": zconvs, "wconvs": wconvs, "pairs": pairs,
            "only_z": only_z, "only_w": only_w, "workspaces": ws,
            "paths": resolve_workspace_paths(cfg, zconvs, wconvs),
            "z_paths": used(zconvs), "w_paths": used(wconvs),
            "active_wb": [r for r in wrows if (r["status"] or "").lower() in workbuddy.ACTIVE_STATUS]}
