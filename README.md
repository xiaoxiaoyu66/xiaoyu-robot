# XiaoYu Robot

一台放在书桌上的陪伴机器人。你喊它名字，它醒过来；你跟它说话，它答你；它记得你上次说过的事。

自用项目，慢慢做，不赶进度。

---

## 现在做到哪一步了

| 阶段 | 内容 | 状态 |
|---|---|---|
| S0 | 音频自检（能录能放） | 代码已就位，待跑通 |
| S1 | 能听会说（识别 + 合成） | 模块已就位，待接线 |
| S2 | 有脑子（DeepSeek 流式对话） | 已完成 |
| S3 | 唤醒词（喊名字才醒） | 已完成 |
| S4 | 性格 + 长期记忆 | 性格已完成；向量检索待做 |
| S5 | 表情脸（Live2D） | 未开始 |
| S6 | 眼睛 / 独立成体 | 未开始 |

> 详细路线见 `docs/总方案_v2_从零开始.md`。

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
│  ├─ wake/                唤醒词（sherpa-onnx KWS）
│  ├─ asr/                 语音转文字（SenseVoice）
│  ├─ tts/                 文字转语音（edge-tts）
│  ├─ llm/                 大模型对话（DeepSeek 流式）
│  ├─ memory/              记忆（SQLite，向量检索预留）
│  └─ vision/              视觉（S6 占位）
├─ scripts/
│  ├─ check_audio.py       S0 音频自检
│  └─ download_models.py   下载模型
├─ config/
│  ├─ keywords.txt         唤醒词（改这里就能换名字）
│  ├─ persona.md           小宇的性格（改这里就能换人格）
│  └─ persona_examples.md  另外两套性格模板
├─ tests/                  测试（零依赖，随时能跑）
├─ models/                 模型文件（不进 git）
├─ data/                   SQLite、TTS 缓存（不进 git）
├─ logs/                   日志文件（不进 git）
└─ docs/                   设计文档
```

**分层原则**：`app.py` 负责编排，各子包只负责自己的那一件事，互不越界。
要换掉某个部件（比如把 edge-tts 换成 Piper），只改对应子包，其他地方不动。

---

## 快速开始

```powershell
# 1. 建虚拟环境（只需一次）
cd "D:\JavaAI\XiaoYu Robot"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 如果报"禁止运行脚本"：Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# 2. 装依赖
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
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
| 唤醒词（比如改成"二娃"） | `config/keywords.txt` |
| 小宇的性格 | `config/persona.md` |
| API Key / 日志级别 / 麦克风编号 | `.env` |
| 采样率、静音阈值、音色、模型名 | `xiaoyu/config.py` |

`config/keywords.txt` 的格式：

```
关键词 :阈值 #增强 @显示名
小宇 :2.5 #0.5 @小宇
```

阈值越大越难唤醒（越不容易误触发），一般 2.0 ~ 4.0 之间调。

---

## 测试

零依赖，随时可跑：

```powershell
python -m unittest discover tests -v
```

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

## 两条不能破的规矩

1. **半双工**：它说话时必须关麦。用外放喇叭又开着麦，它会听见自己，然后自己唤醒自己。
   现在的串行循环天然满足这一点，改代码时别破坏它。
2. **首句 2.5 秒内出声**：回答要按句切分，出一句念一句，不要等整段生成完。
   分句逻辑在 `xiaoyu/text.py`，别绕过它。

---

## 文档

- `docs/总方案_v2_从零开始.md` —— 完整路线、硬件清单、从零装环境的每一步