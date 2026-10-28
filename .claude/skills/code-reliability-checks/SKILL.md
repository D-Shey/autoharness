---
name: code-reliability-checks
description: Use when reviewing or writing a code change (a diff, a self-review, a code-quality review, a skill that tells an agent to write code): the fixed reliability checklist R1-R8 and what to look for in the diff
---

# code-reliability-checks

## When to use

A diff touches code (by file suffix: .py, .js, .ts, .go, .java, .rs, .rb, .c, .cpp, .cs, .kt, .swift,
.php, .sh and the like) and you review it yourself, ask a reviewer to, or write a skill that tells an
agent to write such code. Without a fixed list, what gets checked is whatever comes to mind: a
non-atomic write of a state file once left an empty JSON behind and made a test flaky, and it was found
by luck, not by method. A fixed list also gives the review a language: "R1: the state file is not
written atomically" instead of "there may be a race here".

## Steps

Go through the changed files whole, not only the hunks, and for each item say where it holds or where
it breaks (file and line):

- **R1 atomic file writes.** A file another process or a later run reads is written to a temp file in
  the same directory and moved over with `os.replace`, under the lock that guards it. "If successful,
  the renaming will be an atomic operation (this is a POSIX requirement)"
  (https://docs.python.org/3/library/os.html#os.replace). A plain `write_text` on the target can be
  read half-written or left empty by a crash.
- **R2 one lock for shared state.** Shared mutable state changes under one primitive; a read, then a
  write without it, is a timing window "in which the shared resource can be modified by another code
  sequence operating concurrently" (https://cwe.mitre.org/data/definitions/362.html).
- **R3 only expected exceptions.** Catch the exception you expect, by type; an error is not turned into
  "ok" or silence. "The product detects a specific error, but takes no actions to handle the error" is
  a defect (https://cwe.mitre.org/data/definitions/390.html).
- **R4 aware UTC time.** Times are timezone-aware; no arithmetic on naive datetimes. A naive object
  "does not contain enough information to unambiguously locate itself", and `datetime.utcnow()` is
  deprecated since 3.12 in favour of `datetime.now(timezone.utc)`
  (https://docs.python.org/3/library/datetime.html#aware-and-naive-objects).
- **R5 explicit config defaults.** Every default is written down; a missing key is reported or takes
  the stated default, never a silent guess.
- **R6 resources released on every path.** Files, processes, temp dirs and sockets are closed on the
  error path too (`with`, `try/finally`): "does not release a resource after its effective lifetime"
  is a defect (https://cwe.mitre.org/data/definitions/772.html).
- **R7 bounded input.** Size, count and recursion depth have a limit where input comes from outside
  (https://cwe.mitre.org/data/definitions/770.html).
- **R8 hardening keeps the contract.** A fix for robustness does not change what the spec fixed: exit
  codes, field names, output form.

Two limits on the fix itself:

- **Minimal mechanism**: no background threads, caches or wait loops where one lock does.
- **Hardening on top of the contract, not instead of it** (R8 again, for the fix).

A claim in your own review is what you ran or read: "tested" means a command ran and its output is
there. A check you did not run is said as not run, never as passed.

For a skill that tells an agent to write a file, R1 is about a file a later run or another process
reads back (a state file, `.json`, `.yaml`, `.toml`, `.jsonl`, `.db`, "for the next run"), not a script or
page the agent produces. A crash in the middle of an in-place write leaves any mix of old and new data,
"`a boo`, `a far`, or any other combination"; the fix is to write a temp file and rename it over the
original, and even rename "isn't atomic on crash" without an fsync of the file and its directory
(https://danluu.com/file-consistency/). Only a named way of writing counts as covering it: "atomic
commits" or "rename the variable" elsewhere in the skill do not.

To tell whether a skill asks for such a write, judge sentence by sentence, not by a list of phrases: a
sentence that names a data file (or saved state, "to disk", "for the next run") and holds a change verb
(write, overwrite, edit, add, log, put, set, mark, record, update...) in its base form asks for it,
wherever the verb stands: "you must write", "please save", "make sure to update". English imperatives
drop the subject and are negated with "don't" (https://en.wikipedia.org/wiki/Imperative_mood), so rule
out what is not one instead of listing what is: a negation in the verb's own clause ("don't write",
"never modify", but not "don't forget"; in "if the user does not answer, save the state" the negation
ends at the comma: "the scope of a trigger term extends to the right of the trigger term and ends at a
termination term", https://pmc.ncbi.nlm.nih.gov/articles/PMC2757457/), code as the object ("write a
script that parses data.json"),
a third-person form ("it updates", "the CLI stores"), the verb as a noun ("at write time", "the change
log"), a test as the object ("write a test for config.yaml"), a heading, a URL. A list of verb-object
pairs, or of positions a verb may stand in, grows with every phrasing it misses. A way of writing counts
only where it is said of a write: "commit atomically" or "use os.replace to rename the backup" next to
"write state.json" cover nothing, and "write the report atomically" covers its own paragraph, not
queue.json elsewhere; only a blanket rule about state or files ("all state files are written
atomically") covers the whole skill. Test a rule like this on phrasings written before the first run,
not only on the ones it was fixed for: each round of fixes passed its own list and failed the next. The rename is
what makes the swap safe: "If newpath already exists, it will be atomically replaced, so that there is no
point at which another process attempting to access newpath will find it missing"
(https://man7.org/linux/man-pages/man2/rename.2.html). So a temp file covers the write only together with
that rename over the target, in the same place; a temp file for a download elsewhere covers nothing.
A project's own manifests (package.json, tsconfig.json, pyproject.toml) are edited by hand or with an
editor tool, not kept as state between runs.

A skill names such a file the Markdown way: in backticks, with a directory (`` `.app/state.json` ``), or
says "save the task state" with no file word at all; match those, not only a bare `state.json`. A line
that tells the agent not to write it ("do not write to settings.json", "keep config.yaml unchanged"), a
file made for the user (a report), and a tool described in the third person ("it updates
package-lock.json") are not the agent's saved state.

When you tell which files in a diff are code, take the paths from the lines that hold one whole path
each: `---`/`+++` and the `rename from`/`rename to` headers ("Path names in extended headers do not
include the a/ and b/ prefixes", https://git-scm.com/docs/diff-format). The `diff --git a/x b/y` line is
ambiguous when a path holds " b/", and a rename from `x.py` to `x.md` shows code only on its old side.
With the default `core.quotePath` a path with non-ASCII bytes comes quoted and escaped (`diff --git
"a/\321\204.py" "b/\321\204.py"`), and suffix-less files (Makefile, Dockerfile) are code too. When the
diff cannot be read, say so; do not guess either way.

What failed and what fixed it, when building such a wording check (five critic rounds here): a list of
verb-object pairs, then a list of positions a verb may stand in, each passed the phrasings it was fixed
on and failed the next critic's. What held: rule out what is not a write (negation in its clause, third
person, noun use, code as the object, heading, URL) instead of listing what is; write 10-20 held-out
phrasings before each run and keep them as a check that exits 1 on a miss; run the rule over the real
skills at hand and read every hit (noise went 18 -> 34 -> 5 -> 23 -> 12 as rules changed); give every
pattern over skill text a bounded window and a timing test with a long hostile line (R7: an unbounded
file-name prefix took 84 s on 40,000 characters).

## How to verify

- The review names an item code (R1...R8) with file and line for each point it raises.
- For a diff without code files the list is not used: it would only add noise to a drawing or a text.
- A point raised from the list is shown by the code (the write call, the bare `except`, the naive
  `datetime.now()`), not by the item's name alone.

## Facts found here

- autoharness adds this list to the result hint, the critic prompt and the code-quality prompt when the
  latest whole result is code: a recorded diff that touches a code file, or a code file added as the
  result (evidence.CODE_CHECKS, evidence.code_changed); also when the task matches an image reference.
- On 207 skills on one machine the R1 warning of `craft()` fired on 12, each telling the agent to write a
  data, state or config file; measure again before trusting the count.
