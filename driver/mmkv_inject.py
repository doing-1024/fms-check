"""通过 JDWP/JDI 让 App 自己的 MMKV 写入 deviceId（绕过 generateRandomString 返回空串的 bug）"""
import glob
import os
import subprocess
import time

PKG = "com.nunofacha.fmcs"
MMKV_ROOT = f"/data/data/{PKG}/files/mmkv"
HERE = os.path.dirname(os.path.abspath(__file__))

DEV_ID = "FMSDEV" + "Am9xQ2Zk4Rt7Ls0Pw3Yb6Jn1Vc8Kh5Sg2Df7Ue4Iw9Ol6Pa3Rc8Tz5Xm2Qv7Nb1Ks6Jh4Gf9Dd3Ss8Aa5Ll2Pp7Rk4Wz9Xx6Cc3Vv8Bb5Nn2Mm7"


def javabin(name):
    home = os.environ.get("JAVA_HOME", "")
    pats = [f"{home}/bin/{name}", "/usr/lib/jvm/*/bin/" + name, "/opt/java/*/bin/" + name,
            "/usr/lib/jvm/*/bin/" + name]
    for p in pats:
        for c in sorted(glob.glob(p)):
            if os.path.exists(c):
                return c
    return name


def build():
    src = os.path.join(HERE, "MmkvInject.java")
    os.makedirs("/tmp/jdi", exist_ok=True)
    r = subprocess.run([javabin("javac"), "-d", "/tmp/jdi", src], capture_output=True, text=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if out:
        print("[inject] javac:", out[:400], flush=True)
    return "/tmp/jdi"


def wait_mmkv(adb, rounds=12, gap=5):
    for i in range(rounds):
        t = adb("shell", f"run-as {PKG} ls {MMKV_ROOT}/default 2>/dev/null")
        if "default" in t and "No such" not in t:
            return True
        time.sleep(gap)
    return False


def inject_device_id(adb, pids, key="deviceId", value=DEV_ID, tries=2):
    """返回 True 表示写入并回读成功"""
    cp = build()
    try:
        print("[inject] adb jdwp pids:", " ".join((adb("jdwp") or "").split()), flush=True)
    except Exception as e:
        print("[inject] adb jdwp ERR:", e, flush=True)
    for attempt in range(tries):
        for i, pid in enumerate(pids):
            port = 8700 + i
            adb("forward", "--remove-all")
            fw = adb("forward", f"tcp:{port}", f"jdwp:{pid}").strip()
            print(f"[inject] try{attempt} pid={pid} forward={fw}", flush=True)
            try:
                r = subprocess.run([javabin("java"), "-classpath", cp, "MmkvInject",
                                    "localhost", str(port), "default", MMKV_ROOT, key, value],
                                   capture_output=True, text=True, timeout=120)
                out = (r.stdout or "") + (r.stderr or "")
            except Exception as e:
                out = f"[inject ERR] {e}"
            for ln in out.splitlines()[-12:]:
                print(f"[inject {pid}]", ln[:220], flush=True)
            if "readback" in out and value[:12] in out:
                print("[inject] deviceId 注入成功", flush=True)
                return True
        time.sleep(6)
    print("[inject] deviceId 注入失败", flush=True)
    return False
