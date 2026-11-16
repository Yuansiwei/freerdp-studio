# dist — 實際在跑的執行檔

這個目錄放的是**真正部署在 `C:\Studio\RdpBridge` 的那一份 runtime**，逐位元組複製進 git，讓「線上跑的是哪一版」可以被追蹤、被比對、被還原。

## 為什麼要把 binary 放進 git

FreeRDP 客製版的版本資訊不足以識別身分：

- `ProductVersion` 只有 `3.33.1.0`，所有 3.33.1 之後的 commit 都一樣
- `FileVersion` 是 `3,33,0,1`，跟 ProductVersion 不一致（nightly 產物）
- DLL 裡**沒有嵌 git describe 或 commit id**

結果就是 docs 寫的版本（`9f08c28`）跟 repo 裡的任何 ref 都對不上。把 binary 本身納入版控，至少能保證：**任何一個 commit 都能還原出當時跑的那一份確切檔案**。

## 內容

`rdpbridge-3.33.1-20261116/` — 2026-11-16 23:1x 從 `C:\Studio\RdpBridge` 擷取：

| 檔案 | 大小 | 說明 |
|---|---|---|
| `wfreerdp.exe` | 108 KB | 外殼，只負責載入插件 |
| `wfreerdp-client3.dll` | 201 KB | **客製改動全在這**（對話框、浮條、貼上、注入、截圖） |
| `freerdp-client3.dll` | 655 KB | FreeRDP common client |
| `freerdp3.dll` / `winpr3.dll` | 2.3 MB / 886 KB | FreeRDP 核心與 runtime |
| `libcrypto-3-x64.dll` / `libssl-3-x64.dll` | 5.3 MB / 872 KB | OpenSSL 3.6.2 |
| `SendRdpInput.exe` | 12 KB | 後台輸入／截圖工具（走 WM_COPYDATA） |
| `KeepaliveTimerProbe.exe` | 5 KB | keepalive 探針 |
| `使用说明.md` | 10 KB | 部署當下的操作說明 |

`MANIFEST.json` 記錄每個檔案的 SHA256、大小、版本資源，以及擷取當下的 repo HEAD。

**不含**：`keepalive-observation.jsonl`、`timer-probe.log`（執行期產物，不是版本內容）。

## 版本註記

> ⚠️ `MANIFEST.json` 裡的 `sourceHead` 是**擷取當下**的分支 HEAD，不是「編出這顆 DLL 的 commit」。
> 真正的來源 commit 目前無法證明（見 [`../_versioning/README.md`](../_versioning/README.md) 的 P1）。
> 反過來說，這個 bundle 本身就是目前唯一的權威來源：要以它為準去對 commit，而不是反過來。

## 如何更新

部署新版之後：

```powershell
# 1. 重新掃描並凍結雜湊
.\_versioning\tools\inventory.ps1 -Snapshot

# 2. 新增一個 bundle（不要改舊的！舊的對應舊版本）
$new = 'dist\rdpbridge-3.33.1-YYYYMMDD'
New-Item -ItemType Directory -Force $new
Copy-Item 'C:\Studio\RdpBridge\*.dll','C:\Studio\RdpBridge\*.exe' $new
# 產生 MANIFEST.json 後 commit
```

**舊 bundle 一律保留**，不要就地覆蓋 —— 那會讓歷史失去意義。

## 驗證

```powershell
# 部署目錄是否等於 git 裡這一份
Get-FileHash C:\Studio\RdpBridge\wfreerdp-client3.dll
Get-FileHash .\dist\rdpbridge-3.33.1-20261116\wfreerdp-client3.dll

# 或直接用工具
.\_versioning\tools\verify.ps1
```
