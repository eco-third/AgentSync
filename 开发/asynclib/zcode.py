"""Zcode 适配器：session / message / part 三张表 <-> 统一模型。

Zcode 把「工具调用」和「工具结果」放在同一个 part 里（state.input / state.output），
统一模型里拆成两个事件，回写时再合回去。

**message.data 是一层严格信封**（Zcode 运行时 resume 时直接读 tokens.input、
semantics、anchor 等字段）。少一个关键字段就会 `Cannot read properties of
undefined` -> resume 失败 -> 会话显示空白。所以写入器必须按原生结构构造，
不能只写 role + text。
"""
import json
import os
import sqlite3
import subprocess
import time
import uuid

from .model import (Conversation, Event, KIND_ASSISTANT, KIND_REASONING, KIND_TOOL_CALL,
                    KIND_TOOL_RESULT, KIND_USER, OUTPUT_NULL_LITERALS, norm_output,
                    project_id_for, strip_harness_noise,
                    scrub_controls, scrub_controls_deep,
                    workspace_name, zcode_msg_id, zcode_part_id, zcode_session_id)

SIDE = "zcode"
ACTIVE_HINT = ("zcode.exe",)

# 原生信封里的固定语义块（取值来自真实库统计，均为唯一稳定取值）
SEM_USER = {"origin": "real_user", "kind": "user_prompt",
            "uiVisibility": "visible", "providerVisibility": "visible",
            "transcriptVisibility": "visible"}
SEM_ASST = {"origin": "agent_runtime", "kind": "assistant_response",
            "uiVisibility": "visible", "providerVisibility": "visible",
            "transcriptVisibility": "visible"}


def is_running():
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True,
                             errors="replace", timeout=20).stdout.lower()
        return any(h in out for h in ACTIVE_HINT)
    except Exception:
        return False


def connect(db_path, readonly=False):
    if readonly:
        uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
        c = sqlite3.connect(uri, uri=True, timeout=15)
    else:
        c = sqlite3.connect(db_path, timeout=20)
        c.execute("pragma busy_timeout=20000")
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- 读
def read_archive_state(idx_path):
    """读 Zcode 侧边栏索引，得到 {session_id: (archived, deleted)}。

    Zcode 的「归档/删除」**只记在 tasks-index.sqlite 的 tasks 表里**，
    session 表的 time_archived 一直是空的（实测 19 条全空）。所以判断
    一个会话是否已归档/删除，必须看这里，不能看 session 表。

    注意：不能用 `file:...?mode=ro` 的 URI 形式 —— 这个库常常位于含中文的
    路径下（例如 `D:\\工作\\AI项目\\...`），SQLite 的 URI 解析对非 ASCII 会失败，
    而且异常会被静默吞掉，表现就是"归档状态读不到、会话照样被同步"。
    """
    if not idx_path or not os.path.exists(idx_path):
        return {}
    out = {}
    try:
        c = sqlite3.connect(idx_path, timeout=15)
        c.execute("pragma query_only=1")
        try:
            for r in c.execute("select task_id, archived, deleted from tasks"):
                out[r[0]] = (bool(r[1]), bool(r[2]))
        finally:
            c.close()
    except Exception:
        return {}
    return out


def list_sessions(db_path, include_subagents=False, tasks_index=None,
                  include_archived=False):
    c = connect(db_path, readonly=True)
    # 无论是否过滤都要读状态：include_archived=True 是"不过滤"，
    # 但调用方仍然需要 archived 标记来判定墓碑
    arch = read_archive_state(tasks_index)
    q = """select s.id, s.title, s.directory, s.time_created, s.time_updated,
                  s.parent_id, s.task_type,
                  (select count(*) from message m where m.session_id = s.id) as nmsg
           from session s order by s.time_created"""
    out = []
    for r in c.execute(q):
        d = dict(r)
        if not include_subagents and (d["parent_id"] or d["task_type"] == "subagent_child"
                                      or "_subagent_" in d["id"]):
            continue
        a, dl = arch.get(d["id"], (False, False))
        d["archived"] = a or dl
        # 已归档/已删除的会话不参与同步（用户明确要求），也不写到对侧
        if d["archived"] and not include_archived:
            continue
        d["workspace"] = workspace_name(d["directory"])
        out.append(d)
    c.close()
    return out


def read_conversation(db_path, session_row):
    c = connect(db_path, readonly=True)
    sid = session_row["id"]
    parts_by_msg = {}
    for r in c.execute("select message_id, sequence, data from part where session_id=? "
                       "order by message_id, sequence", (sid,)):
        parts_by_msg.setdefault(r["message_id"], []).append(json.loads(r["data"]))
    events = []
    for r in c.execute("select id, data from message where session_id=? order by sequence", (sid,)):
        m = json.loads(r["data"])
        role = m.get("role")
        ts = (m.get("time") or {}).get("created") or session_row["time_created"]
        plist = parts_by_msg.get(r["id"], [])
        if role == "user":
            for p in plist:
                if p.get("type") != "text" or p.get("synthetic"):
                    continue
                txt = strip_harness_noise(p.get("text") or "")
                if txt:
                    events.append(Event(KIND_USER, ts, text=txt))
            continue
        for p in plist:
            t = p.get("type")
            pts = (p.get("time") or {}).get("start") or ts
            if t == "reasoning":
                txt = (p.get("text") or "").strip()
                if txt:
                    events.append(Event(KIND_REASONING, pts, text=txt))
            elif t == "text":
                if p.get("synthetic"):
                    continue
                txt = (p.get("text") or "").strip()
                if txt:
                    events.append(Event(KIND_ASSISTANT, pts, text=txt))
            elif t == "tool":
                st = p.get("state") or {}
                call_id = p.get("callID") or ("call_" + zcode_part_id())
                # 调用时间优先取 state.time.start：原生 tool part 顶层没有 time，
                # 只有内层有；否则同一 message 里多个工具调用会读到同一时间戳
                pts = ((p.get("time") or {}).get("start")
                       or (st.get("time") or {}).get("start")
                       or ts)
                args = _unwrap_raw(st.get("input"))
                events.append(Event(KIND_TOOL_CALL, pts,
                                    name=p.get("tool") or st.get("title") or "tool",
                                    call_id=call_id,
                                    args=args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)))
                out = st.get("output")
                st_status = st.get("status") or "completed"
                # 不拿 state.error 去补 output：补齐会让「写入后读回」的内容与源事件不等，
                # 内容哈希随之漂移，每次同步都误判成「对方又更新了」而反复重写
                # （selftest S9 会直接抓到这个）。error 状态的文本由写入侧兜底补齐。
                #
                # output 缺失/为 None 时取 ""，**不能** json.dumps(None) —— 那会得到
                # 字符串 "null"，正是当初 34 条脏数据的另一种来源。
                if isinstance(out, str):
                    out_s = out
                elif out is None:
                    out_s = ""
                else:
                    out_s = json.dumps(out, ensure_ascii=False)
                # 结果与调用用同一时间戳：否则「同一时刻多个工具调用」读回时顺序会变，
                # 内容哈希随之漂移，同步就会误判成「对方又更新了」
                events.append(Event(KIND_TOOL_RESULT, pts,
                                    name=p.get("tool") or "tool", call_id=call_id,
                                    output=out_s, status=st_status))
    c.close()
    conv = Conversation(
        side=SIDE, native_id=sid,
        workspace=session_row["workspace"],
        workspace_path=session_row["directory"],
        title=session_row["title"],
        created=session_row["time_created"],
        updated=session_row["time_updated"],
        events=events,
        archived=bool(dict(session_row).get("archived", False)),
    )
    return conv


def read_all(db_path, include_subagents=False, tasks_index=None, include_archived=False):
    rows = list_sessions(db_path, include_subagents, tasks_index, include_archived)
    return [read_conversation(db_path, r) for r in rows], rows


# ---------------------------------------------------------------- 写
def _unwrap_raw(v):
    """写入器对「不是 JSON 对象」的参数会包成 {"_raw": ...}，读回时还原，保证往返一致。"""
    if isinstance(v, dict) and list(v.keys()) == ["_raw"]:
        return v["_raw"]
    return v


def _pack_args(args):
    """state.input 必须是对象（原生如此）。解析不出对象就包一层，读取时再拆。"""
    if isinstance(args, dict):
        return args
    try:
        o = json.loads(args) if args else {}
        return o if isinstance(o, dict) else {"_raw": args}
    except Exception:
        return {"_raw": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}


def _clean_output(v):
    """工具输出必须是「干净的字符串」，且不能是 "null" 这类字面量。

    归一化规则和 Event 入口保持同一份（model.norm_output），否则哈希会漂。
    """
    if not isinstance(v, str):
        v = "" if v is None else str(v)
    return norm_output(v)


def _apply_tool_result(state, status, output, end=None):
    """把工具结果写进 part.state，并保证 Zcode 重放时不会崩。

    根因（2026-10-27 实测，不是推测）：Zcode 打开会话时要重放历史工具结果，走
    tryBatchHydration -> applyHydrationEvent -> onToolCallResult -> buildToolOutput，
    其中 status=="error" 的分支是 `content: state.error`，紧接着
    `Buffer.byteLength(content, "utf8")`。只要 state.error 缺失，content 就是
    undefined，抛 ERR_INVALID_ARG_TYPE，整个 v4/conversation/subscribe 失败 ——
    表现是会话点开空白并弹 `The "string" argument must be of type string ...`。

    WB 侧 status=error 的工具结果 output 往往是空文本，同步过来就正好是这个形状，
    所以 error 状态必须由这里兜底写一个字符串。
    """
    st = status or "completed"
    out = _clean_output(output)
    state["status"] = st
    state["output"] = out
    if end is not None:
        state["time"]["end"] = max(int(end), int(state["time"].get("end") or 0))
    if st == "completed":
        state.pop("error", None)
    else:
        title = state.get("title") or "tool"
        state["error"] = out or ("Tool execution failed (%s)." % title)
    return state


def _new_tool_state(args, name, ts):
    state = {"status": "completed", "input": scrub_controls_deep(_pack_args(args)),
             "output": "", "title": name or "tool", "metadata": {},
             "time": {"start": ts, "end": ts}}
    return state


def _est_tokens(text):
    """粗估 token：中英混排约 3 字节/token。只用于让 UI 的用量看着合理，不追求精确。"""
    if not text:
        return 0
    return max(0, len(text.encode("utf-8")) // 3)


def _default_model(c, directory=None):
    """沿用本库里最近一条原生 assistant 消息的 provider/model，避免写 "synced" 这种假值。"""
    q = ("select data from message where data like '%\"role\":\"assistant\"%' "
         "and data not like '%syncedBy%' order by time_created desc limit 200")
    for (d,) in c.execute(q):
        try:
            o = json.loads(d)
        except Exception:
            continue
        m, pr = o.get("modelId"), o.get("providerId")
        if m and pr and m != "synced" and pr != "synced":
            return pr, m
    return "glm", "glm"


def _project_id(c, directory):
    r = c.execute("select project_id from session where directory=? limit 1", (directory,)).fetchone()
    return r["project_id"] if r else project_id_for(directory)


def _version(c):
    r = c.execute("select version from session order by time_created desc limit 1").fetchone()
    return r["version"] if r else "0.16.9"


def _build_rows(conv, directory, session_id, project_id, version,
                provider="glm", model="glm", mode="build", estimate_tokens=True):
    """把事件序列拆成 message/part 行（Zcode 的两层结构）+ 完整原生信封。"""
    msgs = []          # [{'id','role','ts','end','parts':[...], 'calls':{}, 'turn','round',...}]
    st = {"turn": None, "round": 0, "prev_user": None}
    # 会话内真实用过的模型：来源侧每条事件都带，用它统一 user 消息的 modelSelection
    real_model = next((e.model for e in conv.events if e.model), "") or model

    def new_msg(role, ts):
        if role == "user":
            st["turn"] = "turn_" + str(uuid.uuid4())
            st["round"] += 1
            st["prev_user"] = None
        m = {"id": zcode_msg_id(), "role": role, "ts": int(ts), "end": int(ts),
             "parts": [], "calls": {}, "turn": st["turn"], "round": st["round"],
             "parent": st["prev_user"], "usage": None, "model": ""}
        msgs.append(m)
        if role == "user":
            st["prev_user"] = m["id"]
        return m

    def touch(m, ts):
        if ts > m["end"]:
            m["end"] = int(ts)

    def absorb(m, e):
        """把事件附带的真实用量/模型名记到所属 message 上。

        一个 message 内可能有多条带 usage 的事件（不同 step：文本行与工具调用行各有一份），
        逐字段取较大值 —— 上下文随 step 递增，取最大最接近该 message 结束时的真实状态；
        reasoning 单独取最大，避免被不含该字段的来源抹成 0。
        """
        if e.usage:
            cur = m["usage"]
            if not cur:
                m["usage"] = dict(e.usage)
            else:
                for k in ("input", "output", "cache_read", "total", "reasoning"):
                    cur[k] = max(cur.get(k) or 0, e.usage.get(k) or 0)
        if e.model:
            m["model"] = e.model

    cur = None
    for e in sorted(conv.events, key=lambda x: x.ts):
        if e.kind == KIND_USER:
            cur = new_msg("user", e.ts)
            cur["parts"].append({"type": "text", "text": scrub_controls(e.text),
                                 "time": {"start": e.ts, "end": e.ts}})
            touch(cur, e.ts)
        elif e.kind == KIND_ASSISTANT:
            if not cur or cur["role"] != "assistant":
                cur = new_msg("assistant", e.ts)
            cur["parts"].append({"type": "text", "text": scrub_controls(e.text),
                                 "time": {"start": e.ts, "end": e.ts}})
            touch(cur, e.ts)
            absorb(cur, e)
        elif e.kind == KIND_REASONING:
            if not cur or cur["role"] != "assistant":
                cur = new_msg("assistant", e.ts)
            cur["parts"].append({"type": "reasoning", "text": scrub_controls(e.text),
                                 "time": {"start": e.ts, "end": e.ts}})
            touch(cur, e.ts)
            absorb(cur, e)
        elif e.kind == KIND_TOOL_CALL:
            if not cur or cur["role"] != "assistant":
                cur = new_msg("assistant", e.ts)
            part = {"type": "tool", "callID": e.call_id, "declarationIndex": len(cur["calls"]),
                    "tool": e.name or "tool",
                    "state": _new_tool_state(e.args, e.name, e.ts)}
            cur["parts"].append(part)
            cur["calls"][e.call_id] = part
            touch(cur, e.ts)
            absorb(cur, e)
        elif e.kind == KIND_TOOL_RESULT:
            holder = cur["calls"].get(e.call_id) if cur else None
            if holder is not None:
                _apply_tool_result(holder["state"], e.status, e.output, e.ts)
                touch(cur, e.ts)
            else:                       # 只有结果没有调用：补一条调用占位
                if not cur or cur["role"] != "assistant":
                    cur = new_msg("assistant", e.ts)
                cid = e.call_id or ("call_" + zcode_part_id())
                part = {"type": "tool", "callID": cid,
                        "declarationIndex": len(cur["calls"]), "tool": e.name or "tool",
                        "state": _apply_tool_result(_new_tool_state({}, e.name, e.ts),
                                                    e.status, e.output, e.ts)}
                cur["parts"].append(part)
                cur["calls"][cid] = part
                touch(cur, e.ts)

    # 组装 session 行
    created = conv.created or (msgs[0]["ts"] if msgs else int(time.time() * 1000))
    updated = max([conv.updated or 0] + [m["end"] for m in msgs] + [created])
    session_row = {
        "id": session_id, "project_id": project_id, "workspace_id": None, "parent_id": None,
        "slug": session_id, "directory": directory, "path": directory,
        "title": conv.title or "(无标题)", "version": version, "share_url": None,
        "summary_additions": None, "summary_deletions": None, "summary_files": None,
        "summary_diffs": None, "revert": None,
        "permission": json.dumps({"mode": mode}),
        "time_created": created, "time_updated": updated,
        "time_compacting": None, "time_archived": None,
        "task_type": "interactive", "title_source": "custom", "title_message_id": None,
        "time_title_updated": None, "trace_id": None,
    }

    def _tokens(inp, outn, rean, cache_read=0):
        return {"input": int(inp), "output": int(outn), "reasoning": int(rean),
                "cache": {"read": int(cache_read), "write": 0},
                "total": int(inp) + int(outn) + int(rean)}

    msg_rows, part_rows = [], []
    for seq, m in enumerate(msgs):
        if m["role"] == "user":
            text = "".join(p.get("text") or "" for p in m["parts"] if p.get("type") == "text")
            sel = {"providerId": provider, "modelId": real_model,
                   "options": {"reasoningLevel": "max"}}
            data = {
                "role": "user",
                "time": {"created": m["ts"]},
                "agent": "zcode-agent",
                "modelSelection": sel,
                "contextSnapshot": {"envInfo": {"cwd": directory, "platform": "win32",
                                                "shell": "system shell",
                                                "isGitRepository": False,
                                                "gitStatus": "not_repo"}},
                "semantics": dict(SEM_USER),
                "anchor": {"turnId": m["turn"], "origin": "realUser"},
                "tools": {},
                "metadata": {"inputIntent": {
                    "kind": "sendText", "text": text, "modelSelection": sel,
                    "mode": mode, "planEnabled": False,
                    "requestedDelivery": "startNow", "admittedDelivery": "startNow"}},
                "model": {"providerID": provider, "modelID": real_model},
                "syncedBy": "AgentSync",
            }
        else:
            # 优先用 WorkBuddy 记录的真实用量；拿不到才按内容估算（且 input 不累加，
            # 累加历史会把上下文虚报成几十万，UI 上会显示「上下文将满」）
            if m.get("usage"):
                u = m["usage"]
                tok = _tokens(u["input"], u["output"], u["reasoning"], u.get("cache_read", 0))
            elif estimate_tokens:
                outn = _est_tokens("".join(p.get("text") or "" for p in m["parts"]
                                           if p.get("type") == "text"))
                rean = _est_tokens("".join(p.get("text") or "" for p in m["parts"]
                                           if p.get("type") == "reasoning"))
                tok = _tokens(0, outn, rean)
            else:
                tok = _tokens(0, 0, 0)
            data = {
                "role": "assistant",
                "time": {"created": m["ts"], "completed": max(m["end"], m["ts"])},
                "parentID": m["parent"],
                "modelId": m["model"] or real_model,
                "providerId": provider,
                "mode": mode,
                "planEnabled": False,
                "agent": "zcode-agent",
                "path": {"cwd": directory, "root": directory},
                "cost": 0,
                "tokens": tok,
                "finish": "tool-calls" if m["calls"] else "stop",
                "semantics": dict(SEM_ASST),
                "anchor": {"turnId": m["turn"], "historyRoundCount": m["round"],
                           "orderedMessageIds": [x for x in (m["parent"], m["id"]) if x],
                           "boundaryMessageId": m["id"],
                           "goalBoundary": {"kind": "none"}},
                "syncedBy": "AgentSync",
            }
        msg_rows.append({"id": m["id"], "ts": m["ts"], "seq": seq,
                         "data": json.dumps(data, ensure_ascii=False)})
        for pseq, p in enumerate(m["parts"]):
            part_rows.append({"id": zcode_part_id(), "message_id": m["id"], "ts": m["ts"],
                              "seq": pseq, "data": json.dumps(p, ensure_ascii=False)})
    return session_row, msg_rows, part_rows



_ENTRY_TMPL = (
    ("runtime/model_selection",
     lambda p, m, mode: {"modelSelection": {"providerId": p, "modelId": m,
                                            "options": {"reasoningLevel": "max"}}}),
    ("runtime/bash_shell_selection",
     lambda p, m, mode: {"dialect": "legacy-shell", "display": {"name": "system shell"},
                         "source": "legacy-fallback"}),
    ("runtime/execution_state",
     lambda p, m, mode: {"mode": mode, "planEnabled": False}),
)
_ENTRY_ID_PREFIX = {"runtime/model_selection": ":runtime-model-selection",
                    "runtime/bash_shell_selection": ":runtime:bash_shell_selection",
                    "runtime/execution_state": ":runtime-execution-state"}


def ensure_entries(c, session_id, provider, model, mode, now_ms):
    """补齐 Zcode 的会话运行时状态。

    resume 时读不到 execution_state / model_selection 会直接失败，所以覆盖会话后
    必须重建这三条（workspace_checkpoint 依赖真实工具产物，不做伪造）。
    """
    have = {r[0] for r in c.execute(
        "select type from session_entry where session_id=?", (session_id,))}
    n = 0
    for typ, mk in _ENTRY_TMPL:
        if typ in have:
            continue
        c.execute("insert or replace into session_entry "
                  "(id, session_id, type, time_created, time_updated, data) "
                  "values (?,?,?,?,?,?)",
                  (session_id + _ENTRY_ID_PREFIX[typ], session_id, typ,
                   now_ms, now_ms,
                   json.dumps(mk(provider, model, mode), ensure_ascii=False)))
        n += 1
    return n


def _write_conv(c, conv, directory, provider, model, mode, estimate_tokens=True):
    project_id = _project_id(c, directory)
    version = _version(c)
    sid = zcode_session_id()
    srow, mrows, prows = _build_rows(conv, directory, sid, project_id, version,
                                     provider, model, mode, estimate_tokens)
    cols = list(srow.keys())
    c.execute(f"insert into session ({','.join(cols)}) values ({','.join('?' * len(cols))})",
              [srow[k] for k in cols])
    c.executemany("insert into message (id, session_id, time_created, time_updated, data, sequence) "
                  "values (?,?,?,?,?,?)",
                  [(m["id"], sid, m["ts"], m["ts"], m["data"], m["seq"]) for m in mrows])
    c.executemany("insert into part (id, message_id, session_id, time_created, time_updated, data, sequence) "
                  "values (?,?,?,?,?,?,?)",
                  [(p["id"], p["message_id"], sid, p["ts"], p["ts"], p["data"], p["seq"]) for p in prows])
    ensure_entries(c, sid, provider, model, mode, srow["time_created"])
    return sid, srow["time_updated"], len(mrows), len(prows)


def _rewrite_conv(c, conv, session_id, directory, provider, model, mode, estimate_tokens=True):
    project_id = _project_id(c, directory)
    version = _version(c)
    srow, mrows, prows = _build_rows(conv, directory, session_id, project_id, version,
                                     provider, model, mode, estimate_tokens)
    c.execute("delete from part where session_id=?", (session_id,))
    c.execute("delete from message where session_id=?", (session_id,))
    c.execute("delete from session_entry where session_id=?", (session_id,))
    c.execute("update session set title=?, project_id=?, version=?, time_created=?, time_updated=?, "
              "directory=?, path=? where id=?",
              (srow["title"], srow["project_id"], srow["version"], srow["time_created"],
               srow["time_updated"], directory, directory, session_id))
    c.executemany("insert into message (id, session_id, time_created, time_updated, data, sequence) "
                  "values (?,?,?,?,?,?)",
                  [(m["id"], session_id, m["ts"], m["ts"], m["data"], m["seq"]) for m in mrows])
    c.executemany("insert into part (id, message_id, session_id, time_created, time_updated, data, sequence) "
                  "values (?,?,?,?,?,?,?)",
                  [(p["id"], p["message_id"], session_id, p["ts"], p["ts"], p["data"], p["seq"]) for p in prows])
    ensure_entries(c, session_id, provider, model, mode, srow["time_created"])
    return session_id, srow["time_updated"], len(mrows), len(prows)


def write_conversation(db_path, conv, directory, existing_id=None,
                       provider=None, model=None, mode=None, estimate_tokens=True):
    """existing_id 为空 -> 新建；否则整段覆盖。返回 (id, updated, 消息数, part 数)。"""
    c = connect(db_path)
    try:
        if not provider or not model:
            provider, model = _default_model(c, directory)
        if not mode:
            mode = "build"
        with c:
            if existing_id:
                res = _rewrite_conv(c, conv, existing_id, directory,
                                    provider, model, mode, estimate_tokens)
            else:
                res = _write_conv(c, conv, directory, provider, model, mode, estimate_tokens)
        return res
    finally:
        c.close()



def delete_conversation(db_path, session_id):
    c = connect(db_path)
    try:
        with c:
            c.execute("delete from part where session_id=?", (session_id,))
            c.execute("delete from message where session_id=?", (session_id,))
            c.execute("delete from session_entry where session_id=?", (session_id,))
            c.execute("delete from session where id=?", (session_id,))
    finally:
        c.close()


# ---------------------------------------------------------------- 覆盖前快照
def snapshot_session(db_path, session_id):
    """把一条会话的全部原始行导出成 dict。

    覆盖 Zcode 会话前必须留这个：只 dump 事件序列不够，信封字段（tokens/cost/
    semantics/anchor/modelSelection）一旦被覆盖就再也推不回来。
    """
    c = connect(db_path, readonly=True)
    try:
        s = c.execute("select * from session where id=?", (session_id,)).fetchone()
        if not s:
            return None
        return {
            "session": dict(s),
            "message": [dict(r) for r in c.execute(
                "select * from message where session_id=? order by sequence", (session_id,))],
            "part": [dict(r) for r in c.execute(
                "select * from part where session_id=? order by sequence", (session_id,))],
            "session_entry": [dict(r) for r in c.execute(
                "select * from session_entry where session_id=?", (session_id,))],
        }
    finally:
        c.close()


def restore_session(db_path, snap):
    """把 snapshot_session 的结果原样写回（用于事故回滚）。"""
    sid = snap["session"]["id"]
    c = connect(db_path)
    try:
        with c:
            c.execute("delete from part where session_id=?", (sid,))
            c.execute("delete from message where session_id=?", (sid,))
            c.execute("delete from session_entry where session_id=?", (sid,))
            c.execute("delete from session where id=?", (sid,))
            cols = list(snap["session"].keys())
            c.execute(f"insert into session ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                      [snap["session"][k] for k in cols])
            for r in snap["message"]:
                cols = list(r.keys())
                c.execute(f"insert into message ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                          [r[k] for k in cols])
            for r in snap["part"]:
                cols = list(r.keys())
                c.execute(f"insert into part ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                          [r[k] for k in cols])
            for r in snap["session_entry"]:
                cols = list(r.keys())
                c.execute(f"insert into session_entry ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                          [r[k] for k in cols])
    finally:
        c.close()


# ---------------------------------------------------------------- 界面索引
def upsert_task_index(idx_path, session_id, directory, title, created, updated,
                      provider="glm", model=None, mode="build", status="completed"):
    """写进 Zcode 的界面索引，否则会话存在于 db 里但列表里看不到。

    tasks-index.sqlite 是 Zcode 侧边栏的数据源，主键 (workspace_key, task_id)。
    用 insert or replace + 保留原有 pinned/group 等用户字段（已存在时只更新必要列）。
    """
    if not idx_path or not os.path.exists(idx_path):
        return False
    c = sqlite3.connect(idx_path, timeout=20)
    try:
        c.row_factory = sqlite3.Row
        exist = c.execute("select * from tasks where workspace_key=? and task_id=?",
                          (directory, session_id)).fetchone()
        meta = {"taskId": session_id, "title": title or "", "titleOverridden": True,
                "workspacePath": directory, "createdAt": int(created),
                "updatedAt": int(updated), "mode": mode, "provider": provider,
                "status": status}
        if exist:
            d = dict(exist)
            try:
                old = json.loads(d.get("meta_json") or "{}")
                old.update(meta)
                meta = old
            except Exception:
                pass
            # **不动 archived / deleted**：那是用户在 Zcode 里的选择，
            # 同步不应该把已归档的会话重新翻出来
            with c:
                c.execute("update tasks set title=?, workspace_path=?, task_status=?, "
                          "provider=?, updated_at=?, meta_json=? "
                          "where workspace_key=? and task_id=?",
                          (title or "(无标题)", directory, status, provider, int(updated),
                           json.dumps(meta, ensure_ascii=False), directory, session_id))
        else:
            with c:
                c.execute("insert into tasks (workspace_key, workspace_path, workspace_identity, "
                          "task_id, title, task_status, provider, mode, model, created_at, "
                          "updated_at, unread_at, last_unread_at, pinned, archived, deleted, "
                          "title_overridden, meta_json, searchable_text) "
                          "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (directory, directory, None, session_id, title or "(无标题)",
                           status, provider, mode, model, int(created), int(updated),
                           None, 0, 0, 0, 0, 1,
                           json.dumps(meta, ensure_ascii=False), ""))
        return True
    finally:
        c.close()


# ---------------------------------------------------------------- 侧边栏索引体检
def audit_orphan_tasks(db_path, index_path):
    """找侧边栏索引里「有 task 记录、但 session 表里没有」的孤儿条目。

    这类条目在界面上可以点，点开是空白或抛
    `Received undefined`（渲染时拿不到 session）。成因是写索引时会话行没写成，
    或索引指向了已被删掉的会话。

    :return: {"total": n, "orphans": [dict(task_id, title, archived, workspace_path)]}
    """
    result = {"total": 0, "orphans": []}
    if not index_path or not os.path.exists(index_path):
        return result

    z = sqlite3.connect(db_path)
    try:
        z.execute("pragma query_only=1")
        live = {r[0] for r in z.execute("select id from session")}
    finally:
        z.close()

    ic = sqlite3.connect(index_path)
    try:
        ic.execute("pragma query_only=1")
        try:
            rows = ic.execute("select task_id, title, archived, workspace_path from tasks").fetchall()
        except sqlite3.Error:
            return result
    finally:
        ic.close()

    result["total"] = len(rows)
    result["orphans"] = [{"task_id": r[0], "title": r[1] or "", "archived": r[2],
                          "workspace_path": r[3] or ""}
                         for r in rows if r[0] not in live]
    return result


def fix_orphan_tasks(index_path, orphans):
    """删掉孤儿条目。调用方负责先备份。返回删除条数。"""
    if not orphans or not index_path or not os.path.exists(index_path):
        return 0
    ic = sqlite3.connect(index_path)
    n = 0
    try:
        has_members = bool(ic.execute(
            "select 1 from sqlite_master where type='table' and name='task_group_members'"
        ).fetchone())
        for o in orphans:
            if has_members:
                try:
                    ic.execute("delete from task_group_members where task_id=?", (o["task_id"],))
                except sqlite3.Error:
                    pass
            cur = ic.execute("delete from tasks where task_id=?", (o["task_id"],))
            n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        ic.commit()
    finally:
        ic.close()
    return n


# ---------------------------------------------------------------- 体检
#: 会让 Zcode 打开会话直接失败的形状（app-server 抛 ERR_INVALID_ARG_TYPE）
RULE_ERROR_NO_TEXT = "error_without_text"
RULE_OUTPUT_NOT_STR = "output_not_string"
RULE_CONTROL = "control_chars"
#: 不致命但会被显示成 "null" 的脏值
RULE_LITERAL = "literal_output"

_FATAL_RULES = (RULE_ERROR_NO_TEXT, RULE_OUTPUT_NOT_STR)


def audit_tool_state(db_path):
    """扫描 part 表里会让 Zcode 崩溃的字段形状。

    判据来自 Zcode 自身代码（`resources/glm/zcode.cjs`）：
    `bPn(e,t)` 即 buildToolOutput，首行就是 `Buffer.byteLength(e.content,"utf8")`；
    `content` 在 error 分支取 `state.error`、completed 分支取 `state.output`。
    任一侧不是字符串 -> undefined -> 抛
    `The "string" argument must be of type string ... Received undefined`
    -> 整个 `v4/conversation/subscribe` 失败 -> 会话点开报错或空白。

    :return: {"scanned": n, "fatal": [...], "warn": [...]}
             每条问题是 dict(part_id, session_id, tool, rule, detail)
    """
    fatal, warn = [], []
    scanned = 0
    c = sqlite3.connect(db_path)
    try:
        c.execute("pragma query_only=1")
        rows = c.execute("select id, session_id, data from part").fetchall()
    finally:
        c.close()

    for pid, sid, data in rows:
        scanned += 1
        if scrub_controls(data or "") != (data or ""):
            warn.append({"part_id": pid, "session_id": sid, "tool": "",
                         "rule": RULE_CONTROL, "detail": "文本含 NUL 等控制字符"})
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if obj.get("type") != "tool":
            continue
        st = obj.get("state")
        if not isinstance(st, dict):
            fatal.append({"part_id": pid, "session_id": sid,
                          "tool": obj.get("tool") or "", "rule": RULE_OUTPUT_NOT_STR,
                          "detail": "state 不是对象"})
            continue
        tool = obj.get("tool") or st.get("title") or "tool"
        status = st.get("status") or "completed"
        if status == "completed":
            out = st.get("output")
            if not isinstance(out, str):
                fatal.append({"part_id": pid, "session_id": sid, "tool": tool,
                              "rule": RULE_OUTPUT_NOT_STR,
                              "detail": "completed 但 output 不是字符串（%s）" % type(out).__name__})
            elif out in OUTPUT_NULL_LITERALS:
                warn.append({"part_id": pid, "session_id": sid, "tool": tool,
                             "rule": RULE_LITERAL, "detail": 'output 是字面量 "%s"' % out})
        else:
            err = st.get("error")
            if not (isinstance(err, str) and err.strip()):
                fatal.append({"part_id": pid, "session_id": sid, "tool": tool,
                              "rule": RULE_ERROR_NO_TEXT,
                              "detail": "status=%s 但缺少 state.error" % status})
    return {"scanned": scanned, "fatal": fatal, "warn": warn}


def fix_tool_state(db_path, audit_result):
    """按 audit_tool_state 的结果修库。调用方负责先备份、并确保 Zcode 已退出。

    :return: 实际改动的行数
    """
    items = list(audit_result.get("fatal") or []) + list(audit_result.get("warn") or [])
    if not items:
        return 0
    changed = 0
    c = sqlite3.connect(db_path)
    try:
        for it in items:
            row = c.execute("select data from part where id=?", (it["part_id"],)).fetchone()
            if not row:
                continue
            try:
                obj = json.loads(row[0])
            except Exception:
                continue
            st = obj.get("state")
            if not isinstance(st, dict):
                continue
            if it["rule"] == RULE_ERROR_NO_TEXT:
                _apply_tool_result(st, st.get("status"), st.get("output"))
            elif it["rule"] == RULE_OUTPUT_NOT_STR:
                st["output"] = _clean_output(st.get("output"))
            elif it["rule"] == RULE_LITERAL:
                st["output"] = ""
            elif it["rule"] == RULE_CONTROL:
                if isinstance(st.get("output"), str):
                    st["output"] = scrub_controls(st["output"])
                if isinstance(st.get("error"), str):
                    st["error"] = scrub_controls(st["error"])
                if isinstance(st.get("input"), (dict, list)):
                    st["input"] = scrub_controls_deep(st["input"])
                if isinstance(obj.get("text"), str):
                    obj["text"] = scrub_controls(obj["text"])
            else:
                continue
            c.execute("update part set data=? where id=?",
                      (json.dumps(obj, ensure_ascii=False, separators=(",", ":")), it["part_id"]))
            changed += 1
        c.commit()
    finally:
        c.close()
    return changed

