#!/usr/bin/env python3
"""FMCS App UI 驱动（跑在 GitHub Actions 的模拟器里）。

用法: fms_ui.py [recon|full]
  recon - 装包、启动、周期性 dump UI + logcat，供离线分析
  full  - 完整流程: 登录 -> Free Credits -> claim -> 看广告 -> 领取
"""
import os
import shutil
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

PKG = "com.nunofacha.fmcs"
OUT = "ui"
USER = os.environ.get("FMCS_USER", "doing1024")
PASS = os.environ.get("FMCS_PASS", "")


try:
    from mmkv_inject import inject_device_id, wait_mmkv, DEV_ID, MMKV_ROOT
except Exception as _e:  # pragma: no cover
    print("[warn] mmkv_inject 不可用:", _e)
    inject_device_id = None
    DEV_ID = ""
    MMKV_ROOT = f"/data/data/{PKG}/files/mmkv"

    def wait_mmkv(*a, **k):
        return False


def adb(*args, timeout=90):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except subprocess.TimeoutExpired:
        print(f"[adb] TIMEOUT: {' '.join(args[:3])}")
        return "[TIMEOUT]"
    except Exception as e:
        print(f"[adb] ERR: {e}")
        return "[ERR]"


def sdk_home():
    return (os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
            or "/usr/local/lib/android/sdk")


def emu_image_dir():
    return os.path.join(sdk_home(), "system-images/android-34/google_apis_playstore/x86_64")


def adb_root_ok():
    return adb("shell", "id", timeout=30).strip().startswith("uid=0")


def sh_out(args, timeout=300):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return f"[ERR] {e}"


def patch_images(imgdir):
    """对镜像目录里所有 *.img 做等长字节替换：ro.debuggable=0->1, ro.secure=1->0。
    system.img 受 dm-verity 保护且 mount 失败，故直接扫 ramdisk 等所有 img。"""
    import mmap
    subs = [(b"ro.debuggable=0", b"ro.debuggable=1"),
            (b"ro.secure=1", b"ro.secure=0")]
    if not os.path.isdir(imgdir):
        print("[root] 镜像目录不存在:", imgdir, flush=True)
        return False
    print("[root] 镜像目录内容:", sorted(os.listdir(imgdir))[:40], flush=True)
    hit_total = 0
    for name in sorted(os.listdir(imgdir)):
        if not name.endswith(".img"):
            continue
        path = os.path.join(imgdir, name)
        size = os.path.getsize(path)
        if size < 1024 or size > 6 * 1024 ** 3:
            continue
        with open(path, "rb") as f:
            magic = f.read(8)
        cnt = 0
        try:
            with open(path, "r+b") as f:
                mm = mmap.mmap(f.fileno(), 0)
                for old, newv in subs:
                    pos = 0
                    while True:
                        i = mm.find(old, pos)
                        if i < 0:
                            break
                        print(f"[root] {name}({size},{magic.hex()[:16]}) @{i} "
                              f"hit {old.decode()}", flush=True)
                        mm[i:i + len(old)] = newv
                        cnt += 1
                        pos = i + len(old)
                mm.flush()
                mm.close()
        except Exception as e:
            print(f"[root] patch {name} 失败: {e}", flush=True)
            continue
        if cnt:
            print(f"[root] patched {name}: {cnt} 处", flush=True)
            hit_total += cnt
    print("[root] 替换总数:", hit_total, flush=True)
    return hit_total > 0

def emu_args(extra=()):
    emu = os.path.join(sdk_home(), "emulator", "emulator")
    return [emu, "-avd", "test", "-no-window", "-gpu", "swiftshader_indirect",
            "-no-snapshot", "-noaudio", "-no-boot-anim", "-no-metrics", "-port", "5554",
            *extra]


def stop_emu():
    adb("emu", "kill", timeout=30)
    time.sleep(4)
    subprocess.run(["pkill", "-f", "qemu-system"], capture_output=True)
    time.sleep(4)


def boot_emu(extra=(), tag="emu2"):
    args = emu_args(extra)
    print("[root] 启动模拟器:", " ".join(args), flush=True)
    log = open(tag + ".log", "wb")
    subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
    boot = "0"
    for i in range(60):
        time.sleep(5)
        boot = adb("shell", "getprop", "sys.boot_completed", timeout=30).strip()
        if boot == "1":
            print(f"[root] boot completed ({(i + 1) * 5}s)", flush=True)
            break
    return boot == "1"


def setup_root_jdwp():
    """给 PlayStore 镜像开 root + 全应用 JDWP（原始签名 APK 才能被 JDWP 注入）"""
    if adb_root_ok():
        adb("shell", "setprop", "persist.debug.dalvik.vm.jdwp.enabled", "1")
        print("[root] 已经是 root", flush=True)
        return True
    print("[root] 关模拟器，尝试用 -prop 开 ro.debuggable", flush=True)
    stop_emu()
    boot_emu(["-prop", "ro.debuggable=1", "-prop", "ro.secure=0",
              "-prop", "persist.debug.dalvik.vm.jdwp.enabled=1"])
    for k in ("ro.debuggable", "ro.secure", "persist.debug.dalvik.vm.jdwp.enabled",
              "ro.build.type"):
        print(f"[root] getprop {k} =", adb("shell", "getprop", k, timeout=30).strip(), flush=True)
    print("[root] adb root ->", adb("root", timeout=30).strip(), flush=True)
    for i in range(10):
        time.sleep(3)
        if adb_root_ok():
            break
    print("[root] shell id:", adb("shell", "id", timeout=30).strip()[:120], flush=True)
    if adb_root_ok():
        adb("shell", "setprop", "persist.debug.dalvik.vm.jdwp.enabled", "1")
        print("[root] jdwp prop =", adb("shell", "getprop",
                                       "persist.debug.dalvik.vm.jdwp.enabled").strip(), flush=True)
        return True
    print("[root] -prop 无效, 检查 sysdir 的 build.prop / VerifiedBootParams", flush=True)
    sd = emu_image_dir()
    bp = os.path.join(sd, "build.prop")
    vbp = os.path.join(sd, "VerifiedBootParams.textproto")
    print("[root] sysdir build.prop:", sh_out(["grep", "-nE", "debuggable|secure|build.type", bp])[:400],
          flush=True)
    print("[root] VerifiedBootParams:", sh_out(["cat", vbp])[:1200], flush=True)
    print("[root] local.prop:", sh_out(["cat", os.path.join(sd, "data", "local.prop")])[:300], flush=True)

    def probe(tag):
        print(f"[root] == {tag} ==", flush=True)
        for k in ("ro.debuggable", "ro.secure", "ro.boot.veritymode",
                  "persist.debug.dalvik.vm.jdwp.enabled"):
            print(f"[root] {k} =", adb("shell", "getprop", k, timeout=30).strip(), flush=True)
        print("[root] adb root ->", adb("root", timeout=30).strip(), flush=True)
        for _ in range(8):
            time.sleep(3)
            if adb_root_ok():
                break
        print("[root] id:", adb("shell", "id", timeout=30).strip()[:60], flush=True)
        print("[root] mounts:", adb("shell", "mount | grep -E 'dm-| / | /vendor '", timeout=30
                                   ).strip().replace("\n", " | ")[:700], flush=True)
        return adb_root_ok()

    # 尝试 C: 去掉 androidboot.vbmeta.*（关掉 dm-verity）+ 补丁 system.img
    print("[root] 去掉 vbmeta 参数 + 补丁 system.img", flush=True)
    sh_out(["bash", "-lc", f"cp {vbp} {vbp}.bak; sed -i '/androidboot.vbmeta/d' {vbp}; "
                          f"cat {vbp}"])
    print("[root] vbp now:", sh_out(["cat", vbp])[:300], flush=True)
    stop_emu()
    patch_images(sd)
    boot_emu([], tag="emu4")
    if probe("C: no-vbmeta + patched system.img"):
        adb("shell", "setprop", "persist.debug.dalvik.vm.jdwp.enabled", "1")
        return True
    # 尝试 D: 再加 -writable-system
    print("[root] 尝试 D: -writable-system", flush=True)
    stop_emu()
    boot_emu(["-writable-system"], tag="emu5")
    if probe("D: writable-system"):
        adb("shell", "setprop", "persist.debug.dalvik.vm.jdwp.enabled", "1")
        return True
    print("[root] 全部失败, dump 诊断", flush=True)
    print("[root] guest build.prop content:", adb("shell", "su 0 cat /system/build.prop 2>/dev/null | "
                                                     "grep -n debuggable", timeout=30).strip()[:300],
          flush=True)
    print("[root] verity 仍生效", flush=True)
    print("[root] all props:", adb("shell", "getprop | grep -iE 'debuggable|secure|jdwp'",
                                   timeout=30).strip()[:400], flush=True)
    print("[root] guest /system mount:", adb("shell", "mount | grep -E ' /system | /vendor '",
                                            timeout=30).strip()[:300], flush=True)
    print("[root] build.prop mode:", adb("shell", "ls -la /system/build.prop",
                                        timeout=30).strip()[:200], flush=True)
    return False


def save_proxy():
    """保存 tinyproxy 日志(每阶段调用, 防崩溃丢证据)"""
    try:
        out = subprocess.run(["sudo", "cat", "/var/log/tinyproxy/tinyproxy.log"],
                             capture_output=True, text=True, timeout=30).stdout
        open("proxy.log", "w").write(out)
        conn = [ln for ln in out.splitlines() if "CONNECT" in ln and "freemcserver" in ln]
        for ln in conn[-8:]:
            print("[prx]", ln[:150])
    except Exception as e:
        print("[proxy]", e)


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
    """优先 input text (精确字符); 失败时逐字符 keyevent"""
    esc = text.replace(" ", "%s").replace("&", "\\&").replace("<", "\\<").replace(">", "\\>").replace("(", "\\(").replace(")", "\\)")
    out = adb("shell", "input", "text", esc)
    if "Error" in (out or "") or "Exception" in (out or ""):
        print("[type] input text 失败, 回退 keyevent:", out.strip()[:80])
        for ch in text:
            if ch.isupper():
                adb("shell", "input", "keyevent", "60")
                adb("shell", "input", "keyevent", str(KEYMAP[ch.lower()]))
            elif ch in KEYMAP:
                adb("shell", "input", "keyevent", str(KEYMAP[ch]))
            else:
                adb("shell", "input", "text", ch)
            time.sleep(0.12)


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
        adb("shell", "input", "keyevent", "111")  # ESC 收起键盘 (BACK 会退 Activity)
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
    r = adb("logcat", "-d", timeout=60)
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


def net_forensics(tag):
    """guest 网络取证: DNS 解析结果 + TCP socket 状态 + 连接性"""
    print(f"[net] === {tag} ===")
    r = adb("shell", "ping -c 1 -W 3 api.freemcserver.net", timeout=30)
    print("[net] ping:", r.strip().splitlines()[:2])
    r = adb("shell", "ss -tn 2>/dev/null || netstat -tn 2>/dev/null", timeout=30)
    for ln in r.splitlines():
        if ":443" in ln or ":80 " in ln:
            print("[net] sock:", ln.strip()[:130])
    r = adb("shell", "dumpsys connectivity", timeout=60)
    for ln in r.splitlines():
        ll = ln.strip()
        if ll.startswith(("Active default network", "Current Networks", "NetworkCapabilities")) or "VALIDATED" in ll:
            print("[net] conn:", ll[:130])


def guest_api_probe():
    """guest 内直接发 HTTP 探针, 绕开 app: 判定网络层 vs app 层问题"""
    # 1) 尝试推静态 curl 进 guest
    push = adb("push", "guest-curl", "/data/local/tmp/curl", timeout=60)
    print("[probe] push curl:", push.strip()[:100], flush=True)
    adb("shell", "chmod 755 /data/local/tmp/curl", timeout=20)
    curl = "/data/local/tmp/curl"
    chk = adb("shell", f"{curl} --version 2>&1 | head -1", timeout=30)
    if "curl" not in chk:
        print("[probe] guest curl 不可用:", chk.strip()[:100], flush=True)
        # Chrome 兼底: 浏览器开 GET 页面看结果
        print("[probe] fallback: Chrome 打开 API 页面", flush=True)
        adb("shell", "am start -a android.intent.action.VIEW -d https://api.freemcserver.net/v4/app-settings/credits", timeout=30)
        time.sleep(12)
        adb("shell", "screencap -p /sdcard/chrome_probe.png", timeout=30)
        subprocess.run(["adb", "pull", "/sdcard/chrome_probe.png", "ui/chrome_probe.png"], capture_output=True, timeout=60)
        adb("shell", "input keyevent 4", timeout=20)
        return
    print("[probe] guest curl OK:", chk.strip()[:60], flush=True)
    RES = "--resolve api.freemcserver.net:443:104.26.0.208"  # 绕过 musl DNS bug
    r = adb("shell", f"getprop net.dns1; ping -c1 -W2 api.freemcserver.net 2>&1 | head -2", timeout=30)
    print("[probe] dns/ping ->", r.strip()[:120], flush=True)
    # GET 探针 (对照: 宿主同请求 <1s 返回 scope JSON)
    r = adb("shell", f"{curl} -s -m 12 {RES} -A FMCS-App https://api.freemcserver.net/v4/app-settings/credits -w ' HTTP=%{{http_code}} t=%{{time_total}}s'", timeout=40)
    print("[probe] GET ->", r.strip()[:200])
    # POST 探针: 用假凭据, 不碰真实账号; 若网络正常会快速返回 4xx JSON
    r = adb("shell", f"{curl} -s -m 15 {RES} -A FMCS-App -H 'Content-Type: application/json' -d '{{\"username\":\"probebot\",\"password\":\"x\",\"scope\":\"ANDROID_APP\"}}' https://api.freemcserver.net/v4/user/login -w ' HTTP=%{{http_code}} t=%{{time_total}}s'", timeout=60)
    print("[probe] POST ->", r.strip()[:220])
    r = adb("shell", f"{curl} -s -m 15 {RES} -A FMCS-App -H 'Content-Type: application/json' -d '{{\"username\":\"probebot2\",\"password\":\"x\",\"scope\":\"ANDROID_APP\"}}' https://api.freemcserver.net/v4/user/login -w ' HTTP=%{{http_code}} t=%{{time_total}}s'", timeout=60)
    print("[probe] POST2 ->", r.strip()[:220])


def foreground():
    r = adb("shell", "dumpsys window 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp' | head -2", timeout=30)
    return r.strip().replace("\n", " | ")


def relaunch():
    adb("shell", f"am force-stop {PKG}")
    time.sleep(2)
    adb("shell", f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
    for _ in range(10):
        time.sleep(3)
        if app_pid():
            time.sleep(8)
            return True
    return False


def is_home(ns):
    return bool(find(ns, "my servers") or find(ns, "new server") or find(ns, "your servers")
                or find(ns, "credits", "watch ads"))


def is_login(ns):
    return bool(find(ns, "welcome back", "username", "sign in"))


def on_desktop(ns):
    return bool(find(ns, "predicted app") or find(ns, "sun, nov") or find(ns, "mon, nov")
                or find(ns, "play store", "gmail", "photos"))


def wait_login_ready(rounds=15, gap=4):
    """等登录页稳定渲染 (App 空闲时下断点注入更安全)"""
    for i in range(rounds):
        if not app_pid():
            return False
        ns = dump_ui(f"preinject{i}") and nodes(f"preinject{i}") or []
        if is_login(ns):
            print(f"[main] 登录页就绪 (第{i + 1}轮)", flush=True)
            return True
        if on_desktop(ns):
            print("[main] 在桌面, 拉回前台", flush=True)
            relaunch()
        time.sleep(gap)
    return False


def crash_forensics():
    print("\n[crash] ===== 崩溃/进程事件 =====", flush=True)
    r = adb("shell",
            "logcat -d -b all | grep -iE 'nunofacha|am_crash|am_proc_died|am_anr|AndroidRuntime|FATAL' | tail -60",
            timeout=120)
    for ln in r.splitlines()[-60:]:
        print("[crash]", ln[:200], flush=True)
    print("[crash] ===== 前台窗口 =====", flush=True)
    print(foreground(), flush=True)
    print("[crash] pid:", app_pid() or "dead", flush=True)


def login_once(dump_prefix="lg"):
    """填表 + 提交 + 轮询主页; 返回 True 表示进入主页"""
    dump_ui(f"{dump_prefix}_0")
    ns = nodes(f"{dump_prefix}_0")
    if not is_login(ns):
        print("[login] 当前不在登录页")
        return is_home(ns)
    ets = [n for n in ns if "EditText" in n["cls"]]
    print(f"[login] EditText 数: {len(ets)}")
    if len(ets) < 2:
        print("[login] 字段不足, 放弃")
        return False
    input_text(ets[0], USER, "f_user", "Username")
    time.sleep(1)
    dump_ui("pre_pass")
    ns2 = nodes("pre_pass")
    ets2 = [n for n in ns2 if "EditText" in n["cls"]]
    ok = input_text(ets2[1], PASS, "f_pass", "Password", secure=True) if len(ets2) >= 2 else False
    print("[login] 密码填入:", ok)
    time.sleep(0.5)
    dump_ui("login_ready")
    shot("login_ready")
    def submit(tag, label):
        adb("shell", "input keyevent 111", timeout=30)
        time.sleep(0.8)
        dump_ui(tag)
        nsx = nodes(tag)
        print("[login] IME:", adb("shell", "dumpsys input_method | grep -m1 mInputShown",
                                 timeout=30).strip()[:80], flush=True)
        b = find(nsx, "sign in")
        if not b:
            print("[login] 未找到 Sign In")
            return False
        tap(b)
        print(f"[login] 已提交登录({label})", flush=True)
        time.sleep(6)
        cnt = adb("shell", "logcat -d -b all | grep -c 'Executing username/password login'",
                  timeout=90).strip()
        print("[login] Executing 计数:", cnt, flush=True)
        return cnt.isdigit() and int(cnt) > 0

    if not submit("signin0", "first"):
        if not submit("signin1", "retry"):
            print("[login] Sign In 点击未触发登录请求", flush=True)
    for i in range(11):
        time.sleep(8)
        tag = f"{dump_prefix}_home{i:02d}"
        dump_ui(tag)
        shot(tag)
        ns = nodes(tag)
        if is_home(ns) and not is_login(ns):
            print(f"[login] 主页就绪 (第{i}轮)")
            summarize(ns, tag)
            return True
        if on_desktop(ns):
            print("[login] 掉到桌面, 拉回前台")
            adb("shell", f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
            time.sleep(6)
        if i == 5:
            b = find(ns, "sign in")
            if b:
                print("[login] 补点一次 Sign In")
                tap(b)
        summarize(ns, tag)
    return False


def reward_log(pat_kw=r"AdsForCredits|Integrity|PLAY_STORE|claim|Reward|Nonce|nonce|Authorization ID|Device registration|Failed to obtain|Token obtained|credits/|reward/"):
    out = adb("shell", f"logcat -d -v time | grep -E '{pat_kw}' | grep -vE 'Bugle|Heterodyne|TextClassifier'", timeout=120)
    return out if out and "[TIMEOUT]" not in out and "[ERR]" not in out else ""


def save_reward_log(n=12):
    txt = reward_log()
    try:
        open("reward_log.txt", "w").write(txt)
    except Exception as e:
        print("[reward] save failed:", e)
    for ln in txt.splitlines()[-n:]:
        print("[js]", ln[:190])


def flow_full():
    ns = wait_app()
    net_forensics("pre_login")
    logged_in = False
    for attempt in range(2):
        tag = f"pre_chk{attempt}"
        dump_ui(tag)
        ns = nodes(tag)
        if is_home(ns):
            logged_in = True
            break
        if on_desktop(ns) or not app_pid():
            print("[flow] App 不在前台/已死, 重新拉起")
            relaunch()
            dump_ui(f"{tag}_re")
            ns = nodes(f"{tag}_re")
            if is_home(ns):
                logged_in = True
                break
        if is_login(ns):
            print(f"[flow] 登录页 (第{attempt+1}次尝试)")
            if login_once(f"try{attempt}"):
                logged_in = True
                break
        else:
            summarize(ns, tag)
    print("[flow] 登录结果:", "OK" if logged_in else "FAILED (已停止, 保护账号)")
    save_logcat()
    save_proxy()
    if not logged_in:
        dump_ui("login_final")
        shot("login_final")
        crash_forensics()
        print("[flow] 登录未成功, 提前结束")
        return

    def go_credits():
        for i in range(10):
            tag = f"nav_{i:02d}"
            dump_ui(tag)
            shot(tag)
            ns = nodes(tag)
            if on_desktop(ns) or not app_pid():
                print("[nav] 桌面/进程死, 拉回")
                adb("shell", f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
                time.sleep(6)
                continue
            tab = find(ns, "credits")
            if tab:
                print("[nav] 点 Credits tab")
                tap(tab)
                return True
            if is_login(ns):
                print("[nav] 回到登录页, 说明被踢/重启")
                return False
            time.sleep(3)
        return False

    if not go_credits():
        print("[flow] 未能进入 Credits tab")
        crash_forensics()
        return
    time.sleep(6)
    dump_ui("credits_tab")
    shot("credits_tab")
    save_reward_log()
    ns = nodes("credits_tab")
    summarize(ns, "credits_tab")
    if is_login(ns):
        print("[flow] 又回到登录页, 尝试重新登录一次")
        if not login_once("relog"):
            crash_forensics()
            return
        if not go_credits():
            return
        time.sleep(6)
        dump_ui("credits_tab2")
        shot("credits_tab2")
        ns = nodes("credits_tab2")
        summarize(ns, "credits_tab2")

    # Watch Ads for Credits 卡片
    card = find(ns, "watch ads for credits", "watch ads")
    if card:
        print("[flow] 点 Watch Ads for Credits")
        tap(card)
        time.sleep(8)
        dump_ui("reward_page")
        shot("reward_page")
        save_reward_log()
        summarize(nodes("reward_page"), "reward_page")
    else:
        print("[flow] 未找到 Watch Ads for Credits 卡片")
        crash_forensics()

    earn = find(nodes("reward_page"), "watch ad & earn", "watch ad")
    if earn:
        print("[flow] 点 Watch Ad & Earn -> 应进入 Integrity 验证")
        tap(earn)
    else:
        print("[flow] 未找到 Watch Ad & Earn 按钮")

    seen = set()
    for i in range(24):
        time.sleep(7)
        tag = f"rw_{i:02d}"
        dump_ui(tag)
        shot(tag)
        save_reward_log(6)
        save_proxy()
        ns = nodes(tag)
        tsz = " | ".join([x["text"] for x in ns if x["text"]])[:120]
        print(f"[rw {i}] {tsz}")
        if on_desktop(ns) or not app_pid():
            print("[rw] 桌面/进程死 -> 取证并尝试拉回")
            crash_forensics()
            adb("shell", f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
            time.sleep(8)
            continue
        txt = reward_log(r"Authorization ID|Token obtained|Token not obtained|PLAY_STORE|Failed to obtain|claim|Claim|Reward granted|Nonce")
        for ln in txt.splitlines():
            k = ln[18:]
            if k in seen:
                continue
            for key in ("Authorization ID", "Token obtained", "Token not obtained", "PLAY_STORE",
                        "Failed to obtain", "claim", "Claim", "Reward granted", "Nonce"):
                if key in ln:
                    seen.add(k)
                    print("[js*]", ln[:190])
                    break
        close = None
        for m in ns:
            hay = (m["text"] + " " + m["desc"]).lower()
            if any(k in hay for k in ("close", "skip", "dismiss", "×")) and m["box"][2] - m["box"][0] < 200:
                close = m
                break
        if close:
            print("[flow] 点广告关闭")
            tap(close)
            time.sleep(5)
            continue
        if find(ns, "watch ad & earn") and i > 3:
            print("[flow] 回到 reward 页, 再点 Watch Ad & Earn")
            b = find(ns, "watch ad & earn")
            tap(b)
            time.sleep(3)
        if find(ns, "failed to obtain integrity"):
            print("[flow] !! Integrity 失败 (无法签发 token)")
            break
    dump_ui("final")
    shot("final")
    save_reward_log(20)
    save_logcat()
    crash_forensics()
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


def img_info(imgdir):
    for name in sorted(os.listdir(imgdir)):
        if not name.endswith(".img"):
            continue
        p = os.path.join(imgdir, name)
        sz = os.path.getsize(p)
        head = sh_out(["bash", "-lc", f"xxd -s 1024 -l 32 {p} 2>/dev/null | head -2"])
        print(f"[img] {name} size={sz} sb1024={head}", flush=True)
        print(f"[img] file: {sh_out(['bash', '-lc', f'file {p}'])[:200]}", flush=True)


def extract_sysroot(imgdir):
    """把 system.img 挂/解到 /tmp/sysroot，用于取 build.prop 与 Play Store APK。"""
    sysimg = os.path.join(imgdir, "system.img")
    root = "/tmp/sysroot"
    os.makedirs(root, exist_ok=True)
    sh_out(["sudo", "umount", "-f", root])
    for fstype in ("ext4", "erofs"):
        out = sh_out(["sudo", "mount", "-t", fstype, "-o", "loop,ro", sysimg, root], timeout=180)
        if sh_out(["mountpoint", root]).find("is a mountpoint") >= 0:
            print(f"[extract] mounted as {fstype} ✓", flush=True)
            return root, True
        print(f"[extract] mount -t {fstype} failed: {out[:160]}", flush=True)
    print("[extract] 尝试 fsck.erofs --extract（用户态解包）", flush=True)
    sh_out(["sudo", "apt-get", "install", "-y", "-qq", "erofs-utils"], timeout=600)
    print("[extract]", sh_out(["sudo", "fsck.erofs", f"--extract={root}", sysimg],
                              timeout=1800)[-400:], flush=True)
    return root, os.path.isdir(root)


def find_vending(root):
    out = sh_out(["sudo", "bash", "-lc",
                  f"find {root} -maxdepth 5 -iname '*vending*' -o -maxdepth 5 -iname "
                  f"'*Phonesky*' 2>/dev/null | head -20"])
    print("[vending] candidates:", out[:800], flush=True)
    return out


def sdk_install_pkg(pkg):
    sd = os.path.join(sdk_home(), "cmdline-tools", "latest", "bin", "sdkmanager")
    if not os.path.exists(sd):
        print("[sdk] sdkmanager 缺失", flush=True)
        return False
    env = dict(os.environ, JAVA_HOME=sh_out(["bash", "-lc", "ls -d /usr/lib/jvm/*17* 2>/dev/null"
                                             " | head -1"]) or os.environ.get("JAVA_HOME", ""))
    print("[sdk] install", pkg, flush=True)
    r = subprocess.run([sd, "--install", pkg], input="y\n" * 40, capture_output=True,
                       text=True, timeout=1500, env=env)
    tail = ((r.stdout or "") + (r.stderr or ""))[-500:]
    print("[sdk] ->", tail.replace("\n", " | ")[:500], flush=True)
    return "100%" in (r.stdout or "") or True


def make_avd(name, sysdir_rel, w=1080, h=2340, mem=3072):
    base = os.path.expanduser("~/.android/avd")
    os.makedirs(os.path.join(base, name + ".avd"), exist_ok=True)
    with open(os.path.join(base, name + ".ini"), "w") as f:
        f.write(f"avd.ini.encoding=UTF-8\npath={base}/{name}.avd\npath.rel=avd/{name}.avd\n"
                f"target=android-34\n")
    cfg = f"""avd.ini.encoding=UTF-8
AvdId={name}
abi.type=x86_64
hw.cpu.arch=x86_64
hw.cpu.ncore=2
hw.ramSize={mem}
hw.lcd.width={w}
hw.lcd.height={h}
hw.lcd.density=440
hw.gpu.enabled=yes
hw.gpu.mode=swiftshader_indirect
hw.keyboard=yes
hw.mainKeys=no
disk.dataPartition.size=6G
image.sysdir.1={sysdir_rel}
tag.id=google_apis
tag.display=Google APIs
hw.initialOrientation=Portrait
showDeviceFrame=no
PlayStore.enabled=false
"""
    with open(os.path.join(base, name + ".avd", "config.ini"), "w") as f:
        f.write(cfg)
    print("[avd]", name, "created, sysdir =", sysdir_rel, flush=True)


def explore():
    """诊断 system.img 格式 / 取 Play Store APK / 装 google_apis(userdebug) 镜像并测 adb root。"""
    imgdir = emu_image_dir()
    print("[explore] imagedir =", imgdir, flush=True)
    img_info(imgdir)
    root, ok = extract_sysroot(imgdir)
    print("[explore] extracted:", ok, flush=True)
    if ok:
        print("[explore] sysroot top:", sh_out(["sudo", "ls", root])[:300], flush=True)
        print("[explore] build.prop:",
              sh_out(["sudo", "grep", "-nE", "debuggable|build.type", root + "/build.prop"]
                     if os.path.exists(root + "/build.prop") else
                     ["sudo", "bash", "-lc", f"find {root} -maxdepth 2 -name build.prop"])[:400],
              flush=True)
        print("[explore] vending:", find_vending(root)[:800], flush=True)
    # google_apis (userdebug) 镜像 + root 测试
    ga_rel = "system-images/android-34/google_apis/x86_64/"
    ga_dir = os.path.join(sdk_home(), ga_rel)
    if not os.path.isdir(ga_dir):
        sdk_install_pkg("system-images;android-34;google_apis;x86_64")
    print("[explore] google_apis image exists:", os.path.isdir(ga_dir), flush=True)
    if os.path.isdir(ga_dir):
        make_avd("ga", ga_rel)
        adb("emu", "kill", timeout=30)
        time.sleep(4)
        subprocess.run(["pkill", "-f", "qemu-system"], capture_output=True)
        time.sleep(4)
        emu = os.path.join(sdk_home(), "emulator", "emulator")
        args = [emu, "-avd", "ga", "-no-window", "-gpu", "swiftshader_indirect", "-no-snapshot",
                "-noaudio", "-no-boot-anim", "-no-metrics", "-port", "5554"]
        print("[explore] boot ga:", " ".join(args), flush=True)
        log = open("ga.log", "wb")
        subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
        for i in range(40):
            time.sleep(5)
            if adb("shell", "getprop", "sys.boot_completed", timeout=30).strip() == "1":
                print(f"[explore] ga boot ok ({(i + 1) * 5}s)", flush=True)
                break
        print("[explore] ga adb root ->", adb("root", timeout=40).strip()[:120], flush=True)
        for i in range(10):
            time.sleep(3)
            if adb_root_ok():
                break
        print("[explore] ga id:", adb("shell", "id", timeout=30).strip()[:80], flush=True)
        print("[explore] ga props:", adb("shell", "getprop | grep -iE 'debuggable|build.type|"
                                        "veritymode'", timeout=30).strip()[:300], flush=True)


GA_REL = "system-images/android-34/google_apis/x86_64/"


def gpt_parts(path):
    """解析 GPT 全部分区：返回 [(start_lba, end_lba, name)]。"""
    parts = []
    try:
        with open(path, "rb") as f:
            f.seek(512)
            hdr = f.read(92)
            if hdr[:8] != b"EFI PART":
                return parts
            ent_lba = int.from_bytes(hdr[72:80], "little")
            nent = int.from_bytes(hdr[80:84], "little")
            esz = int.from_bytes(hdr[84:88], "little") or 128
            f.seek(ent_lba * 512)
            raw = f.read(esz * min(nent, 64))
            for i in range(min(nent, 64)):
                e = raw[i * esz:(i + 1) * esz]
                if len(e) < 128 or e[0:16] == b"\x00" * 16:
                    continue
                name = e[56:128].decode("utf-16-le", "ignore").rstrip("\x00") if len(e) >= 128 else ""
                parts.append((int.from_bytes(e[32:40], "little"),
                              int.from_bytes(e[40:48], "little"), name))
    except Exception as ex:
        print("[gpt] err", ex, flush=True)
    return parts


def find_fs_offset(path, scan=64 * 1024 * 1024):
    """在镜像前 64MB 里找 ext4/erofs/f2fs 超级块偏移（GPT 分区起点）。"""
    magics = [(b"\x53\xef", "ext4", 1080), (b"\xe2\xe1\xf5\xe0", "erofs", 1024),
              (b"\x10\x20\xf5\xf2", "f2fs", 1024)]
    try:
        with open(path, "rb") as f:
            buf = f.read(scan + 8192)
    except Exception as e:
        print("[fs] read err", e, flush=True)
        return None, 0
    for off in range(0, max(0, len(buf) - 8192), 512):
        for m, name, delta in magics:
            if buf[off + delta:off + delta + len(m)] == m:
                return name, off
    return None, 0


def find_all_ext4(path, limit=8):
    """在整块 GPT/super 镜像里找出所有 ext4 超级块偏移（mm.find 快速扫）。"""
    import mmap
    offs = []
    try:
        with open(path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            pos = 0
            while len(offs) < limit:
                i = mm.find(b"\x53\xef", pos)
                if i < 0:
                    break
                fs = i - 1080
                if fs > 0 and fs % 512 == 0:
                    offs.append(fs)
                    print("[fs] ext4 candidate @", fs, flush=True)
                    pos = i + 4096 * 64
                else:
                    pos = i + 1
            mm.close()
    except Exception as ex:
        print("[fs] scan err", ex, flush=True)
    return offs


def try_mount_partition(img, off, mnt):
    for fstype in ("ext4", "erofs", "f2fs"):
        sh_out(["sudo", "umount", "-f", mnt])
        out = sh_out(["sudo", "mount", "-t", fstype, "-o", f"loop,ro,offset={off}", img, mnt],
                     timeout=240)
        if sh_out(["mountpoint", mnt]).find("is a mountpoint") >= 0:
            return fstype
    return ""


def extract_vending_from_playstore_image():
    """遍历镜像 GPT 分区，逐个尝试挂载，找 Play Store 的 apk。"""
    mnt = "/tmp/psimg"
    os.makedirs(mnt, exist_ok=True)
    for name in ("system.img", "vendor.img", "product.img", "system_ext.img"):
        img = os.path.join(emu_image_dir(), name)
        if not os.path.exists(img):
            continue
        parts = gpt_parts(img)
        print(f"[vending] {name} partitions:", [(a, b, n) for a, b, n in parts], flush=True)
        cands = [(a * 512) for a, b, n in parts]
        fstype, off = find_fs_offset(img)
        if fstype:
            cands.insert(0, off)
        cands += find_all_ext4(img)
        for off in dict.fromkeys(cands):
            mt = try_mount_partition(img, off, mnt)
            if not mt:
                continue
            print(f"[vending] {name} mounted {mt} @ {off} ✓", flush=True)
            apks = sh_out(["sudo", "bash", "-lc", f"find {mnt} -name '*.apk' 2>/dev/null"])
            hit = [a for a in apks.splitlines() if "ending" in a.lower() or "phonesky" in a.lower()
                   or "play" in a.lower().split("/")[-1]]
            print("[vending] apk total:", len(apks.splitlines()), "hits:", hit[:6], flush=True)
            if hit:
                sh_out(["sudo", "cp", hit[0], "/tmp/Vending.apk"])
                sh_out(["sudo", "chmod", "644", "/tmp/Vending.apk"])
                print("[vending] size:", os.path.getsize("/tmp/Vending.apk"), flush=True)
                sh_out(["sudo", "umount", "-f", mnt])
                return "/tmp/Vending.apk"
            print("[vending] ls:", sh_out(["sudo", "bash", "-lc", f"ls {mnt}"]
                                          ).replace("\n", " ")[:300], flush=True)
            sh_out(["sudo", "umount", "-f", mnt])
    return ""


def vend_mode():
    """只做: 从 playstore 镜像抽 Vending.apk 并装成系统应用（独立小实验）。"""
    boot_ga()
    v = extract_vending_from_playstore_image()
    print("[vend] vending apk:", v, flush=True)
    if not v:
        return False
    print("[vend] disable-verity:", adb("disable-verity", timeout=180).strip()[:150], flush=True)
    adb("reboot", timeout=60)
    time.sleep(12)
    for _ in range(24):
        time.sleep(5)
        if adb("shell", "getprop", "sys.boot_completed", timeout=30).strip() == "1":
            break
    print("[vend] root:", adb("root", timeout=40).strip()[:60], flush=True)
    time.sleep(5)
    print("[vend] remount:", adb("remount", timeout=300).strip().replace("\n", " | ")[:400],
          flush=True)
    adb("shell", "mkdir -p /system/priv-app/Vending", timeout=60)
    print("[vend] push:", adb("push", v, "/system/priv-app/Vending/Vending.apk", timeout=600)[:120],
          flush=True)
    print("[vend] fix:", adb("shell", "chmod 644 /system/priv-app/Vending/Vending.apk; "
                                     "restorecon -R /system/priv-app/Vending; df -h /system | tail -1",
                             timeout=120).strip()[:300], flush=True)
    adb("reboot", timeout=60)
    time.sleep(15)
    for _ in range(24):
        time.sleep(5)
        if adb("shell", "getprop", "sys.boot_completed", timeout=30).strip() == "1":
            break
    print("[vend] pm path:", adb("shell", "pm path com.android.vending", timeout=60).strip()[:150],
          flush=True)
    print("[vend] pkgs:", adb("shell", "pm list packages | grep -i -e vending -e gms",
                              timeout=60).strip()[:300], flush=True)
    return True


def boot_ga():
    """引导 google_apis(userdebug) 镜像：可用 adb root。"""
    ga_dir = os.path.join(sdk_home(), GA_REL)
    if not os.path.isdir(ga_dir):
        sdk_install_pkg("system-images;android-34;google_apis;x86_64")
    if not os.path.isdir(ga_dir):
        print("[ga] 镜像不存在, 放弃", flush=True)
        return False
    make_avd("ga", GA_REL)
    adb("emu", "kill", timeout=30)
    time.sleep(4)
    subprocess.run(["pkill", "-f", "qemu-system"], capture_output=True)
    time.sleep(4)
    emu = os.path.join(sdk_home(), "emulator", "emulator")
    args = [emu, "-avd", "ga", "-no-window", "-gpu", "swiftshader_indirect", "-no-snapshot",
            "-noaudio", "-no-boot-anim", "-no-metrics", "-port", "5554"]
    print("[ga] boot:", " ".join(args), flush=True)
    log = open("ga.log", "wb")
    subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
    for i in range(40):
        time.sleep(5)
        if adb("shell", "getprop", "sys.boot_completed", timeout=30).strip() == "1":
            print(f"[ga] boot ok ({(i + 1) * 5}s)", flush=True)
            break
    print("[ga] adb root ->", adb("root", timeout=40).strip()[:80], flush=True)
    for _ in range(10):
        time.sleep(3)
        if adb_root_ok():
            break
    if not adb_root_ok():
        print("[ga] root FAILED", flush=True)
        return False
    adb("shell", "setprop", "persist.debug.dalvik.vm.jdwp.enabled", "1")
    print("[ga] jdwp =", adb("shell", "getprop",
                             "persist.debug.dalvik.vm.jdwp.enabled").strip(), flush=True)
    adb("shell", "settings", "put", "global", "window_animation_scale", "0")
    adb("shell", "settings", "put", "global", "transition_animation_scale", "0")
    adb("shell", "settings", "put", "global", "animator_duration_scale", "0")
    return True


def push_pristine_libs():
    """把 split 里的 arm64 .so 手工塞进应用 nativeLibraryDir，让原签名包起得来。"""
    import zipfile
    dst = "/tmp/lib64"
    os.makedirs(dst, exist_ok=True)
    for apk in ("config.arm64_v8a.apk", "config.en.apk", "config.mdpi.apk"):
        try:
            z = zipfile.ZipFile(apk)
        except Exception:
            continue
        for n in z.namelist():
            if n.startswith("lib/arm64-v8a/") and n.endswith(".so"):
                with open(os.path.join(dst, os.path.basename(n)), "wb") as f:
                    f.write(z.read(n))
    names = sorted(os.listdir(dst))
    print("[libs] extracted:", len(names), names[:3], flush=True)
    if not names:
        return False
    adb("shell", "rm", "-rf", "/data/local/tmp/lib64")
    adb("push", dst, "/data/local/tmp/", timeout=600)
    out = adb("shell", "ls -d /data/app/*/com.nunofacha.fmcs*/lib 2>/dev/null | head -1",
              timeout=60).strip()
    libdir = out.splitlines()[0].strip() if out.strip() else ""
    print("[libs] full dumpsys:", adb(
        "shell", f"dumpsys package {PKG} | grep -m2 -e legacyNativeLibraryDir -e primaryCpuAbi",
        timeout=60).strip()[:300], flush=True)
    print("[libs] legacyNativeLibraryDir =", libdir, flush=True)
    native = libdir.rstrip("/")
    if not native:
        return False
    abi_dir = native + "/arm64"
    cmd = (f"mkdir -p {abi_dir} && cp /data/local/tmp/lib64/*.so {abi_dir}/ && "
           f"chmod 755 {abi_dir}/*.so && chown -R $(stat -c %u:%g {native}) {abi_dir} && "
           f"restorecon -R {abi_dir} ; ls -la {abi_dir} | head -5")
    print("[libs]", adb("shell", cmd, timeout=300).strip()[:800], flush=True)
    print("[libs] abilist:", adb("shell", "getprop ro.product.cpu.abilist").strip(), flush=True)
    return True


def seed_dir():
    return os.path.join(OUT, "mmkv_seed")


def install_hybrid():
    print("[hybrid] uninstall:", adb("uninstall", PKG, timeout=120).strip()[:100], flush=True)
    print("[hybrid] install:", adb(
        "install-multiple", "-r", "-g", "fms_patched-aligned-debugSigned.apk",
        "cfg_arm64-aligned-debugSigned.apk", "config.en-aligned-debugSigned.apk",
        "config.mdpi-aligned-debugSigned.apk", timeout=900)[:200], flush=True)


def install_pristine():
    print("[pristine] uninstall:", adb("uninstall", PKG, timeout=120).strip()[:100], flush=True)
    print("[pristine] install:", adb(
        "install-multiple", "-r", "-g", "--abi", "arm64-v8a", "fms.apk",
        "config.arm64_v8a.apk", "config.en.apk", "config.mdpi.apk", timeout=900)[:200], flush=True)


def harvest_mmkv_seed():
    """hybrid(debuggable) 包注入好 deviceId 后，root 把 MMKV 目录整份捞出来。"""
    d = seed_dir()
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    adb("shell", f"rm -rf /data/local/tmp/seed && mkdir -p /data/local/tmp/seed && "
                 f"cp -a {MMKV_ROOT}/. /data/local/tmp/seed/ 2>/dev/null; "
                 f"chmod -R 777 /data/local/tmp/seed; ls -la /data/local/tmp/seed", timeout=120)
    print("[seed] pull:", adb("pull", "/data/local/tmp/seed/.", d, timeout=300)[:200], flush=True)
    print("[seed] files:", sorted(os.listdir(d)) if os.path.isdir(d) else "MISSING", flush=True)
    print("[seed] deviceId in seed:", sh_out(["bash", "-lc",
                                              f"strings {d}/default 2>/dev/null | grep -c FMSDEV"])[:20],
          flush=True)
    return os.path.isdir(d) and len(os.listdir(d)) > 0


def plant_mmkv_seed():
    d = seed_dir()
    if not (os.path.isdir(d) and os.listdir(d)):
        print("[seed] 没有 seed, 跳过移植", flush=True)
        return False
    adb("shell", f"mkdir -p {MMKV_ROOT}", timeout=60)
    print("[seed] push:", adb("push", d + "/.", MMKV_ROOT, timeout=300)[:120], flush=True)
    uid = adb("shell", f"stat -c %u:%g /data/data/{PKG}", timeout=60).strip()
    print("[seed] app uid:", uid, flush=True)
    print("[seed] fixup:", adb(
        "shell", f"chown -R {uid} {MMKV_ROOT} && chmod -R 600 {MMKV_ROOT}/* && "
                 f"chmod 700 {MMKV_ROOT} && restorecon -R {MMKV_ROOT}; ls -la {MMKV_ROOT}",
        timeout=120).strip()[:500], flush=True)
    return True


def ga_flow():
    """目标：原签名 APK + 从 debuggable 包移植来的 MMKV(deviceId) + Play Store 签 token。"""
    if not boot_ga():
        return False
    # ---- 1) hybrid 包: JDI 注入 deviceId, 然后 harvest MMKV ----
    install_hybrid()
    adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    for i in range(8):
        time.sleep(3)
        if app_pid():
            break
    print("[hybrid] pid:", app_pid() or "dead", flush=True)
    ok = False
    for round_ in range(2):
        pids = (app_pid() or "").split()
        print(f"[hybrid] inject round {round_} pids={pids}", flush=True)
        if not pids:
            relaunch()
            continue
        ok = inject_device_id(adb, pids) if inject_device_id else False
        print("[hybrid] inject:", "OK" if ok else "FAILED", flush=True)
        if ok:
            break
        relaunch()
    if ok:
        relaunch()
        time.sleep(4)
        print("[hybrid] deviceId 回读:", adb(
            "shell", "logcat -d -b all | grep -m2 -a -e 'Device ID already' -e "
                     "'Generated new device ID'", timeout=120).strip()[:300], flush=True)
        harvest_mmkv_seed()
    # ---- 2) 原签名包 + 移植 MMKV + 手工补 .so ----
    install_pristine()
    push_pristine_libs()
    adb("shell", "am", "start", "-W", "-n", PKG + "/.MainActivity", timeout=180)
    for i in range(6):
        time.sleep(3)
        if app_pid():
            break
    print("[ga] pristine pid (first launch):", app_pid() or "dead", flush=True)
    adb("shell", "am", "force-stop", PKG, timeout=60)
    time.sleep(2)
    plant_mmkv_seed()
    adb("logcat", "-c", timeout=60)
    adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    for i in range(8):
        time.sleep(3)
        if app_pid():
            break
    print("[ga] pristine pid (seeded):", app_pid() or "dead", flush=True)
    print("[ga] deviceId 日志:", adb(
        "shell", "logcat -d -b all | grep -m3 -a -e 'Device ID already' -e "
                 "'Generated new device ID' -e 'Device registration response'", timeout=120
    ).strip()[:600], flush=True)
    if not app_pid():
        save_logcat("ga_seeded")
        return False
    # ---- 3) Play Store (integrity token 需要) ----
    v = extract_vending_from_playstore_image()
    if v:
        print("[ga] disable-verity:", adb("disable-verity", timeout=180).strip()[:120], flush=True)
        adb("reboot", timeout=60)
        time.sleep(15)
        for _ in range(24):
            time.sleep(5)
            if adb("shell", "getprop", "sys.boot_completed", timeout=30).strip() == "1":
                break
        print("[ga] root:", adb("root", timeout=40).strip()[:60], flush=True)
        time.sleep(5)
        print("[ga] remount:", adb("remount", timeout=300).strip().replace("\n", " | ")[:400],
              flush=True)
        adb("shell", "mkdir -p /system/priv-app/Vending", timeout=60)
        print("[ga] push Vending:", adb("push", v, "/system/priv-app/Vending/Vending.apk",
                                        timeout=600)[:150], flush=True)
        adb("shell", "chmod 644 /system/priv-app/Vending/Vending.apk; "
                     "restorecon -R /system/priv-app/Vending", timeout=120)
        adb("reboot", timeout=60)
        time.sleep(20)
        for _ in range(24):
            time.sleep(5)
            if adb("shell", "getprop", "sys.boot_completed", timeout=30).strip() == "1":
                break
        print("[ga] vending:", adb("shell", "pm path com.android.vending",
                                   timeout=60).strip()[:150], flush=True)
        print("[ga] gms:", adb("shell", "pm list packages | grep -i -e gms -e vending",
                               timeout=60).strip()[:300], flush=True)
        plant_mmkv_seed()
    else:
        print("[ga] 无 Play Store, 仅验证 deviceId 移植", flush=True)
    relaunch()
    time.sleep(5)
    flow_full()
    return True


def pristine_test():
    """装厂商原始签名四包，看 App 能否在 x86_64 模拟器(ARM 翻译层)上跑起来，
    并抓 'Generated new device ID' 日志判断 generateRandomString 是否仍返回空串。"""
    print("[pristine] 安装原始签名四包", flush=True)
    print("[pristine]", adb("install-multiple", "-r", "-g", "--abi", "arm64-v8a", "fms.apk",
                            "config.arm64_v8a.apk", "config.en.apk", "config.mdpi.apk",
                            timeout=900)[:400], flush=True)
    for pat in ("primaryCpuAbi", "legacyNativeLibraryDir", "codePath"):
        print(f"[pristine] {pat}:", adb(
            "shell", f"dumpsys package {PKG} | grep -m2 -E '{pat}'", timeout=60).strip()[:200],
            flush=True)
    print("[pristine] lib dir:", adb(
        "shell", "ls -la $(pm path com.nunofacha.fmcs | head -1 | sed 's/package://;s/base.apk//')lib 2>&1",
        timeout=60).strip()[:300], flush=True)
    adb("logcat", "-c", timeout=60)
    print("[pristine] am start -W:", adb("shell", "am", "start", "-W", "-n",
                                        PKG + "/.MainActivity", timeout=180).strip()[:600],
          flush=True)
    alive = ""
    for i in range(8):
        time.sleep(3)
        alive = app_pid()
        print(f"[pristine] pid {i}: {alive or 'dead'}", flush=True)
        if alive:
            break
    lg = adb("shell", "logcat -d -b all | grep -iE 'nunofacha|SoLoader|UnsatisfiedLink|"
                      "AndroidRuntime|FATAL|Generated new device ID|Device ID|libreactnative' "
                      "| tail -40", timeout=120)
    print("[pristine] logcat:\n" + lg[:3000], flush=True)
    print("[pristine] devid log:", adb(
        "shell", "logcat -d -b all | grep -m3 -a 'Generated new device ID' ; "
                      "logcat -d -b all | grep -m3 -a 'Device ID already exists'",
        timeout=120).strip()[:600], flush=True)
    if alive:
        print("[pristine] App 存活 ✓", flush=True)
        return True
    print("[pristine] App 立刻死亡 ✗", flush=True)
    save_logcat("pristine")
    return False


def main():
    os.makedirs(OUT, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else "recon"
    print(f"[main] mode={mode} user={USER} pass={'set' if PASS else 'UNSET'}")
    adb("shell", "settings", "put", "global", "window_animation_scale", "0")
    adb("shell", "settings", "put", "global", "transition_animation_scale", "0")
    adb("shell", "settings", "put", "global", "animator_duration_scale", "0")
    if mode == "recon":
        explore()
        return
    if mode == "ga":
        ga_flow()
        return
    if mode == "vend":
        vend_mode()
        return
    # ==== 步骤 1: 原始签名 APK 能否运行（决定整条模拟器路线是否可行）====
    pristine_ok = pristine_test()
    if pristine_ok and mode != "full":
        print("[main] recon 模式结束", flush=True)
        return
    # ==== 步骤 2: 混编 debuggable 包 + JDWP 注入 deviceId（已验证可用）====
    print("[main] 装混编 debuggable 包 + JDI 注入", flush=True)
    print("[main] uninstall pristine:", adb("uninstall", PKG, timeout=120).strip()[:100], flush=True)
    print("[install]", adb("install-multiple", "-r", "-g", "--abi", "arm64-v8a", "fms.apk",
                            "config.arm64_v8a.apk", "config.en.apk", "config.mdpi.apk",
                            timeout=900)[:300] if False else adb(
        "install-multiple", "-r", "-g", "fms_patched-aligned-debugSigned.apk",
        "cfg_arm64-aligned-debugSigned.apk", "config.en-aligned-debugSigned.apk",
        "config.mdpi-aligned-debugSigned.apk", timeout=900)[:400], flush=True)
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
    if inject_device_id:
        print("[main] 立刻注入 (App 启动期日志密集, 断点易命中)")
        time.sleep(2)
        ok = False
        for round_ in range(2):
            pids = (app_pid() or "").split()
            print(f"[main] 注入轮次 {round_} pids = {pids}", flush=True)
            if not pids:
                print("[main] App 已死, 重新拉起", flush=True)
                relaunch()
                continue
            ok = inject_device_id(adb, pids)
            print("[main] deviceId 注入:", "OK" if ok else "FAILED", flush=True)
            if ok:
                break
            print("[main] 注入失败, 重启 App 重试", flush=True)
            time.sleep(3)
            relaunch()
        time.sleep(3)
        print("[main] pid after inject:", app_pid() or "dead")
        if ok:
            print("[main] 注入成功, 重启 App 清缓存", flush=True)
            relaunch()
            print("[main] 重启后 pid:", app_pid() or "dead")
        if not app_pid():
            relaunch()
    if mode == "full":
        flow_full()
    else:
        flow_recon()


if __name__ == "__main__":
    main()
