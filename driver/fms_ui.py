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
    """按 text/desc 找节点: 先精确, 再前缀, 最后包含"""
    wl = [w.lower() for w in words]
    for n in ns:
        hay = (n["text"] + " " + n["desc"]).strip().lower()
        if any(hay == w for w in wl):
            return n
    for n in ns:
        hay = (n["text"] + " " + n["desc"]).strip().lower()
        if any(hay.startswith(w) for w in wl) and not hay.startswith("please"):
            return n
    for n in ns:
        hay = (n["text"] + " " + n["desc"]).lower()
        if any(w in hay for w in wl) and "please" not in hay:
            return n
    return None


def tap(n):
    x = (n["box"][0] + n["box"][2]) // 2
    y = (n["box"][1] + n["box"][3]) // 2
    print(f"[tap] {n['text']!r}/{n['desc']!r} @ {x},{y}")
    adb("shell", "input", "tap", str(x), str(y))


KEYMAP = {c: 7 + int(c) for c in "0123456789"}
KEYMAP.update({chr(ord("a") + i): 29 + i for i in range(26)})
KEYMAP.update({"@": 77, ".": 56, ",": 55, "-": 69, "_": 73, "/": 76, "!": 71})


def type_text(text):
    """逐字符 keyevent，大小写用 SHIFT，最稳"""
    for ch in text:
        if ch.isupper():
            adb("shell", "input", "keyevent", "60")  # SHIFT down
            adb("shell", "input", "keyevent", str(KEYMAP[ch.lower()]))
            adb("shell", "input", "keyevent", "60")  # SHIFT off
        elif ch in KEYMAP:
            adb("shell", "input", "keyevent", str(KEYMAP[ch]))
        else:
            adb("shell", "input", "text", ch)
        time.sleep(0.15)


def input_text(n, text, tag="in", placeholder=None, secure=False):
    ph = (placeholder or "").lower()
    for attempt in range(3):
        tap(n)
        time.sleep(1.5)
        adb("shell", "input", "keyevent", "111")  # ESC: 确保无残留状态
        time.sleep(0.5)
        tap(n)
        time.sleep(1.2)
        type_text(text)
        time.sleep(1)
        adb("shell", "input", "keyevent", "4")  # BACK 收起键盘
        time.sleep(1.2)
        dump_ui(tag)
        for m in nodes(tag):
            if m["box"] == n["box"]:
                cur = (m["text"] or "").strip()
                cur_l = cur.lower()
                # 安全输入框: 有内容时系统隐藏原文, dump 里 text 为空串
                if secure:
                    if cur_l != ph:
                        print(f"[input] 已填入 (安全框, 占位符已消失, dump={cur[:12]!r})")
                        return True
                elif cur and cur_l != ph and cur_l.replace("•", "") != "":
                    print(f"[input] 已填入 (掩码或原文): {cur[:20]!r}")
                    return True
        print(f"[input] 第 {attempt+1} 次未验证到内容, 重试")
    return False


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


def shot(tag):
    try:
        r = subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, timeout=30)
        open(f"{OUT}/{tag}.png", "wb").write(r.stdout)
    except Exception as e:
        print(f"[shot] {tag} failed: {e}")


def app_pid():
    return adb("shell", "pidof", PKG).strip()


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
    if find(ns, "welcome back", "username", "sign in"):
        print("[flow] 登录页")
        # 填 username
        ets = [n for n in ns if "EditText" in n["cls"]]
        input_text(ets[0], USER, "f_user", "Username")
        # 收键盘后重新 dump, 拿密码框新坐标再 tap
        time.sleep(1)
        dump_ui("pre_pass")
        ns2 = nodes("pre_pass")
        ets2 = [n for n in ns2 if "EditText" in n["cls"]]
        ok = False
        if len(ets2) >= 2:
            ok = input_text(ets2[1], PASS, "f_pass", "Password", secure=True)
        print("[flow] 密码填入:", ok)
        time.sleep(0.5)
        dump_ui("login_ready")
        shot("login_ready")
        btn = find(nodes("login_ready"), "sign in")
        if btn:
            tap(btn)
        print("[flow] 已提交登录, 等待...")
    for i in range(10):
        time.sleep(8)
        tag = f"home{i:02d}"
        dump_ui(tag)
        shot(tag)
        ns = nodes(tag)
        if find(ns, "server", "credits", "my servers", "welcome") and not find(ns, "welcome back"):
            print(f"[flow] 主页就绪 (第{i}轮)")
            summarize(ns, tag)
            break
        summarize(ns, tag)
        if find(ns, "welcome back"):
            print("[flow] 仍在登录页, 再点一次 Sign In")
            b = find(ns, "sign in")
            if b: tap(b)
    # 找 credits 入口
    for i in range(15):
        tag = f"nav_{i:02d}"
        dump_ui(tag)
        shot(tag)
        ns = nodes(tag)
        tab = find(ns, "free credit", "credits", "earn")
        if tab and not find(ns, "welcome back"):
            print("[flow] 进入 credits tab")
            tap(tab)
            break
        time.sleep(3)
    time.sleep(10)
    dump_ui("credits_tab")
    shot("credits_tab")
    summarize(nodes("credits_tab"), "credits_tab")
    # 找 claim 按钮
    for i in range(20):
        tag = f"claim_{i:02d}"
        dump_ui(tag)
        shot(tag)
        ns = nodes(tag)
        btn = find(ns, "claim", "watch ad", "earn", "get credit", "integrity")
        if btn:
            print(f"[flow] 点 claim: {btn['text']!r}/{btn['desc']!r}")
            tap(btn)
            time.sleep(12)
        else:
            # 广告可能是全屏 activity, 找关闭按钮
            close = None
            for m in ns:
                if "close" in (m["text"] + m["desc"]).lower():
                    close = m; break
            if close:
                print("[flow] 广告关闭按钮, 点击")
                tap(close)
            time.sleep(6)
        save_logcat()
    dump_ui("final")
    shot("final")
    save_logcat()
    print("[flow] 完成")


def flow_recon(iters=14, gap=6):
    ns = wait_app()
    if find(ns, "login", "username"):
        print("[recon] 出现登录页, 填一次试试")
        do_login(ns)
    for i in range(iters):
        time.sleep(gap)
        tag = f"r{i:02d}"
        dump_ui(tag)
        shot(tag)
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
    print("[install]", adb("install-multiple", "-r", "-g", "fms_patched-aligned-debugSigned.apk",
                            "cfg_arm64-aligned-debugSigned.apk", "config.en-aligned-debugSigned.apk",
                            "config.mdpi-aligned-debugSigned.apk",
                            timeout=600)[:600])
    adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    for i in range(10):
        time.sleep(3)
        pid = app_pid()
        print(f"[pid] {i}: {pid or 'dead'}")
        if pid:
            break
    if not app_pid():
        print("[!] App 未存活, dump 崩溃日志")
        save_logcat()
        sys.exit(1)
    if mode == "full":
        flow_full()
    else:
        flow_recon()


if __name__ == "__main__":
    main()

# 结束时把 tinyproxy 日志拷到工作目录
try:
    with open("proxy.log", "w") as f:
        f.write(subprocess.run(["sudo", "cat", "/var/log/tinyproxy/tinyproxy.log"],
                               capture_output=True, text=True, timeout=30).stdout)
    print("[proxy] log 已保存")
except Exception as e:
    print("[proxy]", e)
