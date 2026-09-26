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

- **Citation needed** — a paragraph heading (numbered bullet) whose text
  holds a `[cite]` placeholder (`[cite]`, `[cite: …]`) gets a quiet
  *Citation needed* note (quote-mark icon, muted text) in the right margin, level with its first line
  (*×n* for several). It appears live as you type the placeholder and goes
  when you remove it; when the margin is too narrow (sidebar open, small
  window) it sits at the end of the row instead. Display only — nothing is
  written to the file.
- **Navigator** — a left sidebar (toggle: panel icon, top left) listing the
  outline as a tree that **descends with the level switch**, like walking
  down a directory: in mode N every node above level N is expanded and the
  level-N nodes sit collapsed, so the top mode shows the lone title, the
  next opens it onto its sections, and so on (paragraphs straight under a
  `##` stay out of a heading view). Carets expand a node by hand — showing
  all its children — until the next level change. Clicking a label scrolls
  the outline to that bullet and flashes it, switching to its level first
  when the current mode hides it. Written text is never listed. Open beside
  the text on wide screens (remembered), an overlay under 900px. Works in
  the static export too.
- **Collapsible tree** — carets on branch bullets, plus a **level switch**
  centred in the header with one mode per **heading level**
  the outline uses (icon: that many bulleted, stepped-in bars), then one for the
  paragraphs (¶), then one for the written text (lines of prose): `2`
  shows `#` and `##` headings, `3` adds the `###` headings, `4`
  the paragraphs (topic sentences), and the highest number the fully
  written paragraphs — the text nested under each paragraph bullet. A
  non-heading bullet under a heading is a paragraph, a non-heading bullet
  under a paragraph is its written text, wherever they sit in the tree, so
  a heading-only view never mixes in paragraphs (an outline with no
  headings falls back to nesting depth).
  The `1` mode is omitted when a lone title is all that lives at level 1.
  Bullets below the chosen level are **removed from view**, not merely
  collapsed — a branch whose children are all hidden shows a leaf dot, not a
  caret — while carets keep working among the visible bullets. The buttons
  follow the outline, the chosen level is remembered per file so the reload
  after Save comes back at the same level, and a bullet being edited is
  always shown: once staged it follows its level, with a notice if that
  hides it.
- **Reading typography** — a book-like serif at a comfortable measure, with
  a dark palette that follows the system colour scheme; the sun/moon button
  in the header flips light/dark (also in read-only exports), remembered per
  browser — flipping back to the system's scheme returns to following it.
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
  its whole subtree, with a drop indicator above/below the target. The target
  is picked by height: releasing in the indent gutter or the page margin,
  level with a row, drops beside that row.
- **Insert between** — hovering the boundary between two bullets shows a line
  with a plus; clicking it creates a new bullet there and opens it for
  editing. Leaving it empty (or Esc) cancels the insert. The hint also works
  while another bullet is being edited: clicking the plus stages that edit
  first, then opens the new bullet.
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
  scheme in the **settings sidebar** (cog, top right):
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
- **Written paragraphs live in a draft file, not in the outline** — the
  text of a paragraph shows as a child bullet of its topic sentence in the
  editor (the paragraph reads as a semibold heading over it), but the
  outline `.md` keeps structure only. On Save the written text goes to
  `<outline>.draft.md`: the whole outline with each paragraph's text nested
  under its topic sentence, readable as a document and marked
  `status: draft`. On load the text is re-attached by a content fingerprint
  of the topic sentence; text whose paragraph has disappeared (the outline
  was edited outside) is reported and kept at the end of the draft under an
  "Orphaned draft text" heading, never dropped. Written text belongs to its
  paragraph: no drag handle, dot or number, it moves only with its
  paragraph, and nothing nests under it. Make it with the quill, by
  indenting a bullet under the paragraph above (Tab), or in the markdown
  view (which shows structure and text together).
- **Write the paragraph (quill button)** — every numbered paragraph has a
  quill next to its trash icon (hover the row). Clicking it sends the whole
  staged outline, with that paragraph marked, to a model and inserts the
  reply as the paragraph's written text — staged like any edit, undoable
  with Cmd/Ctrl+Z, written to the file on Save; existing written text is
  kept below the new version. The prompt asks for one paragraph in the
  outline's language that uses only what the outline states, keeps bracketed
  placeholders such as `[cite]`, and invents nothing. Backend, in order: the
  `anthropic` SDK if it is installed and `ANTHROPIC_API_KEY` is set;
  otherwise the `claude` CLI (Claude Code) on PATH, run tool-less and
  non-interactively in the outline's directory. Model: `--model` (default
  `claude-opus-5`). Serve mode only.
- **Skeleton variants (button top right of the text)** — "Skeleton
  variant…" opens a small dialog: type an instruction (the example in the
  box, e.g. rebuilding the argument in a different legal argumentation
  style, is what **Use default settings** runs) and **Generate**. The model
  rewrites the skeleton on disk to that instruction — same facts, same
  placeholders, same language — **at the level you are viewing**: invoked on
  the section level, the variant holds only the title and section headings
  (deeper levels are developed later under the new structure); invoked at
  the paragraph level it is the whole skeleton. The page moves to the new document at once
  and **shows it being written**: a banner with a pulse and a sliding bar,
  the outline filling in bullet by bullet as the model's reply streams in
  (via the `claude` CLI's partial messages or the SDK's text stream), then a
  reload onto the finished file. The result is saved as a **new file beside
  the original**, `<outline>.variant-N.md`, with frontmatter recording what
  it derives from and the instruction used. The original is untouched.
  Each variant is a skeleton in its own right, with its own `.draft.md` and
  `.tags.yaml`. In the **navigator** the family shares the
  outline's top level: this document's title row carries a suffix such as
  "(variant 1)", and the other members sit beside it as links (`?doc=<name>`)
  labelled by their own titles. The level switch is sized by the whole
  family, so a variant that so far holds only headings still offers the
  base's modes. Serve mode only; needs
  the same model backend as the quill; unsaved edits must be saved first.
- **Nesting rules** — under a paragraph only its written text may nest (a
  childless non-heading bullet); drops never land inside a paragraph; and a
  heading can only be moved under a heading of a shallower level, so a `#`
  never lands beneath a `##` and a `##` never beneath another `##`.
- **Markdown view** — a header toggle swaps the tree for a raw-markdown
  textarea of the current staged state: bulk-edit, add, delete, re-indent
  bullets, then toggle back or Save directly.
- **One-row header** — document name and file on the left, the level switch
  centred, Markdown / help / light-dark / Save / settings on the right; status messages
  drop below the bar as a toast, and the **?** button opens a key card with
  the editing gestures and shortcuts.
- **Save with conflict safety** — the Save button or **Cmd/Ctrl+S** writes the
  staged outline back; the page carries a hash of the file it was built from,
  and if the file changed on disk the save is refused instead of clobbering.
  Closing the tab with unsaved changes prompts a warning.

## File format

- `- ` bullets, 2-space indentation per level. The outline holds headings and
  paragraph topic sentences; written paragraph text is stored beside it in
  `<outline>.draft.md` (see Written paragraphs) and never in the outline.
- A skeleton's family beside it: `<outline>.variant-N.md` (variants, each
  with its own `.draft.md` / `.tags.yaml`), `<outline>.tags.yaml`,
  `<outline>.draft.md`. Start the server on the base skeleton and it serves
  the whole family.
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

Python ≥ 3.9. Nothing else — except for the quill button, which needs either
the `claude` CLI on PATH or `pip install anthropic` plus `ANTHROPIC_API_KEY`.

## Provenance

Extracted from a legal-drafting workflow where reverse outlines of long
documents needed to be inspected, restructured, and synced back to markdown.
The tool itself is fully generic.

## License

MIT — see [LICENSE](LICENSE).
