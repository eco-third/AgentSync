#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 wb-plugin 挂载进 WorkBuddy 的插件注册表，实现「每轮注入全局记忆」。

原理（从 WorkBuddy 程序包实测得出）：
  · 对话由 `cli/bin/codebuddy`（同一份 codebuddy-lite-wb.mjs）执行，hook 引擎在那里；
  · 该引擎支持 UserPromptSubmit，但**只从插件目录**加载 hook（`<pluginRoot>/hooks/hooks.json`）；
  · `~/.workbuddy/settings.json` 里写 hooks 无效——主进程不读也不透传；
  · 插件是否加载由 registry 决定：
        known_marketplaces.json   市场来源（type=directory 指向本地目录）
        installed_plugins.json    已安装记录（installPath 指向插件根）
        settings.json -> enabledPlugins.<name>@<marketplace> = true

用法：
    python install_wb_hook.py              # 安装/更新
    python install_wb_hook.py --dry-run    # 只看会改什么
    python install_wb_hook.py --uninstall  # 移除注册（恢复原状）
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from asynclib import paths as _paths
    # 项目根（wb-plugin 与 state 都在这里）。源码搬进「开发/」后不能再用 HERE。
    ROOT = _paths.PROJECT_DIR
except Exception:
    ROOT = os.path.dirname(HERE)
PLUGIN_MARKETPLACE = os.path.join(ROOT, "wb-plugin")
PLUGIN_DIR = os.path.join(PLUGIN_MARKETPLACE, "plugins", "global-memory-inject")

MARKETPLACE_NAME = "wb-local-hooks"
PLUGIN_NAME = "global-memory-inject"
PLUGIN_VERSION = "1.0.0"
PLUGIN_ID = "%s@%s" % (PLUGIN_NAME, MARKETPLACE_NAME)

HOME = os.path.expanduser("~")
PLUGINS_DIR = os.path.join(HOME, ".workbuddy", "plugins")
F_KNOWN = os.path.join(PLUGINS_DIR, "known_marketplaces.json")
F_INSTALLED = os.path.join(PLUGINS_DIR, "installed_plugins.json")
F_CONFIGURED = os.path.join(PLUGINS_DIR, "configured-plugins.json")
F_SETTINGS = os.path.join(HOME, ".workbuddy", "settings.json")
BACKUP_ROOT = os.path.join(ROOT, "state", "backups")


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    tmp = path + ".agentsync-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def backup(paths):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(BACKUP_ROOT, "wb-plugin-" + stamp)
    os.makedirs(d, exist_ok=True)
    for p in paths:
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(d, os.path.basename(p)))
    return d


def refresh_plugin_copies(changed, dry):
    """写「AgentSync 根目录」标记，并把当前脚本热更新到 WorkBuddy 的缓存副本。

    WorkBuddy 会把 directory 型 marketplace 复制到
    `~/.workbuddy/plugins/cache/<marketplace>/<plugin>/<version>/` 再执行 hook。
    副本里的「上四级」会落到 plugins\\cache，权威副本和日志目录都会找错，
    所以这里把绝对路径写成标记文件，让脚本无论被搬到哪里都能认回家。
    hook 是每次调用才起进程读文件，所以直接覆盖缓存副本即可立即生效，不必重启。
    """
    marker_payload = ROOT + "\n"   # 插件靠这个找项目根，必须是根目录
    targets = [os.path.join(PLUGIN_DIR, "hooks")]

    cache_root = os.path.join(PLUGINS_DIR, "cache")
    if os.path.isdir(cache_root):
        for mkt in os.listdir(cache_root):
            pdir = os.path.join(cache_root, mkt, PLUGIN_NAME)
            if not os.path.isdir(pdir):
                continue
            for ver in os.listdir(pdir):
                hooks = os.path.join(pdir, ver, "hooks")
                if os.path.isdir(hooks):
                    targets.append(hooks)

    src_mjs = os.path.join(PLUGIN_DIR, "hooks", "global-memory.mjs")
    src_ok = os.path.exists(src_mjs)

    for hooks_dir in targets:
        for name in ("agentsync-root.txt",):
            p = os.path.join(hooks_dir, name)
            cur = None
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    cur = f.read()
            if cur != marker_payload:
                if not dry:
                    with open(p, "w", encoding="utf-8", newline="\n") as f:
                        f.write(marker_payload)
                changed.append("root 标记: " + p)

        dst_mjs = os.path.join(hooks_dir, "global-memory.mjs")
        if src_ok and os.path.abspath(dst_mjs) != os.path.abspath(src_mjs):
            same = False
            if os.path.exists(dst_mjs):
                with open(src_mjs, "rb") as a, open(dst_mjs, "rb") as b:
                    same = a.read() == b.read()
            if not same:
                if not dry:
                    shutil.copy2(src_mjs, dst_mjs)
                changed.append("同步脚本到缓存副本: " + dst_mjs)

    # 家目录兜底标记：插件目录整体被搬走时也能认路
    home_marker = os.path.join(HOME, ".workbuddy", "agentsync-root.txt")
    cur = None
    if os.path.exists(home_marker):
        with open(home_marker, "r", encoding="utf-8") as f:
            cur = f.read()
    if cur != marker_payload:
        if not dry:
            with open(home_marker, "w", encoding="utf-8", newline="\n") as f:
                f.write(marker_payload)
        changed.append("root 标记: " + home_marker)


def install(dry):
    changed = []

    # ---- 0. 前置校验：插件文件必须在 ----
    need = [
        os.path.join(PLUGIN_MARKETPLACE, ".codebuddy-plugin", "marketplace.json"),
        os.path.join(PLUGIN_DIR, ".codebuddy-plugin", "plugin.json"),
        os.path.join(PLUGIN_DIR, "hooks", "hooks.json"),
        os.path.join(PLUGIN_DIR, "hooks", "global-memory.mjs"),
    ]
    miss = [p for p in need if not os.path.exists(p)]
    if miss:
        print("!! 插件文件缺失，无法安装：")
        for p in miss:
            print("   ", p)
        return 1

    bk = None
    if not dry:
        bk = backup([F_KNOWN, F_INSTALLED, F_CONFIGURED, F_SETTINGS])
        print("已备份注册表 ->", bk)

    # ---- 0.5 根目录标记 + 刷新缓存副本 ----
    refresh_plugin_copies(changed, dry)

    # ---- 1. known_marketplaces.json ----
    km = load_json(F_KNOWN, {}) or {}
    entry = {
        "manifestName": MARKETPLACE_NAME,
        "type": "directory",
        "source": {"source": "directory", "path": PLUGIN_MARKETPLACE},
        "installLocation": PLUGIN_MARKETPLACE,
        "description": "AgentSync 本地 hook 插件市场（每轮注入全局记忆）",
        "lastUpdated": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "autoUpdate": False,
    }
    if km.get(MARKETPLACE_NAME, {}).get("installLocation") != PLUGIN_MARKETPLACE:
        km[MARKETPLACE_NAME] = entry
        changed.append("known_marketplaces.json: +" + MARKETPLACE_NAME)
        if not dry:
            save_json(F_KNOWN, km)
    else:
        print("known_marketplaces.json 已存在，跳过")

    # ---- 2. installed_plugins.json ----
    ip = load_json(F_INSTALLED, {"version": 2, "plugins": {}}) or {"version": 2, "plugins": {}}
    ip.setdefault("plugins", {})
    recs = ip["plugins"].get(PLUGIN_ID) or []
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    ok = any(
        r.get("scope") == "user" and r.get("installPath")
        for r in recs
    )
    if not ok:
        recs = [r for r in recs if r.get("scope") != "user"]
        recs.append({
            "scope": "user",
            "installPath": PLUGIN_DIR,
            "version": PLUGIN_VERSION,
            "installedAt": now,
            "lastUpdated": now,
        })
        ip["plugins"][PLUGIN_ID] = recs
        changed.append("installed_plugins.json: " + PLUGIN_ID)
        if not dry:
            save_json(F_INSTALLED, ip)
    else:
        print("installed_plugins.json 已存在，跳过")

    # ---- 3. configured-plugins.json（与内置插件写法保持一致）----
    cp = load_json(F_CONFIGURED, None)
    if isinstance(cp, dict):
        lst = cp.setdefault("builtInMarketPlugins", [])
        hit = [x for x in lst if x.get("name") == PLUGIN_NAME and x.get("marketplaceName") == MARKETPLACE_NAME]
        if not hit:
            lst.append({"name": PLUGIN_NAME, "marketplaceName": MARKETPLACE_NAME, "scope": "user"})
            changed.append("configured-plugins.json: " + PLUGIN_NAME)
            if not dry:
                save_json(F_CONFIGURED, cp)
        else:
            print("configured-plugins.json 已存在，跳过")

    # ---- 4. settings.json -> enabledPlugins ----
    st = load_json(F_SETTINGS, None)
    if st is None:
        print("!! 找不到", F_SETTINGS)
        return 1
    ep = st.setdefault("enabledPlugins", {})
    if ep.get(PLUGIN_ID) is not True:
        ep[PLUGIN_ID] = True
        changed.append("settings.json: enabledPlugins." + PLUGIN_ID + " = true")
        if not dry:
            save_json(F_SETTINGS, st)
    else:
        print("settings.json enabledPlugins 已开启，跳过")

    if not changed:
        print("\n已是安装状态，无需改动。")
    else:
        print("\n%s：" % ("将执行（dry-run）" if dry else "已执行"))
        for c in changed:
            print("  ·", c)
    return 0


def uninstall(dry):
    bk = None
    if not dry:
        bk = backup([F_KNOWN, F_INSTALLED, F_CONFIGURED, F_SETTINGS])
        print("已备份注册表 ->", bk)

    km = load_json(F_KNOWN, {}) or {}
    if MARKETPLACE_NAME in km:
        km.pop(MARKETPLACE_NAME)
        if not dry:
            save_json(F_KNOWN, km)
        print("removed known_marketplaces:", MARKETPLACE_NAME)

    ip = load_json(F_INSTALLED, {"version": 2, "plugins": {}}) or {"version": 2, "plugins": {}}
    if PLUGIN_ID in (ip.get("plugins") or {}):
        ip["plugins"].pop(PLUGIN_ID)
        if not dry:
            save_json(F_INSTALLED, ip)
        print("removed installed_plugins:", PLUGIN_ID)

    cp = load_json(F_CONFIGURED, None)
    if isinstance(cp, dict) and isinstance(cp.get("builtInMarketPlugins"), list):
        before = len(cp["builtInMarketPlugins"])
        cp["builtInMarketPlugins"] = [
            x for x in cp["builtInMarketPlugins"]
            if not (x.get("name") == PLUGIN_NAME and x.get("marketplaceName") == MARKETPLACE_NAME)
        ]
        if len(cp["builtInMarketPlugins"]) != before:
            if not dry:
                save_json(F_CONFIGURED, cp)
            print("removed configured-plugins:", PLUGIN_NAME)

    st = load_json(F_SETTINGS, None)
    if isinstance(st, dict) and PLUGIN_ID in (st.get("enabledPlugins") or {}):
        st["enabledPlugins"].pop(PLUGIN_ID)
        if not dry:
            save_json(F_SETTINGS, st)
        print("disabled plugin in settings.json")

    print("\n卸载完成。重启 WorkBuddy 后生效。")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    return uninstall(a.dry_run) if a.uninstall else install(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
