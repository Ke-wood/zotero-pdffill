"""并发抓取：多线程 + 单镜像快速失败 + 总超时。替代串行慢跑。

用法:
    python logs/pdffill_fast.py [--workers 12] [--timeout 8] [--limit N] [--retry-failed]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

ROOT = pathlib.Path(os.environ.get("PDFFILL_ROOT") or pathlib.Path.cwd())
FETCHED = ROOT / "fetched"
LOGS = ROOT / "logs"
FETCHED.mkdir(exist_ok=True)
PLAN = LOGS / "pdffill-plan.json"
LOG = LOGS / "pdffill-fetch.jsonl"

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}
MAIL = os.environ.get("PDFFILL_MAIL", "user@example.com")
MIRRORS = ["https://sci-hub.box", "https://www.sci-hub.ren", "https://sci-hub.ru"]
PDF_META = re.compile(r'citation_pdf_url"\s+content="([^"]+)"', re.I)
PDF_ANY = re.compile(r'(?:data|href)\s*=\s*["\']([^"\']*\.pdf[^"\']*)["\']', re.I)

_lock = threading.Lock()


def log(**kw):
    with _lock, LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), **kw}, ensure_ascii=False) + "\n")


def valid(p: pathlib.Path) -> bool:
    try:
        if p.open("rb").read(5) != b"%PDF-":
            return False
        out = subprocess.run(["pdftotext", "-l", "2", str(p), "-"],
                             capture_output=True, timeout=40).stdout
        return len(out.decode("utf-8", "ignore").strip()) >= 200
    except Exception:
        return False


def save(dest: pathlib.Path, content: bytes) -> bool:
    if len(content) < 20000 or content[:5] != b"%PDF-":
        return False
    dest.write_bytes(content)
    if valid(dest):
        return True
    dest.unlink(missing_ok=True)
    return False


def try_scihub(c, doi, dest, quick):
    """quick=True 时只试第一个镜像，失败即返回（快速筛）。"""
    for base in (MIRRORS[:1] if quick else MIRRORS):
        try:
            r = c.get(f"{base}/{doi}", timeout=quick and 8 or 20)
            if r.status_code != 200:
                continue
            m = PDF_META.search(r.text) or PDF_ANY.search(r.text)
            if not m:
                continue
            pu = m.group(1)
            if pu.startswith("//"):
                pu = "https:" + pu
            elif pu.startswith("/"):
                pu = base + pu
            r2 = c.get(pu, headers={**UA, "Referer": f"{base}/{doi}"}, timeout=90)
            if save(dest, r2.content):
                return f"scihub:{base.split('//')[1]}"
        except Exception:
            continue
    return ""


def try_unpaywall(c, doi, dest):
    try:
        r = c.get(f"https://api.unpaywall.org/v2/{doi}?email={MAIL}", timeout=15)
        if r.status_code != 200:
            return ""
        for loc in (r.json().get("oa_locations") or []):
            u = loc.get("url_for_pdf") or loc.get("url")
            if not u:
                continue
            try:
                if save(dest, c.get(u, timeout=40).content):
                    return "unpaywall"
            except Exception:
                continue
    except Exception:
        pass
    return ""


def worker(x, quick):
    dest = FETCHED / f"{x['k']}.pdf"
    if dest.exists():
        return x["k"], "exists", 0
    with httpx.Client(trust_env=False, follow_redirects=True, headers=UA) as c:
        for fn in (try_unpaywall,):
            src = fn(c, x["doi"], dest)
            if src:
                return x["k"], src, dest.stat().st_size if dest.exists() else 0
        src = try_scihub(c, x["doi"], dest, quick)
        if src:
            return x["k"], src, dest.stat().st_size if dest.exists() else 0
    return x["k"], "", 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--quick", action="store_true", default=True,
                    help="只试第一个镜像（默认开），失败的留给人工/后续全量重试")
    a = ap.parse_args()

    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    failed = set()
    if not a.retry_failed:
        for name in ("pdffill-fetch.jsonl", "multi.jsonl", "scihub.jsonl"):
            f = LOGS / name
            if not f.exists():
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ok") is False and r.get("k"):
                    failed.add(r["k"])
    todo = [x for x in plan
            if not (FETCHED / f"{x['k']}.pdf").exists() and x["k"] not in failed]
    if a.limit:
        todo = todo[:a.limit]
    print(f"待抓 {len(todo)}（并发 {a.workers}，快速模式={a.quick}）", flush=True)
    if not todo:
        return

    ok = bad = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(worker, x, a.quick): x for x in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            x = futs[fut]
            try:
                k, src, size = fut.result()
            except Exception as e:
                k, src, size = x["k"], "", 0
            if src:
                ok += 1
                log(k=k, doi=x["doi"], ok=True, via=src, size=size)
                print(f"  [{i}/{len(todo)}] OK {k} via {src} {size}B", flush=True)
            else:
                bad += 1
                log(k=k, doi=x["doi"], ok=False)
                if i % 20 == 0:
                    print(f"  [{i}/{len(todo)}] ... 成功 {ok} 失败 {bad}", flush=True)
    dt = time.time() - t0
    print(f"\n完成：成功 {ok} / 失败 {bad}，耗时 {dt/60:.1f} 分钟", flush=True)
    print(f"速度 {len(todo)/max(dt,1):.2f} 篇/秒", flush=True)


main()
