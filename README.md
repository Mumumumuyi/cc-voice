# cc-voice

Windows 上的中文语音输入。按住触发键说话，松开自动把文字上屏 —— Claude Code
终端、微信、浏览器、任何能打字的地方。**全程本地离线**，不联网、不需要 API Key。

- 识别内核：[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) + FunASR-Nano / SenseVoice（int8 ONNX，CPU 推理）
- 悬浮岛：乳白玻璃质感的常驻药丸，可拖动，红灯待机 / 绿灯录音
- 管理面板：本地网页，可调模型、触发键、生效范围、热词表

## 从零安装

需要 [uv](https://github.com/astral-sh/uv)（管 Python 环境）和 Windows 10/11。

```powershell
git clone https://github.com/Mumumumuyi/cc-voice.git "$env:USERPROFILE\.claude-voice"
cd "$env:USERPROFILE\.claude-voice"

uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe sherpa-onnx sounddevice numpy pillow

pwsh -File tools\fetch_models.ps1        # 下载 ASR 模型，约 340MB
```

模型和虚拟环境都不入库（一起近 600MB），所以克隆后要跑上面这两步。

做一个桌面快捷方式指向 `语音输入开关.cmd`，双击即可开关。想让它跟 Claude Code
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

悬浮岛可以**拖到任何位置**，松手即记住；**双击**它打开管理面板。

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

「最近识别」会记录**每一次尝试**，包括没上屏的，并显示录音时长、音量峰值和未上屏
原因（太短 / 没听到说话 / 没识别到内容）—— 按了没反应时先看这里。

## 日志

```
logs/
  识别记录.log        人看的：对齐流水，√ 已上屏 / · 未上屏，记事本直接打开
  data/history.jsonl  面板读的结构化数据
  hook.log            只在插件钩子出错时才会出现
```

两种读者要的东西不一样 —— 面板要能解析，人要能一眼扫出哪条没上屏、为什么。
塞进同一个格式的结果是两边都难受，所以分开写。

## 识别精度

默认模型 **FunASR-Nano**（int8 ONNX，CPU 推理）。构建时在本机测过（TTS 合成音，6 句）：

| 模型 | 平均字错率 | 平均推理 | RTF |
|---|---|---|---|
| funasr-nano | 2.1% | 154ms | 0.040 |
| sense-voice | 6.1% | 86ms | 0.022 |

真人语音的错误率会高于这组数字（TTS 无口音、无环境噪声），此表只用于模型间比较。
两个模型都装好了，在管理面板的「识别模型」里可随时切换、按感受选。

**专有名词靠热词救，不靠模型。** `git` / `pnpm` / `useEffect` 这类词在 ASR 语料里
极罕见，两个模型都会听错。在 `hotwords.txt` 里加一行即可强制纠正（拼音模糊匹配），
固定口误写进 `rules.txt`（`原文=>替换`）。

## 目录

```
daemon/     守护进程：触发、录音、识别、注入、灵动岛、管理面板
  winapi.py   Win32 绑定（DPI、剪贴板、SendInput、低级钩子）
  trigger.py  低级鼠标/键盘钩子：触发判定 + 灵动岛拖动
  gate.py     会话闸门：枚举进程找 claude.exe，判断前台窗口是不是它的终端
  audio.py    麦克风采集      asr.py     sherpa-onnx 识别
  textfix.py  热词与规则纠正   inject.py  剪贴板上屏
  render.py   Pillow 出图      layered.py 分层窗口推送
  hud.py      灵动岛状态机     panel.py + web/  管理面板
models/     FunASR-Nano / SenseVoice（int8 ONNX，约 487MB）
plugin/     本地插件市场：SessionStart 钩子 + /voice 命令
tools/      toggle.ps1（开关）、fetch_models.ps1（重新下载模型）
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
- **ASR 模型没有「静音」这个输出**：喂它底噪一定会硬猜出字来。静音判定必须在送进
  模型之前按音频电平做（`min_level`）。模型还会输出 `<|nospeech|>`、`<|zh|>` 这类
  控制标记，必须剥掉，否则会被原样粘进输入框。
- **守护进程的互斥量名必须与钩子里 `OpenExisting` 的完全一致**。曾经一边 `Global\`
  一边 `Local\`，钩子永远探不到，于是每开一个 cc 窗口就多起一个守护进程 —— 多个
  进程抢同一个麦克风、重复注入，表现为「录音经常断」。
- **低级钩子回调里不能做文件 I/O**：Windows 有 LowLevelHooksTimeout，超时会把钩子
  静默摘掉，表现是「时好时坏地失灵」。闸门的会话刷新因此挪到主线程周期执行。
- **`.cmd` 文件内容必须是纯 ASCII**：批处理按系统 OEM 代码页(GBK)读文件，UTF-8 的
  中文注释会变成乱码字节并被当作命令执行。文件名用中文没问题。
