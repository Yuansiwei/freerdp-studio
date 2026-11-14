# 专用 RDP 客户端（基于 FreeRDP 3.33.1，wfreerdp）

版本：`studio-1.0`（2026-11-15 固化）

目录：`C:\Users\13611\Documents\电脑管理\freerdp-98184`
上游源码在 `upstream\`（分支 `studio-rdp-client` 上有本客户端的全部改动），构建产物在 `build\`，运行用的 exe/dll 在 `runtime\`。

## 启动

```powershell
.\Start-FreeRdp.ps1 -Server 192.168.98.184          # 默认端口 3389
.\Start-FreeRdp.ps1 -Server 192.168.98.184 -Port 3390 -Username Verisilicon\cn1842
.\Start-FreeRdp.ps1 -Server 192.168.98.184 -ScaleDesktop 150   # 手动指定会话缩放
```

- 凭据：首次登录勾选“保存密码”后写入 Windows 凭据管理器，键名 `TERMSRV/<host>` 或 `TERMSRV/<host>:<port>`，下次自动读取。
- 证书：`/cert:ignore` 直连，不再弹证书窗口（仅建议在可信内网使用）。
- 偶发的 `ACTIVATION_TIMEOUT`（上一会话尚未释放）会自动重试，默认最多 3 次。

## 全屏与多显示器

- 会话锁定到**启动时鼠标所在的那块显示器**，尺寸取该显示器的物理分辨率。
- 会话缩放取该显示器的 DPI（主屏 3840×2160@225%，副屏 2560×1440@150%），因此远端界面大小与本机一致。
- 启用动态分辨率，窗口尺寸变化会同步给远端。

## 顶部连接条（对标 mstsc）

全屏时顶部居中显示连接条：左侧图钉（固定/自动隐藏）、标题（仅显示主机名或 IP）、最小化、还原、关闭。

- 尺寸跟随**所在显示器**的缩放：150% 屏 864×40，225% 屏 1296×60，文字与按钮同步放大。
- 窗口尺寸变化、全屏切换、跨屏吸附后都会重新布局并居中。
- 操作行为（与 mstsc 一致）：

| 操作 | 行为 |
|---|---|
| 左右拖动条身 | 移动控制条本身，在屏内左右滑动 |
| 向下拖过约 3 个条高 | 退出全屏，窗口随鼠标移动直到松手落位 |
| 单击 | 不动作 |
| 双击 | 退出全屏 |
| 图钉 / 最小化 / 还原 / 关闭 | 分别对应固定、最小化、还原窗口、断开 |

- 窗口模式下自动启用 SmartSizing（画面等比缩放到窗口，不留黑边）；全屏恢复 1:1，保持清晰。

## 空闲保活（F15）

连续 **2 分钟没有任何输入**（键盘、鼠标、注入的输入都算）才发送一次 F15；一旦有输入立即重新计时，重复空闲则每 2 分钟发一次。
查询已发送次数：`SendRdpInput.exe --target 192.168.98.184:3389 --keepalive 1`。

## 无焦点输入（自动化用）

`SendRdpInput.exe` 通过 `WM_COPYDATA` 把输入投递给客户端，**不需要远端窗口有焦点**：

```powershell
.\SendRdpInput.exe --target 192.168.98.184:3389 --status query      # 连接状态
.\SendRdpInput.exe --target 192.168.98.184:3389 --text "hello"      # 输入文本
.\SendRdpInput.exe --target 192.168.98.184:3389 --key CTRL+A        # 组合键（F1-F24、方向键等）
.\SendRdpInput.exe --target 192.168.98.184:3389 --keepalive 1       # 保活计数
```

窗口标题即匹配依据（只含主机名/IP），所以同一主机不要同时开多个客户端实例。

## 构建

```powershell
.\Build.ps1     # 配置 + 编译 wfreerdp + 拷到 runtime + 编译 SendRdpInput.exe
```

依赖：`C:\tools\vcpkg\installed\x64-windows` 的 OpenSSL、VS2022 BuildTools。
仅重建客户端：`cmake --build build --config Release --target wfreerdp --parallel 4`，再把 `build\**\Release\*.dll` 与 `wfreerdp.exe` 拷进 `runtime\`。

## 已知限制

- **会话 DPI 在会话建立/重激活时确定，之后改不了**（已用 mstsc 交叉验证）：
  - mstsc 全屏到 225% 的显示器后，远端界面确实是 225%（文字明显变大）；
  - 把 mstsc 窗口从 225% 屏拖到 150% 屏，远端界面**完全不变**（两帧文字同尺寸）。
  所以「跨屏自动改缩放」mstsc 也不做；能做的是在建立会话时把 DPI 定对。
- 本项目客户端会把所在显示器的缩放（如 225%）写进 CS_CORE 并在连接时下发，并**继承**会话已有的 DPI：
  实测在同一会话里，mstsc 全屏把 DPI 设为 225% 后，我们的客户端连上去界面尺寸与 mstsc 完全一致。
- 若远端会话当前是 100%（例如很久以前建立、或 VM 刚重装），客户端无法在会话中途改它；
  需要让会话以目标 DPI 重新建立（注销一次远端会话，或重启该 VM）。
- **多屏会话（`+multimon`）为实验特性**：已枚举所有显示器、每屏各自缩放因子、会话桌面 6400×2160 协商正确，
  但实际只绘制主屏区域，副屏区域是黑的，未完成。默认单屏全屏模式不受影响。
- 客户端依赖 `+toggle-fullscreen` 才能显示最小化/还原按钮。
- 排障开关：环境变量 `STUDIO_LAYOUT_DEBUG=1` 输出窗口/显示器/缩放布局日志；缩放变化会以
  `session scale changed` 记录在 `client.stderr.log`。
