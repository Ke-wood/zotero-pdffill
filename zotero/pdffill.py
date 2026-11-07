#!/usr/bin/env python
"""pdffill — Zotero 缺 PDF 补齐的统一入口。

一条命令完成：盘点 -> 抓取 -> 入库 -> 报告。

用法
    python pdffill.py scan   [--collection 名称或ID] [--all]
        盘点缺 PDF 的条目，生成工作清单 logs/pdffill-plan.json

    python pdffill.py fetch  [--limit N] [--retry-failed]
        按清单多源抓取（Sci-Hub 多镜像 -> Unpaywall -> arXiv -> EuropePMC -> 出版社直链）

    python pdffill.py attach [--backup]
        把 fetched/ 里已抓的 PDF 挂回 Zotero（要求 Zotero 完全退出）

    python pdffill.py report
        输出覆盖统计报告

    python pdffill.py run    [--collection X] [--limit N]
        scan + fetch + attach 一条龙（attach 前自动检查 Zotero 是否退出）

设计要点
    - 抓取不需要 Zotero；只有 attach 需要，且必须 Zotero 正常退出（强杀会回滚）。
    - 入库是直写 sqlite：items + itemAttachments + itemData，文件放 storage/<key>/。
    - 写前必须 wal_checkpoint(TRUNCATE)，否则残留 -wal 会让脚本误判。
    - 每批写前整库备份。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = pathlib.Path(os.environ.get("PDFFILL_ROOT") or pathlib.Path.cwd())
FETCHED = ROOT / "fetched"
LOGS = ROOT / "logs"
FETCHED.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)


def find_zotero_db() -> pathlib.Path:
    """定位 zotero.sqlite：环境变量 > prefs.js 的 dataDir > 常见默认位置。"""
    env = os.environ.get("ZOTERO_DB_PATH")
    if env:
        return pathlib.Path(env)
    roaming = pathlib.Path(os.environ.get("APPDATA", "")) / "Zotero" / "Zotero" / "Profiles"
    if roaming.is_dir():
        for prefs in roaming.glob("*/prefs.js"):
            try:
                for line in prefs.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "extensions.zotero.dataDir" in line and "useDataDir" not in line:
                        m = re.search(r'"([^"]+)"\s*\)', line)
                        if m:
                            cand = pathlib.Path(m.group(1).replace("\\\\", "\\")) / "zotero.sqlite"
                            if cand.exists():
                                return cand
            except OSError:
                continue
    for p in (pathlib.Path.home() / "Zotero" / "zotero.sqlite",
              pathlib.Path.home() / "Documents" / "Zotero" / "zotero.sqlite"):
        if p.exists():
            return p
    return pathlib.Path.home() / "Zotero" / "zotero.sqlite"


DB = find_zotero_db()
STORAGE = DB.parent / "storage"
PLAN = LOGS / "pdffill-plan.json"
FETCHLOG = LOGS / "pdffill-fetch.jsonl"

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")}
MAIL = os.environ.get("PDFFILL_MAIL", "user@example.com")
MIRRORS = ["https://sci-hub.box", "https://www.sci-hub.ren", "https://sci-hub.ru",
           "https://sci-hub.wf", "https://scihub.help"]
PDF_META = re.compile(r'citation_pdf_url"\s+content="([^"]+)"', re.I)
PDF_ANY = re.compile(r'(?:data|href)\s*=\s*["\']([^"\']*\.pdf[^"\']*)["\']', re.I)


# ---------------------------------------------------------------- 工具

def say(msg: str) -> None:
    print(msg, flush=True)


def zotero_running() -> bool:
    for port in (23119, 23140):
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
    return False


def ro_conn() -> sqlite3.Connection:
    """读库用只读副本（Zotero 开着也能读）。"""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "z.sqlite"
    shutil.copy2(DB, tmp)
    for e in ("-wal", "-shm"):
        p = pathlib.Path(str(DB) + e)
        if p.exists():
            shutil.copy2(p, pathlib.Path(str(tmp) + e))
    return sqlite3.connect(tmp)


def valid_pdf(p: pathlib.Path) -> bool:
    try:
        if p.open("rb").read(5) != b"%PDF-":
            return False
        out = subprocess.run(["pdftotext", "-l", "3", str(p), "-"],
                             capture_output=True, timeout=45).stdout
        return len(out.decode("utf-8", "ignore").strip()) >= 200
    except Exception:
        return False


def pub_of(doi: str, group: str = "") -> str:
    d, g = (doi or "").lower(), (group or "").lower()
    for pre, name in (("10.1016", "elsevier"), ("10.1093", "oup"), ("10.1021", "acs"),
                      ("10.1002", "wiley"), ("10.1111", "wiley"), ("10.1034", "wiley"),
                      ("10.1007", "springer"), ("10.1186", "springer"),
                      ("10.1038", "nature"), ("10.1073", "pnas"), ("10.3390", "mdpi"),
                      ("10.1242", "cob"), ("10.1080", "tandf"), ("10.1126", "science"),
                      ("10.1101", "biorxiv"), ("10.6084", "figshare")):
        if d.startswith(pre):
            return name
    return "other"


# ---------------------------------------------------------------- scan

def cmd_scan(a) -> int:
    c = ro_conn()
    where, params = [], []
    if a.collection:
        sel = a.collection
        if sel.isdigit():
            cid = int(sel)
        else:
            row = c.execute("SELECT collectionID FROM collections WHERE collectionName=?",
                            (sel,)).fetchone()
            if not row:
                say(f"找不到分类：{sel}")
                return 1
            cid = row[0]
        where.append("EXISTS (SELECT 1 FROM collectionItems ci WHERE ci.itemID=i.itemID "
                     "AND ci.collectionID=?)")
        params.append(cid)

    sql = f"""
    SELECT i.key,
      (SELECT v.value FROM itemData d JOIN fields f ON f.fieldID=d.fieldID
        JOIN itemDataValues v ON v.valueID=d.valueID
        WHERE d.itemID=i.itemID AND f.fieldName='DOI') doi,
      (SELECT v.value FROM itemData d JOIN fields f ON f.fieldID=d.fieldID
        JOIN itemDataValues v ON v.valueID=d.valueID
        WHERE d.itemID=i.itemID AND f.fieldName='title') ttl,
      (SELECT COUNT(*) FROM itemAttachments ia
        WHERE ia.parentItemID=i.itemID AND ia.contentType='application/pdf') npdf
    FROM items i JOIN itemTypes t ON t.itemTypeID=i.itemTypeID
    WHERE t.typeName NOT IN ('attachment','note','annotation')
      AND {(' AND '.join(where)) if where else '1=1'}
    """
    rows = c.execute(sql, params).fetchall()
    missing = [r for r in rows if r[3] == 0]
    withdoi = [r for r in missing if (r[1] or "").strip()]
    plan = [{"k": r[0], "doi": r[1].strip(), "pub": pub_of(r[1]), "title": r[2] or ""}
            for r in withdoi]
    # 去掉本地已有文件的
    plan = [x for x in plan if not (FETCHED / f"{x['k']}.pdf").exists()]
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    from collections import Counter
    say(f"条目 {len(rows)} | 缺 PDF {len(missing)} | 其中有 DOI {len(withdoi)} | 待抓 {len(plan)}")
    for p, n in Counter(x["pub"] for x in plan).most_common():
        say(f"  {p:10s} {n}")
    say(f"清单 -> {PLAN}")
    return 0


# ---------------------------------------------------------------- fetch

def log_fetch(**kw) -> None:
    with FETCHLOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), **kw}, ensure_ascii=False) + "\n")


def save(dest: pathlib.Path, content: bytes) -> bool:
    if len(content) < 20000 or content[:5] != b"%PDF-":
        return False
    dest.write_bytes(content)
    if valid_pdf(dest):
        return True
    dest.unlink(missing_ok=True)
    return False


def src_scihub(c, doi, dest) -> str:
    for base in MIRRORS:
        url = f"{base}/{doi}"
        try:
            r = c.get(url, timeout=40)
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
            r2 = c.get(pu, headers={**UA, "Referer": url}, timeout=90)
            if save(dest, r2.content):
                return f"scihub:{base.split('//')[1]}"
        except Exception:
            continue
    return ""


def src_unpaywall(c, doi, dest) -> str:
    try:
        r = c.get(f"https://api.unpaywall.org/v2/{doi}?email={MAIL}", timeout=30)
        if r.status_code != 200:
            return ""
        for loc in (r.json().get("oa_locations") or []):
            u = loc.get("url_for_pdf") or loc.get("url")
            if not u:
                continue
            try:
                if save(dest, c.get(u, timeout=60).content):
                    return "unpaywall"
            except Exception:
                continue
    except Exception:
        pass
    return ""


def src_arxiv(c, doi, dest) -> str:
    if "arxiv" not in doi.lower():
        return ""
    m = re.search(r"(\d{4}\.\d{4,5})", doi)
    if not m:
        return ""
    try:
        if save(dest, c.get(f"https://arxiv.org/pdf/{m.group(1)}", timeout=60).content):
            return "arxiv"
    except Exception:
        pass
    return ""


def src_epmc(c, doi, dest) -> str:
    try:
        r = c.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                  params={"query": f"DOI:{doi}", "format": "json", "resultType": "core"},
                  timeout=30)
        if r.status_code != 200:
            return ""
        for hit in r.json().get("resultList", {}).get("result", []):
            pmcid = hit.get("pmcid")
            u = hit.get("pdfUrl") or (f"https://europepmc.org/articles/{pmcid}?pdf=render"
                                      if pmcid else "")
            if u:
                try:
                    if save(dest, c.get(u, timeout=60).content):
                        return "europepmc"
                except Exception:
                    pass
    except Exception:
        pass
    return ""


SOURCES = [src_unpaywall, src_arxiv, src_epmc, src_scihub]


def cmd_fetch(a) -> int:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    failed = set()
    if not a.retry_failed:
        # 汇总历史所有抓取日志，避免重复重试已知拿不到的条目
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
    todo = [x for x in plan if not (FETCHED / f"{x['k']}.pdf").exists()
            and x["k"] not in failed]
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        say("没有要抓的（都已存在或已标记失败；用 --retry-failed 重试失败的）")
        return 0
    say(f"待抓 {len(todo)}")
    ok = bad = 0
    with httpx.Client(trust_env=False, follow_redirects=True, headers=UA) as c:
        for i, x in enumerate(todo, 1):
            dest = FETCHED / f"{x['k']}.pdf"
            got = ""
            for fn in SOURCES:
                got = fn(c, x["doi"], dest)
                if got:
                    break
            if got:
                ok += 1
                log_fetch(k=x["k"], doi=x["doi"], ok=True, via=got,
                          size=dest.stat().st_size)
                say(f"  [{i}/{len(todo)}] OK {x['k']} via {got}")
            else:
                bad += 1
                log_fetch(k=x["k"], doi=x["doi"], ok=False)
                say(f"  [{i}/{len(todo)}] 失败 {x['k']} {x['doi'][:40]}")
            time.sleep(1.5)
    say(f"\n成功 {ok} / 失败 {bad}")
    return 0


# ---------------------------------------------------------------- attach

def new_key(c: sqlite3.Connection) -> str:
    import random
    chars = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
    while True:
        k = "".join(random.choice(chars) for _ in range(8))
        if not c.execute("SELECT 1 FROM items WHERE key=?", (k,)).fetchone():
            return k


def safe_name(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return (s[:80] or "fulltext") + ".pdf"


def cmd_attach(a) -> int:
    if zotero_running():
        say("Zotero 正在运行。请从菜单 File -> Quit 正常退出后再跑 attach。")
        say("（不要强杀：强杀会触发库重建并回滚已导入的条目。）")
        return 2
    if not DB.exists():
        say(f"找不到数据库：{DB}")
        return 1

    if a.backup:
        dst = ROOT / "backups" / (f"zotero-{dt.datetime.now():%Y%m%d-%H%M%S}-pdffill.sqlite")
        dst.parent.mkdir(exist_ok=True)
        shutil.copy2(DB, dst)
        say(f"备份 -> {dst.name}")

    # WAL 检查点，否则残留 -wal 让后续判断出错
    try:
        wc = sqlite3.connect(DB, timeout=5)
        wc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wc.close()
    except sqlite3.OperationalError as e:
        say(f"WAL checkpoint 失败：{e}")
        return 1

    c = sqlite3.connect(DB)
    c.isolation_level = None
    c.row_factory = sqlite3.Row
    have = {r[0] for r in c.execute(
        "SELECT i.key FROM itemAttachments ia JOIN items i ON i.itemID=ia.parentItemID")}
    keys = {r[0] for r in c.execute("SELECT key FROM items")}
    plan = json.loads(PLAN.read_text(encoding="utf-8")) if PLAN.exists() else []
    title_by_key = {x["k"]: x.get("title", "") for x in plan}

    todo = [p for p in sorted(FETCHED.glob("*.pdf"))
            if p.stem in keys and p.stem not in have]
    if not todo:
        say("没有待挂的 PDF")
        return 0
    say(f"待挂 {len(todo)}")

    ok = bad = 0
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for p in todo:
        key = p.stem
        parent = c.execute("SELECT itemID, libraryID FROM items WHERE key=?", (key,)).fetchone()
        if not parent:
            bad += 1
            continue
        title = title_by_key.get(key) or p.stem
        fname = safe_name(title)
        if c.execute("SELECT 1 FROM itemAttachments WHERE parentItemID=? AND path=?",
                     (parent["itemID"], f"storage:{fname}")).fetchone():
            continue
        nk = new_key(c)
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "INSERT INTO items (itemTypeID,dateAdded,dateModified,clientDateModified,"
                "libraryID,key,version,synced) VALUES (3,?,?,?,?,?,0,0)",
                (now, now, now, parent["libraryID"], nk))
            iid = cur.lastrowid
            c.execute(
                "INSERT INTO itemAttachments (itemID,parentItemID,linkMode,contentType,"
                "charsetID,path,syncState,storageModTime,storageHash,"
                "lastProcessedModificationTime,lastRead) VALUES (?,?,0,'application/pdf',"
                "NULL,?,0,NULL,NULL,?,NULL)",
                (iid, parent["itemID"], f"storage:{fname}", int(p.stat().st_mtime)))
            vid = c.execute("SELECT valueID FROM itemDataValues WHERE value=?", (title,)).fetchone()
            value_id = vid[0] if vid else c.execute(
                "INSERT INTO itemDataValues (value) VALUES (?)", (title,)).lastrowid
            c.execute("INSERT INTO itemData (itemID,fieldID,valueID) VALUES (?,1,?)",
                      (iid, value_id))
            d = STORAGE / nk
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, d / fname)
            c.execute("COMMIT")
            ok += 1
        except Exception as e:
            c.execute("ROLLBACK")
            bad += 1
            say(f"  失败 {key}: {type(e).__name__}: {e}")
    c.close()

    chk = sqlite3.connect(DB)
    integ = chk.execute("PRAGMA integrity_check").fetchone()[0]
    fk = len(chk.execute("PRAGMA foreign_key_check").fetchall())
    total = chk.execute("SELECT COUNT(*) FROM itemAttachments").fetchone()[0]
    chk.close()
    say(f"挂回 {ok} 条，失败 {bad} 条 | integrity={integ} 外键问题={fk} 附件总数={total}")
    return 0 if bad == 0 else 1


# ---------------------------------------------------------------- report

def cmd_report(a) -> int:
    c = ro_conn()
    rows = c.execute("""
      SELECT i.key,
        (SELECT COUNT(*) FROM itemAttachments ia
          WHERE ia.parentItemID=i.itemID AND ia.contentType='application/pdf') n
      FROM items i JOIN itemTypes t ON t.itemTypeID=i.itemTypeID
      WHERE t.typeName NOT IN ('attachment','note','annotation')
    """).fetchall()
    total = len(rows)
    has = sum(1 for r in rows if r[1] > 0)
    fetched = len(list(FETCHED.glob("*.pdf")))
    say(f"顶层条目 {total} | 已有 PDF {has} | 缺 {total-has} | 覆盖率 {has*100//max(total,1)}%")
    say(f"本地底档 fetched/ {fetched} 个")
    if PLAN.exists():
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        got = sum(1 for x in plan if (FETCHED / f"{x['k']}.pdf").exists())
        say(f"当前清单 {len(plan)} 条，已抓 {got}，待抓 {len(plan)-got}")
    return 0


# ---------------------------------------------------------------- run

def cmd_run(a) -> int:
    ns = argparse.Namespace(collection=a.collection, all=True)
    if cmd_scan(ns) != 0:
        return 1
    fns = argparse.Namespace(limit=a.limit, retry_failed=a.retry_failed)
    cmd_fetch(fns)
    if zotero_running():
        say("\n抓取完成，但 Zotero 正在运行 —— 请正常退出后再执行：")
        say("  python logs/pdffill.py attach --backup")
        return 0
    return cmd_attach(argparse.Namespace(backup=True))


def main() -> int:
    ap = argparse.ArgumentParser(description="Zotero 缺 PDF 补齐统一入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="盘点缺 PDF 的条目")
    s.add_argument("--collection", default="")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_scan)

    f = sub.add_parser("fetch", help="多源抓取")
    f.add_argument("--limit", type=int, default=0)
    f.add_argument("--retry-failed", action="store_true")
    f.set_defaults(fn=cmd_fetch)

    at = sub.add_parser("attach", help="挂回 Zotero（需 Zotero 退出）")
    at.add_argument("--backup", action="store_true")
    at.set_defaults(fn=cmd_attach)

    r = sub.add_parser("report", help="覆盖统计")
    r.set_defaults(fn=cmd_report)

    rn = sub.add_parser("run", help="一条龙")
    rn.add_argument("--collection", default="")
    rn.add_argument("--limit", type=int, default=0)
    rn.add_argument("--retry-failed", action="store_true")
    rn.set_defaults(fn=cmd_run)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
