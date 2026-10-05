"""
Self-contained, colourful HTML rendering of the design document (docs/DESIGN.html).

The markdown stays the source of truth; this wraps it in a page that renders it client-side with
marked.js and draws the mermaid diagrams (flowchart + sequence diagrams) with mermaid.js, both from
cdnjs. The markdown is embedded as JSON, so no HTML from the model is ever injected unescaped.
"""
from __future__ import annotations

import json

OPENAPI_PATHS = {"spec": "/v3/api-docs", "ui": "/swagger-ui/index.html"}

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js"></script>
<style>
:root{--bg:#f4f7fb;--card:#fff;--ink:#1b2430;--muted:#5b6b7e;--a:#1565c0;--b:#6a1b9a;--c:#2e7d32;--d:#ef6c00}
@media (prefers-color-scheme:dark){:root{--bg:#10151c;--card:#19212b;--ink:#e6edf5;--muted:#9fb0c3}}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,sans-serif}
header{background:linear-gradient(120deg,#1565c0,#6a1b9a 55%,#ef6c00);color:#fff;padding:28px 16px}
header h1{margin:0 auto;max-width:960px;font-size:1.8rem}
header p{margin:6px auto 0;max-width:960px}
header a{color:#fff;font-weight:600;margin-right:14px}
main{max-width:960px;margin:0 auto;padding:16px}
section{background:var(--card);border-radius:12px;padding:4px 20px 14px;margin:16px 0;box-shadow:0 1px 6px rgba(0,0,0,.08);border-left:6px solid var(--a)}
section:nth-of-type(4n+2){border-color:var(--b)}section:nth-of-type(4n+3){border-color:var(--c)}section:nth-of-type(4n){border-color:var(--d)}
h2{margin-top:.9em}
table{border-collapse:collapse;width:100%;margin:10px 0;display:block;overflow-x:auto}
th{background:linear-gradient(90deg,#e3f2fd,#f3e5f5);color:#1b2430;text-align:left}
th,td{border:1px solid rgba(128,128,128,.3);padding:6px 10px}
blockquote{margin:10px 0;padding:8px 14px;border-left:5px solid var(--d);background:rgba(239,108,0,.1);border-radius:6px}
code{background:rgba(128,128,128,.18);padding:1px 5px;border-radius:4px}
pre{background:#0f1720;color:#e6edf5;padding:12px;border-radius:8px;overflow-x:auto}
.mermaid{background:#fff;border-radius:8px;padding:10px;overflow-x:auto;text-align:center}
</style></head><body>
<header><h1>__TITLE__</h1>
<p>__LINKS__</p></header>
<main id="doc"></main>
<script>
const md = __MARKDOWN__;
const html = marked.parse(md, {mangle:false, headerIds:false});
const tmp = document.createElement('div'); tmp.innerHTML = html;
tmp.querySelectorAll('pre > code.language-mermaid').forEach(c => {
  const d = document.createElement('div'); d.className = 'mermaid'; d.textContent = c.textContent; c.parentElement.replaceWith(d);
});
const main = document.getElementById('doc'); let sec = null;
[...tmp.childNodes].forEach(n => {
  if (n.nodeName === 'H2') { sec = document.createElement('section'); main.appendChild(sec); }
  (sec || main).appendChild(n);
});
mermaid.initialize({startOnLoad:false, theme:'default', securityLevel:'strict'});
mermaid.run({querySelector:'.mermaid'}).catch(e => console.warn('mermaid', e));
</script></body></html>
"""


def render_design_html(markdown: str, title: str = "URL Shortener - Design Document") -> str:
    links = (f'📘 Live OpenAPI spec once running: <code>{OPENAPI_PATHS["spec"]}</code> &nbsp; '
             f'🧪 Swagger UI: <code>{OPENAPI_PATHS["ui"]}</code> &nbsp; 📄 Contract file: <code>docs/openapi.yaml</code>')
    md_json = json.dumps(markdown).replace("</", "<\\/")   # keep an embedded "</script>" from ending the script block
    return (_TEMPLATE.replace("__TITLE__", title).replace("__LINKS__", links).replace("__MARKDOWN__", md_json))
