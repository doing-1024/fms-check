#!/usr/bin/env python3
"""FMCS App UI 驱动（跑在 GitHub Actions 的模拟器里）。

用法: fms_ui.py [recon|full]
  recon - 装包、启动、周期性 dump UI + logcat，供离线分析
  full  - 完整流程: 登录 -> Free Credits -> claim -> 看广告 -> 领取
"""
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

PKG = "com.nunofacha.fmcs"
OUT = "ui"
USER = os.environ.get("FMCS_USER", "doing1024")
PASS = os.environ.get("FMCS_PASS", "")


def adb(*args, timeout=90):
    r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def dump_ui(tag):
    try:
        adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
        adb("pull", "/sdcard/ui.xml", f"{OUT}/{tag}.xml")
        return True
    except Exception as e:
        print(f"[dump] {tag} failed: {e}")
        return False


def nodes(tag):
    """解析 dump 出的 xml -> [(text, desc, cls, (x1,y1,x2,y2))]"""
    path = f"{OUT}/{tag}.xml"
    if not os.path.exists(path):
        return []
    out = []
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return []
    for n in root.iter("node"):
        b = n.get("bounds", "")
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
        if not m:
            continue
        box = tuple(map(int, m.groups()))
        out.append({
            "text": n.get("text", ""), "desc": n.get("content-desc", ""),
            "cls": n.get("class", ""), "rid": n.get("resource-id", ""), "box": box,
        })
    return out


def find(ns, *words):
    """按 text/desc 包含任一关键词找节点"""
    for n in ns:
        hay = (n["text"] + " " + n["desc"]).lower()
        if any(w.lower() in hay for w in words):
            return n
    return None


def tap(n):
    x = (n["box"][0] + n["box"][2]) // 2
    y = (n["box"][1] + n["box"][3]) // 2
    print(f"[tap] {n['text']!r}/{n['desc']!r} @ {x},{y}")
    adb("shell", "input", "tap", str(x), str(y))


def input_text(n, text):
    tap(n)
    time.sleep(0.8)
    adb("shell", "input", "text", text.replace(" ", "%s"))
    adb("shell", "input", "keyevent", "111")  # ESC 收起键盘


def save_logcat(tag=""):
    r = adb("logcat", "-d")
    with open("logcat.txt", "w") as f:
        f.write(r)
    hits = [ln for ln in r.splitlines()
            if "ReactNativeJS" in ln and re.search(r"Integrity|reward|claim|credit|token", ln, re.I)]
    for h in hits[-60:]:
        print("[js]", h[:240])


def summarize(ns, tag):
    print(f"--- UI {tag}: {len(ns)} nodes ---")
    for n in ns:
        if n["text"] or n["desc"]:
            print(f"  {n['cls']:<28} t={n['text'][:40]!r} d={n['desc'][:40]!r}")


def wait_app(t=25):
    time.sleep(t)
    dump_ui("boot")
    ns = nodes("boot")
    summarize(ns, "boot")
    return ns


def do_login(ns):
    ets = [n for n in ns if "EditText" in n["cls"]]
    print(f"[login] EditText 数: {len(ets)}")
    if len(ets) >= 2:
        input_text(ets[0], USER)
        input_text(ets[1], PASS)
        time.sleep(0.5)
        dump_ui("login_filled")
        ns2 = nodes("login_filled")
        btn = find(ns2, "login", "sign in", "log in")
        if btn:
            tap(btn)
        else:
            # 兜底: 回车提交
            adb("shell", "input", "keyevent", "66")
        return True
    return False


def flow_full():
    ns = wait_app()
    # 若已在主页(有 BottomNav)则跳过登录
    if not find(ns, "login", "username"):
        print("[flow] 无登录界面, 可能已登录")
    else:
        do_login(ns)
        time.sleep(15)
        dump_ui("after_login")
        summarize(nodes("after_login"), "after_login")

    # 找 Credits / Free Credits 入口
    for i in range(12):
        ns = nodes("after_login") if i == 0 else None
        if ns is None:
            dump_ui(f"nav_{i:02d}")
            ns = nodes(f"nav_{i:02d}")
        tab = find(ns, "credit", "reward", "earn")
        if tab:
            print("[flow] 找到 credits 入口")
            tap(tab)
            break
        time.sleep(4)
    time.sleep(10)
    dump_ui("credits_tab")
    summarize(nodes("credits_tab"), "credits_tab")
    save_logcat()
    print("[flow] recon 到此为止, 后续步骤由下一版驱动接管")


def flow_recon(iters=14, gap=6):
    ns = wait_app()
    if find(ns, "login", "username"):
        print("[recon] 出现登录页, 填一次试试")
        do_login(ns)
    for i in range(iters):
        time.sleep(gap)
        tag = f"r{i:02d}"
        dump_ui(tag)
        ns = nodes(tag)
        summarize(ns, tag)
    save_logcat()
    print("[recon] 完成")


def main():
    os.makedirs(OUT, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else "recon"
    print(f"[main] mode={mode} user={USER} pass={'set' if PASS else 'UNSET'}")
    adb("shell", "settings", "put", "global", "window_animation_scale", "0")
    adb("shell", "settings", "put", "global", "transition_animation_scale", "0")
    adb("shell", "settings", "put", "global", "animator_duration_scale", "0")
    print("[install]", adb("install", "-r", "-g", "fms.apk")[:200])
    adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    if mode == "full":
        flow_full()
    else:
        flow_recon()


if __name__ == "__main__":
    main()
