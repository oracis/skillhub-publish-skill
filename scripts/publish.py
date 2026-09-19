#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SkillHub 发布器（纯标准库，零第三方依赖）。

在官方 CLI 之上补齐官方 CLI 缺的能力，并在失败时给出**可定位根因**的判定。

相比官方 CLI 多做的事：
  * 白名单预过滤：官方 CLI 会把 .gitignore/LICENSE/README 一起传，服务端报
    400「不允许的文件类型」。本脚本按服务端接受范围自动过滤。
  * 封面图：走独立的 `cover` multipart 字段（不打进包，包内位图会被拒）。
  * 429 限流：指数退避重试（30s 起、翻倍、封顶 600s、最多 7 次）。
  * slug 冲突自愈：409/500「已被占用」时自动追加后缀重试，并回写 SKILL.md。
  * **WAF 566 判定（本脚本独有）**：区分「被腾讯云 WAF 按内容拦」与「业务校验失败」。
    判据：同一份 multipart 发过去，401=已到应用层（内容安全），566=WAF 拦截。
    这是官方 CLI 报「提交失败」时唯一能定位根因的手段 —— 见 scripts/waf_bisect.py。
  * **发布后状态回读（本脚本独有）**：`status` 命令从 /api/v1/search 读线上版本、
    下载量、更新时间，自动判断审核是否通过 —— 不用再开浏览器看 Dashboard。
  * **版本递增硬卡点（本脚本独有）**：发布前比对「本地版本 vs 线上版本」，
    版本号不递增会被服务端拒绝，本脚本提前拦下并说明原因。

用法：
    python publish.py check-login                          # 检查登录态
    python publish.py validate <dir>                       # 校验 frontmatter
    python publish.py status <slug>                        # 回读线上状态（独有）
    python publish.py status <dir>                         # 传目录则自动比对本地/线上版本
    python publish.py publish <dir> --version 1.3.0 \
           --changelog "..." [--cover cover.png] [--slug x] [--dry-run] [--force]
    python publish.py compare "<关键词>"                    # 竞品对标（独有）

退出码：0 = 成功，1 = 失败。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOME = os.path.expanduser("~")
CREDENTIALS = os.path.join(HOME, ".skillhub", "credentials.json")
DEFAULT_HOST = "https://api.skillhub.cn"
PUBLISH_PATH = "/api/v1/community/skills/publish"
ME_PATH = "/api/v1/auth/me"
SEARCH_PATH = "/api/v1/search"

# SkillHub 服务端接受的类型（2026-09-19 实测）：
#   ✅ .md（含 references/*.md）、.yaml/.yml、.py、.json、.txt 等文本
#   ❌ 位图（.png/.jpg/...）、LICENSE（无论有无扩展名）
# 注意：这不是「只有 SKILL.md/manifest.yaml/scripts」的白名单——`.md` 是允许的，
# 所以 references/ 目录可以正常带上（已用真实请求验证）。
ALLOWED_FILES = {"SKILL.md", "manifest.yaml", "manifest.yml"}
ALLOWED_PREFIXES = ("scripts/", "references/", "assets/")
ALLOWED_EXTS = {".md", ".yaml", ".yml", ".py", ".json", ".txt", ".toml", ".cfg", ".ini", ".sh"}
# 这些必被拒（实测）：白名单外 + 服务端明确报「不允许的文件类型」
NEVER_UPLOAD = {".gitignore", ".gitattributes", ".ds_store"}
NEVER_UPLOAD_PREFIX = ("license",)
BITMAP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


# --------------------------------------------------------------------------- #
# 凭据
# --------------------------------------------------------------------------- #
def load_creds(host_override=None):
    """读 ~/.skillhub/credentials.json，返回 (token, host)。"""
    if not os.path.exists(CREDENTIALS):
        return None, "凭据文件不存在，先运行 SkillHub CLI 的 login"
    try:
        d = json.loads(open(CREDENTIALS, encoding="utf-8").read())
        user = d.get("user") or {}
        token = user.get("token") or d.get("token")
        host = host_override or user.get("host") or DEFAULT_HOST
        if not token:
            return None, "凭据文件里没有 token"
        return (token, host), None
    except Exception as e:
        return None, f"凭据解析失败：{e}"


def check_login(host_override=None):
    creds, err = load_creds(host_override)
    if not creds:
        print(json.dumps({"loggedIn": False, "error": err}, ensure_ascii=False, indent=2))
        return 1
    token, host = creds
    req = urllib.request.Request(
        host.rstrip("/") + ME_PATH,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
            u = data.get("user", data) or {}
            print(json.dumps({"loggedIn": True, "userId": u.get("id"),
                              "handle": u.get("handle", ""), "host": host},
                             ensure_ascii=False, indent=2))
            return 0
    except Exception as e:
        print(json.dumps({"loggedIn": False, "error": str(e)[:200]}, ensure_ascii=False, indent=2))
        return 1


# --------------------------------------------------------------------------- #
# 版本比较（SemVer）
# --------------------------------------------------------------------------- #
def ver_tuple(v):
    """把 SemVer 转成可比较的元组，忽略 pre-release 后缀。"""
    core = str(v or "").strip().split("-")[0].split("+")[0]
    parts = []
    for seg in core.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def ver_newer(new, old):
    """new 是否严格大于 old。任一为空 → 返回 None（无法判断）。"""
    if not new or not old:
        return None
    return ver_tuple(new) > ver_tuple(old)


# --------------------------------------------------------------------------- #
# 线上状态回读（/api/v1/search）
# --------------------------------------------------------------------------- #
def search_skills(query, token=None, host=DEFAULT_HOST, limit=None):
    """调 /api/v1/search，返回 results 列表。无需鉴权也能查，带上更稳。"""
    url = host.rstrip("/") + SEARCH_PATH + "?q=" + urllib.parse.quote(query)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8") or "{}")
    results = data.get("results") or data.get("skills") or []
    return results[:limit] if limit else results


def fetch_remote(slug, token=None, host=DEFAULT_HOST):
    """按 slug **精确**取线上条目。

    注意：search 是模糊匹配 —— 查一个不存在的 slug 也会返回 10 条无关结果，
    所以必须按 slug 字段精确过滤，绝不能取第一条。
    """
    for item in search_skills(slug, token, host):
        if str(item.get("slug") or "").strip() == slug:
            return item
    return None


def fmt_ts(ms):
    """毫秒时间戳 → 可读时间。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except Exception:
        return "-"


def status(target, host_override=None, as_json=False):
    """回读线上状态；target 可以是 slug 或本地 skill 目录。

    传目录时会额外比对「本地版本 vs 线上版本」，直接告诉你审核是否通过、
    以及版本号是否够新。
    """
    local_dir, local_ver, local_slug = None, None, None
    if os.path.isdir(target):
        local_dir = os.path.abspath(target)
        md = os.path.join(local_dir, "SKILL.md")
        if not os.path.exists(md):
            print(json.dumps({"ok": False, "error": f"{md} 不存在"}, ensure_ascii=False, indent=2))
            return 1
        fm = load_frontmatter(md)
        local_ver = str(fm.get("version") or "").strip() or None
        local_slug = str(fm.get("slug") or "").strip() or None

    slug = local_slug or target

    creds, _ = load_creds(host_override)
    token, host = creds if creds else (None, host_override or DEFAULT_HOST)

    try:
        item = fetch_remote(slug, token, host)
    except Exception as e:
        print(json.dumps({"ok": False, "slug": slug, "error": f"查询失败：{e}"},
                         ensure_ascii=False, indent=2))
        return 1

    if not item:
        result = {"ok": True, "slug": slug, "found": False,
                  "message": "线上没有这个 slug（尚未发布，或还在审核中未对外可见）"}
        if local_ver:
            result["localVersion"] = local_ver
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    remote_ver = str(item.get("version") or "").strip() or None
    out = {
        "ok": True, "slug": slug, "found": True,
        "displayName": item.get("displayName") or item.get("name"),
        "remoteVersion": remote_ver,
        "downloads": item.get("downloads", 0),
        "installs": item.get("installs", 0),
        "stars": item.get("stars", 0),
        "updatedAt": fmt_ts(item.get("updatedAt") or item.get("updated_at")),
        "category": item.get("category") or "-",
        "url": item.get("homepage"),
    }

    if local_ver:
        out["localVersion"] = local_ver
        newer = ver_newer(local_ver, remote_ver)
        same = local_ver == remote_ver
        if same:
            out["sync"] = "已同步"
            out["hint"] = f"本地 {local_ver} 已上线，审核已通过"
        elif newer:
            out["sync"] = "本地更新"
            out["hint"] = (f"本地 {local_ver} > 线上 {remote_ver} —— 还没发布，或已提交但仍在审核中")
        elif newer is False:
            out["sync"] = "版本倒挂"
            out["hint"] = (f"本地 {local_ver} <= 线上 {remote_ver} —— 服务端会拒绝此版本号，"
                           "请把版本调到高于线上再发")
        else:
            out["sync"] = "未知"

    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"技能      {out['displayName']}  ({slug})")
    print(f"线上版本  {remote_ver or '-'}      更新时间 {out['updatedAt']}")
    print(f"数据      下载 {out['downloads']}   安装 {out['installs']}   收藏 {out['stars']}")
    if local_ver:
        mark = {"已同步": "✓", "本地更新": "↑", "版本倒挂": "✗"}.get(out.get("sync"), "?")
        print(f"本地版本  {local_ver}")
        print(f"同步状态  {mark} {out.get('sync')} —— {out.get('hint')}")
    return 0


def compare(query, host_override=None, limit=12):
    """竞品对标：搜关键词，按下载量列出同类技能。"""
    creds, _ = load_creds(host_override)
    token, host = creds if creds else (None, host_override or DEFAULT_HOST)
    try:
        items = search_skills(query, token, host, limit=limit)
    except Exception as e:
        print(f"查询失败：{e}", file=sys.stderr)
        return 1
    if not items:
        print("没有匹配结果")
        return 0

    items.sort(key=lambda x: -(x.get("downloads") or 0))
    print(f"关键词「{query}」同类技能（按下载量排序）")
    print("=" * 76)
    print(f"{'版本':<10}{'下载':>9}{'安装':>8}  技能")
    print("-" * 76)
    for it in items:
        ver = str(it.get("version") or "-")[:9]
        dl = it.get("downloads") or 0
        ins = it.get("installs") or 0
        name = it.get("displayName") or it.get("name") or it.get("slug")
        owner = (it.get("namespace") or {}).get("handle") or it.get("owner_name") or ""
        print(f"{ver:<10}{dl:>9}{ins:>8}  {name}  @{owner}")
    print("-" * 76)
    print(f"共 {len(items)} 条。下载量反映曝光面，安装量反映真实使用。")
    return 0


# --------------------------------------------------------------------------- #
# frontmatter
# --------------------------------------------------------------------------- #
def load_frontmatter(md_path):
    text = open(md_path, encoding="utf-8", errors="replace").read()
    if not text.startswith("---"):
        return {}
    lines = text.split("\n")
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return {}
    fm = {}
    for raw in lines[1:end]:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            fm[k] = [] if not inner else [s.strip().strip("'\"") for s in inner.split(",") if s.strip()]
        else:
            fm[k] = v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"') else v
    return fm


def validate_skill(skill_dir):
    md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(md):
        print(json.dumps({"valid": False, "error": "SKILL.md 不存在"}, ensure_ascii=False, indent=2))
        return 1
    fm = load_frontmatter(md)
    issues = []
    for f in ("slug", "version", "displayName", "name", "description"):
        if not str(fm.get(f) or "").strip():
            issues.append(f"缺必填字段：{f}")
    slug = str(fm.get("slug") or "").strip()
    if slug and not SLUG_RE.match(slug):
        issues.append(f"slug 非法（须 kebab-case 小写）：{slug}")
    ver = str(fm.get("version") or "").strip()
    if ver and not SEMVER_RE.match(ver):
        issues.append(f"version 非 SemVer：{ver}")
    if issues:
        print(json.dumps({"valid": False, "issues": issues, "metadata": fm},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"valid": True, "metadata": fm}, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# 打包含 / 封面
# --------------------------------------------------------------------------- #
def collect_bundle(skill_dir, verbose=True):
    """按服务端接受的类型收集待上传文件，返回 [(rel, bytes)]。"""
    files, skipped = [], []
    for root, dirs, names in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in
                   {"__pycache__", ".git", ".idea", ".vscode", "node_modules", ".venv", "venv"}]
        for n in names:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, skill_dir).replace("\\", "/")
            base = os.path.basename(rel)
            low = base.lower()
            ext = os.path.splitext(low)[1]

            if low in NEVER_UPLOAD or low.startswith(NEVER_UPLOAD_PREFIX) or ext in {".pyc", ".pyo"}:
                skipped.append((rel, "服务端必拒（不允许的文件类型）"))
                continue
            if ext in BITMAP_EXTS:
                skipped.append((rel, "位图：须走 cover 字段单独上传，不能打进包"))
                continue
            if not (rel in ALLOWED_FILES or rel.startswith(ALLOWED_PREFIXES) or ext in ALLOWED_EXTS):
                skipped.append((rel, "类型不在服务端接受范围"))
                continue
            try:
                files.append((rel, open(p, "rb").read()))
            except Exception as e:
                skipped.append((rel, f"读取失败 {e}"))
    files.sort(key=lambda x: x[0])
    if verbose and skipped:
        print("已排除（打入包会被服务端拒）:", file=sys.stderr)
        for rel, why in skipped:
            print(f"  - {rel}  ({why})", file=sys.stderr)
    return files


def find_cover(skill_dir, explicit=None):
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(skill_dir, explicit)
        if os.path.exists(p):
            return p
        print(f"WARN: --cover {explicit} 不存在，跳过", file=sys.stderr)
        return None
    for n in ("cover.png", "cover.jpg", "cover.jpeg", "cover.webp", "icon.png", "icon.jpg"):
        cand = os.path.join(skill_dir, n)
        if os.path.exists(cand):
            return cand
    return None


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #
def post_publish(host, token, payload, files, cover_bytes=None, cover_name="cover.png", timeout=120):
    boundary = "----skillhubboundary" + uuid.uuid4().hex
    body = bytearray()

    def add(name, data, filename=None, ctype="application/octet-stream"):
        body.extend(f"--{boundary}\r\n".encode())
        if filename:
            body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        else:
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n'.encode())
        body.extend(f"Content-Type: {ctype}\r\n\r\n".encode())
        body.extend(data)
        body.extend(b"\r\n")

    add("payload", json.dumps(payload, ensure_ascii=False).encode("utf-8"), ctype="application/json")
    for rel, data in files:
        ctype = "text/markdown" if rel.lower().endswith((".md", ".yaml", ".yml")) else "text/x-python"
        add("files", data, filename=rel, ctype=ctype)
    if cover_bytes:
        ext = os.path.splitext(cover_name)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp"}.get(ext, "application/octet-stream")
        add("cover", cover_bytes, filename=cover_name, ctype=ctype)
    body.extend(f"--{boundary}--\r\n".encode())

    url = host.rstrip("/") + PUBLISH_PATH
    req = urllib.request.Request(url, data=bytes(body), method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
        "Origin": "https://skillhub.cn",
        "Referer": "https://skillhub.cn/",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.getcode(), json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return r.getcode(), {"raw": raw.decode("utf-8", "replace")[:400]}
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read() or b""
        except Exception:
            pass
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            parsed = {"raw": raw.decode("utf-8", "replace")[:400]}
        return e.code, parsed
    except Exception as e:
        return "ERR", {"error": str(e)[:200]}


def judge_failure(status, body):
    """把失败状态归类，并给出**正确的**下一步动作。"""
    msg = json.dumps(body, ensure_ascii=False).lower()
    if status == 566 or "566" in msg:
        return ("WAF拦截",
                "腾讯云 WAF 按内容拦截（不是结构问题，也不是网络问题）。"
                "跑 scripts/waf_bisect.py --step check 确认，再用 --step bisect 二分出触发行。")
    if status == 401 or status == 403:
        return ("未授权", "登录态失效，重跑 check-login 或 skillhub login")
    if status == 400 and ("不允许的文件类型" in msg or "file type" in msg):
        return ("文件类型被拒", "包里有白名单外文件；本脚本已自动过滤，若仍报，检查是否手动传了 zip")
    if status == 400:
        return ("字段/内容校验", "检查 frontmatter 必填字段与 payload，跑 validate 命令")
    if status == 409 or (status == 500 and ("exist" in msg or "占用" in msg or "another user" in msg)):
        return ("slug 冲突", "slug 已被占用，换一个（本脚本会自动重试后备 slug）")
    if status == 429:
        return ("限流", "触发限流，本脚本会自动退避重试")
    return ("未知", "把完整响应贴出来再排查")


def bump_version(v):
    """给一个版本号建议下一个值（patch +1）。"""
    t = list(ver_tuple(v))
    t[2] += 1
    return ".".join(str(x) for x in t)


def _safe_fetch_remote(slug, host_override=None):
    """查线上条目；任何异常都吞掉返回 None（版本检查不应阻断离线场景）。"""
    try:
        creds, _ = load_creds(host_override)
        token, host = creds if creds else (None, host_override or DEFAULT_HOST)
        return fetch_remote(slug, token, host)
    except Exception:
        return None


def rewrite_slug(md_path, slug):
    try:
        lines = open(md_path, encoding="utf-8").read().split("\n")
        for i, line in enumerate(lines):
            if line.strip().startswith("slug:"):
                lines[i] = f"slug: {slug}"
                open(md_path, "w", encoding="utf-8").write("\n".join(lines))
                return True
        for i, line in enumerate(lines):
            if line.strip() == "---" and i > 0:
                lines.insert(i + 1, f"slug: {slug}")
                open(md_path, "w", encoding="utf-8").write("\n".join(lines))
                return True
    except Exception:
        pass
    return False


def publish(skill_dir, version="", changelog="", cover=None, slug_override="",
            dry_run=False, host_override=None, max_retry=7, force=False):
    skill_dir = os.path.abspath(skill_dir)
    md_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.exists(md_path):
        print(json.dumps({"success": False, "error": "SKILL.md 不存在"}, ensure_ascii=False))
        return 1

    fm = load_frontmatter(md_path)
    base_slug = str(slug_override or fm.get("slug") or os.path.basename(skill_dir)).strip()
    if not SLUG_RE.match(base_slug):
        base_slug = re.sub(r"[^a-z0-9-]", "-", base_slug.lower()).strip("-")
    ver = version or str(fm.get("version") or "1.0.0")
    display = str(fm.get("displayName") or fm.get("name") or os.path.basename(skill_dir))

    files = collect_bundle(skill_dir)
    if not any(rel == "SKILL.md" for rel, _ in files):
        print(json.dumps({"success": False, "error": "包里没有 SKILL.md"}, ensure_ascii=False))
        return 1

    cover_path = find_cover(skill_dir, cover)
    cover_bytes = open(cover_path, "rb").read() if cover_path else None

    if dry_run:
        remote_preview = _safe_fetch_remote(base_slug, host_override)
        out = {
            "dryRun": True, "slug": base_slug, "version": ver, "displayName": display,
            "files": [rel for rel, _ in files],
            "totalBytes": sum(len(d) for _, d in files),
            "cover": os.path.basename(cover_path) if cover_path else None,
        }
        if remote_preview:
            rv = remote_preview.get("version")
            out["remoteVersion"] = rv
            out["versionCheck"] = "OK" if ver_newer(ver, rv) else (
                "SAME" if str(ver) == str(rv) else "TOO_OLD")
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    creds, err = load_creds(host_override)
    if not creds:
        print(json.dumps({"success": False, "error": err}, ensure_ascii=False, indent=2))
        return 1
    token, host = creds

    # ---- 版本递增硬卡点（本脚本独有）----
    # 服务端要求更新时 version 必须严格高于线上；离线时降级为提示而非阻断。
    if not force:
        remote = _safe_fetch_remote(base_slug, host_override)
        if remote:
            rver = str(remote.get("version") or "").strip()
            if rver and not ver_newer(ver, rver):
                kind = "相同" if str(ver) == rver else "更低"
                print(json.dumps({
                    "success": False, "blockedBy": "version_check", "slug": base_slug,
                    "localVersion": ver, "remoteVersion": rver,
                    "error": f"版本号 {ver} 比线上 {rver} {kind}，服务端会拒绝更新",
                    "fix": f"把版本号改成高于 {rver} 的值（如升到 {bump_version(rver)}），"
                           "或加 --force 跳过此检查",
                }, ensure_ascii=False, indent=2))
                return 1
            print(f"  版本检查通过：{ver} > 线上 {rver}", file=sys.stderr)
        else:
            print(f"  版本检查跳过：线上查不到 {base_slug}（首次发布，或审核中未可见）", file=sys.stderr)

    payload = {
        "slug": base_slug, "version": ver, "displayName": display,
        "summaryZh": str(fm.get("summary") or fm.get("description") or "")[:500],
        "changelog": changelog or "",
    }

    candidates = [base_slug]
    if not base_slug.endswith("-skill"):
        candidates.append(base_slug + "-skill")
    if not base_slug.startswith("wb-"):
        candidates.append("wb-" + base_slug)

    last_status, last_body = None, {}
    for idx, slug in enumerate(candidates):
        payload["slug"] = slug
        delay = 30
        for attempt in range(max_retry):
            last_status, last_body = post_publish(host, token, payload, files, cover_bytes,
                                                 cover_name=os.path.basename(cover_path) if cover_path else "cover.png")
            if last_status == 429:
                ra = last_body.get("retryAfter") or delay
                try:
                    ra = int(ra)
                except Exception:
                    ra = delay
                print(f"  [429] 限流，等待 {ra}s（{attempt+1}/{max_retry}）", file=sys.stderr, flush=True)
                time.sleep(ra)
                delay = min(delay * 2, 600)
                continue
            break

        if last_status in (200, 201):
            result = last_body if isinstance(last_body, dict) else {}
            result["slugUsed"] = slug
            print(json.dumps({"success": True, "result": result}, ensure_ascii=False, indent=2))
            if slug != str(fm.get("slug") or ""):
                if rewrite_slug(md_path, slug):
                    print(f"  已把 slug={slug} 回写进 SKILL.md", file=sys.stderr)
            return 0

        kind, advice = judge_failure(last_status, last_body)
        print(f"  [{kind}] status={last_status} → {advice}", file=sys.stderr, flush=True)
        body_str = json.dumps(last_body, ensure_ascii=False).lower()
        taken = last_status == 409 or (last_status == 500 and
                                       ("exist" in body_str or "占用" in body_str or "another user" in body_str))
        if taken and idx < len(candidates) - 1:
            print(f"  slug '{slug}' 被占用，尝试后备：{candidates[idx+1]}", file=sys.stderr)
            continue
        break

    kind, advice = judge_failure(last_status, last_body)
    print(json.dumps({"success": False, "status": last_status, "kind": kind,
                      "advice": advice, "body": last_body}, ensure_ascii=False, indent=2))
    return 1


def main():
    ap = argparse.ArgumentParser(description="SkillHub 发布器（含 WAF 566 定位、状态回读、版本卡点）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-login", help="检查登录态").add_argument("--host", default=None)

    pv = sub.add_parser("validate", help="校验 frontmatter")
    pv.add_argument("dir")

    ps = sub.add_parser("status", help="回读线上状态（传目录则比对本地/线上版本）")
    ps.add_argument("target", help="slug 或本地 skill 目录")
    ps.add_argument("--host", default=None)
    ps.add_argument("--json", dest="as_json", action="store_true")

    pc = sub.add_parser("compare", help="竞品对标：按关键词搜同类技能并按下载量排序")
    pc.add_argument("query")
    pc.add_argument("--host", default=None)
    pc.add_argument("--limit", type=int, default=12)

    pp = sub.add_parser("publish", help="发布/更新")
    pp.add_argument("dir")
    pp.add_argument("--version", default="")
    pp.add_argument("--changelog", default="")
    pp.add_argument("--cover", default=None)
    pp.add_argument("--slug", default="")
    pp.add_argument("--host", default=None)
    pp.add_argument("--dry-run", action="store_true")
    pp.add_argument("--force", action="store_true", help="跳过版本递增检查")

    args = ap.parse_args()
    if args.cmd == "check-login":
        return check_login(args.host)
    if args.cmd == "validate":
        return validate_skill(args.dir)
    if args.cmd == "status":
        return status(args.target, args.host, args.as_json)
    if args.cmd == "compare":
        return compare(args.query, args.host, args.limit)
    if args.cmd == "publish":
        return publish(args.dir, args.version, args.changelog, args.cover,
                       args.slug, args.dry_run, args.host, force=args.force)
    return 1


if __name__ == "__main__":
    sys.exit(main())
