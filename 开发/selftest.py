#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自测：全部跑在数据库副本上，绝不动真实数据。

覆盖场景：
  S1 仅 Zcode 有   -> 复制到 WorkBuddy
  S2 仅 WorkBuddy 有 -> 复制到 Zcode
  S3 两边都有、内容不同 -> 时间较新的一方覆盖较旧的一方（两个方向各测一次）
  S4 幂等：再跑一次不应有任何写入
  S5 删除传播：一侧删除 -> 另一侧跟着删，且不会被复活
  S6 全局规则：只有一侧改动 -> 推到另一侧；两侧都改 -> 按小节合并
  S7 工作区规则按文件夹名匹配，不被路径差异干扰

用法: python selftest.py
"""
import json
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 源码在「开发/」下，沙盒必须落在项目根的 state 里（和 exe 用同一个 state）
ROOT = os.path.dirname(HERE) if os.path.basename(HERE) == "开发" else HERE
SB = os.path.join(ROOT, "state", "selftest")
if os.path.isdir(SB):
    shutil.rmtree(SB)
os.makedirs(SB, exist_ok=True)

# 用沙盒配置与沙盒状态目录，彻底隔离
os.environ["AGENTSYNC_STATE_DIR"] = os.path.join(SB, "state")
os.environ["AGENTSYNC_CONFIG"] = os.path.join(SB, "config.json")

sys.path.insert(0, HERE)
from asynclib import console as _console  # noqa: E402

_console.setup()

from asynclib import memory, paths, state, sync, workbuddy, zcode  # noqa: E402

HOME = os.path.expanduser("~")
REAL_ZDB = os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite")
REAL_TIDX = os.path.join(HOME, ".zcode", "v2", "tasks-index.sqlite")
REAL_WDB = os.path.join(HOME, ".workbuddy", "workbuddy.db")
REAL_WPROJ = os.path.join(HOME, ".workbuddy", "projects")

# ---------------------------------------------------------------- 沙盒搭建
ZDB = os.path.join(SB, "zcode.db")
WDB = os.path.join(SB, "workbuddy.db")
WPROJ = os.path.join(SB, "projects")

#: Zcode 侧「标准目录」的根。真实数据里是 <某盘>:\...\Documents\<工作区>，
#: 沙盒里统一换成这个中立路径（见下面的改写逻辑）。
CANON_ROOT = "D:\\sandbox\\Documents"


def snapshot(src, dst):
    s = sqlite3.connect(src, timeout=20)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    d.close()
    s.close()


os.makedirs(WPROJ, exist_ok=True)
snapshot(REAL_ZDB, ZDB)
snapshot(REAL_WDB, WDB)

# 沙盒里的历史记录带着**本机真实路径**。统一改写到中立的 CANON_ROOT 下：
# 既要脱敏（自测代码会进仓库），也让 S8 的「目录一致性」断言有确定的期望值。
# 只改沙盒副本，真实库一个字不动。
_c = sqlite3.connect(ZDB)
with _c:
    for _sid, _d in _c.execute("select id, directory from session").fetchall():
        if not _d or "Documents" not in _d:
            continue
        _tail = _d.split("Documents", 1)[1]
        _c.execute("update session set directory=? where id=?",
                   (CANON_ROOT + _tail, _sid))
_c.close()
# tasks 索引也必须快照进沙盒：它是 Zcode 侧边栏的数据源，不隔离的话
# 自测建的 S1/S2 测试会话会直接出现在用户真实的 Zcode 列表里（已实际踩过）。
TIDX = os.path.join(SB, "tasks-index.db")
if os.path.exists(REAL_TIDX):
    snapshot(REAL_TIDX, TIDX)
else:
    sqlite3.connect(TIDX).close()
# 只搬运真实 jsonl 里的一小批，够用即可
copied = 0
for root, _, files in os.walk(REAL_WPROJ):
    for f in files:
        if f.endswith(".jsonl") and copied < 30:
            rel = os.path.relpath(os.path.join(root, f), REAL_WPROJ)
            dst = os.path.join(WPROJ, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(root, f), dst)
            copied += 1

ZRULES = os.path.join(SB, "zcode-AGENTS.md")
WRULES = os.path.join(SB, "wb-AGENTS.md")
# 留档镜像也必须落在沙盒里：默认值是 ~/.workbuddy/AGENTS.md，
# 不覆盖的话自测会把测试数据写进真实用户目录。
WMIRROR = os.path.join(SB, "wb-mirror-AGENTS.md")
shutil.copy2(os.path.join(HOME, ".zcode", "AGENTS.md"), ZRULES)

cfg = {
    # 沙盒里放开覆盖：这里操作的是库副本，覆盖不到真实会话。
    # 写入器已能重建完整原生信封（tokens/cost/semantics/anchor/modelSelection），
    # 所以「较新一方覆盖较旧一方」这条需求要在这里被真正验证，而不是被闸门挡住。
    "zcode": {"db": ZDB, "rules_file": ZRULES, "include_subagents": False,
              "allow_overwrite": True,
              # 必须显式指向沙盒：默认值是真实路径，漏掉就会污染用户侧边栏
              "tasks_index": TIDX},
    "workbuddy": {"db": WDB, "projects_dir": WPROJ, "rules_file": WRULES,
                  "mirror_file": WMIRROR,
                  "write_project_rules": True, "project_rules_relpath": ".codebuddy/CODEBUDDY.md"},
    "sync": {"skip_active_sessions": False, "allow_cross_workspace_match": True,
             "equal_tolerance_ms": 1000, "normalize_workspace_paths": False,
             # 沙盒写的是库副本，与真实 Zcode 进程无关，不能套用「运行中禁写」
             "require_zcode_closed": False},
    "memory": {"backup_on_change": True, "max_rules_bytes": 8000},
}
json.dump(cfg, open(os.environ["AGENTSYNC_CONFIG"], "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

CFG = paths.load_config()
CFGP = paths.cfg_paths(CFG)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {detail}" if detail else ""))


def run(dry=False, revive=False, ws=None):
    plan = sync.make_plan(CFG, CFGP, only_workspace=ws, revive=revive, propagate_delete=True)
    acts = {}
    for i in plan["items"]:
        acts[i["action"]] = acts.get(i["action"], 0) + 1
    res = sync.apply_plan(CFG, CFGP, plan, dry_run=dry, verbose=False)
    print(f"    [plan] {acts}  写入={len(res['written'])} 删除={len(res['deleted'])} "
          f"z={len(zsessions())} w={len([x for x in wsessions() if not x['deleted']])}")
    for e in res.get("errors", []):
        print(f"      ! 写入失败 {e['title'][:20]}: {e['error'][:160]}")
    if VERBOSE_PLAN:
        for i in plan["items"]:
            if i["action"] not in ("in-sync",):
                print(f"      {i['action']:<6} {i['kind']:<12} {(i['z'] or i['w']).title[:26]:<28} {i['why']}")
    return plan, res


VERBOSE_PLAN = True


def zsessions():
    c = zcode.connect(ZDB, readonly=True)
    rows = [dict(r) for r in c.execute("select id,title,directory,time_created,time_updated from session "
                                       "where parent_id is null")]
    c.close()
    return rows


def wsessions(include_deleted=False):
    return workbuddy.list_sessions(WDB, include_deleted=include_deleted)


def count_events_z(sid):
    c = zcode.connect(ZDB, readonly=True)
    n = c.execute("select count(*) from part where session_id=?", (sid,)).fetchone()[0]
    c.close()
    return n


print("=" * 70)
print("S0 基线：先跑一次，建立配对")
print("=" * 70)
plan, res = run()
print(f"  计划 {plan['stats']}  写入 {len(res['written'])}")
check("S0 首次同步成功（无异常）",
      not [e for e in res["errors"] if "allow_overwrite" not in str(e.get("error", ""))],
      str([e for e in res["errors"] if "allow_overwrite" not in str(e.get("error", ""))][:1]))
check("S0 建立了配对", len(state.all_pairs(state.connect())) > 0,
      f"pairs={len(state.all_pairs(state.connect()))}")
# 「对侧已删除的会话不会被复活」不再在这里断言：
# 这条依赖真实库里恰好存在「Zcode 有、WorkBuddy 已删」的未配对会话，是环境相关
# 的假阳性来源（真实库里 7 个会话全部已配对，only_z=0，场景根本不存在）。
# 该需求由 S5 用自建场景确定性验证：软删配对会话 -> 同步删除 -> 写墓碑
# -> 再跑不复活 -> 源行被物理删除后仍不复活。

print()
print("=" * 70)
print("S4 幂等：再跑一次应当没有任何写入")
print("=" * 70)
LIVE_TITLE = "Zcode 对话迁移至 Workbudddy 方案"     # 这个会话正在被使用，内容一直在变
plan2, res2 = run()
extra = [w for w in res2["written"] if w.get("title") != LIVE_TITLE]
check("S4 第二次同步零写入（活跃会话除外）",
      not extra and len(res2["deleted"]) == 0,
      f"written={len(res2['written'])}（其中活跃会话 {len(res2['written']) - len(extra)}）")
check("S4 已同步的对话全部判定为一致",
      all(i["action"] == "in-sync" for i in plan2["items"]
          if i["kind"] == "pair" and (i["z"] or i["w"]).title != LIVE_TITLE))

print()
print("=" * 70)
print("S1 仅 Zcode 有 -> 复制到 WorkBuddy")
print("=" * 70)
from asynclib.model import Conversation, Event, KIND_ASSISTANT, KIND_USER, KIND_TOOL_CALL, KIND_TOOL_RESULT

src_z = zsessions()[0]
ws_name = "日常项目"
MARK1 = "S1-" + os.urandom(3).hex()
now1 = 1792740000000
conv1 = Conversation(side="zcode", native_id="tmp", workspace=ws_name,
                     workspace_path=src_z["directory"], title="S1-仅Zcode有",
                     created=now1, updated=now1,
                     events=[Event(KIND_USER, now1, text="S1 独有提问 " + MARK1),
                             Event(KIND_ASSISTANT, now1 + 1, text="S1 独有回复"),
                             Event(KIND_TOOL_CALL, now1 + 2, name="Bash",
                                   call_id="call_s1", args='{"command":"echo s1"}'),
                             Event(KIND_TOOL_RESULT, now1 + 2, name="Bash",
                                   call_id="call_s1", output="s1\n")])
sid_new, _, _, _ = zcode.write_conversation(ZDB, conv1, src_z["directory"])
wn = len([w for w in wsessions() if not w["deleted"]])
plan, res = run()
check("S1 新建的 Zcode 会话被复制到 WorkBuddy",
      len([w for w in wsessions() if not w["deleted"]]) == wn + 1
      and any(w["title"] == "S1-仅Zcode有" for w in wsessions()),
      f"wb {wn} -> {len([w for w in wsessions() if not w['deleted']])}")
w_new = [w for w in wsessions() if w["title"] == "S1-仅Zcode有"]
check("S1 WorkBuddy 侧事件数非零",
      bool(w_new) and os.path.getsize(workbuddy.jsonl_path(WPROJ, w_new[0]["cwd"], w_new[0]["id"])) > 100)

print()
print("=" * 70)
print("S2 仅 WorkBuddy 有 -> 复制到 Zcode")
print("=" * 70)
zn = len(zsessions())
now2 = now1 + 3600_000
conv2 = Conversation(side="wb", native_id="tmp2", workspace="agentsync-ws",
                     workspace_path=os.path.join(HOME, "Temp", "agentsync-ws"),
                     title="S2-仅WorkBuddy有", created=now2, updated=now2,
                     events=[Event(KIND_USER, now2, text="S2 独有提问 " + os.urandom(3).hex()),
                             Event(KIND_ASSISTANT, now2 + 1, text="S2 独有回复")])
wb_sid, _, _ = workbuddy.write_conversation(WDB, WPROJ, conv2, conv2.workspace_path)
plan, res = run()
check("S2 新建的 WorkBuddy 会话被复制到 Zcode",
      len(zsessions()) == zn + 1 and any(s["title"] == "S2-仅WorkBuddy有" for s in zsessions()),
      f"zcode {zn} -> {len(zsessions())}")

print()
print("=" * 70)
print("S3 时间较新的一方覆盖较旧的一方")
print("=" * 70)
# 找到 S1 那一对
pair = None
c = state.connect()
for p in state.all_pairs(c):
    zr = [s for s in zsessions() if s["id"] == p["zcode_id"]]
    wr = [w for w in wsessions() if w["id"] == p["wb_id"]]
    if zr and wr and zr[0]["title"] == "S1-仅Zcode有":
        pair = (zr[0], wr[0])
        break
c.close()
check("S3 找到 S1 建立的配对", pair is not None)

if pair:
    zr, wr = pair
    # 让 Zcode 侧变新：追加一条用户消息
    conv = zcode.read_conversation(ZDB, {"id": zr["id"], "title": zr["title"],
                                         "directory": zr["directory"],
                                         "time_created": zr["time_created"],
                                         "time_updated": zr["time_updated"],
                                         "workspace": ws_name})
    conv.events.append(Event(KIND_USER, zr["time_updated"] + 60000, text="ZCODE-NEWER-MARK"))
    conv.updated = zr["time_updated"] + 60000
    zcode.write_conversation(ZDB, conv, zr["directory"], existing_id=zr["id"])
    plan, res = run()
    wrow = [w for w in wsessions() if w["id"] == wr["id"]][0]
    text = open(workbuddy.jsonl_path(WPROJ, wrow["cwd"], wrow["id"]), encoding="utf-8").read()
    check("S3 Zcode 较新 -> WorkBuddy 被覆盖（含新标记）", "ZCODE-NEWER-MARK" in text)

    # 反向：让 WorkBuddy 侧更新
    conv2 = workbuddy.read_conversation(WPROJ, wrow)
    conv2.events.append(Event(KIND_USER, max(e.ts for e in conv2.events) + 60000,
                              text="WB-NEWER-MARK"))
    conv2.updated = max(e.ts for e in conv2.events)
    workbuddy.write_conversation(WDB, WPROJ, conv2, wrow["cwd"], existing_id=wrow["id"])
    plan, res = run()
    c = zcode.connect(ZDB, readonly=True)
    ztext = ""
    for r in c.execute("select data from part where session_id=?", (zr["id"],)):
        d = json.loads(r["data"])
        if d.get("type") == "text":
            ztext += d.get("text") or ""
    c.close()
    check("S3 WorkBuddy 较新 -> Zcode 被覆盖（含新标记）", "WB-NEWER-MARK" in ztext)

print()
print("=" * 70)
print("S5 删除传播：一侧删除，另一侧跟着删，且不会被复活")
print("=" * 70)
c = state.connect()
pairs = state.all_pairs(c)
c.close()
if pairs:
    p = pairs[0]
    workbuddy.soft_delete(WDB, p["wb_id"])
    plan, res = run()
    gone = not any(s["id"] == p["zcode_id"] for s in zsessions())
    check("S5 WorkBuddy 删除 -> Zcode 侧同步删除", gone, f"删除 {len(res['deleted'])} 个")
    c = state.connect()
    tomb = state.tombstones(c)
    c.close()
    check("S5 已写入墓碑", len(tomb) >= 1, f"墓碑 {len(tomb)} 条")
    plan2, res2 = run()
    still = not any(s["id"] == p["zcode_id"] for s in zsessions())
    check("S5 再跑一次不会复活", still and not any(i["kind"] == "only-z" for i in plan2["items"]
                                            if i["z"] and i["z"].native_id == p["zcode_id"]))
    # 墓碑在「源行被物理清掉」后仍然有效（WorkBuddy 清理历史时会真删行）
    c = sqlite3.connect(WDB)
    with c:
        c.execute("delete from sessions where id=?", (p["wb_id"],))
    c.close()
    plan3, res3 = run()
    check("S5 WorkBuddy 行被物理删除后也不会复活",
          not any(i["kind"] == "only-z" and i["z"] and i["z"].native_id == p["zcode_id"]
                  for i in plan3["items"]))

print()
print("=" * 70)
print("S6/S7 全局规则双向同步")
print("=" * 70)
z_text = memory.read_text(ZRULES)
write = lambda p, t: memory.write_text(p, t)          # noqa: E731
# 首次：把 Zcode 侧推到 WorkBuddy
r = memory.sync(verbose=False)
check("S6 首次运行把规则推到 WorkBuddy",
      memory.h(memory.read_text(WRULES)) == memory.h(memory.read_text(ZRULES)))

# 只有 WorkBuddy 侧改动
write(WRULES, memory.read_text(WRULES) + "\n## 仅WB新增的小节\n\n这行只应该出现在 WorkBuddy 侧。\n")
r = memory.sync(verbose=False)
check("S6 WorkBuddy 单侧改动 -> 推到 Zcode",
      "仅WB新增的小节" in (memory.read_text(ZRULES) or "")
      and memory.h(memory.read_text(ZRULES)) == memory.h(memory.read_text(WRULES)))

# 只有 Zcode 侧改动
write(ZRULES, memory.read_text(ZRULES) + "\n## 仅Zcode新增的小节\n\n这行只应该出现在 Zcode 侧。\n")
r = memory.sync(verbose=False)
check("S6 Zcode 单侧改动 -> 推到 WorkBuddy",
      "仅Zcode新增的小节" in (memory.read_text(WRULES) or ""))

# 两侧都改：不同小节 -> 应当合并
zt = memory.read_text(ZRULES).replace("这行只应该出现在 Zcode 侧。", "ZCODE改写了这一行")
wt = memory.read_text(WRULES).replace("这行只应该出现在 WorkBuddy 侧。", "WB改写了这一行")
write(ZRULES, zt)
write(WRULES, wt)
r = memory.sync(prefer="zcode", verbose=False)
merged = memory.read_text(ZRULES)
check("S6 两侧各改不同小节 -> 合并保留双方改动",
      "ZCODE改写了这一行" in merged and "WB改写了这一行" in merged)
check("S6 合并结果两侧一致",
      memory.h(memory.read_text(ZRULES)) == memory.h(memory.read_text(WRULES)))

# 工作区规则
wsdir = os.path.join(SB, "ws-日常项目")
os.makedirs(wsdir, exist_ok=True)
r = memory.sync(verbose=False, project_workspaces=[wsdir])
target = os.path.join(wsdir, ".codebuddy", "CODEBUDDY.md")
check("S7 规则写入工作区 .codebuddy/CODEBUDDY.md",
      os.path.exists(target) and "全局记忆" in open(target, encoding="utf-8").read())

print()
print("=" * 70)
print("S8 同名工作区目录统一（不统一的话 App 里会是两个同名分组）")
print("=" * 70)
live = [w for w in wsessions() if not w["deleted"]]
if live:
    name = live[0]["workspace"]
    canon_w = os.path.join(SB, "ws-canon", "日常项目")
    canon_z = CANON_ROOT + "\\" + name
    cfg2 = json.loads(json.dumps(CFG))
    cfg2["sync"]["normalize_workspace_paths"] = True
    cfg2["workspaces"] = {name: {"zcode": canon_z, "wb": canon_w}}
    plan2 = sync.make_plan(cfg2, CFGP)
    res2 = sync.apply_plan(cfg2, CFGP, plan2, dry_run=False, verbose=False)
    same_ws = [w for w in wsessions() if not w["deleted"] and w["workspace"] == name]
    # 被判为墓碑（对侧删过/归档过）的会话本来就不该动，不算"未统一"
    _skipped_w = {(i["w"].native_id if i["w"] else None)
                  for i in plan2["items"] if i["action"] == "skip"}
    bad = [w for w in same_ws
           if w["cwd"].replace("\\", "/") != canon_w.replace("\\", "/")
           and w["id"] not in _skipped_w]
    check("S8 该工作区在 WorkBuddy 侧的会话全部落到标准目录",
          bool(same_ws) and not bad,
          f"{len(same_ws)} 个会话，未统一 {len(bad)} 个（另 {len(_skipped_w)} 个按墓碑跳过）")
    f0 = workbuddy.jsonl_path(WPROJ, canon_w, same_ws[0]["id"])
    check("S8 换目录后 jsonl 跟着搬过去了", os.path.exists(f0))
    c = zcode.connect(ZDB, readonly=True)
    dirs = {r[0] for r in c.execute("select distinct directory from session "
                                    "where directory like ?", ("%Documents%",))}
    c.close()
    check("S8 Zcode 侧同一工作区目录写法一致",
          all(d.replace("/", "\\") == canon_z.replace("/", "\\")
              for d in dirs if d.rstrip("\\").endswith(name)) if dirs else True,
          str(sorted(dirs))[:80])
    plan3 = sync.make_plan(cfg2, CFGP)
    check("S8 统一后不再重复搬动",
          not [i for i in plan3["items"] if i["action"] == "fix-path"])

print()
print("=" * 70)
print("S9 Zcode 写入器：信封完整性 + 往返一致（这条挂了就会复现「会话空白」）")
print("=" * 70)
_wb, _ = workbuddy.read_all(WDB, WPROJ)
_idle = [c for c in _wb if (c.status or "").lower() not in workbuddy.ACTIVE_STATUS]
_src = max(_idle or _wb, key=lambda c: len(c.events)) if (_idle or _wb) else None
if _src is None:
    check("S9 沙盒里至少有一条 WorkBuddy 会话可用于写入", False)
else:
    _dir = CANON_ROOT + "\\日常项目"
    _sid, _upd, _nm, _np = zcode.write_conversation(ZDB, _src, _dir)
    _c = zcode.connect(ZDB, readonly=True)
    _rows = [json.loads(r["data"]) for r in
             _c.execute("select data from message where session_id=?", (_sid,))]
    _need_a = ["role", "time", "parentID", "modelId", "providerId", "mode", "planEnabled",
               "agent", "path", "cost", "tokens", "finish", "semantics", "anchor"]
    _need_u = ["role", "time", "agent", "modelSelection", "contextSnapshot",
               "semantics", "anchor", "tools"]
    _miss = 0
    for _d in _rows:
        _req = _need_a if _d.get("role") == "assistant" else _need_u
        if [k for k in _req if k not in _d]:
            _miss += 1
            continue
        if _d.get("role") == "assistant" and not isinstance(
                (_d.get("tokens") or {}).get("input"), int):
            _miss += 1
    check("S9 所有 message 的信封字段齐全（tokens.input 是数字）",
          _rows and _miss == 0, f"{_miss}/{len(_rows)} 条缺字段")

    _ent = {r["type"] for r in
            _c.execute("select type from session_entry where session_id=?", (_sid,))}
    check("S9 session_entry 三条运行时状态已重建",
          {"runtime/model_selection", "runtime/execution_state",
           "runtime/bash_shell_selection"} <= _ent, str(sorted(_ent)))
    _c.close()

    _row = [r for r in zcode.list_sessions(ZDB, include_subagents=True, tasks_index=TIDX, include_archived=True) if r["id"] == _sid][0]
    _back = zcode.read_conversation(ZDB, _row)
    check("S9 写入后读回的内容哈希与源一致（不回退就会反复重写）",
          _src.content_hash() == _back.content_hash(),
          f"{_src.content_hash()} vs {_back.content_hash()}")
    _st = [e.text for e in sorted(_src.events, key=lambda x: x.ts)
           if e.kind in ("user", "assistant", "reasoning")]
    _dt = [e.text for e in sorted(_back.events, key=lambda x: x.ts)
           if e.kind in ("user", "assistant", "reasoning")]
    check("S9 正文/思考文本逐条一致（无编码损坏）", _st == _dt,
          f"{len(_st)} vs {len(_dt)}")

    _snap = zcode.snapshot_session(ZDB, _sid)
    check("S9 覆盖前快照能完整取到四张表的行",
          bool(_snap) and _snap["session"] and _snap["message"] and _snap["part"],
          f"msg={len(_snap['message']) if _snap else 0}")

    # token 用量必须是真实值（WorkBuddy 的 message.usage / rawUsage），不是估算。
    # 用合成会话测，不依赖真实库里恰好有 usage。
    _now = 1793000000000
    _usrc = Conversation(side="wb", native_id="s9-token", workspace="日常项目",
                         workspace_path=_dir, title="S9 token 映射",
                         created=_now, updated=_now + 10,
                         events=[
                             Event(KIND_USER, _now, text="hi"),
                             Event(KIND_ASSISTANT, _now + 1, text="ok",
                                   usage={"input": 1234, "output": 56, "reasoning": 7,
                                          "cache_read": 100, "total": 1297},
                                   model="m-test"),
                         ])
    _uid, _u2, _u3, _u4 = zcode.write_conversation(ZDB, _usrc, _dir)
    _cc = zcode.connect(ZDB, readonly=True)
    _got = None
    for _r in _cc.execute("select data from message where session_id=?", (_uid,)):
        _d = json.loads(_r["data"])
        if _d.get("role") == "assistant":
            _got = _d
    _cc.close()
    _tk = (_got or {}).get("tokens") or {}
    check("S9 token 用量原样取自源记录（不是按内容估算）",
          _tk.get("input") == 1234 and _tk.get("output") == 56 and
          _tk.get("reasoning") == 7 and (_tk.get("cache") or {}).get("read") == 100
          and _tk.get("total") == 1297,
          f"got input={_tk.get('input')} output={_tk.get('output')} "
          f"reason={_tk.get('reasoning')} cache={(_tk.get('cache') or {}).get('read')}")
    check("S9 message 的 modelId 取源事件里的真实模型名",
          (_got or {}).get("modelId") == "m-test", (_got or {}).get("modelId"))

    zcode.write_conversation(ZDB, _src, _dir, existing_id=_sid)
    _row2 = [r for r in zcode.list_sessions(ZDB, include_subagents=True, tasks_index=TIDX, include_archived=True) if r["id"] == _sid][0]
    _back2 = zcode.read_conversation(ZDB, _row2)
    check("S9 覆盖已有会话后仍保持往返一致（幂等）",
          _src.content_hash() == _back2.content_hash(),
          f"{_src.content_hash()} vs {_back2.content_hash()}")

print()
print("=" * 70)
print("S10 沙盒隔离：自测的任何写入都不能落到真实用户目录")
print("=" * 70)
_out = {k: v for k, v in CFGP.items() if v and not os.path.abspath(v).startswith(SB)}
check("S10 所有落地路径都在 state/selftest 下", not _out, str(_out)[:160])
check("S10 tasks 索引指向沙盒（否则会污染 Zcode 侧边栏）",
      os.path.abspath(CFGP.get("zcode_tasks_index") or "").startswith(SB),
      CFGP.get("zcode_tasks_index"))

print()
print("=" * 70)
print("S11 已归档/已删除的会话不参与同步")
print("=" * 70)
_anow = 1793000000000 + 7200_000
_arc = Conversation(side="zcode", native_id="s11-arc", workspace="agentsync-ws",
                    workspace_path=os.path.join(HOME, "Temp", "agentsync-ws"),
                    title="S11-已归档会话", created=_anow, updated=_anow,
                    events=[Event(KIND_USER, _anow, text="S11 归档测试"),
                            Event(KIND_ASSISTANT, _anow + 1, text="回复")])
_aid, _, _, _ = zcode.write_conversation(ZDB, _arc, _arc.workspace_path)
zcode.upsert_task_index(TIDX, _aid, _arc.workspace_path, _arc.title, _anow, _anow)
_ac = sqlite3.connect(TIDX)
with _ac:
    _ac.execute("update tasks set archived=1 where task_id=?", (_aid,))
_ac.close()

_ids = {r["id"] for r in zcode.list_sessions(ZDB, True, tasks_index=TIDX)}
check("S11 已归档会话不出现在同步列表里", _aid not in _ids)
_ids2 = {r["id"] for r in zcode.list_sessions(ZDB, True, tasks_index=TIDX,
                                              include_archived=True)}
check("S11 include_archived=True 仍能读到（供墓碑判定）", _aid in _ids2)

_plan = sync.make_plan(CFG, CFGP)
_moves = [i for i in _plan["items"]
          if i["action"] in ("z2w", "w2z")
          and ((i["z"] and i["z"].native_id == _aid) or (i["w"] and i["w"].native_id == _aid))]
check("S11 计划里不会把它复制到 WorkBuddy", not _moves,
      str([i["action"] for i in _moves]))
check("S11 归档会话被记为墓碑（防止被对侧复活）",
      any(c_.native_id == _aid for c_ in _plan.get("zarchived", [])))

# 反向：把它取消归档，应当重新参与同步
_ac = sqlite3.connect(TIDX)
with _ac:
    _ac.execute("update tasks set archived=0 where task_id=?", (_aid,))
_ac.close()
_ids3 = {r["id"] for r in zcode.list_sessions(ZDB, True, tasks_index=TIDX)}
check("S11 取消归档后重新进入同步范围", _aid in _ids3)

print()
print("=" * 70)
print("S12 工具结果形状：写出来的 state 不能让 Zcode 打开会话时崩")
print("=" * 70)
# 背景（2026-10-27 实测）：Zcode 的 buildToolOutput 首行就是
# Buffer.byteLength(content)，content 取 state.error（error 分支）或 state.output
# （completed 分支）。任一侧不是字符串 -> ERR_INVALID_ARG_TYPE ->
# v4/conversation/subscribe 整体失败 -> 会话点开报错或整页空白。
# WorkBuddy 侧工具失败时 status="error" 且 output 常为空文本，早期写入器只搬
# status 不搬 error 文本，于是同步出来的记录正好踩中这个形状。
_now = 1793100000000
_dir = CANON_ROOT + "\\日常项目"
_ec = Conversation(side="wb", native_id="s12-toolshape", workspace="日常项目",
                   workspace_path=_dir, title="S12 工具结果形状", created=_now,
                   updated=_now, events=[
                       Event("user", _now, text="跑一下"),
                       Event("assistant", _now + 10, text="好"),
                       Event("tool_call", _now + 20, name="Bash", call_id="c_err",
                             args='{"command":"false"}'),
                       Event("tool_result", _now + 30, name="Bash", call_id="c_err",
                             output="", status="error"),
                       Event("tool_call", _now + 40, name="Read", call_id="c_null",
                             args='{"file":"a"}'),
                       Event("tool_result", _now + 50, name="Read", call_id="c_null",
                             output="null", status="completed"),
                   ])
_esid, _, _, _ = zcode.write_conversation(ZDB, _ec, _dir)
_audit = zcode.audit_tool_state(ZDB)
_mine = [x for x in _audit["fatal"] + _audit["warn"] if x["session_id"] == _esid]
check("S12 写入后该会话没有会让 Zcode 崩溃的工具结果", not _mine,
      str([(x["rule"], x["tool"]) for x in _mine]))

_ec2 = zcode.connect(ZDB, readonly=True)
_parts = [json.loads(r["data"]) for r in
          _ec2.execute("select data from part where session_id=?", (_esid,))]
_ec2.close()
_tools = [p for p in _parts if p.get("type") == "tool"]
_err_part = next((p for p in _tools if p.get("callID") == "c_err"), None)
_ok_part = next((p for p in _tools if p.get("callID") == "c_null"), None)
check("S12 status=error 的工具结果带上了字符串型 error 文本",
      bool(_err_part) and isinstance((_err_part["state"]).get("error"), str)
      and bool(_err_part["state"]["error"]),
      json.dumps((_err_part or {}).get("state", {}), ensure_ascii=False)[:120])
check('S12 字面量 "null" 输出被归一化成空串（否则会一路传染到对侧）',
      bool(_ok_part) and _ok_part["state"].get("output") == "",
      repr((_ok_part or {}).get("state", {}).get("output")))

print()
print("=" * 70)
print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:
    for f in FAIL:
        print(f"  ✗ {f}")
    print(f"沙盒保留在 {SB}（便于排查，可手动删除）")
else:
    shutil.rmtree(SB, ignore_errors=True)
    print("沙盒已清理。")
print("=" * 70)
sys.exit(1 if FAIL else 0)
