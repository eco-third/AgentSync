#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentSync.exe 的入口：中文菜单，双击就能用，不需要记任何命令。

设计原则（按用户的实际使用习惯）：
  · 双击 = 看到菜单，按数字选，回车执行，回车返回
  · 只有一个动作是高频的（一键同步），所以它是 1，且直接回车默认选它
  · 出问题时只有一个入口（修复），不再让人分辨该跑哪个脚本
  · 任何异常都打印出来并停下，绝不让窗口一闪而过
"""
import os
import shutil
import sys
import time
import traceback
from types import SimpleNamespace

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from asynclib import console as _console

    _console.setup()
except Exception:
    pass

from asynclib import memory, paths, sync, workbuddy, zcode  # noqa: E402

try:
    import run as runner          # 复用已验证的 CLI 实现，避免维护两套逻辑
except Exception:
    runner = None


TITLE = "AgentSync  ·  Zcode ⇄ WorkBuddy 同步工具"


def line(t=""):
    print(("─" * 4 + " " + t) if t else "─" * 66)


def ask(prompt, default_yes=False):
    """统一的 y/N 询问。读不到输入（非交互）时返回默认值。"""
    tail = "[Y/n]" if default_yes else "[y/N]"
    try:
        a = input("%s %s " % (prompt, tail)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default_yes
    if not a:
        return default_yes
    return a in ("y", "yes", "是", "1")


def pause():
    try:
        input("\n按回车返回菜单…")
    except (EOFError, KeyboardInterrupt):
        print()


# ---------------------------------------------------------------- 菜单动作
def do_sync():
    """一键同步：两侧对话 + 全局规则。"""
    if zcode.is_running():
        print("提示：Zcode 正在运行。")
        print("      它自己那份数据库这次不会写入（避免退出时把改动覆盖回去）。")
        print("      想让两侧完全一致，请先完全退出 Zcode（含托盘图标）再执行。")
        print()
    args = SimpleNamespace(dry_run=False, workspace=None, yes=False, all=False,
                           verbose=True, no_memory=False, revive=False, delete=None,
                           project_rules=False, keep_paths=False, force_open=False)
    return runner.cmd_sync(args)


def do_status():
    return runner.cmd_status(SimpleNamespace(verbose=False))


def do_doctor():
    return runner.cmd_doctor(None)


def do_backup():
    return runner.cmd_backup(None)


def do_backups():
    return runner.cmd_backups(None)


def do_repair():
    """修复「点开会话是空白 / 弹 Received undefined」的两类成因。

    两类问题共用同一个入口，是因为用户看到的症状一样，分不清也不需要分清：
      1. 工具结果字段损坏 —— Zcode 重放历史工具调用时算字节数拿到 undefined
      2. 侧边栏孤儿条目   —— 列表里有这条、session 表里没有
    """
    cfg = paths.load_config()
    cfgp = paths.cfg_paths(cfg)
    zdb = cfgp["zcode_db"]
    idx = cfgp.get("zcode_tasks_index") or ""

    if not os.path.exists(zdb):
        print("找不到 Zcode 数据库：%s" % zdb)
        return 1

    print("这个功能会检查并修复两类问题：")
    print("  ① 工具结果字段损坏 —— 点开会话报错或空白")
    print("  ② 侧边栏孤儿条目   —— 列表里有、点开是空的")
    print()

    if zcode.is_running():
        print("⚠ 检测到 Zcode 正在运行。")
        print("  写入它的数据库可能在 Zcode 退出时被覆盖回去，建议先完全退出再修。")
        if not ask("仍然继续吗？"):
            print("已取消。")
            return 0

    print("\n正在备份…")
    stamp = "repair-" + time.strftime("%Y%m%d-%H%M%S")
    d, made = sync.snapshot_dbs(cfgp, tag=stamp)
    if idx and os.path.exists(idx):
        try:
            shutil.copy2(idx, os.path.join(d, "tasks-index.sqlite"))
        except Exception:
            pass
    print("  已备份 → %s" % d)

    ok = True
    line("① 工具结果字段")
    try:
        audit = zcode.audit_tool_state(zdb)
        fatal, warn = audit["fatal"], audit["warn"]
        if not fatal and not warn:
            print("  ✓ 正常（扫描 %d 条）" % audit["scanned"])
        else:
            print("  发现 %d 个可疑项（致命 %d），正在修复…" % (len(fatal) + len(warn), len(fatal)))
            n = zcode.fix_tool_state(zdb, audit)
            after = zcode.audit_tool_state(zdb)
            print("  已修 %d 条；复检剩余 %d 个问题"
                  % (n, len(after["fatal"]) + len(after["warn"])))
            if after["fatal"]:
                ok = False
    except Exception as e:
        print("  ✗ 检查失败：%s" % e)
        ok = False

    line("② 侧边栏索引")
    try:
        o = zcode.audit_orphan_tasks(zdb, idx)
        if not o["orphans"]:
            print("  ✓ 正常（索引 %d 条，全部有效）" % o["total"])
        else:
            print("  发现 %d 个孤儿条目（列表里有、点开是空的）：" % len(o["orphans"]))
            for x in o["orphans"][:6]:
                print("    · %s" % (x["title"][:44] or "(无标题)"))
            if len(o["orphans"]) > 6:
                print("    … 其余 %d 个" % (len(o["orphans"]) - 6))
            if ask("\n删除这些孤儿条目吗？（只删索引，不删对话内容）"):
                n = zcode.fix_orphan_tasks(idx, o["orphans"])
                print("  已删除 %d 条。重启 Zcode 后列表才会更新。" % n)
            else:
                print("  已跳过。")
    except Exception as e:
        print("  ✗ 检查失败：%s" % e)
        ok = False

    print()
    if ok:
        print("完成。现在重启 Zcode，再点开之前出问题的会话看看。")
    else:
        print("有项目没能修好，备份在：%s" % d)
    return 0 if ok else 1


MENU = [
    ("1", "一键同步", "两侧对话 + 全局规则（最常用）", do_sync),
    ("2", "查看状态", "两边各有多少对话、是否配对", do_status),
    ("3", "体检", "检查数据是否正常（不改任何东西）", do_doctor),
    ("4", "修复打不开的会话", "点开空白 / 弹 Received undefined 时用", do_repair),
    ("5", "立即备份", "数据库 + 全局规则", do_backup),
    ("6", "历史备份", "看看有哪些备份、什么时候做的", do_backups),
]


def _width(s):
    """中文/全角字符在控制台占两列，直接 %-14s 会错位。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


def pad(s, width):
    return s + " " * max(0, width - _width(s))


def show_menu():
    print()
    print("=" * 66)
    print("  " + TITLE)
    print("=" * 66)
    print()
    for key, name, desc, _ in MENU:
        print("  [%s] %s%s" % (key, pad(name, 18), desc))
    print("  [0] %s%s" % (pad("退出", 18), ""))
    print()


def main():
    if runner is None:
        print("程序文件不完整：找不到核心模块 run.py。")
        print("请重新解压完整的压缩包。")
        return 2

    paths.ensure_dirs()

    while True:
        show_menu()
        try:
            choice = input("请选择 [1]：").strip() or "1"
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice in ("0", "q", "Q", "exit"):
            return 0

        hit = next((m for m in MENU if m[0] == choice), None)
        if hit is None:
            print("\n没有这个选项，请输入 0-6。")
            continue

        _, name, _, func = hit
        print()
        print("=" * 66)
        print("  " + name)
        print("=" * 66)
        print()
        try:
            func()
        except KeyboardInterrupt:
            print("\n已中断。")
        except Exception:
            print("\n执行时出错，下面是详细信息：\n")
            traceback.print_exc()
        pause()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        print("\n程序异常退出：")
        traceback.print_exc()
        try:
            input("\n按回车关闭窗口…")
        except Exception:
            pass
        sys.exit(1)
