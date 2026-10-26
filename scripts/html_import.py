#!/usr/bin/env python3
"""Upload HTML files (with local relative-path resources) to Lexiang via OpenAPI.

Production import script of the upload-html-to-lexiang-by-openapi skill
(SaaS AND privatized deployments — api_base comes from the config file).

For each HTML file:
  1. Scan relative-path resources (img/link/script/a...). http(s):// and
     data: URIs are kept as-is; missing local files degrade to placeholder
     notices (never silently dropped).
  2. Upload as a RAW .html file whenever possible — default mode is auto
     (VERIFIED 2026-10-14: the Lexiang frontend renders raw .html entries;
     zip bundles only show a non-previewable file card). auto uploads
     self-contained HTML as-is and inlines images/css/js as data: URIs when
     needed; zip bundle stays as the fallback for non-inlinable resources
     (attachments: pdf/csv/zip...). --raw is strict (self-contained as-is,
     no inlining); --bundle forces the legacy zip form.
  3. Drive the OpenAPI three-step chain (see openapi_client.py):
     upload-params -> COS PUT -> POST /kb/entries?state=
  4. Persist a content-hash manifest for idempotent re-runs (skips unchanged).

Update semantics (VERIFIED 2026-10-14, see references/probe-results-2026-10-14.md):
  - raw html entry: PUT /kb/entries/{id}/upload — in-place, entry_id unchanged.
  - zip bundle entry: re-upload ALWAYS 400 ("不允许重复上传不同文件类型文件")
    -> delete + recreate under the same parent; entry_id CHANGES, manifest
    is updated automatically and the new id is reported.

stdout: single JSON summary. Diagnostics: stderr.

Usage:
  html_import.py <source.html | dir> --config config.json \
      [--space-id ID] [--parent-entry-id ID] [--entry-id ID] \
      [--name NAME] [--raw | --bundle] [--state STATE] [--token TOKEN] \
      [--dry-run] [--force] [--no-incremental] [--manifest-path PATH]

Privatized gateways (config-driven, no code changes): auth_mode=bearer with a
ready access_token (JWT exp is checked up front), upload_params_path/
upload_params_template for their signature endpoint, cos_put_url_template when
they return bucket/region/key without an upload_url, and html_entry_path for
their html-specific create route (target located by parent_entry only).
`--state` binds an already-uploaded object and skips signature + storage PUT.

Target rules: --entry-id (update existing entry, parent auto-resolved) OR
--parent-entry-id (create under it; space_id needed via --space-id or config)
OR --space-id only (create under space root). Policies with html_entry_path
may omit space_id (their html route infers it from parent_entry).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from openapi_client import OpenApi, load_config

SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = SKILL_ROOT / "configs"          # multiple policy files live here
CONFIG_PATH = CONFIG_DIR / "default.json"    # default policy (zero-arg runs)
MANIFEST_DIR = SKILL_ROOT / "manifests"      # per-policy manifest: manifests/<policy>.json
RE_HTTP = re.compile(r"^(https?://|//|data:|#|mailto:|tel:|javascript:)", re.I)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# resource scanning
# --------------------------------------------------------------------------
class ResourceScanner(HTMLParser):
    """Collect local relative-path resource references from an HTML file."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs: set[str] = set()
        self.srcset_tags: set[str] = set()

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for attr in ("src", "href", "poster"):
            if d.get(attr):
                self._add(d[attr])
        for attr in ("srcset", "imagesrcset"):  # "a 1x, b 2x" / "a 100w"
            if d.get(attr):
                for part in d[attr].split(","):
                    u = part.strip().split(" ")[0]
                    if u:
                        self._add(u)

    def _add(self, url: str):
        url = url.strip()
        if not url or RE_HTTP.match(url):
            return
        if url.startswith("/"):  # absolute local path — treated as missing
            self.srcset_tags.add(url)  # keep out of bundle refs
            return
        self.refs.add(url)


def scan_resources(html_path: Path) -> tuple[set[str], set[str]]:
    """Returns (relative refs found, refs whose local file is missing)."""
    s = ResourceScanner()
    s.feed(html_path.read_text(encoding="utf-8", errors="replace"))
    found, missing = set(), set()
    base = html_path.parent
    for ref in s.refs:
        if (base / ref).resolve().is_file():
            found.add(ref)
        else:
            missing.add(ref)
    return found, missing


def inject_missing_notices(html: str, missing: set[str]) -> str:
    """Insert visible placeholder notices for missing resources (never silent)."""
    if not missing:
        return html
    block = "\n".join(
        f'<div class="lx-missing-resource">[资源缺失：{m} — 源文件不存在，已跳过]</div>'
        for m in sorted(missing))
    marker = re.search(r"<body[^>]*>", html, re.I)
    inject = f"<div class='lx-missing-notices'>{block}</div>"
    if marker:
        i = marker.end()
        return html[:i] + inject + html[i:]
    return inject + html


# --------------------------------------------------------------------------
# raw upload support (VERIFIED 2026-10-14: frontend renders raw .html entries;
# zip bundles are NOT previewable — OpenAPI has no extension=html parameter)
# --------------------------------------------------------------------------
INLINEABLE_SUFFIXES = {".jpg", ".jpeg", ".gif", ".png", ".svg", ".webp",
                        ".bmp", ".ico", ".css", ".js",
                        # media — VERIFIED 2026-10-14: 1.13MB mp4 as data URI
                        # previews AND plays in the frontend; ceiling untested
                        ".mp4", ".webm", ".mov", ".m4v",
                        ".mp3", ".wav", ".m4a", ".aac", ".flac"}
INLINE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp",
               ".bmp": "image/bmp", ".ico": "image/x-icon",
               ".css": "text/css", ".js": "text/javascript",
               ".mp4": "video/mp4", ".webm": "video/webm",
               ".mov": "video/quicktime", ".m4v": "video/mp4",
               ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
               ".aac": "audio/aac", ".flac": "audio/flac"}
# media above this size falls back to zip (which does NOT preview): base64
# inflates by 33% and data-URI media cannot stream — big videos must go in
# as separate video entries (media_type=video), not embedded HTML
INLINE_MAX_BYTES = 10 * 1024 * 1024


def inlinable_file(p: Path) -> bool:
    """Inline-able as a data URI: known suffix AND size <= INLINE_MAX_BYTES."""
    try:
        return p.suffix.lower() in INLINE_MIME and p.stat().st_size <= INLINE_MAX_BYTES
    except OSError:
        return False


def inline_data_uris(html: str, base: Path, resources: set[str]) -> str | None:
    """Rewrite relative src/href/poster refs as data: URIs.

    Returns the rewritten HTML, or None when a ref cannot be rewritten
    (non-inlinable type / unreadable file / leftovers like srcset lists) —
    the caller must then fall back to the zip bundle."""
    failed: list[str] = []

    def repl(m):
        attr, path = m.group(1), m.group(2)
        if RE_HTTP.match(path):
            return m.group(0)
        p = (base / path).resolve()
        if not inlinable_file(p):
            failed.append(path)
            return m.group(0)
        mime = INLINE_MIME[p.suffix.lower()]
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f'{attr}="data:{mime};base64,{b64}"'

    out = re.sub(r'(src|href|poster)="([^"]+)"', repl, html)
    if failed:
        log(f"inline_data_uris: cannot rewrite: {sorted(failed)}")
        return None

    # srcset / imagesrcset: if the list contains local refs, strip the whole
    # attribute — data URIs contain commas, which the comma-separated srcset
    # grammar cannot express; the tag's src (already inlined above) then takes
    # over. All-external lists are kept untouched.
    stripped: list[str] = []

    def srcset_repl(m):
        value = m.group(2)
        local = [p.strip().split(" ")[0] for p in value.split(",")
                 if p.strip() and not RE_HTTP.match(p.strip().split(" ")[0])]
        if not local:
            return m.group(0)
        stripped.extend(local)
        return ""

    out = re.sub(r'\s+(srcset|imagesrcset)="([^"]*)"', srcset_repl, out)
    if stripped:
        log(f"inline_data_uris: srcset with local refs stripped, "
            f"src data URI takes over: {sorted(stripped)}")
    s = ResourceScanner()  # residual local refs (unquoted attrs...)?
    s.feed(out)
    if s.refs:
        log(f"inline_data_uris: residual local refs: {sorted(s.refs)}")
        return None
    return out


def build_raw(src: Path, resources: set[str],
              missing: set[str]) -> tuple[bytes | None, bool, str | None]:
    """Self-contained .html payload for raw upload.

    Inlines resources as data URIs when present (auto mode). Returns
    (payload, inlined, None); on failure (None, False, reason)."""
    html = inject_missing_notices(
        src.read_text(encoding="utf-8", errors="replace"), missing)
    inlined = False
    if resources:
        out = inline_data_uris(html, src.parent, resources)
        if out is None:
            return (None, False,
                    "cannot rewrite all relative refs as data URIs "
                    "(srcset lists / unquoted attrs / non-inlinable types)")
        html, inlined = out, True
    return html.encode("utf-8"), inlined, None


# --------------------------------------------------------------------------
# packaging
# --------------------------------------------------------------------------
def content_hash(html_path: Path, resources: set[str], mode: str) -> str:
    """sha256(mode + HTML + resource bytes).

    Mode is part of the hash (2026-10-14 fix): switching raw/zip for the
    same source now invalidates the manifest entry — previously a zip
    upload blocked a later raw re-upload of the same file (skipped forever)."""
    h = hashlib.sha256()
    h.update(mode.encode())
    h.update(b"\x00")
    h.update(html_path.read_bytes())
    for ref in sorted(resources):
        h.update(ref.encode())
        h.update((html_path.parent / ref).read_bytes())
    return h.hexdigest()


def build_bundle(html_path: Path, resources: set[str],
                 missing: set[str]) -> bytes:
    """ZIP bundle: index.html (renamed, notices injected) + resources in place.
    Built in memory (BytesIO) — no temp files to leak."""
    html = inject_missing_notices(
        html_path.read_text(encoding="utf-8", errors="replace"), missing)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.html", html)
        for ref in sorted(resources):
            z.writestr(ref, (html_path.parent / ref).read_bytes())
    return buf.getvalue()


def html_title(html_path: Path) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>",
                  html_path.read_text(encoding="utf-8", errors="replace"),
                  re.I | re.S)
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------
# manifest (idempotency)
# --------------------------------------------------------------------------
def load_manifest(path: Path, enabled: bool) -> dict:
    if not enabled or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)  # atomic


# --------------------------------------------------------------------------
# import one file
# --------------------------------------------------------------------------
def import_one(api: OpenApi | None, src: Path, args, space_id: str | None,
               parent_id: str | None, manifest: dict) -> dict:
    r = {"source": str(src)}
    resources, missing = scan_resources(src)
    r["resources"] = len(resources)
    r["degraded_resources"] = sorted(missing)
    name = args.name or html_title(src) or src.stem
    # ---- mode resolution: --raw (strict) | --bundle (forced zip) | auto ----
    # auto (default, 2026-10-14): raw .html whenever possible — the frontend
    # renders raw .html entries; zip bundles are NOT previewable. Self-contained
    # HTML goes as-is; images/css/js are inlined as data URIs when needed; zip
    # bundle is the fallback for non-inlinable resources (attachments...).
    if args.raw and args.bundle:
        return {**r, "status": "error", "message": "--raw and --bundle are exclusive"}
    if args.raw:
        mode = "raw"
    elif args.bundle:
        mode = "zip"
    elif not resources:
        mode = "raw"
    else:
        blocked = [x for x in resources if not inlinable_file(src.parent / x)]
        if not blocked:
            mode = "raw"
        else:
            mode = "zip"
            oversize = [x for x in blocked
                        if (src.parent / x).suffix.lower() in INLINE_MIME]
            r["mode_reason"] = (
                f"oversize media >{INLINE_MAX_BYTES // 1048576}MB -> zip "
                f"(NOT previewable; use separate video entry instead): {sorted(oversize)}"
                if oversize else
                f"non-inlinable resource types -> zip (NOT previewable): {sorted(blocked)}")

    if args.raw and (resources or missing):
        return {**r, "status": "error",
                "message": "--raw requires self-contained HTML (no relative refs)"}

    # ---- payload (mode may downgrade raw->zip when inlining fails) ----
    inlined = False
    if mode == "raw":
        payload, inlined, err = build_raw(src, resources, missing)
        if payload is None:
            if args.raw:
                return {**r, "status": "error", "message": f"--raw failed: {err}"}
            mode = "zip"  # auto downgrade: refs not rewritable as data URIs
            inlined = False
    if mode == "zip":
        payload = build_bundle(src, resources, missing)
        payload_name = f"{name}.zip"
    else:
        payload_name = f"{name}.html"
    r["zip_mode"] = (mode == "zip")
    r["mode"] = mode
    r["inlined"] = inlined
    # html payloads route to the privatized html entry endpoint when configured
    is_html = payload_name.endswith(".html")

    # ---- hash with FINAL mode (2026-10-14 fix: mode is part of the hash,
    # otherwise a zip upload blocks a later raw re-upload of the same file) --
    chash = content_hash(src, resources, mode)
    r["content_hash"] = chash
    key = str(src.resolve())
    if not args.force and key in manifest and manifest[key].get("content_hash") == chash:
        return {**r, "status": "skipped",
                "entry_id": manifest[key].get("entry_id"),
                "message": "unchanged (manifest hash match)"}

    if args.dry_run or api is None:
        return {**r, "status": "dry_run", "payload_bytes": len(payload),
                "payload_name": payload_name, "name": name}

    # ---- update mode (--entry-id): keep the same parent, replace content ----
    if args.entry_id:
        eid = args.entry_id
        st, det = api.entry_detail(eid)
        if st != 200:
            return {**r, "status": "error", "message": f"entry {eid} not reachable "
                    f"(http {st}): {json.dumps(det, ensure_ascii=False)[:200]}"}
        space_id = (det.get("data", {}).get("relationships", {})
                    .get("space", {}).get("data", {}).get("id"))
        if mode == "raw":
            if args.state:                       # state supplied out of band
                obj = {"state": args.state}
            else:
                st, sig = api.upload_params(payload_name, "file")
                if st != 200:
                    return {**r, "status": "error", "message": f"upload-params http {st}: "
                            f"{json.dumps(sig, ensure_ascii=False)[:200]}"}
                obj = api.extract_object(sig)
                # html bytes land as text/html (rendering requirement, see
                # OpenApi.cos_put docstring — same fix as the create chain)
                forced_ct = "text/html" if is_html else None
                put_st, _ = api.cos_put(obj, payload, force_content_type=forced_ct)
                if put_st not in (200, 204):
                    return {**r, "status": "error", "message": f"cos put http {put_st}"}
            st, resp = api.reupload_entry(eid, obj["state"])
            if st == 200:
                manifest[key] = {"content_hash": chash, "entry_id": eid,
                                 "entry_name": name, "mode": "raw-inplace"}
                return {**r, "status": "succeed", "entry_id": eid, "mode": "raw-inplace"}
            return {**r, "status": "error",
                    "message": f"re-upload http {st}: "
                    f"{json.dumps(resp, ensure_ascii=False)[:200]}"}
        # zip bundle: in-place re-upload is a verified dead end -> delete+recreate
        old_parent = (det.get("data", {}).get("relationships", {})
                      .get("parent", {}).get("data", {}).get("id"))
        st, _ = api.delete_entry(eid)
        if st not in (200, 204):
            return {**r, "status": "error",
                    "message": f"cannot delete old entry {eid} (http {st}) — "
                    f"zip bundle update requires delete+recreate"}
        new_id, err = api.upload_bytes(payload_name, payload, space_id,
                                       old_parent, name)
        if err:
            return {**r, "status": "error", "old_entry_id": eid,
                    "message": f"old deleted, recreate failed: {json.dumps(err, ensure_ascii=False)[:200]}"}
        manifest[key] = {"content_hash": chash, "entry_id": new_id,
                         "entry_name": name, "mode": "zip-recreated"}
        return {**r, "status": "succeed", "old_entry_id": eid,
                "entry_id": new_id, "mode": "zip-recreated",
                "message": "entry_id CHANGED (zip bundle update = delete+recreate)"}

    # ---- create mode ----
    if not parent_id:
        # privatized html entry endpoint infers the space from parent_entry,
        # so space_id may be absent there — but then parent must be explicit
        parent_id = api.space_root_entry(space_id) if space_id else None
        if not parent_id:
            return {**r, "status": "error",
                    "message": "need space_id (to resolve the space root) or "
                               "parent_entry_id (privatized html endpoint)"}
    if args.state:
        eid, err = api.bind_state(args.state, space_id, parent_id, name,
                                  html=is_html)
    else:
        eid, err = api.upload_bytes(payload_name, payload, space_id, parent_id,
                                    name, html=is_html)
    if err:
        return {**r, "status": "error",
                "message": json.dumps(err, ensure_ascii=False)[:300]}
    manifest[key] = {"content_hash": chash, "entry_id": eid,
                     "entry_name": name, "mode": mode}
    return {**r, "status": "succeed", "entry_id": eid, "name": name}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source", nargs="?",
                    help="HTML file or directory; omit to use config.source "
                         "(zero-arg runs for cron/automation)")
    ap.add_argument("--config", default=str(CONFIG_PATH),
                    help=f"policy config path (default: {CONFIG_PATH}); "
                         f"policies live under configs/ — template "
                         f"configs/config.example.json; api_base REQUIRED "
                         f"(SaaS or privatized)")
    ap.add_argument("--space-id", help="target space (overrides config)")
    ap.add_argument("--parent-entry-id",
                    help="target parent entry (overrides config.parent_entry_id; "
                         "default: space root)")
    ap.add_argument("--entry-id", help="UPDATE existing entry in place "
                    "(raw: re-upload; zip: delete+recreate, entry_id will change)")
    ap.add_argument("--name", help="entry name (default: HTML <title> or filename)")
    ap.add_argument("--raw", action="store_true",
                    help="force raw .html upload, strict self-contained as-is "
                         "(no relative refs, no inlining)")
    ap.add_argument("--bundle", action="store_true",
                    help="force zip bundle (legacy form; NOT previewable in "
                         "the frontend — use only when raw is impossible)")
    ap.add_argument("--dry-run", action="store_true", help="scan+package, no upload")
    ap.add_argument("--force", action="store_true", help="ignore manifest hash")
    ap.add_argument("--no-incremental", action="store_true", help="disable manifest")
    ap.add_argument("--manifest-path", default=None,
                    help="manifest path (default: per-policy "
                         f"manifests/<config_stem>.json)")
    ap.add_argument("--token", default=None,
                    help="override the Bearer access_token (auth_mode=bearer "
                         "policies: tokens rotate ~2h — refresh without editing "
                         "the config file)")
    ap.add_argument("--state", default=None,
                    help="bind an ALREADY uploaded object: skips upload-params + "
                         "COS PUT and passes this state straight to the create / "
                         "re-upload endpoint (privatized gateways whose "
                         "upload-params is unavailable)")
    args = ap.parse_args()

    # config: strict for real runs, lenient for dry-run (dry-run needs no creds,
    # but may read source/space/parent from config)
    config_path = Path(args.config).expanduser()
    if args.dry_run:
        try:
            conf = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            conf = {}
    else:
        conf = load_config(str(config_path))  # raises on missing api_base/keys
        if args.token:                        # refresh an expiring bearer token
            conf["access_token"] = args.token
            conf["auth_mode"] = "bearer"
            missing = [k for k in ("api_base", "staff_id") if not conf.get(k)]
            if missing:
                print(json.dumps({"fatal": f"config missing required keys: {missing}"},
                                 ensure_ascii=False))
                return 1
        # policy must be self-contained: source REQUIRED; space_id REQUIRED too,
        # EXCEPT for privatized policies that route html through
        # `html_entry_path` (that endpoint infers the space from parent_entry).
        # CLI args only override values; never the sole source of a target.
        required = ["source"] + ([] if conf.get("html_entry_path") else ["space_id"])
        missing = [k for k in required if not conf.get(k)]
        if missing:
            print(json.dumps({"fatal": f"config missing required keys: {missing} — "
                              "policy must be self-contained: source (+ space_id, "
                              "unless html_entry_path is set) required in "
                              "configs/<策略>.json (zero-arg ready); "
                              "parent_entry_id optional (default: space root). "
                              "See configs/config.example.json"},
                             ensure_ascii=False))
            return 1
        if conf.get("html_entry_path") and not conf.get("parent_entry_id"):
            print(json.dumps({"fatal": "html_entry_path policies need "
                              "parent_entry_id: the privatized html endpoint "
                              "locates the target by parent_entry only "
                              "(no space_id query)"}, ensure_ascii=False))
            return 1

    # manifest is per-policy: configs/foo.json -> manifests/foo.json
    manifest_path = (Path(args.manifest_path).expanduser()
                     if args.manifest_path
                     else MANIFEST_DIR / f"{config_path.stem}.json")

    # source: CLI arg > config.source
    src = Path(args.source).expanduser() if args.source else None
    if src is None and conf.get("source"):
        src = Path(conf["source"]).expanduser()
    if src is None:
        print(json.dumps({"fatal": "source missing: pass as CLI arg or set "
                          "config.source"}, ensure_ascii=False))
        return 1
    if not src.exists():
        print(json.dumps({"fatal": f"source not found: {src}"}, ensure_ascii=False))
        return 1
    html_files = ([src] if src.is_file() else sorted(src.glob("**/*.html")))
    if not html_files:
        print(json.dumps({"fatal": f"no .html files found under {src}"},
                         ensure_ascii=False))
        return 1

    manifest = load_manifest(manifest_path, not args.no_incremental)

    api = None
    if not args.dry_run:
        api = OpenApi(conf)
        ok, tok = api.get_token()
        if not ok:
            print(json.dumps({"fatal": "get_token failed",
                              "resp": tok}, ensure_ascii=False))
            return 1
        log("token ok")

    # target: CLI args > config
    space_id = args.space_id or conf.get("space_id")
    parent_id = args.parent_entry_id or conf.get("parent_entry_id")
    if not args.dry_run and not args.entry_id and not space_id:
        if parent_id and conf.get("html_entry_path"):
            log("space_id omitted: html_entry_path policy infers space from "
                "parent_entry")
        else:
            print(json.dumps({"fatal": "space_id missing: use --space-id or set "
                              "it in config (or set parent_entry_id together with "
                              "html_entry_path for privatized html uploads)"},
                             ensure_ascii=False))
            return 1

    results = []
    for f in html_files:
        log(f"processing {f} ...")
        try:
            results.append(import_one(api, f, args, space_id,
                                      parent_id, manifest))
        except Exception as e:
            results.append({"source": str(f), "status": "error",
                            "message": f"{type(e).__name__}: {e}"})
        if not args.no_incremental and not args.dry_run:
            save_manifest(manifest_path, manifest)  # save after each item

    def n(status): return sum(1 for x in results if x["status"] == status)
    print(json.dumps({
        "results": results,
        "summary": {"total": len(results), "succeed": n("succeed"),
                    "skipped": n("skipped"), "degraded": sum(
                        1 for x in results if x.get("degraded_resources")),
                    "failed": n("error"), "dry_run": n("dry_run"),
                    "degraded_resources": sorted({d for x in results
                                                  for d in x.get("degraded_resources", [])})},
    }, ensure_ascii=False, indent=1))
    return 0 if n("error") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
