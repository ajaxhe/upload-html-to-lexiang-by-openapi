# HTML 上传接口 Cookbook（接口级调用序列）

> 2026-10-26 实测环境：测试网关 `https://<测试网关域名>`（`X-Zerus-Env: base-xxxx`），
> 凭证 AppKey 模式，租户 `company_<id>…`。公有云 `lxapi.lexiangla.com` 形态见 §5。
> 本文写给「直接调接口」的场景（不经 scripts/）；用脚本直接看 SKILL.md 工作流。

## 0. 适用边界

| 事项 | 结论 |
|---|---|
| 已验证环境 | 测试网关 <测试网关域名>（本文全部步骤实测 200）；公有云 lxapi.lexiangla.com（脚本回归全绿） |
| 未闭环环境 | <私有化网关域名>（存储 PUT 缺 sign，走本文 §4 的 --state 通道） |
| 流程 A（单 HTML） | 条目**可预览渲染**（前端直接显示网页） |
| 流程 B（zip bundle） | 条目为**文件卡片，不预览**（「暂不支持预览」）；要预览请用流程 A + 资源内联 |
| HTML 内容限制 | 乐享 WAF 拦截含 `os.system` / `__import__` / `subprocess` 危险模式的正文 |

## 1. 通用约定（每个网关请求都要）

```
Authorization: Bearer <token>          # §2 换来的
X-Staff-Id: <staff_id>                 # 操作人，须对目标空间有 editor+ 权限
X-Zerus-Env: base-xxxx        # 仅 <测试网关域名> 需要；其他环境不要带
Content-Type: application/json; charset=utf-8   # JSON 请求
query: company_from=<值>               # 值由部署方提供；按部署填
```

⚠️ 这三个网关 header **不要**发给 COS（§3 的 PUT）——发了对象会被建条目接口拒绝。

## 2. 第一步：换 token（两小时有效）

```bash
curl -s -X POST 'https://<测试网关域名>/cgi-bin/token?company_from=<值>' \
  -H 'Content-Type: application/json; charset=utf-8' \
  -H 'X-Zerus-Env: base-xxxx' \
  --data-raw '{"grant_type": "client_credentials",
               "app_key": "<APP_KEY>", "app_secret": "<APP_SECRET>"}'
```

回包：`{"token_type":"Bearer","expires_in":7200,"access_token":"eyJ0eXAi…"}`

- 错误 `app key 是无效的` → 凭证不属于这个网关（每个网关独立凭证体系，.com 的 AppKey 在 .net 无效）
- token JWT `exp` 约 7200s，过期前换新

## 3. 第二步：签名（出 state + COS 临时密钥）

```bash
curl -s -X POST 'https://<测试网关域名>/cgi-bin/v1/docs/cos-param' \
  -H 'Authorization: Bearer <TOKEN>' -H 'X-Staff-Id: <STAFF>' \
  -H 'X-Zerus-Env: base-xxxx' -H 'company_from…（query 上）' \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-raw '{"filename": "报告.html", "type": "file"}'
```

**⚠️ body 必须内联，`--data-raw @file` 在该网关报「参数错误」（Python requests 不受影响）。**

回包（关键字段）：

```json
{"options": {"Bucket": "<bucket>", "Region": "ap-shanghai"},
 "object": {
   "key":    "company_855…/files/2026/10/<uuid>.<ext>",   ← COS 对象路径
   "state":  "<uuid>",                                    ← 第四步建条目用
   "headers": {"Content-Type": "application/octet-stream", ← ⚠️ 见第三步覆盖规则
               "Content-Disposition": "attachment; filename*=…"},
   "auth":   {"Authorization": "q-sign-algorithm=sha1&q-ak=…", ← COS PUT 用
              "XCosSecurityToken": "<长串>"}}}
```

- `type` 枚举只有 `file` / `attachment`（html/image/video 都报「选定的 type 是无效的」）
- **`options`（Bucket/Region）在回包顶层**、与 `object` 平级——自写解析别只取 `object`
- `filename` 决定对象 key 的后缀与 Content-Disposition；**流程 A 须带 `.html`，流程 B 须带 `.zip`**

## 4-A. 流程 A：单 HTML 文件（可预览）

第三步之后依次：

**③ PUT 文件字节到 COS（Content-Type 必须覆盖为 text/html！）**

```bash
curl -X PUT 'https://<bucket>.cos.ap-shanghai.myqcloud.com/<object.key>' \
  -H 'Authorization: <auth.Authorization>' \
  -H 'x-cos-security-token: <auth.XCosSecurityToken>' \
  -H 'Content-Type: text/html' \
  -H 'Content-Disposition: attachment; filename=报告.html' \
  --data-binary @报告.html
```

- URL 拼法：`https://{options.Bucket}.cos.{options.Region}.myqcloud.com/{object.key}`
- **🔴 决定性坑：cos-param 签的 Content-Type 是 `application/octet-stream`，照抄上去条目能建成功但前端渲染一片空白（无报错）**。HTML 必须覆盖成 `text/html`（COS 签名 q-header-list 为空，换 header 不破坏签名；公有云 upload-params 本来就回 text/html）
- 预期回包：HTTP 200 空体

**④ 建条目（html 专用路由，name 必须带 .html 后缀）**

```bash
curl -s -X POST 'https://<测试网关域名>/cgi-bin/v1/kb/entries/html?state=<object.state>&company_from=<值>' \
  -H 'Authorization: Bearer <TOKEN>' -H 'X-Staff-Id: <STAFF>' \
  -H 'X-Zerus-Env: base-xxxx' -H 'Content-Type: application/json; charset=utf-8' \
  --data-raw '{"data": {"attributes": {"name": "报告.html", "entry_type": "file"},
              "relationships": {"parent_entry": {"data": {"type": "kb_entry",
              "id": "<目标目录 entry_id>"}}}}}'
```

回包：`{"data":{"type":"kb_entry","id":"<EID>","attributes":{"name":"报告",…}}}`

- **🔴 name 不带 `.html/.htm` 后缀 → 400「HTML 知识仅支持 .html/.htm 单文件或 .zip 包」**；服务端展示时会自动剥后缀（建出来的条目名不带 .html），所以放心补
- `entry_type` 只认 `file`（传 `page` 会被强制改回）
- query **只有 `state`，不带 space_id**——目标由 body 的 `parent_entry` 定位
- 前端预览读取的就是这个 COS 对象（text/html 才渲染）；条目详情 `GET /cgi-bin/v1/kb/entries/<EID>` 回 `links.download` 可验证内容

## 4-B. 流程 B：zip bundle（index.html + 本地图片/视频，文件卡片不预览）

**① 打包（客户端做，服务端不管）**：
- 主 HTML 文件**必须改名 `index.html`** 放 zip 根
- 本地资源（图片/视频/字体…）按**原始相对路径**放（如 `assets/pic.jpg`），HTML 里引用不改
- 缺失资源不要静默丢弃：HTML 里注入 `[资源缺失：xxx]` 占位

**② 签名**：同 §3，但 `filename` 用 `<名字>.zip`。

**③ PUT 到 COS**：

```bash
curl -X PUT 'https://…cos…/<object.key>' \
  -H 'Authorization: <auth.Authorization>' \
  -H 'x-cos-security-token: <auth.XCosSecurityToken>' \
  -H 'Content-Type: application/octet-stream' \
  -H 'Content-Disposition: attachment; filename=<名字>.zip' \
  --data-binary @bundle.zip
```

- zip 是二进制，**保持 octet-stream，不要覆盖 text/html**

**④ 建条目（走标准路由，带 space_id）**：

```bash
curl -s -X POST 'https://<测试网关域名>/cgi-bin/v1/kb/entries?state=<object.state>&space_id=<SPACE_ID>&company_from=<值>' \
  -H 'Authorization: Bearer <TOKEN>' -H 'X-Staff-Id: <STAFF>' \
  -H 'X-Zerus-Env: base-xxxx' -H 'Content-Type: application/json; charset=utf-8' \
  --data-raw '{"data": {"attributes": {"name": "<名字>.zip", "entry_type": "file"},
              "relationships": {"parent_entry": {"data": {"type": "kb_entry",
              "id": "<目标目录 entry_id>"}}}}}'
```

- 实测（测试环境）：05 附件超链接 / 06 混合内容两个 zip 均 200 建成
- 条目形态：**文件卡片，前端不预览**（要预览就别打 zip：图片/小视频 base64 内联进单 HTML 走流程 A）
- 更新语义：zip 条目原地重传实测**一律 400**（「不允许重复上传不同文件类型文件」）→ 更新 = DELETE 旧条目 + 重走全流程（entry_id 会变）

## 5. 公有云形态对照（lxapi.lexiangla.com）

| 环节 | 差异 |
|---|---|
| 签名 | 用标准 `POST /cgi-bin/v1/kb/files/upload-params`，body `{"name":"x.html","media_type":"file"}`；回包 `object.upload_url` 是**带签名的直传 URL**（headers 也回好了，Content-Type 本来就是 `text/html`），PUT 直接用，不需要 cos-param / auth 临时密钥 |
| 建条目 | `POST /kb/entries?state=&space_id=`（无 html 专用路由；name 后缀规则同上） |
| headers | 无 X-Zerus-Env、无 company_from |
| 其余 | 与本文一致 |

## 6. 错误码速查

| 码 | 含义 | 动作 |
|---|---|---|
| `app key 是无效的` | 凭证不属于该网关 | 换该网关的 AppKey，别试 company_from 组合 |
| `400 state 不能为空` | 建条目 query 没带 state | 检查 URL |
| `30010001 上传任务不存在` | state 是自造的/没注册 | state 只能来自签名接口回包 |
| `30010003 文件不存在` | state 合法但对象字节没 PUT 成功 | 回看第三步 PUT 是否真 200 |
| `400 HTML 知识仅支持 .html/.htm 单文件或 .zip 包` | 建条目 name 无后缀 | name 补 `.html`（流程 A）/ `.zip`（流程 B） |
| `400 选定的 type 是无效的` | cos-param type 传了 file/attachment 之外 | 只用 `file` |
| COS `403 InvalidAccessKeyId` | 用了 upload-params 的 upload_url（测试环境密钥失效） | 改走 cos-param + auth 临时密钥对 |
| COS `403 AccessDenied` | PUT 没带 auth.Authorization / x-cos-security-token | 从 cos-param 回包取并带上 |
| 前端空白无报错 | 对象 Content-Type 是 octet-stream | 重传，PUT 覆盖 `Content-Type: text/html` |
| `400 参数错误`（curl） | POST 用了 `--data-raw @file` | 改内联 body |

## 7. 脚本通道（推荐入口）

以上序列已封装为 `scripts/html_import.py`（自动选 raw/zip、幂等、失败重传）：
单文件/目录批量/原地更新见 SKILL.md「正式脚本」；流程 A/B 的模式判定（auto/raw/bundle）见 SKILL.md「上传模式」。自写代码时推荐直接 import `scripts/openapi_client.py` 的 `OpenApi.upload_bytes()`（三步链 + Content-Type 覆盖已内置）。
