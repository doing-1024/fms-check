"""JDWP 探针: 通过 jdb 调用 App 自己的 MMKV 写入 deviceId"""
import glob
import os
import subprocess
import time

OUT = "ui"
PKG = "com.nunofacha.fmcs"

APKS = [
    "fms_patched-aligned-debugSigned.apk",
    "cfg_arm64-aligned-debugSigned.apk",
    "config.en-aligned-debugSigned.apk",
    "config.mdpi-aligned-debugSigned.apk",
]

DEV_ID = "DiagDevice" + "x" * 40
MMKV_ROOT = f"/data/data/{PKG}/files/mmkv"


def adb(*args, timeout=600):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except Exception as e:
        return f"[ERR] {e}"


def javabin(name):
    home = os.environ.get("JAVA_HOME", "")
    cands = [f"{home}/bin/{name}", "/usr/lib/jvm/*/bin/" + name, "/opt/java/*/bin/" + name]
    for pat in cands:
        for c in sorted(glob.glob(pat)):
            if os.path.exists(c):
                return c
    return name


STUB_SRC = """package com.ammarahmed.mmkv;
public class MMKV {
    public static MMKV mmkvWithID(String id, int mode, String root) { return null; }
    public static MMKV mmkvWithID(String id, int mode) { return null; }
    public boolean encode(String k, String v) { return false; }
}
"""


def build_stub():
    os.makedirs("/tmp/stub/com/ammarahmed/mmkv", exist_ok=True)
    src = "/tmp/stub/com/ammarahmed/mmkv/MMKV.java"
    with open(src, "w") as f:
        f.write(STUB_SRC)
    r = subprocess.run([javabin("javac"), "-d", "/tmp/stub", src], capture_output=True, text=True)
    print("[stub] javac:", (r.stdout or "") + (r.stderr or "") or "ok", flush=True)
    return "/tmp/stub"


def jdb_eval(pid, expr):
    adb("forward", "tcp:8700", f"jdwp:{pid}")
    cp = build_stub()
    script = f"suspend\nprint {expr}\nresume\nexit\n"
    print(f"[jdb] pid={pid} expr={expr[:90]}", flush=True)
    try:
        r = subprocess.run([javabin("jdb"), "-classpath", cp, "-attach", "localhost:8700"],
                           input=script, capture_output=True, text=True, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        out = f"[jdb ERR] {e}"
    for line in out.splitlines()[-18:]:
        print("[jdb]", line[:200], flush=True)
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    print("[probe] install:", adb("install-multiple", "-r", "-g", *APKS, timeout=900)[-160:], flush=True)
    adb("logcat", "-c")
    adb("shell", f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
    pid = ""
    for i in range(10):
        time.sleep(5)
        pid = adb("shell", f"pidof {PKG}").strip()
        if pid:
            pid = pid.split()[-1]
        print(f"[probe] t={i*5}s pid={pid!r}", flush=True)
        if pid:
            break
    if not pid:
        print("[probe] App 未启动，退出", flush=True)
        return

    print("\n[probe] ===== 写入前 mmkv =====", flush=True)
    print(adb("shell", f"run-as {PKG} ls -la {MMKV_ROOT} 2>&1"))
    print(adb("shell", f"run-as {PKG} cat {MMKV_ROOT}/default 2>/dev/null | strings | head -20"))

    expr = (f'com.ammarahmed.mmkv.MMKV.mmkvWithID("default", 1, "{MMKV_ROOT}")'
            f'.encode("deviceId", "{DEV_ID}")')
    jdb_eval(pid, expr)

    print("\n[probe] ===== 写入后 mmkv =====", flush=True)
    print(adb("shell", f"run-as {PKG} cat {MMKV_ROOT}/default 2>/dev/null | strings | head -20"))

    print("\n[probe] ===== 重启 App 后日志 =====", flush=True)
    adb("shell", f"am force-stop {PKG}")
    adb("logcat", "-c")
    adb("shell", f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
    time.sleep(14)
    out = adb("shell", "logcat -d -v time | grep -F ReactNativeJS", timeout=120)
    for l in out.splitlines():
        if "device ID" in l or "Device ID" in l or "Generated" in l:
            print("[rn]", l[:200], flush=True)

    adb("shell", f"run-as {PKG} cat {MMKV_ROOT}/default > /sdcard/mmkv_default.bin 2>/dev/null")
    adb("pull", "/sdcard/mmkv_default.bin", "mmkv_default.bin")
    adb("shell", "logcat -d > /sdcard/lc.txt")
    adb("pull", "/sdcard/lc.txt", "logcat.txt")
    print("[probe] 完成", flush=True)


if __name__ == "__main__":
    main()
