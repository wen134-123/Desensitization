# 标书脱敏工作台（纯本地 · 可逆还原）

面向标书 / 投标文件的脱敏工具。服务只监听 `127.0.0.1`，文档在本机处理；
源文件、映射与报告保存在本机 `workspace/jobs`，过期任务会在启动时和运行期间清理。

## 快速开始

```bash
pip install -r requirements.txt
python webapp.py                 # 自动打开 http://127.0.0.1:8765
python webapp.py --port 9000 --no-browser   # 指定端口 / 不自动打开浏览器
python webapp.py --dev           # 开发模式：跳过激活校验
```

桌面版（内置网页窗口，推荐给终端用户）：

```bash
pip install pywebview
python desktop.py                # 随机本地端口 + 内嵌窗口
python desktop.py --dev          # 开发模式
```

浏览器打开后按三步操作：上传标书并设置策略 → 复核识别结果 → 导出与留痕。
识别完成后页面下方会显示**左右分栏的脱敏前后对比预览**；取消勾选误标项、
切换替换方式或调整类别策略时，预览会即时刷新。导出后预览仍然保留，便于对照
检查最终脱敏件。

复核支持**实体级**和**出现位置级**两种取消方式：同一个人名或公司出现在多处时，
可以只排除其中的某一处，保留其他位置的自动脱敏。预览超过 30 个内容块会自动分页；
左侧原文选中文字后，也可以按类别一键补充为新敏感词并重新识别。

## 能识别什么

| 类别 | 说明 |
| --- | --- |
| 公司/单位 | 宏远建设工程有限公司、中国银行北京分行营业部 |
| 人名 | 张伟、李建国（含词性智能识别与职务后姓名） |
| 电话 | 手机、座机、400/800 |
| 身份证 | 18 位（含校验码）/ 15 位 |
| 银行卡/账号 | Luhn 校验 + “银行账号/卡号”标签 |
| 邮箱 | 标准邮箱地址 |
| 地址 | 省市区县 + 街道门牌 |
| 报价/金额 | ￥12,800,000.00、人民币大写金额 |
| 项目代号 | 项目编号/代号/标段名称、字母数字编号 |
| 信用代码 | 统一社会信用代码（GB32100 校验） |

覆盖范围：正文、表格、嵌套表格、页眉、页脚、文本框、超链接、脚注、尾注与批注内的文字。
支持 `.docx` / `.txt` / `.md`，单文件最大 50MB。

## 替换方式

- **占位符**（推荐）：`【公司A】【电话A】`，清晰可读、稳定可逆。
- **拟真伪装**：生成格式一致的虚构公司名、手机号、身份证、银行卡、邮箱、地址、报价，
  同样可逆，适合需要保持文档观感的场景。

按类别可单独选择策略：

- **假名替换**：写入映射表，可逐字还原。
- **星号遮盖**：如 `138****5678`，便于内部比对，不可还原。
- **严格删除**：整段抹除，不可还原。

## 可逆还原

假名替换的每个实体都会进入映射表；星号遮盖和严格删除不进入映射，无法还原。
设置口令后，映射以
`PBKDF2(20万次) + Fernet` 加密保存为 `.enc`；还原时必须同时提供：

1. 脱敏后的文档
2. 映射文件
3. 口令

在“还原文档”页上传三者即可下载还原件。处理报告只显示遮盖后的原文，避免二次泄漏。

## 识别优先级

1. 白名单：`白名单|中国银行`，明确不脱的词
2. 用户补充敏感词：`类别|词`
3. 正则硬规则：证件、账号、电话、邮箱、金额、地址、公司等
4. 本地 NER：内置 ONNX 模型优先，缺失时自动降级 jieba 词性识别（纯本地）

### NER 后端

`ner_hits(text)` 是唯一的可插拔识别入口，`redactor.detect()` 只调用它。
后端选择顺序：`models/ner/` 下的 ONNX 模型 → jieba 词性标注 → 仅规则词库。

模型目录约定：

```
models/ner/
  model.onnx      NER 模型（CPU 推理，单文件）
  tokenizer.json  fast tokenizer（已关闭 padding，含 offset）
  labels.json     标签顺序（支持 BIO / BMESO，如 HanLP 的 33 个 MSRA 标签）
```

- 打包时 `models/ner/` 会被放入安装包；首次启动解压到用户目录，之后永久离线。
- 内置模型为 HanLP `MSRA_NER_ELECTRA_SMALL_ZH`；重新导出（需要 torch/hanlp/onnx）：
  `python tools/export_ner_onnx.py --model <HanLP模型目录> --out models/ner`
- 强制指定后端：`REDACT_NER_BACKEND=onnx|jieba|auto`。
- 缺少 onnxruntime / tokenizers / 模型文件时自动退回 jieba，不影响使用。

## 订阅授权（本地软件 + 年费）

- 软件在本机运行，**文档永不离开用户机器**；授权服务器只处理激活码、机器码与订单。
- 激活码即身份，不做账号注册登录。激活后许可证用 Ed25519 签名，客户端只内置公钥，
  日常校验可完全离线。
- 许可证绑定本机机器码；换机需在管理后台解绑。
- 订阅到期后保留 **30 天宽限期**；客户端每 7 天尝试一次心跳，失败不影响离线使用。

开发调试（源码运行默认放行）：

```bash
REDACT_DEV=1 python webapp.py     # 强制开发模式
REDACT_DEV=0 python webapp.py     # 强制校验授权
```

## 授权服务器部署

```bash
# 1. 初始化数据库与签名密钥（会把公钥同步为客户端 license_public_key.pem）
python -m license_server.admin init

# 2. 生成激活码
python -m license_server.admin create --plan pro --days 365 --count 10 --customer 某公司

# 3. 启动服务（生产环境请放在 HTTPS 反向代理后；客户端拒绝向外部 HTTP 地址提交激活请求）
LICENSE_PUBLIC_URL=https://license.example.com \
python -m uvicorn license_server.app:app --host 0.0.0.0 --port 8790
```

- 私钥 `license_server/keys/private_key.pem` **只留在服务器**，权限必须为 `0600`，
  绝不能打包进客户端或提交到代码仓库。
- 订单与支付回调骨架已内置：`POST /api/orders`、`GET /api/orders/{id}`、
  `POST /api/payments/webhook/{provider}`（HMAC-SHA256 校验）。
- 开发环境可设 `LICENSE_DEV_PAYMENTS=1` 启用模拟支付。
- `site/` 为定价与下载页，授权服务启动后挂载在根路径。

| 管理命令 | 作用 |
| --- | --- |
| `admin stats` / `admin list` | 查看授权统计与激活码 |
| `admin create --plan pro --days 365 --count 10` | 批量签发激活码 |
| `admin revoke --code BRD-...` | 停用激活码 |
| `admin extend --license <id> --days 365` | 续期 |
| `admin reset-machine --code BRD-...` | 换机解绑 |
| `admin order` / `admin fulfill` | 手工创建订单 / 标记已支付并发码 |

## 打包 macOS 安装包

```bash
./build_macos.sh
open dist/BidRedact.app
```

脚本会创建独立构建环境、安装依赖并调用 `bidredact.spec`。发布前需要：

1. Apple Developer ID 代码签名（`codesign --options runtime`）
2. 公证（`notarytool submit` + `stapler staple`）
3. 把 `dist/BidRedact.app` 压缩为 `.dmg` 或 `.zip` 上传到官网

## 目录说明

```
webapp.py          本地 FastAPI 服务（仅 127.0.0.1）
desktop.py         pywebview 桌面壳
licensing.py       机器码、许可证校验、宽限期、心跳
ner_backend.py     NER 后端（ONNX / jieba）
redactor.py        识别、脱敏、预览、还原引擎
models/ner/        内置 ONNX NER 模型
web/               内嵌网页
license_server/    授权服务器与管理 CLI
site/              官网定价页
tools/             模型导出脚本
bidredact.spec     PyInstaller 配置
build_macos.sh     macOS 打包脚本
样本标书.docx      测试样本
```

## 命令行（可选）

```bash
python redactor.py redact 样本标书.docx --vocab vocab.txt
python redactor.py redact 样本标书.docx --mode realistic --password 你的口令
python redactor.py restore 样本标书_脱敏.docx --mapping 样本标书_映射.enc --password 你的口令 -o 还原.docx
```

## 当前边界

- 暂不支持 PDF、Excel、图片 OCR；扫描件与图片中的文字需先转成 Word/文本。
- 图片、印章、手写签名不在自动识别范围内，外发前请人工检查。
- 正式外发前建议再通读一遍脱敏件，并核对处理报告中的防漏提示。
