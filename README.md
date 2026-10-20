# ALL228X 出廠測試工具

用 GUI 對指定網段內的設備批次 SSH 連線，確認是目標設備後執行測試指令或變更設定，並把結果記錄到檔案。

- **v4**：Python 腳本，需安裝 Python 與 `paramiko`
- **v5**：功能與 v4 相同，可在 Windows 上打包成單一 exe，目標電腦不需安裝任何東西

📊 [工具介紹簡報](https://yiing1208.github.io/all228x_factory_test_tool/)

## 下載

Windows 10／11（x64）直接下載 exe 即可使用，不需安裝 Python：
👉 [最新版 all228x_factory_test_tool_v5.exe](https://github.com/yiing1208/all228x_factory_test_tool/releases/latest)

第一次執行若跳 SmartScreen：點「其他資訊」→「仍要執行」。

## 功能

### 待測 IP 清單
- 填「Class C 網段」（第三段）與「待測 IP 清單」（第四段），前兩段固定為 `192.168`
- 清單支援跳號與範圍，例如：`10, 50-53, 88, 120-125`
- 按「解析並預覽」展開完整 IP 清單，並偵測本機是否有網卡落在該網段（避免用錯網卡）

### MAC 防呆
SSH 進設備後讀取 MAC（`/sys/class/net/*/address`），開頭是 `00:0F:C9` 才視為目標設備，其餘一律略過。

### 測試模式（擇一）

| 模式 | 執行內容 | 單道指令 timeout |
|---|---|---|
| stress | `stress --cpu 6 --timeout 900s` → `stress --vm 15 --vm-bytes 256M --timeout 900s` | 20 分鐘 |
| allhicom | `allhicom` | 90 分鐘 |
| 自訂指令 | GUI 填入的一行指令（多道請用 `&&` 或 `;` 串接） | 預設 90 分鐘，可在 GUI 修改 |
| 不執行測試 | 只做設定變更 | — |

自訂指令：
- 開始前會跳確認視窗，顯示完整指令、timeout 與 IP 數量
- 指令含 `sudo` 時，會先檢查 `sudo -n true`，不是免密碼就略過該台
- Log 記錄 exit code、stdout 最後 20 行，失敗時附 stderr

### 設定變更（勾選才執行）
- 更改時區（`timedatectl set-timezone`）
- 更改鍵盤佈局（`/etc/default/keyboard` 的 `XKBLAYOUT` + `dpkg-reconfigure`）
- 完成後重開機（鍵盤佈局在 XFCE 需重開機才生效）

每台流程：讀 MAC 防呆 → 設定變更 → 測試模式 → 寫結果檔 → 重開機。
設定變更前會檢查 sudo 免密碼與設備本機的 zoneinfo／xkb 檔，改完讀回驗證；任一步失敗就停止該台、不跑測試、不寫結果檔。

### 結果檔
每完成一台即時寫入 `YY-MM-DD-HHMM.txt`，格式：

```
hostname,mac                         # stress / allhicom
hostname,mac,tz=...,kb=...           # 有做設定變更時
hostname,mac,rc=<exit code>          # 自訂指令（不論成敗都寫入）
```

- v4：存在目前目錄
- v5：exe 執行時存在 exe 所在資料夾；該資料夾無法寫入時改存到使用者「文件」資料夾，Log 會註明

## 使用方式

### v4（直接執行 Python）

```
pip install paramiko
python all228x_factory_test_tool_v4.py
```

`psutil` 為選用（裝了網卡偵測較準確，沒裝會用備用方式）。

### v5（Windows 單一 exe）

打包必須在 Windows 上進行，只有打包機需要安裝 [Python](https://www.python.org/downloads/windows/)：

1. 把 `all228x_factory_test_tool_v5.py` 與 `build.bat` 放在同一資料夾
2. 點兩下 `build.bat`：會建立專用 venv（`.venv-build`），安裝固定版本的 `pyinstaller`、`paramiko`，再打包
3. 產出 `dist\all228x_factory_test_tool_v5.exe`

搬到其他電腦只需複製這個 exe：
- 適用 64 位元 Windows 10／11，建議放在可寫入的資料夾（例如桌面）
- 未做程式碼簽章，第一次執行可能出現 SmartScreen 警告（「其他資訊」→「仍要執行」），防毒軟體偶有誤判
- 啟動時會先解壓到暫存資料夾，需等待約 2～5 秒

## 需求與限制

- 設備帳號需有 sudo 免密碼（NOPASSWD）權限，才能使用設定變更與含 `sudo` 的自訂指令
- 最多同時 10 條 SSH 平行執行；連線失敗會重試 10 次、每次間隔 5 秒
- Stop 只會停止尚未開始的設備，已在執行中的指令不會被強制中斷
- timeout 實際是「多久沒有輸出就中斷」，不是總執行時間上限

## 安全性

- SSH 帳號密碼只存在執行期記憶體，不會寫入任何檔案或 Log
- 時區／鍵盤值只允許英數字與 `/ _ + -`，避免 shell injection
- 自訂指令不做過濾（用途就是執行任意指令），以確認視窗把關
