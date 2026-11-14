# Document writing style

Rules that apply to all documents in this repo (narrative.md, README.md, and any future documents).

## Tone

Concise and professional. Brevity is a goal; informality is not a
shortcut to it. A short sentence that reads like a real engineering
document is better than a long one that reads like a casual explanation.

## Specific rules
- ** British.** Use British academic-style English throughout.
- ** Minimise Acronyms. ** Minimise use of acronyms and jargon unless necessary. If using an acronym, introduce it in text first, like an academic paper.
- ** Consistency of Terminology. ** Make sure particular objects/concepts/things are always referred to by the same name consistently throughout the repository.
- **No colloquialisms.** Words and phrases like "overkill", "zero
  friction", "at a glance" (as a heading), and similar informal
  constructions are not acceptable.
- **No hedging qualifiers on deliberate design choices.** Words like
  "slightly", "a bit", "somewhat", and "fairly" weaken statements that
  should be confident. If a design choice is forward-looking, say it is
  forward-looking — do not say it is "slightly forward-looking".
- **No informal coinages.** Do not coin informal compound words or
  append "-ish" to technical terms (e.g. "one-way-ish"). If the
  precise term does not exist, use a short descriptive phrase instead.
- **No metaphors — be explicit.** A metaphor gives a false sense of
  understanding and comfort, where this work requires precision and full
  understanding; hiding detail behind a convenient metaphor creates blind
  spots and cascades into decisions made without proper scrutiny. Where no
  precise term exists, use a short descriptive phrase, not an analogy.
- **Precise terms, not vague stand-ins.** Name the specific mechanism,
  quantity, or category rather than a convenient word that stands in for
  it. Words to avoid, with their replacements (extend as cases arise):
  "tell" → "indicator" (the detector) or "signature" (the observable); a
  generic "plane" for a category of data → name it (the specific telemetry,
  stream, or source); "concrete" as an adjective → "specific", "explicit",
  or "direct"; "sharper"/"sharp" of an indicator → state what it depends on;
  "keyed"/"keyed on" of an indicator or rule → "depends on", "derives from",
  or name its source/observable (indicators have sources and dependencies,
  not keys); "residual" → reserve for an actual computed residual, not "what
  remains";
  "arms race" → "progression", or describe the pattern; "bottom out" /
  "the wall" → "reach the limit". A formally agreed team term of art is
  exempt.
- **Do not state the obvious.** Omit introductory sentences that
  paraphrase the section heading, meta-labels ("Scope note", "Note:")
  before paragraphs that stand on their own, and closing clauses that
  restate an obvious consequence of what was just said.
