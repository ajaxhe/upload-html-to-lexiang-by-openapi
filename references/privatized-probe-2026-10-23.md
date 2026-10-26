# 私有化环境实测（2026-10-23，`<私有化网关域名>`）

对象：私有化乐享网关 `https://<私有化网关域名>`，凭证 `app_key=<APP_KEY>…`（AppKey 换 token 可用），
staff `<staff>`，目标空间 `<SPACE_ID>`（「<空间名A>」，root=`<ROOT_ENTRY_ID>`），
目标目录 `<PARENT_ENTRY_ID>`（「html接口测试」，folder，owner=<staff>）。
另有 Web 端抓包得到的 Bearer token（UA `ifbook-http-client`，staff `<staff_b>`，JWT 有效期 2h）作为对照凭证。

## 1. 认证

| 调用 | 结果 | 说明 |
|---|---|---|
| `POST /cgi-bin/token`（grant_type=client_credentials + AppKey/AppSecret） | ✅ 200，`expires_in: 7200` | 私有化网关**保留**标准换 token 接口，无需手工贴 Bearer |
| 抓包 Bearer token 直连（`Authorization: Bearer …` + `X-Staff-Id`） | ✅ 可用 | 两条路都通；脚本用 `auth_mode` 区分 |

## 2. 读接口（全部正常）

| 调用 | 结果 |
|---|---|
| `GET /cgi-bin/v1/kb/spaces/{space_id}` | ✅ 200（含 `relationships.root_entry`） |
| `GET /cgi-bin/v1/kb/entries/{entry_id}` | ✅ 200（folder 带 `relationships.parent`） |
| `GET /cgi-bin/v1/kb/entries?space_id=&parent_id=&per_page=` | ✅ 200（`parent_id` 过滤有效，缺 space_id → 400） |
| `GET /cgi-bin/v1/kb/spaces?team_id=63` | ❌ 404（`不存在ID为63的team资源`） |
| `GET /cgi-bin/v1/kb/entries`（不带 space_id） | ❌ 400 `{"space_id":["不能为空"]}` |

补充：`extra_query` 挂 `company_from=<值>` 对上述接口无副作用（200 一致）。

## 3. 签名步：标准接口坏了，老接口能用

| 调用 | 结果 |
|---|---|
| `POST /cgi-bin/v1/kb/files/upload-params` `{"name":"a.html","media_type":"file"}` | ❌ **500 / `code:31`「系统内部错误」**（带 request_id） |
| 同接口 `media_type=video` / `audio` / `txt`类文件名 / 加 `size` / 加 `entry_type` / `company_from` 放 body、query、header / 换 `Content-Type`(vnd.api+json) | ❌ 全部同样 500 —— 与该部署的存储签名能力有关，不是参数问题 |
| `media_type=html` | ❌ 400「选定的值是无效的」（与公有云一致：枚举只有 file/video/audio） |
| `POST /cgi-bin/v1/docs/cos-param` `{"filename":"probe.html","type":"file"}` | ✅ 200，**能出 state** |

cos-param 实测响应（注意 `auth: null`、**无 `upload_url`**）：

```json
{"options": {"Bucket": "<bucket>", "Region": "<region>"},
 "object": {"key": "company_<id>/files/2026/10/<uuid>.html",
            "state": "<uuid>",
            "headers": {"Content-Type": "application/octet-stream",
                        "Content-Disposition": "attachment; filename*=utf-8''probe.html; filename=probe.html"},
            "auth": null}}
```

`type` 只接受 `file` / `attachment`（`html`/`image`/`video`/`kb_file` → 400 无效值）。

## 4. 建条目：`/kb/entries/html?state=`

| 场景 | 结果 |
|---|---|
| `POST /cgi-bin/v1/kb/entries/html`（无 state） | ❌ 400 `{"data":["不能为空"],"state":["不能为空"]}` |
| 客户端自造 UUID 当 state | ❌ 400 `code:30010001`「上传任务不存在」 |
| **cos-param 的真实 state，但对象未上传** | ❌ 400 `code:30010003`「文件不存在」 |
| 同 state + 额外塞 `content` / `html` / `data` / `base64` 内联正文 | ❌ 仍然「文件不存在」——**接口不吃内联内容** |
| 请求体（与抓包样例一致） | `{"data":{"attributes":{"name":"x.html","entry_type":"file"},"relationships":{"parent_entry":{"data":{"type":"kb_entry","id": PARENT }}}}}`；URL 只有 `?state=`，**没有 space_id** |

结论：state 合法且被受理，但**文件二进制必须先落到存储**，否则建条目必然失败。这也是本链路目前唯一断点。

## 5. 存储上传路由（未闭环）

条目详情里的下载链接暴露了私有化存储走网关代理：

```
https://<私有化网关域名>/private-mono/api/open3rd/files/csp/company_<id>/kb/entries/2026/08/<id>.psd?filename=…&sign=d985599a…
```

| 尝试 | 结果 |
|---|---|
| `GET` 该下载链接 | ✅ 路由存在（带 `sign`） |
| `PUT /private-mono/api/open3rd/files/{key}` | ❌ 404 `page not found`（路由未注册） |
| `PUT /private-mono/api/open3rd/files/csp/{key}` | ⚠️ 400 `code:41`「verification parameter error」——**路由存在，但需要签名** |
| 同路径带 `?sign=abc` / `?filename=&sign=abc` / `?state=<state>` | ❌ 同样 `code:41`（sign 必须真实有效） |
| DNS `<bucket>.cos.<region>.myqcloud.com` 等 COS 猜测域名 | ❌ NXDOMAIN（非公网 COS，Region `<region>` 是私有云命名） |
| `POST /private-mono/api/open3rd/{upload,files,object,…}`（multipart） | ❌ 404 |

**待确认一件事**：私有化客户端把文件 PUT 到哪个地址、`sign` 怎么算。抓一次前端/客户端上传的请求头即可（`/private-mono/api/open3rd/files/csp/{key}` 上的 `sign` 生成方式）。
拿到后在策略配置写 `cos_put_url_template`（必要时补 `headers`），代码无需改动即可闭环。

## 6. 本机验证过的命令

```bash
# 签名步已可走 cos-param（state 能出），卡在存储 PUT 前
scripts/html_import.py samples/01-基础富文本结构.html --config configs/私有化-min.json --no-incremental
# → {"step":"cos-put", ... "no upload_url in response …"}  ← 预期

# 注入既有 state：直达 entries/html（验证路由与报文正确性）
scripts/html_import.py samples/01-基础富文本结构.html --config configs/私有化-min.json --state <STATE> --no-incremental
# → {"step":"create-entry","http":400,"resp":{"code":30010003,"message":"文件不存在"}}  ← 预期（对象没上传）
```

公有云回归（同一次改动后复跑 `configs/default.json`）仍全绿：control_txt / control_pdf / raw_html / html_as_txt / html_bundle_zip 全 PASS，media_type=html 与 entry_type=html 仍被拒（符合 2026-10-14 基线）。

## 7. 2026-10-26 复测（`debug_private_chain.py` 逐环节打印）

新增诊断脚本后按调用顺序复跑一遍，结论与 10-23 一致；同时暴露了一个客户端 bug 并已修：

| 环节 | 请求 | 回包 | 结论 |
|---|---|---|---|
| S1 | `POST /cgi-bin/token` AK/SK | 200，token 1072 字符 / 7200s | ✅ 正常 |
| S2 | `GET /kb/spaces/{space}` | 200，「<空间名A>」，root=<ROOT_ENTRY_ID>… | ✅ 正常 |
| S3 | `GET /kb/entries/{parent}` | 200，「html接口测试」folder | ✅ 正常 |
| S4 | `POST /kb/files/upload-params` `{name,media_type}` | 400 `code:31 系统内部错误` | ⚠ 已知缺陷（改走 S5） |
| S5 | `POST /docs/cos-param` `{filename,type}` | 200，`options{Bucket:<bucket>,Region:<region>}` + `object.key/state`，`auth:null` | ✅ state 可出 |
| S6 | `PUT /private-mono/api/open3rd/files/csp/{key}` | 400 `code:41 verification parameter error` | ❌ **断点**（缺 sign） |
| S6' | `PUT /private-mono/api/open3rd/files/{key}` | 404 page not found | ❌ 路由不存在 |
| S6'' | `PUT https://<bucket>.cos.<region>.myqcloud.com/{key}` | 代理 502（域名不可达） | ❌ 非公网 COS |
| S7 | `POST /kb/entries/html?state=<S5 的 state>` | 400 `code:30010003 文件不存在` | 报文正确，仅差对象未上传 |

**修复**：`cos-param` 的 `options` 在回包**顶层**、与 `object` 平级；此前 `cos_put` 从 `object.options` 取 Bucket/Region，导致模板拼出空域名（idna 报错）。已新增 `OpenApi.extract_object()` 统一合并，`html_import.py` / `test_upload_chain.py` / `debug_private_chain.py` 全部改用它。

复跑公有云回归仍全绿（5 PASS + 2 rejected + cleanup 全 204），改动无副作用。

## 8. 2026-10-26 追加：测试环境 header `X-Zerus-Env`

测试环境每个请求还须带 `X-Zerus-Env: base-xxxx`（用户确认）。已写入策略配置的 `headers`（与 `User-Agent: ifbook-http-client` 一起），`OpenApi._request` 会把它合并进**所有**请求（含存储 PUT），公有云策略不受影响（`configs/default.json` 无 headers）。

> **2026-10-26 更正（用户）**：min 私有化环境**不需要** X-Zerus-Env（已从 `configs/私有化-min.json` 移除）；该 header 属于测试网关 `<测试网关域名>` 的策略（见 §11）。`test_private_chain.py` 的 T1 已改为纯配置自检（展示 X-Zerus-Env 是否配置）。

加 header 后复跑 `debug_private_chain.py`：8 次请求全部带上该 header（日志中 16 处 = 每请求的 header 列表 + curl 行），S1/S2/S3/S5 结果不变，**S6 仍 400 `code:41 verification parameter error`** —— 说明网关已接受该 header，断点仍是存储上传缺 `sign`，与本 header 无关。

## 9. 2026-10-26 追加：`/kb/entries/html` 的 state 从哪来（排查记录）

疑问：`POST /cgi-bin/v1/kb/entries/html?state=` 是**新增的 html 专用建条目接口**，为什么常规诊断里看不到它？
答：诊断脚本在 S6（存储 PUT）失败即停，S7 没跑到。加 `--continue-on-fail` 后可见（2026-10-26 实测）：

```
  #9  →  POST /cgi-bin/v1/kb/entries/html?state=14de8055-…&company_from=<值>
      headers : Content-Type / User-Agent / X-Zerus-Env / Authorization / x-staff-id
      body    : {"data":{"attributes":{"name":"测试01 - 基础富文本结构","entry_type":"file"},
                 "relationships":{"parent_entry":{"data":{"type":"kb_entry","id":"<PARENT_ENTRY_ID>…"}}}}}
  #9  ←  ✘ HTTP 400  {"code":30010003,"message":"文件不存在"}
```

即：**接口本身可用、state 也被受理**，失败原因是该 state 对应的文件对象没上传成功（S6）。

顺带确认两件事：

1. **不存在 html 专用的签名接口**。探测 `/cgi-bin/v1/docs/html-param`、`/kb/files/html-params`、`/kb/entries/html-param`、`/kb/html-params`、`/v1/html/upload-params`、`/kb/html/upload-params` —— 全部 404（返回 SPA 兜底 HTML 或 `code:12 Not Found`）。state 只能来自 `cos-param`（或标准 `upload-params`）这条上传链。
2. **别猜 `sign`**。用 `md5(key)`、`md5(key+secret)`、`md5(secret+key)`、`md5(key+state)`、`md5(state)`、`md5(key+state+secret)` 依次 PUT `/private-mono/api/open3rd/files/csp/{key}?sign=…`，**全部 400 `code:41`**。结论不变：需抓包确认（含 `sign` 的入参与时间戳规则）。

## 10. 2026-10-26 追加：矩阵回归脚本 `test_private_chain.py`（T1-T11）

把上述逐接口实测固化成回归矩阵（`scripts/test_private_chain.py`，默认 `configs/私有化-min.json`），
服务端升级 / 换网关 / 改配置后跑一遍即可判断链路行为是否变化。

**verdict 语义**：PASS=符合已实测行为（含预期失败）；BLOCK=已知环境断点（不算失败）；FAIL=行为回归（exit 非零）；SKIP=前置缺失。

| # | 用例 | 预期（已实测行为） | 本次结果 |
|---|---|---|---|
| T1 | headers 配置自检 | X-Zerus-Env + User-Agent 均在 | PASS |
| T2 | token 交换 | 200，token len=1072 | PASS |
| T3 | 空间详情 | 200「<空间名A>」 | PASS |
| T4 | 目标目录 | 200「html接口测试」folder | PASS |
| T5 | 标准 `upload-params` | 400 `code=31`（该部署签名步坏） | PASS（预期失败） |
| T6 | `cos-param` 策略签名 | 200，有 state，Bucket=lexiang | PASS |
| T7 | `/kb/entries/html` 无 state | 400「state 不能为空」 | PASS（预期失败） |
| T8 | `/kb/entries/html` 假 state | 400 `code=30010001`「上传任务不存在」 | PASS（预期失败） |
| T9 | `/kb/entries/html` 真 state 未传字节 | 400 `code=30010003`「文件不存在」 | PASS（预期失败） |
| T10 | 存储 PUT（gateway-csp / gateway-raw / cos-guess 三候选） | —— | **BLOCK**（400 code=41 / 404 / 不可达） |
| T11 | 端到端（签名→PUT→建条目） | —— | SKIP（无可用已上传 state） |

**summary: PASS 9 / BLOCK 1 / FAIL 0 / SKIP 1，exit 0**。

回归判据：`cos_put_url_template` 补齐（sign 拿到）后 T10/T11 应转 PASS，其余 9 项不应变化；
若 T5 转为 200（标准签名接口被修复）或 T7-T9 报错码变化，说明服务端行为变更，需更新 SKILL.md 对照表与本表。

## 11. 2026-10-26 追加：min 移除 X-Zerus-Env + 测试环境 <测试网关域名> 探测

**变更**：用户确认 min 私有化不需要 `X-Zerus-Env` header（已从 `configs/私有化-min.json` 移除，该 header 归属测试环境策略）；min 的上传报错疑似环境本身问题。

**测试环境**（`https://<测试网关域名>` + `X-Zerus-Env: base-xxxx`，策略 `configs/测试环境.json`）：

| 尝试 | 结果 |
|---|---|
| min AppKey 换 token（± company_from=<值>、± X-Zerus-Env、company_from=company_<id>… 共 5 组合） | ❌ 400 `app key 是无效的.` |
| 公有云 AppKey 换 token（± company_from、± X-Zerus-Env 共 4 组合） | ❌ 同样无效（对照：该凭证在 `lxapi.lexiangla.com` 正常 200 出 token，凭证本身有效） |
| `.com` 换的 token 直接调 `.net` + X-Staff-Id + X-Zerus-Env | ❌ 403 `接口凭证无效` |

结论：`<测试网关域名>` 是**独立凭证体系**的测试网关（server: istio-envoy，腾讯 EdgeOne 前置），现有两套 AppKey 与公有云 token 都不被接受。→ 用户随后提供了测试环境专属凭证（AppKey `<APP_KEY>…`，见下节）。

## 12. 2026-10-26 终章：测试环境 `<测试网关域名>` **全链打通**

用户提供测试环境凭证（AppKey `<APP_KEY>`）后逐环节验证：

| 环节 | 请求 | 回包 | 结论 |
|---|---|---|---|
| token | `POST /cgi-bin/token`（AppKey `<APP_KEY>…`） | 200，token 1074 字符 / 7200s | ✅ staff=`<staff>` 在该环境存在（`<测试staff>` 报 71「不存在的StaffID」） |
| 空间发现 | `GET /cgi-bin/v1/teams` → `GET /kb/spaces?team_id=<UUID>` | 200 | ✅ 找到 team `<团队名>` 的空间「1」（`<SPACE_ID_B>…`，root=`<ROOT_ENTRY_ID>…`）；min 的空间/目录在此环境不存在（独立数据集） |
| S4 标准签名 | `POST /kb/files/upload-params` | 200 带 `upload_url` | ⚠ **PUT 报 403 `InvalidAccessKeyId`**——回包里的 COS 临时密钥已失效（该环境已知缺陷，即用户说的「环境有些问题」），**不可走** |
| S5 cos-param | `POST /docs/cos-param` | 200，`auth{Authorization,XCosSecurityToken}` 临时密钥对**有效** | ✅ 签名步用它 |
| S6 存储 PUT | `PUT https://<bucket>.cos.ap-shanghai.myqcloud.com/{key}` + Authorization + x-cos-security-token | **200** | ✅ `cos_put_url_template` 填 `"https://{bucket}.cos.{region}.myqcloud.com/{key}"` |
| S7 建条目 | `POST /kb/entries/html?state=` / `POST /kb/entries?state=&space_id=` | 200 | ✅ 两条路由均可用 |

**调试过程中修的两个真 bug（都已在 `openapi_client.py` 修复）**：

1. **`/kb/entries/html` 要求 name 带 `.html/.htm` 后缀**：无后缀报 400「HTML 知识仅支持 .html/.htm 单文件或 .zip 包」；服务端展示时自动剥后缀。修复：`create_file_entry` html 分支自动补后缀——**必须发生在构造 payload 之前**（第一版修复改在 payload 构造之后改 name 变量，body 没变，白改）。
2. **`extra_headers`（X-Zerus-Env）原被合并进所有请求含 COS PUT**：现在按 URL 前缀限定为 api_base（与 `extra_query` 一致）。网关 header 发给 COS 会让对象被建条目接口拒绝。

**curl 坑**：该网关 POST `--data-raw @file` 报「参数错误」，须内联 body（Python requests 不受影响）。

**最终验证**：`scripts/html_import.py --config configs/测试环境.json`（source=samples 目录）9 个文件批量上传**全部 succeed**（4 raw + 2 zip bundle + 3 内联 raw），重跑全 skip（幂等）；公有云回归同代码复跑仍全绿。

## 13. 2026-10-26 补：正式目标 + company_from

用户随后提供正式配置：`company_from=<值>`、目标知识库 `<SPACE_ID>`（「<空间名B>」）、目标目录 `<PARENT_ENTRY_ID>`（「html接口导入测试」，与 min 的「html接口测试」目录对应）、staff_id 确认 `<staff>`。

- 初次探测期间误传到空间「1」根目录的 9 个条目已全部删除（204）+ 旧 manifest 清除（防 hash 命中 skip）
- 换正式目标后重传：samples 9 文件**全部 succeed**，目录列表核实 9 条目均挂在「html接口导入测试」下
- `company_from=<值>` 走 `extra_query`（用户提供；AppKey 本身已绑定租户 company_<id>，从 cos-param 的 object.key 前缀可见——company_from 是网关路由参数，与租户绑定是两回事）

## 14. 2026-10-26 追加：前端渲染空白 → Content-Type 修复

**症状**：用户反馈条目建成功但前端内容区空白（无报错，仅空白 + 加载圆点）。

**排查**：对象 GET 内容完好；`GET /kb/entries/{id}/content?content_type=html` 报「entry type is not page or smartsheet」（该接口是文档内容读取，与 file 条目无关，非本因）；`entry_type=page` 传给 `/kb/entries/html` 被服务端强制改回 `file`。对照公有云签名回包发现**决定性差异**：

| 项 | 公有云 upload-params（可预览） | 测试环境 cos-param（空白） |
|---|---|---|
| headers.Content-Type | **text/html** | **application/octet-stream** |
| object.key 前缀 | `kb/entries/…` | `files/…` |

**修复**：HTML 字节 PUT 时覆盖 `Content-Type: text/html`（COS 签名 q-header-list 为空，换 header 不破坏签名）→ 对象元数据变 text/html → 条目可渲染。已实现为 `cos_put(force_content_type=)`：`upload_bytes` 按文件名 `.html/.htm` 后缀自动传 `text/html`（zip bundle 保持 octet-stream）；html_import 的 raw 原地更新分支同样处理。

**验证**：删旧 10 条目 + 清 manifest 后全量重传 9 文件全 succeed，抽查对象 content-type 均为 `text/html`；公有云回归仍 5 PASS。SaaS 路径 headers 本来就是 text/html，行为不变。
