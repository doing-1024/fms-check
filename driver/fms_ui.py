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


try:
    from mmkv_inject import inject_device_id, wait_mmkv, DEV_ID
except Exception as _e:  # pragma: no cover
    print("[warn] mmkv_inject 不可用:", _e)
    inject_device_id = None
    DEV_ID = ""

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
    btn = find(nodes("login_ready"), "sign in")
    if btn:
        tap(btn)
        print("[login] 已提交登录")
    else:
        print("[login] 未找到 Sign In")
        return False
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
    # ==== 关键: 用 JDWP 让 App 自己的 MMKV 写入 deviceId ====
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
            # 注入过程中 App 可能被杀/崩溃, 重新拉起再试
            print("[main] 注入失败, 重启 App 重试", flush=True)
            time.sleep(3)
            relaunch()
        time.sleep(3)
        print("[main] pid after inject:", app_pid() or "dead")
        if ok:
            # 重启一次 App: 让 C++ 侧 index_cache 与 MMKV 内存状态从磁盘重建
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
