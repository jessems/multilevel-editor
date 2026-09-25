# multilevel-editor

View and edit nested-bullet markdown outlines in the browser. One Python file,
standard library only, no build step, no dependencies.

Run it npx-style with [uv](https://docs.astral.sh/uv/) — no install, always the
latest published version:

```bash
uvx --refresh --from git+https://github.com/jessems/multilevel-editor multilevel-editor outline.md --serve
```

(Drop `--refresh` to reuse uv's cache offline; pin a version with
`git+…@<tag-or-sha>`.) Or just run the file — it's stdlib-only:

```bash
python3 outline_to_html.py examples/demo.md              # static HTML export (read-only)
python3 outline_to_html.py examples/demo.md --serve      # editable, served locally
```

Serve mode opens a browser tab. Changes are **staged in the page** and written
back to the `.md` only when you press **Save**.

## Features

- **Collapsible tree** — carets on branch bullets, Expand-all / Collapse-all.
- **Reading typography** — a book-like serif at a comfortable measure, with
  a dark palette that follows the system colour scheme.
- **Click to edit in place** — the bullet's raw markdown, same font and
  position as the rendered view. Enter or click away stages the edit; Esc
  cancels.
- **Enter continues the outline** — staging a non-empty bullet with Enter
  opens a new bullet directly underneath it (first child of an open branch,
  else next sibling). Shift+Enter stages without creating one; Enter on an
  empty bullet just exits.
- **Tab / Shift+Tab while editing** — indent under the previous sibling /
  outdent to after the parent, subtree included, caret preserved.
- **Drag to move** — a grip appears on hover; dragging moves the bullet with
  its whole subtree, with a drop indicator above/below the target.
- **Insert between** — hovering the boundary between two bullets shows a line
  with a plus; clicking it creates a new bullet there and opens it for
  editing. Leaving it empty (or Esc) cancels the insert.
- **Delete** — a trash icon appears at the right of a bullet on hover; clicking
  it swaps in a check and a cross. The check removes the bullet with its whole
  subtree, the cross (or Esc) cancels.
- **Undo / redo** — Cmd/Ctrl+Z reverses the last move (drag, Tab, Shift+Tab),
  delete, or insert; Cmd/Ctrl+Shift+Z redoes it. Text edits keep the browser's
  own undo while editing.
- **Automatic paragraph numbering** — bullets sitting directly under a heading
  bullet (and not themselves headings or `[bracketed placeholders]`) get a
  small sequential number chip. Numbers are **computed by the UI, never stored
  in the file**, and recompute live on every reorder.
- **Paragraph role tagging** — a one-letter badge before the paragraph number
  marks the paragraph's argumentative role; clicking it (or the faint circle
  that appears on hover) cycles through the active scheme's letters and back
  to unset, with a black tooltip naming each role. Toggle tagging and pick the
  scheme in the **settings sidebar** (☰, top right; persisted in
  localStorage):
  - **CREAC** (Neumann) — **C**onclusion · **R**ule · **E**xplanation ·
    **A**pplication
  - **Syllogism** (Scalia & Garner) — **M**ajor premise (the rule) ·
    **m**inor premise (the facts) · **C**onclusion
  - **Subsumtion** (Gutachtenstil) — **O**bersatz · **D**efinition ·
    **S**ubsumtion · **E**rgebnis

  Roles live in a **sidecar metadata document**, `<outline>.tags.yaml`, keyed
  by a content fingerprint (sha256[:8]) of each bullet's text and carrying the
  paragraph quote as a comment — the outline itself stays clean:

  ```yaml
  # paragraph roles for outline.md — maintained by multilevel-editor
  scheme: creac
  tags:
    8f3a21c4: R   # "The two companies are separate legal…"
  ```

  The sidecar (including the scheme, which is document metadata) is rewritten
  on every save, so reorders and in-editor edits keep tags attached; a bullet
  edited *outside* the editor orphans its tag (reported on load, dropped on
  the next save). The markdown view projects tags inline as ` {X}` for bulk
  editing, and legacy inline tags in old files migrate to the sidecar on the
  first save. Badge hues follow the letter's position in the scheme; a letter
  not in the active scheme renders muted, and clicking it retags into the
  current scheme.
- **Nesting rules** — nothing can be nested under a non-heading bullet via
  the tree view (paragraphs are the deepest level an operation may create),
  and a heading can only be moved under a heading of a shallower level, so a
  `#` never lands beneath a `##` and a `##` never beneath another `##`.
- **Markdown view** — a header toggle swaps the tree for a raw-markdown
  textarea of the current staged state: bulk-edit, add, delete, re-indent
  bullets, then toggle back or Save directly.
- **Save with conflict safety** — the Save button or **Cmd/Ctrl+S** writes the
  staged outline back; the page carries a hash of the file it was built from,
  and if the file changed on disk the save is refused instead of clobbering.
  Closing the tab with unsaved changes prompts a warning.

## File format

- `- ` bullets, 2-space indentation per level.
- Optional YAML frontmatter is preserved verbatim (never shown or edited).
- Bullets support minimal inline markdown: `**bold**`, `*italic*`,
  `` `code` ``, and `#`/`##`/`###` prefixes for heading bullets.
- Multi-line bullets (continuation lines) are joined to a single line on save.

## Agent skill

`SKILL.md` documents the tool as an agent skill (for Claude Code and
compatible harnesses): drop this folder into `.claude/skills/` — or copy
`SKILL.md` and `outline_to_html.py` into a skill folder — and the tool becomes
invocable as `/multilevel-editor`.

## Requirements

Python ≥ 3.9. Nothing else.

## Provenance

Extracted from a legal-drafting workflow where reverse outlines of long
documents needed to be inspected, restructured, and synced back to markdown.
The tool itself is fully generic.

## License

MIT — see [LICENSE](LICENSE).
