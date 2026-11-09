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


def jdi_inject(pids, key, value):
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MmkvInject.java")
    r = subprocess.run([javabin("javac"), "-d", "/tmp/jdi", src], capture_output=True, text=True)
    print("[jdi] javac:", ((r.stdout or "") + (r.stderr or "")).strip() or "ok", flush=True)
    out = ""
    for i, pid in enumerate(pids):
        port = 8700 + i
        adb("forward", "--remove-all")
        print(adb("forward", f"tcp:{port}", f"jdwp:{pid}").strip(), flush=True)
        try:
            r = subprocess.run([javabin("java"), "-classpath", "/tmp/jdi", "MmkvInject",
                                "localhost", str(port), "default", MMKV_ROOT, key, value],
                               capture_output=True, text=True, timeout=180)
            out = (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            out = f"[jdi ERR] {e}"
        for line in out.splitlines()[-25:]:
            print(f"[jdi {pid}]", line[:220], flush=True)
        if "readback" in out:
            break
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
        print(f"[probe] t={i*5}s pid={pid!r}", flush=True)
        if pid:
            break
    if not pid:
        print("[probe] App 未启动，退出", flush=True)
        return

    print("\n[probe] ===== 写入前 mmkv =====", flush=True)
    print(adb("shell", f"run-as {PKG} ls -laR /data/data/{PKG}/files 2>&1 | head -30"))
    print(adb("shell", f"run-as {PKG} cat {MMKV_ROOT}/default 2>/dev/null | strings | head -20"))

    print("[probe] 等待 MMKV 文件创建...", flush=True)
    for i in range(12):
        t = adb("shell", f"run-as {PKG} ls {MMKV_ROOT}/default 2>/dev/null")
        if "default" in t and "No such" not in t:
            print(f"[probe] mmkv 就绪 t={i*5}s", flush=True)
            break
        time.sleep(5)
    pids = pid.split()
    print("[probe] pids =", pids, flush=True)
    jdi_inject(pids, "deviceId", DEV_ID)

    print("\n[probe] ===== 写入后 mmkv =====", flush=True)
    print(adb("shell", f"run-as {PKG} ls -la {MMKV_ROOT} 2>&1"))
    print(adb("shell", f"run-as {PKG} cat {MMKV_ROOT}/default 2>/dev/null | strings | head -20"))

    for fn in ("default", "default.crc", "mmkvIDStore", "mmkvIDStore.crc"):
        b64 = adb("shell", f"run-as {PKG} cat {MMKV_ROOT}/{fn} 2>/dev/null | base64")
        print(f"\n[mmkv-b64] {fn}", flush=True)
        print(b64.replace("\n", "").strip()[:6000], flush=True)

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
