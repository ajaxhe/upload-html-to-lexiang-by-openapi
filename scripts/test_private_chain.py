#!/usr/bin/env python3
"""Matrix regression for the privatized (min) Lexiang HTML upload chain.

Asserts, hop by hop, that the private gateway behaves as probed and documented
(references/privatized-probe-2026-10-23.md). Verdict per case:

  PASS   actual matches the verified expectation (including EXPECTED error
         codes — e.g. /kb/files/upload-params code=31 is the documented defect)
  BLOCK  a hop the deployment cannot serve today (storage PUT without a sign).
         NOT a failure: it is the known breakpoint. Re-run once
         cos_put_url_template is configured (or pass --state).
  FAIL   actual deviates from the documented behaviour — regression somewhere.
  SKIP   not executed (needs --state / a working cos_put_url_template).

Chain under test (private the privatized gateway), and where the new html route
sits — it REPLACES the create-entry hop only:
  ① signature   POST /cgi-bin/v1/docs/cos-param  {filename,type}      -> state
                 (public /kb/files/upload-params is probed as evidence)
  ② storage     PUT  <candidate urls>                             -> bytes
  ③ create      POST /cgi-bin/v1/kb/entries/html?state=  (html route,
                 parent_entry locates the target, no space_id in query)
     semantics: no state      -> 400 state 不能为空
                fake state    -> 400 code 30010001 上传任务不存在
                real state, object not uploaded -> 400 code 30010003 文件不存在
  ④ e2e         bind a state that HAS an uploaded object -> entry_id
                 (only with --state or a working cos_put_url_template)

Usage:
  test_private_chain.py [--config configs/私有化-min.json] [--state STATE] [--cleanup]

stdout: per-case lines + final JSON summary. Exit 0 unless a FAIL exists.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from openapi_client import OpenApi, load_config

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "configs" / "私有化-min.json"
DEFAULT_FILE = SKILL_ROOT / "samples" / "01-基础富文本结构.html"

ICONS = {"PASS": "✔", "BLOCK": "⛔", "FAIL": "✘", "SKIP": "⏭"}
results: list[dict] = []


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def add_case(no: str, name: str, verdict: str, expect: str, actual: str,
             detail: dict | None = None) -> None:
    rec = {"case": f"T{no}", "name": name, "verdict": verdict,
           "expect": expect, "actual": actual}
    if detail:
        rec["detail"] = detail
    results.append(rec)
    print(f"  {ICONS[verdict]} T{no:<3} {name:<22} {verdict:<5} "
          f"expect: {expect[:40]:<42} got: {actual[:46]}")


def code_of(resp) -> int | None:
    return resp.get("code") if isinstance(resp, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--state", default=None,
                    help="a state whose object IS uploaded -> runs the e2e case")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--cleanup", action="store_true",
                    help="delete the entry created in the e2e case")
    args = ap.parse_args()

    conf = load_config(str(args.config))
    api = OpenApi(conf)
    space_id = conf.get("space_id")
    parent_id = conf.get("parent_entry_id")
    html_path = conf.get("html_entry_path") or ""

    print(f"私有化链路矩阵测试  api_base={api.api_base}  staff={api.staff_id}")
    print(f"  ①签名步 = {api.paths['upload_params_path']}")
    print(f"  ③建条目 = {html_path or '(未配置 html_entry_path)'}"
          + ("" if html_path else " — T7-T9 需要 html_entry_path"))

    # ---- T1 静态自检：策略 headers ---------------------------------------
    # 2026-10-26 起改为纯配置自检（不再按网关域名猜测）：X-Zerus-Env 是
    # 测试网关专用 header（值见 SKILL.md 私有化对照表），配了就展示，
    # 没配提示一句（漏配时 T2 起的网关 4xx 会暴露，不必在此 FAIL）。
    hdrs = {k.lower(): v for k, v in (conf.get("headers") or {}).items()}
    has_env = "x-zerus-env" in hdrs
    has_ua = "user-agent" in hdrs
    env_val = str(hdrs.get("x-zerus-env", "")) or ""
    add_case("1", "headers 配置自检",
             "PASS",
             "X-Zerus-Env 为测试网关专用（漏配会在后续用例暴露）；User-Agent 可选",
             f"X-Zerus-Env={'有(' + env_val[:9] + '…)' if has_env else '无'}"
             f" User-Agent={'有' if has_ua else '无'}")

    # ---- T2 token ------------------------------------------------------
    ok, resp = api.get_token()
    add_case("2", "token 交换",
             "PASS" if ok else "FAIL",
             "POST /cgi-bin/token → 200 access_token (expires_in=7200)",
             "200, token len=%d" % len(api.token or "") if ok else
             json.dumps(resp, ensure_ascii=False)[:80])

    if not ok:
        return finish()

    # ---- T3 空间 & T4 目标目录 ------------------------------------------
    st, sd = api.space_detail(space_id)
    sname = ((sd or {}).get("data", {}) or {}).get("attributes", {}).get("name", "") \
        if isinstance(sd, dict) else ""
    add_case("3", "空间详情",
             "PASS" if st == 200 else "FAIL",
             f"GET /kb/spaces/{space_id} → 200",
             f"http {st} {sname[:20]}")

    # ---- T4 目标目录（无 parent 的策略如 SaaS 默认形态 → SKIP） ----------
    if not parent_id:
        add_case("4", "目标目录详情", "SKIP",
                 "GET /kb/entries/<parent> → 200 folder",
                 "策略未配 parent_entry_id（SaaS 形态，建到空间根目录）")
    else:
        st, det = api.entry_detail(parent_id)
        etype = ((det or {}).get("data", {}) or {}).get("attributes", {}).get("entry_type") \
            if isinstance(det, dict) else None
        add_case("4", "目标目录详情",
                 "PASS" if st == 200 and etype == "folder" else "FAIL",
                 f"GET /kb/entries/{parent_id} → 200 folder",
                 f"http {st} {etype or '?'}")

    # ---- T5 标准签名接口（公有云路径，作证据） ---------------------------
    st, sig = api._json(*api._request(
        "POST", f"{api.api_base}/cgi-bin/v1/kb/files/upload-params",
        {"name": "probe.html", "media_type": "file"}, auth=True, staff=True))
    c = code_of(sig)
    if st == 200:
        log("note: public upload-params works on this gateway now — "
            "consider dropping upload_params_path from the policy")
    add_case("5", "标准 upload-params",
             "PASS" if (st == 200 or c == 31) else "FAIL",
             "POST /kb/files/upload-params → 400 code=31（已知缺陷，200 也算通过）",
             f"http {st} code={c}")

    # ---- T6 策略签名步（配置的路径；min=cos-param，SaaS=upload-params） ---
    st, sig = api.upload_params("probe-matrix.html", "file")
    obj = api.extract_object(sig)
    state = (obj or {}).get("state")
    add_case("6", "策略签名步（按配置）",
             "PASS" if st == 200 and bool(state) else "FAIL",
             "POST /docs/cos-param → 200，返回 object.state + key",
             f"http {st} state={'有' if state else '无'} "
             f"Bucket={(obj or {}).get('options', {}).get('Bucket')}",
             {"state": state, "key": (obj or {}).get("key")})

    if not state:
        return finish()

    # ---- T7-T9 /kb/entries/html 语义矩阵 --------------------------------
    if html_path:
        payload = {"data": {"attributes": {"name": "probe-matrix.html",
                                           "entry_type": "file"},
                            "relationships": {"parent_entry": {"data": {
                                "type": "kb_entry", "id": parent_id}}}}}

        st, r = api._json(*api._request("POST", f"{api.api_base}{html_path}",
                                        payload, auth=True, staff=True))
        no_state_err = "不能为空" in json.dumps(r, ensure_ascii=False)
        add_case("7", "html 建条目·无 state",
                 "PASS" if st == 400 and no_state_err else "FAIL",
                 "POST /kb/entries/html（无 state）→ 400 state 不能为空",
                 f"http {st} {str(r)[:50]}")

        fake = str(uuid.uuid4())
        st, r = api._json(*api._request(
            "POST", f"{api.api_base}{html_path}?state={fake}", payload,
            auth=True, staff=True))
        add_case("8", "html 建条目·假 state",
                 "PASS" if st == 400 and code_of(r) == 30010001 else "FAIL",
                 "POST /kb/entries/html?state=<fake> → 400 code=30010001",
                 f"http {st} code={code_of(r)}")

        st, r = api._json(*api._request(
            "POST", f"{api.api_base}{html_path}?state={state}", payload,
            auth=True, staff=True))
        add_case("9", "html 建条目·真 state 未传字节",
                 "PASS" if st == 400 and code_of(r) == 30010003 else "FAIL",
                 "POST /kb/entries/html?state=<real> → 400 code=30010003",
                 f"http {st} code={code_of(r)}")
    else:
        for no in "789":
            add_case(no, "html 建条目语义", "SKIP",
                     "需要 html_entry_path", "未配置")

    # ---- T10 存储 PUT ---------------------------------------------------
    if args.state:
        add_case("10", "存储 PUT", "SKIP",
                 "对象已上传（--state 提供）", "跳过探测")
        put_ok = False
    else:
        data = Path(args.file).expanduser().read_bytes()
        # 2026-10-26: probe with the SAME headers as the real chain —
        # cos-param auth credentials + text/html override for .html payloads
        # (OpenApi.build_put_headers), otherwise the test gateway 403s.
        hdrs = OpenApi.build_put_headers(
            obj, OpenApi.html_content_type(Path(args.file).name))
        probe, put_ok, used = {}, False, "-"
        for label, url in storage_candidates(api, obj):
            st, body = api._request("PUT", url, raw_body=data, headers=hdrs)
            probe[label] = f"HTTP {st}"
            if st in (200, 201, 204):
                put_ok, used = True, f"{label} → HTTP {st}"
                break
        if put_ok:
            add_case("10", "存储 PUT", "PASS", "PUT <storage> → 2xx", used, probe)
        elif api.cos_put_template:
            add_case("10", "存储 PUT", "FAIL",
                     "PUT <cos_put_url_template> → 2xx（模板已配置，应通）",
                     used, probe)
        else:
            add_case("10", "存储 PUT", "BLOCK",
                     "PUT <storage> → 2xx（需 sign，已知环境断点）",
                     used, probe)

    # ---- T11 端到端 ------------------------------------------------------
    last = results[-1]["verdict"]
    e2e_state = args.state or (state if last == "PASS" else None)
    # SaaS 形态（未配 parent_entry_id）→ 解析空间根目录，保证端到端可跑
    e2e_parent = parent_id or (api.space_root_entry(space_id) if space_id else None)
    if e2e_state and last in ("PASS", "SKIP") and e2e_parent:
        eid, err = api.bind_state(e2e_state, space_id, e2e_parent,
                                  "probe-matrix-e2e.html", html=True)
        if eid:
            st, det = api.entry_detail(eid)
            link = ((det or {}).get("data", {}) or {}).get("links", {}).get("download") \
                if isinstance(det, dict) else None
            add_case("11", "端到端建条目+校验", "PASS",
                     "bind state → entry_id + GET 详情 200",
                     f"entry_id={eid} http {st} download={'有' if link else '无'}")
            if args.cleanup:
                st, _ = api.delete_entry(eid)
                log(f"cleanup DELETE {eid} -> http {st}")
        else:
            add_case("11", "端到端建条目+校验", "FAIL",
                     "bind state → entry_id",
                     json.dumps(err, ensure_ascii=False)[:90])
    else:
        add_case("11", "端到端建条目+校验", "SKIP",
                 "bind state → entry_id", "无可用 state（存储 PUT 断点）")

    return finish()


def storage_candidates(api: OpenApi, obj: dict):
    opts = obj.get("options") or {}
    bucket, region, key = opts.get("Bucket", ""), opts.get("Region", ""), obj.get("key", "")
    cands = []
    if obj.get("upload_url"):
        cands.append(("upload_url", obj["upload_url"]))
    if api.cos_put_template:
        cands.append(("cos_put_url_template",
                      api.cos_put_template.format(bucket=bucket, region=region, key=key)))
    cands += [
        ("gateway-csp", f"{api.api_base}/private-mono/api/open3rd/files/csp/{key}"),
        ("gateway-raw", f"{api.api_base}/private-mono/api/open3rd/files/{key}"),
    ]
    if bucket and region:
        cands.append(("cos-guess", f"https://{bucket}.cos.{region}.myqcloud.com/{key}"))
    return cands


def finish() -> int:
    counts = {v: sum(1 for r in results if r["verdict"] == v)
              for v in ("PASS", "BLOCK", "FAIL", "SKIP")}
    print()
    print(json.dumps({"summary": {**counts, "total": len(results)},
                      "cases": results}, ensure_ascii=False, indent=1))
    fails = [r for r in results if r["verdict"] == "FAIL"]
    if fails:
        print(f"\n❌ {len(fails)} 个用例与已实测行为不符（视为回归）：")
        for r in fails:
            print(f"   {r['case']} {r['name']}: expect {r['expect']} / got {r['actual']}")
    elif counts.get("BLOCK"):
        print(f"\n⛔ 环境断点仍在（存储 PUT 缺 sign）——非脚本/链路问题；"
              f"{counts['PASS']} 个用例符合已实测行为")
    else:
        print(f"\n✅ 全部 {counts.get('PASS', 0)} 个用例通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
