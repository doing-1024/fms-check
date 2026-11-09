"""设备/存储诊断: 装 App -> 启动 -> 抓 deviceId 生成与 user/device 注册日志"""
import os
import subprocess
import sys
import time

OUT = "ui"
PKG = "com.nunofacha.fmcs"

APKS = [
    "fms_patched-aligned-debugSigned.apk",
    "cfg_arm64-aligned-debugSigned.apk",
    "config.en-aligned-debugSigned.apk",
    "config.mdpi-aligned-debugSigned.apk",
]


def adb(*args, timeout=600):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except Exception as e:
        return f"[ERR] {e}"


def grep_log(pat, n=40):
    out = adb("shell", f"logcat -d -v time | grep -E '{pat}'", timeout=120)
    lines = [l for l in out.splitlines() if l.strip() and "[ERR]" not in l]
    for l in lines[-n:]:
        print("[log]", l[:230])
    return lines


def main():
    os.makedirs(OUT, exist_ok=True)
    print("[diag] install:", adb("install-multiple", "-r", "-g", *APKS, timeout=900)[-200:], flush=True)
    adb("shell", "settings", "put", "global", "window_animation_scale", "0")
    adb("logcat", "-c")
    adb("shell", f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
    for i in range(10):
        time.sleep(6)
        pid = adb("shell", f"pidof {PKG}").strip()
        print(f"[diag] t={i*6}s pid={pid!r}", flush=True)
    print("\n[diag] ===== device/registry 日志 =====", flush=True)
    grep_log("Generated new device ID|Device ID already|Device registration response|Failed to register device|Generated device ID|App Version:|Device Brand:|Device Model:|Fingerprint:")
    print("\n[diag] ===== MMKV / 存储错误 =====", flush=True)
    grep_log("MMKV|SecureStore|KeyStore|keystore|Storage"  , n=25)
    print("\n[diag] ===== 全部 RN 日志(前 60) =====", flush=True)
    out = adb("shell", "logcat -d -v time | grep -F ReactNativeJS", timeout=120)
    for l in [x for x in out.splitlines() if x.strip()][:60]:
        print("[rn]", l[:230])
    print("\n[diag] ===== android.R.attr 真实 ID =====", flush=True)
    for jar in [os.environ.get("ANDROID_HOME","") + "/platforms/android-34/android.jar",
                os.environ.get("ANDROID_HOME","") + "/platforms/android-35/android.jar"]:
        if os.path.exists(jar):
            r = subprocess.run(["javap", "-p", "-constants", "-cp", jar, "android.R$attr"],
                               capture_output=True, text=True)
            for line in (r.stdout or "").splitlines():
                if any(k in line for k in ("debuggable", "allowBackup", "extractNativeLibs")):
                    print("[attr]", line.strip())
            break
    print("\n[diag] ===== package flags =====", flush=True)
    print(adb("shell", "dumpsys package com.nunofacha.fmcs | grep -iE 'flags=|pkgFlags|DEBUGGABLE' | head -8"))
    print(adb("shell", "pm dump com.nunofacha.fmcs | grep -iE 'flags|debuggable' | head -8"))
    print("\n[diag] ===== run-as 可用性 =====", flush=True)
    print(adb("shell", "run-as com.nunofacha.fmcs id 2>&1"))
    print(adb("shell", "adb root 2>&1; id"))
    print(adb("shell", "cat /data/data/com.nunofacha.fmcs/files/mmkv/default 2>&1 | head -c 120"))
    print("\n[diag] ===== App 数据目录 =====", flush=True)
    print(adb("shell", "run-as com.nunofacha.fmcs ls -R /data/data/com.nunofacha.fmcs 2>&1 | head -40"))
    print("\n[diag] ===== MMKV 内容(strings) =====", flush=True)
    print(adb("shell", "run-as com.nunofacha.fmcs cat /data/data/com.nunofacha.fmcs/files/mmkv/default 2>/dev/null | strings | head -40"))
    print("\n[diag] ===== MMKV 十六进制头 =====", flush=True)
    print(adb("shell", "run-as com.nunofacha.fmcs dd if=/data/data/com.nunofacha.fmcs/files/mmkv/default bs=1 count=256 2>/dev/null | od -An -tx1"))
    print("\n[diag] ===== 其它存储文件 =====", flush=True)
    print(adb("shell", "run-as com.nunofacha.fmcs sh -c 'ls -la /data/data/com.nunofacha.fmcs/files /data/data/com.nunofacha.fmcs/shared_prefs 2>&1'"))
    adb("shell", "logcat -d > /sdcard/lc.txt")
    adb("pull", "/sdcard/lc.txt", "logcat.txt")
    print("[diag] 完成", flush=True)


if __name__ == "__main__":
    main()
