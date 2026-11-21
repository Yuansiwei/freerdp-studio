# rdp-reader：每秒截图 + OCR 读取工作流

对正在运行的 studio FreeRDP 客户端（`wfreerdp.exe`）每秒截一帧，降采样后做变化检测，
有变化才送 OCR，结果写成 JSONL 和「最新结果」文件，供脚本或 agent 读取。

```
wfreerdp 主画面缓冲 ──命名管道──▶ 降采样(约1920宽) ──▶ 变化检测 ──▶ PP-OCRv6 small（默认：准，带坐标，GPU）
   (WM_COPYDATA --capture)          3840×2160→1920×1080      │        [可选] PaddleOCR-VL / Windows OCR
                                                             └──▶ events.jsonl / latest.* / frames/
```

## 原理

- 截图复用客户端现有的 `--capture` 接口（WM_COPYDATA kind `0x46524435`，见
  `client/Windows/studio_input.h` 的 `studio_capture_primary()`）。客户端用 `fopen(path, "wb")`
  写 BMP；我们给它的路径是一次性的**命名管道** `\\.\pipe\studio-rdpcap-<pid>-<随机>`，
  于是整帧直接流进本进程内存：**不落盘、不用重编客户端、不打断当前会话**。
- 原生分辨率的帧只在内存里停留约 20 ms，随即用 `INTER_AREA` 降采样（`--scale`，默认 `auto`：缩到约 1920 宽，4K→0.5、2560→0.75，从不放大）。
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
| `--scale` | auto | 截图后立即降采样：`auto` 缩到约 1920 宽，或给 0.05–1.0 的倍率。实测 1920/1536 宽与原生 4K 一样准，约 1270 宽开始掉中文字（2560 屏减半时核对输入就失败过） |
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

## 与远端 OpenCode 端到端交互（`rdp_chat.py`）

```powershell
& $py rdp_chat.py --session "E2E-OK-4821" --text "只回复一行：OK"   # 切到指定会话再发
& $py rdp_chat.py --new-session --text "..."                         # 新建会话再发
& $py rdp_chat.py --session "..." --text "..." --dry-run              # 只定位，不点击不输入
```

流程：OCR 找到空输入框 → 点击 → `SendRdpInput --text` → OCR 确认文字确实进了输入框 → 回车
→ 边流式输出边读（见下）；画面先变化、再连续 `--settle` 秒（默认 8）不变即视为回复结束
→ 输出「我们的提问之后、输入框之前」的文字。结果存为 `chat.json` + `final.jpg`。

**边流式边读（默认，长回复不用往上翻）**：OpenCode 输出时视图固定在底部，回复的每一段都会从屏幕上经过。

- 截图线程连续截图，每次间隔 `--poll` 秒（默认 0.02 s，单次截图 12–14 ms，实测 26–28 帧/秒）。
  每个有变化的帧，只把对话区以 PNG 无损压缩存进队列（约 46 KB/帧，未压缩为 3.5 MB）。
- 主线程按顺序 OCR 队列（只读对话区，约 0.19 s/帧），拼接出完整回复，并自适应跳帧：
  - 接得上，下次就跳远一倍（最多 16 帧）；接不上，就退回读中间的帧。
  - 只有连相邻两帧都接不上，才记为断档。
- OCR 比输出慢也不会漏，因为每一步都还在队列里；读完的帧马上释放。
- 拼接的位移必须得到重叠区多数行的认可：只靠「}」这类重复行投出的票不算数。
- 结果的 `follow` 给出 `snapshots`、`ocr_frames`、`gaps`、`max_queue`、`left_in_queue`、`ocr_ms_median`。
- 有断档时才退回往上滚（下一段）。`--no-follow` 关闭边读。

安全约束（都有负对照测试）：

- `--session`：先点侧栏，再确认**顶部标题**就是该会话才输入，否则拒绝；不会把消息打进别的对话。
- 输入框不是空的（可能是用户的草稿）就拒绝输入；输入后看不到自己的文字就**不按回车**。
- 文字核对用容错比对（OCR 错一两个字仍能确认），完全不同的内容不会被当成一致。
- 长提问在气泡里折行，折出来的尾巴（如「或指令。」）不会被算进回复。

后备（边读出现断档时）：回复比一屏高、提问被卷出屏幕时，自动往上滚（每次 3 格）逐页 OCR，直到看见提问；
各页按「相同文字行的位移投票」拼接，不怕悬浮的「↓」按钮和被屏幕边缘切掉的行；
然后按原路每次 3 格滚回，并**逐字核对回到原画面**（一次性大幅滚动会被 OpenCode 丢掉一部分）。
结果的 `scrollback` 给出 `pages`、`gaps`（拼接是否断档）、`top_reached`、`restored`；
回复末尾的「Build · 模型 · 耗时」单独放进 `footer`。`--no-scrollback` 关闭此功能。

实测（2026-11-19，远端 2560×1440，测试会话「端到端連線測試 E2E-OK-4821」）：

| 场景 | 结果 |
|---|---|
| 新会话首问 | 回复 `E2E-OK-4821`，约 18 s（含 8 s 静止判定） |
| 多轮追问（4821×2） | 回复 `9642`，正确且不是上一轮的回复 |
| 60 项编号清单（超过一屏） | 滚 7 页读全 101–160，无遗漏、无重复，`restored=true`，整轮 29 s |
| 80 项清单单独回读 | 9 页 13.6 s，80 项齐全 |
| 旧版边读（截图→OCR→截图串行，约 0.3 s 一帧） | 201–280：拼接有断档，退回滚动（37 s） |
| 新版边读，90 行清单（`--no-scrollback`） | 401–490 全对，`mode=follow`，0 断档，**完全不滚动**；截 450 帧，50 帧入队，OCR 29 帧，队列最多 5 帧 |
| 新版边读，150 行清单（`--no-scrollback`） | 501–650 全对，`mode=follow`，0 断档，不滚动；截 706 帧，80 帧入队，OCR 52 帧，队列最多 4 帧；回复结束后 8 s 即完成 |

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

- 客户端必须从 `C:\Studio\RdpBridge` 运行（桌面快捷方式已指向那里）。仓库所在的
  `deepseek-harness\default-workspace` 是 Low 完整性沙箱，从 dist 启动的客户端写不了文件、
  打不开截图管道，截图会全部失败（返回 0，日志里也没有记录）。
- 需要 2026-11-19 之后的 `wfreerdp-client3.dll`。旧版把截图也当成输入，每秒截图会让 F15 保活永远不触发，
  远端约 4 分钟就锁屏。新版只认真正的输入（文字、按键、滚轮、指针），保活间隔改为 60 秒。
- 客户端每截一次会在 `%TEMP%\studio-capture.log` 追加两行（按 1 帧/秒约 13 MB/天；`rdp_chat` 边读时每轮约 700 次截图，约 0.1 MB），日志不会自动截断。
- 截到的是客户端缓冲：远端锁屏或安全桌面时，截到的是客户端最后收到的画面。
- 窗口按标题（主机名）匹配，并且只认 `wfreerdp.exe`；同一主机只能开一个客户端实例。
- GPU 与 llama-server（PaddleOCR-VL）共用显卡。远端改分辨率后的第一帧会慢约 1 s（新的检测尺寸）。
- PP-OCRv6 的弱点：浅灰色小字（例如回复底部的「Build · DeepSeek-V4-Flash · 17s」）可能读不到。

## 测试

```powershell
.\run-tests.ps1     # 88 项，约 18 s（预算 10 分钟）
```

测试内置一个模拟 `wfreerdp` 的隐藏窗口（同样响应 WM_COPYDATA），覆盖管道协议和各种失败路径：
拒绝、谎报成功、流截断、非 BMP、错误进程、找不到窗口。还覆盖变化检测（含无变化、光标级变化的负对照）、
PP-OCRv6（CPU/GPU 结果一致、方框为原生坐标、空白帧、GPU 不可用时自动退回 CPU、超宽批次退回 CPU、
缺模型报错）、Windows OCR、VL 重复输出截断（含正常文本不被误截的负对照）、引擎列表解析和读取循环。
`rdp_chat` 的测试用模拟屏幕，覆盖：
- 回滚拼接；
- 边读：
  - OCR 慢到每读一帧就有两屏内容流过，仍能从队列补齐；
  - 自适应跳帧（不跳过接不上的帧）；
  - 只在相邻帧接不上时才记断档（负对照）；
  - 截图线程出错会抛出（负对照）；
  - 重复行不能单独完成拼接（负对照）；
  - PNG 快照无损。
有真实客户端窗口、8080 服务在线或 GPU 可用时，会自动加跑对应的测试。
