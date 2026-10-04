# Windows 通道验证交接说明

> 给在 Windows 机器上运行的 agent。目标：**把 Windows 截屏通道验证到能用。**

---

## 0. 先读这些

1. 项目根目录的 `AGENTS.md` —— **铁律在里面，先读完**
2. 本文档 —— 你要做的具体事
3. `README.md` —— 项目整体说明

**你的任务边界**：只验证和修好 Windows 通道。**不要顺手重构别的部分**，
`app/` 和 `adapters/vision_common.py` 的算法在 macOS 上被真实数据验证过
（见 AGENTS.md 第七节）。

---

## 1. 背景：为什么这件事必须由你来做

Windows 通道的代码写完了，但**作者只有 macOS，一次都没在真实 Windows 上跑过**。

代码长这样：

```
adapters/vision_common.py     ← 与平台无关的算法（macOS 上验证过）
        ├── adapters/macos_vision.py    ← macOS 平台层（验证过）
        └── adapters/windows_vision.py  ← Windows 平台层（★ 没验证过）
```

`vision_common.py` 里的东西都是实测踩出来的：气泡解析、左右判断、
增量比对、会话名匹配、OCR 抖动容错。**这些不用你管，是对的。**

你要验证的是 `windows_vision.py` 里那些**平台相关的假设**：

| # | 未验证的假设 | 怎么验 |
|---|---|---|
| 1 | `DEFAULT_LAYOUT` 的版面数值 | `--layout` 看图 |
| 2 | 窗口类名（微信 4.x 的类名是猜的） | `--window` |
| 3 | 高 DPI 缩放下的点击坐标 | 看显示缩放 + 实点测试 |
| 4 | OCR 坐标换算（像素 → 归一化左下原点） | `--ocr` 看 y 值大小关系 |
| 5 | 回车行为（Windows 默认发送，代码走按钮） | 实发测试（**先问用户**） |

---

## 2. 验收标准

做完之后要满足：

- [ ] `--check` 全绿（依赖齐、能找到微信窗口）
- [ ] `--shot` 截出来的是完整微信窗口，没被遮挡
- [ ] `--ocr` 能正确识别中文，坐标合理
- [ ] `--layout` 画出来的四条边界**全部对齐**（见第 5 节）
- [ ] `--list` 扫出来的会话名**认得对**（跟微信里实际的名字一致）
- [ ] `tests/simulate.py` 55/55 通过（改过代码的话）
- [ ] **（可选，需用户同意）** 真实收发一条消息验证

---

## 3. 装环境

```bat
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

`requirements.txt` 已经按平台标记过（`; sys_platform == "win32"`），
Windows 上会自动装 `pyautogui` / `pywin32` / `rapidocr-onnxruntime`，
自动跳过 macOS 的 pyobjc。

**没有 uv 的话**：

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

也可以直接用 `deploy\windows\1-install.bat`（会自动装 Python + 依赖）。

**装不上时的排查**：
- 国内网络 → 换源 `--index-url https://mirrors.aliyun.com/pypi/simple/`
- `rapidocr-onnxruntime` 要下 ONNX 模型（约 15MB），网慢会超时，重试即可
- 杀毒软件可能拦 `pyautogui` / `pywin32`，加白名单

---

## 4. 逐步诊断

按顺序跑，**每一项都要过再往下走**：

```bat
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --check
```

期望：系统信息、5 个依赖全 OK、能找到微信窗口。

失败处理：
- 「找不到微信主窗口」→ 微信没开/没登录/最小化了
- 缺依赖 → 回到第 3 节

```bat
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --window
```

期望：打印出窗口句柄、坐标、尺寸，以及版面假定值。

**记下窗口尺寸**，后面调版面要用。

```bat
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --shot
```

期望：`C:\Windows\Temp\wechat_cs_probe\probe.png` 是完整微信窗口。

**用图像工具打开这张图看一眼。** 确认：
- 是完整的微信窗口（不是黑屏、不是别的窗口）
- 左侧会话列表、右侧聊天区都在

```bat
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --ocr
```

期望：识别出几十个文本块，中文正确，前 20 条按 y 从大到小列出。

**坐标要合理**：`y` 接近 1.0 = 屏幕顶部，接近 0.0 = 屏幕底部。
如果 y 值反了（标题跑到 y≈0），说明 `_ocr()` 里的坐标翻转写错了。

```bat
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --list
```

期望：扫出来的会话名跟微信左侧列表里看到的一致。

**注意**：微信会截断显示名字（"某电商福利群6禁广告.."），代码会把省略号剥掉，
所以扫出来可能是 `某电商福利群6禁广告`。**这是正常的**，匹配是前缀容忍的。

扫出来是空 → 版面没对齐，回去做第 5 节。

---

## 5. ★ 核心：版面标定

**这是整个验证任务的关键。** Windows 版的版面跟 macOS 不一样，我给的
`DEFAULT_LAYOUT` 是按 macOS 推的，大概率要调。

```bat
.venv\Scripts\python.exe bridge\inspect_wechat_windows.py --layout
```

会在截图（`layout.png`）上画：

```
红色竖线（两条）= 图标栏右边 / 会话列表右边
蓝色横线（两条）= 标题栏底 / 输入区顶
绿色圆圈       = 输入框点击位置
紫色圆圈       = 发送按钮位置
```

**用图像工具打开 `layout.png`，对着检查四条线：**

| 画的东西 | 应该落在哪里 | 不对就调 |
|---|---|---|
| 第一条红线 | 图标栏和会话列表之间 | `rail_w`（默认 80） |
| 第二条红线 | 会话列表和聊天区之间 | `rail_w + list_w`（默认 80+226） |
| 第一条蓝线 | 标题栏下沿（"微信"两个字下面） | `title_h`（默认 48） |
| 第二条蓝线 | 聊天区和输入框之间 | `input_h`（默认 164） |
| 绿圈 | 输入框正中间 | `input_center()` |
| 紫圈 | 「发送」按钮上 | `send_button()` |

改的地方：`adapters/vision_common.py` 里的 `DEFAULT_LAYOUT`

```python
@dataclass
class Layout:
    rail_w: float = 80.0      # 左侧图标栏宽度
    list_w: float = 226.0     # 会话列表宽度
    title_h: float = 48.0     # 顶部标题栏高度
    input_h: float = 164.0    # 底部输入区高度
```

**怎么估数值**：`--window` 会打印窗口尺寸，`layout.png` 的像素尺寸跟窗口一样。
在图上看某个边界大概在横向/纵向的第几像素，就是该填的值。

**迭代方法**：改数值 → 重跑 `--layout` → 再看图，直到四条线全部贴合。

**高 DPI 注意**：如果显示缩放不是 100%，`GetWindowRect` 返回的可能是
逻辑坐标而截图是物理像素，导致点击偏移。检查方法见
`--check` 输出的「显示缩放」段。**如果缩放非 100%，这是最可能出问题的地方。**

---

## 6. 改完代码必须做的事

```bat
.venv\Scripts\python.exe tests\simulate.py
```

**期望 55/55 通过。** 这个测试不联网、不花钱、几秒跑完。

它覆盖了：去重、连发合并、指代消解、人工接管、高频拦截、OCR 抖动容错、
会话名匹配、六项风控。**你改 `vision_common.py` 的话一定要跑。**

**如果失败**：先看是不是风控在正常工作（比如凌晨跑会命中夜间静默）。
改测试，**不要改风控**。见 AGENTS.md 铁律第 3 条。

---

## 7. 真实收发测试（**必须先问用户**）

前面都过了之后，**问用户能不能发一条测试消息**。得到明确同意才做。

准备工作：

1. 确保 `.env` 里是 `DRY_RUN=1`（只出草稿，不会真发）
2. 白名单里**只加一个不重要的会话**（问用户加哪个）
3. 先只测**读**：让对方发一条消息，跑 `wechat_mac_watch.py` 的等价流程看能不能读到

要真发的时候，**必须**：把会话同时加到 `WECHAT_SEND_ALLOWLIST`，
并且把 `DRY_RUN=0`。**这两件事都要先问用户。**

`wechat_mac_watch.py` 是 macOS 的诊断工具，Windows 上跑不了。
需要的话参照它写一个 Windows 版，或者直接用审核台网页。

---

## 8. 常见故障对照

| 现象 | 可能原因 | 怎么办 |
|---|---|---|
| `--list` 扫出来是空 | 版面没对齐 | 回去做第 5 节 |
| `--list` 扫出来一堆乱码 | 把预览行当成了标题行 | 时间戳正则没匹配上，看 `_LIST_TIME_RE` |
| `--ocr` 一个字都没有 | 截图是黑的 | 微信窗口被遮挡或最小化 |
| `--ocr` 中文乱码 | RapidOCR 模型没下全 | 删掉缓存重装 |
| 点列表行没反应 | 坐标偏了（DPI） | 见第 5 节的高 DPI 注意 |
| 点开的是别的会话 | 版面偏了 / 名字匹配错 | 看 `--layout` 和 `--list` |
| `SetForegroundWindow` 失败 | Windows 的前台锁定 | 代码里有最小化再恢复的兜底 |
| 找不到微信窗口 | 类名不对 | 改 `WECHAT_WINDOW_CLASSES`，用 Spy++ 或 `--window` 的输出确认 |

---

## 9. 完成后怎么汇报

给用户一份简短报告，包含：

1. **哪几项过了**，哪几项没过
2. **改了哪些文件、哪些数值**（Layout 的四个值改成多少了）
3. **`--window` 的输出**（窗口尺寸 + DPI 缩放）
4. **`--layout` 的截图**（改好之后那张）
5. **`--list` 扫出来的会话名**
6. `tests/simulate.py` 的结果

**不要**在报告里包含 `.env` 的内容或者任何 API Key。

---

## 10. 一句话提醒

**这个项目连着真人正在使用的微信号。**

读到本文档时请回到 `AGENTS.md` 第一节再确认一遍：
**发任何消息之前必须先问用户。**

只读操作（截屏、OCR、读消息、扫描会话列表）不需要问，
但也不要打开白名单外的会话。
