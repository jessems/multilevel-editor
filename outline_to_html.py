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
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import date
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BULLET_RE = re.compile(r"^(\s*)- (.*)$")
# Paragraph role tags live in a sidecar metadata document, NOT in the outline:
# <outline>.tags.yaml maps a content fingerprint (sha256[:8] of the bullet's
# text) to a one-letter role, with the paragraph quoted in a comment for human
# readers. The sidecar also records the tagging scheme (creac / syllogism /
# subsumtion) so the letters are self-describing. The editor re-keys the whole
# sidecar on every save, so reorders and in-editor edits keep tags attached;
# a bullet edited outside the editor orphans its tag (reported, dropped on the
# next save). Legacy inline ' {X}' tags in the outline are migrated to the
# sidecar on the next save. The markdown view projects tags inline as ' {X}'
# for bulk editing; the file on disk stays clean.
TAG_RE = re.compile(r"^(.*?)\s*\{([A-Za-z])\}\s*$", re.S)
DEFAULT_TITLES = {"C": "Conclusion", "R": "Rule", "E": "Explanation", "A": "Application"}
DEFAULT_POS = {"C": 1, "R": 2, "E": 3, "A": 4}
SCHEME_NAMES = ("creac", "syllogism", "subsumtion")


def split_tag(raw: str):
    m = TAG_RE.match(raw)
    return (m.group(1), m.group(2)) if m else (raw, None)


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8]


def tags_path(source: Path) -> Path:
    return source.with_suffix(".tags.yaml")


def load_meta(source: Path):
    """Parse the sidecar (a deliberately flat YAML subset: 'scheme:' plus
    'hash: letter' entries under 'tags:'; comments ignored)."""
    meta = {"scheme": "creac", "tags": {}}
    p = tags_path(source)
    if not p.exists():
        return meta
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^scheme:\s*(\w+)", line)
        if m and m.group(1) in SCHEME_NAMES:
            meta["scheme"] = m.group(1)
            continue
        m = re.match(r"^\s+([0-9a-f]{8}):\s*([A-Za-z])\b", line)
        if m:
            meta["tags"][m.group(1)] = m.group(2)
    return meta


def write_meta(source: Path, scheme: str, tagged):
    """Rewrite the sidecar from live state: tagged = [(clean_text, letter)].
    No tags and the default scheme -> remove the sidecar entirely."""
    p = tags_path(source)
    if not tagged and scheme == "creac":
        p.unlink(missing_ok=True)
        return
    lines = [
        "# paragraph roles for " + source.name + " — maintained by multilevel-editor",
        "# keys are sha256[:8] of the bullet text; quotes are regenerated on save",
        f"scheme: {scheme}",
        "tags:",
    ]
    for text, letter in tagged:
        quote = " ".join(text.split())
        if len(quote) > 48:
            quote = quote[:48] + "…"
        lines.append(f"  {fingerprint(text)}: {letter}   # {quote}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    t = re.sub(r"\[([^\[\]]{1,80})\](?!\()", r'<span class="ph">[\1]</span>', t)
    return t, heading


def is_placeholder(text: str) -> bool:
    return text.startswith("[") and text.rstrip().endswith("]")


def render_node(node) -> str:
    letter = node.get("tag")
    text, heading = render_inline(node["raw"])
    cls = f"h{heading}" if heading else ("item placeholder" if is_placeholder(node["raw"]) else "item")
    numbered = bool(node.get("num"))
    if letter:
        pos = DEFAULT_POS.get(letter, "x")
        tip = DEFAULT_TITLES.get(letter, letter)
        badge = f'<span class="creac p-{pos}" data-tip="{tip}">{letter}</span>'
    elif numbered:
        badge = '<span class="creac empty" data-tip="tag paragraph role"></span>'
    else:
        badge = ""
    chip = f'<span class="pnum">{node["num"]}</span>' if numbered else ""
    chip = badge + chip
    row_cls = "row numbered" if numbered else "row"
    grip = '<span class="grip" draggable="true" title="drag to move">⋮⋮</span>'
    span = (
        f'{chip}<span class="txt {cls}" '
        f'data-raw="{html.escape(node["raw"], quote=True)}" '
        f'data-tag="{letter or ""}">{text}</span></div>'
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


DARK_VARS = (
    "--bg:#1c1b19; --ink:#e7e2d8; --mut:#9d968a; --acc:#d2ab6a; --line:#37342f; "
    "--hover:#26241f; --chip:#33302a; --codebg:#2b2925; --editbg:#232119; --btn:#26241f; "
    "--sel:#4a3f2a; --ok:#8fc48f; --err:#e08b7d; "
    "--t1:#82b6d6; --t2:#d2ab6a; --t3:#8fc48f; --t4:#d49bd4;"
)

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script>
  try {{ const t = localStorage.getItem('multilevel-editor.theme');
        if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; }} catch {{}}
</script>
<style>
  :root {{
    color-scheme: light dark;
    --serif: Charter, 'Iowan Old Style', 'Source Serif Pro', 'Palatino Linotype', Georgia, 'Times New Roman', serif;
    --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, system-ui, sans-serif;
    --mono: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    --bg:#faf8f3; --ink:#1f1d1a; --mut:#6e6960; --acc:#7a5c2e; --line:#ddd7cb;
    --hover:#f2eee5; --chip:#e9e2d3; --codebg:#eee9dd; --editbg:#fffdf6; --btn:#fffefb;
    --sel:#e9dcc2; --ok:#3d7a3d; --err:#a33b2e;
    --t1:#3f6e8e; --t2:#8a5a24; --t3:#3d7a3d; --t4:#8f4a8f;
  }}
  /* dark palette: follows the OS unless the header toggle pinned a theme (html[data-theme]) */
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{ {dark_vars} }}
  }}
  :root[data-theme="dark"] {{ {dark_vars} color-scheme:dark; }}
  :root[data-theme="light"] {{ color-scheme:light; }}
  html {{ -webkit-text-size-adjust:100%; }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
         font:17px/1.65 var(--serif);
         text-rendering:optimizeLegibility; font-kerning:normal;
         font-variant-ligatures:common-ligatures; }}
  ::selection {{ background:var(--sel); }}

  /* ---------- header chrome (UI face, not reading face) ---------- */
  /* one slim rail in three columns: the document on the left, the level switch
     in the true page centre (equal 1fr sides), the actions on the right. The
     status message drops out of the rail as a toast; the usage notes live in
     a key card behind the ? button. */
  header {{ position:sticky; top:0; z-index:2; box-sizing:border-box;
           display:grid; grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);
           align-items:center; gap:12px; padding:8px 14px; min-height:54px;
           background:color-mix(in srgb, var(--bg) 86%, transparent);
           -webkit-backdrop-filter:blur(12px) saturate(1.3); backdrop-filter:blur(12px) saturate(1.3);
           border-bottom:1px solid var(--line); font:13px/1.4 var(--sans); }}
  .hl {{ grid-column:1; display:flex; align-items:center; gap:10px; min-width:0; }}
  .hr {{ grid-column:3; justify-self:end; display:flex; align-items:center; gap:4px; }}
  .doc {{ display:flex; flex-direction:column; min-width:0; line-height:1.2; }}
  header h1 {{ font:600 15px/1.25 var(--serif); margin:0; letter-spacing:-0.005em;
              white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  header .src {{ font:500 10.5px/1.3 var(--mono); color:var(--mut); letter-spacing:.02em;
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  header button {{ font:500 12.5px/1 var(--sans); padding:0 10px; height:30px; cursor:pointer;
                  color:var(--ink); background:transparent; border:1px solid transparent;
                  border-radius:7px; transition:background .12s, border-color .12s, color .12s; }}
  header button:hover {{ background:var(--hover); }}
  header button:focus-visible {{ outline:2px solid var(--acc); outline-offset:1px; }}
  .hr .sep {{ width:1px; height:18px; background:var(--line); margin:0 6px; }}
  #saveBtn {{ display:inline-flex; align-items:center; gap:8px; padding:0 12px; font-weight:600;
             border-color:var(--line); background:var(--btn); }}
  #saveBtn kbd {{ font:500 10.5px/1 var(--sans); color:var(--mut); letter-spacing:.04em; }}
  #saveBtn:disabled {{ opacity:.5; cursor:default; background:transparent; }}
  #saveBtn:disabled kbd {{ display:none; }}
  #saveBtn.dirty {{ background:var(--acc); color:var(--bg); border-color:var(--acc);
                   box-shadow:0 1px 0 rgba(0,0,0,.08), 0 4px 12px -6px var(--acc); }}
  #saveBtn.dirty kbd {{ color:inherit; opacity:.75; }}
  #saveBtn.dirty::before {{ content:''; width:6px; height:6px; border-radius:50%;
                           background:currentColor; animation:pulse 1.8s ease-in-out infinite; }}
  @keyframes pulse {{ 50% {{ opacity:.35; }} }}
  body.readonly #saveBtn, body.readonly #mdBtn, body.readonly #helpBtn,
  body.readonly .hr .sep {{ display:none; }}
  /* icon buttons: a masked glyph in currentColor */
  .ibtn {{ width:30px; padding:0 !important; color:var(--mut) !important; }}
  .ibtn:hover {{ color:var(--ink) !important; }}
  .ibtn::before {{ content:''; display:block; width:16px; height:16px; margin:auto;
                  background:currentColor; -webkit-mask:var(--icon) center/contain no-repeat;
                  mask:var(--icon) center/contain no-repeat; }}
  #mdBtn {{ color:var(--mut); }}
  #mdBtn:hover, body.mdmode #mdBtn {{ color:var(--ink); }}
  body.mdmode #mdBtn {{ background:var(--chip); }}
  #helpBtn {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01'/%3E%3C/svg%3E"); }}
  /* light/dark toggle — shows the theme a click switches TO: a moon on light, a sun on dark */
  #themeBtn {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z'/%3E%3C/svg%3E"); }}
  html.theme-dark #themeBtn {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='5'/%3E%3Cpath d='M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42'/%3E%3C/svg%3E"); }}
  #helpBtn[aria-expanded="true"] {{ color:var(--acc) !important; background:var(--hover); }}
  /* the key card */
  #help {{ position:absolute; top:calc(100% + 8px); right:14px; width:320px; max-width:calc(100vw - 28px);
          box-sizing:border-box; padding:14px 16px 12px; background:var(--bg); color:var(--ink);
          border:1px solid var(--line); border-radius:10px; z-index:7;
          box-shadow:0 12px 32px -12px rgba(0,0,0,.28), 0 2px 6px -2px rgba(0,0,0,.08);
          transform-origin:top right; animation:cardin .14s ease-out; }}
  #help[hidden] {{ display:none; }}
  @keyframes cardin {{ from {{ opacity:0; transform:translateY(-4px) scale(.98); }} }}
  #help h2 {{ font:600 10.5px/1 var(--sans); letter-spacing:.1em; text-transform:uppercase;
             color:var(--mut); margin:0 0 10px; }}
  #help ul {{ list-style:none; margin:0; padding:0; border-left:none; font:13px/1.45 var(--sans); }}
  #help li {{ padding:5px 0; border-top:1px dashed var(--line); }}
  #help li:first-child {{ border-top:none; }}
  #help kbd, #help b {{ font:600 11px/1 var(--sans); padding:3px 6px; border-radius:4px;
                       background:var(--chip); border-bottom:1px solid var(--line);
                       font-variant-numeric:tabular-nums; }}
  #help b {{ font-weight:600; background:none; border:none; padding:0; }}
  /* the status message drops out of the rail as a toast, centred under the switch */
  #status {{ position:absolute; top:calc(100% + 8px); left:50%; transform:translateX(-50%);
            font:500 12.5px/1 var(--sans); color:var(--ink); background:var(--bg);
            border:1px solid var(--line); border-radius:999px; padding:7px 14px;
            box-shadow:0 8px 20px -10px rgba(0,0,0,.3); white-space:nowrap; z-index:4;
            pointer-events:none; animation:toast .18s ease-out; }}
  #status:empty {{ display:none; }}
  @keyframes toast {{ from {{ opacity:0; transform:translate(-50%, -6px); }} }}
  #status.saved {{ color:var(--ok); border-color:color-mix(in srgb, var(--ok) 45%, var(--line)); }}
  #status.error {{ color:var(--err); border-color:color-mix(in srgb, var(--err) 45%, var(--line)); }}
  /* level switch — one mode per depth the outline uses: N shows the top N levels.
     The centre column of the header grid, so it sits on the page centre. */
  .levels-row {{ grid-column:2; display:flex; justify-content:center; }}
  .levels-row:has(#levels:empty), body.mdmode .levels-row {{ visibility:hidden; }}
  #levels {{ display:inline-flex; align-items:stretch; gap:2px; padding:2px;
             background:var(--hover); border:1px solid var(--line); border-radius:9px; }}
  li.lvhide {{ display:none !important; }}                 /* below the chosen level */
  li.lvhide[data-pending-new], li.lvhide:has(> .row .txt[contenteditable="true"]) {{
    display:list-item !important; }}                       /* …but never while being edited */
  li.lvcut > .row .caret {{ pointer-events:none; }}         /* all children hidden: a leaf dot */
  li.lvcut > .row .caret::before {{ width:5px; height:5px; border:0; border-radius:50%;
    background:var(--mut); opacity:.75; transform:none; position:relative; top:-1px; }}
  li.lvcut > .row.numbered .caret::before {{ display:none; }}
  #levels button {{ border:none; border-radius:7px; height:28px; padding:0 9px; min-width:34px;
                   color:var(--mut); }}
  #levels button svg {{ display:block; width:18px; height:18px; margin:0 auto; }}
  #levels button:hover {{ background:var(--bg); color:var(--ink); }}
  #levels button.on {{ background:var(--acc); color:var(--bg);
                      box-shadow:0 1px 2px rgba(0,0,0,.15), inset 0 1px 0 rgba(255,255,255,.12); }}
  #levels button:focus-visible {{ outline-offset:-2px; }}

  /* ---------- reading column ---------- */
  main {{ max-width:780px; margin:0 auto; padding:28px 24px 140px; }}
  #mdview {{ display:none; width:100%; min-height:calc(100vh - 140px); box-sizing:border-box;
            font:14.5px/1.7 var(--mono); color:var(--ink); tab-size:2;
            background:var(--editbg); border:1px solid var(--line); border-radius:8px;
            padding:20px 24px; resize:vertical; white-space:pre-wrap; overflow-wrap:break-word; }}
  #mdview:focus {{ outline:2px solid var(--acc); outline-offset:-1px; }}
  body.mdmode #tree {{ display:none; }}
  body.mdmode #mdview {{ display:block; }}

  :root {{ --indent:26px; }}
  ul {{ list-style:none; margin:0; padding:0 0 0 var(--indent); border-left:1px solid var(--line); }}
  /* numbered paragraphs form ONE column whatever heading depth they hang
     under: a paragraph directly under a ## is shifted to where a paragraph
     under a ### sits (the level switch computes --shift = levels missing) */
  li.para {{ margin-left:calc(var(--shift, 0) * (var(--indent) + 1px)); }}   /* +1px per skipped list border */
  main > ul {{ border-left:none; padding-left:0; }}
  li {{ margin:0; }}
  /* a row is two boxes: .main (grip, caret/dot, chip, text) and .act (the
     delete controls). They highlight separately, with a gap between them,
     and .act keeps a fixed width so the text column never reflows. */
  .row {{ display:flex; align-items:baseline; gap:6px; }}
  .main {{ flex:1 1 auto; min-width:0; display:flex; align-items:baseline; gap:8px;
           padding:5px 8px 5px 0; border-radius:6px; }}
  .row:hover > .main {{ background:var(--hover); }}

  /* vertical rhythm: sections breathe most, counts next, paragraphs a little */
  li:has(> .row > .main > .txt.h1) {{ margin-top:8px; }}
  li:has(> .row > .main > .txt.h2) {{ margin-top:36px; }}
  li:has(> .row > .main > .txt.h3) {{ margin-top:22px; }}
  li:has(> .row.numbered) {{ margin-top:8px; }}
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
  /* the number chip is the marker on numbered rows: no dot, and no caret
     either when the paragraph has written text under it (the level switch
     shows or hides that text), so its heading stays aligned with its siblings */
  .row.numbered > .main > .dot, .row.numbered > .main > .caret {{ display:none; }}

  /* text */
  .txt {{ flex:1 1 auto; min-width:0; overflow-wrap:break-word;
         text-wrap:pretty; hanging-punctuation:first; }}
  .h1 {{ font-size:27px; line-height:1.22; font-weight:700; letter-spacing:-0.015em; text-wrap:balance; }}
  .h2 {{ font-size:21px; line-height:1.3; font-weight:700; letter-spacing:-0.01em; text-wrap:balance; }}
  .h3 {{ font-size:18px; line-height:1.38; font-weight:600; letter-spacing:-0.005em; text-wrap:balance; }}
  .h3 em {{ font-style:italic; font-weight:500; }}
  strong {{ font-weight:700; }}
  /* a paragraph's topic sentence is a plain reading line; once it has written
     text under it, it becomes the run-in heading over that prose */
  li.written > .row > .main > .txt {{ font-weight:600; letter-spacing:-0.004em; }}
  li.written > ul {{ border-left:none; padding-left:var(--indent); }}   /* prose is not a branch */
  li.pbody {{ margin:2px 0 14px; }}
  li.pbody > .row > .main {{ padding-top:2px; padding-bottom:2px; }}
  li.pbody > .row > .main > .txt {{ font-size:17.5px; line-height:1.72; font-weight:400;
                                     color:var(--ink); max-width:62ch; }}
  /* placeholders: [cite], [fact: …] inline become quiet chips; a bullet that
     is nothing but a bracket note reads muted and italic */
  .ph {{ font:500 12px/1 var(--sans); color:var(--mut); white-space:nowrap; letter-spacing:.02em;
        opacity:.85; }}
  li.pbody .ph {{ font-size:11.5px; }}              /* quieter still inside running prose */
  .txt.placeholder {{ color:var(--mut); font-style:italic; }}
  .txt.placeholder .ph {{ font:inherit; color:inherit; opacity:1; letter-spacing:0; }}
  .pnum {{ flex:0 0 auto; min-width:14px; text-align:center; color:var(--mut);
          font:600 11.5px/1 var(--sans); font-variant-numeric:tabular-nums;
          background:var(--chip); border-radius:999px; padding:3px 7px; white-space:nowrap;
          position:relative; top:-2px; }}
  .tag {{ color:var(--mut); font-style:italic; }}
  /* CREAC role badge — one letter, before the paragraph number; click cycles */
  .creac {{ flex:0 0 auto; width:18px; height:18px; border-radius:50%; text-align:center;
           font:700 10.5px/15px var(--sans); cursor:pointer; user-select:none;
           border:1.5px solid transparent; box-sizing:border-box;
           position:relative; top:-2px; }}
  .creac.empty {{ opacity:0; border:1.5px dashed var(--mut); transition:opacity .1s; }}
  /* an empty flex item has no text baseline and would align by its bottom
     edge — a zero-width space gives it the same baseline as a lettered badge */
  .creac.empty::before {{ content:'\\200B'; }}
  .creac.empty:hover {{ opacity:1; }}
  body.notags .creac {{ display:none; }}
  /* black tooltip naming the role */
  .creac::after {{ content:attr(data-tip); position:absolute;
                  bottom:calc(100% + 7px); left:50%; transform:translateX(-50%);
                  background:#111; color:#fff; padding:4px 9px; border-radius:5px;
                  font:500 11px/1.35 var(--sans); letter-spacing:.01em;
                  white-space:nowrap; opacity:0; visibility:hidden;
                  transition:opacity .1s; pointer-events:none; z-index:4; }}
  .creac:hover::after {{ opacity:1; visibility:visible; }}
  .row:hover .creac.empty {{ opacity:.55; }}
  /* hues by POSITION in the active scheme, so every scheme reads the same */
  .creac.p-1 {{ color:var(--t1); border-color:var(--t1);
               background:color-mix(in srgb, currentColor 12%, transparent); }}
  .creac.p-2 {{ color:var(--t2); border-color:var(--t2);
               background:color-mix(in srgb, currentColor 12%, transparent); }}
  .creac.p-3 {{ color:var(--t3); border-color:var(--t3);
               background:color-mix(in srgb, currentColor 12%, transparent); }}
  .creac.p-4 {{ color:var(--t4); border-color:var(--t4);
               background:color-mix(in srgb, currentColor 12%, transparent); }}
  .creac.p-x {{ color:var(--mut); border-color:var(--mut); border-style:dashed;
               background:color-mix(in srgb, currentColor 10%, transparent); }}
  body.readonly .creac {{ pointer-events:none; }}
  body.readonly .creac.empty {{ display:none; }}

  /* ---------- settings sidebar (opened from the top-right cog) ---------- */
  #menuBtn {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='3'/%3E%3Cpath d='M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z'/%3E%3C/svg%3E"); }}
  body.readonly #menuBtn {{ display:none; }}
  #sidebar {{ position:fixed; top:0; right:0; bottom:0; width:300px; z-index:6;
             background:var(--bg); border-left:1px solid var(--line);
             box-shadow:-6px 0 24px rgba(0,0,0,.12);
             transform:translateX(105%); transition:transform .16s ease-out;
             padding:18px 20px; box-sizing:border-box; overflow-y:auto;
             font:14px/1.5 var(--sans); }}
  body.sidebar-open #sidebar {{ transform:none; }}
  .sb-head {{ display:flex; align-items:baseline; justify-content:space-between;
             border-bottom:1px solid var(--line); padding-bottom:10px; margin-bottom:14px; }}
  .sb-head h2 {{ font:600 15px/1.4 var(--sans); margin:0; }}
  #sbClose {{ border:none; background:none; cursor:pointer; font:400 20px/1 var(--sans);
             color:var(--mut); padding:2px 6px; border-radius:4px; }}
  #sbClose:hover {{ background:var(--hover); color:var(--ink); }}
  .sb-row {{ display:flex; align-items:baseline; gap:8px; padding:6px 0; cursor:pointer; }}
  .sb-row input {{ accent-color:var(--acc); }}
  .sb-sub {{ display:block; color:var(--mut); font-size:12px; margin-left:2px; }}
  .sb-group {{ border:1px solid var(--line); border-radius:8px; margin:12px 0 0;
              padding:8px 14px 12px; }}
  .sb-group legend {{ font:600 12.5px var(--sans); color:var(--mut); padding:0 4px; }}
  .sb-group:disabled {{ opacity:.45; }}
  .sb-group .sb-row {{ flex-direction:row; flex-wrap:wrap; }}
  .sb-row b {{ font-weight:600; }}
  code {{ font:0.88em var(--mono); background:var(--codebg); padding:1px 5px; border-radius:4px; }}
  .txt.editing {{ background:var(--editbg); outline:1.5px solid var(--acc); border-radius:4px;
                 padding:0 4px; margin:0 -4px; cursor:text; caret-color:var(--acc); }}

  /* delete: a fixed-width action slot sits at the row's right edge on every
     row, so revealing controls never changes the text column's width. On
     hover the slot shows a trash; clicking it swaps in a check (delete) and
     a cross (cancel) in the same slot. */
  /* ---------- right margin: notes beside a paragraph heading ---------- */
  /* Hung in the page margin, level with the row's first line; when the
     margin is too narrow (JS sets body.margin-inline) they sit at the end of
     the row instead, before the action slot. */
  .row {{ position:relative; }}
  .margin {{ position:absolute; left:calc(100% + 18px); top:9px; width:max-content;
            display:flex; flex-direction:column; align-items:flex-start; gap:4px; }}
  body.margin-inline .margin {{ position:static; order:1; flex:0 0 auto; align-self:baseline; }}
  /* quiet by default: muted small text behind a tinted icon, no chip;
     firms up a little while the row is hovered */
  .mnote {{ display:inline-flex; align-items:center; gap:5px; white-space:nowrap;
           font:500 11.5px/1 var(--sans); letter-spacing:.01em; color:var(--mut);
           opacity:.8; transition:opacity .1s; }}
  .row:hover .mnote {{ opacity:1; }}
  .mnote::before {{ content:''; flex:0 0 auto; width:12px; height:12px;
    background:color-mix(in srgb, var(--err) 65%, var(--mut));
    -webkit-mask:var(--icon) center/contain no-repeat; mask:var(--icon) center/contain no-repeat; }}
  .mnote.cite::before {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 21c3 0 7-1 7-8V5c0-1.25-.76-2-2-2H4c-1.25 0-2 .75-2 2v6c0 1.25.75 2 2 2 1 0 1 0 1 1v1c0 1-1 2-2 2s-1 0-1 1v3c0 1 0 1 1 1z'/%3E%3Cpath d='M15 21c3 0 7-1 7-8V5c0-1.25-.76-2-2-2h-4c-1.25 0-2 .75-2 2v6c0 1.25.75 2 2 2h.75c0 2.25.25 4-2.75 4v3c0 1 0 1 1 1z'/%3E%3C/svg%3E"); }}
  .act {{ order:2; flex:0 0 80px; display:flex; justify-content:flex-end; gap:4px;
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
  .del, .gen {{ opacity:0; transition:opacity .1s; }}
  /* the quill: generate the paragraph's written text (numbered rows only) */
  .gen::before {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20.24 12.24a6 6 0 0 0-8.49-8.49L5 10.5V19h8.5zM16 8L2 22M17.5 15H9'/%3E%3C/svg%3E"); }}
  .row:hover > .act > .gen, .gen:focus-visible {{ opacity:1; }}
  .gen:hover {{ color:var(--acc); }}
  .gen.busy {{ opacity:1; color:var(--acc); cursor:progress; }}
  .gen.busy::before {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83'/%3E%3C/svg%3E");
                       animation:spin 1s linear infinite; }}
  @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
  .row.confirming > .act > .gen {{ display:none; }}
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
  /* written text (the level under a paragraph): belongs to its paragraph, so
     no handle of its own and no dot; the paragraph it belongs to reads as a
     heading over it */
  li.pbody > .row .grip {{ display:none; }}
  li.pbody > .row > .main > .dot {{ visibility:hidden; width:40px; }}   /* text starts past the heading text */
  li.written > .row > .main > .txt {{ font-weight:600; }}
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

  /* ---------- a variant being written: banner + live-filling tree ---------- */
  #genBanner {{ margin:-4px 0 22px; padding:14px 16px 12px; border:1px solid var(--line); border-radius:10px;
                background:var(--editbg); font:13px/1.45 var(--sans); color:var(--ink); }}
  .gen-head {{ display:flex; align-items:center; gap:9px; }}
  .gen-dot {{ width:9px; height:9px; border-radius:50%; background:var(--acc); flex:0 0 auto;
             animation:genpulse 1.2s ease-in-out infinite; }}
  .gen-count {{ margin-left:auto; color:var(--mut); font-variant-numeric:tabular-nums; }}
  .gen-sub {{ color:var(--mut); margin:4px 0 0 18px; }}
  .gen-bar {{ position:relative; height:3px; margin:12px 0 0; border-radius:2px; overflow:hidden;
             background:color-mix(in srgb, var(--acc) 18%, transparent); }}
  .gen-bar span {{ position:absolute; inset:0; width:38%; border-radius:2px; background:var(--acc);
                  animation:genslide 1.6s cubic-bezier(.4,0,.2,1) infinite; }}
  .gen-err {{ color:var(--err); margin-top:10px; }}
  .gen-err a {{ color:inherit; }}
  body.gen-done .gen-dot {{ animation:none; background:var(--ok); }}
  body.gen-done .gen-bar span {{ animation:none; width:100%; background:var(--ok); }}
  body.gen-failed .gen-dot {{ animation:none; background:var(--err); }}
  body.gen-failed .gen-bar {{ display:none; }}
  @keyframes genpulse {{ 0%,100% {{ transform:scale(.8); opacity:.55; }} 50% {{ transform:scale(1.15); opacity:1; }} }}
  @keyframes genslide {{ from {{ left:-40%; }} to {{ left:100%; }} }}
  /* bullets arriving from the stream fade in; the placeholder title breathes */
  @keyframes genfade {{ from {{ opacity:0; transform:translateY(3px); }} to {{ opacity:1; transform:none; }} }}
  body.generating li.fresh > .row {{ animation:genfade .35s ease-out both; }}
  body.generating .txt.h1.placeholder-title {{ color:var(--mut); animation:genpulse 1.6s ease-in-out infinite; }}
  body.generating #levels, body.generating .main-tools {{ display:none; }}

  /* ---------- main tools (top right of the reading column) + variant dialog ---------- */
  .main-tools {{ display:flex; justify-content:flex-end; margin:-6px 0 16px; }}
  /* the primary action of the top level: an accent pill with a branch icon,
     in the header's sans face — the same voice as the armed Save button */
  .main-tools button {{ display:inline-flex; align-items:center; gap:7px; cursor:pointer;
                        font:600 12.5px/1 var(--sans); letter-spacing:.01em; padding:8px 14px 8px 12px;
                        color:var(--bg); background:var(--acc); border:1px solid var(--acc); border-radius:999px;
                        box-shadow:0 1px 2px rgba(0,0,0,.10);
                        transition:transform .08s ease, box-shadow .12s ease, filter .12s ease; }}
  .main-tools button::before {{ content:''; display:inline-block; width:14px; height:14px; background:currentColor;
    -webkit-mask:var(--icon) center/contain no-repeat; mask:var(--icon) center/contain no-repeat;
    --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 3v12'/%3E%3Ccircle cx='18' cy='6' r='3'/%3E%3Ccircle cx='6' cy='18' r='3'/%3E%3Cpath d='M18 9a9 9 0 0 1-9 9'/%3E%3C/svg%3E"); }}
  .main-tools button:hover {{ filter:brightness(1.07); box-shadow:0 3px 8px rgba(0,0,0,.16); transform:translateY(-1px); }}
  .main-tools button:active {{ transform:none; box-shadow:0 1px 2px rgba(0,0,0,.10); filter:none; }}
  .main-tools button:focus-visible {{ outline:2px solid var(--acc); outline-offset:3px; }}
  body:not(.toplevel) .main-tools, body.readonly .main-tools, body.mdmode .main-tools {{ display:none; }}
  #variantDlg {{ border:1px solid var(--line); border-radius:10px; background:var(--bg); color:var(--ink);
                 padding:20px 22px 18px; width:min(560px, 92vw); box-sizing:border-box;
                 font:14px/1.5 var(--sans); box-shadow:0 18px 50px rgba(0,0,0,.25); }}
  #variantDlg::backdrop {{ background:rgba(0,0,0,.35); }}
  #variantDlg h2 {{ margin:0 0 6px; font:600 16px/1.3 var(--sans); }}
  .dlg-sub {{ margin:0 0 12px; color:var(--mut); font-size:13px; }}
  #variantPrompt {{ width:100%; box-sizing:border-box; font:15px/1.55 var(--serif); color:var(--ink);
                    background:var(--editbg); border:1px solid var(--line); border-radius:8px;
                    padding:10px 12px; resize:vertical; }}
  #variantPrompt::placeholder {{ color:var(--mut); opacity:.9; }}
  #variantPrompt:focus {{ outline:2px solid var(--acc); outline-offset:-1px; }}
  .dlg-hint {{ margin:6px 0 0; color:var(--mut); font-size:12px; }}
  .dlg-err {{ margin:8px 0 0; color:var(--err); font-size:13px; min-height:1.2em; }}
  .dlg-actions {{ display:flex; gap:8px; align-items:center; margin-top:14px; }}
  .dlg-spacer {{ flex:1 1 auto; }}
  .dlg-actions button {{ font:500 12.5px/1 var(--sans); padding:7px 12px; cursor:pointer;
                         color:var(--ink); background:var(--btn); border:1px solid var(--line); border-radius:6px; }}
  .dlg-actions button:hover {{ border-color:var(--mut); }}
  .dlg-actions button.primary {{ background:var(--acc); color:var(--bg); border-color:var(--acc); font-weight:600; }}
  .dlg-actions button:disabled {{ opacity:.5; cursor:progress; }}
  #variantDlg.busy .dlg-err {{ color:var(--mut); }}

  /* ---------- navigator (left sidebar): the outline's levels as a descending tree ---------- */
  :root {{ --navw:264px; --hdr-h:52px; }}
  #navBtn {{ --icon:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='3' width='18' height='18' rx='2'/%3E%3Cpath d='M9 3v18'/%3E%3C/svg%3E"); }}
  body.nav-open #navBtn {{ color:var(--acc) !important; }}
  #nav {{ position:fixed; left:0; top:var(--hdr-h); bottom:0; width:var(--navw); z-index:1;
         display:none; box-sizing:border-box; overflow-y:auto; padding:14px 8px 40px 10px;
         background:var(--bg); border-right:1px solid var(--line); font:13px/1.35 var(--sans); }}
  body.nav-open #nav {{ display:block; }}
  body.nav-open main {{ margin-left:max(calc(var(--navw) + 16px), calc((100% - 780px) / 2)); }}
  body.mdmode #nav {{ opacity:.45; pointer-events:none; }}   /* stale while the raw text is edited */
  .nav-title {{ font:600 11px/1 var(--sans); letter-spacing:.08em; text-transform:uppercase;
               color:var(--mut); padding:0 6px 10px; }}
  #nav ul {{ list-style:none; margin:0; padding:0 0 0 12px; border-left:none; }}
  #nav > ul {{ padding-left:0; }}
  #nav li:not(.open) > ul, #nav li.deep {{ display:none; }}
  .nrow {{ display:flex; align-items:center; border-radius:5px; }}
  .nrow:hover {{ background:var(--hover); }}
  #nav li.current > .nrow {{ background:var(--chip); }}
  .ncaret, .nspace {{ flex:0 0 16px; height:24px; }}
  .ncaret {{ border:none; background:none; padding:0; cursor:pointer; border-radius:4px; }}
  .ncaret::before {{ content:''; display:inline-block; vertical-align:middle;
    border-left:5px solid var(--mut); border-top:4px solid transparent;
    border-bottom:4px solid transparent; transition:transform .12s; }}
  .ncaret:hover::before {{ border-left-color:var(--ink); }}
  #nav li.open > .nrow .ncaret::before {{ transform:rotate(90deg); }}
  .nlabel {{ flex:1 1 auto; min-width:0; border:none; background:none; cursor:pointer;
            text-align:left; padding:3px 4px; font:inherit; color:var(--ink);
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-radius:4px; }}
  .nlabel.nh1, .nlabel.nh2 {{ font-weight:600; }}
  /* the skeleton's variants share the outline's top level, each drawn as a
     full tree; the open document's title row is tinted, a suffix names a variant */
  #nav > ul > li.here > .nrow {{ background:var(--chip); }}
  .nsuffix {{ color:var(--mut); font-weight:500; }}
  .nlabel.npara {{ color:var(--mut); }}
  .ncaret:focus-visible, .nlabel:focus-visible {{ outline:2px solid var(--acc); outline-offset:-2px; }}
  .nnum {{ font:600 10.5px var(--sans); font-variant-numeric:tabular-nums; margin-right:6px; }}
  /* a jump from the navigator lands below the sticky header and flashes the row */
  .row {{ scroll-margin-top:calc(var(--hdr-h) + 12px); }}
  @keyframes navflash {{ from {{ background:var(--sel); }} to {{ background:transparent; }} }}
  .row.navflash > .main {{ animation:navflash 1.4s ease-out; }}
  @media (max-width: 900px) {{   /* too narrow to sit beside the text: overlay it */
    body.nav-open main {{ margin-left:auto; }}
    #nav {{ z-index:5; box-shadow:6px 0 24px rgba(0,0,0,.12); }}
  }}

  @media (max-width: 640px) {{
    body {{ font-size:16px; }}
    header {{ grid-template-columns:minmax(0,1fr) auto; row-gap:6px; padding:8px 10px; }}
    .hr {{ grid-column:2; }}
    .levels-row {{ grid-column:1 / -1; grid-row:2; }}
    .levels-row:has(#levels:empty), body.mdmode .levels-row {{ display:none; }}
    #saveBtn kbd, .hr .sep {{ display:none; }}
    main {{ padding:18px 14px 100px; }}
    :root {{ --indent:18px; }}
    .h1 {{ font-size:23px; }}
    .h2 {{ font-size:19px; }}
  }}
</style>
</head>
<body class="{bodycls}">
<header>
  <div class="hl">
    <button id="navBtn" class="ibtn" aria-label="navigator" title="navigator" aria-controls="nav" aria-expanded="false"></button>
    <div class="doc"><h1 title="{title}">{title}</h1><span class="src" title="{source}">{source}</span></div>
  </div>
  <div class="levels-row"><span id="levels" role="group" aria-label="visible levels" title="visible levels"></span></div>
  <div class="hr">
    <button id="mdBtn" title="edit the raw markdown">Markdown</button>
    <button id="helpBtn" class="ibtn" aria-label="how to edit" title="how to edit" aria-controls="help" aria-expanded="false"></button>
    <button id="themeBtn" class="ibtn" type="button"></button>
    <span class="sep"></span>
    <button id="saveBtn" disabled title="write the skeleton and the draft">Save<kbd>⌘S</kbd></button>
    <button id="menuBtn" class="ibtn" aria-label="settings" title="settings"></button>
  </div>
  <span id="status" role="status" aria-live="polite"></span>
  <div id="help" hidden data-hint="{hint}"><h2>How to edit</h2><ul></ul></div>
</header>
<aside id="sidebar" aria-label="settings">
  <div class="sb-head"><h2>Settings</h2><button id="sbClose" aria-label="close">×</button></div>
  <label class="sb-row"><input type="checkbox" id="setTagging"> <span><b>Paragraph tagging</b>
    <span class="sb-sub">one-letter role badges before the paragraph numbers</span></span></label>
  <fieldset id="schemeGroup" class="sb-group">
    <legend>Tagging scheme</legend>
    <label class="sb-row"><input type="radio" name="scheme" value="creac"> <span><b>CREAC</b>
      <span class="sb-sub">Neumann — Conclusion · Rule · Explanation · Application</span></span></label>
    <label class="sb-row"><input type="radio" name="scheme" value="syllogism"> <span><b>Syllogism</b>
      <span class="sb-sub">Scalia &amp; Garner — Major premise · minor premise · Conclusion</span></span></label>
    <label class="sb-row"><input type="radio" name="scheme" value="subsumtion"> <span><b>Subsumtion</b>
      <span class="sb-sub">Gutachtenstil — Obersatz · Definition · Subsumtion · Ergebnis</span></span></label>
  </fieldset>
</aside>
<nav id="nav" aria-label="navigator"><div class="nav-title">Navigator</div><ul id="navTree"></ul></nav>
<main>
<div id="genBanner" hidden>
  <div class="gen-head"><span class="gen-dot"></span><strong id="genTitle">Writing this variant…</strong>
    <span id="genCount" class="gen-count"></span></div>
  <div id="genSub" class="gen-sub"></div>
  <div class="gen-bar"><span></span></div>
  <div id="genErr" class="gen-err" hidden></div>
</div>
<div class="main-tools"><button id="variantBtn" type="button" title="generate a variant of this skeleton as a new file">New skeleton variant</button></div>
<ul id="tree">{tree}</ul>
<textarea id="mdview" spellcheck="false"></textarea>
</main>
<dialog id="variantDlg" aria-labelledby="variantTitle">
  <h2 id="variantTitle">Generate a skeleton variant</h2>
  <p class="dlg-sub">The model rebuilds this skeleton at the level you are viewing — same facts, same
    placeholders — following your instruction. Only the headings down to this level are generated; deeper
    levels are developed later. The result is saved as a new file beside this one and opened; this skeleton
    is not changed.</p>
  <textarea id="variantPrompt" rows="5" spellcheck="true" placeholder="{default_instruction}"></textarea>
  <p class="dlg-hint">Leave the box empty to use the example above.</p>
  <p class="dlg-err" id="variantErr" role="alert"></p>
  <div class="dlg-actions">
    <button type="button" id="variantCancel">Cancel</button>
    <span class="dlg-spacer"></span>
    <button type="button" id="variantDefault" title="generate with the example instruction">Use default settings</button>
    <button type="button" id="variantGo" class="primary">Generate</button>
  </div>
</dialog>
<div id="insertHint"><button class="plus" type="button" title="insert bullet" aria-label="insert bullet">+</button></div>
<script>
  const EDITABLE = {editable};
  const FILEHASH = "{filehash}";
  const SOURCE = {source_js};
  const DOC = {doc_js};                        // the family member this page edits
  const DOCS = {docs_json};                    // the skeleton and its variants
  const FAMILY = {family_json};                // the family's level shape: {{h, para}} or null
  const DEFAULT_INSTRUCTION = {default_instruction_js};
  const GENERATING = {generating_json};        // set on the page of a variant still being written
  const SCHEME = "{scheme}";
  const ORPHANS = {orphans};
  const DRAFT_ORPHANS = {draft_orphans};   // written text whose paragraph is gone — sent back on save, kept in the draft
  const DRAFT_NAME = {draft_name};
  const tree = document.getElementById('tree');
  const mdview = document.getElementById('mdview');
  const mdBtn = document.getElementById('mdBtn');
  const status = document.getElementById('status');
  const saveBtn = document.getElementById('saveBtn');
  // ---- key card: the usage notes, one per line, keys set as <kbd> ----
  {{
    const help = document.getElementById('help'), helpBtn = document.getElementById('helpBtn');
    const esc = t => t.replace(/[&<>]/g, c => ({{ '&':'&amp;', '<':'&lt;', '>':'&gt;' }})[c]);
    help.querySelector('ul').innerHTML = help.dataset.hint.split(' · ').filter(Boolean)
      .map(t => '<li>' + esc(t).replace(/⌘[A-Z]|Enter|Shift\\+Tab|\\bTab\\b/g, k => '<kbd>' + k + '</kbd>') + '</li>')
      .join('').replace(/\\((<kbd>[^<]*<\\/kbd>)\\)/g, '$1');
    const setHelp = open => {{ help.hidden = !open; helpBtn.setAttribute('aria-expanded', String(open)); }};
    helpBtn.addEventListener('click', () => setHelp(help.hidden));
    document.addEventListener('click', e => {{
      if (!help.hidden && !e.target.closest('#help') && !e.target.closest('#helpBtn')) setHelp(false);
    }});
    document.addEventListener('keydown', e => {{ if (e.key === 'Escape' && !help.hidden) setHelp(false); }});
  }}

  // ---- level switch: one mode per heading level, then paragraphs, then text ----
  // A bullet's level is its heading level (#, ##, ### → 1, 2, 3); a
  // non-heading bullet under a heading is a PARAGRAPH (one level below the
  // deepest heading used) and a non-heading bullet under a paragraph is that
  // paragraph's WRITTEN TEXT, one level further — the last mode, offered
  // whenever the outline has paragraphs. (An outline with no headings at all
  // falls back to nesting depth.) Mode N shows the bullets of level ≤ N and
  // REMOVES the rest from view — a branch whose children are all hidden
  // renders with a leaf dot instead of a caret — independent of the carets'
  // own open/closed state. Picking N opens every branch shallower than N, so
  // everything up to level N is actually on screen; the highest mode is the
  // paragraph level and hides nothing. Mode 1 is omitted when only one
  // bullet lives at level 1 (a lone title). Buttons are rebuilt after every
  // structural change (renumberChips) and the choice is remembered per file,
  // so the reload after Save comes back at the same level. A bullet being
  // edited is always shown; once staged, it follows its level.
  const levelBar = document.getElementById('levels');
  const LEVEL_KEY = 'multilevel-editor.level:' + SOURCE;
  let level = 0;                               // 0 = everything; N = levels 1..N only
  let activeLevel = 0;                         // the mode on screen (a variant is generated at this level)

  // ---- navigator: the outline as a descending tree in the left sidebar ----
  // Follows the level switch like a walk down a directory tree: in mode N
  // every node above level N is expanded and the level-N nodes sit collapsed,
  // so the top level shows the lone top node, level 2 opens it onto its
  // children, and so on. Written text (the level under a paragraph) is never
  // listed. A caret toggle is a manual override kept until the next level
  // change. Clicking a label scrolls the outline to that bullet — switching
  // to its level first when the current mode hides it. Rebuilt by syncLevels,
  // i.e. after every level change and every structural or text change.
  const nav = document.getElementById('nav');
  const navTree = document.getElementById('navTree');
  const navBtn = document.getElementById('navBtn');
  const navOverride = new Map();               // outline li -> open? (manual caret toggles)
  let navCurrent = null;                       // outline li last jumped to
  // every member of the family is drawn as a full tree; the open document's
  // nodes point at outline rows, the others' at ?doc=<name>&at=<index>
  function navNodesFromBullets(bullets, H) {{
    const root = {{ indent: -1, kids: [] }};
    const stack = [root];
    let index = 0, num = 0;
    bullets.forEach(b => {{
      while (stack[stack.length - 1].indent >= b.indent) stack.pop();
      const parent = stack[stack.length - 1];
      const m = /^(#{{1,6}})\\s+/.exec(b.raw);
      const h = m ? m[1].length : 0;
      const parentHeading = parent === root || parent.h > 0;
      const body = !h && !parentHeading;                       // written text: never listed
      const node = {{ indent: b.indent, h, body, kids: [], index: index++,
                     text: b.raw.replace(/^#{{1,6}}\\s+/, '').replace(/[*`]/g, ''),
                     num: !h && parentHeading && !b.raw.startsWith('[') ? ++num : 0 }};
      node.lvl = h || (H ? (body ? H + 2 : H + 1) : stack.length);
      if (!body) parent.kids.push(node);
      stack.push(node);
    }});
    return root.kids;
  }}
  function navNodesFromRows(rows, H) {{
    const info = new Map(rows.map(r => [r.li, r]));
    let index = 0;
    const walk = ul => ul ? [...ul.children].flatMap(li => {{
      const r = info.get(li);
      if (!r) return [];
      const i = index++;
      if (H && r.body) return [];
      const span = li.querySelector(':scope > .row .txt');
      const numEl = li.querySelector(':scope > .row > .main > .pnum');
      return [{{ li, h: r.h, lvl: r.lvl, index: i, kids: walk(li.querySelector(':scope > ul')),
                text: span.dataset.raw.replace(/^#{{1,6}}\\s+/, '').replace(/[*`]/g, ''),
                num: numEl ? numEl.textContent : 0 }}];
    }}) : [];
    return walk(tree);
  }}
  function renderNav(rows, max, H) {{
    const active = level && level < max ? level : max;
    function build(node, doc) {{
      const item = document.createElement('li');
      item.navLi = node.li || null; item.navDoc = doc.name; item.navAt = node.index;
      const key = node.li || doc.name + ':' + node.index;
      const row = document.createElement('div');
      row.className = 'nrow';
      const lead = document.createElement(node.kids.length ? 'button' : 'span');
      if (node.kids.length) {{ lead.type = 'button'; lead.className = 'ncaret'; lead.setAttribute('aria-label', 'toggle'); }}
      else lead.className = 'nspace';
      const label = document.createElement('button');
      label.type = 'button';
      label.className = 'nlabel ' + (node.h ? 'nh' + node.h : H ? 'npara' : 'nitem');
      if (node.num) {{
        const n = document.createElement('span');
        n.className = 'nnum'; n.textContent = node.num;
        label.append(n);
      }}
      label.append(node.text || '…');
      label.title = node.text;
      row.append(lead, label);
      item.append(row);
      if (node.kids.length) {{
        const manual = navOverride.get(key);
        item.classList.toggle('open', manual !== undefined ? manual
          : node.lvl < active && node.kids.some(k => k.lvl <= active));
        const ul = document.createElement('ul');
        node.kids.forEach(k => {{
          const it = build(k, doc);
          it.classList.toggle('deep', manual !== true && k.lvl > active);
          ul.append(it);
        }});
        item.append(ul);
      }}
      item.classList.toggle('current', !!node.li && node.li === navCurrent);
      return item;
    }}
    const suffixed = (item, suffix) => {{
      if (!item || !suffix) return;
      const sfx = document.createElement('span');
      sfx.className = 'nsuffix'; sfx.textContent = ' ' + suffix;
      item.querySelector(':scope > .nrow > .nlabel').append(sfx);
    }};
    const top = nav.scrollTop;
    const items = [];
    DOCS.forEach(doc => {{
      const roots = doc.current ? navNodesFromRows(rows, H)
                  : doc.pending ? [{{ text: doc.title, h: 1, lvl: 1, index: 0, kids: [], num: 0 }}]
                  : navNodesFromBullets(doc.bullets || [], H);
      const built = roots.map(n => build(n, doc));
      if (built[0]) {{
        suffixed(built[0], doc.suffix);
        built[0].classList.toggle('here', !!doc.current);
        built[0].querySelector(':scope > .nrow > .nlabel').title = doc.name;
      }}
      items.push(...built);
    }});
    navTree.replaceChildren(...items);
    nav.scrollTop = top;
  }}
  function revealBullet(li) {{
    if (!li || !li.isConnected) return;
    navCurrent = li;
    if (li.classList.contains('lvhide')) {{       // hidden at this level: descend to it
      const r = levelRows().rows.find(x => x.li === li);
      if (r) showLevels(r.lvl);
    }}
    for (let p = parentLiOf(li); p; p = parentLiOf(p)) p.classList.add('open');
    navTree.querySelectorAll('li').forEach(it => it.classList.toggle('current', it.navLi === li));
    const row = li.querySelector(':scope > .row');
    row.scrollIntoView({{ block: 'start', behavior: 'smooth' }});
    row.classList.remove('navflash'); void row.offsetWidth; row.classList.add('navflash');
    if (narrowNav.matches) setNav(false, false);
  }}
  navTree.addEventListener('click', e => {{
    const item = e.target.closest('li');
    if (!item) return;
    if (e.target.closest('.ncaret')) {{
      const open = !item.classList.contains('open');
      item.classList.toggle('open', open);
      navOverride.set(item.navLi || item.navDoc + ':' + item.navAt, open);
      if (open) item.querySelectorAll(':scope > ul > li.deep').forEach(k => k.classList.remove('deep'));
    }} else if (e.target.closest('.nlabel')) {{
      if (item.navLi) revealBullet(item.navLi);
      else location.href = '?doc=' + encodeURIComponent(item.navDoc) + '&at=' + item.navAt;   // another member: open it there
    }}
  }});
  {{   // arrived from another member's tree: jump to the node that was clicked
    const at = new URLSearchParams(location.search).get('at');
    if (at !== null) {{
      const li = [...tree.querySelectorAll('li')].filter(li => !li.classList.contains('pbody'))[+at];
      if (li) setTimeout(() => revealBullet(li), 60);
    }}
  }}
  // open beside the text on wide screens (remembered), as an overlay on narrow ones
  const NAV_KEY = 'multilevel-editor.nav';
  const narrowNav = matchMedia('(max-width: 900px)');
  function setNav(open, remember) {{
    document.body.classList.toggle('nav-open', open);
    fitMargin();
    navBtn.setAttribute('aria-expanded', String(open));
    if (remember) try {{ localStorage.setItem(NAV_KEY, open ? 'open' : 'closed'); }} catch {{}}
  }}
  navBtn.addEventListener('click', () =>
    setNav(!document.body.classList.contains('nav-open'), !narrowNav.matches));
  document.addEventListener('click', e => {{
    if (narrowNav.matches && document.body.classList.contains('nav-open') &&
        !e.target.closest('#nav') && !e.target.closest('#navBtn')) setNav(false, false);
  }});
  {{
    let stored = null;
    try {{ stored = localStorage.getItem(NAV_KEY); }} catch {{}}
    setNav(!narrowNav.matches && stored !== 'closed', false);
  }}
  // the sidebar starts under the sticky header, whose height wraps with the title
  {{
    const header = document.querySelector('header');
    const fit = () => document.documentElement.style.setProperty('--hdr-h', header.offsetHeight + 'px');
    fit();
    if (window.ResizeObserver) new ResizeObserver(fit).observe(header);
  }}
  function eachRow(fn) {{                      // fn(li, depth, hasChildren), depth from 1
    (function walk(ul, d) {{
      [...ul.children].forEach(li => {{
        const sub = li.querySelector(':scope > ul');
        fn(li, d, !!sub);
        if (sub) walk(sub, d + 1);
      }});
    }})(tree, 1);
  }}
  function levelRows() {{                      // [{{li, branch, lvl}}], max level, deepest heading
    const rows = [];
    let H = 0, anyPara = false;
    eachRow((li, d, branch) => {{
      const h = headingLevel(li);
      if (h > H) H = h;
      if (!h) anyPara = true;
      rows.push({{ li, d, branch, h, body: isBody(li) }});
    }});
    if (FAMILY) {{                             // a variant offers the same modes as its family
      H = Math.max(H, FAMILY.h || 0);
      anyPara = anyPara || !!FAMILY.para;
    }}
    let max = 0;
    rows.forEach(r => {{
      r.lvl = r.h || (H ? (r.body ? H + 2 : H + 1) : r.d);
      if (r.lvl > max) max = r.lvl;
    }});
    if (H && anyPara) max = Math.max(max, H + 2);   // the written-text level is always on offer
    return {{ rows, max, H }};
  }}
  const minLevel = rows => rows.filter(r => r.lvl <= 1).length === 1 ? 2 : 1;
  // one icon per mode: headings (and plain depths) as an outline of n
  // bulleted, stepped-in bars, the paragraph level as a pilcrow, written text as lines of prose
  function levelIcon(kind, n) {{
    let d, dots = '';
    if (kind === 'para') d = 'M13 4v16M17 4v16M19 4H9.5a4.5 4.5 0 0 0 0 9H13';
    else if (kind === 'text') d = 'M4 5h16M4 10h16M4 15h16M4 20h10';
    else {{
      // a bulleted staircase: each row a filled dot plus an equal-length bar, so
      // the left edges step in while the right edges don't line up (≠ right-aligned text)
      const k = Math.min(n, 6), gap = k > 1 ? Math.min(7, 16 / (k - 1)) : 0,
            top = 12 - gap * (k - 1) / 2, step = k > 1 ? Math.min(4, 6 / (k - 1)) : 0,
            len = 21 - (3 + (k - 1) * step) - 6;
      d = '';
      for (let i = 0; i < k; i++) {{
        const x = 3 + i * step, y = +(top + i * gap).toFixed(1);
        dots += '<circle cx="' + (x + 1) + '" cy="' + y + '" r="' + (k > 3 ? 1.4 : 1.8) +
                '" fill="currentColor" stroke="none"/>';
        d += 'M' + (x + 6) + ' ' + y + 'h' + len;
      }}
    }}
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" ' +
           'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + dots +
           '<path d="' + d + '"/></svg>';
  }}
  function showLevels(n, remember = true) {{
    const {{ rows, max }} = levelRows();
    n = Math.max(n, minLevel(rows));
    level = n >= max ? 0 : n;
    rows.forEach(r => {{ if (r.branch && r.lvl < n) r.li.classList.add('open'); }});
    navOverride.clear();                       // a level change resets the navigator's expansion
    if (remember) try {{ localStorage.setItem(LEVEL_KEY, level ? String(level) : 'all'); }} catch {{}}
    syncLevels();
  }}
  // rebuild the buttons when the levels change, re-apply the cut, and
  // highlight the active mode (the last button when nothing is cut)
  function syncLevels() {{
    const {{ rows, max, H }} = levelRows();
    const min = minLevel(rows);
    if (level && level < min) level = min;
    const want = max > min ? max - min + 1 : 0;   // nothing to switch below the minimum
    const sig = want ? min + '-' + max + '-' + H : '';
    if (levelBar.dataset.sig !== sig) {{
      levelBar.dataset.sig = sig;
      levelBar.innerHTML = '';
      for (let n = min; want && n <= max; n++) {{
        const b = document.createElement('button');
        const kind = !H || n <= H ? 'depth' : n === H + 1 ? 'para' : 'text';
        b.type = 'button'; b.dataset.level = n;
        b.innerHTML = levelIcon(kind, n);
        b.title = !H ? (n === max ? 'show all levels' : 'show the top ' + n + ' levels')
                : n <= H ? 'show headings down to level ' + n
                : n === H + 1 ? 'show the paragraphs' : 'show the fully written paragraphs';
        b.setAttribute('aria-label', b.title);
        levelBar.appendChild(b);
      }}
    }}
    const cut = level > 0 && level < max ? level : 0;
    rows.forEach(r => {{
      const para = H > 0 && !r.h && !r.body;          // a paragraph: align it to the paragraph column
      r.li.classList.toggle('para', para);
      if (para) r.li.style.setProperty('--shift', H + 1 - r.d); else r.li.style.removeProperty('--shift');
    }});
    rows.forEach(r => r.li.classList.toggle('lvhide', cut > 0 && r.lvl > cut));
    rows.forEach(r => r.li.classList.toggle('lvcut', r.branch &&
      [...r.li.querySelector(':scope > ul').children].every(c => c.classList.contains('lvhide'))));
    const active = cut || max;
    activeLevel = active;
    document.body.classList.toggle('toplevel', active === min);   // the variant button lives on the top level only
    [...levelBar.children].forEach(b => b.classList.toggle('on', +b.dataset.level === active));
    renderNav(rows, max, H);
  }}
  levelBar.addEventListener('click', e => {{
    const b = e.target.closest('button');
    if (b) showLevels(+b.dataset.level);
  }});
  {{
    let stored = null;
    try {{ stored = localStorage.getItem(LEVEL_KEY); }} catch {{}}
    if (stored === 'all') showLevels(99, false);
    else if (parseInt(stored, 10) > 0) showLevels(parseInt(stored, 10), false);
    else syncLevels();
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

  // Paragraph role tag: trailing ' {{X}}' (one letter, case-sensitive) on the
  // raw bullet. The letter's meaning comes from the tagging scheme selected
  // in the settings sidebar; letters outside the active scheme render muted.
  const SCHEMES = {{
    creac: {{ label: 'CREAC', letters: ['C', 'R', 'E', 'A'],
             titles: {{C: 'Conclusion', R: 'Rule', E: 'Explanation', A: 'Application'}} }},
    syllogism: {{ label: 'Syllogism', letters: ['M', 'm', 'C'],
             titles: {{M: 'Major premise — the rule', m: 'Minor premise — the facts',
                      C: 'Conclusion'}} }},
    subsumtion: {{ label: 'Subsumtion', letters: ['O', 'D', 'S', 'E'],
             titles: {{O: 'Obersatz — the issue framed as a rule',
                      D: 'Definition — the rule\\u2019s elements',
                      S: 'Subsumtion — the facts applied to the elements',
                      E: 'Ergebnis — the result'}} }},
  }};
  // the scheme is DOCUMENT metadata (persisted in the sidecar on Save);
  // only the tagging display toggle is a browser preference
  const settings = {{ tagging: true, scheme: SCHEMES[SCHEME] ? SCHEME : 'creac' }};
  try {{
    const stored = JSON.parse(localStorage.getItem('multilevel-editor.settings') || '{{}}');
    if (typeof stored.tagging === 'boolean') settings.tagging = stored.tagging;
  }} catch {{}}
  function saveSettings() {{
    try {{ localStorage.setItem('multilevel-editor.settings',
      JSON.stringify({{ tagging: settings.tagging }})); }} catch {{}}
  }}
  // trailing ' {{X}}' — the markdown view's inline projection of a tag
  function splitTag(raw) {{
    const m = raw.match(/^([\\s\\S]*?)\\s*\\{{([A-Za-z])\\}}\\s*$/);
    return m ? {{text: m[1], letter: m[2]}} : {{text: raw, letter: null}};
  }}
  function cycleTag(badge) {{
    const span = badge.closest('.main').querySelector('.txt');
    if (span.isContentEditable) return;
    const sch = SCHEMES[settings.scheme];
    const cur = span.dataset.tag || null;
    const i = cur === null ? -1 : sch.letters.indexOf(cur);
    // unknown letter (from another scheme) restarts the cycle at this scheme's first letter
    const next = i === -1 ? sch.letters[0]
               : i === sch.letters.length - 1 ? null
               : sch.letters[i + 1];
    span.dataset.tag = next || '';
    renumberChips();
    markDirty();
  }}

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
    s = s.replace(/\\[([^\\[\\]]{{1,80}})\\](?!\\()/g, '<span class="ph">[$1]</span>');
    const placeholder = !heading && t.startsWith('[') && t.trimEnd().endsWith(']');
    return {{ html: s, cls: heading ? 'h' + heading : placeholder ? 'item placeholder' : 'item' }};
  }}

  // ---- serialization: DOM -> bullets, bullets -> markdown/DOM ----
  function serialize() {{
    const bullets = [];
    (function walk(ul, depth) {{
      [...ul.children].forEach(li => {{
        const span = li.querySelector(':scope > .row .txt');
        if (span) bullets.push({{ indent: depth, raw: span.dataset.raw,
                                 tag: span.dataset.tag || null }});
        const sub = li.querySelector(':scope > ul');
        if (sub) walk(sub, depth + 1);
      }});
    }})(tree, 0);
    return bullets;
  }}
  // the markdown view is the full-fidelity projection: tags appear inline as
  // ' {{X}}' there (and only there — the file on disk stays clean)
  function toMarkdown(bullets) {{
    return bullets.map(b => '  '.repeat(b.indent) + '- ' + b.raw +
                            (b.tag ? ' {{' + b.tag + '}}' : '')).join('\\n');
  }}
  function parseMarkdown(text) {{
    const bullets = [];
    text.split('\\n').forEach(line => {{
      const m = line.match(/^(\\s*)- (.*)$/);
      if (m) {{
        const st = splitTag(m[2].trim());
        bullets.push({{ indent: Math.floor(m[1].length / 2), raw: st.text.trim(), tag: st.letter }});
      }} else if (line.trim() && bullets.length) {{
        bullets[bullets.length - 1].raw += ' ' + line.trim();
      }}
    }});
    return bullets;
  }}
  function buildTree(bullets) {{
    undoStack.length = redoStack.length = 0;   // positions no longer valid
    const root = {{ indent: -1, children: [] }};
    const stack = [root];
    bullets.forEach(b => {{
      const node = {{ indent: b.indent, raw: b.raw, tag: b.tag || null, children: [] }};
      while (stack[stack.length - 1].indent >= b.indent) stack.pop();
      stack[stack.length - 1].children.push(node);
      stack.push(node);
    }});
    function renderNode(n) {{
      const d = renderMd(n.raw);
      const grip = '<span class="grip" draggable="true" title="drag to move">⋮⋮</span>';
      const span = '<span class="txt ' + d.cls + '" data-raw="' + esc(n.raw) +
                   '" data-tag="' + esc(n.tag || '') + '">' + d.html + '</span></div>' +
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
  // ---- right-margin notes ----
  // A paragraph heading (numbered row) whose text holds a [cite] placeholder
  // — [cite], [cite: …] — gets a "Citation needed" badge in the right margin
  // (×n when there are several). Recomputed with the chips, and live while
  // the bullet is being edited.
  const CITE_RE = /\\[cite\\b[^\\]]*\\]/gi;
  function syncMargin(row, raw) {{
    const n = row.classList.contains('numbered') ? (raw.match(CITE_RE) || []).length : 0;
    let m = row.querySelector(':scope > .margin');
    if (!n) {{ if (m) m.remove(); return; }}
    if (!m) {{ m = document.createElement('div'); m.className = 'margin'; row.append(m); }}
    const label = 'Citation needed' + (n > 1 ? ' ×' + n : '');
    if (m.textContent !== label)
      m.innerHTML = '<span class="mnote cite" title="this paragraph has ' + n +
                    ' [cite] placeholder' + (n > 1 ? 's' : '') + '">' + label + '</span>';
  }}
  // hang the notes in the margin only when there is room beside the column
  function fitMargin() {{
    const col = document.querySelector('main').getBoundingClientRect();
    document.body.classList.toggle('margin-inline', window.innerWidth - (col.right - 24) < 170);
  }}
  window.addEventListener('resize', fitMargin);
  function renumberChips() {{
    let k = 0;
    (function walk(ul, parentHeading) {{
      [...ul.children].forEach(li => {{
        const span = li.querySelector(':scope > .row .txt');
        let isHeading = parentHeading;
        if (span) {{
          const raw = span.dataset.raw;
          isHeading = raw.startsWith('#');
          li.classList.toggle('pbody', !isHeading && !parentHeading);   // written text
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
            chip = null;
          }}
          span.closest('.row').classList.toggle('numbered', numbered);
          if (!span.isContentEditable) syncMargin(span.closest('.row'), raw);
          // numbered rows get the quill (generate written text) before the trash
          const act = li.querySelector(':scope > .row > .act');
          let gen = act && act.querySelector(':scope > .gen');
          if (numbered && act && !gen) {{
            gen = iconButton('gen', 'write this paragraph');
            act.insertBefore(gen, act.querySelector(':scope > .del'));
          }} else if (!numbered && gen) gen.remove();
          // role badge sits before the number chip: a letter when tagged,
          // an empty click target on numbered rows, nothing otherwise
          const letter = span.dataset.tag || null;
          let badge = li.querySelector(':scope > .row > .main > .creac');
          if (letter || numbered) {{
            if (!badge) {{
              badge = document.createElement('span');
              (chip || span).before(badge);
            }}
            const sch = SCHEMES[settings.scheme];
            const pos = letter ? sch.letters.indexOf(letter) : -1;
            badge.className = 'creac ' + (letter ? (pos >= 0 ? 'p-' + (pos + 1) : 'p-x') : 'empty');
            badge.textContent = letter || '';
            badge.dataset.tip = letter
              ? (sch.titles[letter] || letter + ' — not in ' + sch.label + '; click to retag')
              : 'tag paragraph role';
          }} else if (badge) {{
            badge.remove();
          }}
        }}
        const sub = li.querySelector(':scope > ul');
        if (sub) walk(sub, isHeading);
      }});
    }})(tree, true);
    tree.querySelectorAll('li').forEach(li =>
      li.classList.toggle('written', !!li.querySelector(':scope > ul > li.pbody')));
    syncLevels();
  }}

  // nesting rules — returns an error message, or null when `li` may become a
  // child of `parentLi` (null = root level):
  //   * under a paragraph (non-heading bullet) only its written text may
  //     nest: a childless non-heading bullet; nothing nests under that
  //   * a heading may only nest under a heading of a shallower level
  //     (## under #, ### under ##, never # under ## or ## under ##)
  // (function declarations: the level switch above uses them at load time)
  function headingLevel(li) {{
    const span = li && li.querySelector(':scope > .row .txt');
    const m = span && /^(#{{1,6}})\\s/.exec(span.dataset.raw);
    return m ? m[1].length : 0;
  }}
  function parentLiOf(li) {{ return li.parentElement === tree ? null : li.parentElement.closest('li'); }}
  // a paragraph's written text: a non-heading bullet under a non-heading bullet
  function isBody(li) {{
    const p = parentLiOf(li);
    return !!p && !headingLevel(li) && !headingLevel(p);
  }}
  function nestError(li, parentLi) {{
    if (!parentLi) return null;
    const plvl = headingLevel(parentLi);
    const lvl = headingLevel(li);
    if (!plvl) {{
      if (isBody(parentLi)) return 'nothing nests under a written paragraph';
      if (lvl) return 'a heading cannot go under a paragraph';
      if (li.querySelector(':scope > ul')) return 'a bullet with children cannot become written text';
      return null;
    }}
    if (lvl && lvl <= plvl) return 'a heading can only go under a shallower heading';
    return null;
  }}
  // drops never land inside a paragraph: written text is made by indenting
  // (Tab) or in the markdown view, and moves only with its paragraph
  const dropError = (li, parentLi) =>
    parentLi && !headingLevel(parentLi) ? 'no drops inside a paragraph' : nestError(li, parentLi);

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

  // ---- generate a paragraph's written text (quill button) ----
  // Sends the whole staged outline plus the target paragraph to the server,
  // which asks the model for the paragraph's prose; the reply comes back as a
  // new written-text bullet under the paragraph (staged, undoable, saved
  // with everything else on Save). Existing written text is kept below it.
  async function generateFor(li) {{
    const gen = li.querySelector(':scope > .row > .act > .gen');
    if (!gen || gen.classList.contains('busy')) return;
    if (document.activeElement && document.activeElement.isContentEditable)
      document.activeElement.blur();               // stage a pending inline edit first
    const span = li.querySelector(':scope > .row .txt');
    const path = [];
    for (let p = parentLiOf(li); p; p = parentLiOf(p)) path.unshift(p.querySelector(':scope > .row .txt').dataset.raw);
    const existing = [...li.querySelectorAll(':scope > ul > li.pbody > .row .txt')].map(t => t.dataset.raw);
    gen.classList.add('busy'); gen.title = 'writing…';
    flash('writing the paragraph…');
    try {{
      const r = await fetch('/generate', {{ method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ doc: DOC, outline: toMarkdown(serialize()), target: span.dataset.raw, path, existing }}) }});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.status);
      const text = (data.text || '').replace(/\\s+/g, ' ').trim();
      if (!text) throw new Error('the model returned no text');
      if (!li.isConnected) throw new Error('the paragraph was removed meanwhile');
      const nb = freshBullet();
      delete nb.dataset.pendingNew;
      const t = nb.querySelector('.txt');
      t.dataset.raw = text;
      const d = renderMd(text); t.innerHTML = d.html; t.className = 'txt ' + d.cls;
      toBranch(li);
      li.querySelector(':scope > ul').prepend(nb);
      recordChange(nb, null, posOf(nb), 'generate');
      showLevels(99);                              // make sure the written level is on screen
      renumberChips(); markDirty();
      flash(existing.length ? 'written · earlier text kept below it · ⌘Z to undo' : 'written · ⌘Z to undo');
    }} catch (err) {{
      flash('could not write the paragraph: ' + err.message, 'error');
    }} finally {{
      gen.classList.remove('busy'); gen.title = 'write this paragraph';
    }}
  }}

  // ---- a variant being written: follow /variant/status and grow the tree live ----
  if (GENERATING) {{
    const banner = document.getElementById('genBanner');
    banner.hidden = false;
    document.getElementById('genSub').textContent =
      'Variant ' + GENERATING.n + ' of ' + GENERATING.src + ' — ' + GENERATING.instruction;
    const count = document.getElementById('genCount');
    let shown = 0, lastKey = '';
    const poll = async () => {{
      try {{
        const r = await fetch('/variant/status?name=' + encodeURIComponent(GENERATING.name));
        const st = await r.json();
        if (!r.ok) throw new Error(st.error || r.status);
        const key = st.lines.length + ':' + st.chars;
        if (st.lines.length && key !== lastKey) {{
          lastKey = key;
          buildTree(parseMarkdown(st.lines.join('\\n')));
          showLevels(99, false);
          [...tree.querySelectorAll('li')].slice(shown).forEach(li => li.classList.add('fresh'));
          shown = st.lines.length;
        }}
        count.textContent = st.lines.length ? st.lines.length + ' bullets · ' + st.chars + ' characters' : 'waiting for the first words…';
        if (st.status === 'done') {{
          document.body.classList.add('gen-done');
          document.getElementById('genTitle').textContent = 'Written — opening the file…';
          setTimeout(() => location.reload(), 600);
          return;
        }}
        if (st.status === 'error') {{
          document.body.classList.add('gen-failed');
          document.getElementById('genTitle').textContent = 'The variant could not be written';
          const err = document.getElementById('genErr');
          err.hidden = false;
          err.innerHTML = esc(st.error || 'unknown error') + ' · <a href="?doc=' +
            encodeURIComponent(GENERATING.src) + '">back to ' + esc(GENERATING.src) + '</a>';
          return;
        }}
      }} catch (err) {{
        count.textContent = 'lost contact with the server: ' + err.message;
      }}
      setTimeout(poll, 600);
    }};
    tree.querySelector('.txt.h1')?.classList.add('placeholder-title');
    poll();
  }}

  // ---- skeleton variant: instruction dialog → /variant → open the new file ----
  const variantDlg = document.getElementById('variantDlg');
  const variantPrompt = document.getElementById('variantPrompt');
  const variantErr = document.getElementById('variantErr');
  document.getElementById('variantBtn').addEventListener('click', () => {{
    if (dirty) {{ flash('save your changes first — a variant is built from the file on disk', 'error'); return; }}
    variantErr.textContent = ''; variantDlg.classList.remove('busy');
    variantDlg.showModal(); variantPrompt.focus();
  }});
  document.getElementById('variantCancel').addEventListener('click', () => variantDlg.close());
  async function makeVariant(instruction) {{
    const buttons = variantDlg.querySelectorAll('button');
    buttons.forEach(b => b.disabled = true); variantDlg.classList.add('busy');
    variantErr.textContent = 'starting…';
    try {{
      const r = await fetch('/variant', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ doc: DOC, instruction, level: activeLevel }}) }});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.status);
      location.href = '?doc=' + encodeURIComponent(data.name);   // the new page shows it being written
    }} catch (err) {{
      variantDlg.classList.remove('busy');
      variantErr.textContent = 'could not generate the variant: ' + err.message;
      buttons.forEach(b => b.disabled = false);
    }}
  }}
  document.getElementById('variantDefault').addEventListener('click', () => makeVariant(DEFAULT_INSTRUCTION));
  document.getElementById('variantGo').addEventListener('click', () =>
    makeVariant(variantPrompt.value.trim() || DEFAULT_INSTRUCTION));
  variantDlg.addEventListener('cancel', e => {{ if (variantDlg.classList.contains('busy')) e.preventDefault(); }});

  // ---- delegated events (survive tree rebuilds) ----
  tree.addEventListener('click', e => {{
    const caret = e.target.closest('.caret');
    if (caret) {{ caret.closest('li').classList.toggle('open'); return; }}
    const badge = e.target.closest('.creac');
    if (badge) {{ if (EDITABLE) cycleTag(badge); return; }}
    const del = e.target.closest('.del');
    if (del) {{ if (EDITABLE) askDelete(del.closest('.row')); return; }}
    const gen = e.target.closest('.gen');
    if (gen) {{ if (EDITABLE) generateFor(gen.closest('li')); return; }}
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
    const onInput = () => syncMargin(span.closest('.row'), span.textContent);
    span.addEventListener('input', onInput);
    const cleanup = () => {{ done = true; span.contentEditable = 'false';
      span.classList.remove('editing');
      span.removeEventListener('input', onInput);
      span.removeEventListener('keydown', onKey); span.removeEventListener('blur', onBlur); }};
    const cancel = () => {{ cleanup(); span.innerHTML = prev;
      if (span.isConnected) syncMargin(span.closest('.row'), span.dataset.raw);
      const liEl = span.closest('li');
      if (liEl && liEl.dataset.pendingNew) {{ liEl.remove(); renumberChips(); }} }};
    const stage = () => {{
      if (done) return;
      const val = span.textContent.replace(/\\n+/g, ' ').trim();
      if (!val || val === span.dataset.raw) {{ cancel(); return; }}
      // a trailing ' {{X}}' typed inline becomes the bullet's tag
      const st = splitTag(val);
      span.dataset.raw = st.text.trim();
      if (st.letter) span.dataset.tag = st.letter;
      cleanup();
      const d = renderMd(span.dataset.raw);
      span.innerHTML = d.html;
      span.className = 'txt ' + d.cls;
      const liEl = span.closest('li');
      if (liEl && liEl.dataset.pendingNew) {{     // a bullet born from insert-between
        delete liEl.dataset.pendingNew;
        recordChange(liEl, null, posOf(liEl), 'insert');
        flash('inserted · ⌘Z to undo');
      }}
      renumberChips();                              // heading-ness may have changed
      if (liEl && liEl.classList.contains('lvhide'))
        flash('staged, but hidden at this level — pick a deeper level to see it', 'error');
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
    parentLi && isBody(parentLi) ? 'nothing nests under a written paragraph' : null;
  tree.addEventListener('mousemove', e => {{
    if (!EDITABLE || dragLi || e.buttons) {{ hideInsert(); return; }}   // no hint mid-press/drag
    // editing a bullet does NOT suppress the hint: the plus stays reachable, and
    // its mousedown blurs (stages) the open edit before the new bullet opens
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
  // Clicking the plus while a bullet is being edited: the mousedown blurs
  // (stages) that edit before the click lands. If the edit was an empty
  // pending bullet it is removed by then — and it may be the very target the
  // hint pointed at — so snapshot its neighbours on mousedown and fall back to
  // them when the target is no longer in the tree.
  let insertAnchor = null;
  insertHint.addEventListener('mousedown', () => {{
    if (!insertTarget) return;
    const t = insertTarget.li;
    insertAnchor = {{ prev: t.previousElementSibling, parent: t.parentElement }};
  }});
  insertHint.addEventListener('click', () => {{
    if (!insertTarget) return;
    const li = freshBullet();
    const t = insertTarget.li;
    if (t.isConnected) {{
      if (insertTarget.where === 'before') t.before(li); else t.after(li);
    }} else if (insertAnchor && insertAnchor.prev && insertAnchor.prev.isConnected) {{
      insertAnchor.prev.after(li);
    }} else if (insertAnchor && insertAnchor.parent && insertAnchor.parent.isConnected) {{
      insertAnchor.parent.prepend(li);
    }} else {{
      hideInsert(); return;
    }}
    insertAnchor = null;
    hideInsert();
    openBullet(li);
  }});

  // drag-to-move — pure DOM; chips recompute; synced on Save
  let dragLi = null;
  const clearMarks = () => tree.querySelectorAll('.drop-before,.drop-after')
    .forEach(r => r.classList.remove('drop-before', 'drop-after'));
  tree.addEventListener('dragstart', e => {{
    const g = e.target.closest('.grip');
    if (!g || !EDITABLE || isBody(g.closest('li'))) {{ e.preventDefault(); return; }}
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
  // The drop target is resolved by HEIGHT, not by what is under the pointer:
  // the row the pointer is over, else the nearest visible row by vertical
  // distance (within a small band). So a drag released in the indent gutter or
  // the page margin, level with a row, still lands beside that row — and the
  // handlers sit on document so the margins outside #tree count too.
  const DROP_SNAP = 40;
  function dropRowAt(e) {{
    const direct = e.target instanceof Element && e.target.closest('.row');
    if (direct && tree.contains(direct)) return direct;
    let best = null, bestD = Infinity;
    for (const r of tree.querySelectorAll('.row')) {{
      if (r.offsetParent === null) continue;
      const b = r.getBoundingClientRect();
      const d = e.clientY < b.top ? b.top - e.clientY : e.clientY > b.bottom ? e.clientY - b.bottom : 0;
      if (d < bestD) {{ bestD = d; best = r; }}
    }}
    return bestD <= DROP_SNAP ? best : null;
  }}
  const overNav = e => e.target instanceof Element && !!e.target.closest('#nav');
  document.addEventListener('dragover', e => {{
    if (!dragLi) return;
    if (overNav(e)) {{ clearMarks(); return; }}   // the sidebar is not a drop margin
    const row = dropRowAt(e);
    if (!row) {{ clearMarks(); return; }}
    const li = row.closest('li');
    if (li === dragLi || dragLi.contains(li)) {{ clearMarks(); return; }}
    if (dropError(dragLi, parentLiOf(li))) {{ clearMarks(); return; }}   // breaks nesting rules
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearMarks();
    const r = row.getBoundingClientRect();
    row.classList.add(e.clientY < r.top + r.height / 2 ? 'drop-before' : 'drop-after');
  }});
  document.addEventListener('drop', e => {{
    if (!dragLi || overNav(e)) return;
    const row = dropRowAt(e);
    if (!row) return;
    const li = row.closest('li');
    if (li === dragLi || dragLi.contains(li)) return;
    const err = dropError(dragLi, parentLiOf(li));
    if (err) {{ flash(err, 'error'); return; }}
    e.preventDefault();
    const r = row.getBoundingClientRect();
    const where = e.clientY < r.top + r.height / 2 ? 'before' : 'after';
    clearMarks();
    const before = posOf(dragLi);
    if (where === 'before') li.before(dragLi); else li.after(dragLi);
    recordMove(dragLi, before);
    renumberChips();
    markDirty();
  }});

  // ---- light/dark toggle (header, left of Save) ----
  // no stored choice = follow the OS; a click pins the opposite of what is showing,
  // and a pin that lands back on the OS theme is dropped so the page tracks the OS again
  const THEME_KEY = 'multilevel-editor.theme';
  const themeBtn = document.getElementById('themeBtn');
  const osDark = matchMedia('(prefers-color-scheme: dark)');
  function applyTheme() {{
    const pinned = document.documentElement.dataset.theme;
    const dark = pinned ? pinned === 'dark' : osDark.matches;
    document.documentElement.classList.toggle('theme-dark', dark);
    const label = dark ? 'switch to light mode' : 'switch to dark mode';
    themeBtn.title = label; themeBtn.setAttribute('aria-label', label);
  }}
  themeBtn.addEventListener('click', () => {{
    const next = document.documentElement.classList.contains('theme-dark') ? 'light' : 'dark';
    const follow = (next === 'dark') === osDark.matches;
    if (follow) delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    try {{ follow ? localStorage.removeItem(THEME_KEY) : localStorage.setItem(THEME_KEY, next); }} catch {{}}
    applyTheme();
  }});
  osDark.addEventListener('change', applyTheme);
  applyTheme();

  // ---- settings sidebar (top-right menu) ----
  const sidebar = document.getElementById('sidebar');
  const menuBtn = document.getElementById('menuBtn');
  const setTagging = document.getElementById('setTagging');
  const schemeGroup = document.getElementById('schemeGroup');
  function applySettings() {{
    document.body.classList.toggle('notags', !settings.tagging);
    setTagging.checked = settings.tagging;
    schemeGroup.disabled = !settings.tagging;
    document.querySelectorAll('input[name="scheme"]')
      .forEach(r => {{ r.checked = r.value === settings.scheme; }});
    renumberChips();
  }}
  menuBtn.addEventListener('click', () => document.body.classList.toggle('sidebar-open'));
  document.getElementById('sbClose').addEventListener('click',
    () => document.body.classList.remove('sidebar-open'));
  document.addEventListener('click', e => {{
    if (document.body.classList.contains('sidebar-open') &&
        !e.target.closest('#sidebar') && !e.target.closest('#menuBtn'))
      document.body.classList.remove('sidebar-open');
  }});
  document.addEventListener('keydown', e => {{
    if (e.key === 'Escape') document.body.classList.remove('sidebar-open');
  }});
  setTagging.addEventListener('change', () => {{
    settings.tagging = setTagging.checked;
    saveSettings(); applySettings();
  }});
  document.querySelectorAll('input[name="scheme"]').forEach(r =>
    r.addEventListener('change', () => {{
      // scheme is document metadata: staged now, written to the sidecar on Save
      if (r.checked) {{ settings.scheme = r.value; applySettings(); markDirty(); }}
    }}));
  applySettings();   // reinterpret server-rendered badges per settings
  {{
    const n = Object.keys(DRAFT_ORPHANS).length;
    if (n) flash(n + ' written paragraph' + (n > 1 ? 's' : '') + ' no longer match' + (n > 1 ? '' : 'es') +
                 ' a topic sentence — kept at the end of ' + DRAFT_NAME, 'error');
  }}
  if (ORPHANS) flash(ORPHANS + ' orphaned tag' + (ORPHANS > 1 ? 's' : '') +
                     ' in the sidecar (paragraph text changed) — dropped on next save', 'error');

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
        body: JSON.stringify({{ doc: DOC, hash: FILEHASH, scheme: settings.scheme, bullets,
                                draft_orphans: DRAFT_ORPHANS }}) }});
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


GENERATE_PROMPT = """You are writing one paragraph of a document from its outline.

The outline below is a nested bullet list: heading bullets start with #, ## or
###; the bullets under a heading are paragraphs, each given by its topic
sentence; a bullet nested under a paragraph is that paragraph's written text.

Write the full text of ONE paragraph: the one whose topic sentence is marked
>>> TARGET <<< in the outline. Requirements:
- One paragraph of prose, in the language of the outline. No heading, no
  bullet, no label, no quotation marks around the whole thing, no commentary.
- Open with the topic sentence's point (you may polish its wording) and develop
  it so the paragraph reads naturally between the neighbouring paragraphs.
- Use only what the outline states or clearly implies. Do not invent facts,
  names, dates, figures or citations. Where the paragraph needs a fact or a
  source the outline does not supply, keep or insert a bracketed placeholder
  such as [cite] or [fact: what is needed].
- Keep any bracketed placeholders from the topic sentence, and keep quoted
  passages verbatim.
- Match the register and tone of the outline.

Reply with the paragraph text only.

OUTLINE
{outline}

TARGET TOPIC SENTENCE
{target}
{existing}"""


def build_generate_prompt(req: dict) -> str:
    target = req.get("target", "").strip()
    outline = req.get("outline", "")
    marked = []
    done = False
    for line in outline.splitlines():
        body = re.sub(r" \{[A-Za-z]\}$", "", line.strip())   # ignore a projected role tag
        if not done and body == "- " + target:
            line = line + "   >>> TARGET <<<"
            done = True
        marked.append(line)
    existing = [e for e in req.get("existing", []) if e.strip()]
    ex = ""
    if existing:
        ex = ("\nEXISTING WRITTEN TEXT (a fresh version is wanted; you may reuse what is good)\n"
              + "\n".join("- " + e for e in existing))
    return GENERATE_PROMPT.format(outline="\n".join(marked), target=target, existing=ex)


def generate_text(prompt: str, model: str, cwd: Path, on_text=None) -> str:
    """Ask the model and return its text. `on_text(accumulated)` is called as
    the reply streams in. Backends, in order: the `anthropic` SDK when it is
    installed and has credentials; otherwise the `claude` CLI (Claude Code) on
    PATH, run tool-less and non-interactively with partial messages streamed."""
    try:
        import anthropic  # optional — the tool itself stays stdlib-only
    except ImportError:
        anthropic = None
    if anthropic is not None and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        client = anthropic.Anthropic()
        parts = []
        with client.messages.stream(model=model, max_tokens=32000,
                                    messages=[{"role": "user", "content": prompt}]) as stream:
            for chunk in stream.text_stream:
                parts.append(chunk)
                if on_text:
                    on_text("".join(parts))
            final = stream.get_final_message()
        if final.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        return "".join(parts)
    cli = shutil.which("claude")
    if not cli:
        raise RuntimeError("no model backend: install the `claude` CLI, or `pip install anthropic` "
                           "and set ANTHROPIC_API_KEY")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}   # allow nesting inside a session
    proc = subprocess.Popen(
        [cli, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
         "--model", model, "--tools", "", "--no-session-persistence"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(cwd), env=env,
    )
    proc.stdin.write(prompt)
    proc.stdin.close()
    parts, full, result_error = [], None, None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        kind = obj.get("type")
        if kind == "stream_event":
            ev = obj.get("event", {})
            if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                parts.append(ev["delta"].get("text", ""))
                if on_text:
                    on_text("".join(parts))
        elif kind == "assistant":
            full = "".join(b.get("text", "") for b in obj.get("message", {}).get("content", [])
                           if b.get("type") == "text") or full
        elif kind == "result":
            if obj.get("is_error"):
                result_error = str(obj.get("result") or "claude CLI reported an error")
            elif not parts and not full and obj.get("result"):
                full = str(obj["result"])
    proc.wait(timeout=600)
    if result_error:
        raise RuntimeError(result_error[-400:])
    if proc.returncode != 0 and not (parts or full):
        raise RuntimeError((proc.stderr.read() or "claude CLI failed").strip()[-400:])
    return ("".join(parts) if parts else (full or "")).strip()


# ---------- the draft: written paragraphs live beside the outline, not in it ----------
# The outline (skeleton) holds structure only: headings and paragraph topic
# sentences. A paragraph's written text — level 5 in the editor — is a DRAFT
# and is stored in `<outline>.draft.md`: the whole outline with the written
# text nested under each paragraph, readable as a document. On load the text
# is re-attached to its paragraph by a content fingerprint of the topic
# sentence; written text whose paragraph no longer exists is an orphan,
# reported on load and kept at the end of the draft under an
# "Orphaned draft text" heading (never silently dropped).
ORPHAN_HEADING = "# Orphaned draft text"


def draft_path(source: Path) -> Path:
    return source.with_name(source.stem + ".draft.md")


def is_heading_raw(raw: str) -> bool:
    return bool(re.match(r"^#{1,6}\s", raw))


def load_draft(source: Path) -> dict:
    """fingerprint(topic sentence) -> {"para": raw, "texts": [written...]}"""
    p = draft_path(source)
    if not p.exists():
        return {}
    written = {}

    def walk(nodes, parent):
        for n in nodes:
            raw = split_tag(n["raw"])[0].strip()
            if parent is not None and not is_heading_raw(parent) and not is_heading_raw(raw):
                entry = written.setdefault(fingerprint(parent), {"para": parent, "texts": []})
                entry["texts"].append(raw)
            walk(n["children"], raw)

    walk(parse_outline(p.read_text(encoding="utf-8")), None)
    return written


def partition_bullets(bullets):
    """Split saved bullets into skeleton lines (structure) and draft lines
    (structure + written text). A bullet is written text when it and its
    parent are both non-headings."""
    stack = []          # (indent, is_heading)
    skeleton, draft = [], []
    for b in bullets:
        indent, raw = b["indent"], b["raw"]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        heading = is_heading_raw(raw)
        written = bool(stack) and not stack[-1][1] and not heading
        line = "  " * indent + "- " + raw
        draft.append(line)
        if not written:
            skeleton.append(line)
        stack.append((indent, heading))
    return skeleton, draft


def write_draft(source: Path, draft_lines, orphans: dict) -> None:
    lines = [
        "---",
        f"summary: Draft of {source.name} — the outline with each paragraph's written text nested under its topic sentence. Maintained by multilevel-editor; structure is edited in the skeleton, the prose here.",
        f"source: {source.name}",
        "status: draft",
        "---",
        "",
    ] + list(draft_lines)
    if orphans:
        lines += ["", "- " + ORPHAN_HEADING]
        for entry in orphans.values():
            lines.append("  - " + entry["para"])
            lines += ["    - " + t for t in entry["texts"]]
    draft_path(source).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- skeleton variants: sibling files, each with its own draft ----------
# A variant of the skeleton X.md is X.variant-N.md beside it (its tags and
# draft follow the same rule: X.variant-N.tags.yaml, X.variant-N.draft.md).
# The server is started on the base skeleton and serves the whole family;
# the navigator lists the family as its top level and `?doc=<name>` opens a
# member. A variant is produced by the model from the skeleton on disk plus
# an instruction, validated as an outline, and written with frontmatter that
# records its provenance and the instruction used.
VARIANT_RE = re.compile(r"\.variant-(\d+)\.md$")
DEFAULT_VARIANT_INSTRUCTION = (
    "Rebuild the skeleton in a different legal argumentation style: lead with the strongest "
    "point, organise the sections by the rule each breach engages rather than by chronology, "
    "and make every paragraph's topic sentence a single assertion a reader could accept on its own. "
    "Keep every fact and every [cite] placeholder."
)
VARIANT_SCOPE = """
SCOPE — ONLY THE TOP {level} LEVEL(S)
The variant was requested at the level of the {hashes} headings. Output ONLY
the heading bullets down to that level: the # title and the headings marked
with up to {level} hash marks. Do not output paragraphs or deeper headings —
they will be developed later under your new structure. Every deeper point of
the original must still have an obvious home under one of your headings; a
heading may end with a short lead sentence, as the original's headings do,
saying what will be argued there.
"""
VARIANT_PROMPT = """You are producing a VARIANT of a document skeleton (a reverse outline).

A skeleton is a nested bullet list: '- ' bullets, two spaces of indentation per
level; heading bullets start with #, ## or ###; the bullets under a heading are
the paragraphs, each given as its topic sentence; a bracketed bullet such as
[parties block: ...] is a placeholder and stays as it is.

INSTRUCTION FOR THE VARIANT
{instruction}
{scope}
RULES
- Same subject, same facts, same evidence, same language as the original. Use
  only what the skeleton states; keep every bracketed placeholder such as
  [cite] with the sentence it belongs to; invent nothing.
- Every paragraph of the original keeps a counterpart, merged or split as the
  instruction requires; never drop content silently.
- Output the COMPLETE variant skeleton in exactly the original format, with
  the single top-level # title bullet (rephrasing allowed). No written text
  under paragraphs, no numbering labels.
- Reply with the bullet list only: no preamble, no code fence, no commentary.

ORIGINAL SKELETON
{skeleton}
"""


def deepest_heading(skeleton: str) -> int:
    return max((len(m.group(1)) for m in re.finditer(r"^\s*- (#{1,6})\s", skeleton, re.M)), default=0)


def scope_for(level, skeleton: str) -> tuple:
    """(scope paragraph for the prompt, max heading level to keep) — a level
    at or below the deepest heading restricts the variant to headings down to
    that level; the paragraph level and below mean the whole skeleton."""
    H = deepest_heading(skeleton)
    try:
        level = int(level or 0)
    except (TypeError, ValueError):
        level = 0
    if not level or not H or level > H:
        return "", 0
    return VARIANT_SCOPE.format(level=level, hashes="#" * level), level


def restrict_levels(lines, max_heading: int):
    """Keep only heading bullets of at most `max_heading` hashes; the model
    is told the same, this makes it certain."""
    if not max_heading:
        return list(lines)
    kept = []
    for line in lines:
        m = re.match(r"^(\s*)- (#{1,6})\s", line)
        if m and len(m.group(2)) <= max_heading:
            kept.append(line)
    return kept


def strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1:]).strip("\n")
    return text


def read_frontmatter(text: str) -> dict:
    """Flat 'key: value' lines of the frontmatter (block scalars skipped)."""
    fm = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return fm
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m and m.group(2).strip() not in ("", "|", ">"):
            fm[m.group(1)] = m.group(2).strip()
    return fm


def family_of(base: Path):
    """The base skeleton and its variants, in variant order."""
    pat = re.compile(r"^" + re.escape(base.stem) + r"\.variant-(\d+)\.md$")
    found = []
    for p in base.parent.iterdir():
        m = pat.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return [base] + [p for _, p in sorted(found)]


def doc_label(p: Path, base: Path) -> str:
    fm = read_frontmatter(p.read_text(encoding="utf-8"))
    if fm.get("title"):
        return fm["title"]
    m = VARIANT_RE.search(p.name)
    return f"Variant {m.group(1)}" if m else base.stem


def parse_variant_reply(text: str, strict: bool = True):
    """Keep the bullet lines of the model's reply, normalise indentation to
    two spaces per level, and (strict) insist on an outline that starts with
    a heading. Lenient mode serves the live view while the reply streams."""
    raw = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            continue
        m = re.match(r"^(\s*)[-*] (.+)$", line.rstrip())
        if m:
            raw.append((len(m.group(1).replace("\t", "    ")), m.group(2).strip()))
        elif raw and line.strip() and not line.lstrip().startswith(("-", "*")):
            raw[-1] = (raw[-1][0], raw[-1][1] + " " + line.strip())   # continuation line
    if not raw:
        if strict:
            raise RuntimeError("the model returned no bullets")
        return []
    levels = {ind: i for i, ind in enumerate(sorted({ind for ind, _ in raw}))}
    lines = ["  " * levels[ind] + "- " + txt for ind, txt in raw]
    if strict:
        tree = parse_outline("\n".join(lines))
        if not tree or not is_heading_raw(tree[0]["raw"]):
            raise RuntimeError("the model did not return a skeleton (it must start with a # heading bullet)")
    return lines


def variant_path(base: Path, n: int) -> Path:
    return base.with_name(f"{base.stem}.variant-{n}.md")


def write_variant(base: Path, derived_from: Path, instruction: str, lines, n: int, level: int = 0) -> Path:
    path = variant_path(base, n)
    short = re.sub(r"\s+", " ", instruction).strip()
    fm = ["---",
          f"summary: Skeleton variant {n} of {base.name} — {short[:140]}",
          f"variant_of: {derived_from.name}",
          f"variant: {n}",
          f"title: Variant {n}",
          f"level: {level or 'all'}",
          "prompt: |"] + ["  " + l for l in instruction.strip().splitlines()] + [
          f"created: {date.today().isoformat()}",
          "status: draft",
          "---", ""]
    path.write_text("\n".join(fm + list(lines)) + "\n", encoding="utf-8")
    return path


def combined_hash(source: Path) -> str:
    """Conflict guard spans the outline AND its sidecars (tags, draft)."""
    parts = [source.read_text(encoding="utf-8")]
    for side in (tags_path(source), draft_path(source)):
        parts.append(side.read_text(encoding="utf-8") if side.exists() else "")
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def doc_title(p: Path) -> str:
    """The outline's own title: its first bullet, heading marks stripped."""
    for line in strip_frontmatter(p.read_text(encoding="utf-8")).splitlines():
        m = BULLET_RE.match(line)
        if m:
            return re.sub(r"^#{1,6}\s+", "", m.group(2).strip()).replace("*", "") or p.stem
    return p.stem


def family_levels(base: Path) -> dict:
    """The level shape shared by the whole family, so a variant that holds
    only headings still offers the base's modes (counts, paragraphs, text)."""
    H, para = 0, False
    for p in family_of(base):
        body = strip_frontmatter(p.read_text(encoding="utf-8"))
        H = max(H, deepest_heading(body))
        if re.search(r"^\s*- (?!#)", body, re.M):
            para = True
    return {"h": H, "para": para}


def family_docs(base: Path, current: str, jobs=None):
    """The skeleton and its variants as the navigator lists them: each by its
    own title, variants with a suffix — all on the outline's top level."""
    docs = []
    for p in family_of(base):
        m = VARIANT_RE.search(p.name)
        bullets = []
        if p.name != current:                 # the navigator draws the others' trees from this

            def flat(nodes):
                for n in nodes:
                    bullets.append({"indent": n["indent"], "raw": split_tag(n["raw"])[0].strip()})
                    flat(n["children"])

            flat(parse_outline(p.read_text(encoding="utf-8")))
        docs.append({"name": p.name, "label": doc_label(p, base), "title": doc_title(p),
                     "suffix": f"(variant {m.group(1)})" if m else "",
                     "current": p.name == current, "pending": False, "bullets": bullets})
    names = {d["name"] for d in docs}
    for name, job in (jobs or {}).items():
        if name not in names and job.status != "done":
            docs.append({"name": name, "label": f"Variant {job.n} (generating…)",
                         "title": f"Variant {job.n}", "suffix": "(generating…)",
                         "current": name == current, "pending": True})
    return docs


def build_page(source: Path, editable: bool, base: Path = None, docs=None) -> str:
    base = base or source
    if docs is None:
        docs = family_docs(base, source.name) if editable else [
            {"name": source.name, "label": source.stem, "current": True, "pending": False}]
    text = source.read_text(encoding="utf-8")
    meta = load_meta(source)
    tree = parse_outline(text)
    if not tree:
        sys.exit(f"error: no '- ' bullets found in {source}")

    used = set()
    draft = load_draft(source)
    draft_used = set()

    def attach(nodes, parent_heading=True):
        for n in nodes:
            clean, inline = split_tag(n["raw"])
            n["raw"] = clean.strip()
            fp = fingerprint(n["raw"])
            if fp in meta["tags"]:
                n["tag"] = meta["tags"][fp]
                used.add(fp)
            else:
                n["tag"] = inline   # legacy inline tag — migrates to the sidecar on save
            heading = is_heading_raw(n["raw"])
            attach(n["children"], heading)
            if parent_heading and not heading and fp in draft:      # a paragraph: hang its draft text under it
                draft_used.add(fp)
                n["children"] += [{"indent": n["indent"] + 1, "raw": t, "tag": None, "children": []}
                                  for t in draft[fp]["texts"]]

    attach(tree)
    orphans = sum(1 for k in meta["tags"] if k not in used)
    draft_orphans = {k: v for k, v in draft.items() if k not in draft_used}
    assign_numbers(tree)
    body = "\n".join(render_node(n) for n in tree)
    return PAGE.format(
        title=doc_label(source, base) if source != base else source.stem,
        source=source.name,
        doc_js=json.dumps(source.name),
        docs_json=json.dumps(docs),
        family_json=json.dumps(family_levels(base) if editable else None),
        generating_json="null",
        default_instruction=html.escape(DEFAULT_VARIANT_INSTRUCTION, quote=True),
        default_instruction_js=json.dumps(DEFAULT_VARIANT_INSTRUCTION),
        source_js=json.dumps(source.name),
        tree=body,
        editable="true" if editable else "false",
        bodycls="" if editable else "readonly",
        dark_vars=DARK_VARS,
        filehash=combined_hash(source),
        scheme=meta["scheme"],
        orphans=orphans,
        draft_orphans=json.dumps(draft_orphans),
        draft_name=json.dumps(draft_path(source).name),
        hint="click to edit · Enter adds a bullet below · drag to move · hover between bullets to insert · letter badge tags the paragraph role · quill writes the paragraph (draft) · trash to delete · ⌘Z undoes · Markdown for raw view · Save (⌘S) writes the skeleton and the draft"
        if editable else "",
    )


def build_generating_page(job, base: Path, docs) -> str:
    """The page for a variant that is still being written: read-only, a
    banner, and a tree the page fills in live from /variant/status."""
    placeholder = {"indent": 0, "raw": f"# Variant {job.n} — being written…", "tag": None, "children": [], "num": None}
    return PAGE.format(
        title=f"Variant {job.n}",
        source=job.name,
        doc_js=json.dumps(job.name),
        docs_json=json.dumps(docs),
        family_json=json.dumps(family_levels(base)),
        generating_json=json.dumps({"name": job.name, "n": job.n, "src": job.src.name,
                                    "instruction": job.instruction}),
        source_js=json.dumps(job.name),
        tree=render_node(placeholder),
        editable="false",
        bodycls="readonly generating",
        dark_vars=DARK_VARS,
        filehash="",
        scheme="creac",
        orphans=0,
        draft_orphans="{}",
        draft_name=json.dumps(""),
        default_instruction=html.escape(DEFAULT_VARIANT_INSTRUCTION, quote=True),
        default_instruction_js=json.dumps(DEFAULT_VARIANT_INSTRUCTION),
        hint="",
    )


class VariantJob:
    """One variant being generated in the background."""

    def __init__(self, name, n, src, instruction, level=0):
        self.name, self.n, self.src, self.instruction, self.level = name, n, src, instruction, level
        self.status, self.text, self.error = "running", "", None

    def snapshot(self):
        complete = self.text.rsplit("\n", 1)[0] if "\n" in self.text else ""
        return {"status": self.status, "n": self.n, "src": self.src.name, "instruction": self.instruction,
                "lines": restrict_levels(parse_variant_reply(complete, strict=False), self.level),
                "chars": len(self.text), "error": self.error}


def serve(source: Path, port: int, open_browser: bool = True, model: str = "claude-opus-5"):
    base = source
    jobs = {}                    # variant name -> VariantJob (running, done, or failed)
    jobs_lock = threading.Lock()

    def start_variant(src: Path, instruction: str, level) -> VariantJob:
        skeleton = strip_frontmatter(src.read_text(encoding="utf-8"))
        scope, max_heading = scope_for(level, skeleton)
        with jobs_lock:
            taken = [int(VARIANT_RE.search(p.name).group(1)) for p in family_of(base)[1:]]
            taken += [j.n for j in jobs.values()]
            n = 1 + max(taken or [0])
            job = VariantJob(variant_path(base, n).name, n, src, instruction, max_heading)
            jobs[job.name] = job

        def run():
            try:
                prompt = VARIANT_PROMPT.format(instruction=instruction, skeleton=skeleton, scope=scope)

                def on_text(t):
                    job.text = t

                reply = generate_text(prompt, model, src.parent, on_text=on_text)
                job.text = reply
                lines = restrict_levels(parse_variant_reply(reply), max_heading)
                if not lines:
                    raise RuntimeError("the model returned no headings at the requested level")
                write_variant(base, src, instruction, lines, n, max_heading)
                job.status = "done"
                print(f"  variant: {job.name} from {src.name}", flush=True)
            except Exception as e:  # noqa: BLE001 — surfaced to the page via /variant/status
                job.status, job.error = "error", str(e)
                print(f"  variant failed: {job.name}: {e}", flush=True)

        threading.Thread(target=run, daemon=True).start()
        return job

    def resolve(name):
        """A member of the family by file name — never an arbitrary path."""
        if not name:
            return base
        for p in family_of(base):
            if p.name == name:
                return p
        raise FileNotFoundError(f"{name} is not the skeleton or one of its variants")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/variant/status":
                job = jobs.get(q.get("name", [""])[0])
                if not job:
                    self._send(404, json.dumps({"error": "no such generation"}), "application/json")
                    return
                self._send(200, json.dumps(job.snapshot()), "application/json")
                return
            if url.path in ("/", "/index.html"):
                name = q.get("doc", [""])[0]
                try:
                    doc = resolve(name)
                except FileNotFoundError as e:
                    job = jobs.get(name)
                    if job and job.status != "done":
                        self._send(200, build_generating_page(job, base, family_docs(base, name, jobs)))
                        return
                    self._send(404, str(e), "text/plain")
                    return
                self._send(200, build_page(doc, editable=True, base=base, docs=family_docs(base, doc.name, jobs)))
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path == "/variant":
                # starts the generation in the background and answers at once
                # with the new file's name; the page navigates there and
                # follows /variant/status while the model writes
                try:
                    req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    src = resolve(req.get("doc"))
                    instruction = (req.get("instruction") or "").strip() or DEFAULT_VARIANT_INSTRUCTION
                    job = start_variant(src, instruction, req.get("level"))
                    self._send(200, json.dumps({"name": job.name}), "application/json")
                except Exception as e:  # noqa: BLE001 — report any failure to the client
                    self._send(500, json.dumps({"error": str(e)}), "application/json")
                return
            if self.path == "/generate":
                try:
                    req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    source = resolve(req.get("doc"))
                    text = generate_text(build_generate_prompt(req), model, source.parent)
                    self._send(200, json.dumps({"text": text}), "application/json")
                    print(f"  wrote: {req.get('target', '')[:60]!r} ({len(text)} chars)")
                except Exception as e:  # noqa: BLE001 — report any generation failure to the client
                    self._send(500, json.dumps({"error": str(e)}), "application/json")
                return
            if self.path != "/save":
                self._send(404, "not found", "text/plain")
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                source = resolve(req.get("doc"))
                if combined_hash(source) != req["hash"]:
                    self._send(409, json.dumps(
                        {"error": "file changed on disk — refresh the page (your staged edits will be lost)"}),
                        "application/json")
                    return
                current = source.read_text(encoding="utf-8")
                cur_lines = current.splitlines(keepends=True)
                fm = []
                if cur_lines and cur_lines[0].strip() == "---":
                    fm.append(cur_lines[0])
                    for line in cur_lines[1:]:
                        fm.append(line)
                        if line.strip() == "---":
                            break
                bullets, tagged = [], []
                for b in req["bullets"]:
                    clean = split_tag(b["raw"].strip())[0].strip()   # belt & braces: outline stays tag-free
                    bullets.append({"indent": b["indent"], "raw": clean})
                    if b.get("tag"):
                        tagged.append((clean, b["tag"]))
                scheme = req.get("scheme")
                if scheme not in SCHEME_NAMES:
                    scheme = "creac"
                # structure goes to the skeleton; structure + written text to the draft
                skeleton, draft_lines = partition_bullets(bullets)
                out = "".join(fm) + ("\n" if fm else "") + "\n".join(skeleton) + "\n"
                source.write_text(out, encoding="utf-8")
                write_meta(source, scheme, tagged)
                orphans = req.get("draft_orphans") or {}
                n_written = len(draft_lines) - len(skeleton)
                if n_written or orphans or draft_path(source).exists():
                    write_draft(source, draft_lines, orphans)
                self._send(200, json.dumps({"ok": True}), "application/json")
                print(f"  saved: {len(skeleton)} bullets, {len(tagged)} tags, "
                      f"{n_written} written paragraphs -> {draft_path(source).name}", flush=True)
            except Exception as e:  # noqa: BLE001 — report any save failure to the client
                self._send(400, json.dumps({"error": str(e)}), "application/json")

        def log_message(self, *args):  # quiet
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"serving {source} at {url}  (staged edits write on Save; Ctrl-C to stop)")
    if open_browser:
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
    ap.add_argument("--no-browser", action="store_true",
                    help="don't open a browser tab (for server restarts)")
    ap.add_argument("--model", default="claude-opus-5",
                    help="model for the quill button (paragraph writing); default claude-opus-5")
    args = ap.parse_args()

    if args.serve:
        serve(args.source, args.port, open_browser=not args.no_browser, model=args.model)
        return
    out = args.output or args.source.with_suffix(".html")
    out.write_text(build_page(args.source, editable=False), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
