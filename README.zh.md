# zotero-pdffill — Zotero 缺 PDF 批量补齐工具包

> English: [README.md](README.md)

给 Zotero 库里缺 PDF 的条目批量补全文并挂回附件。**随取随用，不用安装。**
核心事实：**抓 PDF 和入库是两件事**——抓取不需要 Zotero，入库必须 Zotero 完全退出。

---

## 快速开始

```bash
cd zotero-pdffill
pdffill.bat scan                     # 盘点：哪些条目没 PDF
pdffill.bat fast --workers 14        # 并发抓（约 3 篇/秒）
pdffill.bat attach --backup          # 挂回（先用菜单退出 Zotero）
pdffill.bat report                   # 覆盖率
```

只补某个分类：

```bash
pdffill.bat scan --collection "生态学作业2"
```

想让 `fetched/` 和 `logs/` 落在已有的库目录而不是工具包的 `work\`：

```bash
set PDFFILL_ROOT=D:\plan\zotero
pdffill.bat scan
```

Linux/macOS 直接调脚本：

```bash
export PDFFILL_ROOT="$PWD"
python scripts/pdffill.py scan
python scripts/pdffill_fast.py --workers 14
python scripts/pdffill.py attach --backup
```

---

## 命令一览

| 命令 | 干什么 | 要关 Zotero 吗 |
|---|---|---|
| `scan` | 查库，列出缺 PDF 的条目，生成清单 | 不用 |
| `fetch` | 串行抓取（稳） | 不用 |
| `fast` | 并发抓取（**快 60 倍，首选**） | 不用 |
| `attach` | 把抓到的 PDF 挂回 Zotero | **必须关** |
| `report` | 覆盖率统计 | 不用 |
| `run` | scan + fetch + attach 一条龙 | 需关闭 |

`fast` 参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--workers` | 12 | 并发数，14 左右较稳 |
| `--quick` | 开 | 只试收录最全的镜像，**这是快的原因** |
| `--retry-failed` | 关 | 连已知失败的也重试（实测 327 条只多捞 1 条） |
| `--limit` | 0 | 只跑前 N 条，调试用 |

---

## 抓取源（按顺序试，命中即停）

1. **Unpaywall** — 官方 OA 副本，最干净
2. **arXiv** — DOI 含 `arxiv` 直接取
3. **EuropePMC** — 生物医学 PMC 副本
4. **Sci-Hub 多镜像** — 主力，实测命中率约 65%

### Sci-Hub 镜像实测（2026-11）

| 镜像 | 状态 |
|---|---|
| `sci-hub.box` | ✅ 最好，收录最全（脚本优先） |
| `sci-hub.ru` | ✅ 可用 |
| `www.sci-hub.ren` | ✅ 可用 |
| `sci-hub.wf` | ⚠️ 常出 "Checking your browser" |
| `scihub.help` | ⚠️ 多为 404 |
| `sci-hub.se` | ❌ 连不上 |
| `sci-hub.st` | ❌ 403 |

页面写「该文章在 Sci-Hub 上不可用」= **真没收录**，换镜像无用。

---

## 出版社直链速查

| 出版商 | 直链 | 备注 |
|---|---|---|
| Springer/BMC | `link.springer.com/content/pdf/<DOI>.pdf` | **不能挂代理**，挂了返回 3KB 拦截页 |
| MDPI | `mdpi-res.com/d_attachment/<刊>/<刊>-<卷02d>-<文05d>/article_deploy/<同名>.pdf` | www 站被 Akamai 挡，CDN 不挡 |
| Wiley | `onlinelibrary.wiley.com/doi/pdfdirect/<DOI>` | 近年可用，2010 前大量 403 |
| ACS | `pubs.acs.org/doi/pdf/<DOI>` | 需机构登录 |
| PNAS | `www.pnas.org/doi/pdf/<DOI>` | |
| Nature | `www.nature.com/articles/<DOI 去掉 10.1038/>.pdf` | |
| Elsevier | 无直链 | 必须从文章页取带 md5 token 的 `pdfft` 链接 |

### 官网免费渠道

**OECD / WHO / FAO / UNEP / EPA / EFSA / ECHA / 各学会** 有公开 PDF，走官网 CDN，
**不吃 Cloudflare**：

```
oecd.org/content/dam/oecd/en/publications/reports/<年>/<月>/<完整slug>_<hash>/<DOI后缀>.pdf
```

路径拼不出来，要用浏览器打开落地页取一次「Download PDF」链接。OECD 实测 5/5 全中。
**但商业出版社的书（Elsevier/Springer/CRC 书籍章节）没有这条路**——官网就是付费墙。

---

## 入库机制

Zotero 的 `zotero-write-endpoint` 插件（23119）**不支持导入本地文件**，所以直写 sqlite：

```
items          (itemTypeID=3, 8位大写key, libraryID=1, version=0, synced=0)
itemAttachments(parentItemID, linkMode=0, contentType='application/pdf',
                path='storage:<文件名>', syncState=0,
                storageModTime / storageHash 可 NULL,
                lastProcessedModificationTime = 文件 mtime)
itemData       (fieldID=1 的 title；值进 itemDataValues，不是 values 表)
storage/<新key>/<文件名>
```

`attach` 会自动：自检 Zotero 已退出 → 整库备份 → WAL checkpoint → 逐条事务写入 → 完整性校验。

---

## 坑清单（每条都真踩过）

1. **强杀 Zotero 会回滚库** — 实测强杀后库从 178MB 重建为 23MB，一晚导入的 142 条全丢。
   **必须菜单 File → Quit。** `attach` 发现进程还在就拒绝执行。

2. **WAL 残留导致拒写** — 进程退干净了 `zotero.sqlite-wal` 可能还在，写前必须 checkpoint。

3. **浏览器工具 20 秒硬超时** — PDF 端点常要 15–25 秒。绕法：隐藏 iframe 隔离上下文。

4. **`dsh_browser_download` 本质是页内 fetch** — 跨域跳转会失败，必须先导航到 PDF 地址。

5. **Cloudflare 认浏览器指纹** — 脚本全 403，机构 IP 只在真浏览器生效，且每个域名要人过验证。

6. **代理帮倒忙** — Springer/MDPI 挂代理反而被拦，直连才对。脚本统一 `trust_env=False`。

7. **直写不建全文索引** — PDF 能打开，但 Zotero 内「全文搜索」搜不到新附件内容。

---

## 文档

| 文件 | 内容 |
|---|---|
| [`references/troubleshooting.md`](references/troubleshooting.md) | 按现象排错 |
| [`references/field-data.md`](references/field-data.md) | 实测命中率、按出版商战果、速度基准 |

## 要求

- Python 3.10+ / `httpx`
- `pdftotext`（Poppler），用于校验 PDF 真伪
- Zotero（开发时针对 10.0.7）

库路径自动探测：`ZOTERO_DB_PATH` → prefs.js 的 `extensions.zotero.dataDir` → `~/Zotero/zotero.sqlite`。

## 免责声明

本工具可能从法律地位因地区而异的数据源获取论文，并直接写入 Zotero 数据库。
**请备份文献库，仅用于你有权访问的内容。** 作者不承担误用责任。

## 许可

MIT，见 [LICENSE](LICENSE)。
