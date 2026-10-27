#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentSync — Zcode <-> WorkBuddy 对话与全局记忆双向同步器。

常用：
    python run.py doctor                 体检：两侧数据库/规则文件是否可访问
    python run.py status                 看一眼两边各有多少对话、怎么配对的
    python run.py sync --dry-run         只看计划，不写任何东西
    python run.py sync                   真正同步（自动备份）
    python run.py memory status          全局规则两侧是否一致
    python run.py memory sync            规则双向同步
    python run.py memory backup          备份全局规则
    python run.py backup                 立刻快照两侧数据库
"""
import argparse
import datetime
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asynclib import console as _console   # noqa: E402

_console.setup()

from asynclib import memory, paths, state, sync, workbuddy, zcode   # noqa: E402


def ts(ms):
    if not ms:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def hr(t=""):
    print(("─" * 4 + " " + t) if t else "─" * 70)


# ---------------------------------------------------------------- doctor
def cmd_doctor(args):
    cfg = paths.load_config()
    cfgp = paths.cfg_paths(cfg)
    print("AgentSync 体检")
    hr("两侧数据源")
    ok = True
    for label, p, kind in (("Zcode 数据库", cfgp["zcode_db"], "db"),
                           ("Zcode 规则", cfgp["zcode_rules"], "file"),
                           ("WorkBuddy 数据库", cfgp["wb_db"], "db"),
                           ("WorkBuddy 项目目录", cfgp["wb_projects"], "dir"),
                           ("WorkBuddy 规则镜像", cfgp["wb_rules"], "file")):
        exists = os.path.exists(p)
        if kind == "db":
            ok = ok and exists
        mark = "✓" if exists else ("·" if kind in ("file", "dir") else "✗")
        print(f"  {mark} {label:<20} {p}")
    hr("进程状态")
    zr, wr = zcode.is_running(), workbuddy.is_running()
    print(f"  Zcode: {'运行中' if zr else '未运行'}    WorkBuddy: {'运行中' if wr else '未运行'}")
    if zr or wr:
        print("  ! 建议先退出两侧应用再同步；即使应用在运行，本工具也会先做一致快照备份")
    hr("数据概览")
    try:
        zc, _ = zcode.read_all(cfgp["zcode_db"], cfg["zcode"].get("include_subagents", False),
                               tasks_index=cfgp.get("zcode_tasks_index"))
        print(f"  Zcode 会话（不含子代理、不含已归档）: {len(zc)}")
    except Exception as e:
        print(f"  ✗ 读取 Zcode 失败: {e}")
    try:
        wc, _ = workbuddy.read_all(cfgp["wb_db"], cfgp["wb_projects"])
        print(f"  WorkBuddy 会话: {len(wc)}")
    except Exception as e:
        print(f"  ✗ 读取 WorkBuddy 失败: {e}")
    hr("工具结果形状")
    # Zcode 重放历史工具结果时 Buffer.byteLength(state.error / state.output)，
    # 任一侧不是字符串就会让整个会话订阅失败（点开空白 + 报 ERR_INVALID_ARG_TYPE）。
    try:
        audit = zcode.audit_tool_state(cfgp["zcode_db"])
        fatal, warn = audit["fatal"], audit["warn"]
        if not fatal and not warn:
            print(f"  ✓ {audit['scanned']} 条 part 全部正常")
        else:
            print(f"  {'✗' if fatal else '·'} 致命 {len(fatal)} 条，提示 {len(warn)} 条"
                  f"（扫描 {audit['scanned']} 条）")
            if fatal:
                ok = False
                print("    ! 这些会话点开会报 ERR_INVALID_ARG_TYPE 或显示空白")
                print("      修法：双击 AgentSync.exe → 选 4「修复打不开的会话」")
    except Exception as e:
        print(f"  ✗ 检查失败: {e}")
    hr("侧边栏索引")
    try:
        o = zcode.audit_orphan_tasks(cfgp["zcode_db"], cfgp.get("zcode_tasks_index") or "")
        if not o["orphans"]:
            print(f"  ✓ 索引 {o['total']} 条，全部有对应会话")
        else:
            ok = False
            print(f"  ✗ {len(o['orphans'])} 个孤儿条目（列表里有、点开是空的）")
            for x in o["orphans"][:5]:
                print(f"    · {x['title'][:40] or '(无标题)'}")
            if len(o["orphans"]) > 5:
                print(f"    … 其余 {len(o['orphans']) - 5} 个")
            print("      修法：双击 AgentSync.exe → 选 4「修复打不开的会话」")
    except Exception as e:
        print(f"  ✗ 检查失败: {e}")
    hr("全局规则")
    memory.status()
    return 0 if ok else 1


# ---------------------------------------------------------------- status
def cmd_status(args):
    cfg = paths.load_config()
    cfgp = paths.cfg_paths(cfg)
    s = sync.status(cfg, cfgp)
    print("工作区分布（文件夹名相同 = 同一工作空间）")
    for ws, n in sorted(s["workspaces"].items()):
        print(f"  {ws or '(无)':<24} Zcode {n['zcode']:>3}  |  WorkBuddy {n['wb']:>3}")
    print()
    print("每个工作区在该侧使用的完整目录（不一致 = App 里会分成两个同名分组）")
    for ws, pp in sorted(s["paths"].items()):
        zc = sorted({u for u in s["z_paths"].get(ws, [])})
        wc = sorted({u for u in s["w_paths"].get(ws, [])})
        print(f"  【{ws or '(无)'}】标准：Zcode {pp['zcode'] or '-'} | WorkBuddy {pp['wb'] or '-'}")
        if len(zc) > 1 or (zc and pp["zcode"] and zc != [pp["zcode"]]):
            print(f"       Zcode 实际用过：{', '.join(zc)}")
        if len(wc) > 1 or (wc and pp["wb"] and wc != [pp["wb"]]):
            print(f"       WB    实际用过：{', '.join(wc)}")
    print()
    print(f"配对：{len(s['pairs'])}   仅 Zcode 有：{len(s['only_z'])}   仅 WorkBuddy 有：{len(s['only_w'])}"
          f"   进行中：{len(s['active_wb'])}")
    if args.verbose:
        print()
        hr("配对明细")
        for p in s["pairs"]:
            z, w = p["z"], p["w"]
            print(f"  [{p['how']}] {z.title[:28]:<30} {ts(z.updated)} | {ts(w.updated)}")
            if z.workspace != w.workspace:
                print(f"       ! 工作区不同：Zcode={z.workspace} / WB={w.workspace}（按指纹配对）")
        if s["only_z"]:
            hr("仅 Zcode 有（下次会复制到 WorkBuddy）")
            for c in s["only_z"]:
                print(f"  {ts(c.updated)}  {c.workspace:<20} {c.title[:40]}")
        if s["only_w"]:
            hr("仅 WorkBuddy 有（下次会复制到 Zcode）")
            for c in s["only_w"]:
                print(f"  {ts(c.updated)}  {c.workspace:<20} {c.title[:40]}")
    return 0


# ---------------------------------------------------------------- sync
def cmd_sync(args):
    cfg = paths.load_config()
    if getattr(args, "keep_paths", False):
        cfg["sync"]["normalize_workspace_paths"] = False
    if getattr(args, "force_open", False):
        cfg["sync"]["require_zcode_closed"] = False
    cfgp = paths.cfg_paths(cfg)
    print(f"扫描两侧…（工作区过滤：{args.workspace or '全部'}）")
    plan = sync.make_plan(cfg, cfgp, only_workspace=args.workspace, revive=args.revive,
                          propagate_delete=args.delete)
    st = plan["stats"]
    print(f"  Zcode {st['z_total']} 个 / WorkBuddy {st['w_total']} 个 / 已配对 {st['pairs']} 个 "
          f"/ 仅侧各 {st['only_z']}·{st['only_w']}")
    if plan["skipped_active"]:
        print(f"  跳过 {len(plan['skipped_active'])} 个「WorkBuddy 侧正在进行」的会话"
              f"（不往里写；从它读快照复制到 Zcode 是允许的）")
    if st.get("archived_z"):
        print(f"  Zcode 侧有 {st['archived_z']} 个已归档/已删除会话，不参与同步"
              f"（--revive 可强制恢复）")
    if st.get("deleted_wb"):
        print(f"  WorkBuddy 侧有 {st['deleted_wb']} 个已删除会话，其中 {st['tombstone']} 个"
              f"与 Zcode 现有对话同源 —— 默认不再复制回去（--revive 可强制恢复）")

    acts = {"z2w": 0, "w2z": 0, "del-z": 0, "del-w": 0, "in-sync": 0, "skip": 0,
            "fix-path": 0}
    for it in plan["items"]:
        acts[it["action"]] = acts.get(it["action"], 0) + 1
    print(f"  计划：Zcode→WB {acts['z2w']} | WB→Zcode {acts['w2z']} | "
          f"统一目录 {acts['fix-path']} | 删除传播 {acts['del-z'] + acts['del-w']} | "
          f"一致 {acts['in-sync']} | 待人工 {acts['skip']}")
    no_del = [i for i in plan["items"] if i["action"] == "skip" and i["kind"].startswith("deleted")]
    if no_del:
        print(f"  ! 其中 {len(no_del)} 个是对侧已删除的会话：默认只报告、不删这一侧"
              f"（要严格一致可加 --delete）")
    print()

    if args.verbose:
        hr("工作区标准目录（同名文件夹统一落到这里，App 里才是一个分组）")
        for name, pp in sorted((plan.get("paths") or {}).items()):
            print(f"  {name or '(无)':<18} Zcode: {pp['zcode'] or '-'}")
            print(f"  {'':<18} WB   : {pp['wb'] or '-'}")
        print()
        hr("逐条计划")
        for it in plan["items"]:
            if it["action"] in ("in-sync",) and not args.all:
                continue
            src = it["z"] or it["w"]
            arrow = {"z2w": "Z→W", "w2z": "W→Z", "del-z": "删Z", "del-w": "删W",
                     "fix-path": "搬目", "in-sync": " =  ", "skip": " ?? "}[it["action"]]
            print(f"  {arrow} {ts(src.updated)} {it['workspace'][:22]:<24} {src.title[:34]:<36} {it['why']}")
        print()

    # 需要时确认
    writes = acts["z2w"] + acts["w2z"] + acts["del-z"] + acts["del-w"] + acts["fix-path"]
    if not args.dry_run and writes and sys.stdin.isatty() and not args.yes:
        try:
            ans = input(f"将写入 {writes} 个会话，是否继续？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            print("已取消。")
            return 0

    res = sync.apply_plan(cfg, cfgp, plan, dry_run=args.dry_run, verbose=True)
    print()
    if args.dry_run:
        print("（dry-run：没有写入任何内容）")
    else:
        print(f"写入完成：{len(res['written'])} 个成功，删除 {len(res['deleted'])} 个，"
              f"{len(res['errors'])} 个失败")
        if res["backup"]:
            print(f"备份：{res['backup']}")
        for e in res["errors"]:
            print(f"  ✗ {e['title']}: {e['error']}")

    # 全局规则
    if not args.no_memory:
        hr("全局规则同步")
        wss = _all_workspaces() if args.project_rules else []
        mres = memory.sync(dry_run=args.dry_run, verbose=True, project_workspaces=wss)
        if args.dry_run:
            print("（dry-run：规则未写入）")
        elif mres["pushed"]:
            print(f"已更新：{', '.join(mres['pushed'])}")
    return 0


# ---------------------------------------------------------------- memory
def cmd_memory(args):
    if args.action == "status":
        memory.status()
        return 0
    if args.action == "diff":
        cfgp = paths.cfg_paths(paths.load_config())
        z, w = memory.read_text(cfgp["zcode_rules"]), memory.read_text(cfgp["wb_rules"])
        if z is None or w is None:
            print("有一侧规则文件不存在，无法比较。")
            return 0
        d = memory.diff_summary(z, w)
        for k, label in (("added", "WorkBuddy 独有"), ("removed", "Zcode 独有"), ("changed", "两侧不同")):
            if d[k]:
                print(f"{label}（{len(d[k])}）：")
                for s in d[k]:
                    print(f"  - {s}")
        if not any(d.values()):
            print("两侧完全一致。")
        return 0
    if args.action == "sync":
        r = memory.sync(prefer=args.prefer, dry_run=args.dry_run, verbose=True,
                        project_workspaces=[] if not args.project_rules else _all_workspaces())
        if r.get("backup"):
            print(f"备份：{r['backup']}")
        return 0
    if args.action == "backup":
        d, saved = memory.backup()
        print(f"已备份到 {d}")
        for p in saved:
            print(f"  {os.path.basename(p)}")
        return 0
    if args.action == "paths":
        cfgp = paths.cfg_paths(paths.load_config())
        for k, v in cfgp.items():
            print(f"  {k:<14} {v}")
        return 0
    print("未知动作。可用：status / diff / sync / backup / paths")
    return 1


def _all_workspaces():
    cfg = paths.load_config()
    cfgp = paths.cfg_paths(cfg)
    s = sync.status(cfg, cfgp)
    ws = {c.workspace_path for c in s["zconvs"] + s["wconvs"] if c.workspace_path}
    return sorted(w for w in ws if os.path.isdir(w))


# ---------------------------------------------------------------- backup
def cmd_backup(args):
    cfgp = paths.cfg_paths(paths.load_config())
    d, made = sync.snapshot_dbs(cfgp)
    print(f"数据库快照 -> {d}")
    for p in made:
        print(f"  {os.path.basename(p)}  {os.path.getsize(p) / 1024:.0f} KB")
    md, saved = memory.backup()
    print(f"规则备份   -> {md}")
    for p in saved:
        print(f"  {os.path.basename(p)}")
    return 0


def cmd_backups(args):
    d = paths.BACKUP_DIR
    if not os.path.isdir(d):
        print("还没有任何备份。")
        return 0
    for name in sorted(os.listdir(d), reverse=True)[:20]:
        p = os.path.join(d, name)
        n = sum(len(f) for _, _, f in os.walk(p))
        print(f"  {name:<18} {n} 个文件  {ts(os.path.getmtime(p) * 1000)}")
    return 0


def cmd_reset_baseline(args):
    """清空 state.db 里记录的哈希基线。

    哈希算法升级（例如工具调用改为按 call_id 归一化比较）后，旧哈希无法直接
    比较，会让所有配对被判成「两侧都改了」。清掉基线后，下一次 sync 会按
    「谁的时间戳更新」重新判定并建立新基线，配对关系本身不丢。
    """
    c = state.connect()
    n = c.execute("select count(*) from pairs").fetchone()[0]
    with c:
        c.execute("update pairs set z_hash=null, w_hash=null")
    c.close()
    print(f"已清空 {n} 个配对的哈希基线（配对关系保留）。下次 sync 会重建。")
    return 0



# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="AgentSync", description="Zcode <-> WorkBuddy 双向同步")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="体检两边数据源").set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="查看配对情况")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync", help="双向同步对话 + 全局规则")
    p.add_argument("--dry-run", action="store_true", help="只输出计划，不写入")
    p.add_argument("--workspace", help="只同步指定工作区（文件夹名）")
    p.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    p.add_argument("--all", action="store_true", help="计划里也显示已一致的会话")
    p.add_argument("--verbose", "-v", action="store_true", default=True)
    p.add_argument("--no-memory", action="store_true", help="不同步全局规则")
    p.add_argument("--revive", action="store_true",
                   help="忽略墓碑：把对侧已删除的对话重新复制回来")
    p.add_argument("--delete", dest="delete", action="store_true", default=None,
                   help="严格一致：一侧删除时，另一侧也删（默认只报告，不删）")
    p.add_argument("--no-delete", dest="delete", action="store_false",
                   help="（默认行为）一侧删除时不删另一侧")
    p.add_argument("--project-rules", action="store_true",
                   help="同时把规则写入各工作区的 .codebuddy/CODEBUDDY.md（WorkBuddy 实际加载路径）")
    p.add_argument("--keep-paths", action="store_true",
                   help="不统一工作区目录（默认会统一，否则 App 里同名工作区不合并）")
    p.add_argument("--force-open", dest="force_open", action="store_true",
                   help="Zcode 正在运行时也强行写入它的数据库（默认拒绝，风险自负）")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("memory", help="全局规则相关")
    p.add_argument("action", nargs="?", default="status",
                   choices=["status", "diff", "sync", "backup", "paths"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--prefer", choices=["zcode", "wb"], help="冲突时以哪一侧为准")
    p.add_argument("--project-rules", action="store_true", help="同步到各工作区项目规则文件")
    p.set_defaults(func=cmd_memory)

    sub.add_parser("backup", help="立刻快照数据库与规则").set_defaults(func=cmd_backup)
    sub.add_parser("backups", help="列出历史备份").set_defaults(func=cmd_backups)
    sub.add_parser("reset-baseline",
                   help="清空哈希基线（升级比较算法后跑一次）").set_defaults(
                       func=cmd_reset_baseline)

    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 0
    paths.ensure_dirs()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
