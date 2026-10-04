# AGENTS.md — 微信客服助手（wechat-cs）

> 这份文件会被 agent 自动加载。**在你做任何事之前，先读完「一、铁律」。**

---

## 一、铁律（最重要，违反会造成真实损害）

### 1. 绝不在没有人类明确同意的情况下发送任何微信消息

这个项目连着**真人正在使用的微信号**。历史上发生过一次事故：agent 自作主张
往「文件传输助手」和另一个会话发了约 15 条测试消息，用户明确提出
**「你不要给我乱发信息，发信息的时候一定要通过我的同意」**。

所以：

- 任何会调用 `adapter.send()` 的操作，**动手前必须先问**
- 包括但不限于：测试发送、验证通道、"就发一条试试"
- 「验证发送功能」不是理由，先问
- 只读操作（截屏、OCR、读消息）不需要问，但**不要打开白名单外的会话**

### 2. 白名单外的东西碰不得

`config/chats.json` 是会话白名单。**名单外的会话，程序打不开、读不到**，
这是用户明确要求的设计。不要绕过它，不要为了方便临时把会话加进去 ——
要加必须先问用户。

### 3. 默认配置就是最安全的，不要"为了跑通"把它放松

```env
DRY_RUN=1                    # 只出草稿，绝不发送
WECHAT_SEND_ALLOWLIST=       # 空 = 一个都不许发
WECHAT_GUARD=whitelist       # 名单外拒绝
```

**不要为了让某个测试通过而改这些值。** 如果测试因为风控过不去，那是风控在
正常工作 —— 改测试，不是改风控。（真实例子：加完六项风控后 4 个测试失败，
原因是当时是凌晨、命中夜间静默。正确的修法是测试里设 `QUIET_HOURS=`，
而不是关掉风控。）

### 4. 改配置不要碰 `.env`

`.env` 里是用户的**真实 API Key**。它是 gitignore 的，不要读出来展示，
不要提交，不要复制到别处。要改配置就用 `.env.example` 或让用户自己改。

---

## 二、这个项目是什么

让 AI 在**普通个人微信**里替快递网点客服干活：读懂商家消息、查真实物流轨迹、
调用网点内部系统，生成靠谱回复；涉及真操作的诉求自动转人工，绝不编造事实。

核心原则：**模型只负责理解和组织语言；能不能查、能不能发，全由代码管。**

```
微信消息 → 截屏+OCR读出来 → 白名单检查 → 去重 → 合并窗口
        → 组装上下文 → 调大模型(带工具) → 可能查物流/内部系统
        → 代码校验事实 → 策略层决定能不能发 → 人在审核台点发送
```

---

## 三、你现在最可能在做的事：验证 Windows 通道

**Windows 通道的代码写完了，但一次都没在真实 Windows 上跑过。**
作者只有 macOS，写得出代码但测不了。**你的任务就是把它验证通。**

详细步骤见 **[docs/Windows验证交接说明.md](docs/Windows验证交接说明.md)**，
按那里的清单走。

一句话版本：

```bat
:: 1. 装依赖（requirements.txt 按平台标记，Windows 会自动选对）
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt

:: 2. 逐项诊断（按顺序，每项都要过）
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --check
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --window
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --shot
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --ocr
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --layout   ← 最关键
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --list
```

**`--layout` 是核心**：它会在截图上画出假定的分区边界（红线=列表边界，
蓝线=标题栏/输入区，绿圈=输入框，紫圈=发送按钮）。你可以用图像工具直接看
那张图，对照着调 `adapters/vision_common.py` 里 `DEFAULT_LAYOUT` 的数值。

---

## 四、改代码前先跑测试

离线回归测试有 55 项，**不联网、不花钱、几秒钟跑完**：

```bash
./run.sh sim        # 期望 55/55 通过（macOS/Linux）
# Windows 上等价命令：
.venv\Scripts\python.exe tests\simulate.py
```

**改完代码必须跑一遍。** 它覆盖了去重、连发合并、指代消解、人工接管、
高频拦截、OCR 抖动容错、会话名匹配、六项风控 —— 这些逻辑在 macOS 上
被真实数据反复验证过，改坏了很难在 Windows 上发现。

其余测试：

| 命令 | 内容 | 花钱吗 |
|---|---|---|
| `tests/simulate.py` | 离线回归 55 项 | 不花钱 |
| `tests/run_cases.py` | 模型契约 4 条（含提示词注入） | 花钱 |
| `tests/run_business.py` | 业务场景 18 条 | 花钱 |
| `tests/run_conversation.py` | 多轮对话 3 段 | 花钱 |

**跑花钱的测试前先问用户。** 走的是用户的 DeepSeek 余额。

---

## 五、目录导航

| 想改什么 | 改哪里 |
|---|---|
| AI 的性格、事实铁律 | `app/prompts.py` |
| 什么能发、什么不能发 | `app/policy.py` |
| 模型调用、工具循环、事实校验 | `app/llm.py` |
| 消息编排、去重、合并窗口 | `app/pipeline.py` |
| 六项风控参数 | `app/config.py`（搜"风控"） |
| **两种截屏通道共用的算法** | `adapters/vision_common.py` |
| macOS 通道的平台层 | `adapters/macos_vision.py` |
| **Windows 通道的平台层** | `adapters/windows_vision.py` |
| 会话白名单 | `bridge/guard.py` |
| 模型配置 | `config/models.json` |
| 网点知识库（价格/时效口径） | `config/knowledge.md` |
| 外部系统接入 | `config/integrations.json` |

**注意**：气泡解析、左右判断、增量比对、会话名匹配这些**与平台无关的逻辑都在
`adapters/vision_common.py`**，两个通道共用。改它们的算法会同时影响 macOS 和
Windows，**改完一定要跑 `simulate.py`**。

---

## 六、踩过的坑（别再踩一遍）

这些都是实测才发现的，看代码看不出来：

1. **微信 Mac 的回车是换行不是发送**（Windows 相反，默认是发送）。
   所以代码统一走「点发送按钮」，不按回车。而且发送后要**回读消息区**
   确认（不含输入框）—— 第一版按回车后 OCR 读到了输入框里的字，
   看起来"发送成功"其实是假象。

2. **OCR 抖动会让精确比对整段崩掉。** 一次截图把「末」认成「未」，
   增量比对就对不上，代码的选择是"不回复" —— 消息静默丢了。
   所以所有文本比对都走模糊匹配（`_similar`），短消息额外容忍一个错字。

3. **群名会被截断**（列表里显示 "某电商福利群6禁广告.."）。所以会话名匹配
   是**前缀容忍**的（`title_ok`，最少 4 个字），从列表扫出来的名字
   要把尾部省略号剥掉。

4. **人在用微信时会随时切走会话。** 不检查的话会把别的会话的消息当成
   这个会话的新消息发出去 —— 这是灾难。所以 `read_messages` 每次都在
   **同一帧**里校验会话身份，对不上就返回空。

5. **打开会话后按 ESC 会把聊天面板关掉**，变成"列表选中但右侧空白"。
   已经去掉了 ESC。

6. **`_env()` 用 `or default` 会把空字符串当成"没设置"**，导致
   用户想关掉夜间静默时段也关不掉。所以有 `_env_allow_empty()`，
   区分"没设置"和"显式设空"。

7. **不要信模型给的标签。** 实测模型把「先别退了，继续送」标成
   `intent=other` 而不是 `cancel_return`。如果策略层只看标签，
   这条涉及**真实退回操作**的消息就会被自动发出去。所以
   `policy.py` 里有一道只看文字、不看模型分类的高危词兜底。

---

## 七、这个项目已经验证过的部分（不要"顺手重构"）

| 部分 | 状态 |
|---|---|
| 客服核心（`app/`） | 离线 55/55、业务 18/18、模型契约 4/4 |
| macOS 截屏通道 | 实机验证（真实收发、人在用微信时切走、OCR 抖动） |
| 多轮对话 | 2~3 / 3（**不稳定，约每十几轮出一轮问题，这是已知的**） |
| 六项风控 | 55 项测试覆盖 |
| 会话白名单 + 一键添加 | 实机验证 |
| **Windows 通道** | **未验证 —— 这是你来这里的原因** |
| 物流真实接入 | 未接，默认 mock 假数据 |
| 网点知识库 | 空的，价格/时效问题答不了 |

---

## 八、还没做的事（路线图，别自作主张开工）

- 图片/语音/文件消息处理（现在会静默跳过，这是硬伤）
- 读会话列表未读红点，只打开有新消息的会话（解决规模化瓶颈）
- 物流配置化（`config/logistics.json` + `./run.sh logistics check`）
- 按意图分流模型

**要做这些先问用户。** 用户当前的目标是「先让 Windows 跑通给朋友看」。
