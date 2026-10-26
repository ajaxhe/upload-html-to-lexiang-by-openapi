# OpenAPI 链路实测结果（2026-10-14）

环境：<测试staff> 测试 AppKey（lexiang-openapi-skill 的 `test/config.local.json`），空间「<空间名C>」`<SPACE_ID>`，SaaS api_base `https://lxapi.lexiangla.com`。
工具：`scripts/test_upload_chain.py`（回归脚本，`_openapi_html_probe` 临时目录，自清理）。
注意：以下为**实测**结论，推翻了接口文档「上传文件」entry_type=file 后缀白名单（图片/Office/pdf/txt）会硬拦截 html/zip 的推断——白名单只描述预览方式，不做上传校验。

## 三步链路矩阵

| 测试 | 结果 | 说明 |
|---|---|---|
| control.txt / control.pdf | ✅ PASS | 正对照，链路本身通 |
| raw `.html` 直传 | ✅ PASS | 签名 200 → COS PUT 200 → 建条目 200 |
| HTML bundle zip（index.html + assets/） | ✅ PASS | 同上，能传上去 |
| html 内容改 .txt 后缀 | ✅ PASS | 服务端不嗅探内容 |
| `upload-params` 传 media_type=html | ❌ 400「选定的值是无效的」 | 枚举仅 file/video/audio |
| 建条目传 entry_type=html | ❌ 400「选定的值是无效的」 | 枚举仅 video/audio/file |
| DELETE entry / folder | ✅ 204 | 自清理可行；folder 须先删子节点 |

## 重新上传文件（原地更新）矩阵

| 测试 | 结果 | 说明 |
|---|---|---|
| html → html | ✅ 200 | raw html 条目可原地更新（entry_id 不变） |
| html → zip | ⚠️ 200 且**真实生效** | ext 一致性校验形同虚设，更新脚本必须自校验新旧扩展名 |
| zip → zip（同名/异名字节一致） | ❌ 400「不允许重复上传不同文件类型文件」 | **zip 条目（含 bundle）原地更新是死路** |
| zip → html | ❌ 400 同上 | 无解，用 DELETE+重建 |

结论已固化到 `scripts/html_import.py` 的更新语义：raw 走 `PUT /kb/entries/{id}/upload?state=` 原地更新；zip 走删除重建（entry_id 会变，manifest 自动记录新 id）。

## 正式脚本 E2E（2026-10-14，同一环境）

| 步骤 | 结果 |
|---|---|
| 导入 04-图片-相对路径.html（6 资源 bundle） | ✅ succeed，entry_id 9576be58...，名称取 `<title>` |
| 幂等重跑 | ✅ skipped（manifest content_hash 命中，复用 entry_id） |
| `--entry-id` 更新 zip bundle | ✅ zip-recreated：旧 9576be58 → 新 4d5e1aa4，manifest 同步 |
| `--raw` 导入 01-基础富文本结构.html | ✅ 直传 .html，zip_mode=False |
| `--entry-id --raw` 原地更新 | ✅ raw-inplace：entry_id 31de9f86 不变 |
| 清理（2 entry + folder + 临时 manifest） | ✅ 全 204 |

## 未验证（需人工确认）

~~OpenAPI 传上去的 zip，乐享前端是否按 HTML bundle 渲染~~ **已确认（2026-10-14 晚，用户前端实测）：zip bundle 显示为文件卡片「暂不支持预览」；raw .html（文件名 .html 后缀）正常渲染。**结论：OpenAPI 链路对 HTML 预览的支持一直都在，判断依据是**上传文件本身的 .html 后缀**——无需 MCP 版的 extension=html 参数。raw 路径即正解，zip bundle 不预览。

据此 `html_import.py` 已改默认 **auto 模式**（2026-10-14）：
- 无资源 → raw 直传；图片/css/js → base64 内联为 data URI 后 raw（用户已确认 04 内联版渲染正常）
- 含本地引用的 srcset **整体剥离**（data URI 含逗号，与 srcset 逗号分隔语法冲突，靠已内联的 src 回退）——实测坑
- 附件类（csv/pdf/txt/zip）不可预览内联 → zip 兜底（不预览，已知缺口）
- content_hash 加入 mode：修掉「先 zip 后 raw 重传被 skip」幂等盲区

附加实验条目（「html导入测试」文件夹，待前端确认后清理）：
- `验证-05-附件dataURI` entry `ad0e022a62eb4987bb79282d93c53f0a`——附件(csv/pdf/txt/zip)全部内联为 data: 超链接，验证渲染器是否保留可下载的 data URI 链接；若可行则 05/06 类也能走 raw
- `验证-09-视频dataURI` entry `777472db36e149d5bd958ee26b2c23c3`——**已确认（2026-10-14 用户实测）：html 可预览，1.13MB mp4 base64 内联可正常播放**。COS 接受 1.5MB 级 raw .html，大小不构成障碍；data URI 视频无法流式（整段载入后才能拖动），大视频应拆独立视频条目（media_type=video）。据此 INLINEABLE 已纳入视频/音频后缀（单文件 ≤10MB，超限降级 zip 并在 mode_reason 说明），上限未实测

## 视频+zip 矩阵（2026-10-14 晚，「html导入测试」文件夹，待前端确认）

| # | 变量 | API 结果 | 前端形态 |
|---|---|---|---|
| 验证10 (af39e113…) | 1.13MB 视频+zip | 3 步全 200 | 待确认（预期文件卡片） |
| 验证11 (f5712a6b…) | 5.23MB 视频+zip | 3 步全 200，**5MB 级 zip 无障碍** | 待确认 |
| 验证12 (6e8a7939…) | 双视频+poster+zip | 3 步全 200 | 待确认 |
| 验证-13 (68429b2f…) | zip 内容 + `upload-params(media_type=video)` + `create(entry_type=video)` | **全 200，服务端以 entry_type=video 建档** | 待确认——若前端因此出播放器（哪怕播不了），说明条目形态由 entry_type 驱动，可作 zip 通道的渲染开关 |

**列表端点发现（已封装为 `openapi_client.entry_children(space_id, parent_id)`）**：`GET /cgi-bin/v1/kb/entries?space_id=X&parent_id=Y&per_page=100`。坑：过滤参数是 **`parent_id`**（`parent_entry_id` 被静默忽略返回未过滤列表）；缺 `space_id` 直接 400。这是 API 层唯一能枚举/找回 entry_id 的手段（entry 详情无形态字段，渲染仍需前端人工确认）。

保留样本在空间「<空间名C>」`_openapi_html_kept` 目录（待页面人工确认后清理）：
- `OpenAPI-直传html` entry `a401412bd3924e68918f9807b60030c3`（raw 渲染已由 2026-10-14 验证条目证实，可清理）
- `OpenAPI-zip-bundle` entry `2d71cadc9c39461989d04b38c9dd1599`（已证实为文件卡片，可清理）

## 凭证与权限实况（同日）

| 凭证 | 状态 |
|---|---|
| 主 AppKey（`~/.config/lexiang/credentials`，staff <staff>） | token OK 但知识库/上传接口全 403「未授权接口权限」——AppKey 未勾接口，暂不可用 |
| kefu key | 跨租户（对测试空间 404），不可用 |
| <测试staff> 测试 key | ✅ 全链路可用（当前唯一可用） |
| MCP 个人 token（csig） | 与 <测试staff> 不同租户，无法做 MCP/OpenAPI 同空间对照 |

坑：`/kb/personal-space` 接口对 <测试staff>/kefu key 返回 400「staff_id 不能为空」（x-staff-id 传了也一样），对主 key 403——勿依赖，直接传 space_id 走 `/kb/spaces/{id}`。
