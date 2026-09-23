# multilevel-editor

View and edit nested-bullet markdown outlines in the browser. One Python file,
standard library only, no build step, no dependencies.

```bash
python3 outline_to_html.py examples/demo.md              # static HTML export (read-only)
python3 outline_to_html.py examples/demo.md --serve      # editable, served locally
```

Serve mode opens a browser tab. Changes are **staged in the page** and written
back to the `.md` only when you press **Save**.

## Features

- **Collapsible tree** — carets on branch bullets, Expand-all / Collapse-all.
- **Click to edit in place** — the bullet's raw markdown, same font and
  position as the rendered view. Enter or click away stages the edit; Esc
  cancels.
- **Tab / Shift+Tab while editing** — indent under the previous sibling /
  outdent to after the parent, subtree included, caret preserved.
- **Drag to move** — a grip appears on hover; dragging moves the bullet with
  its whole subtree, with a drop indicator above/below the target.
- **Automatic paragraph numbering** — bullets sitting directly under a heading
  bullet (and not themselves headings or `[bracketed placeholders]`) get a
  small sequential number chip. Numbers are **computed by the UI, never stored
  in the file**, and recompute live on every reorder.
- **Depth cap** — nothing can be nested under a non-heading bullet via the
  tree view; paragraphs are the deepest level an operation may create.
- **Markdown view** — a header toggle swaps the tree for a raw-markdown
  textarea of the current staged state: bulk-edit, add, delete, re-indent
  bullets, then toggle back or Save directly.
- **Save with conflict safety** — the page carries a hash of the file it was
  built from; if the file changed on disk, Save is refused instead of
  clobbering. Closing the tab with unsaved changes prompts a warning.

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
