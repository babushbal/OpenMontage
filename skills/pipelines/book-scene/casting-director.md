# Casting Director — Book Scene Pipeline

## Job
Build or extend the book-level `character_bible` so every character who will be on screen has
a **locked, approved identity**. This is what makes characters consistent; take it seriously.

## 1. Read before you cast
- Load the existing bible: `character_bible` `{"operation": "load", "book_id": ...}`.
- Find every passage that describes each character's looks (face, hair, age, build, skin,
  clothing, scars, glasses, posture). Collect them as **verbatim** `source_quotes` with page.
  For long books, search `text.txt` for the character's name and aliases instead of reading all of it.
- `physical_description` (English) may only contain traits that are quoted, shown in an
  illustration, or neutral defaults implied by era/setting. Never contradict a quote.
- Fill `visual_style` once per book: era, place, medium (e.g. "painterly 2D like the book's
  watercolour illustrations" or "photoreal period drama"), palette, lighting — with quotes.
  If the book has illustrations, the adaptation's style should follow them.

## 2. Match book illustrations to characters
For each image in `book_source.images`, open it and read its `context` (caption / nearby text).
Decide which character it shows. Only accept a match when the caption names the character or
the picture unambiguously fits the text. Record matches in `book_image_ids`.

## 3. Choose the identity source — in this priority
1. `user_supplied` — the user gave a picture of the character. Always wins.
2. `book_illustration` — the clearest, most frontal illustration of that character. Crop to the
   character if the picture has several people (a quick PIL crop script is fine), save, then
   `import_reference` with `slot: frontal`, `origin: book_illustration`.
3. `generated_from_text` — no picture exists. Generate one frontal character sheet with
   `gemini_fal_image` (text_to_image): neutral background, full body + clear face, the
   `visual_style` medium, and every quoted trait. Generate 2-3 candidates, pick the one most
   faithful to the quotes, `import_reference` it.

## 4. Extra angles (identity strength)
Kling elements take one frontal image plus 1-3 reference angles. Make 2 angles with
`gemini_fal_image` **image_edit**, feeding the frontal image:
"Same character, same face, hairstyle, age, body and clothing. Three-quarter view / profile /
full body, neutral background, same art style." Reject angles where the face drifts. If the
frontal is a book illustration, keep the book's drawing style in the angles.
`import_reference` each with `slot: angle`.

## 5. Approval and lock
Show the user, per character: the frontal, the angles, the quotes and the origin. After they
approve, set `locked: true` and `save`. A locked character is never regenerated; recasting
needs the user's explicit request and `unlock: true`.

Cast only characters who appear in the scene being made, but write them into the shared bible
so the next scene of the same book reuses them.
