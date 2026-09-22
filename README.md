# XiaoYu Robot

一台放在书桌上的陪伴机器人。你喊它名字，它醒过来；你跟它说话，它答你；它记得你上次说过的事。

自用项目，慢慢做，不赶进度。

---

## ⚠️ 开工前必读（AI 也一样）

**接手这个项目之前，先读 [`HANDOFF.md`](HANDOFF.md)。**

里面记着当前进度、**已经踩过的坑（这些别再踩一遍）**、关键环节的实测耗时、
下一步待办，以及参考过的开源项目。用户习惯开新对话框干活，
这份文档就是跨对话保持上下文的唯一手段 —— 读完它才算“接上了”。
改完代码记得回去更新它的第 0 节和第 6 节。

---

## 现在做到哪一步了

| 阶段 | 内容 | 状态 |
|---|---|---|
| S0 | 音频自检（能录能放） | ✅ 真机跑通 |
| S1 | 能听会说（识别 + 合成） | ✅ 2026-09-17 真人验证通过（文字 + 语音两种模式） |
| S2 | 有脑子（DeepSeek 流式对话） | ✅ 首句出声 0.6 秒（拆解见 `docs/总方案_v2` §1.3.1） |
| S3 | 唤醒词（喊"小宇"就醒） | ✅ 2026-09-17 真人验证通过 |
| S4 | 性格 + 长期记忆 | ✅ 4a / 4b 真人验证通过（2026-09-18）；4c 向量检索可选 |
| S5 | 表情脸 + 触屏打断 | ✅ 第一版真人验证通过（2026-09-18）；Live2D 换皮未开始 |
| S6 | 眼睛 / 独立成体 | ⬜ 未开始 |

> 4a 做的事：启动时把最近的对话从 SQLite 读回上下文，并告诉模型"现在几点、上次聊天是多久以前"。
> 也就是说 —— **现在关掉程序再开，它还记得你。**
> 详细路线见 `docs/总方案_v2_从零开始.md`（历史教程）和 `HANDOFF.md`（当前进度）。


---

## 目录结构

```
XiaoYu Robot/
├─ xiaoyu/                 主包，所有业务代码都在这
│  ├─ __main__.py          入口：python -m xiaoyu
│  ├─ app.py               主控，把各模块串起来（状态机在这）
│  ├─ logger.py            日志 ★ 全项目唯一出口
│  ├─ config.py            配置：路径 / 参数 / 密钥 / 自检
│  ├─ state.py             状态机：idle / listening / thinking / speaking
│  ├─ text.py              纯文本工具（分句），零依赖
│  ├─ audio/               录音器、扬声器、设备清单
│  ├─ wake/                唤醒词（KWS；models.py 是纯逻辑，kws.py 跑模型）
│  ├─ asr/                 语音转文字（SenseVoice）
│  ├─ tts/                 文字转语音（本地 sherpa-onnx，可切 edge-tts）
│  ├─ llm/                 大模型对话（DeepSeek 流式）
│  ├─ memory/              记忆（SQLite，向量检索预留）
│  ├─ face/                表情脸（S5 的协议和服务；网页在仓库根的 face/）
│  ├─ vision/              眼睛跟随（S6a：gaze 纯函数 + 摄像头守护线程）
│  ├─ xiaozhi/             小智设备协议层（A 档：ESP32 板子当耳朵和嘴巴，默认关）
│  └─ body/                身体接口层（舵机；默认关，真舵机回来才换 backend）
├─ scripts/
│  ├─ check_audio.py       S0 音频自检
│  ├─ download_models.py   下载模型
│  ├─ make_keywords.py     中文唤醒词 -> 音素格式
│  └─ xiaozhi_fake_device.py  假设备：不用板子也能验小智那一侧（--selftest 只验编解码）
├─ config/
│  ├─ keywords.txt         唤醒词（改这里就能换名字）
│  ├─ persona.md           小宇的性格（改这里就能换人格）
│  └─ persona_examples.md  另外两套性格模板
├─ tests/                  测试（零依赖，随时能跑）
├─ models/                 模型文件（不进 git）
├─ data/                   SQLite、TTS 缓存（不进 git）
├─ logs/                   日志文件（不进 git）
├─ face/                   表情脸网页（Vector 风豆眼）：index.html?token=xiaoyu
└─ docs/                   设计文档
```

**分层原则**：`app.py` 负责编排，各子包只负责自己的那一件事，互不越界。
要换掉某个部件（比如把本地 TTS 换成 GPT-SoVITS），只改对应子包，其他地方不动。
`xiaoyu/tts/engine.py` 就是干这个的：加一个类，别的代码一行不用改。

---

## 快速开始

```powershell
cd "D:\JavaAI\XiaoYu Robot"

# 1. 装依赖
#    当前直接用系统 Python（依赖已装好，直接能跑）。
#    规范做法是装进虚拟环境，见 TODO.md，以后再做。
pip install -r requirements.txt

# 3. 配密钥
Copy-Item .env.example .env
notepad .env      # 填入 DEEPSEEK_API_KEY

# 4. 下模型（约 200MB）
python scripts\download_models.py

# 5. 自检
python -m xiaoyu --check

# 6. 先玩键盘模式（不需要麦克风，最快见效）
python -m xiaoyu --text

# 7. 都通了再上语音
python scripts\check_audio.py
python -m xiaoyu
```

> **有小智那块板子（A 档）时**（`docs/到货当天_测试清单.md`）：
>
> ```powershell
> # 不用板子也能验我们这一侧：自己起服务 + 自己当设备，走完整的一轮
> python scripts\xiaozhi_fake_device.py --selftest
> python scripts\xiaozhi_fake_device.py
>
> # 只跑板子这条路（不要麦克风 / 喇叭 / 摄像头；板子配网页里填 http://<本机IP>:8766）
> $env:XIAOYU_XIAOZHI_ENABLED='1'
> python -m xiaoyu --xiaozhi-only
> ```
>
> 板子服务端占 **8766（OTA）+ 8767（WebSocket）** —— 为什么是两个口而不是一个：
> `docs/到货当天_手把手.md` §6.1。

---

## 日志

**规矩：项目里不许出现 `print()`，一律走 `logger`。**

每个模块顶部这样拿 logger：

```python
from ..logger import get_logger
logger = get_logger(__name__)

logger.debug("细节，排查时才看")
logger.info("正常流程的关键节点")
logger.warning("不对劲但还能跑")
logger.error("这一步失败了")
logger.exception("出异常了，自动带上堆栈")   # 只在 except 块里用
```

产物：

| 文件 | 内容 | 保留 |
|---|---|---|
| 控制台 | 彩色，级别由 `.env` 的 `XIAOYU_LOG_LEVEL` 控制 | — |
| `logs/xiaoyu_YYYY-MM-DD.log` | 全量（含 DEBUG），按天切分 | 14 天，自动压缩 |
| `logs/error.log` | 只记 ERROR 以上 | 90 天 |

**出问题时先看 `logs/error.log`**，再按时间翻当天的全量日志。

日志级别在 `.env` 里改：

```
XIAOYU_LOG_LEVEL=DEBUG      # 排查问题时打开，平时用 INFO
```

未捕获的异常会自动进日志（`logger.py` 里挂了 `sys.excepthook`），
所以程序不会"静默死亡"。

---

## 配置改哪里

| 想改什么 | 改哪个文件 |
|---|---|
| 唤醒词（用 `scripts\make_keywords.py` 生成） | `config/keywords.txt` |
| 小宇的性格 | `config/persona.md` |
| API Key / 日志级别 / 麦克风编号 | `.env` |
| 表情脸开关 / 端口 / token | `.env` 的 `XIAOYU_FACE_*` |
| 采样率、静音阈值、音色、模型名 | `xiaoyu/config.py` |

### 换唤醒词（别手写，用脚本）

唤醒模型的词表是**音素**级的，中文要用「声母 + 带声调韵母」写，直接写汉字会让程序崩溃：

```
错：  小宇 :2.5 #0.5 @小宇
对：  x iǎo y ǔ :2.5 #0.5 @小宇
```

所以换唤醒词请用脚本生成：

```powershell
python scripts\make_keywords.py 小宇              # 先看转换结果对不对
python scripts\make_keywords.py 二娃 --write      # 确认没问题再写进 config/keywords.txt
python scripts\make_keywords.py 小宇 --write --threshold 3.0    # 顺带调阈值
```

`config/keywords.txt` 的格式（脚本会自动生成）：

```
音素序列 :阈值 #增强 @显示名
x iǎo y ǔ :2.5 #0.5 @小宇
```

阈值越大越难唤醒（越不容易误触发），一般 2.0 ~ 4.0 之间调。
如果加载时报"音素不在模型词表里"，说明词转错了，换一个唤醒词试试。

> 程序启动时会先校验唤醒词文件，音素不对会给出明确报错，
> 而不是让 sherpa-onnx 在 C++ 层静默崩溃。

---

## 测试

零依赖，随时可跑（当前 251 个）：

```powershell
python -m unittest discover tests -v
python -m pytest tests          # 同一批用例，数字应该和上面一致
```

测试**不会**往 `logs/` 写日志 —— `logger.py` 发现自己在 pytest / unittest 下，
就把日志改道到系统临时目录，免得测试堆栈淹掉真实故障。

---

## Git 工作流

```powershell
git status                      # 看改了啥
git add -A
git commit -m "feat: 加上唤醒词检测"
git push
```

提交信息用 `类型: 说明` 的格式，一眼能看懂就行：

| 前缀 | 用途 |
|---|---|
| `feat:` | 新功能 |
| `fix:` | 修 bug |
| `docs:` | 只改文档 |
| `chore:` | 杂项（依赖、配置） |
| `refactor:` | 重构，行为不变 |

**不要提交的东西**（`.gitignore` 已经挡住了，别硬来）：
`.env`（密钥）、`models/`（模型太大）、`logs/`、`data/`、`.venv/`

---

---

## 密钥与安全

**代码里永远不许出现明文密钥。** 这条有自动化兜底：`tests/test_no_hardcoded_secrets.py`
每次跑测试都会扫全项目，发现像真密钥的字符串就失败。

密钥有三种放法，任选一种：

| 方式 | 怎么做 | 适合 |
|---|---|---|
| `.env` 文件（推荐） | 复制 `.env.example` 为 `.env`，填进去 | 平时开发 |
| 系统环境变量 | `setx DEEPSEEK_API_KEY "sk-你的key"` | 已经配好了就一直用 |
| 密钥文件 | 密钥写到别处，`.env` 里只填 `DEEPSEEK_API_KEY_FILE=D:\secrets\deepseek.key` | 以后搬到 N100 上跑 |

优先级：环境变量 > `_FILE` 指向的文件 > `.env`。读取逻辑在 `xiaoyu/config.py` 的 `_read_secret()`。

### 三道防线

1. **`.gitignore`** 挡住 `.env`，永远不会被提交；还有一个测试专门确认 `.env` 没被 git 跟踪。
2. **日志脱敏**：日志出口挂了过滤器，任何像 `sk-xxxx` 或 `Bearer xxxx` 的内容，
   在写盘前就会被替换成 `<已脱敏>` —— **连异常堆栈里的也不放过**。
   密钥一旦进了日志文件就是永久留痕，所以这层必须有。
3. **不打印密钥**：代码里只打印"已配置 / 缺失"，从不输出密钥本身。

实测效果：

```
18:48:29 | INFO  | 模拟误用：正在用 key=<已脱敏> 连接
18:48:29 | ERROR | 模拟异常里带密钥
RuntimeError: 认证失败: <已脱敏>
```

> 如果哪天你不小心把密钥提交上去了：**立刻去 platform.deepseek.com 吊销那个 key 重新生成**，
> 再改代码 —— 光删掉提交记录没用，key 已经进了 git 历史，等于已泄露。

## 两条不能破的规矩

1. **半双工**：它说话时必须关麦。用外放喇叭又开着麦，它会听见自己，然后自己唤醒自己。
   现在的串行循环天然满足这一点，改代码时别破坏它。
2. **首句 2.5 秒内出声**：回答要按句切分，出一句念一句，不要等整段生成完。
   分句逻辑在 `xiaoyu/text.py`，别绕过它。

---

## 记忆与备份

- 对话存在 `data/xiaoyu.db`（SQLite，不进 git）。**启动时会把最近 `max_history` 条读回上下文**
  —— 所以关掉程序再开，它还记得你。
- 每次启动还会告诉模型"现在几点、上次聊天是多久以前"（`xiaoyu/text.py` 的 `describe_now` /
  `describe_last_seen`）。时间每轮现取，不是启动时算一次。
- 每天第一次打开时会自动备份成 `data/xiaoyu.db.YYYYMMDD.bak`，**当天不覆盖**——
  当天第一份通常才是干净的那份，覆盖它等于把好备份换成坏备份。
- **`--text` 键盘模式也会写记忆**，和语音模式共用同一个库。调试时说的话会进入它的上下文，
  想干净地试就先备份一份 `data/xiaoyu.db`。
- 断网 / 欠费 / 超时不会静默：它会说一句"我这会儿连不上脑子了，等一下再喊我"
  （映射逻辑在 `xiaoyu/llm/client.py` 的 `_speakable_error`）。认不出的异常照旧抛堆栈。

---

## 文档

- `docs/总方案_v2_从零开始.md` —— 完整路线、硬件清单、从零装环境的每一步（**环境搭建部分已过时**）
- `TODO.md` —— 想做但先记着的事（含"迁移到虚拟环境"）
