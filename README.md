# upload-html-to-lexiang-by-openapi

用乐享**开放接口**（AppKey/AppSecret 或 Bearer token）上传 HTML / HTML bundle ZIP 到腾讯乐享知识库——MCP 版 [`upload-html-to-lexiang`](../upload-html-to-lexiang) 的 OpenAPI 通道克隆。**`api_base` + 全部路由配置化，公有云 / 私有化部署同一套脚本。**

完整说明、工作流、踩坑预警见 [SKILL.md](SKILL.md)；实测数据见 [references/](references/)（公有云 2026-10-14、私有化 2026-10-23）。

## 脚本分工

| 脚本 | 定位 | 说明 |
|---|---|---|
| `scripts/html_import.py` | **正式导入脚本** | 单文件/目录批量导入、幂等重跑、raw 直传、原地更新/删除重建、`--state` 注入既有上传任务、stdout JSON |
| `scripts/debug_private_chain.py` | **链路诊断脚本** | 按调用顺序打印每个接口的请求/回包，末尾给第一个失败环节；`--state` 可跳过签名+上传 |
| `scripts/test_private_chain.py` | **矩阵回归脚本（三环境通用）** | SaaS/测试环境/min 私有化同一入口（T1-T11，按策略自适应）；verdict 含 BLOCK=已知环境断点；`--cleanup` 自清理测试条目 |
| `scripts/openapi_client.py` | 共享客户端 | token（app_key / bearer 双模式）+ 三步上传链封装，全部路径由配置驱动 |

## 快速上手

```bash
# 1. 配置（多策略：一个策略一个文件，放 configs/ 目录；configs/* 已被 .gitignore 忽略）
cp configs/config.example.json configs/default.json
#    必填：api_base / staff_id / source / space_id + 凭证
#          auth_mode=app_key → app_key + app_secret
#          auth_mode=bearer  → access_token（可写 env:LEXIANG_TOKEN，不落盘）
#    私有化差异（不改代码，只改配置）：upload_params_path/template、cos_put_url_template、html_entry_path、headers、extra_query

# 2. 矩阵回归（三环境通用，--cleanup 自删测试条目）
scripts/test_private_chain.py --config configs/default.json --cleanup

# 3. 正式导入（执行时带策略配置）
scripts/html_import.py --config configs/default.json report.html
scripts/html_import.py --config configs/prod-saas.json        # 策略配齐后零参数；定时任务用这个
scripts/html_import.py --config configs/私有化-min.json --state <STATE>   # 私有化：注入既有上传任务
```

manifest 幂等状态按策略自动隔离：`configs/foo.json` → `manifests/foo.json`。

## 状态

**公有云（2026-10-14 实测）**

- ✅ raw `.html` 与 bundle `.zip` 三步链路全 PASS（文档后缀白名单非硬校验）
- ✅ raw html 原地更新；zip bundle 更新 = DELETE+重建（重传接口对 zip 一律 400，脚本已自动处理）
- ✅ E2E 全绿：导入 → 幂等 skip → 更新（两种模式）→ 自清理
- ❓ **待人工确认**：OpenAPI 上传的 zip 前端是否渲染为 HTML bundle（MCP 版靠 `extension=html` 显式标记，OpenAPI 无此参数）。保留样本：空间「<空间名C>」→ `_openapi_html_kept` 目录

**私有化 `<私有化网关域名>`（2026-10-23 实测）**

- ✅ AppKey 换 token、空间/目录读取、`/kb/entries/html?state=` 建条目路由全部打通
- ⚠️ 标准签名接口 `/kb/files/upload-params` 该部署**恒 500**；改用老接口 `/docs/cos-param` 可出 state（已做成 `upload_params_template`）
- ⚠️ cos-param 不返回 `upload_url`、`auth: null` → 文件二进制的存储上传地址与 `sign` 算法**待确认**；补齐后填 `cos_put_url_template` 即闭环，代码零改动
- ✅ 过渡方案：`--state <STATE>` 注入既有上传任务，建条目/幂等/更新逻辑照常工作
