# 交接文档（HANDOFF）

> **给下一个对话里的 AI 看的。开工前先从头读一遍。**
>
> 用户会开新对话框、让你读这个文件 —— 这是跨对话保持上下文的唯一手段。
> 所以这里**只写事实和实测证据，不写猜测**。改了代码就回来更新它，
> 尤其是"待办"和"现状"两节，过期的交接文档比没有更糟。
>
> 最后更新：2026-09-17 夜（延迟优化收尾）

---

## 0. 一句话现状

**S0（音频自检）已通过，S1（能听会说）的文字模式已跑通并验证。**
**"反应慢"这件事已经做完**（2026-09-17 夜）：TTS 换成本地 sherpa-onnx，
再把 DeepSeek 的连接预热做掉，首句出声从 3~13 秒降到 **0.6 秒**。
**S1 的语音模式（麦克风 + 唤醒词）还没有被真人完整验证过** —— 这是下一件要干的事。

代码在 `D:\JavaAI\XiaoYu Robot`（**路径里有空格**，写脚本时记得加引号）。
远程仓库 <https://github.com/xiaoxiaoyu66/xiaoyu-robot>，分支 `main`，已同步。

---

## 1. 用户是谁，这个项目是什么

- 用户是 Java 出身的大四学生，**Python 较弱**，中文交流，回复用中文。
- 这是一个**纯爱好的桌面陪伴机器人**，不商用、不赶交付，每周大约 **3 小时**。
- 所以：宁可慢一点、讲清楚为什么，也不要用一堆他看不懂的抽象。
  **一次只改一件事，改完立刻验证。**
- 机器：Windows 11 / Ryzen 5 5600H / 16GB / RTX 3050 4GB / Python 3.11.9（系统级安装，**还没用虚拟环境**）。

---

## 2. 铁律（用户明确要求过，别违反）

1. **日志必须用好** —— 全项目禁止 `print()`，一律 `logger = get_logger(__name__)`。
   双通道：控制台彩色 + `logs/xiaoyu_YYYY-MM-DD.log`（DEBUG 全量）+ `logs/error.log`。
2. **API 密钥绝对不能硬编码** —— 读取顺序：环境变量 `DEEPSEEK_API_KEY` >
   `DEEPSEEK_API_KEY_FILE` 指向的文件 > `.env`。有 `tests/test_no_hardcoded_secrets.py` 自动扫描。
3. **代码放 `D:\JavaAI\XiaoYu Robot`**，注意保持现有目录结构。
4. **用 git 管好** —— 每次改完跑测试、提交、`git push`。
5. `.env` 已被 `.gitignore` 忽略，永远不要提交它。

---

## 3. 常用命令

```powershell
cd 'D:\JavaAI\XiaoYu Robot'

python -m unittest discover tests        # 跑测试（当前 86 个，全过）
python -m xiaoyu --check                 # 环境自检
python -m xiaoyu --text                  # 键盘模式，不碰麦克风/喇叭，验证 大模型+TTS
python -m xiaoyu                         # 语音模式：唤醒词 -> 录音 -> 识别 -> 回答
python scripts\check_audio.py            # 列出所有音频设备 + 录 3 秒回放
python scripts\diagnose_audio.py         # 挨个设备放提示音，找出哪个真的会响
python scripts\bench_latency.py          # 量延迟：TTS 合成速度 + 大模型首句（会真的调 API）
python scripts\bench_latency.py --no-llm # 只量本地 TTS，不联网、不花钱
```

**提交时的坑**：commit message 用 `git commit -F <临时文件>` 传。
直接写 `-m "..."` 时中文和引号容易被 PowerShell 吃掉。
写文件一律用 `[System.IO.File]::WriteAllText($p, $c, (New-Object System.Text.UTF8Encoding($false)))`，
否则会带 BOM。`apply_patch` 在这个带空格的路径下不可靠，改用 PowerShell + Python 精确替换。

---

## 4. 当前进度

### 已验证（有实测证据，可以信）

- S0 音频自检：用户亲耳听到回放，确认通过。
- S1 文字模式：`python -m xiaoyu --text`，中文输入 → DeepSeek 回复 → TTS 说出，
  扬声器正确落到 `[3] 扬声器 (Realtek(R) Audio)`。
- 单元测试 86 个全过；17 个模块导入正常。
- 三种提示音（ack / done / error）已在真实扬声器上播出。
- **延迟优化已实测**：`python -m xiaoyu --text` 日志显示"首句出声 0.61 秒"。
  本地 TTS 两个候选模型（piper-huayan / matcha-baker）都跑通，RTF 都在 0.06~0.08。

### 未验证（别当成已完成）

- **语音模式端到端**：用户还没真的喊过唤醒词走完一轮。唤醒词是**音素级**写法，
  中文必须写 `x iǎo y ǔ` 而不是 `小宇`（写汉字会让 sherpa-onnx 在 C++ 层崩溃、
  Python 抓不到异常）。这个坑已经用启动前校验 + `scripts\make_keywords.py` 挡住了，
  但**真人跑通之前都不算数**。
- 提示音在真实唤醒流程里的体感效果。
- S4 记忆向量检索（`memory/store.py` 的 `recall()` 目前是占位实现）。
- S5 表情脸、S6 眼睛：只有占位。

### 里程碑

| 步骤 | 状态 |
|------|------|
| S0 音频自检 | ✅ 真人验证通过 |
| S1 能听会说 | 🟡 文字模式已验证，语音模式待真人验证 |
| S2 有脑子 | 🟡 DeepSeek 已接通，待配合语音走通 |
| S3 唤醒词 | ⬜ 代码就绪，未真人验证 |
| S4 性格 + 记忆 | ⬜ 未开始 |
| S5 表情脸 | ⬜ 未开始 |
| S6 眼睛 / 独立成体 | ⬜ 未开始 |

---

## 5. 已经踩过的坑（都修了，别重复踩）

这些都是**实测撞出来**的，不是理论风险：

1. **系统默认输出是显示器的 HDMI**。
   这台机器的默认输出是 `[3] H25T7-3 (NVIDIA High Definition)`，也就是显示器。
   声音全进了显示器，笔记本扬声器一声不响，而**日志看起来一切正常**。
   → 必须显式指定输出设备。

2. **音频设备编号会漂移**。
   同一台机器、同一次开机，两次运行之间 `[3]` 和 `[4]` **互换了**
   （扬声器 ↔ 显示器 HDMI）。PortAudio 的 MME 枚举顺序不是固定的。
   → 所以**按名字选设备**（`xiaoyu/audio/devices.py`），编号只当备用。
   这是最坑的一个 bug：写死编号会"时好时坏"。

3. **pygame 无法指定输出设备**。它的混音器自己挑设备，无视配置。
   → 已换成 `soundfile` 解码 + `sounddevice` 播放。

4. **孤立代理字符会把日志系统搞瘫**。
   从管道/文件拿到的坏字节会变成 `'\udc80'` 这类字符，
   它让 loguru 的**文件 sink 抛 `UnicodeEncodeError`**，整条日志丢掉、
   控制台刷一大段堆栈。→ `_redact()` 现在同时抹密钥 + 清编码，
   两个文件 sink 再加 `errors="backslashreplace"` 兜底。

5. **中文 Windows 上配置文件很容易被存成 GBK**（记事本"另存为 ANSI"）。
   写死 `utf-8` 读会直接抛 `UnicodeDecodeError`，报错新手完全看不懂。
   → `xiaoyu/text.py` 的 `read_text()` 自动识别 utf-8-sig / gbk / utf-16。

6. **中文里混进 ASCII 双引号 `"` 会截断 Python 字符串**，导致 `SyntaxError`。
   已经吃过一次（`check_audio.py`）。→ `tests/test_syntax.py` 防复发。

7. **唤醒模型文件是混搭的**：官方包里 int8 版本只有 encoder/joiner 没有 decoder，
   用 `sorted(glob())[0]` 会拼出组合不起来的三个文件。
   → 改成只挑"三件齐全且后缀一致"的一组。

8. **声码器和模型目录是并排放的，"旁边有 vocoder 就当 matcha"是错的**。
   sherpa-onnx 官方把模型都堆在 `models/tts/` 下：
   `models/tts/vocos-22khz-univ.onnx`（声码器）和 `models/tts/vits-piper-.../`（模型目录）**并排**。
   照"找得到声码器就按 matcha 加载"去判断，piper 会被当成 matcha，
   加载时报一个完全看不懂的错：`'use_eos_bos' does not exist in the metadata`。
   → 只能看模型**自己**的文件名：Matcha 的声学模型叫 `model-steps-N.onnx`，其余一律当 vits。
   `resolve_model_files()` 的 `_looks_like_matcha()` 就是这个判据，
   `tests/test_tts_engine.py::test_vocoder_next_door_is_not_matcha` 是它的回归测试。

9. **git 下载镜像只有 `https://gh-proxy.com/` 在这台机器上可用**（1.28 MB/s）。
   而且**必须带 User-Agent**，否则连接能建起来但永远不传数据（看起来像卡死）。
   `ghfast.top` / `ghproxy.net` / `gh.llkk.cc` / `github.moeyy.xyz` 全部失败。
   `scripts/download_models.py` 已经带上了 UA，也支持 `--prefix` 和 `--only`。

---

## 6. 下一步 TODO（按优先级）

### ✅ 已完成（2026-09-17 夜）：TTS 换成本地 sherpa-onnx + 大模型连接预热

**为什么**：实测 edge-tts 出第一块音频要 **0.88 ~ 11.76 秒**，另外 2 次直接失败
（连接超时 / 握手失败）。**每句话都要重新握手一次**，回三句话就是抽三次奖。
这是"反应慢"的元凶，比大模型慢得多。

**怎么做**：用已经装好的 `sherpa-onnx` 做本地 TTS，不加新依赖。候选中文模型（官方列表里确认过）：

- `vits-melo-tts-zh-en` —— 中英混说，质量不错
- `matcha-icefall-zh-baker` —— 最快
- `kokoro-multi-lang-v1-1` —— 中英 103 音色，质量最好但模型最大

**预期**：首块音频从 1~12 秒降到 0.1~0.3 秒，而且不会失败。

**要求**：**保留 edge-tts 作为可切换的"高音质模式"**，别直接删掉 ——
本地音质不如晓晓。用配置开关切换（用户可以自己对比）。

**实际做法**：
- 新增 `xiaoyu/tts/engine.py`（引擎抽象：`SherpaEngine` / `EdgeEngine`，`build_engine()` 按配置选）。
- 重写 `xiaoyu/tts/synthesizer.py`：合成在主线程、播放在 daemon 线程，中间一个容量 2 的队列 ——
  **放第一句的时候第二句已经在合成**。
- 下载了 `vits-piper-zh_CN-huayan-medium`（60MB）和 `matcha-icefall-zh-baker`（72MB + 声码器 51MB），
  默认用 piper。切模型只改 `.env` 的 `XIAOYU_TTS_MODEL`。

**顺手做掉的第二件事：DeepSeek 连接预热**（这个才是"第一句话"最难受的那段）。
实测同一个问题，**第一次请求 2.0~2.3 秒，之后只要 0.5~0.8 秒**；
把历史对话从 0 轮堆到 40 轮，首句耗时几乎不变 —— 所以那多出来的 1.5 秒**全是建连接**
（TCP + TLS 握手），跟问什么没关系。加了 `DeepSeekClient.warmup()` / `warmup_async()`：
启动时异步预热一次，唤醒词命中后再异步预热一次（正好和录音+识别并行）。
实测首句 **2.05 秒 → 0.41 秒**。

**还没定的事**：用户要**盲听挑音色**，文件已经生成好了：
`data/voice_1_huayan.wav`（piper）和 `data/voice_2_baker.wav`（matcha），同一句话。
两个延迟一样，纯看喜好，听完改 `.env` 的 `XIAOYU_TTS_MODEL` 就行。

### 🟠 P1 · 真人验证语音模式

跑 `python -m xiaoyu`，喊唤醒词，走完一轮。
这一步没通过之前，S1 不算完成。重点看：

- 唤醒词能不能被听到（音素写法见第 5 节第 7 条）
- 提示音是不是真的改善了体感
- `XIAOYU_SILENCE_SECONDS=0.5` 会不会把说话中间的停顿误判成"说完了"
- 录音峰值是不是够高（之前测到过环境底噪只有 0.0027，
  而静音阈值是 0.015 —— 如果真的偏低，要么调麦克风增益，要么降阈值）

### 🟡 P2 · 打断功能（barge-in）

说话时喊唤醒词能打断它。
**前提是先解决回声**：外放喇叭 + 开麦，它会听见自己。要么用耳机，要么上回声消除。
参考 `Open-LLM-VTuber` 的做法。

### ⚪ P3 · 后面的事

- S4：`memory/store.py` 的 `recall()` 换成 bge-small-zh-v1.5 + 余弦相似度；
  再加"每 20 轮自动总结关于主人的事实"。可参考 `MemoryWebAssistant` 的
  RAG/CAG 双模式和"相似度 > 0.8 就不重复存"。
- S5：Live2D 表情脸，旧手机浏览器当屏幕，接状态机的 `on_change`。
- S6：RapidOCR 读字 + InsightFace 认人；搬 N100 独立成体。
- 环境：迁移到虚拟环境（用户之前选了"以后弄"）。
  注意工作区里有个**空的 `.venv` 目录需要用户手动删**，`Remove-Item -Recurse` 被安全策略拦。

---

## 7. 关键实测数据（别再重新测一遍）

| 环节 | 实测耗时 | 说明 |
|------|----------|------|
| VAD 等你说完 | 0.5 秒 | 可调（`XIAOYU_SILENCE_SECONDS`），原为 0.8 |
| SenseVoice 识别 | 0.13~0.15 秒 | 本地。**反复调都一样，没有冷启动问题，不用预热** |
| DeepSeek 出第一句（冷连接） | **1.99~2.29 秒** | 其中约 1.5 秒是 TCP + TLS 握手 |
| DeepSeek 出第一句（预热后） | **0.53 / 0.61 / 0.65 秒** | 历史 0~40 轮都一样，跟上下文长度无关 |
| **本地 TTS 合成一句** | **0.037 ~ 0.35 秒** | RTF 0.053~0.080。33 字的长句也只要 0.33 秒 |
| local TTS 加载模型 | 1.4~1.9 秒 | 启动时一次性，`warmup()` 之后不算在回复里 |
| ~~edge-tts 出第一块音频~~ | ~~0.88~11.76 秒~~ | **已弃用为可选项，另有 2 次直接失败** |

**用户实际等多久（从你说完话到它开口）**：

    VAD 0.5 + 识别 0.14 + 大模型首句 0.6 + 合成 0.1 ≈ 1.35 秒

改造前这条链是 `0.5 + 0.3 + 2.3 + 4（edge-tts 中位数）≈ 7 秒`。

**两个候选音色的实测对比**（Ryzen 5 5600H，2026-09-17）：

| 模型 | 大小 | 加载 | 合成 33 字 | RTF | 备注 |
|------|------|------|-----------|-----|------|
| `vits-piper-zh_CN-huayan-medium` | 60MB | 1.4s | 0.32s | 0.057 | 自带声码器；**默认** |
| `matcha-icefall-zh-baker` | 72MB | 1.9s | 0.36s | 0.059 | 要配 `vocos-22khz-univ.onnx`（51MB）；有 OOV 警告 |

声卡现状（2026-09-17 实测，MME 这一组）：

- `[1] 麦克风 (Realtek(R) Audio)` —— 输入
- `[3] 扬声器 (Realtek(R) Audio)` —— 输出（注意：**曾经是 `[4]`**）
- `[3或4] H25T7-3 (NVIDIA High Definition)` —— 显示器，系统默认输出，**别用**

`.env` 现在按名字选：`XIAOYU_SPK_DEVICE_NAME=扬声器` / `XIAOYU_MIC_DEVICE_NAME=麦克风`。

---

## 8. 参考过的开源项目（真实 star 数，2026-09-17 查）

用户自己找了两个，评估如下：

- `RiccardoDominici/MemoryWebAssistant` —— **3 stars**，单个大文件，架构参考价值有限。
  只有两个小点子值得偷：RAG/CAG 双模式、记忆查重（相似度阈值 0.8）。
  别跟它的 Ollama 本地小模型（中文质量差）和 faster-whisper `large-v3`（4GB 显存吃力）。
- `well-it-wasnt-me/RON`（项目名 DeskBot）—— **1 star，但架构值得学**。
  硬件抽象在 protocol 接口后面（`Display`/`ServoController`/`AudioOutput`/`Microphone`/
  `Camera`/`LLM`/`EventBus`），行为代码永不 import 具体驱动，同一套代码能跑真机 / mock / 无头仿真。
  **这就是 S5/S6 和"搬到 N100"的正解。** 别照搬它的树莓派 + 舵机 + Q-learning 强化学习，太重。

更该看的：

| 项目 | Stars | 语言 | 备注 |
|------|-------|------|------|
| `78/xiaozhi-esp32` | 30.0k | C++ | 中文语音助手的事实标准，极活跃 |
| `xinnan-tech/xiaozhi-esp32-server` | 10.6k | JS | 后端就是完整的 VAD→ASR→LLM→TTS + 打断，**架构图最值得抄** |
| `Open-LLM-VTuber/Open-LLM-VTuber` | 13.8k | Python | Live2D + 语音打断，和 S5 对口；但 2026-05 后没更新 |
| `moeru-ai/airi` | 49.2k | TypeScript | 终态参考，很重，现在看不懂正常 |
| `k2-fsa/sherpa-onnx` | 14.8k | C++ | 已在使用，KWS + ASR + TTS 一家全包，**别再加别的音频库** |

反面例子：`rhasspy/piper` 有 11.2k stars 但**已停止维护（archived）**，
活跃继任者是 `OHF-Voice/piper1-gpl`。`dscripka/openWakeWord` 2.8k stars
且 2025-12 后没更新 —— 所以当初选 sherpa-onnx KWS 是对的，不用换。

---

## 9. 给下一个 AI 的提醒

- 用户说"读文档"就是让你读这个文件。
- **不要一次塞太多东西**。他每周只有 3 小时，一次做一件事做完再下一件。
  这个项目最大的风险不是技术难度，是收藏了一堆项目结果一个都没跑通。
- 改完代码要更新本文件的**第 0 节和第 6 节**，然后提交 + push。
- 说话直接一点，有问题就说有问题，别为了让他高兴而说"已完成"。