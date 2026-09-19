# A 档硬件到货 · 开工指南

> **这一份是给"下一个 AI 对话"看的。** 硬件到货那天，只读这一份 + 仓库里的
> [`HANDOFF.md`](../HANDOFF.md)，就能接上，**不需要翻聊天记录**。
>
> - 采购/接线/刷机的明细：[`A档购物清单.md`](A档购物清单.md)
> - 项目全景 + 踩过的坑：[`HANDOFF.md`](../HANDOFF.md)
> - 长期待办池：[`TODO.md`](../TODO.md)
> - 为什么要买这套硬件：[`离开电脑_计划.md`](离开电脑_计划.md) §4.3

---

## 0. 一句话背景

XiaoYu Robot = Python 写的桌面陪伴机器人（对话 / 记忆三层 / 情绪 / 表情脸 / 眼睛跟随 /
常驻自愈，**309 个测试全绿**）。

A 档是花 **¥95** 给它买一个"身体"：**ESP32-S3 小板当耳朵和嘴巴**（板子上跑离线唤醒 +
硬件 AEC，能全双工打断），**脑子仍然是我们这套 Python**。设备端刷上游开源固件
[`78/xiaozhi-esp32`](https://github.com/78/xiaozhi-esp32)（MIT，现成的、不用写 C++），
**我们只写服务端**（Python，正是我们的主场）。

> **这一档要验证的只有一件事：能不能把这小板接到「我们自己的」服务端。**
> 通了才买舵机和壳（C 档）；不通就别往下买。

---

## 1. 环境事实（先记住，都是踩出来的）

- 仓库：`D:\JavaAI\XiaoYu Robot` —— **路径里有空格，所有命令都要加引号**
- Python：`py -3.11`（= `C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe`）
- 终端中文乱码就加：`$env:PYTHONIOENCODING='utf-8'; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8`
- **`apply_patch` 在带空格路径下不可靠** → 改代码用 Python 脚本做精确替换
  （替换前先 `assert s.count(old) == 1`，替换后跑测试）
- **commit message 用 `git commit -F <临时文件>`** —— `-m` 里的中文会被 PowerShell 吃掉
- **`curl.exe` 在本机到 GitHub 是被挡的，但 PowerShell 的 `Invoke-WebRequest` 正常**
  （GitHub 时通时不通；`git push` 一般没问题）
- 本机 WLAN：`10.35.76.134`（手机/平板连同一个路由器才打得到脸页 `http://10.35.76.134:8765/`）
- **铁律**：① 禁止 `print()`，一律 `logger = get_logger(__name__)`；② API key 不硬编码；
  ③ `.env` 永不提交；④ 改完**跑测试 + commit + push**
- **测试基线**：`py -3.11 -m unittest discover tests` 与 `py -3.11 -m pytest tests -q`
  必须都是 **309 全绿**，两个数字要一致
- 临时产物一律放 `.scratch/`（已 gitignore）：`mkdir .scratch` 随便用，别污染仓库

---

## 2. 手上有什么（2026-09-19 下单，¥95.15 全包邮）

| 件 | 规格 | 平台 |
|---|---|---|
| ESP32-S3 开发板 | 鹿小班 · 乐鑫原装 **N16R8**（16MB Flash + 8MB 八线 PSRAM）· 【向下焊】排针 · Type-C 口 | 天猫 |
| 麦克风 | **INMP441** 模块（I2S 数字麦，已焊排针） | 天猫 |
| 功放 | **MAX98357A** 模块（I2S，已焊排针，绿色螺丝端子接喇叭） | 拼多多 |
| 喇叭 | 2W / 8Ω · 直径 28MM · 杜邦公头（带针） | 拼多多 |
| 彩屏 | 2.4 寸 **ST7789 SPI 240x320**，已焊排针 | 拼多多 |
| 面包板 | MB-102（830 孔） | 拼多多 |
| 杜邦线 | 20CM；20P 三款组合（公对公/公对母/母对母，共 60 条） | 拼多多 |

**板子为什么必须是 N16R8**：固件里写死了 `CONFIG_ESPTOOLPY_FLASHSIZE_16MB` +
`CONFIG_SPIRAM_MODE_OCT`（八线 PSRAM）。买 N8R2 之类刷进去跑不起来。

---

## 3. 到货验收（先做这个，10 分钟）

- [ ] 拆包**核对型号丝印**：板子 `N16R8`、屏 `ST7789`、功放 `MAX98357A`、麦 `INMP441`
- [ ] 板子**先单独插电脑、先不接线** → 设备管理器里有没有出现 COM 口
      - 没有 → 装 USB 转串口驱动（看板子上 USB 口旁边那颗芯片的丝印：
        `CH343/CH340` 或 `CP2102`，按型号装对应驱动）
- [ ] 有发错货 / 少件 → 立刻找商家。**拼多多的单是"先用后付"，验收没问题再确认收货**

---

## 4. 接线（**必须先断电**，I2S / SPI 都不支持带电插拔）

引脚**不是随便定的**，是固件里写死的（`main/boards/bread-compact-wifi-lcd/config.h`）。
照着接，接线表也在 [`A档购物清单.md`](A档购物清单.md) §三。

**INMP441（麦克风）**

| 模块脚 | ESP32-S3 |
|---|---|
| WS | GPIO **4** |
| SCK / BCLK | GPIO **5** |
| SD / DIN | GPIO **6** |
| L/R | GND（选左声道） |
| VDD | **3V3（只能 3.3V，别接 5V）** |
| GND | GND |

**MAX98357A（功放）**

| 模块脚 | ESP32-S3 |
|---|---|
| BCLK | GPIO **15** |
| LRC / LRCLK | GPIO **16** |
| DIN | GPIO **7** |
| SD | **3V3**（不接会静音） |
| GAIN | 悬空（默认 9dB） |
| VIN | 5V（或 3V3 都行） |
| GND | GND |
| 喇叭 | 接**绿色螺丝端子**（+ / −），把喇叭那两根针插进去拧紧 |

**ST7789 彩屏**

| 屏脚 | ESP32-S3 |
|---|---|
| CS | GPIO **41** |
| RST / RES | GPIO **45** |
| DC / RS | GPIO **40** |
| SDA / MOSI | GPIO **47** |
| SCL / CLK | GPIO **21** |
| BLK / BL（背光） | GPIO **42** |
| VCC | 3V3 |
| GND | GND |

板载 **BOOT 键（GPIO0）** 留着进下载模式/复位，不用自己接。
板子【向下焊】是给面包板用的：排针插进面包板，再从面包板排孔引杜邦线到各个模块。

---

## 5. 刷固件（要下的东西**已经预先下好了**）

在 `.scratch/firmware/`（不进 git，但机器上现在就有）：

- `v2.5.0_bread-compact-wifi-lcd.zip`（2.4MB）→ 解压只有一个 `merged-binary.bin`（9.6MB）
- `tools/flash_download_tool.zip`（24.7MB）→ 乐鑫官方烧录工具

**步骤**

1. 解压烧录工具 → 打开 Flash Download Tool → 芯片选 **ESP32-S3** → Develop
2. 文件选 `merged-binary.bin`，**烧写地址填 `0x0`**
3. 选板子那个 **COM 口**，波特率 **921600**
4. 先 **ERASE**，再 **START**，等它烧完（约 1-2 分钟）
5. 烧不进去、一直等连接 → 按住板子上的 **BOOT** 键再插 USB，进下载模式重烧

固件版本：`v2.5.0`（对应变体 `bread-compact-wifi-lcd`，屏配的就是 ST7789 240x320）。
若之后 GitHub 通了想换新版本，找同名的 `vX.Y.Z_bread-compact-wifi-lcd.zip` 即可。

---

## 6. 先验硬件（跟我们的代码无关）

1. 拔插一次板子 → 它会开一个 WiFi 热点 → 手机连上 → 在配网页填家里 WiFi
2. 喊 **"你好小智"**（默认唤醒词）→ 它答应、能对话 = **硬件全好**
3. 这一步走的是它的**官方云**（xiaozhi.me）。看到这一步成了，A 档就成了一半

**万一没声**：先去串口监视器看日志（烧录工具里不带，用 `py -3.11 -m serial.tools.miniterm`
或任意串口助手，115200）。麦克风/喇叭最常错的就是 **GPIO 接错**和 **SD 脚没拉高**。

---

## 7. ✅ 真正的任务：把设备接到我们自己的服务端

**目标**：设备喊"你好小智" → 音频送到**我们的 Python** → 用**我们的**记忆 / 性格 / 情绪
→ 我们合成的声音从设备喇叭出来 → 说半句能打断。

**协议**（小智仓库 `docs/websocket_zh.md`，用 `Invoke-WebRequest` 下下来读）：

- 设备连上后先发 `hello`（JSON：`version` / `transport: websocket` / `audio_params`
  `{format: opus, sample_rate: 16000, frame_duration: 60}`）
- 服务端回一条 `hello`（带 `transport` + `session_id` + 输出的 `audio_params`（24000））
- 之后：**二进制帧 = Opus 音频**，**文本帧 = JSON**（聊天/TTS/STT 事件/MCP）
- 请求头：`Authorization: Bearer <token>` / `Protocol-Version` / `Device-Id` / `Client-Id`
- 另有 `docs/mqtt-udp_zh.md`。**先只做 WebSocket，别一开始就上 MQTT**

**怎么落地（建议，动手前先跟用户对一次）**

- 新建包 `xiaoyu/xiaozhi/`（协议解析 + 服务端），**先把纯逻辑写出来 + 单测**，
  不要一上来就动 `app.py` 里现有的语音链路
- 复用现成的：`llm/client.py`（DeepSeek 流式 + 情绪标记）、`memory/*`、`tts/*`、
  `face/protocol.py` 的情绪定义
- Opus 编解码要新依赖（`opuslib` / `pyogg`）—— **单独一步，装完立刻写个最小单测**
- 最后才用**一个开关**接进 `app.py`，默认关，保证老链路一行不受影响

**两个"白给"的好消息**（已核实，省得重新查）

1. **表情能换成我们自己的**：官方有单页工具
   [`78/xiaozhi-assets-generator`](https://github.com/78/xiaozhi-assets-generator)，
   **纯浏览器本地生成 `assets.bin`**、无需后端，一次打包表情包 + 唤醒词 + 字体 + 背景；
   表情集合 21 个位置，**我们的 5 个情绪（`happy/sad/angry/surprised/neutral`，
   见 `xiaoyu/llm/emotion.py` 的 `VALID_MOODS`）正好是它的子集**，映射表是白给的
2. **唤醒词能改成"小柚子"**：S3 支持 MultiNet 自定义唤醒词（拼音输入）。
   我们 PC 端 KWS 现在用的词就是「小柚子」（`config/keywords.txt`），两边能统一。
   （**只支持 S3/P4，C3 不行**）

**这一档的验收标准**：喊「小柚子」→ 它用**我们的记忆和性格**回答 → 能打断。

---

## 8. 别忘的并行事项

- **浸泡验收**：本体正在跑一周的常驻验收，**截止 2026-09-23 16:41**。
  **在那之前不要重启本体**（重启要留到那天一次性生效 S6a 眼睛镜像修复 + 平板端自适应）。
  查状态：`py -3.11 scripts\install_autostart.py --status`
- **常驻监控自动化**：`C:\Users\Administrator\.codex\automations\automation\automation.toml`
  （heartbeat，每 6 小时，异常才出声）。**别重复创建**，要改用
  `mcp__codex_app__automation_update`（`mode=update`, `id=automation`）
- **平板端（甲路线）**：同一 WiFi 打开 `http://10.35.76.134:8765/` → 加主屏幕 → 息屏改"永不"
  → 系统音量关 0 → 立起来挂一天。这一步和 A 档**互不干扰**，可以先做

---

## 9. 已经定过的事（**别再问用户、别重新论证**）

- **买什么已定**，7 件已下单，别再比较硬件方案
- 路线：**设备端画脸（乙/丙）+ 我们的大脑**；平板那条（甲）并行保留，不冲突
- 唤醒词目标：**「小柚子」**
- 先 **WebSocket**，不做 MQTT
- **引脚不许改**（固件写死），要按 §4 接；硬件装反只允许用配置项（例如
  `XIAOYU_BODY_INVERT_PAN`），**不许在代码里到处反符号**（S6a 那次返工的教训）
- `xiaoyu/body/` 那层"身体接口层"已经做好了（`ServoBackend` + 假舵机 + gaze→转头角度，
  默认关）。真舵机买回来时**只需换 backend**，不要去改对话逻辑

---

## 10. 到货那天的顺序（照这个抄）

1. 拆包核对型号 → 板子插电脑认 COM 口 → 有问题先找商家
2. 断电接线（§4）→ 检查有没有接错（尤其 INMP441 的 3V3 和功放的 SD 脚）
3. 刷 `merged-binary.bin`（§5）→ 配网 → 喊「你好小智」验硬件（§6）
4. 通了 → 开始写 `xiaoyu/xiaozhi/`（§7），**先纯逻辑 + 单测**
5. 每次改动都跑 309 测试 → commit → push；进度写回 `HANDOFF.md` 和 `TODO.md`

**卡住了怎么办**：先看 `HANDOFF.md` §5「已经踩过的坑」和 §8「参考过的开源项目」，
再决定要不要问用户。能自己查的（固件引脚、协议文档）都能用 `Invoke-WebRequest` 从
小智仓库直接下下来读。