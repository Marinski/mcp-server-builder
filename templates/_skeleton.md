# Skeleton fragments shared by all templates

These reusable fragments appear across the existing templates (02, 03, 06).
New templates should compose from these pieces rather than re-deriving them.

---

## Fragment 1 — Document header

Every template starts with an H1 containing the document title and the project
variable, followed by a one-line subtitle that states scope or purpose.

```markdown
# <Title> — {{PROJECT}}

<One-line subtitle describing scope, purpose, or source.>
```

Variants observed:
- `{{PROJECT}}` — used by 02 and 03.
- `{{SERVER}}` — used by 06 (review template); acceptable when the
  document is scoped to the server rather than the project as a whole.

---

## Fragment 2 — Key-value metadata block (optional)

Some templates carry a compact metadata block between the subtitle and the
first section.  Use only when the document has a small fixed set of
properties to declare up front.

```markdown
- **Key:** value
- **Key:** value
- **Key:** value
```

Observed in 03 (Spec revision, SDK, Status, Conflicts).

---

## Fragment 3 — Section heading

H2 headings label logical parts of the document.  Numbered sections
(e.g. `## 1. Context`) are appropriate when the document has a prescribed
reading order; unnumbered headings work for enumerations or checklists.

```markdown
## Section title

Prose or sub-structure follows.
```

---

## Fragment 4 — Table

Tables are the primary structured-data format.  Every table uses the same
Markdown syntax.

```markdown
| Col A | Col B | Col C |
|---|---|---|
| | | |
```

Convention: the first column is an ID or label; subsequent columns carry
details.  Leave body rows empty as placeholders for the template consumer
to fill in.

---

## Fragment 5 — Open questions

Templates that need maintainer input end with a numbered-questions section.
The list always starts at `1.` and each item is answerable inline.

```markdown
## Open questions

Numbered, answerable, blocking-first.  Answer inline before the next stage
reads this file.

1.
```

Observed in 02 ("Part F — Open questions for the maintainer") and
03 ("Risks and open questions").  Use in any template that requires
human decisions before proceeding.
