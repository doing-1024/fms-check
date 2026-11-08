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
    pid = ""
    # 装 4 个 split APK
    print("[install]", adb("install-multiple", "-r", "-g", "fms_patched-aligned-debugSigned.apk",
                           "cfg_arm64-aligned-debugSigned.apk", "config.en-aligned-debugSigned.apk",
                           "config.mdpi-aligned-debugSigned.apk", timeout=600)[:200], flush=True)
    pid = wait_app()
    if not pid or not pid.isdigit():
        print("[probe] App 进程不存在, 放弃", flush=True)
        return
    # 等 RN 渲染
    time.sleep(15)
    print("\n[probe] ===== guest HTTP 探针 =====", flush=True)
    print("[probe] tools:", adb("shell", "which curl wget", timeout=20).strip(), flush=True)
    # GET 探针 (宿主同请求 <1s 返回 scope JSON)
    r = adb("shell", "curl -s -m 12 -A FMCS-App https://api.freemcserver.net/v4/app-settings/credits", timeout=40)
    print("[probe] GET  ->", r.strip()[:150], flush=True)
    # POST 探针: 假凭据, 不碰真实账号; 网络正常时服务端会快速回 4xx JSON
    r = adb("shell",
            'curl -s -m 15 -A FMCS-App -H "Content-Type: application/json" '
            '-d \'{"username":"probebot","password":"x","scope":"ANDROID_APP"}\' '
            'https://api.freemcserver.net/v4/user/login -w " HTTP=%{http_code} t=%{time_total}s"',
            timeout=60)
    print("[probe] POST ->", r.strip()[:220], flush=True)
    # 第二次 POST 验证稳定性
    r = adb("shell",
            'curl -s -m 15 -A FMCS-App -H "Content-Type: application/json" '
            '-d \'{"username":"probebot2","password":"x","scope":"ANDROID_APP"}\' '
            'https://api.freemcserver.net/v4/user/login -w " HTTP=%{http_code} t=%{time_total}s"',
            timeout=60)
    print("[probe] POST2 ->", r.strip()[:220], flush=True)
    print("\n[probe] ===== socket 取证 =====", flush=True)
    r = adb("shell", "ss -tn", timeout=30)
    for ln in r.splitlines():
        if ":443" in ln:
            print("[probe] sock:", ln.strip()[:130], flush=True)
    # logcat 再存一份 (含 app 网络日志)
    adb("shell", "logcat -d > /sdcard/lc2.txt")
    subprocess.run(["adb", "pull", "/sdcard/lc2.txt", "logcat.txt"], capture_output=True, timeout=60)
    print("[probe] 完成", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Watchdog as e:
        print(f"[probe] {e} - 保存现场退出", flush=True)
        adb("shell", "logcat -d > /sdcard/lcw.txt")
        subprocess.run(["adb", "pull", "/sdcard/lcw.txt", "logcat.txt"], capture_output=True, timeout=60)
        sys.exit(1)
