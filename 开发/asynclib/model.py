"""统一会话模型：两边数据库结构完全不同，先归一成 Conversation/Event 再谈同步。"""
import hashlib
import json
import os
import re
import uuid

# ---------------------------------------------------------------- 事件
KIND_USER = "user"
KIND_ASSISTANT = "assistant"
KIND_REASONING = "reasoning"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"


def _norm_blob(s):
    """把 JSON 文本归一成可比较形式。

    Zcode 的 state.input/output 是对象，WorkBuddy 的 arguments 是原始字符串；
    写回再读出来时空格、键序都会变（'{"a":1}' -> '{"a": 1}'），
    若直接比字符串就会永远判定「内容变了」，同步反复重写。所以按语义比较。
    """
    if not s:
        return ""
    t = s.strip()
    if t[:1] in "{[":
        try:
            return json.dumps(json.loads(t), sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False)
        except Exception:
            pass
    return s


class Event:
    """一条消息级事件。工具调用与结果拆开，靠 call_id 关联。

    usage / model 是「附带信息」，**不参与 key() 与内容哈希**：
    WorkBuddy 有真实用量而 Zcode 读回来只有自己那份，算进哈希就会永远不等。
    """

    __slots__ = ("kind", "ts", "text", "name", "call_id", "args", "output", "status",
                 "usage", "model")

    def __init__(self, kind, ts, text="", name="", call_id="", args="", output="",
                 status="", usage=None, model=""):
        self.kind = kind
        self.ts = int(ts)
        # NUL 等控制字符在这里就摘掉：哈希与落库必须看到同一份数据，
        # 否则「写入前算哈希 -> 写入时清洗 -> 读回哈希不等」会破坏幂等。
        self.text = scrub_controls(text)
        self.name = name
        self.call_id = call_id
        self.args = args
        self.output = norm_output(output)
        self.status = status
        self.usage = usage or None      # {'input','output','reasoning','cache_read','total'}
        self.model = model or ""        # 该步真实使用的模型名

    def key(self):
        """用于判断内容是否变化（不含时间与 id）。"""
        h = hashlib.sha1()
        for x in (self.kind, self.text, self.name, self.call_id,
                  _norm_blob(self.args), _norm_blob(self.output), self.status):
            h.update((x or "").encode("utf-8", "ignore"))
            h.update(b"\x00")
        return h.hexdigest()[:16]

    def __repr__(self):
        return f"<{self.kind} {self.ts} {self.text[:20]!r}>"


def _norm_status(s):
    """空状态与 completed 视为同义：Zcode 的 tool part 没有「进行中」这一态。"""
    return "completed" if s in ("", None, "success", "completed") else s


class Conversation:
    def __init__(self, side, native_id, workspace, workspace_path, title,
                 created, updated, events=None, status="", deleted=False,
                 archived=False):
        self.side = side                      # 'zcode' | 'wb'
        self.native_id = native_id
        self.workspace = workspace            # 归一化后的文件夹名（小写）
        self.workspace_path = workspace_path  # 原始绝对路径
        self.title = title or "(无标题)"
        self.created = int(created or 0)
        self.updated = int(updated or 0)
        self.events = events or []
        self.status = status
        self.deleted = bool(deleted)          # 软删除（WorkBuddy 的 deleted_at）
        self.archived = bool(archived)        # 已归档：不参与同步，也不写到对侧

    # --- 指纹：判断「两边是不是同一个对话」的核心依据 ---
    def fingerprint(self):
        users = [e for e in sorted(self.events, key=lambda x: x.ts)
                 if e.kind == KIND_USER and e.text.strip()]
        head = "\x1f".join(norm_text(e.text)[:400] for e in users[:3])
        if not head:                       # 没有用户消息时退化为标题
            head = norm_text(self.title)
        bucket = self.created // 60000     # 分钟级容差
        return hashlib.sha1(f"{head}\x1e{bucket}".encode("utf-8", "ignore")).hexdigest()

    def content_hash(self):
        """内容哈希（按时间排序后比较）。

        两个必须做的归一化，都是「同内容在两端的必然差异」，不归一化就会
        每次同步都判定有改动、反复重写：

        1. **按 ts 排序**：WorkBuddy 的 jsonl 行序不等于时间序（会话压缩时插入的
           摘要消息尤其如此），Zcode 的 part 序则基本等于时间序。
        2. **工具事件按 call_id 聚合**：WorkBuddy 的并行调用是
           `callA callB resultA resultB`，Zcode 的 part 结构天生是
           `callA resultA callB resultB`（一个 part 同时装输入与输出）。
        语义上两者等价，所以按集合比较。
        """
        h = hashlib.sha1()
        run = []

        def flush():
            if not run:
                return
            agg = {}
            for e in run:
                k = e.call_id or (e.kind + "|" + e.key())
                if k not in agg:
                    agg[k] = {"names": set(), "args": "", "output": "", "status": ""}
                v = agg[k]
                if e.name:
                    v["names"].add(e.name)
                if e.kind == KIND_TOOL_CALL:
                    v["args"] = e.args or ""
                else:
                    v["output"] = e.output or ""
                    v["status"] = _norm_status(e.status)
            for k in sorted(agg):
                v = agg[k]
                blob = "\x1f".join([k, ",".join(sorted(v["names"])), _norm_blob(v["args"]),
                                    _norm_blob(v["output"]), v["status"]])
                h.update(("T\x00" + blob + "\x00").encode("utf-8", "ignore"))
            run.clear()

        for e in sorted(self.events, key=lambda x: x.ts):
            if e.kind in (KIND_TOOL_CALL, KIND_TOOL_RESULT):
                run.append(e)
                continue
            flush()
            h.update((e.key() + "\x00").encode())
        flush()
        return h.hexdigest()[:16]



    def last_ts(self):
        ts = [e.ts for e in self.events if e.ts]
        return max(ts + [self.updated or 0, self.created or 0])

    def counts(self):
        c = {}
        for e in self.events:
            c[e.kind] = c.get(e.kind, 0) + 1
        return c

    def __repr__(self):
        return f"<{self.side}:{self.native_id[:12]} {self.workspace} {self.title[:16]!r} {len(self.events)}事件>"


# ---------------------------------------------------------------- 工具函数
_WS_STRIP = re.compile(r"[\s/\\]+$")


def workspace_name(path):
    """规则 2：文件夹名相同 = 同一工作空间。这里统一成可比较的形式。"""
    if not path:
        return ""
    p = _WS_STRIP.sub("", str(path).replace("\\", "/"))
    name = p.rsplit("/", 1)[-1]
    return name.strip().lower()


# system-reminder 必须有属性才删：正文里讨论「<system-reminder> 这个标签」时
# 是不带属性的，误删会把用户真正写的内容吃掉
_REMINDER = re.compile(r"<system-reminder\b[^>]+>.*?</system-reminder>", re.S | re.I)

# 整段被包裹时剥壳：Zcode 与 WorkBuddy 都把用户输入包成 <user_query>…</user_query>
_WRAP_WHOLE = re.compile(r"^\s*<(user_query|cb_summary)>\s*(.*?)\s*</\1>\s*$", re.S | re.I)

# 会话压缩摘要内部的结构标签（只在摘要里出现，标签本身无信息量）
_SUMMARY_TAG = re.compile(
    r"</?(conversation_history_summary|analysis|summary|previous_user_message|"
    r"previous_assistant_message|previous_tool_call|tool_call|result|"
    r"user_query|cb_summary|omitted)\s*/?>", re.I)

# 自闭合残留（如 <omitted />）
_SELFCLOSE = re.compile(r"<[a-z_][a-z0-9_-]*\s*/>", re.I)
_MANY_NL = re.compile(r"\n{3,}")


def strip_harness_noise(text):
    """去掉两端注入的系统噪音与包裹标签，只留人真正说过/写下的内容。

    **只处理确定由 harness 生成的标记**，不做宽泛的"删掉所有尖括号块"——
    用户会在正文里写 HTML（`<td>`/`<div>`）或讨论这些标签本身
    （`<system-reminder>`、`<user_memory>`），宽泛匹配会把真实内容吃掉。
    """
    if not text:
        return ""
    t = _REMINDER.sub("", text)
    prev = None
    while prev != t:                       # 嵌套的包裹壳要剥到不再变化
        prev = t
        t = _WRAP_WHOLE.sub(lambda m: m.group(2), t)
    t = _SUMMARY_TAG.sub("", t)
    t = _SELFCLOSE.sub("", t)
    t = _MANY_NL.sub("\n\n", t)
    return t.strip()


# C0 控制字符（保留 \t \n \r）。NUL 是主要目标。
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: 工具输出里这些「字符串字面量」一律视作空（见 norm_output）
OUTPUT_NULL_LITERALS = ("null", "undefined", "None")


def scrub_controls(text):
    """摘掉字符串里的 C0 控制字符。

    工具输出经常混进二进制或 UTF-16LE 文本（`wsl -l -v`、直接读一个 .sqlite
    文件），这些内容被当字符串存进 Zcode 后留下大量 `\\u0000`。实测：
    全库 20 个会话里，只有含 NUL 的那两个打不开——Zcode 渲染层遇到含 NUL
    的字符串会抛 `The "string" argument must be of type string ... Received
    undefined`，或者整页空白。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    return _CTRL.sub("", text)


def norm_output(v):
    """工具输出的统一归一化（所有事件都从这里过一遍）。

    ``"null"`` / ``"undefined"`` / ``"None"`` 这些**字符串字面量**不是合法工具输出，
    是历史上把 js 的 null 当字符串搬来搬去造成的（2026-10-27 实测：WorkBuddy 侧
    jsonl 里就有 35 条 `output.text == "null"`）。它们的危害不止是显示难看：

    Zcode 打开会话时要重放历史工具结果，`buildToolOutput` 第一行是
    `Buffer.byteLength(content, "utf8")`，content 取自 `state.error`（error 分支）
    或 `state.output`（completed 分支）。任一侧缺失/非字符串都会抛
    `ERR_INVALID_ARG_TYPE`，使整条 `v4/conversation/subscribe` 失败 ——
    表现就是会话点开报错或整页空白。

    归一化必须放在事件入口（和 scrub_controls 同理）：如果只在写入时清洗，
    「源算哈希 -> 写入时清洗 -> 读回哈希不等」会破坏幂等，selftest S9 会直接抓到。
    """
    s = scrub_controls(v)
    return "" if s in OUTPUT_NULL_LITERALS else s


def scrub_controls_deep(o):
    """对 dict / list 里的所有字符串递归清洗（用于工具入参）。"""
    if isinstance(o, str):
        return scrub_controls(o)
    if isinstance(o, dict):
        return {k: scrub_controls_deep(v) for k, v in o.items()}
    if isinstance(o, list):
        return [scrub_controls_deep(v) for v in o]
    return o


def norm_text(text):
    return re.sub(r"\s+", " ", strip_harness_noise(text)).strip()


def norm_usage(u):
    """把各家不同的用量字段归一成一套。

    WorkBuddy 的 jsonl 里有两处：`message.usage`（input_tokens/output_tokens/
    cache_read_input_tokens）和 `providerData.rawUsage`（prompt_tokens/
    completion_tokens/prompt_cache_hit_tokens + completion_tokens_details.reasoning_tokens）。
    有这个就不必估算，直接写真实值进 Zcode 的 message 信封。
    """
    if not isinstance(u, dict):
        return None

    def g(*keys):
        for k in keys:
            v = u.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    inp = g("input_tokens", "prompt_tokens", "inputTokens")
    out = g("output_tokens", "completion_tokens", "outputTokens")
    total = g("total_tokens", "totalTokens") or (inp + out)
    cache = g("cache_read_input_tokens", "prompt_cache_hit_tokens", "cached_tokens")
    rea = 0
    det = u.get("completion_tokens_details")
    if isinstance(det, dict) and isinstance(det.get("reasoning_tokens"), (int, float)):
        rea = int(det["reasoning_tokens"])
    if not rea:
        rea = g("completion_thinking_tokens")
    if not (inp or out or total):
        return None
    return {"input": inp, "output": out, "reasoning": rea,
            "cache_read": cache, "total": total}



# ---------------------------------------------------------------- UUID
_seq = {"ms": 0, "n": 0}


def reset_seq(floor_ms=0):
    _seq["ms"] = int(floor_ms)
    _seq["n"] = 0


def uuid7(ts_ms):
    """单调递增 UUIDv7：WorkBuddy 靠 id 顺序还原时间线。"""
    ts_ms = int(ts_ms)
    if ts_ms <= _seq["ms"]:
        _seq["n"] += 1
    else:
        _seq["ms"], _seq["n"] = ts_ms, 0
    low = _seq["n"] & ((1 << 74) - 1)
    b = bytearray(16)
    b[0:6] = _seq["ms"].to_bytes(6, "big")
    b[6] = ((low >> 68) & 0x0F) | 0x70
    b[7] = (low >> 60) & 0xFF
    b[8:16] = (low & ((1 << 60) - 1)).to_bytes(8, "big")
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def new_uuid():
    return str(uuid.uuid4())


def zcode_msg_id():
    return "msg_" + uuid.uuid4().hex


def zcode_part_id():
    return "prt_" + uuid.uuid4().hex


def zcode_session_id():
    return "sess_" + str(uuid.uuid4())


def slugify_path(path):
    """WorkBuddy 的 projects 子目录名规则（已用真实目录反推验证）。"""
    p = str(path).replace("\\", "/")
    p = re.sub(r"^([A-Za-z]):", lambda m: m.group(1).lower() + ":", p)
    p = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", p)
    return p.strip("-")


def project_id_for(directory):
    """Zcode 的 project_id 约定：proj_ + 路径里的 ASCII 片段。"""
    segs = [s for s in re.split(r"[\\/]+", str(directory)) if s]
    if segs and re.match(r"^[A-Za-z]:$", segs[0]):
        segs[0] = segs[0][0].lower()
    keep = [re.sub(r"[^0-9A-Za-z]+", "-", s).strip("-").lower() for s in segs]
    keep = [k for k in keep if k]
    return "proj_" + "-".join(keep) if keep else "proj_default"
