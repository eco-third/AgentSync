"""控制台输出：Windows 中文环境下不乱码的关键。

实测结论（这是「一键同步」窗口里出现一串乱码的真正原因）：
  · Python 3.6+ 在**真控制台**里，stdout 底下是 _WindowsConsoleIO：
    它只接受 UTF-8 字节，再自己解码成 UTF-16 交给 WriteConsoleW。
  · 所以一旦把 encoding 改成 cp936（跟随代码页），写出去的 GBK 字节会被
    当成 UTF-8 解 → 直接乱码。改为 utf-8 反而是唯一正确的选择，
    因为 _WindowsConsoleIO 会把 UTF-8 还原成宽字符，跟代码页无关。

因此：
  · 真控制台（isatty）      -> encoding 必须是 utf-8
  · 重定向到文件/管道        -> 跟随系统本地编码（cmd 的 > 才是对的），
                              设 AGENTSYNC_UTF8=1 可强制 UTF-8
"""
import locale
import os
import sys

_FORCE_UTF8 = os.environ.get("AGENTSYNC_UTF8") == "1"


def _reconfigure(enc):
    for s in (sys.stdout, sys.stderr):
        try:
            if enc:
                s.reconfigure(encoding=enc, errors="replace")
            else:
                s.reconfigure(errors="replace")
        except Exception:
            pass


def setup():
    try:
        is_console = sys.stdout.isatty()
    except Exception:
        is_console = False

    if is_console:
        # 控制台：必须 utf-8（_WindowsConsoleIO 的要求），中文由系统自己渲染
        _reconfigure("utf-8")
        return

    if _FORCE_UTF8:
        _reconfigure("utf-8")
        return

    enc = "utf-8"
    try:
        if os.name == "nt":
            enc = locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        pass
    _reconfigure(enc)
