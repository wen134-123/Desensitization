# 标书脱敏工具（BidRedact）PRD v2.0 —— 开发设计版

| | |
|---|---|
| 版本 | v2.0（2026-11-20），替代 v1.0 |
| 目标读者 | 开发者 / AI 编码代理（本文档粒度=照着可直接实现 M2/M3/M4） |
| 前置状态 | M1 引擎已完成并实测（详见第 3 节基线）；M2/M3/M4 未开始 |
| 决策溯源 | 11-17 立项 · 11-18 商业模式 · 11-19 技术选型 · 11-20 PRD 定稿 |

---

## 1. 产品概述

### 1.1 一句话定位
给标书做脱敏的本地桌面软件：数据不出本机、脱敏可逆、买断制、断网可用。

### 1.2 商业模式（定案，模式 B）
1. 买断制永久使用，激活含 1 年更新；续更新 ¥99/年 量级；单设备/双设备两档
2. 授权服务器**只碰钱和机器码**，永远不碰用户文档
3. 客群：投标专员、代理机构、需对外提交标书的供应商

### 1.3 硬约束（任何实现不得违反）
| # | 约束 | 实现含义 |
|---|---|---|
| C1 | 数据不出本机 | 引擎全离线；云 API 永不默认；遥测仅机器码+版本号（心跳） |
| C2 | 可逆脱敏 | 假名策略必须写映射表；映射默认加密（口令+文件双因子） |
| C3 | 断网可用 | 识别/脱敏/还原/验签离线完成；联网仅心跳 |
| C4 | 占位符规范 | 假名统一【公司A】【张某某】式，文档保持可读 |
| C5 | 仅支持 Word (.docx) | 暂不支持 PDF/Excel/图片 OCR，此类内容需人工后续处理 |

### 1.4 名词表
| 术语 | 定义 |
|---|---|
| 命中(hit) | 一次识别到的敏感片段：{原文, 类别, 识别层, 位置} |
| 策略(strategy) | 类别级替换方式：fake 假名 / star 星号 / delete 删除 |
| 映射(mapping) | {原文 → 占位符} 字典，fake 策略的可逆凭据 |
| 防漏扫描 | 脱敏后对输出全文的残留复查 |
| 心跳 | 每 7 天一次的授权状态校验联网请求 |

---

## 2. 系统总体架构

### 2.1 分层图

```
┌─ M3 桌面壳 ─────────────────────────────┐
│  desktop.py: pywebview 窗口             │
│    ↕ (加载 http://127.0.0.1:PORT)        │
├─ M2 Web 层 ─────────────────────────────┤
│  webapp.py: FastAPI (仅 127.0.0.1)      │
│    ↕ 进程内函数调用，不经 HTTP 中转引擎   │
├─ M1 引擎层 ─────────────────────────────┤
│  redactor.py   识别管道/替换/还原/报告   │
│  ner_backend.py NerBackend 可插拔接口   │
│    ├─ HanLPBackend   (M2，torch)        │
│    ├─ OnnxBackend    (M3，onnxruntime)  │
│    └─ JiebaBackend   (M3 降级兜底)      │
├─ M4 授权层（客户端）────────────────────┤
│  licensing.py 验签/心跳/状态机/防回拨    │
└─────────────────────────────────────────┘
        M4 授权服务器（独立部署）：license_server/
```

### 2.2 技术栈（锁定）
| 层 | 选型 | 版本基调 |
|---|---|---|
| 引擎 | Python 3.11+ / python-docx / cryptography | 现状延续 |
| NER(M2) | HanLP msra_ner_electra_small_zh | torch，M2 用 |
| NER(M3) | onnxruntime + 导出 ONNX 模型 | jieba 兜底 |
| Web | FastAPI + uvicorn，静态页 vanilla JS | 不引前端框架 |
| 壳 | pywebview | 系统 WebView |
| 打包 | PyInstaller | macOS .app + Windows .exe |
| 授权 | cryptography RSA-PSS + SHA256 | 公钥内置，私钥仅服务端 |

### 2.3 关键架构决策
1. **Web 层调引擎走进程内函数调用**，不走 HTTP 自调用；HTTP 仅服务浏览器 UI
2. **引擎与界面解耦**：redactor.py 保持可独立 CLI 运行（自动化/排障通道），Web 层只做编排
3. **NerBackend 可插拔**：引擎只认接口协议（见 5.4），实现可替换——这是 M3 换 ONNX 不动引擎的前提

---

## 3. 现状基线（M1 已交付，M2 的输入）

### 3.1 引擎 v3 能力清单（全部实测通过）
| 能力 | 实测结果 |
|---|---|
| 四层识别（白名单→正则→词库→NER） | 合成标书 15 处全命中 |
| NER 词库外实体 | 公司 4/4、人名 4/4、地址宽口径 3/3（街道级偶有截断）；~9s 加载、~50ms/句 |
| 混合策略 fake/star/delete | 按类别生效 |
| 口令加密映射 | PBKDF2-SHA256 20 万次 + Fernet；错口令正确拒识 |
| 防漏扫描 | ≥7 位连续数字 / 残留 @ / 已识别原文残留，三类报警 |
| 还原 | 与原文逐字一致 |
| 白名单 | WHITELIST 词条优先级最高，实测「中国银行」保留 |

### 3.2 已知边界（M2/M3 不解决、登记在案）
1. 页眉页脚未处理（python-docx 限制，需 header/footer 遍历——M2 可顺手补，见 6.6 G6）
2. PDF/Excel 不支持；批量/项目级映射不做（P2）
3. 命令行口令进 shell 历史（M2 用输入框根除）

---

## 4. 数据规范（版本锁定，向后兼容）

### 4.1 词库 vocab.txt
```
类别|词          # 每行一条，类别缺省=COMPANY
WHITELIST|词     # 白名单：绝不脱敏，优先级最高
# 注释行以 # 开头
```
- 支持类别：COMPANY / PERSON / ADDRESS / CODE（词库可扩，引擎按 CAT_CN 映射）
- 编码 UTF-8；词内不可含 `|`；空白行忽略

### 4.2 映射文件
1. **明文映射**（无口令）：`{ "原文": "占位符", ... }` 纯 JSON
2. **加密容器**（有口令）——锁定格式，还原端按此解：
```json
{
  "v": 1,
  "kdf": "PBKDF2-SHA256",
  "iters": 200000,
  "salt": "<16字节 hex>",
  "data": "<Fernet token，明文为映射 JSON>"
}
```
3. 密钥派生：PBKDF2HMAC(SHA256, 32B, salt, iters) → urlsafe_b64encode → Fernet key

### 4.3 处理报告 JSON（实际实现 v3）
```json
{
  "工具": "标书脱敏工作台",
  "引擎版本": "3.0",
  "ner_enabled": true,
  "ner_backend": "onnx",
  "时间": "2026-11-20 15:00:00",
  "源文件": "样本标书.docx",
  "替换方式": "占位符（可读、稳定）",
  "策略配置": {"公司": "假名", "人名": "假名"},
  "命中统计": {"公司(假名)": 6, "电话(假名)": 3},
  "实体数": 14,
  "防漏扫描": {"通过": true, "警告": []},
  "耗时秒": 2.34,
  "输出": {"脱敏文档": "路径", "映射文件": "路径"},
  "说明": "为避免二次泄漏，报告中的原文均按类别遮盖",
  "明细": [
    {"类别": "公司", "原文": "华信科技(遮盖)", "替换为": "【公司A】",
     "策略": "假名", "可逆": true, "出现次数": 6}
  ]
}
```
- 报告是留痕归档件；「明细.原文」已遮盖，完整原文仅保存在映射文件
- 新增字段：引擎版本 / ner_enabled / ner_backend（v2 新增，已实现）

### 4.4 输出文件命名（延续现状）
`{stem}_脱敏.docx`、`{stem}_映射.enc|.json`、`{stem}_报告.json`，还原输出由用户指定 `-o`。

### 4.5 占位符分配规则
1. fake 策略：按类别独立编号——公司A/公司B…、张某某/李某某…、地址A…、代号A…
2. 同一原文多次出现 → 同一占位符（映射一对一）；不同原文 → 不同占位符
3. 人名假名格式：姓保留 + 「某某」（张伟→张某某），保持文档语感

---

## 5. 引擎层详细设计

### 5.1 识别管道（顺序与让位规则）
```
输入: 段落文本
1) 白名单精确匹配命中 → 该片段跳过后续所有层
2) 正则硬规则命中 → 占位，片段不让位给词库/NER
3) 词库精确匹配（最长优先）→ 占位
4) NER 推理（--ner 开启时）→ 只处理前三层未覆盖片段
重叠让位: 高层已占位的区间，低层不得再占（区间合并去交）
```

### 5.2 正则规则表（锁定）
| 类别 | 模式要点 |
|---|---|
| EMAIL | 标准邮箱；`(?<!\S)`…`(?!\S)` 边界 |
| IDCARD | 18 位 `\d{17}[\dXx]`，前后非数字 |
| PHONE | 手机 `1[3-9]\d{9}`；座机 `0\d{2,3}-\d{7,8}` |
| BANK | 13-19 位数字（Luhn 校验过滤误报） |
| AMOUNT | `￥/¥/人民币` 前缀 或 `数字(万)元` 后缀 |

### 5.3 策略应用规则
| 策略 | 规则 | 可逆 |
|---|---|---|
| fake（默认） | 【类别+字母】；人名【张某某】；写入映射 | ✅ |
| star | 人名留首字（张伟→张**）；手机留前3后4（138\*\*\*\*5678）；公司留首2字（华\*\*科技）；金额→¥\*\*；不写映射 | ❌ |
| delete | 整段抹除为空；不写映射 | ❌ |
- 同类别只允许一种策略；未配置类别默认 fake
- **映射表只收录 fake 策略条目**（还原安全前提）

### 5.4 NerBackend 接口协议（M3 可插拔的契约）
```python
class NerBackend(Protocol):
    def load(self, model_dir: Path) -> None: ...          # 失败可抛 NerLoadError
    def infer(self, text: str) -> list[tuple[str, str]]: ...
        # 返回 [(片段, 类别)]，类别 ∈ {COMPANY, PERSON, ADDRESS}
    @property
    def name(self) -> str: ...                            # 报告里标注用了哪个后端
```
1. 引擎捕获 `NerLoadError` → 自动切换 JiebaBackend + 在报告「防漏扫描」追加降级警告
2. NER_STOP 过滤表延续现状（挡「项目部/负责人」类误报），随实现允许增补

### 5.5 防漏扫描（锁定三条规则 + 降级提醒位）
1. 输出全文存在 `≥7 位连续数字` → 报警
2. 输出全文存在 `@` → 报警
3. 任一 fake 映射的原文在输出中残留 → 报警
4. v2 预留：NER 降级/未开启时追加一条提示性警告

### 5.6 性能预算
| 指标 | 预算 | 备注 |
|---|---|---|
| 10 页标书端到端脱敏 | ≤ 30s | 含 NER 推理 |
| NER 冷加载 | ≤ 12s | 进程内复用，一次成本 |
| 引擎推理（不含加载） | ≤ 50ms/句 | HanLP small 实测水位 |
| 还原 10 页 | ≤ 5s | 纯字符串替换 |
| Web 首页可交互 | ≤ 3s | 本地静态资源 |

---

## 6. M2 —— Web 层（localhost，无账号体系）

### 6.1 技术约束
1. FastAPI 仅绑定 `127.0.0.1`；无登录/无 Cookie/无数据库
2. 单用户单会话假设：文件上传落 `tempfile` 工作目录，进程退出/任务完成 24h 后清理
3. 上传限制：单文件 ≤ 20MB，仅接受 `.docx`（413/415 拒绝）
4. **口令安全**：口令只在内存流转，禁止落盘、禁止进日志、禁止进报告

### 6.2 页面清单与交互规格（实际实现）
工作台采用三步引导式流程，底部固定导航；桌面壳内置 pywebview，浏览器访问同一服务。

| # | 页面/Tab | 核心元素与交互 |
|---|---|---|
| T1 | 脱敏工作台（三步） | **Step 1 上传**：拖拽/点选 → 文件校验 → 替换方式（占位符/拟真）→ NER 开关 + 补充敏感词/白名单 → 按类别策略（星号/假名/删除）→ 开始识别<br>**Step 2 复核**：实体列表（类别/原文/出现次数/搜索）→ 逐条勾选排除 → 手动补词输入框 → 底部固定「返回上传」「下一步预览」<br>**Step 3 预览导出**：左右分栏原文/脱敏对比预览 → 敏感片段彩色高亮 + 类别标签 → 每页条数 50/100/300/全部 + 页码跳转 → 右上角「导出脱敏文档」→ 口令弹窗 → 脱敏执行 + 下载卡片弹窗 |
| T2 | 还原文档 | 三要素：脱敏件 + 映射文件(.enc/.json) + 口令 → 还原下载 |
| T3 | 导出历史 | 本机已导出记录列表，每条含脱敏件/映射/报告三文件下载入口 |
| T4 | 使用说明 | 本机离线/可逆/三种策略/建议操作顺序/已知边界 |
| — | 激活弹窗 | 未激活时显示，机器码展示 + 激活码输入 + 自建服务器可选 |

### 6.3 REST API 契约（实际实现，统一前缀 `/api`）

| 方法 | 路径 | 请求 | 成功响应 | 备注 |
|---|---|---|---|---|
| GET | /api/health | — | `{ok, version, smart, ner, max_mb, local_only, license}` | 健康检查 |
| POST | /api/analyze | multipart: file + use_smart + custom_words | `{job_id, file, stats, entities, occurrences, categories, findings}` | 上传+识别，**同步**返回 |
| POST | /api/reanalyze | `{job_id, use_smart, custom_words}` | 同 analyze | 手动补词后重扫 |
| POST | /api/preview | `{job_id, mode, strategies, excluded}` | `{blocks:[...], changed_blocks, entities, occurrences}` | 对比预览，不落盘 |
| POST | /api/redact | `{job_id, mode, strategies, excluded, password?}` | `{job_id, stats, entities, occurrences, files:[{kind,url,name,size}]}` | 同步脱敏，直接返回产物 |
| GET | /api/download/{job_id}/{kind} | — | 文件流 | kind ∈ redacted/mapping/report/restored |
| POST | /api/restore | multipart: redacted + mapping + password | `{replaced, file}` | 同步还原，返回下载 |
| GET | /api/vocab | — | `{entries:[{类别,词}], whitelist:[词], path}` | 词库管理 |
| POST | /api/vocab | `{entries:[], whitelist:[]}` | `{saved, count}` | 全量替换 |
| GET | /api/exports | — | `{exports:[{job_id, filename, entities, occurrences, files:[{kind,name,size,url,exists}]}]}` | 导出历史 |
| DELETE | /api/exports/{job_id} | — | `{ok}` | 删除历史记录 |
| GET | /api/license/status | — | 授权状态 | |
| POST | /api/license/activate | `{code, server_url?}` | 授权状态 | |

**会话模型**：单用户单会话，job_id = uuid4；产物保留至进程退出 / 24h 自动清理。

### 6.4 会话与任务模型
1. `file_id`/`task_id` 用 uuid4，服务端字典 `task_id → {状态, 报告, 产物路径}`
2. 任务同步执行即可（10 页 ≤30s 在可接受等待内），响应前不锁界面由前端 loading 处理；**预留异步升级位**（status 字段即为此设计）
3. 产物命名遵循 4.4；产物保留至进程退出

### 6.5 安全清单（M2 出厂前逐条核对）
- [ ] uvicorn 仅 127.0.0.1 绑定
- [ ] 口令不落盘/不进日志/不进报告（全局搜代码确认）
- [ ] 上传文件类型白名单 + 大小上限
- [ ] 路径遍历防护（download artifact 白名单枚举，不拼用户输入路径）
- [ ] 临时目录任务后清理

### 6.6 M2 顺带引擎补丁（小改动，随 Web 一起做）
| # | 补丁 | 说明 |
|---|---|---|
| G1 | 页眉页脚遍历 | python-docx `section.header/footer` 段落纳入识别替换；报告明细加「位置: 正文/页眉/页脚」字段 |
| G2 | 表格单元格星号策略 | 确认 star/delete 在表格内生效路径与正文一致 |
| G3 | 报告加「引擎版本」「ner_backend」字段 | 见 4.3 |

### 6.7 M2 验收清单（全过才算完）
- [ ] 合成标书全流程零命令行：上传→预览→手动移除一条命中→改两类别策略→设口令→脱敏→下载
- [ ] 还原页：用下载的 .enc+口令还原，与原文逐字一致；错口令 401 且界面提示友好
- [ ] 词库页增删词条，下次预览生效
- [ ] 口令出现在内存以外的一切落盘内容中 = 0 处（grep 验证）
- [ ] P2 编辑后报告明细与编辑结果一致
- [ ] 页眉中的公司名被识别替换（G1）
- [ ] 断网（拔网线）全流程可用

---

## 7. M3 —— 桌面化与打包

### 7.1 pywebview 集成
1. `desktop.py`：子线程起 uvicorn(127.0.0.1 随机空闲端口) → `webview.create_window("BidRedact", url)` → `webview.start()`
2. 窗口关闭 → 服务线程 daemon 退出；无残留进程
3. 开发期仍浏览器直连（P7 设置页显示实际端口）

### 7.2 模型内置方案
1. 导出：`tools/export_ner_onnx.py --model hanlp小模型 --out models/ner/`（产出 model.onnx + label 映射 + tokenizer 词表）
2. 打包：PyInstaller `--add-data models/ner`；词库 vocab.txt 同打入
3. 运行时：首启解压/拷贝到用户数据目录（macOS `~/Library/Application Support/BidRedact/`，Windows `%APPDATA%/BidRedact/`），此后永久离线
4. 加载顺序：OnnxBackend → 失败 `NerLoadError` → JiebaBackend + 报告警告 + 界面横幅提示

### 7.3 打包规格
| 项 | macOS | Windows |
|---|---|---|
| 产物 | BidRedact.app（再 dmg） | BidRedactSetup.exe |
| 签名 | ad-hoc 起步；正式需 Apple Developer ID（M4 前） | 可选自签，弹窗提示话术备好 |
| WebView 依赖 | 系统 WKWebView | WebView2 检测，缺则引导下载 |
| 体积目标 | ≤ 250MB（onnxruntime + 模型 + 依赖） | 同 |
| 冷启动 | ≤ 5s 双击到窗口可交互 | ≤ 8s |

### 7.4 M3 验收清单
- [ ] 全新机器（无 Python 环境）安装 → 双击 → 30s 内完成一次完整脱敏
- [ ] 断网全流程可用
- [ ] ONNX 后端推理结果与 HanLP torch 版抽检一致（≥95% 命中重合）
- [ ] 手动破坏 onnx 文件 → 自动降级 jieba + 明确提示（降级演练）
- [ ] 窗口关闭无残留进程（活动监视器/任务管理器核对）

---

## 8. M4 —— 授权系统

### 8.1 授权文件 schema（RSA-PSS + SHA256 签名）
```json
{
  "licensee": "客户名称",
  "order_no": "ORD-2026-0001",
  "machine_id": "<机器码>",
  "issued_at": "2026-01-01",
  "expires": "2027-01-01",          // = 授权到期，非软件报废日
  "update_until": "2026-12-31",     // 免费更新截止
  "seats": 1,
  "features": ["ner", "restore"],
  "sig": "<对以上全部字段的 RSA-PSS 签名，base64>"
}
```
客户端内置公钥（`license_public_key.pem` 随包分发），验签离线毫秒级；私钥只存服务端。

### 8.2 机器码生成（锁定规则）
| 平台 | 来源 | 处理 |
|---|---|---|
| macOS | IOPlatformUUID | sha256(uuid + 固定盐) 前 16 hex |
| Windows | 注册表 MachineGuid | 同上 |

### 8.3 授权状态机
```
未激活 → (导入授权文件+验签+机器码匹配) → activated
activated → 心跳成功(now 内) → activated
activated → 心跳超期 ≤30天 → grace（全功能，界面提示剩余天数）
grace → 超期 >30天 → readonly（只读模式：可看报告/词库，禁脱敏还原）
readonly → 任意时刻心跳/验签恢复 → activated
activated → 服务器吊销(revoked=true) → readonly
防时钟回拨: 本地持久化 max_seen_timestamp；系统时间 < max - 24h → 强制联网校准，
           校准失败 → readonly
```

### 8.4 心跳协议
| 项 | 规格 |
|---|---|
| 端点 | `POST https://授权域名/api/heartbeat` |
| 请求 | `{machine_id, app_version, license_order_no}` |
| 响应 | `{now: "<服务器ISO时间>", revoked: bool, license_valid: bool, notice?: "..."}` |
| 频率 | 每 7 天；失败指数退避重试（1d/2d/4d），静默不扰用户 |
| 隐私红线 | 请求体**只有**上述三字段；无文件名/无文档内容/无路径 |

### 8.5 服务端（license_server，独立小服务）
| 端点 | 功能 |
|---|---|
| POST /api/activate | 订单号+机器码 → 下发签名授权文件 |
| POST /api/heartbeat | 8.4 |
| POST /api/revoke | 管理端吊销 |
| 管理后台 | 订单/机器码/吊销列表（M4 可先 CLI 脚本代替网页） |
- 存储：SQLite 起步；全站 HTTPS；不做用户账号体系

### 8.6 M4 验收清单
- [ ] 空白机器：购买（模拟订单）→ activate → 导入授权 → activated，全程 ≤ 2 分钟
- [ ] 修改授权文件任一字段 → 验签拒绝
- [ ] 授权文件换机器 → 机器码不匹配拒绝
- [ ] 模拟 31 天未心跳 → readonly，且界面明确告知原因与恢复路径
- [ ] 系统时间回拨 90 天 → 强制校准流程生效
- [ ] 抓包心跳请求，确认请求体只有 8.4 三字段

---

## 9. 非功能需求汇总

| 维度 | 要求 |
|---|---|
| 性能 | 见 5.6；M3 体积/冷启动见 7.3 |
| 平台 | macOS 12+（Apple Silicon 优先）/ Windows 10+ |
| 安全 | 6.5 清单 + 8.4 隐私红线 + 报告与映射同级保密（4.3） |
| 合规 | MSRA 语料许可商用前复核（风险 R3）；第三方库均为 MIT/Apache 系 |
| 可维护 | 引擎保持 CLI 可独立运行；所有格式变更走版本号（v1 锁定） |

---

## 10. 测试与验收总表

### 10.1 测试矩阵
| 层 | 手段 | 覆盖 |
|---|---|---|
| 引擎 | pytest 单测 + 合成标书回归（现有 15 处基准不得退化） | 5.1-5.5 全规则 |
| Web | 手动验收清单 6.7 + API 错误码逐条 | 6.3 全端点 |
| 桌面 | 7.4 清单 + 降级演练 | 打包/降级/退出 |
| 授权 | 8.6 清单 + 篡改/回拨/吊销对抗用例 | 状态机全迁移 |
| 性能 | 每里程碑跑 10 页基准计时，记录进报告 | 5.6 预算 |

### 10.2 回归红线
M1 的实测结果（15 处命中/还原逐字一致/错口令拒识）为回归基准，任何后续改动不得使其退化；退化即阻塞合入。

---

## 11. 风险登记（开发视角）

| # | 风险 | 等级 | 对策与触发点 |
|---|---|---|---|
| R1 | 无真实标书样本，全部调优基于合成样本 | 高 | M2 全程可推进；**真实样本到手前不启动 NER 调优冻结版**；到手后先跑 ner_eval 定基线再决定 ONNX 是否立即做 |
| R2 | HanLP→ONNX 导出踩坑 | 中 | M3 前真跑导出脚本；失败保底 torch 方案（体积大但可行），不影响 M2 |
| R3 | MSRA 训练语料商用许可口径 | 中 | M4 发版前复核；备选 OntoNotes 模型对比（ner_eval.py 十分钟定案） |
| R4 | 真实标书版式（嵌套表格/扫描件）超出现有解析 | 中 | G1/G2 先补常见项；扫描件 OCR 归 P2，M2 报告中明示「扫描件暂不支持」 |
| R5 | 授权被破解 | 低 | 三层+防回拨够用即停；正版体验是护城河 |
| R6 | 单人开发带宽 | 中 | 严格按 M2→M3→M4 串行；P2 功能不承诺排期 |

---

## 12. 里程碑计划与交付物

| 里程碑 | 范围 | 交付物 | 验收 |
|---|---|---|---|
| M1 引擎 | ✅ 已完成 | redactor.py v3 + 词库 + ner_eval.py | 3.1 全实测 |
| M2 Web | 引擎补丁 G1-G3 + FastAPI 全端点 + P1-P7 页面 | webapp.py + web/ + M2 验收记录 | 6.7 全过 |
| M3 桌面 | desktop.py + ONNX 导出与内置 + PyInstaller 双平台 | dist 安装包 + 7.4 验收记录 | 7.4 全过 |
| M4 商业化 | licensing.py + license_server + 官网下载页 + 支付 | 可售安装包 + 授权后台 | 8.6 全过 |

**P2 备选池（不承诺排期，按价值排序）**：PDF（文本层+OCR）→ Excel → 文件夹批量（共享假名词典）→ 项目级映射任务中心 → 标书语料 NER 微调。

---

## 附录 A：M1 引擎 CLI 接口（现状照录，M2 Web 层编排的函数级依据）

```bash
# 脱敏
python redactor.py redact <docx> [--vocab vocab.txt] [--strategy 类别=fake|star|delete ...]
                    [--password 口令] [--dry-run] [--out-dir DIR] [--ner]
# 还原
python redactor.py restore <脱敏docx> --mapping <.enc|.json> [--password 口令] -o <输出docx>
```
关键函数（供 Web 层直接 import）：`redact_docx(...) -> (rows, out_docx, out_map, out_report)`、`restore_docx(redacted, mapping_path, out, password=None)`、`encrypt_mapping(mapping, password) -> 容器dict`（4.2 格式）。

## 附录 B：仓库目录规划（演进目标）

```
Desensitization/
├── redactor.py          # 引擎（现状，G1-G3 补丁落此）
├── ner_backend.py       # NerBackend 协议 + 三实现
├── webapp.py            # FastAPI（M2）
├── web/                 # 静态页（M2）
├── desktop.py           # pywebview 壳（M3）
├── licensing.py         # 授权客户端（M4）
├── license_server/      # 授权服务端（M4，独立部署）
├── tools/export_ner_onnx.py
├── models/ner/          # ONNX 产物（M3）
├── vocab.txt  tests/  docs/
```

---

*本文档为开发设计唯一权威来源；实现与本文冲突时，先改文档（走版本号）再改代码。*
