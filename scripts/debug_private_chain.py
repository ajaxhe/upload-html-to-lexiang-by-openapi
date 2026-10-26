#!/usr/bin/env python3
"""Step-by-step diagnostic for the Lexiang OpenAPI HTML upload chain.

Prints the FULL details of every hop, in call order:

  request   method + URL, scheme/host, path, query params (split one per line),
            every header (Authorization redacted unless --show-token), full body
            (JSON pretty-printed, HTML/markup verbatim, binary hexdumped),
            plus a copy-paste `curl` that reproduces the hop
  response  status, latency, every response header, body (JSON pretty-printed)

It drives the SAME client (`openapi_client.OpenApi`) that `html_import.py` uses,
so what you see here is what production does. At the end a summary names the
first broken hop.

Hops (privatized deployment in brackets):
  S1  POST /cgi-bin/token                      exchange AppKey -> access_token
  S2  GET  /kb/spaces/{space_id}               space readable? root entry id
  S3  GET  /kb/entries/{parent_entry_id}       target folder exists? type?
  S4  POST /kb/files/upload-params             PUBLIC signature step
                                               [privatized: answers 500 — kept
                                                here on purpose, as evidence]
  S5  POST <upload_params_path>                signature per policy config
                                               [privatized: /docs/cos-param]
  S6  PUT  <storage>                           upload bytes (candidate URLs are
                                               probed when the gateway returns
                                               no upload_url)
  S7  POST /kb/entries[/html]?state=           bind state -> entry
  S8  GET  /kb/entries/{entry_id}              verify + download link

Usage:
  debug_private_chain.py [--config configs/私有化-min.json] [--file x.html]
      [--name NAME] [--state STATE] [--put-url TEMPLATE] [--token TOKEN]
      [--skip-public-probe] [--cleanup]
      [--show-token] [--max-request-body N] [--max-body N] [--no-curl]
      [--save-payloads DIR]

  --max-request-body 0 (default) = print request bodies in full; N truncates.
  --max-body         response body limit (default 4000, 0 = unlimited).
  --save-payloads    dump each request body to DIR (payload_<n>.<ext>).

Exit code 0 = every executed hop succeeded; 1 = a hop failed (the first broken
hop is reported in the summary).
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from openapi_client import OpenApi, load_config

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "configs" / "私有化-min.json"
DEFAULT_FILE = SKILL_ROOT / "samples" / "01-基础富文本结构.html"

OK, BAD, SKIP, WARN = "OK", "FAIL", "SKIP", "WARN"
results: list[dict] = []
SEQ = 0
SHOW_TOKEN = False
MAX_REQ_BODY = 0
MAX_BODY = 4000
NO_CURL = False
PAYLOAD_DIR: Path | None = None
IND = "      "


# --------------------------------------------------------------------------
# pretty printing helpers
# --------------------------------------------------------------------------
def hr(ch: str = "─", n: int = 78) -> None:
    print(ch * n)


def step(no: str, title: str) -> None:
    print()
    hr("═")
    print(f"[S{no}] {title}")
    hr("═")


def pad(label: str, width: int = 12) -> str:
    """Left-pad a label by DISPLAY width (CJK chars count as 2 columns)."""
    used = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in label)
    return label + " " * max(0, width - used)


def lin(label: str, value: str) -> None:
    print(f"{IND}{pad(label)}: {value}")


def block(label: str, text: str, limit: int) -> None:
    """Print a possibly multi-line body, indented, with an optional limit."""
    if text is None:
        lin(label, "（无）")
        return
    shown = text
    cut = False
    if limit and len(shown) > limit:
        shown, cut = shown[:limit], True
    print(f"{IND}{pad(label)}:")
    for line in shown.splitlines() or [""]:
        print(f"{IND}| {line}")
    if cut:
        print(f"{IND}| …已截断，共 {len(text)} 字符（--max-request-body 0 / --max-body 0 可看全）")


JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")


def scrub(text: str) -> str:
    """Hide bearer JWTs in printed bodies unless --show-token."""
    if SHOW_TOKEN or not text:
        return text
    return JWT_RE.sub(lambda m: f"eyJ…({len(m.group(0))} chars，--show-token 看全文)", text)


def fmt(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def pretty(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except Exception:
            return text
    return text


def hexdump(data: bytes, n: int = 64) -> str:
    head = data[:n]
    lines = []
    for i in range(0, len(head), 16):
        chunk = head[i:i + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:04x}  {hexpart:<47}  {asc}")
    if len(data) > n:
        lines.append(f"…（共 {len(data)} 字节，只显示前 {n} 字节）")
    return "\n".join(lines)


def mask_headers(hdrs: dict) -> dict:
    out = {}
    for k, v in hdrs.items():
        if k.lower() in ("authorization", "x-cos-security-token") \
                and isinstance(v, str) and len(v) > 24 and not SHOW_TOKEN:
            out[k] = f"{v[:18]}…({len(v)} chars，--show-token 看全文)"
        else:
            out[k] = v
    return out


def shq(s: str) -> str:
    """Shell-quote for display: prefer single quotes, fall back to double quotes
    when the value itself contains a single quote (avoids '"'"' noise in curl)."""
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s and not any(c in s for c in "$`\\"):
        return f'"{s}"'
    return shlex.quote(s)


def curl_of(ev: dict, idx: int) -> str:
    """Copy-pasteable curl reproducing exactly this request."""
    parts = [f"curl -X {ev['method']} {shq(ev['url'])}"]
    for k, v in ev["request_headers_full"].items():
        if k.lower() in ("content-length",):
            continue
        shown = v
        if k.lower() == "authorization" and not SHOW_TOKEN:
            shown = "Bearer <YOUR_ACCESS_TOKEN>"
        parts.append(f"  -H {shq(f'{k}: {shown}')}")
    text, kind, size = (ev["request_body_text"], ev["request_body_kind"],
                        ev["request_body_size"])
    if size:
        # JSON stays inline (readable, short); markup/binary go through @file so
        # the curl block does not explode into hundreds of lines.
        inline_ok = size <= (4000 if kind == "json" else 500)
        if kind in ("json", "text", "markup") and inline_ok:
            parts.append(f"  --data-raw {shq(text)}")
        else:
            name = f"payload_{idx}.{'html' if kind == 'markup' else 'json' if kind == 'json' else 'bin'}"
            path = f"<payloads>/{name}" if not PAYLOAD_DIR else str(PAYLOAD_DIR / name)
            parts.append(f"  --data-binary @{shq(path)}"
                         + ("" if PAYLOAD_DIR else "   # 加 --save-payloads DIR 落地该文件"))
    return " \\\n".join(parts)


def save_payload(ev: dict, idx: int) -> None:
    if not PAYLOAD_DIR or not ev["request_body_size"]:
        return
    kind = ev["request_body_kind"]
    ext = {"json": "json", "markup": "html", "text": "txt"}.get(kind, "bin")
    path = PAYLOAD_DIR / f"payload_{idx}.{ext}"
    data = ev["request_body_text"]
    if data is not None:
        path.write_text(data, encoding="utf-8")
    print(f"{IND}↳ 请求体已保存: {path}")


def on_trace(ev: dict) -> None:
    """Print ONE request/response pair with full request detail."""
    global SEQ
    SEQ += 1
    idx = SEQ
    ok = ev["status"] in (200, 201, 204)
    u = urlsplit(ev["url"])
    print()
    hr("·")
    print(f"  #{idx}  →  {ev['method']} {u.path}   [{ev['elapsed_ms']} ms]")

    # ---- request line, split into its parts --------------------------
    lin("URL", ev["url"])
    lin("scheme/host", f"{u.scheme}://{u.netloc}")
    lin("path", u.path or "/")
    q = parse_qsl(u.query, keep_blank_values=True)
    if q:
        print(f"{IND}{'query':<12}:")
        for k, v in q:
            print(f"{IND}  · {k} = {v}")
    else:
        lin("query", "（无）")

    # ---- headers -----------------------------------------------------
    hdrs = mask_headers(ev["request_headers_full"])
    print(f"{IND}{'headers':<12}: ({len(hdrs)} 项)")
    for k, v in hdrs.items():
        print(f"{IND}  · {k}: {v}")

    # ---- request body ------------------------------------------------
    size, kind = ev["request_body_size"], ev["request_body_kind"]
    if not size:
        lin("body", "（无请求体）")
    else:
        text = pretty(scrub(ev["request_body_text"]))
        lin("body", f"{kind}, {size} bytes" if kind else f"{size} bytes")
        if kind == "binary":
            raw = ev.get("request_body_bytes") or b""
            for line in hexdump(raw).splitlines():
                print(f"{IND}| {line}")
        else:
            block("body 原文", text, MAX_REQ_BODY)
        save_payload(ev, idx)

    # ---- response ----------------------------------------------------
    print()
    mark = "✔" if ok else "✘"
    print(f"  #{idx}  ←  {mark} HTTP {ev['status']}   [{ev['elapsed_ms']} ms]")
    rh = ev["response_headers"] or {}
    if rh:
        print(f"{IND}{'resp headers':<12}: ({len(rh)} 项)")
        for k, v in rh.items():
            print(f"{IND}  · {k}: {v}")
    rtext = ev["response_body"].decode("utf-8", "replace")
    rpretty = pretty(scrub(rtext))
    if rpretty and len(rpretty.strip()) > 1:
        block("resp body", rpretty, MAX_BODY)
    else:
        lin("resp body", f"（空，{len(ev['response_body'])} bytes）")

    # ---- curl --------------------------------------------------------
    if not NO_CURL:
        print(f"{IND}curl 复现:")
        for line in curl_of(ev, idx).splitlines():
            print(f"{IND}  {line}")


def record(no: str, title: str, verdict: str, detail: str = "") -> None:
    results.append({"step": f"S{no}", "hop": title, "verdict": verdict,
                    "detail": detail})
    icon = {OK: "✔", BAD: "✘", SKIP: "⏭", WARN: "⚠"}[verdict]
    print(f"\n  {icon} S{no} {title} → {verdict}" + (f"  |  {detail}" if detail else ""))


# --------------------------------------------------------------------------
# hops
# --------------------------------------------------------------------------
def main() -> int:
    global SHOW_TOKEN, MAX_REQ_BODY, MAX_BODY, NO_CURL, PAYLOAD_DIR
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--file", default=str(DEFAULT_FILE), help="source HTML")
    ap.add_argument("--name", default=None, help="entry name (default: <title>)")
    ap.add_argument("--state", default=None, help="skip S4-S6, bind this state")
    ap.add_argument("--put-url", default=None,
                    help="storage PUT template, {bucket}/{region}/{key} placeholders")
    ap.add_argument("--token", default=None, help="override bearer access_token")
    ap.add_argument("--skip-public-probe", action="store_true",
                    help="do not probe the public upload-params endpoint (S4)")
    ap.add_argument("--cleanup", action="store_true",
                    help="delete the entry created at S7 (evidence is printed first)")
    ap.add_argument("--continue-on-fail", action="store_true",
                    help="even when the storage PUT (S6) fails, keep going and call "
                         "the create-entry endpoint (S7) to show its request/reply")
    ap.add_argument("--show-token", action="store_true",
                    help="print Authorization in full (default: masked)")
    ap.add_argument("--max-request-body", type=int, default=0,
                    help="request body char limit; 0 = unlimited (default)")
    ap.add_argument("--max-body", type=int, default=4000,
                    help="response body char limit; 0 = unlimited")
    ap.add_argument("--no-curl", action="store_true", help="do not print curl")
    ap.add_argument("--save-payloads", default=None,
                    help="dump request bodies to this directory")
    args = ap.parse_args()

    SHOW_TOKEN = args.show_token
    MAX_REQ_BODY = max(0, args.max_request_body)
    MAX_BODY = max(0, args.max_body)
    NO_CURL = args.no_curl
    if args.save_payloads:
        PAYLOAD_DIR = Path(args.save_payloads).expanduser()
        PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)

    conf = load_config(str(args.config))
    if args.token:
        conf["access_token"], conf["auth_mode"] = args.token, "bearer"
    if args.put_url:
        conf["cos_put_url_template"] = args.put_url

    api = OpenApi(conf)
    api.trace = on_trace

    src = Path(args.file).expanduser()
    if not src.is_file():
        print(json.dumps({"fatal": f"source not found: {src}"}, ensure_ascii=False))
        return 1

    space_id = conf.get("space_id")
    parent_id = conf.get("parent_entry_id")
    html_mode = bool(conf.get("html_entry_path"))

    print("乐享 OpenAPI 上传链路诊断（逐接口打印请求/回包全文）")
    print(f"  config  : {args.config}")
    print(f"  api_base: {api.api_base}   auth_mode: {api.auth_mode}")
    print(f"  staff   : {api.staff_id}    space: {space_id or '(html_entry_path 模式可省)'}")
    print(f"  parent  : {parent_id or '(未配置)'}")
    print(f"  source  : {src}   ({src.stat().st_size} bytes)")
    print(f"  create  : {'/kb/entries/html（私有化）' if html_mode else '/kb/entries'}")
    print(f"  display : token={'full' if SHOW_TOKEN else 'masked'} "
          f"req_body_limit={MAX_REQ_BODY or '∞'} resp_body_limit={MAX_BODY or '∞'}")

    # ---- S1 token -----------------------------------------------------
    step(1, "交换 access_token（app_key 模式）" if api.auth_mode == "app_key"
         else "校验配置里的 Bearer token（bearer 模式）")
    ok, resp = api.get_token()
    if not ok:
        record(1, "token", BAD, json.dumps(resp, ensure_ascii=False)[:200])
        return summary()
    record(1, "token", OK, f"{api.auth_mode}, token len={len(api.token or '')}")

    # ---- S2 space -----------------------------------------------------
    step(2, "读取空间详情（验证 space_id 与权限）")
    if not space_id:
        record(2, "空间详情", SKIP, "space_id 未配置")
    else:
        st, sd = api.space_detail(space_id)
        if st == 200 and isinstance(sd, dict):
            name = sd.get("data", {}).get("attributes", {}).get("name")
            root = (sd.get("data", {}).get("relationships", {})
                    .get("root_entry", {}).get("data", {}).get("id"))
            record(2, "空间详情", OK, f"{name} root={root}")
        else:
            record(2, "空间详情", BAD, f"http {st}")
            return summary()

    # ---- S3 parent ----------------------------------------------------
    step(3, "读取目标目录（parent_entry_id）")
    if not parent_id:
        record(3, "目标目录", SKIP, "parent_entry_id 未配置（将建在空间根目录）")
    else:
        st, det = api.entry_detail(parent_id)
        if st == 200 and isinstance(det, dict):
            attr = det.get("data", {}).get("attributes", {})
            record(3, "目标目录", OK, f"{attr.get('name')} ({attr.get('entry_type')})")
        else:
            record(3, "目标目录", BAD, f"http {st}")
            return summary()

    # ---- payload ------------------------------------------------------
    import html_import as hi                      # same dir; reuses real packaging
    resources, missing = hi.scan_resources(src)
    name = args.name or hi.html_title(src) or src.stem
    raw_payload, inlined, err = hi.build_raw(src, resources, missing)
    zip_payload = hi.build_bundle(src, resources, missing) if err else None
    payload = raw_payload if raw_payload is not None else zip_payload
    payload_name = f"{name}.html" if raw_payload is not None else f"{name}.zip"
    print(f"\n  打包: {payload_name}  {len(payload)} bytes  "
          f"resources={len(resources)} missing={sorted(missing)} inlined={inlined}")

    state = args.state
    obj = None

    if state:
        record("4-6", "签名 + 存储上传", SKIP, f"使用 --state {state}")
    else:
        # ---- S4 public signature probe --------------------------------
        if args.skip_public_probe:
            record(4, "标准 upload-params（公有云路径）", SKIP, "--skip-public-probe")
        else:
            step(4, "标准签名接口 /cgi-bin/v1/kb/files/upload-params（公有云路径，私有化作为对照）")
            st, sig = api._json(*api._request(
                "POST", f"{api.api_base}/cgi-bin/v1/kb/files/upload-params",
                {"name": payload_name, "media_type": "file"}, auth=True, staff=True))
            if st == 200 and isinstance(sig, dict):
                record(4, "标准 upload-params", OK, "该网关支持标准接口")
            else:
                code = sig.get("code") if isinstance(sig, dict) else None
                record(4, "标准 upload-params", WARN,
                       f"http {st} code={code}（私有化已知缺陷，不影响 S5）"
                       if code == 31 else f"http {st} code={code}")

        # ---- S5 policy signature step --------------------------------
        step(5, f"策略签名步 POST {api.paths['upload_params_path']}  "
                f"body={json.dumps(api.upload_params_template, ensure_ascii=False)}")
        st, sig = api.upload_params(payload_name, "file")
        obj = api.extract_object(sig)
        state = (obj or {}).get("state")
        if st == 200 and state:
            record(5, "策略签名步", OK, f"state={state} key={(obj or {}).get('key')}")
            if not (obj or {}).get("upload_url") and not api.cos_put_template:
                print("    ⚠ 回包没有 upload_url（auth=%s）——S6 只能按候选地址逐个探测"
                      % json.dumps((obj or {}).get("auth")))
        else:
            record(5, "策略签名步", BAD, f"http {st}")
            return summary(status_only=True)

        # ---- S6 storage PUT -----------------------------------------
        step(6, "上传文件字节到存储（PUT）")
        st_put, used = try_put(
            api, obj, payload,
            force_content_type=OpenApi.html_content_type(payload_name))
        if st_put in (200, 201, 204):
            record(6, "存储 PUT", OK, f"{used}")
        else:
            record(6, "存储 PUT", BAD,
                   f"所有候选地址失败（最后: {used}）——这就是断点")
            if not args.continue_on_fail:
                return summary(status_only=True)
            print("\n    ↳ --continue-on-fail：S6 失败，但仍用 S5 拿到的 state 继续 S7，"
                  "以便看到建条目接口的请求/回包")
            print(f"      （预期回包：code=30010003「文件不存在」，因为对象没上传成功）")

    if not state:
        record(7, "建条目", SKIP, "没有 state")
        return summary(status_only=True)

    # ---- S7 create entry ---------------------------------------------
    step(7, f"建条目 POST {'html_entry_path' if html_mode else 'entries_path'}?state=…")
    eid, err = api.bind_state(state, space_id, parent_id, name,
                              html=payload_name.endswith(".html"))
    if not eid:
        record(7, "建条目", BAD, json.dumps(err, ensure_ascii=False)[:240])
        return summary(status_only=True)
    record(7, "建条目", OK, f"entry_id={eid}")

    # ---- S8 verify ---------------------------------------------------
    step(8, "校验条目详情 + 下载链接")
    st, det = api.entry_detail(eid)
    link = ((det or {}).get("data", {}) or {}).get("links", {}).get("download") \
        if isinstance(det, dict) else None
    if st == 200:
        record(8, "条目校验", OK, f"download={'有' if link else '无'}")
        if link:
            print(f"    download: {link}")
    else:
        record(8, "条目校验", BAD, f"http {st}")

    if args.cleanup:
        st, _ = api.delete_entry(eid)
        print(f"\n  cleanup DELETE /kb/entries/{eid} → HTTP {st}")
    else:
        print(f"\n  清理命令: DELETE {api.api_base}/cgi-bin/v1/kb/entries/{eid}"
              f"   （或加 --cleanup 让脚本自己删）")
    return summary()


def try_put(api: OpenApi, obj: dict, payload: bytes,
            force_content_type: str | None = None):
    """PUT the bytes: config template first, then inferred candidates.

    2026-10-26: headers now come from OpenApi.build_put_headers() — same as
    the real upload chain — so candidate probes carry the cos-param auth
    credentials (Authorization + x-cos-security-token) and the text/html
    override. Before this fix the probes PUT unsigned (403 AccessDenied on
    the test gateway) even though html_import.py succeeded."""
    opts = obj.get("options") or {}
    bucket, region, key = opts.get("Bucket", ""), opts.get("Region", ""), obj.get("key", "")
    cands: list[tuple[str, str]] = []
    if obj.get("upload_url"):
        cands.append(("回包自带 upload_url", obj["upload_url"]))
    if api.cos_put_template:
        cands.append(("config:cos_put_url_template",
                      api.cos_put_template.format(bucket=bucket, region=region, key=key)))
    cands += [
        ("网关 csp 路由", f"{api.api_base}/private-mono/api/open3rd/files/csp/{key}"),
        ("网关 raw 路由", f"{api.api_base}/private-mono/api/open3rd/files/{key}"),
    ]
    if bucket and region:
        cands.append(("COS 猜测域名",
                      f"https://{bucket}.cos.{region}.myqcloud.com/{key}"))
    hdrs = OpenApi.build_put_headers(obj, force_content_type)
    last = "-"
    for label, url in cands:
        if url.startswith("/"):
            url = api.api_base + url
        print(f"\n    尝试 [{label}]")
        st, body = api._request("PUT", url, raw_body=payload, headers=hdrs)
        if st in (200, 201, 204):
            return st, f"{label} → HTTP {st}"
        last = f"{label} → HTTP {st} {' '.join(body.decode('utf-8', 'replace').split())[:90]}"
    return -1, last


def summary(status_only: bool = False) -> int:
    print()
    hr("═")
    print(f"汇总（按调用顺序，共 {SEQ} 次 HTTP 请求）")
    hr("═")
    for r in results:
        icon = {OK: "✔", BAD: "✘", SKIP: "⏭", WARN: "⚠"}[r["verdict"]]
        print(f"  {icon} {r['step']:<5} {r['hop']:<26} {r['verdict']:<5} {r['detail'][:66]}")
    bad = [r for r in results if r["verdict"] == BAD]
    warn = [r for r in results if r["verdict"] == WARN]
    if bad:
        print()
        print(f"❌ 第一个失败环节：{bad[0]['step']} {bad[0]['hop']} — {bad[0]['detail']}")
        return 1
    if warn:
        print()
        for r in warn:
            print(f"⚠ 告警（非断点）：{r['step']} {r['hop']} — {r['detail']}")
    if any(r["verdict"] == SKIP for r in results):
        print()
        print("⏭ 有环节被跳过（见上表说明）")
    elif not warn:
        print()
        print("✅ 全链路通过")
    else:
        print()
        print("✅ 已执行环节全部通过（上面有告警项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
