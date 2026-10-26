# 读链接：把「甩个链接」变成她真读过的内容

## 为什么要做

在这之前她"看到链接"只有两条路：BROWSE 截图（图里的字她读不到）、
或者搜索的佐证 fetch_page_excerpt（限 500 字、不跑 JS、模型也调不到）。
群里最常见的场景恰恰是「甩个链接 + 这啥」，结果她只能顺着 URL 里的英文猜。

现在两条路都通：

1. **自动**：消息里出现链接，进模型之前先读一遍，正文直接拼进这一轮的
   用户消息（llm.py 里的 link prefetch）。对方只说了「这啥」也没关系。
2. **手动**：她自己想起来再读，用 <<<READ https://... | 想知道什么>>>。

两条路共用 link_reader.read_link，围栏只有一份。

## 抓取流程

    校验（scheme/内网/白名单）
        ↓
    httpx 手动跟跳转（每一跳都重新校验，防重定向绕过）
        ↓
    按 content-type 分派
        ├ html → trafilatura 抽正文
        ├ pdf  → pymupdf 抽文字
        └ 其它 → 当纯文本
        ↓
    正文 < 300 字？→ Playwright 渲染一次再取 innerText（SPA 兜底）
        ↓
    正文 < 120 字？→ 判定"没读到"（只剩站名不算正文）
        ↓
    超长 → 先摘要（模型压要点）→ 再按首 70% / 尾 30% 截断
        ↓
    写缓存（含摘要）→ 拼成一段提示词文本

## 围栏（这个比抓取本身重要）

她这条链路由**群里任何人**触发，所以：

- **禁内网/回环/链路本地/保留地址**，IP 字面量和 DNS 解析结果都要查
  （防域名解析到 127.0.0.1 那种绕过）。重定向的每一跳都重查一遍。
- **只读**：不带 cookie、不带 header、不 POST、不下载附件、不落原始文件。
- **限制**：2MB、3 跳、20 秒；超了明确失败，不硬撑。
- **白名单可选**：DL_SENPAI_LINK_READ_ALLOWED_DOMAINS 留空=除内网外都行。
- 单测 tests/test_link_reader.py 钉着 11 个内网地址 + 3 种非 http 协议必须被拒。

## 失败怎么呈现

失败不是异常，是 LinkDoc.error，会变成一句提示喂给模型：

    【系统读链接失败】https://...
    原因：超时（对方可能发了需要登录或极慢的站）
    （读不到就当没读到，直接说「这链接我打不开」，不要凭 URL 猜内容。）

这样她说的是「这链接打不开」，而不是编一段看起来像真的总结。

## 依赖

- 必需：httpx、trafilatura（已有）
- SPA 兜底：playwright + chromium（已有）
- PDF：pymupdf（本次新装，1.28.2）

装 pymupdf 时注意：这台机器 PIP_INDEX_URL 指向清华源会 403，
要用阿里云源：

    python -m pip install -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com "pymupdf<2"

## 实测

| 链接 | 结果 |
|---|---|
| https://example.com | 静态 171 字；开浏览器 917 字（含页脚） |
| https://www.xiaoheihe.cn/app/bbs/link/... (SPA) | 静态只剩 13 字站名 → 浏览器渲染 3269 字 ✅ |
| http://127.0.0.1:27315/ | 拒：内网/保留地址 |
| 本地生成 PDF | pymupdf 正常取出文字 |
| 5000 字长文 | 截断保留首尾，尾部关键词仍在 |
