# `tests/fixtures/xiaozhi/` —— 小智设备协议的真实 JSON 样本

**这些不是编的，是从上游协议文档里逐字抄下来的。** 目的：`xiaoyu/xiaozhi/` 那层
协议解析写单测时，手上有**文档级的真样本**可用。自己现编 JSON 去测自己写的解析器，
等于用自己的理解验证自己的理解 —— 测不出东西，还容易把理解错的地方焊死。

## 来源

| 项 | 值 |
|---|---|
| 仓库 | [`78/xiaozhi-esp32`](https://github.com/78/xiaozhi-esp32)（MIT） |
| 文件 | `docs/websocket_zh.md` |
| 原始 URL | `https://raw.githubusercontent.com/78/xiaozhi-esp32/main/docs/websocket_zh.md` |
| 抓取日期 | 2026-09-19 |
| 抓取方式 | `Invoke-WebRequest`（本机 `curl.exe` 到 GitHub 被挡） |
| 原文快照 sha256 | `d5791528bc41b507dd9b3703da1992190c8436559f2b0afdcc7e78acabdc5c32` |
| 原文快照字节数 | 18021 |

快照本身**没进仓库**（放在 `.scratch/xiaozhi/websocket_zh.md`，`.scratch/` 已 gitignore）。
要验真：把上面那个 URL 重新拉下来、算 sha256 对上，再照下面的办法比对即可。

## 抄写办法（可复核，不依赖任何一次性脚本）

1. 把原文里所有 ```json 代码块，以及行内反引号包起来的 `{...}` 单行样本抽出来；
2. 逐个 `json.loads` 确认是合法 JSON，且 `type` 字段存在且非空
   （文档 §8.6：缺 `type` 的设备端只记一条错误日志、不执行业务）；
3. 原样写入对应文件，只在结尾补一个换行 —— **不重排、不美化、不改字段**。

"抄错"这件事是能当场查出来的：**每个文件的内容（去掉首尾空白）都必须逐字出现在原文里**，
对不上就说明被改过。改过就不叫"来自文档"了。

## 文件清单

| 文件 | `type` | 字节 | sha256(前12) | 文档出处 |
|---|---|---|---|---|
| `device/device_hello_full.json` | `hello` | 405 | `23c5215c535b` | §1 第3步 · 设备端 hello 完整示例（`features`: mcp+glyph_push、`text_font`） |
| `device/device_hello.json` | `hello` | 285 | `4bf82d0214c8` | §4.1.1 设备端→服务器 · Hello（`features`: mcp） |
| `device/device_listen_start_manual.json` | `listen` | 111 | `696a5e68ba30` | §4.1.2 设备端→服务器 · Listen（state=start, mode=manual） |
| `device/device_abort.json` | `abort` | 99 | `89c265b15bf7` | §4.1.3 设备端→服务器 · Abort（reason=wake_word_detected） |
| `device/device_listen_detect.json` | `listen` | 118 | `43670ada865d` | §4.1.4 设备端→服务器 · 唤醒词命中（state=detect） |
| `device/device_mcp_result.json` | `mcp` | 279 | `931b89fceb96` | §4.1.5 设备端→服务器 · MCP result（JSON-RPC 2.0） |
| `device/device_listen_start_auto.json` | `listen` | 99 | `ed60b2f2115b` | §9.3 设备端→服务器 · Listen（state=start, mode=auto） |
| `server/server_hello_24000.json` | `hello` | 220 | `5d16b3447f28` | §1 第4步 · 服务器 hello 应答（`sample_rate`=24000） |
| `server/server_hello_16000.json` | `hello` | 169 | `82d21bc149b2` | §9.2 服务器→设备端 · hello 应答（16000，且无 channels/frame_duration） |
| `server/server_stt.json` | `stt` | 84 | `75511c9bb4ad` | §9.4 服务器→设备端 · STT 结果 |
| `server/server_stt_compact.json` | `stt` | 52 | `6d3c37bb1c74` | §4.2.2 服务器→设备端 · STT（单行紧凑写法） |
| `server/server_llm_emotion.json` | `llm` | 73 | `c26acb783a74` | §4.2.3 服务器→设备端 · LLM 表情（emotion=happy） |
| `server/server_tts_start.json` | `tts` | 75 | `d3fa88650079` | §9.5 服务器→设备端 · TTS start |
| `server/server_tts_start_compact.json` | `tts` | 55 | `b8285c4a09e3` | §4.2.4 服务器→设备端 · TTS state=start（单行紧凑写法） |
| `server/server_tts_start_no_session.json` | `tts` | 31 | `348ead1d7ca1` | §6 第3条 · TTS start（**无 session_id**，状态机流转示例） |
| `server/server_tts_sentence_start.json` | `tts` | 79 | `cad6a6406950` | §4.2.4 服务器→设备端 · TTS state=sentence_start |
| `server/server_tts_stop.json` | `tts` | 74 | `cb10360426a0` | §9.6 服务器→设备端 · TTS stop |
| `server/server_tts_stop_compact.json` | `tts` | 54 | `87eb5c3c09f2` | §4.2.4 服务器→设备端 · TTS state=stop（单行紧凑写法） |
| `server/server_tts_stop_no_session.json` | `tts` | 30 | `6596481e8606` | §6 第4条 · TTS stop（**无 session_id**） |
| `server/server_mcp_tools_call.json` | `mcp` | 292 | `56b8587deb07` | §4.2.5 服务器→设备端 · MCP tools/call（self.light.set_rgb） |
| `server/server_system_reboot.json` | `system` | 89 | `a56a6939014c` | §4.2.6 服务器→设备端 · System（command=reboot） |
| `server/server_custom.json` | `custom` | 129 | `d2ee7a5ec7e0` | §4.2.7 服务器→设备端 · Custom（payload.message） |

## 怎么用

`xiaoyu/xiaozhi/protocol.py` 的测试直接 `json.load` 这些文件当输入 / 期望值：

- `device/` 是**设备发给我们**的 —— 喂给解析函数，断言解析出来的字段
- `server/` 是我们**要构造并回给设备**的 —— 拿构造结果对着比字段

有两处刻意的多样性，**别当重复噪声删掉**：

- **紧凑 vs 缩进**两种写法（`*_compact.json`）：JSON 不在乎空白，
  解析器（和构造器）两种都得认；
- **有 / 无 `session_id`**（`*_no_session.json`）：文档 §6 的状态流转示例就是不带
  `session_id` 的，解析器不能因为缺这个字段就炸。

## 没做成 fixture 的两处（别以为是漏了）

1. **`{"type": ...}`**（文档 §8.6 举的"缺 type"反例）**不是合法 JSON**，是占位符，
   所以没落盘。坏 JSON / 缺字段的用例由协议测试自己构造，注释里引 §8.6。
2. **二进制协议版本 2 / 3 的结构体**（§3.2 / §3.3）是 C 结构体描述、不是 JSON 样本，
   也没落盘。第一版只走 §3.1（版本1：直接发裸 Opus 数据），那两版真要用到再说。

## 一条必须记住的前提

文档开头自己写着：

> 该文档仅基于所提供的代码推断，实际部署时可能需要结合服务器端实现进行进一步确认或补充。

也就是**这份文档是上游从自己代码里反推出来的，不是官方保证**。所以：

- 板子到了之后，第一件事是**抓一次真机的 hello 原文**，跟 `device/device_hello*.json` 对一下；
- 真机跟文档不一致 → **以真机为准**，回来更新 fixture，并在提交信息里写清楚差异。
