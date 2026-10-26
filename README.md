# upload-html-to-lexiang-by-openapi

把本地 HTML 文件批量上传为**腾讯乐享知识库**的在线条目——走乐享开放接口（AppKey 鉴权），一条命令完成「签名 → 存储 → 建条目」全链路，适合人工上传替代、定时同步和 Agent 自动化调用。

## 它解决什么问题

| 痛点 | 本项目的做法 |
|---|---|
| 手工在网页上传 HTML，一个一个点，无法批量/定时 | 命令行一次传单个文件或整个目录；配置齐了**零参数运行**，可直接挂定时任务 |
| HTML 传上去只显示成「文件卡片」，点开不能预览 | 单 HTML 走专用上传链路，**前端直接渲染成网页**（标题、表格、代码块、内嵌图片/视频都在） |
| 引用本地图片/视频的页面，上传后资源丢失 | 自动把可内联的资源（图片/小视频等）**base64 内联**后仍以单 HTML 直传；不可内联的附件自动打 **zip bundle**（`index.html` + 资源原相对路径） |
| 定时同步每次全量重传，重复造条目 | **幂等**：内容哈希（含上传模式）记录在 manifest，未变更自动 skip；已变更原地更新（单 HTML）或删除重建（zip），entry_id 变化自动上报 |
| 出问题不知道卡在哪一步 | 诊断脚本把**每个接口的完整请求/回包**按调用顺序打印出来，直接定位坏环节 |

## 提供什么能力

- **单 HTML 直传**：自包含页面 → 可在线预览的知识条目（含 base64 内联资源后的页面）
- **目录批量**：递归扫描 `*.html`，每个文件一条目，幂等增量
- **zip bundle**：`index.html` + 本地图片/视频等相对路径资源 → 附件型条目（文件卡片）
- **更新**：单 HTML 原地更新（entry_id 不变）；zip 删除重建（自动报告新 id）
- **企业级鉴权**：AppKey/AppSecret 换 token（2h 自动续换），或直接贴 Bearer token；`env:` 变量引用凭证不落盘
- **多策略配置**：不同目标（空间/目录）或不同凭证各一个配置文件，互不干扰；幂等状态按策略自动隔离
- **自检回归**：11 用例矩阵测试（链路语义 + 端到端），改配置/升级后一键验证
- **接口级文档**：不想用脚本、要直接调接口对接自己系统？`references/upload-api-cookbook.md` 给了完整 curl 报文与错误码速查表

## 如何使用

### 前置条件

- Python 3.10+（仅标准库，无第三方依赖）
- 乐享开放接口凭证：在管理后台【开发-接口凭证管理】创建 AppKey/AppSecret，并勾选接口权限（本地文件上传 / 上传文件 / 重新上传文件 / 创建文件夹 / 获取知识库详情 / 删除知识节点）
- 一个你有编辑权限的知识库空间，及操作人 staff_id

### 三步上手

```bash
# ① 建策略配置（configs/ 下，一个目标一套；配置含凭证，已被 .gitignore 忽略不入库）
cp configs/config.example.json configs/my-space.json
#    填：api_base（乐享网关地址）+ app_key/app_secret + staff_id
#        source（要上传的 HTML 文件或目录）+ space_id（目标知识库）
#    可选：parent_entry_id（传到指定子目录，缺省为空间根目录）

# ② 跑一次自检（会在目标空间建一个测试条目并自动删除）
scripts/test_private_chain.py --config configs/my-space.json --cleanup

# ③ 正式上传
scripts/html_import.py --config configs/my-space.json          # 传配置里的 source
scripts/html_import.py --config configs/my-space.json report.html   # 临时指定文件
```

### 常用命令

```bash
# 目录批量 + 幂等（未变更的自动 skip）
scripts/html_import.py --config configs/my-space.json ./html-dir

# 预检（扫描资源、决定 raw/zip 模式，不上传）
scripts/html_import.py --config configs/my-space.json --dry-run

# 更新已有条目（单 HTML 原地更新；zip 删除重建并报告新 entry_id）
scripts/html_import.py --config configs/my-space.json report.html --entry-id <EID>

# 定时任务形态：策略配齐 source/space_id 后零参数
scripts/html_import.py --config configs/my-space.json

# 排查上传失败：逐接口打印完整请求/回文，末尾指出第一个坏环节
scripts/debug_private_chain.py --config configs/my-space.json
```

输出统一为 JSON（`summary: total/succeed/skipped/degraded/failed`），方便脚本/Agent 消费。

## 目录结构

```
scripts/html_import.py         # 正式导入（上传/批量/幂等/更新）
scripts/openapi_client.py      # 共享客户端（token + 三步上传链，可被 import 复用）
scripts/debug_private_chain.py # 链路诊断（逐接口请求/回包全文）
scripts/test_private_chain.py  # 矩阵回归（11 用例，--cleanup 自清理）
configs/config.example.json    # 策略模板（实际配置自建，不入库）
references/upload-api-cookbook.md          # 接口级调用手册（curl 报文+错误码表）
references/probe-results-*.md              # 逐接口实测记录
```

完整工作流、上传模式判定规则、踩坑预警见 [SKILL.md](SKILL.md)；直接对接接口先读 [references/upload-api-cookbook.md](references/upload-api-cookbook.md)。
