# rdp-reader：每秒截图 + OCR 读取工作流

对正在运行的 studio FreeRDP 客户端（`wfreerdp.exe`）每秒截一帧，降采样后做变化检测，
有变化才送 OCR，结果写成 JSONL 和「最新结果」文件，供脚本或 agent 读取。

```
wfreerdp 主画面缓冲 ──命名管道──▶ 降采样(默认 1/2) ──▶ 变化检测 ──▶ PP-OCRv6 small（默认：准，带坐标，GPU）
   (WM_COPYDATA --capture)          3840×2160→1920×1080      │        [可选] PaddleOCR-VL / Windows OCR
                                                             └──▶ events.jsonl / latest.* / frames/
```

## 原理

- 截图复用客户端现有的 `--capture` 接口（WM_COPYDATA kind `0x46524435`，见
  `client/Windows/studio_input.h` 的 `studio_capture_primary()`）。客户端用 `fopen(path, "wb")`
  写 BMP；我们给它的路径是一次性的**命名管道** `\\.\pipe\studio-rdpcap-<pid>-<随机>`，
  于是整帧直接流进本进程内存：**不落盘、不用重编客户端、不打断当前会话**。
- 原生分辨率的帧只在内存里停留约 20 ms，随即用 `INTER_AREA` 降采样（`--scale`，默认 0.5）。
  之后的变化检测、存档、OCR 全部基于降采样后的帧。
- 变化检测：480 宽灰度缩略图与「上一次送 OCR 的帧」比较。光标闪烁、转圈图标这类小变化
  不触发 OCR；缓慢累积的变化最终会触发。
- OCR 采用 latest-wins：引擎忙时只保留最新一帧，旧帧直接丢弃，不排队。`--engine` 可写多个，用逗号分隔：
  - `ppocr`（默认）：PP-OCRv6 small 的官方 ONNX 模型（`C:\Studio\vlm\ppocrv6`），借用 RapidOCR 的前后处理。
    输出逐行文本 + 方框，**方框是远端会话像素**，可直接用于 `SendRdpInput --click X,Y`。详见下文「GPU」。
  - `vl`：本机 llama-server 上的 PaddleOCR-VL（`127.0.0.1:8080`，提示词 `OCR:`），没有坐标。
    偶尔会陷入重复输出（如「點點點…」直到 token 上限），此时会截掉重复的尾巴并标记 `repetition_cut`。
  - `win`：Windows.Media.Ocr，常驻隐藏 PowerShell 进程（`CREATE_NO_WINDOW`，不会闪窗）。很快，但错字多。

## 为什么默认用 PP-OCRv6 small（2026-11-18 实测）

27 张合成页（3 种背景 × 正黑/雅黑/宋体 × 12/14/16 px，先按 4K@200% 渲染再降采样一半，答案精确已知），
加 4 张真实画面（答案由人工对照原图读出）。字符错误率 = 编辑距离 / 字符数：

| 引擎 | 合成页错误率 | 真实画面错误率 | 1920×1080 一帧耗时 |
|---|---|---|---|
| **PP-OCRv6 small** | **1.07%** | **0.00–0.86%** | GPU 0.6 s / CPU 1.1–1.3 s |
| PP-OCRv6 medium | 0.94% | 0.07–0.88% | CPU 约 3× small |
| PP-OCRv4 mobile（RapidOCR 自带） | 4.12%（繁中 12.8%） | 0.81–2.65% | CPU 1.2–1.8 s |
| PaddleOCR-VL-1.6 | 8.92%（4/27 页陷入重复输出） | 1.18–3.46% | 2.2–11.8 s |
| Windows OCR | 18–21% | 13–30% | 0.15–0.5 s |

## GPU

onnxruntime 的 CUDA 后端在**同一个 session 的输入尺寸和上一次不同**时，要多付约 750 ms
（同一尺寸重复跑每批只要约 30 ms；在这台 Hyper-V GPU-P 虚拟机上实测）。RapidOCR 把文字行
6 行一批、宽度取这批最长的一行，几乎每批尺寸都不同，所以直接开 GPU 反而比 CPU 慢（3.1 s）。做法：

- 检测模型直接放 GPU：每帧尺寸固定，形状不会变。
- 识别模型按宽度分 8 档（320/480/640/960/1280/1920/2560/3840），**每档一个 session**，
  每批补零到 `(6, 3, 48, 档宽)`，输出再切回原宽度。超出范围的批次退回 CPU。
- CUDA 12 / cuDNN 9 的 DLL 借用 torch 自带的（`ort.preload_dlls(directory=torch\lib)`，**不 import torch**）；
  也可用环境变量 `RDP_READER_CUDA_DLLS` 指定目录。
- 不加载 NVRTC：加载后 cuDNN 会即时编译算法，启动从约 5 s 变成约 13 s，每帧却不更快。
  不加载时 cuDNN 遇到新尺寸会往 stderr 打印 `Could not locate nvrtc64_120_0.dll`，
  这些只发生在预热和换画面尺寸时，已用 `quiet_stderr()` 屏蔽。
- `--ppocr-device auto`（默认）在 GPU 不可用时自动退回 CPU，原因写进 `start` 事件的 `engines`。

## 用法

```powershell
$py = 'C:\Users\Studio\AppData\Local\Programs\Python\Python312\python.exe'
cd C:\Users\Studio\Documents\deepseek-harness\default-workspace\freerdp-studio\studio-tools\rdp-reader

# 持续读取（Ctrl+C 停止），默认 PP-OCRv6 small，GPU 可用就用 GPU
& $py rdp_reader.py --target 172.30.251.1:3390

# 立即读一次，打印 JSON 后退出
& $py rdp_reader.py --once

# 同时跑 PP-OCRv6 和 PaddleOCR-VL；或强制只用 CPU
& $py rdp_reader.py --engine ppocr,vl
& $py rdp_reader.py --ppocr-device cpu
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interval` | 1.0 | 截图间隔（秒） |
| `--scale` | 0.5 | 截图后立即降采样的倍率（0.05–1.0）。实测 1.0/0.5/0.4 的识别准确度相同，0.33 开始掉字 |
| `--engine` | ppocr | 逗号分隔：`ppocr`、`vl`、`win`；`none` 只截图；旧写法 `both` = `win,vl` |
| `--ppocr-device` | auto | `auto` / `gpu`（不可用就报错）/ `cpu` |
| `--ppocr-size` | small | `small` / `medium`（medium 没更准，慢 3 倍） |
| `--ppocr-threads` | 4 | CPU 线程数（2–4 个和 12 个一样快） |
| `--min-change` | 0.001 | 缩略图中变化像素占比超过此值才算变化 |
| `--out` | `C:\Studio\rdpread\<时间戳>` | 输出目录 |
| `--keep` / `--no-frames` | 300 | 只保留最新 N 张变化帧 JPEG / 不存帧 |
| `--duration` / `--max-frames` | 0 | 运行时长 / 截图次数上限（0 = 不限） |
| `--vl-inflight` | 2 | 同时进行的 PaddleOCR-VL 请求数（服务端有 8 个 slot） |
| `--win-lang` | zh-Hans-CN | 也可 `en-US`、`zh-Hant-TW` |

## 输出

| 文件 | 内容 |
|---|---|
| `events.jsonl` | 追加写入，每行一条：`start`（含各引擎实际用的 `device`）/ `frame`（变化帧：seq、bbox、变化率、截图耗时）/ `ocr` / `ocr_error` / `capture_error` / `idle`（每 30 个无变化 tick 一条）/ `stop`（统计） |
| `latest.json` | 每个引擎最新一条 `ocr` 记录（原子替换） |
| `latest_ppocr.txt` / `latest_vl.txt` / `latest_win.txt` | 最新纯文本（原子替换） |
| `frames/000123.jpg` | 变化帧（降采样后，约 0.2 MB/张） |

`ocr` 记录里有 `seq`、`frame_ts`、`ocr_ms`、`latency_ms`（从截图到出结果）。
`ppocr` 另有 `lines: [{text, score, box:[x,y,w,h]}]` 和 `device`；`win` 另有 `lines`；
`vl` 另有 token 数、`truncated`、`repetition_cut`。

## 实测（2026-11-18，远端 3840×2160）

| 项目 | 数值 |
|---|---|
| 管道截图（含 31.6 MB 传输） | 22–35 ms，中位数 23.5–25 ms |
| 降采样到 1920×1080 | 3–10 ms |
| 对照：旧 `SendRdpInput --capture` 写 BMP | 约 100 ms，每次写盘 31.6 MB |
| PP-OCRv6 small GPU 启动（含 8 档预热） | 约 5 s |
| PP-OCRv6 small 每帧（1920×1080） | GPU 中位数 0.6–0.7 s；CPU 1.1–1.3 s |
| 20 秒实跑（默认设置） | 20 次截图，0 错误；静止画面只触发 1 次 OCR（GPU 0.79 s，62 行） |

## 注意

- 需要 2026-11-19 之后的 `wfreerdp-client3.dll`。旧版把截图也当成输入，每秒截图会让 F15 保活永远不触发，
  远端约 4 分钟就锁屏。新版只认真正的输入（文字、按键、滚轮、指针），保活间隔改为 60 秒。
- 客户端每截一次会在 `%TEMP%\studio-capture.log` 追加两行（按 1 帧/秒约 13 MB/天），日志不会自动截断。
- 截到的是客户端缓冲：远端锁屏或安全桌面时，截到的是客户端最后收到的画面。
- 窗口按标题（主机名）匹配，并且只认 `wfreerdp.exe`；同一主机只能开一个客户端实例。
- GPU 与 llama-server（PaddleOCR-VL）共用显卡。远端改分辨率后的第一帧会慢约 1 s（新的检测尺寸）。
- PP-OCRv6 的弱点：浅灰色小字（例如回复底部的「Build · DeepSeek-V4-Flash · 17s」）可能读不到。

## 测试

```powershell
.\run-tests.ps1     # 44 项，约 11 s（预算 10 分钟）
```

测试内置一个模拟 `wfreerdp` 的隐藏窗口（同样响应 WM_COPYDATA），覆盖管道协议和各种失败路径：
拒绝、谎报成功、流截断、非 BMP、错误进程、找不到窗口。还覆盖变化检测（含无变化、光标级变化的负对照）、
PP-OCRv6（CPU/GPU 结果一致、方框为原生坐标、空白帧、GPU 不可用时自动退回 CPU、超宽批次退回 CPU、
缺模型报错）、Windows OCR、VL 重复输出截断（含正常文本不被误截的负对照）、引擎列表解析和读取循环。
有真实客户端窗口、8080 服务在线或 GPU 可用时，会自动加跑对应的测试。
