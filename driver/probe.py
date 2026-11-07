#!/usr/bin/env python3
"""密码框输入诊断: 逐个策略穷举, 每步 dump + 记录 IME/焦点状态"""
import os, subprocess, sys, time
import xml.etree.ElementTree as ET

OUT = "ui"
os.makedirs(OUT, exist_ok=True)
USER = os.environ.get("FMCS_USER", "")
PASS = os.environ.get("FMCS_PASS", "")
tag_n = 0


def adb(*args, timeout=90):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except Exception as e:
        return f"[ERR] {e}"


def dump(tag):
    global tag_n
    tag_n += 1
    name = f"p{tag_n:02d}_{tag}"
    adb("shell", "uiautomator", "dump", f"/sdcard/{name}.xml")
    x = adb("shell", "cat", f"/sdcard/{name}.xml")
    open(f"{OUT}/{name}.xml", "w").write(x)
    adb("shell", "screencap", "-p", f"/sdcard/{name}.png")
    subprocess.run(["adb", "pull", f"/sdcard/{name}.png", f"{OUT}/{name}.png"],
                   capture_output=True, timeout=60)
    return name


def nodes(name):
    try:
        root = ET.parse(f"{OUT}/{name}.xml").getroot()
    except Exception as e:
        return []
    out = []
    for n in root.iter("node"):
        b = n.get("bounds", "")
        try:
            nums = [int(v) for v in b.replace("][", ",").strip("[]").split(",")]
            cx, cy = (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2
        except Exception:
            cx = cy = 0
        out.append({"cls": n.get("class", ""), "text": n.get("text", ""),
                    "desc": n.get("content-desc", ""), "box": b,
                    "focused": n.get("focused") == "true", "cx": cx, "cy": cy,
                    "pkg": n.get("package", "")})
    return out


def show(name, title):
    ns = nodes(name)
    ets = [n for n in ns if "EditText" in n["cls"]]
    kb = [n for n in ns if "nunofacha" not in n["pkg"] and ("键盘" in n["cls"] or "Keyboard" in n["cls"] or "keyboard" in n["pkg"].lower())]
    print(f"--- {title} [{name}] 节点={len(ns)} EditText={len(ets)} 键盘节点={len(kb)}")
    for e in ets:
        print(f"    ET text={e['text'][:30]!r} focused={e['focused']} box={e['box']}")
    ime = adb("shell", "dumpsys", "input_method")
    for key in ["mInputShown", "mServedView", "mCurFocusedWindow", "mShowRequested"]:
        for line in ime.splitlines():
            if key in line:
                print(f"    IME {line.strip()[:150]}")
                break
    return ets


def tap(n):
    adb("shell", "input", "tap", str(n["cx"]), str(n["cy"]))


def main():
    print(f"[probe] user={USER!r} passLen={len(PASS)}")
    adb("shell", "settings", "put", "secure", "show_ime_with_hard_keyboard", "1")
    print("[probe] 强制显示软键盘")

    ets = show(dump("boot"), "启动")
    if len(ets) < 2:
        print("[probe] 没有登录页, 退出")
        return
    uf, pf = ets[0], ets[1]

    # 策略1: 用户名 tap + input text
    tap(uf); time.sleep(2)
    adb("shell", "input", "text", USER)
    time.sleep(1.5)
    show(dump("s1_user"), "策略1 用户名 input text")

    # 策略2: TAB 切到密码 + keyevent 逐字
    adb("shell", "input", "keyevent", "61")
    time.sleep(1.5)
    ets2 = show(dump("s2_tab"), "策略2 TAB 后")
    adb("shell", "input", "text", "751229Jq")
    time.sleep(1.5)
    show(dump("s2_tabtext"), "策略2 TAB 后 input text (无@)")

    # 策略3: 重新 tap 密码框 + input text 纯字母数字
    ets3 = [n for n in nodes(dump("s3_pre")) if "EditText" in n["cls"]]
    tap(ets3[1]); time.sleep(2)
    show(dump("s3_tap"), "策略3 tap 密码框后(看 focused/键盘)")
    adb("shell", "input", "text", "abc123")
    time.sleep(1.5)
    show(dump("s3_text"), "策略3 input text abc123")

    # 策略4: keyevent 逐字
    for ch in ["4", "5", "6"]:
        adb("shell", "input", "keyevent", str(7 + int(ch)))
        time.sleep(0.3)
    time.sleep(1)
    show(dump("s4_key"), "策略4 keyevent 456")

    # 策略5: 全量密码 input text
    adb("shell", "input", "text", PASS.replace("@", ""))
    time.sleep(1.5)
    show(dump("s5_noshift"), "策略5 全量密码(去@)")

    adb("logcat", "-d", "-v", "time")
    r = adb("logcat", "-d", "-v", "time")
    open(f"{OUT}/probe_logcat.txt", "w").write(r)
    print("[probe] 完成")


if __name__ == "__main__":
    main()
