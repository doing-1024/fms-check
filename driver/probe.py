#!/usr/bin/env python3
"""网络诊断模式: 装包→启动 app→等就绪→guest 内 curl GET/POST 探针→socket 取证→退出
不做任何登录 UI 操作, 快速回答: guest 网络层 POST 是否挂起"""
import os, signal, subprocess, sys, time

OUT = "ui"
os.makedirs(OUT, exist_ok=True)
WATCHDOG = int(os.environ.get("PROBE_WATCHDOG", "900"))


class Watchdog(Exception):
    pass


def _alarm(sig, frm):
    raise Watchdog("watchdog timeout")


signal.signal(signal.SIGALRM, _alarm)
signal.alarm(WATCHDOG)


def adb(*args, timeout=90):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except Exception as e:
        return f"[ERR] {e}"


def wait_app(t=30):
    """等模拟器 ready + 进程存活"""
    for i in range(t):
        r = adb("shell", "getprop sys.boot_completed")
        if "1" in r:
            print(f"[probe] boot done (round {i})", flush=True)
            break
        time.sleep(10)
    time.sleep(5)
    # 启动 app
    adb("shell", "monkey -p com.nunofacha.fmcs -c android.intent.category.LAUNCHER 1")
    time.sleep(15)
    pid = adb("shell", "pidof com.nunofacha.fmcs").strip()
    print("[probe] app pid:", pid, flush=True)
    adb("shell", "logcat -d > /sdcard/lc.txt")
    subprocess.run(["adb", "pull", "/sdcard/lc.txt", "logcat.txt"], capture_output=True, timeout=60)
    return pid


def main():
    # 装 4 个 split APK
    print("[install]", adb("install-multiple", "-r", "-g", "fms_patched-aligned-debugSigned.apk",
                           "cfg_arm64-aligned-debugSigned.apk", "config.en-aligned-debugSigned.apk",
                           "config.mdpi-aligned-debugSigned.apk", timeout=600)[:200], flush=True)
    pid = wait_app()
    if not pid or not pid.isdigit():
        print("[probe] App 进程不存在, 放弃", flush=True)
        return
    time.sleep(15)  # 等 RN 渲染
    # 复用 fms_ui 的探针 + 取证 (同目录)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fms_ui
    fms_ui.adb = adb
    fms_ui.OUT = OUT
    fms_ui.guest_api_probe()
    fms_ui.net_forensics("post_probe")
    print("[probe] 完成", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Watchdog as e:
        print(f"[probe] {e} - 保存现场退出", flush=True)
        adb("shell", "logcat -d > /sdcard/lcw.txt")
        subprocess.run(["adb", "pull", "/sdcard/lcw.txt", "logcat.txt"], capture_output=True, timeout=60)
        sys.exit(1)
