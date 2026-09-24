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
its whole subtree (number chips recompute as you go), hover the boundary
between two bullets and click the plus to insert a new bullet there, or press
the Markdown button to edit the raw outline as text (add/delete/bulk-edit
bullets) and toggle back. Any staged change activates the Save button in the header;
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
        f'data-raw="{html.escape(node["raw"], quote=True)}">{text}</span></div>'
        f'<span class="act"><button class="del" type="button" title="delete bullet" aria-label="delete bullet"></button></span>'
    )
    if node["children"]:
        kids = "\n".join(render_node(c) for c in node["children"])
        return (
            f'<li class="branch open"><div class="{row_cls}"><div class="main">{grip}<button class="caret" '
            f'aria-label="toggle"></button>{span}</div><ul>{kids}</ul></li>'
        )
    return (
        f'<li class="leaf"><div class="{row_cls}"><div class="main">{grip}<span class="dot"></span>'
        f'{span}</div></li>'
    )


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    color-scheme: light dark;
    --serif: Charter, 'Iowan Old Style', 'Source Serif Pro', 'Palatino Linotype', Georgia, 'Times New Roman', serif;
    --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, system-ui, sans-serif;
    --mono: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    --bg:#faf8f3; --ink:#1f1d1a; --mut:#6e6960; --acc:#7a5c2e; --line:#ddd7cb;
    --hover:#f2eee5; --chip:#e9e2d3; --codebg:#eee9dd; --editbg:#fffdf6; --btn:#fffefb;
    --sel:#e9dcc2; --ok:#3d7a3d; --err:#a33b2e;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#1c1b19; --ink:#e7e2d8; --mut:#9d968a; --acc:#d2ab6a; --line:#37342f;
      --hover:#26241f; --chip:#33302a; --codebg:#2b2925; --editbg:#232119; --btn:#26241f;
      --sel:#4a3f2a; --ok:#8fc48f; --err:#e08b7d;
    }}
  }}
  html {{ -webkit-text-size-adjust:100%; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:17px/1.65 var(--serif);
         text-rendering:optimizeLegibility; font-kerning:normal;
         font-variant-ligatures:common-ligatures; }}
  ::selection {{ background:var(--sel); }}

  /* ---------- header chrome (UI face, not reading face) ---------- */
  header {{ position:sticky; top:0; z-index:2; background:var(--bg);
           border-bottom:1px solid var(--line); padding:10px 24px;
           display:flex; flex-wrap:wrap; gap:8px 12px; align-items:baseline;
           font:13px/1.4 var(--sans); }}
  header h1 {{ font:600 15px/1.4 var(--serif); margin:0; letter-spacing:-0.005em; }}
  header .src {{ color:var(--mut); font-size:12.5px; }}
  header .hint {{ color:var(--mut); font-size:12.5px; font-style:italic; }}
  header button {{ font:500 12.5px/1 var(--sans); padding:6px 11px; cursor:pointer;
                  color:var(--ink); background:var(--btn); border:1px solid var(--line);
                  border-radius:6px; }}
  header button:hover {{ border-color:var(--mut); }}
  header button:focus-visible {{ outline:2px solid var(--acc); outline-offset:2px; }}
  #saveBtn {{ margin-left:auto; padding:6px 16px; font-weight:600; }}
  #saveBtn:disabled {{ opacity:.45; cursor:default; }}
  #saveBtn.dirty {{ background:var(--acc); color:var(--bg); border-color:var(--acc); }}
  body.readonly #saveBtn, body.readonly #mdBtn {{ display:none; }}
  #status {{ font:12.5px var(--sans); color:var(--mut); min-width:60px; }}
  #status.saved {{ color:var(--ok); }}
  #status.error {{ color:var(--err); }}

  /* ---------- reading column ---------- */
  main {{ max-width:780px; margin:0 auto; padding:28px 24px 140px; }}
  #mdview {{ display:none; width:100%; min-height:calc(100vh - 140px); box-sizing:border-box;
            font:14.5px/1.7 var(--mono); color:var(--ink); tab-size:2;
            background:var(--editbg); border:1px solid var(--line); border-radius:8px;
            padding:20px 24px; resize:vertical; white-space:pre-wrap; overflow-wrap:break-word; }}
  #mdview:focus {{ outline:2px solid var(--acc); outline-offset:-1px; }}
  body.mdmode #tree {{ display:none; }}
  body.mdmode #mdview {{ display:block; }}

  ul {{ list-style:none; margin:0; padding:0 0 0 26px; border-left:1px solid var(--line); }}
  main > ul {{ border-left:none; padding-left:0; }}
  li {{ margin:0; }}
  /* a row is two boxes: .main (grip, caret/dot, chip, text) and .act (the
     delete controls). They highlight separately, with a gap between them,
     and .act keeps a fixed width so the text column never reflows. */
  .row {{ display:flex; align-items:baseline; gap:6px; }}
  .main {{ flex:1 1 auto; min-width:0; display:flex; align-items:baseline; gap:8px;
           padding:5px 8px 5px 0; border-radius:6px; }}
  .row:hover > .main {{ background:var(--hover); }}

  /* breathing room above section headings */
  li:has(> .row > .main > .txt.h1) {{ margin-top:8px; }}
  li:has(> .row > .main > .txt.h2) {{ margin-top:26px; }}
  li:has(> .row > .main > .txt.h3) {{ margin-top:16px; }}
  main > ul > li:first-child {{ margin-top:0; }}

  /* caret & dot get a real baseline via inline-block pseudo, so they
     centre on the x-height of whatever size text sits beside them */
  .caret {{ flex:0 0 auto; width:20px; border:none; background:none; cursor:pointer;
           padding:0; margin:0; font:inherit; line-height:inherit; color:inherit;
           text-align:center; border-radius:4px; }}
  .caret::before {{ content:''; display:inline-block; width:0; height:0; vertical-align:middle;
                   border-left:7px solid var(--acc); border-top:5px solid transparent;
                   border-bottom:5px solid transparent; transition:transform .12s; }}
  .caret:hover::before {{ border-left-color:var(--ink); }}
  .caret:focus-visible {{ outline:2px solid var(--acc); outline-offset:-2px; }}
  li.branch.open > .row .caret::before {{ transform:rotate(90deg); }}
  li.branch:not(.open) > ul {{ display:none; }}
  .dot {{ flex:0 0 auto; width:20px; text-align:center; }}
  .dot::before {{ content:''; display:inline-block; width:5px; height:5px; border-radius:50%;
                 background:var(--mut); opacity:.75; vertical-align:middle;
                 position:relative; top:-1px; }}
  .row.numbered > .main > .dot {{ display:none; }}

  /* text */
  .txt {{ flex:1 1 auto; min-width:0; overflow-wrap:break-word;
         text-wrap:pretty; hanging-punctuation:first; }}
  .h1 {{ font-size:26px; line-height:1.25; font-weight:700; letter-spacing:-0.015em; text-wrap:balance; }}
  .h2 {{ font-size:20px; line-height:1.3; font-weight:700; letter-spacing:-0.01em; text-wrap:balance; }}
  .h3 {{ font-size:17.5px; line-height:1.4; font-weight:600; font-style:italic; text-wrap:balance; }}
  strong {{ font-weight:700; }}
  .pnum {{ flex:0 0 auto; min-width:14px; text-align:center; color:var(--mut);
          font:600 11.5px/1 var(--sans); font-variant-numeric:tabular-nums;
          background:var(--chip); border-radius:999px; padding:3px 7px; white-space:nowrap;
          position:relative; top:-2px; }}
  .tag {{ color:var(--mut); font-style:italic; }}
  code {{ font:0.88em var(--mono); background:var(--codebg); padding:1px 5px; border-radius:4px; }}
  .txt.editing {{ background:var(--editbg); outline:1.5px solid var(--acc); border-radius:4px;
                 padding:0 4px; margin:0 -4px; cursor:text; caret-color:var(--acc); }}

  /* delete: a fixed-width action slot sits at the row's right edge on every
     row, so revealing controls never changes the text column's width. On
     hover the slot shows a trash; clicking it swaps in a check (delete) and
     a cross (cancel) in the same slot. */
  .act {{ flex:0 0 52px; display:flex; justify-content:flex-end; gap:4px;
          padding:5px 2px; border-radius:6px; }}
  .row:hover > .act, .row.confirming > .act {{ background:var(--hover); }}
  .act button {{ flex:0 0 auto; width:24px; border:none; background:none; cursor:pointer;
                 padding:0; margin:0; font:inherit; line-height:inherit; text-align:center;
                 border-radius:4px; color:var(--mut); }}
  .act button::before {{ content:''; display:inline-block; width:15px; height:15px;
                         vertical-align:middle; position:relative; top:-1px;
                         background:currentColor;
                         -webkit-mask:var(--icon) center/contain no-repeat;
                         mask:var(--icon) center/contain no-repeat; }}
  .act button:hover {{ background:var(--chip); }}
  .act button:focus-visible {{ outline:2px solid var(--acc); outline-offset:-2px; }}
  .del {{ opacity:0; transition:opacity .1s; }}
  .del::before {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6'/%3E%3C/svg%3E"); }}
  .row:hover > .act > .del, .del:focus-visible {{ opacity:1; }}
  .del:hover {{ color:var(--err); }}
  .row.confirming > .act > .del {{ display:none; }}
  .ok {{ color:var(--err); }}
  .ok::before {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20 6L9 17l-5-5'/%3E%3C/svg%3E"); }}
  .no::before {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M18 6L6 18M6 6l12 12'/%3E%3C/svg%3E"); }}
  body.readonly .act {{ display:none; }}

  /* drag handle & drop indicators */
  .grip {{ flex:0 0 auto; width:20px; margin-left:-24px; opacity:0; cursor:grab;
          color:var(--acc); font:700 15px/1.35 var(--sans);
          letter-spacing:-2px; user-select:none; text-align:center;
          border-radius:4px; transition:opacity .1s; }}
  .row:hover .grip {{ opacity:1; }}
  .grip:hover {{ background:var(--chip); }}
  .grip:active {{ cursor:grabbing; }}
  body.readonly .grip {{ display:none; }}
  li.dragging {{ opacity:.35; }}
  .row.drop-before > .main {{ box-shadow:0 -2px 0 var(--acc); }}
  .row.drop-after > .main {{ box-shadow:0 2px 0 var(--acc); }}

  /* insert-between affordance: a line with a centred plus that appears when
     hovering the boundary between two rows; clicking inserts a bullet there */
  #insertHint {{ position:fixed; display:none; align-items:center; gap:6px;
                height:20px; z-index:3; cursor:pointer; }}
  #insertHint.show {{ display:flex; }}
  #insertHint::before, #insertHint::after {{ content:''; flex:1 1 auto; height:2px;
                background:var(--acc); border-radius:1px; opacity:.5; }}
  #insertHint .plus {{ flex:0 0 auto; width:20px; height:20px; border-radius:50%;
                border:1px solid var(--acc); background:var(--btn); color:var(--acc);
                font:700 14px/16px var(--sans); text-align:center; padding:0;
                cursor:pointer; }}
  #insertHint:hover::before, #insertHint:hover::after {{ opacity:.9; }}
  #insertHint:hover .plus {{ background:var(--acc); color:var(--bg); }}
  body.readonly #insertHint {{ display:none; }}

  @media (max-width: 640px) {{
    body {{ font-size:16px; }}
    header {{ padding:8px 14px; }}
    header .hint {{ display:none; }}
    main {{ padding:18px 14px 100px; }}
    ul {{ padding-left:18px; }}
    .h1 {{ font-size:23px; }}
    .h2 {{ font-size:19px; }}
  }}
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
<div id="insertHint"><button class="plus" type="button" title="insert bullet" aria-label="insert bullet">+</button></div>
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
  let flashTimer = null;
  function flash(msg, cls) {{
    status.textContent = msg; status.className = cls || '';
    clearTimeout(flashTimer);
    if (msg) flashTimer = setTimeout(() => {{ status.textContent = ''; status.className = ''; }}, 2500);
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
    undoStack.length = redoStack.length = 0;   // positions no longer valid
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
      const span = '<span class="txt ' + d.cls + '" data-raw="' + esc(n.raw) + '">' + d.html + '</span></div>' +
                   '<span class="act"><button class="del" type="button" title="delete bullet" aria-label="delete bullet"></button></span>';
      if (n.children.length) {{
        return '<li class="branch open"><div class="row"><div class="main">' + grip +
               '<button class="caret" aria-label="toggle"></button>' + span + '</div><ul>' +
               n.children.map(renderNode).join('') + '</ul></li>';
      }}
      return '<li class="leaf"><div class="row"><div class="main">' + grip + '<span class="dot"></span>' +
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

  // nesting rules — returns an error message, or null when `li` may become a
  // child of `parentLi` (null = root level):
  //   * nothing may nest under a non-heading bullet — paragraphs are the
  //     deepest level an operation may create
  //   * a heading may only nest under a heading of a shallower level
  //     (## under #, ### under ##, never # under ## or ## under ##)
  const headingLevel = li => {{
    const span = li && li.querySelector(':scope > .row .txt');
    const m = span && /^(#{{1,6}})\\s/.exec(span.dataset.raw);
    return m ? m[1].length : 0;
  }};
  const parentLiOf = li => li.parentElement === tree ? null : li.parentElement.closest('li');
  function nestError(li, parentLi) {{
    if (!parentLi) return null;
    const plvl = headingLevel(parentLi);
    if (!plvl) return 'no indentation beyond paragraph level';
    const lvl = headingLevel(li);
    if (lvl && lvl <= plvl) return 'a heading can only go under a shallower heading';
    return null;
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

  // ---- history: Cmd/Ctrl+Z undoes the last move or delete, +Shift redoes ----
  // a position is {{parent: li|null, next: li|null}}; null parent = root level;
  // a null position means "not in the tree" (deleted)
  const posOf = li => ({{
    parent: li.parentElement === tree ? null : li.parentElement.closest('li'),
    next: li.nextElementSibling }});
  function placeAt(li, pos) {{
    const from = li.parentElement ? parentLiOf(li) : null;
    if (!pos) {{                                   // (re)delete
      li.remove();
      if (from) toLeafIfEmpty(from);
      renumberChips(); markDirty(); return;
    }}
    let ul = tree;
    if (pos.parent) {{ toBranch(pos.parent); ul = pos.parent.querySelector(':scope > ul'); }}
    if (pos.next && pos.next.parentElement === ul) ul.insertBefore(li, pos.next);
    else ul.appendChild(li);
    if (from && from !== pos.parent) toLeafIfEmpty(from);
    renumberChips(); markDirty();
  }}
  const undoStack = [], redoStack = [];
  function recordMove(li, before) {{ recordChange(li, before, posOf(li), 'move'); }}
  function recordChange(li, before, after, what) {{
    undoStack.push({{ li, before, after, what }});
    redoStack.length = 0;
  }}
  function undoMove() {{
    const m = undoStack.pop();
    if (!m) {{ flash('nothing to undo'); return; }}
    placeAt(m.li, m.before); redoStack.push(m); flash(m.what + ' undone');
  }}
  function redoMove() {{
    const m = redoStack.pop();
    if (!m) {{ flash('nothing to redo'); return; }}
    placeAt(m.li, m.after); undoStack.push(m); flash(m.what + ' redone');
  }}
  document.addEventListener('keydown', e => {{
    if (!EDITABLE || !(e.metaKey || e.ctrlKey) || e.altKey || e.key.toLowerCase() !== 'z') return;
    const a = document.activeElement;
    if (a && (a.isContentEditable || a === mdview)) return;   // native text undo applies
    if (document.body.classList.contains('mdmode')) return;
    e.preventDefault();
    if (e.shiftKey) redoMove(); else undoMove();
  }});

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

  // ---- delete: trash -> check (delete) / cross (cancel) -> gone, undoable ----
  function cancelConfirm() {{
    tree.querySelectorAll('.row.confirming').forEach(r => {{
      r.classList.remove('confirming');
      r.querySelectorAll(':scope > .act > .ok, :scope > .act > .no').forEach(b => b.remove());
    }});
  }}
  function iconButton(cls, label) {{
    const b = document.createElement('button');
    b.type = 'button'; b.className = cls; b.title = label; b.setAttribute('aria-label', label);
    return b;
  }}
  function askDelete(row) {{
    cancelConfirm();
    if (document.activeElement && document.activeElement.isContentEditable)
      document.activeElement.blur();               // stage a pending inline edit first
    row.classList.add('confirming');
    const n = row.closest('li').querySelectorAll('.txt').length;
    const ok = iconButton('ok', n > 1 ? 'delete ' + n + ' bullets' : 'delete bullet');
    const no = iconButton('no', 'cancel');
    row.querySelector(':scope > .act').prepend(ok, no);
    ok.focus();
  }}
  function doDelete(li) {{
    cancelConfirm();
    const before = posOf(li), from = parentLiOf(li);
    li.remove();
    if (from) toLeafIfEmpty(from);
    recordChange(li, before, null, 'delete');
    renumberChips(); markDirty();
    flash('deleted · ⌘Z to undo');
  }}
  document.addEventListener('click', e => {{
    if (!e.target.closest('.act')) cancelConfirm();
  }});
  document.addEventListener('keydown', e => {{ if (e.key === 'Escape') cancelConfirm(); }});

  // ---- delegated events (survive tree rebuilds) ----
  tree.addEventListener('click', e => {{
    const caret = e.target.closest('.caret');
    if (caret) {{ caret.closest('li').classList.toggle('open'); return; }}
    const del = e.target.closest('.del');
    if (del) {{ if (EDITABLE) askDelete(del.closest('.row')); return; }}
    const ok = e.target.closest('.ok');
    if (ok) {{ doDelete(ok.closest('li')); return; }}
    if (e.target.closest('.no')) {{ cancelConfirm(); return; }}
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
    const cancel = () => {{ cleanup(); span.innerHTML = prev;
      const liEl = span.closest('li');
      if (liEl && liEl.dataset.pendingNew) {{ liEl.remove(); renumberChips(); }} }};
    const stage = () => {{
      if (done) return;
      const val = span.textContent.replace(/\\n+/g, ' ').trim();
      if (!val || val === span.dataset.raw) {{ cancel(); return; }}
      span.dataset.raw = val;
      cleanup();
      const d = renderMd(val);
      span.innerHTML = d.html;
      span.className = 'txt ' + d.cls;
      const liEl = span.closest('li');
      if (liEl && liEl.dataset.pendingNew) {{     // a bullet born from insert-between
        delete liEl.dataset.pendingNew;
        recordChange(liEl, null, posOf(liEl), 'insert');
        flash('inserted · ⌘Z to undo');
      }}
      renumberChips();                              // heading-ness may have changed
      markDirty();
    }};
    const indentOutdent = out => {{
      const li = span.closest('li');
      const sel = getSelection();
      const off = sel.rangeCount ? sel.getRangeAt(0).startOffset : span.textContent.length;
      const before = posOf(li);
      suppressBlur = true;
      let moved = false;
      if (!out) {{
        const prevLi = li.previousElementSibling;
        const err = prevLi && nestError(li, prevLi);
        if (!prevLi) {{ flash('cannot indent further', 'error'); }}
        else if (err) {{ flash(err, 'error'); }}
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
      if (moved) {{ recordMove(li, before); renumberChips(); markDirty(); }}
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
      if (ev.key === 'Enter') {{
        ev.preventDefault();
        const val = span.textContent.replace(/\\n+/g, ' ').trim();
        const liEl = span.closest('li');
        stage();
        // plain Enter on a non-empty bullet continues the outline below it;
        // Shift+Enter (or an emptied bullet) just stages and exits
        if (!ev.shiftKey && val && liEl.isConnected) spawnBelow(liEl);
      }}
      if (ev.key === 'Escape') {{ ev.preventDefault(); cancel(); }}
      if (ev.key === 'Tab') {{ ev.preventDefault(); indentOutdent(ev.shiftKey); }}
    }};
    const onBlur = () => {{ if (!suppressBlur) stage(); }};
    span.addEventListener('keydown', onKey);
    span.addEventListener('blur', onBlur);
  }}

  // ---- insert-between: hover a row boundary -> line+plus -> new bullet ----
  const insertHint = document.getElementById('insertHint');
  let insertTarget = null;   // {{ li, where: 'before'|'after' }}
  function hideInsert() {{ insertHint.classList.remove('show'); insertTarget = null; }}
  const newLeafNestError = parentLi =>
    !parentLi || headingLevel(parentLi) ? null : 'no indentation beyond paragraph level';
  tree.addEventListener('mousemove', e => {{
    if (!EDITABLE || dragLi) {{ hideInsert(); return; }}
    if (document.activeElement && document.activeElement.isContentEditable) {{ hideInsert(); return; }}
    const row = e.target.closest('.row');
    if (!row) {{ hideInsert(); return; }}
    const rect = row.getBoundingClientRect();
    const EDGE = 7;
    let tgt = null;
    if (e.clientY < rect.top + EDGE) {{
      tgt = {{ li: row.closest('li'), where: 'before', y: rect.top }};
    }} else if (e.clientY > rect.bottom - EDGE) {{
      const rows = [...tree.querySelectorAll('.row')].filter(r => r.offsetParent !== null);
      const next = rows[rows.indexOf(row) + 1];
      tgt = next
        ? {{ li: next.closest('li'), where: 'before', y: next.getBoundingClientRect().top }}
        : {{ li: row.closest('li'), where: 'after', y: rect.bottom }};
    }}
    if (!tgt || newLeafNestError(parentLiOf(tgt.li))) {{ hideInsert(); return; }}
    const mrect = tgt.li.querySelector(':scope > .row > .main').getBoundingClientRect();
    insertHint.style.left = mrect.left + 'px';
    insertHint.style.width = mrect.width + 'px';
    insertHint.style.top = (tgt.y - 10) + 'px';
    insertHint.classList.add('show');
    insertTarget = tgt;
  }});
  document.addEventListener('mousemove', e => {{
    if (insertTarget && !e.target.closest('#insertHint') && !e.target.closest('#tree')) hideInsert();
  }});
  window.addEventListener('scroll', hideInsert, {{ passive: true }});
  function freshBullet() {{
    const li = document.createElement('li');
    li.className = 'leaf';
    li.dataset.pendingNew = '1';
    li.innerHTML = '<div class="row"><div class="main">' +
      '<span class="grip" draggable="true" title="drag to move">⋮⋮</span>' +
      '<span class="dot"></span><span class="txt item" data-raw=""></span></div>' +
      '<span class="act"><button class="del" type="button" title="delete bullet" ' +
      'aria-label="delete bullet"></button></span></div>';
    return li;
  }}
  function openBullet(li) {{
    renumberChips();
    startEdit(li.querySelector('.txt'), {{ preventDefault() {{}} }});
  }}
  // Enter while editing spawns the next bullet visually underneath: the first
  // child of an open branch (when nesting allows), else the next sibling.
  function spawnBelow(li) {{
    const nb = freshBullet();
    if (li.classList.contains('branch') && li.classList.contains('open') && !newLeafNestError(li)) {{
      li.querySelector(':scope > ul').prepend(nb);
    }} else if (!newLeafNestError(parentLiOf(li))) {{
      li.after(nb);
    }} else {{
      return;                                  // nowhere legal to put it — just exit edit mode
    }}
    openBullet(nb);
  }}
  insertHint.addEventListener('click', () => {{
    if (!insertTarget) return;
    const li = freshBullet();
    if (insertTarget.where === 'before') insertTarget.li.before(li);
    else insertTarget.li.after(li);
    hideInsert();
    openBullet(li);
  }});

  // drag-to-move — pure DOM; chips recompute; synced on Save
  let dragLi = null;
  const clearMarks = () => tree.querySelectorAll('.drop-before,.drop-after')
    .forEach(r => r.classList.remove('drop-before', 'drop-after'));
  tree.addEventListener('dragstart', e => {{
    const g = e.target.closest('.grip');
    if (!g || !EDITABLE) {{ e.preventDefault(); return; }}
    hideInsert();   // a visible insert hint would sit over the drop zone and swallow dragover
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
    if (nestError(dragLi, parentLiOf(li))) return;   // drop target breaks nesting rules
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
    const err = nestError(dragLi, parentLiOf(li));
    if (err) {{ flash(err, 'error'); return; }}
    e.preventDefault();
    const where = row.classList.contains('drop-before') ? 'before' : 'after';
    clearMarks();
    const before = posOf(dragLi);
    if (where === 'before') li.before(dragLi); else li.after(dragLi);
    recordMove(dragLi, before);
    renumberChips();
    markDirty();
  }});

  // Save — serialize current view and write the whole file
  async function doSave() {{
    let bullets;
    if (document.body.classList.contains('mdmode')) {{
      bullets = parseMarkdown(mdview.value);
      if (!bullets.length) {{ flash('no bullets found — fix the markdown first', 'error'); return; }}
    }} else {{
      if (document.activeElement && document.activeElement.isContentEditable)
        document.activeElement.blur();               // stage a pending inline edit first
      bullets = serialize();
      if (!bullets.length) {{ flash('nothing to save — the outline is empty', 'error'); return; }}
    }}
    try {{
      const r = await fetch('/save', {{ method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ hash: FILEHASH, bullets }}) }});
      if (!r.ok) throw new Error((await r.json()).error || r.status);
      dirty = false;
      location.reload();
    }} catch (err) {{ flash('save failed: ' + err.message, 'error'); }}
  }}
  saveBtn.addEventListener('click', doSave);
  document.addEventListener('keydown', e => {{
    if (!EDITABLE || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
    if (e.key.toLowerCase() !== 's') return;
    e.preventDefault();                       // never the browser's save-page dialog
    if (dirty) doSave(); else flash('no changes to save');
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
        hint="click to edit · Enter adds a bullet below · drag to move · hover between bullets to insert · trash to delete · ⌘Z undoes · Markdown for raw view · Save (⌘S) writes to the .md"
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
