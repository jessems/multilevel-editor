#!/usr/bin/env python3
"""Render a nested-bullet outline .md (e.g. a reverse-outline skeleton) as an
HTML page with collapsible bullets — optionally served with in-browser editing
and drag-reordering, synced to the .md when the user presses Save.

Paragraph numbering is a UI affordance, not file content: bullets that sit
directly under a heading bullet (and are not themselves headings or bracketed
placeholders) get a sequential 'para. N' chip computed at render time — the
.md never contains number labels, so reordering just renumbers the display.

Usage:
    python outline_to_html.py <outline.md> [-o out.html]   # static export
    python outline_to_html.py <outline.md> --serve [--port 8383]

Static mode writes <outline>.html beside the source (read-only view).
Serve mode starts a local server and opens the browser. Edits are STAGED in
the page, not written immediately: click a bullet to edit its text (Enter or
click away stages it; Esc cancels), drag the hover grip to move a bullet with
its whole subtree (number chips recompute as you go), or press the Markdown
button to edit the raw outline as text (add/delete/bulk-edit bullets) and
toggle back. Any staged change activates the Save button in the header;
pressing Save writes the whole outline back to the .md (frontmatter
preserved). If the file changed on disk since the page was loaded, Save is
refused (409) rather than clobbering. Closing the tab with unsaved changes
prompts a warning.

Self-contained: stdlib only, inline CSS/JS, no network.
"""

import argparse
import hashlib
import html
import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BULLET_RE = re.compile(r"^(\s*)- (.*)$")


def parse_outline(text: str):
    """Parse '- ' bullets (2-space indents) into a tree.

    Continuation lines are joined into the bullet's raw text (a save
    normalises them to a single line)."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    root = {"indent": -1, "children": []}
    stack = [root]
    for idx in range(start, len(lines)):
        line = lines[idx]
        m = BULLET_RE.match(line)
        if not m:
            if line.strip() and stack[-1] is not root:
                stack[-1]["raw"] += " " + line.strip()
            continue
        indent, content = m.groups()
        node = {"indent": len(indent) // 2, "raw": content.strip(), "children": []}
        while stack[-1]["indent"] >= node["indent"]:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)
    return root["children"]


def assign_numbers(tree) -> None:
    """UI paragraph numbering: a bullet gets a sequential number when its
    parent is a heading bullet and it is neither a heading nor a bracketed
    placeholder. Numbers are display chrome — never written to the file."""
    counter = 0

    def walk(nodes, parent_is_heading):
        nonlocal counter
        for n in nodes:
            is_heading = n["raw"].startswith("#")
            n["num"] = None
            if parent_is_heading and not is_heading and not n["raw"].startswith("["):
                counter += 1
                n["num"] = counter
            walk(n["children"], is_heading)

    walk(tree, True)


def render_inline(text: str):
    """Escape HTML, then apply minimal markdown inline styling."""
    heading = 0
    m = re.match(r"^(#{1,6})\s+(.*)$", text)
    if m:
        heading = len(m.group(1))
        text = m.group(2)
    t = html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*([^*]+?)\*(?![\w*])", r"<em>\1</em>", t)
    t = re.sub(r"`([^`]+?)`", r"<code>\1</code>", t)
    t = re.sub(r"^(\((?:[^)]{0,40})\))", r'<span class="tag">\1</span>', t)
    return t, heading


def render_node(node) -> str:
    text, heading = render_inline(node["raw"])
    cls = f"h{heading}" if heading else "item"
    numbered = bool(node.get("num"))
    chip = f'<span class="pnum">{node["num"]}</span>' if numbered else ""
    row_cls = "row numbered" if numbered else "row"
    grip = '<span class="grip" draggable="true" title="drag to move">⋮⋮</span>'
    span = (
        f'{chip}<span class="txt {cls}" '
        f'data-raw="{html.escape(node["raw"], quote=True)}">{text}</span>'
    )
    if node["children"]:
        kids = "\n".join(render_node(c) for c in node["children"])
        return (
            f'<li class="branch open"><div class="{row_cls}">{grip}<button class="caret" '
            f'aria-label="toggle"></button>{span}</div><ul>{kids}</ul></li>'
        )
    return f'<li class="leaf"><div class="{row_cls}">{grip}<span class="dot"></span>{span}</div></li>'


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --line:#d8d3c8; --ink:#2b2a27; --mut:#8a8578; --acc:#7a5c2e; --bg:#faf8f4; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.55 Georgia, 'Times New Roman', serif; }}
  header {{ position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line);
            padding:10px 24px; display:flex; gap:12px; align-items:baseline; z-index:2; }}
  header h1 {{ font-size:15px; margin:0; font-weight:600; }}
  header .src {{ color:var(--mut); font-size:12px; }}
  header .hint {{ color:var(--mut); font-size:12px; font-style:italic; }}
  header button {{ font:12px/1 -apple-system, sans-serif; padding:4px 10px; cursor:pointer;
                   background:#fff; border:1px solid var(--line); border-radius:4px; }}
  #saveBtn {{ margin-left:auto; padding:5px 16px; font-weight:600; }}
  #saveBtn:disabled {{ opacity:.45; cursor:default; }}
  #saveBtn.dirty {{ background:var(--acc); color:#fff; border-color:var(--acc); }}
  body.readonly #saveBtn, body.readonly #mdBtn {{ display:none; }}
  #status {{ font:12px -apple-system, sans-serif; color:var(--mut); min-width:60px; }}
  #status.saved {{ color:#3d7a3d; }}
  #status.error {{ color:#a33; }}
  main {{ max-width:960px; margin:0 auto; padding:18px 24px 80px; }}
  #mdview {{ display:none; width:100%; min-height:calc(100vh - 140px); box-sizing:border-box;
             font:13px/1.6 ui-monospace, Menlo, monospace; color:var(--ink);
             background:#fffdf8; border:1px solid var(--line); border-radius:6px;
             padding:14px 16px; resize:vertical; white-space:pre; overflow-x:auto; }}
  body.mdmode #tree {{ display:none; }}
  body.mdmode #mdview {{ display:block; }}
  ul {{ list-style:none; margin:0; padding:0 0 0 22px; border-left:1px solid var(--line); }}
  main > ul {{ border-left:none; padding-left:0; }}
  li {{ margin:2px 0; }}
  .row {{ display:flex; align-items:baseline; gap:6px; }}
  .caret {{ flex:0 0 auto; width:18px; height:18px; border:none; background:none; cursor:pointer;
            position:relative; top:2px; padding:0; }}
  .caret::before {{ content:''; display:block; margin:4px auto; width:0; height:0;
                    border-left:6px solid var(--acc); border-top:5px solid transparent;
                    border-bottom:5px solid transparent; transition:transform .12s; }}
  li.branch.open > .row .caret::before {{ transform:rotate(90deg); }}
  li.branch:not(.open) > ul {{ display:none; }}
  .dot {{ flex:0 0 auto; width:18px; text-align:center; color:var(--mut); }}
  .dot::before {{ content:'·'; }}
  .row.numbered > .dot {{ display:none; }}
  .h1 {{ font-size:19px; font-weight:700; }}
  .h2 {{ font-size:16px; font-weight:700; }}
  .h3 {{ font-size:15px; font-weight:600; font-style:italic; }}
  .pnum {{ flex:0 0 auto; min-width:14px; text-align:center; color:var(--mut);
           font:10.5px/1.6 -apple-system, sans-serif; background:#efe9dd;
           border-radius:8px; padding:0 5px; white-space:nowrap;
           position:relative; top:-1px; }}
  .tag {{ color:var(--mut); font-style:italic; }}
  code {{ font:13px ui-monospace, monospace; background:#f0ece3; padding:0 3px; border-radius:3px; }}
  .row:hover {{ background:#f3efe7; border-radius:3px; }}
  .txt.editing {{ background:#fffdf4; outline:1px solid var(--acc); border-radius:3px;
                  padding:0 3px; cursor:text; }}
  .grip {{ flex:0 0 auto; width:20px; margin-left:-24px; opacity:0; cursor:grab;
           color:var(--acc); font:15px/1.35 sans-serif; font-weight:700;
           letter-spacing:-2px; user-select:none; text-align:center;
           border-radius:4px; transition:opacity .1s; }}
  .row:hover > .grip {{ opacity:1; }}
  .grip:hover {{ background:#ece3d0; }}
  .grip:active {{ cursor:grabbing; }}
  body.readonly .grip {{ display:none; }}
  li.dragging {{ opacity:.35; }}
  .row.drop-before {{ box-shadow:0 -2px 0 var(--acc); }}
  .row.drop-after {{ box-shadow:0 2px 0 var(--acc); }}
</style>
</head>
<body class="{bodycls}">
<header>
  <h1>{title}</h1>
  <span class="src">{source}</span>
  <button onclick="setAll(true)">Expand all</button>
  <button onclick="setAll(false)">Collapse all</button>
  <button id="mdBtn">Markdown</button>
  <span class="hint">{hint}</span>
  <span id="status"></span>
  <button id="saveBtn" disabled>Save</button>
</header>
<main>
<ul id="tree">{tree}</ul>
<textarea id="mdview" spellcheck="false"></textarea>
</main>
<script>
  const EDITABLE = {editable};
  const FILEHASH = "{filehash}";
  const tree = document.getElementById('tree');
  const mdview = document.getElementById('mdview');
  const mdBtn = document.getElementById('mdBtn');
  const status = document.getElementById('status');
  const saveBtn = document.getElementById('saveBtn');

  function setAll(open) {{
    document.querySelectorAll('li.branch').forEach(li => li.classList.toggle('open', open));
  }}
  function flash(msg, cls) {{
    status.textContent = msg; status.className = cls || '';
    if (msg) setTimeout(() => {{ status.textContent = ''; status.className = ''; }}, 2500);
  }}
  let dirty = false;
  function markDirty() {{
    dirty = true;
    saveBtn.disabled = false;
    saveBtn.classList.add('dirty');
  }}
  window.addEventListener('beforeunload', e => {{ if (dirty) e.preventDefault(); }});

  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  // client-side mirror of the server's render_inline
  function renderMd(t) {{
    let heading = 0;
    const hm = t.match(/^(#{{1,6}})\\s+(.*)$/);
    if (hm) {{ heading = hm[1].length; t = hm[2]; }}
    let s = t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    s = s.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
    s = s.replace(/(?<![\\w*])\\*([^*]+?)\\*(?![\\w*])/g, '<em>$1</em>');
    s = s.replace(/`([^`]+?)`/g, '<code>$1</code>');
    s = s.replace(/^(\\((?:[^)]{{0,40}})\\))/, '<span class="tag">$1</span>');
    return {{ html: s, cls: heading ? 'h' + heading : 'item' }};
  }}

  // ---- serialization: DOM -> bullets, bullets -> markdown/DOM ----
  function serialize() {{
    const bullets = [];
    (function walk(ul, depth) {{
      [...ul.children].forEach(li => {{
        const span = li.querySelector(':scope > .row .txt');
        if (span) bullets.push({{ indent: depth, raw: span.dataset.raw }});
        const sub = li.querySelector(':scope > ul');
        if (sub) walk(sub, depth + 1);
      }});
    }})(tree, 0);
    return bullets;
  }}
  function toMarkdown(bullets) {{
    return bullets.map(b => '  '.repeat(b.indent) + '- ' + b.raw).join('\\n');
  }}
  function parseMarkdown(text) {{
    const bullets = [];
    text.split('\\n').forEach(line => {{
      const m = line.match(/^(\\s*)- (.*)$/);
      if (m) bullets.push({{ indent: Math.floor(m[1].length / 2), raw: m[2].trim() }});
      else if (line.trim() && bullets.length) bullets[bullets.length - 1].raw += ' ' + line.trim();
    }});
    return bullets;
  }}
  function buildTree(bullets) {{
    const root = {{ indent: -1, children: [] }};
    const stack = [root];
    bullets.forEach(b => {{
      const node = {{ indent: b.indent, raw: b.raw, children: [] }};
      while (stack[stack.length - 1].indent >= b.indent) stack.pop();
      stack[stack.length - 1].children.push(node);
      stack.push(node);
    }});
    function renderNode(n) {{
      const d = renderMd(n.raw);
      const grip = '<span class="grip" draggable="true" title="drag to move">⋮⋮</span>';
      const span = '<span class="txt ' + d.cls + '" data-raw="' + esc(n.raw) + '">' + d.html + '</span>';
      if (n.children.length) {{
        return '<li class="branch open"><div class="row">' + grip +
               '<button class="caret" aria-label="toggle"></button>' + span + '</div><ul>' +
               n.children.map(renderNode).join('') + '</ul></li>';
      }}
      return '<li class="leaf"><div class="row">' + grip + '<span class="dot"></span>' +
             span + '</div></li>';
    }}
    tree.innerHTML = root.children.map(renderNode).join('');
    renumberChips();
  }}

  // UI paragraph numbering: bullets directly under a heading bullet, not
  // themselves headings or [placeholders], get sequential chips.
  function renumberChips() {{
    let k = 0;
    (function walk(ul, parentHeading) {{
      [...ul.children].forEach(li => {{
        const span = li.querySelector(':scope > .row .txt');
        let isHeading = parentHeading;
        if (span) {{
          const raw = span.dataset.raw;
          isHeading = raw.startsWith('#');
          const prev = span.previousElementSibling;
          let chip = prev && prev.classList.contains('pnum') ? prev : null;
          const numbered = parentHeading && !isHeading && !raw.startsWith('[');
          if (numbered) {{
            k += 1;
            if (!chip) {{
              chip = document.createElement('span');
              chip.className = 'pnum';
              span.before(chip);
            }}
            chip.textContent = k;
          }} else if (chip) {{
            chip.remove();
          }}
          span.closest('.row').classList.toggle('numbered', numbered);
        }}
        const sub = li.querySelector(':scope > ul');
        if (sub) walk(sub, isHeading);
      }});
    }})(tree, true);
  }}

  // depth rule: nothing may nest under a non-heading bullet — paragraphs are
  // the deepest level an operation may create
  function depthOk(parentUl) {{
    if (parentUl === tree) return true;
    const pli = parentUl.closest('li');
    const pspan = pli && pli.querySelector(':scope > .row .txt');
    return !!(pspan && pspan.dataset.raw.startsWith('#'));
  }}

  // ---- branch/leaf conversion helpers (for indent/outdent) ----
  function toBranch(li) {{
    if (li.classList.contains('branch')) return;
    li.classList.remove('leaf'); li.classList.add('branch', 'open');
    const dot = li.querySelector(':scope > .row .dot');
    if (dot) {{
      const btn = document.createElement('button');
      btn.className = 'caret'; btn.setAttribute('aria-label', 'toggle');
      dot.replaceWith(btn);
    }}
    if (!li.querySelector(':scope > ul')) li.appendChild(document.createElement('ul'));
  }}
  function toLeafIfEmpty(li) {{
    const ul = li.querySelector(':scope > ul');
    if (!ul || ul.children.length) return;
    ul.remove();
    li.classList.remove('branch', 'open'); li.classList.add('leaf');
    const caret = li.querySelector(':scope > .row .caret');
    if (caret) {{
      const dot = document.createElement('span');
      dot.className = 'dot';
      caret.replaceWith(dot);
    }}
  }}

  // ---- markdown view toggle ----
  let mdBaseline = '';
  mdBtn.addEventListener('click', () => {{
    if (!EDITABLE) return;
    if (!document.body.classList.contains('mdmode')) {{
      if (document.activeElement && document.activeElement.isContentEditable)
        document.activeElement.blur();               // stage a pending inline edit first
      mdBaseline = toMarkdown(serialize());
      mdview.value = mdBaseline;
      document.body.classList.add('mdmode');
      mdBtn.textContent = 'Outline';
    }} else {{
      if (mdview.value !== mdBaseline) {{
        const bullets = parseMarkdown(mdview.value);
        if (!bullets.length) {{ flash('no bullets found — fix the markdown first', 'error'); return; }}
        buildTree(bullets);
        markDirty();
      }}
      document.body.classList.remove('mdmode');
      mdBtn.textContent = 'Markdown';
    }}
  }});
  mdview.addEventListener('input', markDirty);

  // ---- delegated events (survive tree rebuilds) ----
  tree.addEventListener('click', e => {{
    const caret = e.target.closest('.caret');
    if (caret) {{ caret.closest('li').classList.toggle('open'); return; }}
    const span = e.target.closest('span.txt');
    if (span) startEdit(span, e);
  }});

  function startEdit(span, e) {{
    e.preventDefault();
    if (!EDITABLE) {{ flash('read-only export — run with --serve to edit', 'error'); return; }}
    if (span.isContentEditable) return;
    const prev = span.innerHTML;
    span.textContent = span.dataset.raw;
    span.classList.add('editing');
    try {{ span.contentEditable = 'plaintext-only'; }} catch {{ span.contentEditable = 'true'; }}
    span.focus();
    const range = document.createRange();
    range.selectNodeContents(span); range.collapse(false);
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
    let done = false;
    let suppressBlur = false;
    const cleanup = () => {{ done = true; span.contentEditable = 'false';
      span.classList.remove('editing');
      span.removeEventListener('keydown', onKey); span.removeEventListener('blur', onBlur); }};
    const cancel = () => {{ cleanup(); span.innerHTML = prev; }};
    const stage = () => {{
      if (done) return;
      const val = span.textContent.replace(/\\n+/g, ' ').trim();
      if (!val || val === span.dataset.raw) {{ cancel(); return; }}
      span.dataset.raw = val;
      cleanup();
      const d = renderMd(val);
      span.innerHTML = d.html;
      span.className = 'txt ' + d.cls;
      renumberChips();                              // heading-ness may have changed
      markDirty();
    }};
    const indentOutdent = out => {{
      const li = span.closest('li');
      const sel = getSelection();
      const off = sel.rangeCount ? sel.getRangeAt(0).startOffset : span.textContent.length;
      suppressBlur = true;
      let moved = false;
      if (!out) {{
        const prevLi = li.previousElementSibling;
        const prevSpan = prevLi && prevLi.querySelector(':scope > .row .txt');
        if (!prevLi) {{ flash('cannot indent further', 'error'); }}
        else if (!prevSpan.dataset.raw.startsWith('#')) {{
          flash('no indentation beyond paragraph level', 'error');
        }}
        else {{
          toBranch(prevLi);
          prevLi.querySelector(':scope > ul').appendChild(li);
          moved = true;
        }}
      }} else {{
        const parentUl = li.parentElement;
        if (parentUl === tree) {{ flash('cannot outdent further', 'error'); }}
        else {{
          const parentLi = parentUl.closest('li');
          parentLi.after(li);
          toLeafIfEmpty(parentLi);
          moved = true;
        }}
      }}
      if (moved) {{ renumberChips(); markDirty(); }}
      span.focus();
      if (span.firstChild) {{
        const r = document.createRange();
        const pos = Math.min(off, span.firstChild.length);
        r.setStart(span.firstChild, pos); r.collapse(true);
        sel.removeAllRanges(); sel.addRange(r);
      }}
      suppressBlur = false;
    }};
    const onKey = ev => {{
      if (ev.key === 'Enter') {{ ev.preventDefault(); stage(); }}
      if (ev.key === 'Escape') {{ ev.preventDefault(); cancel(); }}
      if (ev.key === 'Tab') {{ ev.preventDefault(); indentOutdent(ev.shiftKey); }}
    }};
    const onBlur = () => {{ if (!suppressBlur) stage(); }};
    span.addEventListener('keydown', onKey);
    span.addEventListener('blur', onBlur);
  }}

  // drag-to-move — pure DOM; chips recompute; synced on Save
  let dragLi = null;
  const clearMarks = () => tree.querySelectorAll('.drop-before,.drop-after')
    .forEach(r => r.classList.remove('drop-before', 'drop-after'));
  tree.addEventListener('dragstart', e => {{
    const g = e.target.closest('.grip');
    if (!g || !EDITABLE) {{ e.preventDefault(); return; }}
    dragLi = g.closest('li');
    dragLi.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', '');
  }});
  tree.addEventListener('dragend', () => {{
    if (dragLi) dragLi.classList.remove('dragging');
    dragLi = null; clearMarks();
  }});
  tree.addEventListener('dragover', e => {{
    const row = e.target.closest('.row');
    if (!row || !dragLi) return;
    const li = row.closest('li');
    if (li === dragLi || dragLi.contains(li)) return;
    if (!depthOk(li.parentElement)) return;   // would nest under a paragraph
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearMarks();
    const r = row.getBoundingClientRect();
    row.classList.add(e.clientY < r.top + r.height / 2 ? 'drop-before' : 'drop-after');
  }});
  tree.addEventListener('drop', e => {{
    const row = e.target.closest('.row');
    if (!row || !dragLi) return;
    const li = row.closest('li');
    if (li === dragLi || dragLi.contains(li)) return;
    if (!depthOk(li.parentElement)) {{ flash('no indentation beyond paragraph level', 'error'); return; }}
    e.preventDefault();
    const where = row.classList.contains('drop-before') ? 'before' : 'after';
    clearMarks();
    if (where === 'before') li.before(dragLi); else li.after(dragLi);
    renumberChips();
    markDirty();
  }});

  // Save — serialize current view and write the whole file
  saveBtn.addEventListener('click', async () => {{
    let bullets;
    if (document.body.classList.contains('mdmode')) {{
      bullets = parseMarkdown(mdview.value);
      if (!bullets.length) {{ flash('no bullets found — fix the markdown first', 'error'); return; }}
    }} else {{
      if (document.activeElement && document.activeElement.isContentEditable)
        document.activeElement.blur();               // stage a pending inline edit first
      bullets = serialize();
    }}
    try {{
      const r = await fetch('/save', {{ method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ hash: FILEHASH, bullets }}) }});
      if (!r.ok) throw new Error((await r.json()).error || r.status);
      dirty = false;
      location.reload();
    }} catch (err) {{ flash('save failed: ' + err.message, 'error'); }}
  }});
</script>
</body>
</html>
"""


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_page(source: Path, editable: bool) -> str:
    text = source.read_text(encoding="utf-8")
    tree = parse_outline(text)
    if not tree:
        sys.exit(f"error: no '- ' bullets found in {source}")
    assign_numbers(tree)
    body = "\n".join(render_node(n) for n in tree)
    return PAGE.format(
        title=source.stem,
        source=source.name,
        tree=body,
        editable="true" if editable else "false",
        bodycls="" if editable else "readonly",
        filehash=file_hash(text),
        hint="click to edit · drag to move · Markdown for raw view · Save writes to the .md"
        if editable else "",
    )


def serve(source: Path, port: int):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, build_page(source, editable=True))
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path != "/save":
                self._send(404, "not found", "text/plain")
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                current = source.read_text(encoding="utf-8")
                if file_hash(current) != req["hash"]:
                    self._send(409, json.dumps(
                        {"error": "file changed on disk — refresh the page (your staged edits will be lost)"}),
                        "application/json")
                    return
                cur_lines = current.splitlines(keepends=True)
                fm = []
                if cur_lines and cur_lines[0].strip() == "---":
                    fm.append(cur_lines[0])
                    for line in cur_lines[1:]:
                        fm.append(line)
                        if line.strip() == "---":
                            break
                bullets = ["  " * b["indent"] + "- " + b["raw"].strip()
                           for b in req["bullets"]]
                out = "".join(fm) + ("\n" if fm else "") + "\n".join(bullets) + "\n"
                source.write_text(out, encoding="utf-8")
                self._send(200, json.dumps({"ok": True}), "application/json")
                print(f"  saved: {len(bullets)} bullets written")
            except Exception as e:  # noqa: BLE001 — report any save failure to the client
                self._send(400, json.dumps({"error": str(e)}), "application/json")

        def log_message(self, *args):  # quiet
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"serving {source} at {url}  (staged edits write on Save; Ctrl-C to stop)")
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--serve", action="store_true",
                    help="serve with staged editing; Save syncs to the .md")
    ap.add_argument("--port", type=int, default=8383)
    args = ap.parse_args()

    if args.serve:
        serve(args.source, args.port)
        return
    out = args.output or args.source.with_suffix(".html")
    out.write_text(build_page(args.source, editable=False), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
