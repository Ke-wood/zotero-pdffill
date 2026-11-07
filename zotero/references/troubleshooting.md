# 排错手册：按现象查

> 按"看到什么"索引，不按"做了什么"索引。

## 抓取失败

### 所有出版商都 403

**第一步：确认是不是代理捣鬼。**

```bash
# 不挂代理
python -c "import httpx; print(httpx.get('https://link.springer.com/content/pdf/10.1007/s00227-024-04569-1.pdf', trust_env=False, timeout=30).status_code)"
# 挂代理对比
python -c "import httpx; print(httpx.get('https://link.springer.com/...', proxy='http://127.0.0.1:7897', trust_env=False, timeout=30).status_code)"
```

Springer/MDPI 实测**挂了代理返回 3KB 拦截页，直连反而 200**。脚本里统一写 `trust_env=False` + 不传 proxy。

### Cloudflare "Just a moment" / "请稍候…"

脚本无解，只能真浏览器。判断依据：返回的是约 800KB 的 HTML，`content-type: text/html`。

```
1. dsh_browser_open 目标 URL
2. dsh_browser_wait（等 CF 自己过，常见 10–20 秒）
3. dsh_browser_challenge → 若报 CHALLENGE 就请人点
4. 过了之后 dsh_browser_execute 里 fetch
```

**验证码反复出现**是正常的：每换一个出版商域名就要重新过一次。一次会话只对当前站有效。

### Sci-Hub 说"该文章不可用"

不是故障。页面 title 会是：
- `Sci-Hub: 该文章在 Sci-Hub 上不可用。`（中文镜像）
- `Sci-Hub: статья отсутствует`（俄文镜像）

**换镜像无用**，这是真没收录。特征：新文章（2024+）、小刊、书籍章节。

### Sci-Hub 页面 200 但抽不到 PDF 链接

检查是不是出了验证：
- title 含 `Verification` / `Checking your browser` → 要人过验证
- title 是正常文章名 → 正则 `citation_pdf_url"\s+content="([^"]+)"` 应该能命中；命中不了就退回 `(?:data|href)\s*=\s*["']([^"']*\.pdf[^"']*)["']`

## 入库失败

### "拒绝写入: 存在 zotero.sqlite-wal"

进程退了但 WAL 没合并。先跑：

```python
import sqlite3
c = sqlite3.connect(r"<库路径>/zotero.sqlite")
print(c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())  # 期望 (0,0,0)
c.close()
```

如果 checkpoint 报 `database is locked`，说明还有 Zotero 进程（含后台的 `zotero.exe`）。

### 写完重开 Zotero，条目全没了

**被 Zotero 的回滚覆盖了。** 根因是没正常退出就写库——Zotero 启动时用内存态重建了库。

恢复：
1. 从 `backups/` 拿最近一份 `.sqlite` 覆盖回去（写前备份就是这个用途）
2. 重新 attach

预防：`pdffill.py attach` 会检查 23119/23140 端口，还在监听就拒绝执行。**别用任务管理器强杀**。

### 附件挂上了但打不开 / 显示"文件缺失"

检查三处是否一致：

```sql
SELECT i.key, ia.linkMode, ia.contentType, ia.path
FROM itemAttachments ia JOIN items i ON i.itemID = ia.itemID
WHERE i.key = '<key>';
```

- `linkMode` 必须 `0`（导入型）。`1` 是链接型，文件不在 storage 里
- `path` 必须是 `storage:<文件名>`，不是绝对路径
- `storage/<key>/<文件名>` 必须真实存在且文件名与 `path` 完全一致（**含中文、空格**）

### integrity_check 报错

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

有孤儿附件（`parentItemID` 指向不存在的条目）就说明写的时候父条目 key 找错了。
用备份恢复比硬修安全。

## 工具限制

### dsh_browser_execute 超时（20000ms）

PDF 端点常要 15–25 秒，硬上限 20 秒。三个绕法，按可靠性排序：

1. **隐藏 iframe**（最通用）：

```javascript
(() => {
  const items = [["KEY1","10.xxxx/aaa"], ["KEY2","10.xxxx/bbb"]];
  let f = document.getElementById('zmframe');
  if (!f) {
    f = document.createElement('iframe');
    f.id = 'zmframe'; f.style.display = 'none';
    (document.body || document.documentElement).appendChild(f);
  }
  const fr = f.contentWindow, fd = f.contentDocument;
  items.forEach(([k, doi]) => {
    fr.fetch("https://publisher/doi/pdf/" + doi, {credentials: "include"})
      .then(r => r.blob())
      .then(b => {
        if (b.size > 20000) {
          const a = fd.createElement('a');
          a.href = URL.createObjectURL(b); a.download = k + '.pdf';
          (fd.body || fd.documentElement).appendChild(a); a.click();
        }
      }).catch(() => {});
  });
  return {launched: items.length};   // 立即返回，不等下载
})()
```

然后 `sleep` 几十秒，再按 DOI 从 `Downloads/` 认领。

2. **dsh_browser_download(url, savePath)** —— 走 CDP，不弹框，一次一条。但它是**页内 fetch**，
   跨域跳转会 `Failed to fetch`。

3. **先导航到 PDF 地址，再页内 fetch(location.href)** —— 对付 Elsevier 的两段跳转
   （文章页 → `pdf.sciencedirectassets.com` 签名地址）。

### 拿到的不是 PDF

检查前 5 字节是不是 `%PDF-`。常见假货：

| 大小 | 内容 | 原因 |
|---|---|---|
| 约 3KB | HTML 拦截页 | 挂了代理 |
| 约 50–100KB | HTML 挑战页 | Cloudflare |
| 约 800KB | HTML 错误页 | 无订阅 / 需要机构登录 |
| 约 200KB HTML | 登录墙 | 书籍章节 |

**别只看状态码**，200 也可能是 HTML。
