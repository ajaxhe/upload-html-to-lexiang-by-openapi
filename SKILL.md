---
name: upload-html-to-lexiang-by-openapi
description: >-
  Upload HTML files or HTML-bundle ZIPs (with relative-path assets) to Tencent
  Lexiang via OpenAPI (AppKey/AppSecret) — SaaS or privatized deployments.
  Use when uploading/importing HTML to Lexiang without an MCP token: single
  file or batch directory, idempotent re-runs, in-place raw-html updates,
  delete+recreate zip-bundle updates, self-cleaning regression tests. For the
  MCP-token channel use upload-html-to-lexiang instead. 凭证+api_base 全配置文件化（支持私有化乐享）。
---

# Upload HTML to Lexiang — by OpenAPI

用乐享开放接口（AppKey/AppSecret）上传 HTML / HTML bundle。与 MCP 版 `upload-html-to-lexiang` 的区别：鉴权是企业 AppKey（非个人 `lxmcp_` token），且 `api_base` 等一切地址走配置文件——**公有云和私有化部署同一套脚本**。

## 工作流

1. **配置（多策略）**：一个策略一个文件，放 **`configs/` 目录**（如 `configs/default.json`、`configs/prod-saas.json`、`configs/私有化-xx.json`；`configs/*` 已被 .gitignore 忽略，仅模板 `configs/config.example.json` 会提交）。复制模板为 `configs/<策略名>.json` 并填：
   - 必填：`api_base`（私有化填本企业网关地址）+ `staff_id` + `source`（源端：HTML 文件或目录，支持 ~）+ `space_id`（目的端：目标空间）+ 凭证（`auth_mode=app_key` → `app_key`/`app_secret`；`auth_mode=bearer` → `access_token`）——**策略必须自包含**，缺任一项直接报错退出
   - 可选：`parent_entry_id`（目的端：目标父目录，缺省建在空间根目录）；注：`--entry-id` 更新模式下 `space_id` 不参与定位（以条目自身 space 为准），但仍必填以保持策略统一
   - 私有化网关的差异全部走配置（不改编码）：`upload_params_path`/`upload_params_template`（签名步）、`cos_put_url_template`（存储 PUT 地址）、`html_entry_path`（html 建条目路由）、`headers`（`<测试网关域名>` 测试网关必需 `X-Zerus-Env: base-xxxx`，min 私有化 2026-10-26 起不需要）、`extra_query`——详见下方「私有化环境」
2. **执行（带策略）**：`html_import.py --config configs/<策略名>.json [source]`——省略 `--config` 则用 `configs/default.json`；策略必含 source+space_id，天然支持**零参数运行**（适合定时任务，严禁在自动化入口拼源/目的参数）
3. **预检（可选）**：`html_import.py --config <策略> --dry-run` 扫描资源、打包预览，不上传
4. **更新**：加 `--entry-id <EID>`（raw html 原地更新；zip bundle 删除重建，entry_id 会变并在输出中报告新值）
5. **回归（改环境/换网关/改代码后）**：`test_private_chain.py --config configs/<策略>.json --cleanup` 三环境通用矩阵（SaaS / 测试环境 / min 私有化均支持，详见下方「矩阵回归」）

**参数优先级**：命令行 > 策略配置；但命令行**仅作覆盖**，不能替代配置——`source`/`space_id` 在策略内必填（自包含、零参数就绪），命令行没传时用配置值。
**状态隔离**：manifest 幂等状态按策略自动隔离——`configs/foo.json` → `manifests/foo.json`，不同策略互不干扰（已 .gitignore）。

## 正式脚本 scripts/html_import.py

```bash
# 指定策略 + 零参数（策略内 source/space_id 配齐；定时任务用这个形态）
scripts/html_import.py --config configs/prod-saas.json

# 默认策略 configs/default.json
scripts/html_import.py report.html                       # source 覆盖 default.json 配置值
scripts/html_import.py                                   # 零参数（default.json 必含 source/space_id）

# 单文件（含相对路径资源 → 自动打 zip bundle：index.html + assets 原路径）
scripts/html_import.py report.html --config configs/policy-a.json --space-id <SID> --parent-entry-id <EID>

# 目录批量（递归 *.html，每个一条目；未变更自动 skip）
scripts/html_import.py ./html-dir --config configs/policy-a.json

# 自包含/可内联 HTML → raw .html 直传（默认 auto，2026-10-14 实测：raw 前端可渲染，zip 不可）
scripts/html_import.py self.html --config configs/policy-a.json --raw

# 强制 zip bundle（legacy 形态；不预览，仅 raw 不可能时用）
scripts/html_import.py report.html --config configs/policy-a.json --bundle

# 原地更新已有条目
scripts/html_import.py report.html --config configs/policy-a.json --entry-id <EID>

# 预检 / 强制重传 / 关幂等
scripts/html_import.py report.html --config configs/policy-a.json --space-id <SID> --dry-run
scripts/html_import.py report.html --config configs/policy-a.json --space-id <SID> --force
scripts/html_import.py report.html --config configs/policy-a.json --space-id <SID> --no-incremental

# 私有化：注入既有上传任务的 state（跳过签名+COS PUT，直达建条目接口）
scripts/html_import.py report.html --config configs/私有化-min.json --state <STATE>

# bearer 策略临时换 token（token 约 2h 过期，不必改配置文件）
scripts/html_import.py --config configs/私有化-min.json --token "eyJ0eXAi..."
```

**更新语义（实测决定，勿改）**：

| 条目形态 | `--entry-id` 更新方式 | entry_id |
|---|---|---|
| raw html（自包含） | `PUT /kb/entries/{id}/upload?state=` 原地更新 | 不变 |
| zip bundle（含资源） | OpenAPI 重传接口实测**一律 400**，自动 DELETE+重建 | **会变**，输出含 new id，manifest 自动同步 |

**上传模式（默认 auto，2026-10-14 实测决定）**：raw .html 前端可渲染（含内嵌视频播放），zip bundle 显示为文件卡片「暂不支持预览」——OpenAPI 无 extension=html 参数，渲染形态由上传文件的 .html 后缀决定。auto 策略：无资源 → raw 直传；图片/css/js/**视频/音频（合计单文件 ≤10MB）** → base64 内联为 data URI 后 raw（含本地引用的 srcset 整属性剥离，data URI 含逗号与 srcset 语法冲突，靠已内联的 src 回退）；超限媒体与附件类（csv/pdf/txt/zip）不可预览内联 → zip 兜底（不预览，mode_reason 会说明原因；大视频建议拆成独立视频条目 media_type=video）。`--raw` 强制自包含原样直传，`--bundle` 强制 zip。

**硬性规则**：
- 主文件打包后必须名 `index.html`（zip 模式），资源保持原始相对路径
- 缺失资源**不静默丢弃**：注入 `[资源缺失：xxx]` 占位提示并计入 `degraded_resources`
- 幂等：content_hash = sha256(**mode** + HTML + 资源字节)——mode 参与哈希，raw/zip 切换会使 manifest 失效重传（2026-10-14 修幂等盲区）；manifest 按策略隔离 `manifests/<策略名>.json`（与 MCP 版分开）
- stdout 只输出 JSON（summary: total/succeed/skipped/degraded/failed + degraded_resources），诊断走 stderr
- 条目名默认取 HTML `<title>`，`--name` 可覆盖

## 接口调用序列（直接调接口时看这个）

两类上传的**接口级 cookbook**（不经脚本的直接对接）见 `references/upload-api-cookbook.md`——按低配模型标准写死步骤、完整 curl 报文、错误码速查：

- **流程 A：单 HTML 文件（可预览渲染）**——token → `cos-param` 签名（filename 带 `.html`）→ PUT COS（**Content-Type 必须覆盖为 text/html**，否则前端空白）→ `POST /kb/entries/html?state=`（**name 必须带 .html 后缀**）
- **流程 B：zip bundle（index.html + 本地图片/视频，文件卡片不预览）**——打包（主文件必须名 `index.html`、资源原相对路径）→ 同链路但 filename 用 `.zip`、Content-Type 保持 octet-stream、建条目走标准 `POST /kb/entries?state=&space_id=`
- 公有云差异（upload-params 直传）、min `--state` 通道、每个错误码的含义与动作都在 cookbook §4-§6

## 私有化环境（2026-10-23 实测 `<私有化网关域名>`）

同一套脚本，差异全走配置。与公有云（`lxapi.lexiangla.com`）的对照：

| 环节 | 公有云 | 私有化 `<私有化网关域名>` | 配置键 |
|---|---|---|---|
| 换 token | `POST /cgi-bin/token` | 同样可用（AppKey→2h token），实测 200 | `auth_mode: app_key` |
| 直接贴 token | — | 也可贴 Bearer（抓包的 `ifbook-http-client` token），不换 token | `auth_mode: bearer` + `access_token` |
| 测试环境标识 | 不需要 | min 私有化**不需要**（2026-10-26 用户确认后移除）；测试网关 `<测试网关域名>` 每个请求要带 `X-Zerus-Env: base-xxxx`（放 `headers`，只发给网关不发给 COS）——该网关为独立凭证体系，需测试环境自己的 AppKey/AppSecret（已有 `configs/测试环境.json`，**全链已打通**） | `headers` |
| 签名步 | `/cgi-bin/v1/kb/files/upload-params`（`name`+`media_type`） | **恒 500「系统内部错误」**（file/video/audio 全试过，`company_from` 各种塞法都无效）；老接口 `/cgi-bin/v1/docs/cos-param`（`filename`+`type`）**可正常出 state** | `upload_params_path` + `upload_params_template` |
| 存储 PUT | 响应带 `object.upload_url`，直接 PUT | 只给 `options.Bucket/Region` + `object.key`，`auth: null`，**没有 upload_url** | `cos_put_url_template`（`{bucket}/{region}/{key}` 占位；以 `/` 开头则拼在 `api_base` 后） |
| 建条目 | `POST /kb/entries?state=&space_id=` | `POST /kb/entries/html?state=`（**parent_entry 定位，query 不带 space_id**） | `html_entry_path` |
| 目标定位 | space_id 或 parent_entry_id | `html_entry_path` 策略可省 `space_id`，但 `parent_entry_id` 必填 | — |

**已核实（可直接引用，详见 `references/privatized-probe-2026-10-23.md`）**：

- `POST /kb/entries/html`：无 `state` → 400「state 不能为空」；假 state → 400 `code=30010001`「上传任务不存在」；**有效 state 但对象没上传 → 400 `code=30010003`「文件不存在」**
- 该接口**不接受内联内容**：`content`/`html`/`data`/`base64` 一律仍报「文件不存在」——文件二进制必须先落到存储
- `cos-param` 返回的 state 是**真实上传任务**（用它建条目会走到「文件不存在」，说明 state 已被受理）
- 存储下载：`{api_base}/private-mono/api/open3rd/files/csp/{key}?filename=&sign=`；同路径 `PUT` → 400 `code=41`「verification parameter error」（**上传需 sign，算法待确认**）

**测试环境 `<测试网关域名>`（2026-10-26 全链打通，`configs/测试环境.json`）**：独立凭证体系（AppKey `<APP_KEY>…`，staff `<staff>`）。标准 `upload-params` 能回 `upload_url` 但其中的 COS 临时密钥已失效（PUT 报 403 `InvalidAccessKeyId`，即该环境已知缺陷）——签名步同样走 `cos-param`，其 `auth{Authorization,XCosSecurityToken}` 临时密钥对有效；存储 PUT 用 `cos_put_url_template: "https://{bucket}.cos.{region}.myqcloud.com/{key}"` 拼（bucket=`<bucket>`，region=`ap-shanghai`）→ 200。`/kb/entries/html` 与标准 `/kb/entries` 两条建条目路由均可用。samples 9 个文件批量上传全绿（4 raw + 2 zip bundle + 3 内联 raw），幂等重跑全 skip。目标：team `<团队名>` 的空间「1」。**curl 注意**：该网关的 POST 用 `--data-raw @file` 会报「参数错误」，须用内联 body（Python requests 不受影响）。

**min 私有化仍未闭环**：文件二进制上传地址 + sign 算法。补齐后只需在策略里写 `cos_put_url_template`（必要时另加 `headers`），**代码零改动**即可全自动；在那之前用 `--state <STATE>` 把已上传对象的 state 注进来，其余环节（建条目/幂等/更新）都能正常跑。

## 诊断脚本 scripts/debug_private_chain.py

排查「到底哪一环坏了」用这个：按调用顺序把每个接口的**请求全文**打出来——method + 完整 URL、scheme/host、path、query 逐项拆开、**每一项 header**、**完整请求体**（JSON 自动美化，HTML 原文，二进制 hexdump），并附一条可直接复制的 `curl`；响应侧给状态码、耗时、**全部响应头**和响应体。

```bash
scripts/debug_private_chain.py                       # 默认 configs/私有化-min.json + samples 里的 HTML
scripts/debug_private_chain.py --config configs/default.json --file report.html
scripts/debug_private_chain.py --state <STATE>        # 跳过签名+上传，只看建条目/校验
scripts/debug_private_chain.py --put-url 'https://{bucket}.cos.{region}.myqcloud.com/{key}'
scripts/debug_private_chain.py --show-token           # Authorization / JWT 打全文（默认脱敏）
scripts/debug_private_chain.py --max-request-body 400 # 请求体截断换紧凑（默认 0=全文）
scripts/debug_private_chain.py --max-body 0           # 响应体不截断（默认 4000）
scripts/debug_private_chain.py --save-payloads /tmp/pl # 把每次请求体落盘，curl 可直接 @file
scripts/debug_private_chain.py --continue-on-fail     # S6 上传失败也继续调 S7 建条目，看它的报文
scripts/debug_private_chain.py --cleanup              # 全链通过时删除测试条目
```

输出形态（每次请求一个 `#n` 块）：

```
  #7  →  POST /cgi-bin/v1/kb/entries/html   [348 ms]
      URL         : https://…/cgi-bin/v1/kb/entries/html?state=…&company_from=<值>
      scheme/host : https://<私有化网关域名>
      path        : /cgi-bin/v1/kb/entries/html
      query       : · state = …  · company_from = lexiang
      headers     : (4 项) · Content-Type / User-Agent / Authorization(脱敏) / x-staff-id
      body        : json, 236 bytes   → 美化后的完整 JSON
  #7  ←  ✘ HTTP 400   [348 ms]
      resp headers: (11 项) …   resp body: 完整 JSON
      curl 复现: curl -X POST '…' -H … --data-raw '…'
```

环节：S1 token → S2 空间详情 → S3 目标目录 → S4 标准 `upload-params`（私有化预期 500，标 **WARN** 不算断点）→ S5 策略签名步 → S6 存储 PUT（回包无 `upload_url` 时逐个探测候选地址）→ S7 建条目 → S8 校验 + 下载链接。任一环节失败默认即停并汇总（含本次共发了多少次请求）；要看后续环节的报文（例如 S6 失败后 S7 `/kb/entries/html` 到底发了什么），加 `--continue-on-fail`。

## 矩阵回归 scripts/test_private_chain.py（三环境通用）

SaaS / 测试环境 / min 私有化**同一入口**（按策略配置自适应）：T1 headers 自检（纯配置展示——X-Zerus-Env 为测试网关专用）→ T2 token → T3 空间 → T4 目标目录（未配 parent_entry_id 的 SaaS 形态 SKIP，端到端自动解析空间根）→ T5 标准 `upload-params`（400 code=31 已知缺陷或 200 可用均 PASS）→ T6 策略签名步（按配置：min/测试环境=cos-param，SaaS=upload-params）→ T7-T9 `/kb/entries/html` state 语义三连（无/假/真 state，未配 html_entry_path 则 SKIP）→ T10 存储 PUT（upload_url / cos_put_url_template / 网关候选多探测，headers 与正式链路同源）→ T11 端到端建条目+校验。

**verdict 语义（与诊断脚本一致）**：`PASS` = 符合已实测行为（含预期失败，如「假 state → 30010001」）；`BLOCK` = 已知环境断点（S6 缺 sign），**不算失败**；`FAIL` = 行为回归（exit 非零）；`SKIP` = 前置缺失跳过。exit 0 除非出现 FAIL。**跑完加 `--cleanup` 自删除测试条目**。

```bash
scripts/test_private_chain.py [configs/<策略>.json] --cleanup   # 默认 configs/私有化-min.json
```

当前基线（2026-10-26）：**测试环境 `configs/测试环境.json` → 11/11 全 PASS**（T11 端到端真实建条目）；**SaaS `configs/default.json` → 7 PASS + 4 SKIP 全绿**（端到端走空间根+标准路由）；**min 私有化 → 9 PASS / 1 BLOCK（T10 存储 PUT）/ 0 FAIL / 1 SKIP（T11，待 S6 闭环后自动转绿）**。服务端升级/换网关/改代码后跑对应策略，PASS/BLOCK 分布不应变化——这是回归判据。

（2026-10-26 精简：原公有云形态矩阵脚本 `test_upload_chain.py` 已删除——其职责（html 三步链回归 + 端到端）已并入本脚本；txt/pdf 对照与 media_type/entry_type 枚举探测的结论固化在 `references/probe-results-2026-10-14.md`，无需脚本化复跑。）

## 踩坑预警

1. **zip 条目原地更新是死路**（400「不允许重复上传不同文件类型文件」）——已由脚本自动走删除重建，别手动调重传接口
2. **ext 一致性校验形同虚设**：html 条目能被 zip 覆盖且真实生效，脚本已自校验
3. **403「未授权接口权限」≠ 凭证错误**：token 能拿到但接口逐个勾权限，勾完即好
4. **跨租户不可混用**：凭证、空间、目标必须同租户（404「不存在 kb_space 资源」即换错凭证）
5. 乐享 WAF 拦截含 `os.system`/`__import__` 等危险模式的 HTTP 正文，上传的 HTML 里避免这些字样
6. **data URI 含逗号，与 srcset 逗号分隔语法冲突**：含本地引用的 srcset 必须整属性剥离（img 回退到已内联的 src），别尝试把 data URI 塞进 srcset 列表
7. **私有化 `upload-params` 500「系统内部错误」≠ 凭证/权限错**（token、空间、目录都能正常读）：该部署的签名步坏了，改用老接口 `/cgi-bin/v1/docs/cos-param`（body 是 `filename`/`type`，不是 `name`/`media_type`）
8. **私有化 cos-param 不返回 `upload_url`、`auth` 为 null**（只有 `options.Bucket=<bucket>` / `Region=<region>` / `object.key`）——别指望直接 PUT COS；先确认存储地址填 `cos_put_url_template`
9. **私有化 html 建条目接口不接受内联内容**：`content`/`html`/`data`/`base64` 全被忽略，只认「已上传文件对象的 state」；`code=30010003`「文件不存在」= state 合法但对象没上去，`code=30010001`「上传任务不存在」= state 根本没注册
10. **bearer 模式 token 约 2h 过期**：脚本会解码 JWT `exp` 提前报错，别把它当 401 排查；`--token` 直接换，不必改配置
11. **私有化 `cos-param` 的 `options`（Bucket/Region）在回包顶层，不在 `object` 里**：只取 `object` 会把 `cos_put_url_template` 拼成空域名（表现为 idna/host 报错）。`OpenApi.extract_object()` 已做合并，自写脚本时别再手动 `.get("object")`
12. **`/kb/entries/html` 要求 name 带 `.html/.htm` 后缀**（2026-10-26 测试环境实测）：无后缀报 400「HTML 知识仅支持 .html/.htm 单文件或 .zip 包」；服务端展示时会自动剥掉后缀，所以 `create_file_entry` 已自动补——注意补后缀必须发生在构造 payload **之前**（改 name 变量晚于 payload 构造 = 白改）
13. **`headers`（如 X-Zerus-Env）只发给乐享网关，不发给外部 COS**（2026-10-26 测试环境实测）：`_request` 已按 URL 前缀限定（与 `extra_query` 同样的 api_base 判断）——把网关专属 header 发到 COS PUT，HTTP 层可能 200 但对象会被 `/kb/entries/html` 拒绝
14. **测试网关 `<测试网关域名>` 的 curl 坑**：POST 用 `--data-raw @file` 报「参数错误」，必须内联 body；其标准 `upload-params` 返回的 `upload_url` 里 COS 临时密钥失效（403 `InvalidAccessKeyId`），别走那条路，用 `cos-param` 的 auth
15. **cos-param 把对象签成 `application/octet-stream`，前端渲染会空白**（2026-10-26 测试环境实测）：HTML 字节 PUT 时必须覆盖 `Content-Type: text/html`（COS 签名 q-header-list 为空，换 header 不破坏签名；公有云 upload-params 本来就回 text/html）。`cos_put(force_content_type=)` 已按文件名后缀自动处理，zip bundle 保持二进制不变

## 停止条件

- **渲染形态已实测确认（2026-10-14）**：raw .html 渲染、zip bundle 不预览（文件卡片）。对渲染有硬要求时用 raw（必要时 base64 内联资源）；zip 只作附件类资源的兜底——此时条目不预览，如不可接受需向乐享提 `extension=html` 开放参数需求
- 403「未授权接口权限」→ 停止，让用户去管理后台勾接口
- 404「不存在 kb_space 资源」→ 凭证与空间不同租户，停止换凭证
- 配置缺 `api_base`/`staff_id`/`source`（+ `space_id`，或被 `html_entry_path` 替代时改为要求 `parent_entry_id`）或凭证 → 直接报错退出（策略必须自包含；`api_base` 私有化必填，无默认值）
- 私有化 `cos-put` 报 `no upload_url in response` → 停止，先把存储上传地址填进 `cos_put_url_template`（或给一个现成 `--state`），不要反复试 `upload-params`
- 私有化建条目返回 `code=30010003`「文件不存在」→ 停止，state 对应的对象还没上传成功，与建条目逻辑无关

## 详细文档

- `references/upload-api-cookbook.md` — **接口级调用序列 cookbook**（单 HTML / zip bundle 两流程的完整 curl 报文、Content-Type 与 name 后缀两条决定性坑、错误码速查表）——直接对接接口时先读这个
- `references/probe-results-2026-10-14.md` — 公有云全部实测矩阵、E2E 记录、凭证权限实况、保留样本 entry_id
- `references/privatized-probe-2026-10-23.md` — 私有化 `<私有化网关域名>` 逐接口实测（token/cos-param/entries-html/存储路由）+ 测试环境 `<测试网关域名>` 全链打通记录
- 官方接口文档 https://lexiang.tencent.com/wiki/api/

## 修改与验证

```bash
python3 -c \
  "import ast; [ast.parse(open(f'scripts/{f}').read()) for f in
   ('html_import.py','openapi_client.py',
    'debug_private_chain.py','test_private_chain.py')]; print('OK')"
scripts/test_private_chain.py --cleanup                                # min 基线（9 PASS + 1 BLOCK）
scripts/test_private_chain.py --config configs/测试环境.json --cleanup  # 测试环境（11 全 PASS）
scripts/test_private_chain.py --config configs/default.json --cleanup  # SaaS（7 PASS + 4 SKIP）
```
