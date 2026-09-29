# cc-voice

Windows 上的中文语音输入。按住触发键说话，松开自动把文字上屏 —— Claude Code
终端、微信、浏览器、任何能打字的地方。**全程本地离线**，不联网、不需要 API Key。

- 识别内核：[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) + FunASR-Nano（默认带 Qwen3-0.6B 解码器的 LLM 版，热词直接参与识别）/ SenseVoice（int8 ONNX，CPU 推理）
- 模糊音纠错：平翘舌、n/l、前后鼻音不分也能纠回热词（「数据酷」→「数据库」），常用词不误改
- 悬浮岛：乳白玻璃质感的常驻药丸，可拖动，红灯待机 / 绿灯录音
- 管理面板：本地网页，可调模型、触发键、生效范围、热词表

## 从零安装

需要 [uv](https://github.com/astral-sh/uv)（管 Python 环境）和 Windows 10/11。

```powershell
git clone https://github.com/Mumumumuyi/cc-voice.git "$env:USERPROFILE\.claude-voice"
cd "$env:USERPROFILE\.claude-voice"

uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe sherpa-onnx sounddevice numpy pillow pypinyin jieba

pwsh -File tools\fetch_models.ps1        # 下载 ASR 模型，约 1.2GB，逐个校验 sha256
```

模型和虚拟环境都不入库（一起约 1.5GB），所以克隆后要跑上面这两步。`pypinyin` 和
`jieba`（只读它的词表）是热词模糊纠错要用的，缺了面板会报警、纠错降级为只修大小写。

做一个桌面快捷方式指向 `语音输入开关.cmd`（图标选 `assets/icon.ico`），双击即可开关。想让它跟 Claude Code
一起自动启动，再执行：

```powershell
claude plugin marketplace add "$env:USERPROFILE\.claude-voice\plugin"
claude plugin install cc-voice@cc-voice-local
```

## 启动和关闭

桌面上一个快捷方式 **「语音输入」**，双击一次开，再双击一次关。

开着的时候屏幕底部有一枚乳白色悬浮岛（红灯=待机）—— **它就是状态指示**：
在，就是开着；不在，就是关着。

脚本本体是 `语音输入开关.cmd`（内部调 `tools/toggle.ps1`）。它靠守护进程的
命名互斥量判断当前状态，和守护进程自己做单实例判断用的是同一个权威源。

插件只负责「开 Claude Code 时顺手把守护进程拉起来」这一件事，**语音功能本身
不依赖它** —— 闸门是自己枚举进程找 `claude.exe` 来认终端的。

**跟 cc/cc2/cc3 一起自动启动**（可选，默认关闭）：

```
claude plugin enable  cc-voice@cc-voice-local     # 开：以后开 Claude Code 自动启动
claude plugin disable cc-voice@cc-voice-local     # 关：只用桌面快捷方式手动启停
```

## 怎么说话

1. 点一下 Claude Code 的终端窗口（语音输入只在它上面生效）
2. **按住鼠标侧键 X2**（或**右 Ctrl**）→ 停半拍 → 说话 → 说完再松开
3. 灯变绿表示在听，松开后文字自动贴进输入框（不会自动回车）
4. 说完可以**马上按住说下一句**，不用等上一句出字：上一句在后台识别，几句按说话顺序上屏

悬浮岛可以**按住左键拖到任何位置**，松手即记住；**双击**它打开管理面板。

## 首次要做的一件事：确认侧键

WMI 报不出鼠标按钮数，你的鼠标有没有 X2 侧键只能实测：

```
.venv\Scripts\python.exe daemon\ccvoice.py --probe
```

按提示依次按两个侧键。看到 `mouse x2 down` 就说明默认配置可用；只看到 `mouse x1`
就在管理面板里把「鼠标触发键」改成 X1；两个都没有，说明这只鼠标没有侧键，用右 Ctrl。

## 管理面板

`http://127.0.0.1:8731/`（双击悬浮岛也能打开）。可调：识别模型、语言、麦克风、
触发键、生效范围、上屏方式、悬浮岛不透明度、静音阈值，以及热词表与替换规则。
右下角还有「暂停 / 恢复」和「退出」。

「识别记录」会记录**每一次尝试**，包括没上屏的和出错的，并显示录音时长、音量峰值和
未上屏原因（太短 / 没听到说话 / 没识别到内容 / 出错）—— 按了没反应时先看这里。
按日期分组，可搜索、可只看未上屏；点开一条能看到音量峰值和静音阈值的对比。

**按下就报错、日志里是「麦克风被系统禁用」**：Windows 的麦克风隐私开关关着。到
设置 › 隐私和安全性 › 麦克风，打开「麦克风访问」和「允许桌面应用访问你的麦克风」。
这时 PortAudio 只会报 `Unanticipated host error [MME error 1]`，看不出原因，所以面板
顶部会直接读系统开关、亮出提示。

## 日志

```
logs/
  识别记录.log        人看的：对齐流水，√ 已上屏 / · 未上屏 / × 出错，记事本直接打开
  data/history.jsonl  面板读的结构化数据
  hook.log            只在插件钩子出错时才会出现
```

两种读者要的东西不一样 —— 面板要能解析，人要能一眼扫出哪条没上屏、为什么。
塞进同一个格式的结果是两边都难受，所以分开写。

## 识别精度

默认模型 **funasr-nano-llm**：FunASR-Nano 的完整版，声学编码器后面接 Qwen3-0.6B
解码器，能结合上下文挑字（「重构」而不是「重购」），热词表直接写进它的提示词。
2026-09-27 在本机测过：35 句 × 3 个 Windows 中文语音 = 105 条 TTS，句子模拟日常开发口述
（专有名词、口音模拟、误改陷阱），电平压到真实麦克风的水平：

| 配置 | 字错率 | 专有名词命中 | 误改 | 平均等待 | p95 | 常驻内存 |
|---|---|---|---|---|---|---|
| funasr-nano（纯声学，旧默认） | 7.30% | 70.8% | 0 | 0.13s | 0.18s | ~0.3GB |
| funasr-nano + 模糊音纠错 | 6.10% | 81.9% | 0 | 0.14s | 0.21s | ~0.3GB |
| Qwen3-ASR 0.6B（对照，未采用） | 4.41% | 83.0% | 0 | 0.88s | 1.24s | ~1.0GB |
| **funasr-nano-llm + 热词 + 模糊音纠错** | **1.36%** | **93.6%** | 0 | 1.04s | 1.38s | ~1.1GB |

代价是等待：LLM 版每句多等约 1 秒，长句按比例增加（实测 10 秒录音约 2 秒、15 秒约 3 秒），
识别时占 4 个线程。要快就在面板「识别模型」切回 `funasr-nano`，热词纠错照样生效。
TTS 没有口音和环境噪声，真人语音的错误率会更高，此表只用于配置间比较。

**热词表怎么写。** 词表是 `hotwords.txt`，首次启动时从模板 `hotwords.example.txt` 复制而来，
**不入库**（在 `.gitignore` 里），项目代号、人名这类私人词汇放心写。只放模型总听错的词。
模型本来就认得的词放进去会变成「磁铁」：实测一个不放热词也 6/6 全对的英文缩写，放进去后
把没听清的其他短英文词都吸成了它，拿掉后字错率从 2.40% 降到 1.36%。热词还会按模糊音逐字
纠正（`数据酷` → `数据库`、`兰点` → `难点`），但窗口本身是常用词就不动（`借口` 不会变 `接口`）。
固定口误写进 `rules.txt`（`原文=>替换`）。

## 目录

```
daemon/     守护进程：触发、录音、识别、注入、灵动岛、管理面板
  winapi.py   Win32 绑定（DPI、剪贴板、SendInput、低级钩子）
  trigger.py  低级鼠标/键盘钩子：触发判定 + 灵动岛拖动
  gate.py     会话闸门：枚举进程找 claude.exe，判断前台窗口是不是它的终端
  audio.py    麦克风采集      asr.py     sherpa-onnx 识别
  textfix.py  规则 + 热词模糊音纠错   inject.py  剪贴板上屏
  render.py   Pillow 出图      layered.py 分层窗口推送
  hud.py      灵动岛状态机     panel.py + web/  管理面板
models/     funasr-nano-llm / funasr-nano / sense-voice（int8 ONNX，约 1.5GB）
plugin/     本地插件市场：SessionStart 钩子 + /voice 命令
tools/      toggle.ps1（开关）、fetch_models.ps1（重新下载模型）、make_icon.py（生成 assets/icon.ico）
assets/     icon.ico（桌面快捷方式图标，16~256px 共 9 个尺寸）
logs/       history.jsonl（识别历史）、gate.log、hook.log
```

## 踩过的坑（改代码前先读）

- **tkinter 不是线程安全的**：工作线程调 `root.after()` 会让进程静默崩溃、没有
  traceback。所有跨线程 UI 动作走 `ui_q` 队列，由主线程 `_pump` 排空。
- **ctypes 默认 restype 是 32 位有符号 int**：x64 上句柄/指针被截断后解引用 =
  访问违例。所有返回或接收句柄的 Win32 调用都必须显式声明类型。
- **PowerShell 5.1 的 `Set-Content -Encoding utf8` 一定写 BOM**，Python 侧
  `json.loads` 会抛异常。写用 `UTF8Encoding($false)`，读用 `utf-8-sig`。
- **灵动岛必须保持 `WS_EX_TRANSPARENT`**：一旦可点击，tkinter 处理点击时会把窗口
  顶到前台，而闸门靠前台窗口 PID 判断终端身份，语音输入会彻底失效。拖动因此
  由低级鼠标钩子在消息抵达窗口之前拦截实现。
- **钩子里只能吞按下/松开，不能吞 `WM_MOUSEMOVE`**：吞掉移动等于把光标钉在原地，药丸
  只会跟着手抖几个像素（`CCVOICE_DEBUG_DRAG=1` 的 drag.log 里坐标始终在按下点 ±20 内）。
  拖动时窗口也不能等渲染循环去挪 —— 待机 40ms 一帧，拖起来一卡一卡的；`move_by` 直接
  `SetWindowPos(SWP_ASYNCWINDOWPOS)`，延迟从 38ms 降到 1ms，且不会让钩子回调等界面线程。
- **录音和识别不能在同一个线程**：识别要 0.5~2 秒，说完立刻再按时「开始录音」排在它后面，
  下一句的开头就丢了（记录里是「只有杂音，人声 20ms」），上一句的「完成」定时器还会把录音中
  的灵动岛切回待机。现在识别有独立线程按顺序做；灵动岛录音中只认「聆听」，过期的回待机作废。
- **连续上屏会串剪贴板**：每句贴完 400ms 后还原剪贴板，两句挨得近时第二句会把第一句当成
  「原剪贴板」存下，还可能被第一句的还原抢先改掉内容。`inject.py` 只在一串的第一句前存一次，
  由最后一句负责还原。
- **ASR 模型没有「静音」这个输出**：喂它底噪一定会硬猜出字来。静音判定必须在送进
  模型之前做。只看峰值（旧的 `min_level=0.045`）会在麦克风整体变小声后把几秒钟的
  真实说话扔掉，所以现在是峰值底线 + 「比本段底噪高 10dB 的人声帧够不够 150ms」
  （`audio.speech_span`）。模型还会输出 `<|nospeech|>`、`<|zh|>`、`/sil` 这类
  控制标记，必须剥掉，否则会被原样粘进输入框。
- **LLM 模型的上下文只有 512 token**（提示词 + 音频 + 输出共用）：22 秒起音频被悄悄
  截掉一半、24 秒起直接返回空串，不报错。长录音在 `asr._split` 里按停顿切成 ≤10 秒的段。
- **依赖缺失不能静默降级**：`pypinyin` 没装时热词纠错一直是空转，129 条识别一条都没改过；
  而旧的纠错代码一旦装上 `pypinyin`，会把「分子结构」改成「分支接口」。现在缺依赖会在
  面板亮警告，纠错按「字对字 + 常用词不动」重写。
- **Windows 上 `TZ=America/New_York` 会让 Python 的本地时间算成 UTC+1**（C 运行时读不懂
  POSIX 时区名）。从带这个变量的终端拉起守护进程，日志时间会快 5 小时，所以 `ccvoice.py`
  一开头就把它删掉。
- **模型镜像要校验**：ModelScope 上同名的 funasr-nano 包比官方小 91MB，curl 照样报成功。
  `fetch_models.ps1` 对每个文件校验 GitHub release 的 sha256。
- **守护进程的互斥量名必须与钩子里 `OpenExisting` 的完全一致**。曾经一边 `Global\`
  一边 `Local\`，钩子永远探不到，于是每开一个 cc 窗口就多起一个守护进程 —— 多个
  进程抢同一个麦克风、重复注入，表现为「录音经常断」。
- **低级钩子回调里不能做文件 I/O**：Windows 有 LowLevelHooksTimeout，超时会把钩子
  静默摘掉，表现是「时好时坏地失灵」。闸门的会话刷新因此挪到主线程周期执行。
- **`.cmd` 文件内容必须是纯 ASCII**：批处理按系统 OEM 代码页(GBK)读文件，UTF-8 的
  中文注释会变成乱码字节并被当作命令执行。文件名用中文没问题。
