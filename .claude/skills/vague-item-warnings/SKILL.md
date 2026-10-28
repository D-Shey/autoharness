---
name: vague-item-warnings
description: Use when adding or tuning a warning-only check that flags vague items in agent-written text (brief items, lessons, skill steps): which words count, what counts as concrete, how to keep false alarms low
---

# vague-item-warnings

## When to use

A tool takes free text from an agent that is meant to be acted on later (a brief's areas, a lesson
"when X, do Y, because Z", a skill's steps) and you want to flag items that say nothing a reader
could act on: "do it carefully", "follow best practices", `create payments-helper` for an area
called payments. The form of such text is easy to check; whether it holds anything is not, and a
wrong refusal on a normal item makes people stop trusting the tool. So these checks warn and never
refuse.

## Steps

1. **Warn, never refuse.** The check is a naive prose linter: write-good, for one, flags "weasel
   words" and "adverbs that can weaken meaning: really, very, extremely" as suggestions, with a
   whitelist to exempt terms (https://github.com/btford/write-good). The item is still recorded;
   the warning goes into the command's `warnings` list, where the author reads it.
2. **Two conditions, both needed.** An item is vague when it leans on a word from a short, explicit
   list (English and Russian: `carefully`, `properly`, `correctly`, `best practice`, `clean code`,
   `high quality`, `аккуратно`, `правильно`, `качественно`, `внимательно`, `как следует`,
   `чистый код`, `лучшие практики`) **and** names nothing concrete. Concrete is anything a reader
   can find or run: text in backticks, a number, a file name with an extension, a dotted or
   snake_case identifier (`os.replace`, `PROMPT_KEPT`), a flag (`-x`, `--count`), a path or a URL.
   "Write it properly: a temp file and os.replace" names the way, so it passes.
3. **A name that only restates the area.** For `create <name>` in a brief, split the name on `-`
   and drop the parts the area or the search query already says (the same word, or one a prefix
   of the other, from 4 letters: `payment`/`payments`) and filler words (`helper`, `utils`,
   `tools`, `guide`, `tips`, `notes`, `misc`, `common`, `basics`, `best`, `practices`). Nothing
   left means the name promises nothing: `payments-helper (searched: payments)` warns,
   `payment-webhook-signature (searched: webhooks)` does not.
   Abbreviations are not concrete: drop `i.e.`, `e.g.`, `etc.`, `т.е.`, `т.к.`, `т.д.`, `т.п.`, `and/or`,
   `и/или` before looking for a dotted name or a path, or "do it carefully, i.e. properly" passes.
   Name parts of 3 letters count as said when a word of the area or query starts with them (`log` in
   `logging`). A name in another language than the area (`payments-helper` under `Платежи`) is not caught:
   no translation, by design.
3a. **A list item is all its lines, not the first.** "Subsequent lines" indented by the marker's width
   belong to the item, and so do unindented "lazy continuation lines" of its paragraph
   (https://spec.commonmark.org/0.31.2/#list-items). So an item runs from its marker up to a blank line,
   the next marker or a heading; judging the first line alone flags `Check it carefully against` and
   misses `ref.png at 100% zoom` on the next line.
3b. **A skill step warns only when it is nothing but vague words.** "Be careful not to delete the user's
   drafts" says what to do although it holds `careful`. Take the vague words out and count what is left:
   4 words or fewer (`Write and test it`) is no step; more is one. A lesson is judged by the plainer rule
   of step 2, since "when X, do Y, because Z" always has words around its Y.
4. **Keep the list short and fixed.** Do not grow it from each phrasing that slipped through: each
   addition is a new source of false alarms. Add a word only when the real data shows it standing
   alone, as the whole of an item.
5. **Write held-out phrasings before the first run** (both kinds: should warn, should not), keep
   them as a script that exits 1 on a miss, and run them after every change of the rules.
6. **Measure noise on real data.** Run the check over the real items at hand (saved insights, the
   skills installed for the agent and in the library) and read every hit: a hit that is a fine item
   is a false alarm to fix in the rule, not in the list.

## How to verify

- Each case of the task has a test, and the tests fail on the code before the change.
- The held-out script exits 0.
- The count of hits on real data is written down below with what each was, and the false alarms
  among them are explained or fixed.

## Facts found here

- autoharness implements these rules in `vague.py`: `hollow_create` (brief `create <name>`), `vague` (lesson
  text), `vague_steps` (bullets under `## Steps`, used by `library.craft`).
- Measured on one machine, 2026-10-28, by a script run through `verify run` (output kept as facts): see the
  counts below. Only a few SKILL.md files have a `## Steps` heading, so the step rule is also measured over
  every list item of every skill, to see what it would flag beyond its section.
  - insights: 0 of 31 warn;
  - skills: 170 SKILL.md, only 9 with `## Steps`; 0 of their 99 step items warn;
  - every list item of every skill: 10 of 10,377 would warn, all of them heading-like items or quotes
    ("Best practices", "**Read Error Messages Carefully**", "it's not working correctly"), outside the rule;
  - brief creates: 0 of 23 across local projects, each read (e.g. `router-reboot-recovery` for `openwrt`).
  No real vague item has turned up yet, so the sensitivity is shown only by the held-out phrasings.
- 25 held-out phrasings (12 names, 8 lessons, 5 steps) were written before the first run and all held at
  once; 8 more were written before the second round (after the critic's points, before rerunning). The
  critic's own cases (a wrapped item, "be careful not to...", `i.e.`) found what the held-out ones missed.
- A name part counts as said when an area or query word starts with it (`log` in `logging`), or when it
  starts with a word of 4 letters or more (`payments` with `payment`); a 3-letter word does not cover a
  longer part (`sqlite` is not `sql`, `webhook` is not `web`, `gitignore` is not `git`).
