---
name: multilevel-editor
description: View and edit any nested-bullet outline .md in the browser — collapsible tree, click-to-edit bullets, drag-to-move, changes staged and written back to the .md on Save. Use whenever the user wants to "open the outline", view an outline as HTML, collapse/expand bullets, or edit an outline interactively with changes syncing to the file. Fully self-contained — the script (outline_to_html.py) lives beside this SKILL.md (static export + editable --serve mode). Works on any '- ' bullet outline with 2-space indents.
---

# Multilevel editor

Browser view for any nested-bullet outline `.md` (2-space indents, `- `
bullets): a collapsible tree, and — in serve mode — in-place editing where
changes are staged and written back to the `.md` when the user presses Save.
Backed by `outline_to_html.py`, which lives **beside this SKILL.md** (stdlib
only, inline CSS/JS, no build step).

## Commands

```bash
# static export — writes <outline>.html beside the source, read-only
python3 <skill-dir>/outline_to_html.py <outline.md>

# editable — local server + browser tab; staged edits sync to the .md on Save
python3 <skill-dir>/outline_to_html.py <outline.md> --serve [--port 8383]
```

Run `--serve` in the background and give the user the URL
(`http://127.0.0.1:<port>/`). The script opens a browser tab itself.

## Behavior (serve mode)

**Changes are STAGED in the browser and written only when the user presses the
Save button (top right).** The button sits inactive until something changes,
then lights up; closing the tab with unsaved changes triggers a browser
warning. Save serializes the whole outline from the page and rewrites the
`.md` in one shot (frontmatter preserved), then reloads.

- **Click a bullet** → its text becomes editable in place (same font/position;
  a hairline outline + tint marks edit mode). **Enter or click away stages the
  edit** (re-rendered locally); **Esc cancels**. While editing, **Tab
  indents** the bullet (last child of its previous sibling, subtree included)
  and **Shift+Tab outdents** it (parent's next sibling); both keep edit mode
  and caret position, and flash a notice when not possible.
- **Written paragraphs — the level under a paragraph.** A non-heading bullet
  nested under a paragraph is that paragraph's written text: no grip, dot or
  number of its own, it moves only with its paragraph (drops never land
  inside a paragraph), and nothing nests under it. Create it with Tab under
  the paragraph above, or in the markdown view. The paragraph over it
  renders semibold.
- **Paragraph numbers are computed by the UI, never stored in the file.** A
  bullet gets a small sequential number chip (replacing its dot) when it sits
  directly under a heading bullet and is neither a heading (`#…`) nor a
  bracketed placeholder (`[…]`). Chips recompute on every
  reorder/edit/rebuild; the markdown view and the `.md` contain plain bullets
  with no number labels.
- **Quill button — write the paragraph.** Numbered rows carry a quill next to
  the trash (hover). It posts the staged outline with the paragraph marked to
  `/generate`; the server asks the model (the `anthropic` SDK with
  `ANTHROPIC_API_KEY` if installed, else the `claude` CLI on PATH, tool-less,
  `--model`, default `claude-opus-5`) for one paragraph that uses only the
  outline's content and keeps `[cite]`-style placeholders, then inserts it as
  written text under the paragraph (staged, ⌘Z undoes, Save writes; earlier
  written text stays below). The view jumps to the written level.
- **Drag to move**: a grip (`⋮⋮`) appears left of a bullet on hover; dragging
  it moves the bullet **with its whole subtree** (indicator line shows the
  before/after drop position; the bullet adopts the target's indent). Chips
  recompute live. Dropping into a bullet's own subtree is refused.
- **Markdown view**: the header's Markdown button swaps the tree for a raw
  markdown textarea holding the current staged state; edit freely — including
  adding, deleting, and re-indenting bullets — then toggle back to Outline
  (the tree rebuilds, chips recompute) or Save directly. Only the outline body
  is shown; frontmatter stays untouched.
- **Collapse/expand** via the caret on branch bullets; the **level switch**
  centred at the bottom of the header has one mode per heading level, then
  the paragraph level, then the written-text level (`2` = `#`/`##` headings,
  `3` = plus `###`, `4` = paragraphs, `5` = fully written paragraphs — the
  bullets nested under each paragraph; `1` omitted for a lone title). Bullets below the
  chosen level are removed from view, not collapsed (branches with all
  children hidden show a dot; a bullet being edited always shows), and the
  choice is remembered per file across reloads. Text selection inside a
  bullet triggers edit mode — Esc backs out without saving.
- **Conflict safety**: the page carries a hash of the file it was built from;
  if the file changed on disk in the meantime, Save is refused (409) rather
  than clobbering — refresh and redo (staged changes are lost). Reloading
  re-reads the file, so external edits appear on refresh.
- **Normalisation on save**: multi-line bullets (continuation lines) are
  joined to a single line; exactly the bullets' text is saved — no number
  labels are added or maintained.

## Server lifecycle

- One server per file. Default port 8383; pick another with `--port` if busy.
- Restart after changing the script (the running process serves old code):
  `lsof -ti:<port> | xargs kill`, then relaunch.
- Stop when done the same way; nothing persists beyond the `.md` itself.

## Caveats

- **Edits go to the outline `.md` only** — never to whatever source document
  the outline was derived from. Carrying an outline change back into a source
  document is a separate, deliberate step.
- **The UI's number chips are positional** — they shift on any reorder and
  make no claim about any external numbering scheme.
- The static `.html` export goes stale the moment the `.md` changes;
  regenerate it, or use serve mode, which re-reads on every load.
