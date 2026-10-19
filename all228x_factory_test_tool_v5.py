#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALL228X 出廠測試工具 v5 (ALL228X Factory Test Tool v5)
--------------------------------------
v5 新增（為了打包成 Windows 單一 exe，測試邏輯與 v4 相同）：
  - 打包成 exe 時，結果檔存在 exe 所在資料夾；直接跑 .py 時存在目前目錄（同 v4）
    該資料夾不能寫入時（例如放在 Program Files），改存到使用者的「文件」資料夾
  - Windows 上啟用 DPI awareness，避免 Win11 開縮放時字體模糊
  - 打包方式見 build.bat（PyInstaller --onefile --windowed）

v4 新增：
  - 測試模式多一個「自訂指令」：在 GUI 填一行指令，對每台目標設備執行
  - timeout 預設 90 分鐘，可在 GUI 修改（單位：分鐘）
  - 指令含 sudo 時，會先檢查 sudo -n true（需 NOPASSWD），不通過就略過該台
  - Log 會記錄 exit code、stdout 最後幾行，失敗時附 stderr
  - 結果檔多一欄 rc=<exit code>（不論成敗都會寫入）
  其餘行為與 v3 相同

v3 新增（與既有測試功能分開，只有勾選才會執行）：
  - 更改時區（timedatectl set-timezone）
  - 更改鍵盤佈局（/etc/default/keyboard 的 XKBLAYOUT + dpkg-reconfigure）
  - 改完後可選擇自動重開機（鍵盤佈局在 XFCE 需重開機才生效）
  - 測試模式多一個「不執行測試」，可以只改設定
  設定變更需要設備帳號有 sudo 免密碼（NOPASSWD）權限
  沒勾任何設定變更時，行為與 v2 完全相同

v2 跟第一版不同的地方：
  - 不再自己架 DHCP server，也不做 ARP scan（因為網段中的 switch 也是
    00:0F:C9 開頭，用 ARP+MAC 篩選會誤判成 switch）
  - 改成「手動輸入待測 IP 清單」，從源頭就只給程式測試設備的 IP
  - 因為不用發 raw 封包了，Windows 上不再需要 Npcap、也不需要系統管理員身分
    只要 paramiko (SSH) + tkinter (GUI，Python 內建) 兩個依賴

流程：
  1. 使用者填「Class C 網段（第三段）」+「待測 IP 清單（第四段，支援跳號/範圍）」
  2. 按「解析並預覽」：展開完整 IP 清單給使用者確認，同時偵測本機網卡是否落在
     這個網段內（避免不小心用錯網卡介面）
  3. 按 Start 後，對清單裡每個 IP 平行做 SSH（retry 機制）
  4. SSH 進去後，從設備內部讀 MAC（防呆用，就算清單填錯也不會誤跑 stress）
     開頭是 00:0F:C9 才執行 stress 兩道指令；不是就略過、記一筆 log
  5. 成功跑完 stress 的設備記錄 hostname,mac 到 YY-MM-DD-HHMM.txt
  6. 總數 = 清單解析出的 IP 數量，全部處理完（不論成功/略過/SSH失敗）就自動結束

安全性備註：
  - SSH 帳號密碼只存在記憶體中，程式關閉就消失，不會寫進任何檔案
  - 不會把密碼印到 log 畫面或結果檔案裡
"""

import os
import re
import sys
import socket
import queue
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

try:
    import paramiko
except ImportError:
    print("缺少 paramiko 套件，請先執行：pip install paramiko")
    sys.exit(1)

# ----------------------------------------------------------------------------
# 可調整的設定 (Configuration)
# ----------------------------------------------------------------------------
TARGET_MAC_PREFIX = "00:0f:c9"     # 目標設備 MAC 開頭（小寫比對）

SSH_PORT          = 22
SSH_RETRY_TIMES   = 10             # SSH 連線重試次數
SSH_RETRY_GAP     = 5              # 每次重試間隔秒數
SSH_CONNECT_TIMEOUT = 8            # 單次連線 timeout 秒數
SSH_KEEPALIVE     = 20             # keepalive 間隔秒數，撐住長時間指令不斷線
MAX_PARALLEL_SSH  = 10             # 同時間最多幾條 SSH 平行跑（避免瞬間灌爆網路/CPU）

STRESS_CMD_1 = "stress --cpu 6 --timeout 900s"
STRESS_CMD_2 = "stress --vm 15 --vm-bytes 256M --timeout 900s"
STRESS_TIMEOUT = 20 * 60           # 單道指令的安全上限（秒），避免卡死

ALLHICOM_CMD = "allhicom"
ALLHICOM_TIMEOUT = 90 * 60         # allhicom 安全上限（秒）

# 測試模式清單：key 是內部代號，label 是 GUI 顯示文字，
# commands 是要「依序」執行的指令，timeout 是每道指令各自的安全上限
TEST_MODES = {
    "stress": {
        "label": "stress（stress --cpu 6 -> stress --vm 15）",
        "commands": [STRESS_CMD_1, STRESS_CMD_2],
        "timeout": STRESS_TIMEOUT,
    },
    "allhicom": {
        "label": "allhicom（sudo allhicom）",
        "commands": [ALLHICOM_CMD],
        "timeout": ALLHICOM_TIMEOUT,
    },
    "custom": {
        "label": "自訂指令（填在下方欄位）",
        "commands": [],                 # 執行時由 GUI 輸入框帶入
        "timeout": 0,                   # 執行時由 GUI timeout 欄位帶入
    },
    "none": {
        "label": "不執行測試（只做設定變更）",
        "commands": [],
        "timeout": 0,
    },
}

CUSTOM_DEFAULT_TIMEOUT_MIN = 90    # 自訂指令預設 timeout（分鐘）
CUSTOM_LOG_TAIL_LINES = 20         # 自訂指令 stdout 只記最後幾行到 Log
DEFAULT_MODE = "stress"

# 設定變更（v3 新增）：下拉選單的常用值，也可以手動輸入其他值
TIMEZONE_CHOICES = ["Europe/Berlin", "Asia/Taipei", "UTC", "Europe/London", "America/New_York"]
LAYOUT_CHOICES   = ["de", "us", "cn", "fr", "gb", "jp"]
SETTING_CMD_TIMEOUT = 30
KEYBOARD_APPLY_TIMEOUT = 60        # dpkg-reconfigure 較慢，給長一點

# 只允許英數字與 / _ + -，避免值被組進 shell 指令時造成 injection
TIMEZONE_PATTERN = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$")
LAYOUT_PATTERN   = re.compile(r"^[A-Za-z0-9_\-]+$")

MAC_READ_CMD = "cat /sys/class/net/*/address 2>/dev/null"
HOSTNAME_CMD = "hostname"


# ----------------------------------------------------------------------------
# IP 清單解析 (parse "10, 50-53, 88" -> ["192.168.100.10", ...])
# ----------------------------------------------------------------------------
def parse_ip_list(class_c_text, ip_list_text):
    """
    回傳 (完整IP清單, 錯誤訊息清單)
    class_c_text: 例如 "100"
    ip_list_text: 例如 "10, 50-53, 88, 120-125"
    """
    errors = []

    class_c_text = class_c_text.strip()
    if not class_c_text.isdigit() or not (0 <= int(class_c_text) <= 255):
        errors.append(f"Class C 網段格式錯誤：{class_c_text}（需為 0-255 的數字）")
        return [], errors

    text = ip_list_text.replace("，", ",")  # 容錯：中文逗號
    parts = [p.strip() for p in text.split(",") if p.strip()]

    if not parts:
        errors.append("IP 清單是空的，請至少填一個")
        return [], errors

    nums = set()
    for p in parts:
        if "-" in p:
            seg = [s.strip() for s in p.split("-")]
            if len(seg) != 2 or not all(s.isdigit() for s in seg):
                errors.append(f"範圍格式錯誤：{p}")
                continue
            start, end = int(seg[0]), int(seg[1])
            if start > end:
                errors.append(f"範圍寫反（起點大於終點）：{p}")
                continue
            if start < 1 or end > 254:
                errors.append(f"超出可用範圍 1-254：{p}")
                continue
            nums.update(range(start, end + 1))
        else:
            if not p.isdigit():
                errors.append(f"格式錯誤：{p}")
                continue
            n = int(p)
            if n < 1 or n > 254:
                errors.append(f"超出可用範圍 1-254：{p}")
                continue
            nums.add(n)

    ip_list = [f"192.168.{class_c_text}.{n}" for n in sorted(nums)]
    return ip_list, errors


# ----------------------------------------------------------------------------
# 本機網卡偵測 (只是唯讀提示，不影響實際 SSH 執行)
# ----------------------------------------------------------------------------
def get_local_ips():
    """盡量抓出本機所有網卡的 IPv4 位址，抓不到就回傳空集合（不影響主流程）"""
    ips = set()
    try:
        import psutil
        for _name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ips.add(addr.address)
        return ips
    except ImportError:
        pass  # 沒裝 psutil 就用備用方式，不強制要求安裝

    try:
        hostname = socket.gethostname()
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        ips.update(ip_list)
    except Exception:
        pass
    return ips


def check_local_nic_in_subnet(class_c_text):
    """回傳 (是否偵測到, 符合的IP清單)"""
    class_c_text = class_c_text.strip()
    if not class_c_text.isdigit():
        return False, []
    prefix = f"192.168.{class_c_text}."
    local_ips = get_local_ips()
    matched = [ip for ip in local_ips if ip.startswith(prefix)]
    return (len(matched) > 0), matched


# ----------------------------------------------------------------------------
# 結果檔位置（v5 新增）
# ----------------------------------------------------------------------------
def get_result_dir():
    """
    回傳 (資料夾, 是否為備用位置)
    打包成 exe 時用 exe 所在資料夾；直接跑 .py 時用目前目錄（同 v4）
    實際寫一個測試檔確認可寫入（Windows 上 os.access 不準），不行就改用「文件」資料夾
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.getcwd()
    try:
        probe = os.path.join(base, ".write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return base, False
    except OSError:
        home = os.path.expanduser("~")
        docs = os.path.join(home, "Documents")
        return (docs if os.path.isdir(docs) else home), True


# ----------------------------------------------------------------------------
# 設定變更（v3 新增）：時區 / 鍵盤佈局
# ----------------------------------------------------------------------------
def _run(client, cmd, timeout=SETTING_CMD_TIMEOUT):
    """執行指令並等它跑完，回傳 (exit_code, stdout, stderr)"""
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="ignore")
    err = stderr.read().decode(errors="ignore")
    return code, out, err


def apply_settings(client, settings, log):
    """
    settings: {"timezone": str 或 None, "layout": str 或 None, "reboot": bool}
    先做檢查（sudo 免密碼、設備本機有對應的 zoneinfo / xkb 檔），再依序變更並讀回驗證。
    回傳 (是否成功, 結果檔要附加的欄位字串)
    """
    tz = settings.get("timezone")
    layout = settings.get("layout")

    code, _, _ = _run(client, "sudo -n true")
    if code != 0:
        log("sudo 需要密碼或權限不足，無法變更設定，略過此設備")
        return False, ""

    checks = []
    if tz:
        checks.append(f"test -e /usr/share/zoneinfo/{tz}")
    if layout:
        checks.append(f"test -e /usr/share/X11/xkb/symbols/{layout}")
    code, _, _ = _run(client, " && ".join(checks))
    if code != 0:
        log(f"設備缺少本機資源檔（zoneinfo '{tz}' 或 xkb layout '{layout}'），略過此設備")
        return False, ""

    if tz:
        code, _, err = _run(client, f"sudo -n timedatectl set-timezone {tz}")
        if code != 0:
            log(f"設定時區失敗：{err.strip()}")
            return False, ""
        log(f"時區已設為 {tz}")

    if layout:
        code, _, err = _run(
            client, f"sudo -n sed -i 's/^XKBLAYOUT=.*/XKBLAYOUT=\"{layout}\"/' /etc/default/keyboard"
        )
        if code != 0:
            log(f"修改 /etc/default/keyboard 失敗：{err.strip()}")
            return False, ""
        code, _, err = _run(
            client, "sudo -n dpkg-reconfigure -f noninteractive keyboard-configuration",
            timeout=KEYBOARD_APPLY_TIMEOUT,
        )
        if code != 0:
            log(f"套用鍵盤設定失敗：{err.strip()}")
            return False, ""
        log(f"鍵盤佈局已設為 {layout}")

    # 讀回實際值驗證
    _, out, _ = _run(client, "timedatectl show -p Timezone --value; grep '^XKBLAYOUT=' /etc/default/keyboard")
    lines = out.strip().splitlines()
    actual_tz = lines[0].strip() if len(lines) > 0 else ""
    actual_kb = lines[1].strip() if len(lines) > 1 else ""
    log(f"驗證：時區={actual_tz or '(空)'} | 鍵盤={actual_kb or '(空)'}")

    if tz and actual_tz != tz:
        log(f"驗證失敗：時區應為 {tz}")
        return False, ""
    if layout and actual_kb != f'XKBLAYOUT="{layout}"':
        log(f"驗證失敗：鍵盤應為 XKBLAYOUT=\"{layout}\"")
        return False, ""

    extra = []
    if tz:
        extra.append(f"tz={tz}")
    if layout:
        extra.append(f"kb={layout}")
    return True, ",".join(extra)


# ----------------------------------------------------------------------------
# 單台設備的 SSH 處理邏輯
# ----------------------------------------------------------------------------
def process_one_device(ip, username, password, mode, commands, cmd_timeout, settings, log_queue, result_lock, result_file, counters, counters_lock, total):
    """
    針對一個 IP：SSH 連線(含retry) -> 讀 MAC -> 判斷是否為目標設備
    -> （有勾才做）設定變更 -> 執行測試模式 -> 成功才寫入結果檔
    -> （有勾設定變更且勾重開機才做）重開機
    commands / cmd_timeout 由 on_start 決定（自訂指令模式時來自 GUI 輸入）
    settings 為 None 表示沒勾任何設定變更，流程與 v2 相同
    無論結果如何，最後都會讓 processed 計數 +1（用來判斷整輪是否跑完）
    """
    def log(msg):
        log_queue.put(f"[{ip}] {msg}")

    client = None
    connected = False

    for attempt in range(1, SSH_RETRY_TIMES + 1):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                ip, port=SSH_PORT, username=username, password=password,
                timeout=SSH_CONNECT_TIMEOUT, banner_timeout=SSH_CONNECT_TIMEOUT,
                auth_timeout=SSH_CONNECT_TIMEOUT,
            )
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(SSH_KEEPALIVE)
            connected = True
            break
        except Exception as e:
            log(f"第 {attempt}/{SSH_RETRY_TIMES} 次連線失敗：{e}")
            if client:
                client.close()
            time.sleep(SSH_RETRY_GAP)

    if not connected:
        log("SSH 連線失敗，放棄此設備")
        _finish(counters, counters_lock, log_queue, total)
        return

    try:
        # 讀 MAC（防呆：就算清單裡的 IP 填錯，這邊也會擋掉非目標設備）
        stdin, stdout, stderr = client.exec_command(MAC_READ_CMD, timeout=15)
        mac_output = stdout.read().decode(errors="ignore").strip().lower()
        macs = [m.strip() for m in mac_output.splitlines() if m.strip()]

        is_target = any(m.startswith(TARGET_MAC_PREFIX) for m in macs)

        if not is_target:
            log(f"MAC 不符合 {TARGET_MAC_PREFIX} 開頭，略過。讀到的 MAC：{macs}")
            client.close()
            _finish(counters, counters_lock, log_queue, total)
            return

        matched_mac = next(m for m in macs if m.startswith(TARGET_MAC_PREFIX))
        mode_conf = TEST_MODES[mode]
        log(f"確認為目標設備，MAC={matched_mac}，開始執行模式：{mode_conf['label']}")

        # 取 hostname，記錄檔用
        stdin, stdout, stderr = client.exec_command(HOSTNAME_CMD, timeout=10)
        hostname = stdout.read().decode(errors="ignore").strip() or ip

        # 設定變更（只有勾選才做；失敗就不跑測試、不寫記錄檔）
        extra_fields = ""
        if settings:
            ok, extra_fields = apply_settings(client, settings, log)
            if not ok:
                return

        if mode == "custom":
            # 指令含 sudo 時先確認免密碼，否則沒有 tty 的 sudo 只會失敗
            if re.search(r"\bsudo\b", commands[0]):
                code, _, _ = _run(client, "sudo -n true")
                if code != 0:
                    log("自訂指令含 sudo，但 sudo 需要密碼或權限不足，略過此設備")
                    return
            log(f"執行自訂指令：{commands[0]}（timeout {cmd_timeout // 60} 分鐘）")
            stdin, stdout, stderr = client.exec_command(commands[0], timeout=cmd_timeout)
            # 先讀完輸出再取 exit code，避免輸出太多塞滿 channel 造成卡住
            out = stdout.read().decode(errors="ignore")
            err = stderr.read().decode(errors="ignore")
            exit_status = stdout.channel.recv_exit_status()
            log(f"自訂指令 exit code={exit_status}")
            tail = out.strip().splitlines()[-CUSTOM_LOG_TAIL_LINES:]
            if tail:
                log(f"stdout（最後 {len(tail)} 行）：\n" + "\n".join(tail))
            if exit_status != 0 and err.strip():
                log(f"stderr：{err.strip()}")
            extra_fields = ",".join(f for f in (extra_fields, f"rc={exit_status}") if f)
        else:
            # 依序執行該模式的指令清單
            for i, cmd in enumerate(commands, start=1):
                log(f"執行第 {i} 道指令：{cmd}")
                stdin, stdout, stderr = client.exec_command(cmd, timeout=cmd_timeout)
                exit_status = stdout.channel.recv_exit_status()  # 等指令跑完
                if exit_status != 0:
                    err_msg = stderr.read().decode(errors="ignore").strip()
                    log(f"第 {i} 道指令 exit code={exit_status}，錯誤：{err_msg}")

        log(f"{mode_conf['label']} 全部執行完畢，寫入記錄檔")
        line = f"{hostname},{matched_mac}"
        if extra_fields:
            line += f",{extra_fields}"
        with result_lock:
            with open(result_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")

        if settings and settings.get("reboot"):
            client.exec_command("sudo -n reboot")
            log("已送出重開機指令")

    except Exception as e:
        log(f"執行過程發生錯誤：{e}")
    finally:
        client.close()
        _finish(counters, counters_lock, log_queue, total)


def _finish(counters, counters_lock, log_queue, total):
    with counters_lock:
        counters["processed"] += 1
        done = counters["processed"]
    log_queue.put(f"__PROGRESS__ {done}/{total}")
    if done >= total:
        log_queue.put("__ALL_DONE__")


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class NetTestToolApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ALL228X 出廠測試工具 v5（手動 IP 清單版）")
        self.root.geometry("760x820")

        self.log_queue = queue.Queue()
        self.log_counter = 0
        self.result_lock = threading.Lock()
        self.counters_lock = threading.Lock()
        self.executor = None
        self.stop_flag = threading.Event()
        self.result_file = None

        self._build_widgets()
        self._poll_log_queue()

    def _build_widgets(self):
        pad = {"padx": 8, "pady": 4}

        frame_net = ttk.LabelFrame(self.root, text="網段設定")
        frame_net.pack(fill="x", **pad)

        ttk.Label(frame_net, text="Class C 網段 192.168.").grid(row=0, column=0, sticky="w", **pad)
        self.entry_class_c = ttk.Entry(frame_net, width=8)
        self.entry_class_c.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(frame_net, text="（只填第三段，例如 100）").grid(row=0, column=2, sticky="w", **pad)

        self.nic_status_label = ttk.Label(frame_net, text="本機網卡偵測：尚未偵測", foreground="gray")
        self.nic_status_label.grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        frame_ip = ttk.LabelFrame(self.root, text="待測 IP 清單（第四段，逗號分隔，支援範圍 50-53）")
        frame_ip.pack(fill="x", **pad)
        self.entry_ip_list = ttk.Entry(frame_ip, width=70)
        self.entry_ip_list.grid(row=0, column=0, columnspan=3, sticky="we", **pad)
        ttk.Button(frame_ip, text="解析並預覽", command=self.on_parse_preview).grid(row=1, column=0, sticky="w", **pad)

        self.preview_text = scrolledtext.ScrolledText(frame_ip, height=5, width=80, state="disabled")
        self.preview_text.grid(row=2, column=0, columnspan=3, sticky="we", **pad)

        frame_mode = ttk.LabelFrame(self.root, text="測試模式")
        frame_mode.pack(fill="x", **pad)
        self.mode_var = tk.StringVar(value=DEFAULT_MODE)
        for i, (key, conf) in enumerate(TEST_MODES.items()):
            ttk.Radiobutton(
                frame_mode, text=conf["label"], value=key, variable=self.mode_var
            ).grid(row=i, column=0, sticky="w", padx=8, pady=1)

        # 自訂指令（v4 新增）：只有選「自訂指令」模式時才會用到
        frame_custom = ttk.Frame(frame_mode)
        frame_custom.grid(row=len(TEST_MODES), column=0, sticky="w", padx=24, pady=2)
        ttk.Label(frame_custom, text="指令：").grid(row=0, column=0, sticky="w")
        self.entry_custom_cmd = ttk.Entry(frame_custom, width=56)
        self.entry_custom_cmd.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(frame_custom, text="timeout（分鐘）：").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.entry_custom_timeout = ttk.Entry(frame_custom, width=6)
        self.entry_custom_timeout.insert(0, str(CUSTOM_DEFAULT_TIMEOUT_MIN))
        self.entry_custom_timeout.grid(row=0, column=3, sticky="w", padx=4)

        # 設定變更（v3 新增）：只有勾選才會執行，與測試模式分開
        frame_set = ttk.LabelFrame(self.root, text="設定變更（勾選才執行，需設備 sudo 免密碼）")
        frame_set.pack(fill="x", **pad)
        self.tz_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame_set, text="更改時區", variable=self.tz_enabled).grid(row=0, column=0, sticky="w", **pad)
        self.combo_tz = ttk.Combobox(frame_set, values=TIMEZONE_CHOICES, width=22)
        self.combo_tz.set(TIMEZONE_CHOICES[0])
        self.combo_tz.grid(row=0, column=1, sticky="w", **pad)

        self.kb_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame_set, text="更改鍵盤佈局", variable=self.kb_enabled).grid(row=0, column=2, sticky="w", **pad)
        self.combo_kb = ttk.Combobox(frame_set, values=LAYOUT_CHOICES, width=8)
        self.combo_kb.set(LAYOUT_CHOICES[0])
        self.combo_kb.grid(row=0, column=3, sticky="w", **pad)

        self.reboot_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame_set, text="完成後重開機（鍵盤佈局需重開機才生效；僅在有勾設定變更時作用）",
            variable=self.reboot_enabled,
        ).grid(row=1, column=0, columnspan=4, sticky="w", **pad)

        frame_ssh = ttk.LabelFrame(self.root, text="SSH 帳號密碼（只存在記憶體，不會寫入任何檔案）")
        frame_ssh.pack(fill="x", **pad)
        ttk.Label(frame_ssh, text="帳號：").grid(row=0, column=0, sticky="w", **pad)
        self.entry_user = ttk.Entry(frame_ssh, width=20)
        self.entry_user.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(frame_ssh, text="密碼：").grid(row=0, column=2, sticky="w", **pad)
        self.entry_pass = ttk.Entry(frame_ssh, width=20, show="*")
        self.entry_pass.grid(row=0, column=3, sticky="w", **pad)

        frame_ctrl = ttk.Frame(self.root)
        frame_ctrl.pack(fill="x", **pad)
        self.btn_start = ttk.Button(frame_ctrl, text="Start", command=self.on_start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(frame_ctrl, text="Stop", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.progress_label = ttk.Label(frame_ctrl, text="進度：尚未開始")
        self.progress_label.pack(side="left", padx=12)

        frame_log = ttk.LabelFrame(self.root, text="Log")
        frame_log.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(frame_log, height=16, state="disabled")
        self.log_text.pack(fill="both", expand=True, **pad)

        self._parsed_ips = []

    # ------------------------------------------------------------------
    def on_parse_preview(self):
        class_c = self.entry_class_c.get()
        ip_list_text = self.entry_ip_list.get()
        ips, errors = parse_ip_list(class_c, ip_list_text)

        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")

        if errors:
            self.preview_text.insert("end", "解析發生錯誤：\n" + "\n".join(errors) + "\n")
            self._parsed_ips = []
        else:
            self.preview_text.insert("end", f"已解析出 {len(ips)} 個 IP：\n" + "\n".join(ips))
            self._parsed_ips = ips

        self.preview_text.configure(state="disabled")

        # 順便更新本機網卡偵測
        found, matched = check_local_nic_in_subnet(class_c)
        if found:
            self.nic_status_label.configure(
                text=f"本機網卡偵測：✔ 偵測到本機在 192.168.{class_c}.0/24（{', '.join(matched)}）",
                foreground="green",
            )
        else:
            self.nic_status_label.configure(
                text=f"本機網卡偵測：✘ 沒偵測到本機網卡在 192.168.{class_c}.0/24，請確認網卡設定是否正確",
                foreground="red",
            )

    # ------------------------------------------------------------------
    def on_start(self):
        if not self._parsed_ips:
            messagebox.showwarning("提醒", "請先按「解析並預覽」，確認 IP 清單無誤後再開始")
            return

        username = self.entry_user.get().strip()
        password = self.entry_pass.get()
        if not username or not password:
            messagebox.showwarning("提醒", "請輸入 SSH 帳號密碼")
            return

        settings, err = self._collect_settings()
        if err:
            messagebox.showwarning("提醒", err)
            return

        mode = self.mode_var.get()
        if mode == "none" and not settings:
            messagebox.showwarning("提醒", "測試模式選了「不執行測試」，又沒勾任何設定變更，沒有事情可做")
            return

        commands = TEST_MODES[mode]["commands"]
        cmd_timeout = TEST_MODES[mode]["timeout"]
        if mode == "custom":
            custom_cmd = " ".join(self.entry_custom_cmd.get().split("\n")).strip()
            if not custom_cmd:
                messagebox.showwarning("提醒", "選了「自訂指令」，但指令欄位是空的")
                return
            timeout_text = self.entry_custom_timeout.get().strip() or str(CUSTOM_DEFAULT_TIMEOUT_MIN)
            if not timeout_text.isdigit() or int(timeout_text) <= 0:
                messagebox.showwarning("提醒", f"timeout 格式錯誤：{timeout_text}（需為正整數，單位分鐘）")
                return
            commands = [custom_cmd]
            cmd_timeout = int(timeout_text) * 60
            if not messagebox.askyesno(
                "確認自訂指令",
                f"將對 {len(self._parsed_ips)} 個 IP 中的目標設備執行：\n\n{custom_cmd}\n\n"
                f"timeout：{int(timeout_text)} 分鐘\n\n確定要繼續嗎？",
            ):
                return

        if settings:
            items = []
            if settings["timezone"]:
                items.append(f"時區 → {settings['timezone']}")
            if settings["layout"]:
                items.append(f"鍵盤 → {settings['layout']}")
            if settings["reboot"]:
                items.append("完成後重開機")
            if not messagebox.askyesno(
                "確認設定變更",
                f"將對 {len(self._parsed_ips)} 個 IP 中的目標設備做以下變更：\n\n"
                + "\n".join(items) + "\n\n確定要繼續嗎？",
            ):
                return

        found, _ = check_local_nic_in_subnet(self.entry_class_c.get())
        if not found:
            if not messagebox.askyesno("警告", "沒偵測到本機網卡在這個網段內，確定要繼續嗎？"):
                return

        timestamp = datetime.now().strftime("%y-%m-%d-%H%M")
        result_dir, is_fallback = get_result_dir()
        self.result_file = os.path.join(result_dir, f"{timestamp}.txt")

        self.stop_flag.clear()
        self.log_counter = 0
        self._clear_log()
        self._append_log(
            f"開始測試，共 {len(self._parsed_ips)} 個目標，"
            f"模式：{TEST_MODES[mode]['label']}，結果檔：{self.result_file}"
        )
        if is_fallback:
            self._append_log("注意：程式所在資料夾無法寫入，結果檔改存到上面的路徑")
        if mode == "custom":
            self._append_log(f"自訂指令：{commands[0]}，timeout {cmd_timeout // 60} 分鐘")
        if settings:
            self._append_log(
                f"設定變更：時區={settings['timezone'] or '不變'}，"
                f"鍵盤={settings['layout'] or '不變'}，重開機={'是' if settings['reboot'] else '否'}"
            )

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress_label.configure(text=f"進度：0/{len(self._parsed_ips)}")

        counters = {"processed": 0}
        total = len(self._parsed_ips)

        self.executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_SSH)
        for ip in self._parsed_ips:
            self.executor.submit(
                process_one_device, ip, username, password, mode, commands, cmd_timeout, settings,
                self.log_queue, self.result_lock, self.result_file,
                counters, self.counters_lock, total,
            )

    def _collect_settings(self):
        """回傳 (settings 或 None, 錯誤訊息)；沒勾任何設定變更時 settings 為 None"""
        tz = self.combo_tz.get().strip() if self.tz_enabled.get() else None
        layout = self.combo_kb.get().strip() if self.kb_enabled.get() else None
        if not tz and not layout:
            if self.tz_enabled.get() or self.kb_enabled.get():
                return None, "已勾選設定變更，但目標值是空的"
            return None, None
        if tz and not TIMEZONE_PATTERN.match(tz):
            return None, f"時區格式錯誤：{tz}（例如 Europe/Berlin）"
        if layout and not LAYOUT_PATTERN.match(layout):
            return None, f"鍵盤佈局格式錯誤：{layout}（例如 de）"
        return {"timezone": tz, "layout": layout, "reboot": self.reboot_enabled.get()}, None

    def on_stop(self):
        # 註：因為是逐指令同步等待完成，Stop 目前只能防止「還沒送出」的工作繼續排隊，
        # 已經在跑 stress 的 SSH session 不會被強制中斷（避免留下設備處於未知狀態）。
        self.stop_flag.set()
        if self.executor:
            self.executor.shutdown(wait=False, cancel_futures=True)
        self._append_log("已送出停止要求（正在執行中的 stress 不會被強制中斷）")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    # ------------------------------------------------------------------
    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _append_log(self, msg):
        self.log_counter += 1
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"#{self.log_counter:03d} {msg}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg.startswith("__PROGRESS__"):
                    progress = msg.split(" ", 1)[1]
                    self.progress_label.configure(text=f"進度：{progress}")
                elif msg == "__ALL_DONE__":
                    self._append_log("全部設備處理完畢")
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                else:
                    self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_log_queue)


def enable_windows_dpi_awareness():
    """Win11 開縮放時 tkinter 字體會糊，這裡告訴 Windows 程式自己處理 DPI；失敗就略過"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def main():
    enable_windows_dpi_awareness()
    root = tk.Tk()
    app = NetTestToolApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
