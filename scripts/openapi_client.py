#!/usr/bin/env python3
"""Shared Lexiang OpenAPI client for the upload-html-to-lexiang-by-openapi skill.

All endpoints are derived from `api_base` in the config JSON — nothing is
hardcoded, so SaaS (https://lxapi.lexiangla.com) and privatized deployments
both work by pointing api_base at the right gateway.

Two auth modes (`auth_mode` in config):
  app_key  (default) POST {api_base}/cgi-bin/token with app_key/app_secret
           -> access_token (2h), exchanged automatically on every run.
  bearer   the private-deployment form: a ready Bearer token (usually copied
           from the browser/client, e.g. `ifbook-http-client`) is configured
           directly; NO token exchange is attempted. The token is a JWT —
           `get_token()` decodes `exp` and fails fast when it is expired.

Config file (JSON):
  {"api_base": "https://lxapi.lexiangla.com",   # REQUIRED, privatized: on-prem gateway
   "auth_mode": "app_key",                      # app_key (default) | bearer
   "app_key": "...", "app_secret": "...",       # app_key mode only
   "access_token": "...",                       # bearer mode only (env:VAR supported)
   "staff_id": "...",                           # x-staff-id, needs editor+ on target
   "headers": {"User-Agent": "ifbook-http-client"},   # optional extra headers
   "staff_header": "x-staff-id",                # optional header name override
   "html_entry_path": "/cgi-bin/v1/kb/entries/html",  # privatized html entry endpoint
   "space_id": "<optional default target space>"}

Chain endpoints (official docs https://lexiang.tencent.com/wiki/api/):
  token            POST {api_base}/cgi-bin/token
  upload-params    POST {api_base}/cgi-bin/v1/kb/files/upload-params
  create entry     POST {api_base}/cgi-bin/v1/kb/entries?state=&space_id=
  create html      POST {api_base}/cgi-bin/v1/kb/entries/html?state=   (privatized)
  re-upload        PUT  {api_base}/cgi-bin/v1/kb/entries/{id}/upload?state=
  delete           DELETE {api_base}/cgi-bin/v1/kb/entries/{id}

Every path above can be overridden per policy (see DEFAULT_PATHS), so a
gateway with different routing needs config changes only — no code changes.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 60
AUTH_MODES = ("app_key", "bearer")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _resolve_secret(value):
    """`env:VAR` -> value of $VAR (lets tokens live outside the config file)."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


def load_config(path: str) -> dict:
    from pathlib import Path
    conf = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    conf = {k: _resolve_secret(v) for k, v in conf.items()}

    mode = (conf.get("auth_mode") or "app_key").lower()
    if mode not in AUTH_MODES:
        raise ValueError(f"auth_mode must be one of {AUTH_MODES}, got {mode!r}")
    conf["auth_mode"] = mode

    required = ["api_base", "staff_id"]
    required += (["app_key", "app_secret"] if mode == "app_key" else ["access_token"])
    missing = [k for k in required if not conf.get(k)]
    if missing:
        raise ValueError(
            f"config missing required keys: {missing} (auth_mode={mode}; "
            f"api_base is always required — privatized deployments must point "
            f"it at their own gateway)")
    return conf


def jwt_exp(token: str) -> int | None:
    """Decode the `exp` claim of a JWT without verifying it. None if not a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


class OpenApi:
    """Thin client over the Lexiang OpenAPI upload chain."""

    DEFAULT_PATHS = {
        "token_path": "/cgi-bin/token",
        "upload_params_path": "/cgi-bin/v1/kb/files/upload-params",
        "entries_path": "/cgi-bin/v1/kb/entries",              # list + create
        "html_entry_path": "",                                 # privatized html create
        "entry_path": "/cgi-bin/v1/kb/entries/{entry_id}",
        "reupload_path": "/cgi-bin/v1/kb/entries/{entry_id}/upload",
        "space_path": "/cgi-bin/v1/kb/spaces/{space_id}",
    }

    def __init__(self, conf: dict):
        self.conf = conf
        self.api_base = conf["api_base"].rstrip("/")
        self.auth_mode = conf.get("auth_mode") or "app_key"
        self.app_key = conf.get("app_key")
        self.app_secret = conf.get("app_secret")
        self.staff_id = conf["staff_id"]
        self.staff_header = conf.get("staff_header") or "x-staff-id"
        self.extra_headers = dict(conf.get("headers") or {})
        # extra query params appended to every api_base request (private
        # deployments sometimes need e.g. company_from=<值>). VERIFIED
        # 2026-10-23: harmless on space/entries endpoints.
        self.extra_query = ""
        eq = conf.get("extra_query") or {}
        if isinstance(eq, dict) and eq:
            self.extra_query = "&".join(f"{k}={v}" for k, v in eq.items())
        # signature-step body: public API takes {name, media_type}; the private
        # legacy endpoint /cgi-bin/v1/docs/cos-param takes {filename, type}.
        # VERIFIED 2026-10-23: on the privatized gateway upload-params is broken
        # (HTTP 500) while cos-param works and DOES mint a usable state.
        self.upload_params_template = conf.get("upload_params_template") or {
            "name": "{name}", "media_type": "{media_type}"}
        # private deployments return {options:{Bucket,Region}, object:{key,state},
        # auth:null} with NO upload_url — set a template to reach their storage:
        #   "https://{bucket}.cos.{region}.myqcloud.com/{key}"
        #   "/private-mono/api/open3rd/files/{key}"   (relative to api_base)
        self.cos_put_template = conf.get("cos_put_url_template") or ""
        self.timeout = int(conf.get("timeout") or TIMEOUT)
        self.paths = {k: (conf[k] if conf.get(k) is not None else v)
                      for k, v in self.DEFAULT_PATHS.items()}
        self.token = conf.get("access_token") or None

    # ---- low-level helpers -------------------------------------------
    # Set to a callable(dict) to receive every request/response pair (used by
    # scripts/debug_private_chain.py; None = zero overhead, no behaviour change).
    trace = None

    @staticmethod
    def _redact(hdrs: dict) -> dict:
        out = {}
        for k, v in hdrs.items():
            if k.lower() in ("authorization", "x-cos-security-token") \
                    and isinstance(v, str) and len(v) > 24:
                out[k] = f"{v[:18]}…({len(v)} chars)"
            else:
                out[k] = v
        return out

    @staticmethod
    def _body_fields(data):
        """Raw request body for tracing: full text + kind + size (never clipped)."""
        if data is None:
            return {"request_body_text": None, "request_body_size": 0,
                    "request_body_kind": None}
        if not isinstance(data, (bytes, bytearray)):
            text = data if isinstance(data, str) else str(data)
            data = text.encode()
        kind, text = "binary", None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        if text is not None:
            stripped = text.lstrip()
            if stripped.startswith("<"):
                kind = "markup"
            elif stripped.startswith(("{", "[")):
                kind = "json"
            else:
                kind = "text"
        return {"request_body_text": text, "request_body_size": len(data),
                "request_body_kind": kind,
                "request_body_bytes": data if kind == "binary" else None}

    def _request(self, method: str, url: str, payload=None, *, auth=False,
                 staff=False, headers=None, raw_body=None):
        if self.extra_query and url.startswith(self.api_base):
            url += ("&" if "?" in url else "?") + self.extra_query
        hdrs = {"Content-Type": "application/json; charset=utf-8"}
        # extra headers (e.g. X-Zerus-Env) belong to the lexiang gateway
        # ONLY — VERIFIED 2026-10-26 on the test gateway: sending them to
        # the external COS PUT succeeds HTTP-wise but the object ends up
        # rejected by /kb/entries/html (400 "HTML 知识仅支持 .html/.htm 单文
        # 件或 .zip 包"), so scope them to api_base like extra_query.
        if self.extra_headers and url.startswith(self.api_base):
            hdrs.update(self.extra_headers)
        if auth:
            hdrs["Authorization"] = f"Bearer {self.token}"
        if staff:
            hdrs[self.staff_header] = self.staff_id
        if headers:
            hdrs.update(headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
        elif raw_body is not None:
            data = raw_body
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        started = time.time()
        resp_headers: dict = {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, body = resp.status, resp.read()
                resp_headers = dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
            resp_headers = dict((e.headers or {}).items())
        except Exception as e:  # network etc.
            status, body = -1, str(e).encode()
        if self.trace:
            self.trace({
                "method": method, "url": url,
                "request_headers": self._redact(hdrs),
                "request_headers_full": dict(hdrs),
                "status": status,
                "elapsed_ms": int((time.time() - started) * 1000),
                "response_headers": resp_headers,
                "response_body": body,
                **self._body_fields(data),
            })
        return status, body

    @staticmethod
    def _json(status: int, body: bytes):
        try:
            return status, json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return status, body.decode("utf-8", "replace")[:500]

    @staticmethod
    def extract_object(sig) -> dict | None:
        """Signature response -> the object dict, with top-level `options`
        (Bucket/Region) merged in.

        DOC shape:      {"options": {...}, "object": {...}, "auth": null}
        wrapped shape:  {"data": {"object": {...}}}
        The public API embeds upload_url in `object`, the privatized gateway
        puts Bucket/Region in a SIBLING `options` key — without this merge a
        cos_put_url_template would format empty bucket/region."""
        if not isinstance(sig, dict):
            return None
        obj = (sig.get("data") or {}).get("object") or sig.get("object")
        if isinstance(obj, dict) and not obj.get("options") and sig.get("options"):
            obj = {**obj, "options": sig["options"]}
        return obj

    def _url(self, path_key: str, **fmt) -> str:
        return f"{self.api_base}{self.paths[path_key].format(**fmt)}"

    # ---- auth ---------------------------------------------------------
    def get_token(self):
        """app_key mode: exchange credentials. bearer mode: reuse configured token.

        bearer mode never calls /cgi-bin/token (privatized gateways may not
        expose it, and the token is issued elsewhere). An expired JWT is
        reported before any upload starts instead of surfacing as a 401."""
        if self.auth_mode == "bearer":
            if not self.token:
                return False, {"error": "auth_mode=bearer requires access_token "
                                        "in config (or env:VAR, or --token)"}
            exp = jwt_exp(self.token)
            if exp and exp <= time.time():
                return False, {"error": "access_token expired — paste a fresh "
                                        "Bearer token into the policy config "
                                        "(or pass --token)",
                               "expired_at": time.strftime(
                                   "%Y-%m-%d %H:%M:%S", time.localtime(exp))}
            if exp:
                left = int(exp - time.time())
                log(f"bearer token ok (expires in {left // 60}m{left % 60}s)")
            return True, {"auth_mode": "bearer", "exp": exp}

        st, body = self._request("POST", self._url("token_path"),
                                  {"grant_type": "client_credentials",
                                   "app_key": self.app_key,
                                   "app_secret": self.app_secret})
        st, js = self._json(st, body)
        if isinstance(js, dict) and js.get("access_token"):
            self.token = js["access_token"]
            return True, js
        return False, js

    # ---- kb read ------------------------------------------------------
    def space_detail(self, space_id: str):
        st, body = self._request("GET", self._url("space_path", space_id=space_id),
                                  auth=True, staff=True)
        return self._json(st, body)

    def space_root_entry(self, space_id: str):
        if not space_id:
            return None
        st, sd = self.space_detail(space_id)
        if st != 200 or not isinstance(sd, dict):
            return None
        return (sd.get("data", {}).get("relationships", {})
                .get("root_entry", {}).get("data", {}).get("id"))

    def entry_detail(self, entry_id: str):
        st, body = self._request("GET", self._url("entry_path", entry_id=entry_id),
                                  auth=True, staff=True)
        return self._json(st, body)

    def entry_children(self, space_id: str, parent_id: str):
        """List children of an entry. PROBED 2026-10-14: param is `parent_id`
        (NOT parent_entry_id — same convention as the MCP toolchain); parent
        filter without space_id returns 400. Useful to recover entry_ids."""
        st, body = self._request(
            "GET",
            f"{self._url('entries_path')}"
            f"?space_id={space_id}&parent_id={parent_id}&per_page=100",
            auth=True, staff=True)
        return self._json(st, body)

    # ---- write chain ---------------------------------------------------
    def create_folder(self, space_id: str, parent_id: str, name: str):
        payload = {"data": {"attributes": {"name": name, "entry_type": "folder"},
                            "relationships": {"parent_entry": {"data": {
                                "type": "kb_entry", "id": parent_id}}}}}
        st, body = self._request(
            "POST", f"{self._url('entries_path')}?space_id={space_id}",
            payload, auth=True, staff=True)
        return self._json(st, body)

    def upload_params(self, name: str, media_type: str):
        body = {k: (v.format(name=name, media_type=media_type)
                    if isinstance(v, str) else v)
                for k, v in self.upload_params_template.items()}
        st, body_resp = self._request(
            "POST", self._url("upload_params_path"), body, auth=True, staff=True)
        return self._json(st, body_resp)

    @staticmethod
    def build_put_headers(obj: dict, force_content_type: str | None = None) -> dict:
        """COS PUT headers from a signature response `object`.

        Merges (in order): auth.XCosSecurityToken → object.headers →
        optional Content-Type override → auth.Authorization (last so the COS
        signature wins over any gateway bearer). Shared by cos_put() and the
        probe scripts (debug_private_chain / test_private_chain) so their
        candidate-PUT probes authenticate exactly like the real chain."""
        hdrs = {}
        auth = obj.get("auth") or {}
        if "XCosSecurityToken" in auth:
            hdrs["x-cos-security-token"] = auth["XCosSecurityToken"]
        for k, v in (obj.get("headers") or {}).items():
            hdrs[k] = v
        if force_content_type:
            hdrs["Content-Type"] = force_content_type
        if "Authorization" in auth:
            hdrs["Authorization"] = auth["Authorization"]
        return hdrs

    @staticmethod
    def html_content_type(filename: str) -> str | None:
        """text/html for .html/.htm payloads, else None (see cos_put)."""
        return "text/html" if filename.lower().endswith((".html", ".htm")) \
            else None

    def cos_put(self, obj: dict, data: bytes, force_content_type: str | None = None):
        """Doc shape: object.upload_url + object.headers + auth.XCosSecurityToken.
        Legacy shape: options.Bucket/Region + object.key + auth.Authorization.
        Private shape: options.Bucket/Region + object.key, auth:null, and NO
        upload_url -> build it from `cos_put_url_template` when configured.

        force_content_type overrides object.headers["Content-Type"] — VERIFIED
        2026-10-26 on the test gateway: cos-param signs objects as
        application/octet-stream, which the frontend then refuses to RENDER
        (blank page); uploading the same bytes as text/html previews fine
        (matches the SaaS upload-params headers, which are text/html). The
        COS signature has an empty q-header-list, so swapping the header
        does not break the signature."""
        url = obj.get("upload_url")
        hdrs = self.build_put_headers(obj, force_content_type)
        if not url and self.cos_put_template:
            opts = obj.get("options") or {}
            url = self.cos_put_template.format(
                bucket=opts.get("Bucket", ""), region=opts.get("Region", ""),
                key=obj.get("key", ""))
            if url.startswith("/"):
                url = self.api_base + url
        if not url:
            return -1, {"error": "no upload_url in response — private deployments "
                                 "return bucket/region/key only; set "
                                 "cos_put_url_template in the policy config"}
        return self._request("PUT", url, raw_body=data, headers=hdrs)

    def create_file_entry(self, space_id: str | None, parent_id: str, state: str,
                          name: str, entry_type: str = "file", html: bool = False):
        """Bind an uploaded object (`state`) to a new entry.

        `html=True` + a configured `html_entry_path` routes to the privatized
        html handler — VERIFIED sample (2026-10-23): it takes only `state` in
        the query, the space is inferred from parent_entry, so space_id is
        dropped there (sending it is useless, not harmful)."""
        html_route = html and self.paths["html_entry_path"]
        if html_route:
            # VERIFIED 2026-10-26 on the test gateway (test env): the html
            # route REQUIRES a suffixed name — 400 "HTML 知识仅支持 .html/.htm
            # 单文件或 .zip 包" when the entry name carries no extension (the
            # default <title>-based name). The server strips the suffix for
            # display, so appending it never leaks into the visible name.
            # NOTE: must happen BEFORE building the payload dict.
            if not name.lower().endswith((".html", ".htm", ".zip")):
                name = f"{name}.html"
        payload = {"data": {"attributes": {"name": name, "entry_type": entry_type},
                            "relationships": {"parent_entry": {"data": {
                                "type": "kb_entry", "id": parent_id}}}}}
        if html_route:
            url = f"{self._url('html_entry_path')}?state={state}"
        else:
            url = f"{self._url('entries_path')}?state={state}"
            if space_id:
                url += f"&space_id={space_id}"
        st, body = self._request("POST", url, payload, auth=True, staff=True)
        return self._json(st, body)

    def reupload_entry(self, entry_id: str, state: str):
        """In-place update. VERIFIED 2026-10-14: works for raw html entries;
        ALWAYS 400 for zip entries (use delete+recreate for those)."""
        st, body = self._request(
            "PUT",
            f"{self._url('reupload_path', entry_id=entry_id)}?state={state}",
            None, auth=True, staff=True)
        return self._json(st, body)

    def delete_entry(self, entry_id: str):
        st, body = self._request(
            "DELETE", self._url("entry_path", entry_id=entry_id),
            auth=True, staff=True)
        return self._json(st, body)

    # ---- high-level: full upload in one call ----------------------------
    def upload_bytes(self, filename: str, data: bytes, space_id: str | None,
                     parent_id: str, name: str, entry_type: str = "file",
                     html: bool = False):
        """Three-step chain. Returns (entry_id, error). entry_id set on success."""
        st, sig = self.upload_params(filename, "file")
        if st != 200:
            err = {"step": "upload-params", "http": st, "resp": sig}
            if isinstance(sig, dict) and sig.get("code") == 31:
                # VERIFIED 2026-10-23 on the privatized gateway: upload-params
                # answers 500/「系统内部错误」 for every media_type — the
                # deployment cannot sign uploads, so no `state` can be minted.
                err["hint"] = ("this gateway cannot mint upload params (server-side "
                               "500). Obtain a `state` out of band and pass "
                               "--state <STATE>, or ask the deployment owner to "
                               "fix /cgi-bin/v1/kb/files/upload-params")
            return None, err
        obj = self.extract_object(sig)
        if not obj or not obj.get("state"):
            return None, {"step": "upload-params", "http": st, "resp": sig}
        # html bytes must land on COS as text/html (see cos_put docstring);
        # zip bundles stay binary/octet-stream on purpose.
        put_st, put_body = self.cos_put(
            obj, data, force_content_type=self.html_content_type(filename))
        if put_st not in (200, 204):
            return None, {"step": "cos-put", "http": put_st,
                          "resp": put_body.decode("utf-8", "replace")[:300]
                          if isinstance(put_body, bytes) else str(put_body)[:300]}
        st, created = self.create_file_entry(space_id, parent_id, obj["state"],
                                             name, entry_type, html=html)
        if st in (200, 201) and isinstance(created, dict) and created.get("data", {}).get("id"):
            return created["data"]["id"], None
        return None, {"step": "create-entry", "http": st, "resp": created}

    def bind_state(self, state: str, space_id: str | None, parent_id: str,
                   name: str, entry_type: str = "file", html: bool = False):
        """Bind an ALREADY uploaded object (state known out of band) to a new
        entry — skips upload-params + COS PUT. Used by `--state`, which makes
        the privatized html endpoint reproducible even when that deployment's
        upload-params is broken (VERIFIED 2026-10-23: HTTP 500 there)."""
        st, created = self.create_file_entry(space_id, parent_id, state,
                                             name, entry_type, html=html)
        if st in (200, 201) and isinstance(created, dict) and created.get("data", {}).get("id"):
            return created["data"]["id"], None
        return None, {"step": "create-entry", "http": st, "resp": created}
