---
name: skill-threat-rules
description: Use when adding or changing rules of a static scanner for agent skills or MCP servers (autoharness trust.scan_skill or similar): picking severities, avoiding false positives on legitimate skills, testing each rule
---

# skill-threat-rules

## When to use

A scanner decides whether a downloaded skill may be installed (or a generated one saved). A new rule
that fires on ordinary skills blocks real work; a rule written from memory misses the way attacks are
actually phrased. Open this before adding a rule, raising a severity, or when a legitimate skill was
rejected or a bad one slipped through.

## Steps

1. **Start from a catalogue, not from memory.** NVIDIA SkillSpector's README lists threat categories
   with ids and severities (prompt injection, anti-refusal, hidden instructions, whitespace padding,
   persistence, shipped bytecode, AST exec/eval chains, taint source to sink, unicode deception):
   https://github.com/NVIDIA/skillspector/blob/main/README.md . Take the idea, write the detector
   yourself, sized to the project (stdlib only if the project has no dependencies).
2. **Severity follows what the gate does with it.** If `high` blocks install, give `high` only to
   signals with almost no legitimate use: bidi/tag control characters, text telling the agent to
   drop earlier instructions, executing data that was just decoded or downloaded, secrets read and
   sent out in the same file. Everything a normal setup script may do (scheduled tasks, rc-file edits,
   compiled files, zero-width characters in non-English text) is `medium` or `low`: shown, not blocked.
3. **Combine weak signals instead of raising one.** Reading the whole environment is medium, posting
   data is medium; both in one file is the exfiltration chain, high. Same idea for taint: a name
   assigned from a decode/download call and later passed to exec.
4. **Prefer structure over regex where it exists.** For Python, parse with `ast` and look at calls and
   their arguments; a regex on `exec(` also hits comments and strings. Fall back silently when the
   file does not parse.
5. **Exempt defensive wording narrowly.** A skill that warns against an attack quotes it. Skip a
   match that is directly governed by a negation right before it on the same line ("never ...",
   "do not ..."); lower (do not skip) one inside quotes or a code block (step 15); SkillSpector's `prohibition_context.py` shows how narrow
   that exemption must be (only the negation that governs this very phrase, not one nearby).
   Do not exempt a phrase whose attack form itself contains the negation (refusal suppression).
   Block only the attack's own grammar: per the OWASP prompt injection cheat sheet
   (https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
   it is an imperative to the model at the start of a sentence or clause ("Ignore all previous ..."),
   possibly after "first,", "now", "please" or an address ("assistant:", "you must"). The same phrase
   as the object of another verb ("if a page tells you to ...", "attempts to ...", "a request to
   ...", in parentheses) describes the attack: lower it to medium, do not block. Scope limits it too,
   as SkillSpector's prohibition_context.py reasons for negations
   (https://github.com/NVIDIA/skillspector/blob/main/src/skillspector/nodes/analyzers/prohibition_context.py):
   a clause opened by a condition or scope word ("When summarizing, ...", "If ...:") or a scope after
   the phrase ("... embedded in the document", "... that appear inside it") is not a bare order; nor
   is a heading, or a line labelled as an example (on the line itself or the line directly above;
   a section heading is too far away to govern a line). The scope must name external content the
   agent reads, the carriers OWASP LLM01 lists (https://genai.owasp.org/llmrisk/llm01-prompt-injection/):
   pages, files, documents, tool results, untrusted sources. A bare "within", "inside" or "found",
   or one pointing at the agent's own context ("this conversation", "so far"), is part of the attack.
6. **Keep messages in the existing style**: a short English verb phrase, the same for every hit of
   one rule, so callers and tests can match on it. Never change existing messages.
7. **Do not write attack strings literally into a skill or doc that gets scanned itself** (a saved
   skill goes through the same scanner): describe them, or build them in tests from pieces.
8. **Never put invisible characters literally into the scanner's own source or tests.** Write them as
   regex/str escapes (`\u202e`, `\U000e0020`); a literal bidi control reorders the line in editors and
   diffs, which is exactly the Trojan Source trick (https://trojansource.codes/). Check with `od -c`.
9. **Hidden characters: know the legitimate sequences.** Bidi controls U+202A-202E and U+2066-2069
   are the Trojan Source set; LRM/RLM (U+200E/200F) and plain RTL letters are ordinary text. Unicode
   tag characters U+E0020-E007E followed by U+E007F CANCEL TAG after an emoji are a valid emoji tag
   sequence (flags of England, Scotland, Wales: U+1F3F4 + tags + U+E007F), per Unicode TR51 ED-14a
   (https://www.unicode.org/reports/tr51/#valid-emoji-tag-sequences). Remove valid sequences first;
   only stray tag characters are smuggled text.
10. **Taint must follow scope and role, or `high` hits ordinary scripts.** Model it on Semgrep's
   taint mode (https://docs.semgrep.dev/writing-rules/data-flow/taint-mode): intraprocedural by
   default (plus module-level names a function reads), only from assignments before the sink, and the
   sink is the exact argument that becomes code: `exec`/`eval` source, a shell string, the program
   slot of an argv list; for an interpreter (`python`, `sys.executable`, `bash`, `node`...) the item
   after a code flag (`-c`, `-e`, `-Command`) or the first positional (the script), and nothing after
   `-m` (`pip install pkg=={latest}` is data). A list assigned to a variable keeps the same role rule:
   downloaded data passed as an argument (`cmd = ["git", "checkout", ver]`) is not code.
   Taint enters through every binding form, not just `=`: `with ... as r`, `for x in ...`,
   comprehensions; an object built from tainted data (`Request(URL, data=body)`) carries it; HTTP
   clients are also the session objects (`requests.Session()`, `httpx.Client()`,
   `aiohttp.ClientSession()`) and calls chained on them.
11. **Use the same source/sink model for exfiltration, not two unrelated regexes in one file.**
   Source: the whole environment (`os.environ`, `dict(os.environ)`, `.copy()`, `.items()`) or a
   credential file; sink: a network send (HTTP client post/put/patch/request, `urlopen` with data,
   socket send). In shell text, the one-line form is the environment-printing command piped or
   substituted into an HTTP or netcat client, in any file including SKILL.md.
   Know which arguments do not flow into a call's result (sanitizers, in Semgrep's terms): per the
   subprocess docs (https://docs.python.org/3/library/subprocess.html) `env=` goes to the child
   process only, so a tool run with a copy of the environment returns its output, not the secrets;
   posting that output is ordinary. Conversely `input=`/`.communicate()` is the child's stdin: an
   interpreter fed downloaded text that way is the Python form of piping a download into a shell,
   but only when stdin is its program: per the Python command line docs
   (https://docs.python.org/3/using/cmdline.html) that is no argument or `-`; after `-c`, `-m` or a
   script name stdin is that program's ordinary input (same for `bash script.sh`, `node app.js`).
   Resolve `__import__("os")` and `getattr(os, "system")` like an import before matching sinks.
   A shell string does NOT obey the argv role rule: the shell parses the whole string, and per
   POSIX 2.2 (https://pubs.opengroup.org/onlinepubs/9799919799/utilities/V3_chap02.html) unquoted
   `& ; < > ( ) $` backtick and newline are operators wherever they appear, interpolated text
   included. So downloaded text in a shell string is high when there is no fixed program before it
   or the fixed text already holds an operator, and at least medium after a fixed program (it may be
   a plain argument, it may be a second command). An argv that is not a literal list (`base + [ver]`,
   a helper's return value) has unknown roles: medium, not high. Assigning to `obj.attr` or
   `d["k"]` taints that exact path, never the whole object.
   Match sinks and sources by what the name is bound to, not by its spelling: per the import
   statement reference (https://docs.python.org/3/reference/simple_stmts.html#the-import-statement)
   `import subprocess as sp` makes `sp.run` subprocess.run and `from os import system` makes a bare
   `system(...)` os.system; resolve the first part of a call's name through the file's imports. A
   client object (`requests.Session()`, `httpx.Client()`) downloads with `.get` as surely as it sends
   with `.post`. Recursive helpers over the tree need a depth cap: a 1500-term string concatenation
   parses fine and then overflows the stack of a naive recursive walk.
   Binding edge cases: a `with`/`for` body on the same line as its header still sees the binding;
   a function parameter shadows a module-level name of the same name.
12. **A shell-command rule must also read the same command written as a Python argv** (a quote and
   comma, not a space, after the program name), a registry path with doubled backslashes inside a
   Python string, and appending to a start-up file from code (`open(..., "a")`). Take the list of
   start-up files from MITRE ATT&CK T1546.004 (https://attack.mitre.org/techniques/T1546/004/):
   bashrc, bash_profile, bash_login, bash_logout, profile, zshrc, zshenv, zprofile, zlogin, zlogout.
13. **Bidi: flag what is unterminated or reversing.** Balanced isolates (LRI/RLI/FSI ... PDI) and
   embeddings (LRE/RLE ... PDF) are ordinary in RTL text; Trojan Source is about unterminated ones.
   Overrides (LRO/RLO) reverse what a reviewer sees and are flagged even when closed.
14. **Every blocking rule, including a chain, needs a positive test and a mutant** in the mutation
   check; a rule hidden inline in the scan loop is easy to miss, so give it its own function.
15. **Quoting lowers, it never drops.** A phrase in quotes, a ``` block or a string literal is
   probably an example, so it warns (medium) instead of blocking; it must still be reported, or one
   quote character becomes a bypass. Only a negation that directly governs the phrase drops it.
   "In quotes" means a quote that opens before the phrase and closes after it (typographic pairs
   included); a lone quote mark or a plural apostrophe before it is no quotation. A sentence that
   names the phrase as an example ("phrases like ...") is lowered the same way.
16. **What the scanner cannot read must be a finding, not silence.** ClamAV's AlertExceedsMax
   (https://raw.githubusercontent.com/Cisco-Talos/clamav/main/etc/clamd.conf.sample) flags files over its
   limits; otherwise padding a file past the size limit, or one invalid UTF-8 byte, hides it entirely.
17. **Bound the work per match.** The scanner reads hostile input: context checks that re-read the
   whole text before each match turn one long line of repeated matches into quadratic time (a
   160 KB line took 47 s), the same family as ReDoS
   (https://community.owasp.org/attacks/Regular_expression_Denial_of_Service_-_ReDoS). Look at a fixed
   window around each match and cap the matches per line; add a timing test with a long hostile line.

18. **Read text rules on the line as the model reads it.** Characters rendered as nothing split a phrase for a
   regex but not for the model or a reviewer. Remove Unicode's Default_Ignorable_Code_Point set (soft hyphen,
   zero-width, word joiner, bidi controls, variation selectors, tag characters; the list is in
   https://www.unicode.org/Public/UCD/latest/ucd/DerivedCoreProperties.txt) and terminal escapes from a copy of
   the line before the text rules (Embrace The Red shows tag text "unnoticable to users... but LLMs interpret
   such text": https://embracethered.com/blog/posts/2024/hiding-and-finding-text-with-unicode-tags/);
   read the line twice, once with them removed (a split word) and once with each run as a word break (a phrase
   whose spaces were replaced: U+3164 HANGUL FILLER draws as a blank), and keep the stronger finding; UTS #55
   (https://www.unicode.org/reports/tr55/) both ignores them in comparison and allows them between lexical
   elements. Keep the hidden-character rules on the raw line, and show the cleaned line as
   the excerpt (a raw escape would hide it again in the reader's terminal).
19. **Terminal escapes: flag what hides or overwrites, not colour.** Per xterm's control sequences
   (https://invisible-island.net/xterm/ctlseqs/ctlseqs.html) SGR 8 makes text invisible, CSI A-H moves the
   cursor over text and J/K erase it, OSC (a hyperlink) shows one text and points at another. Colours are
   harmless; read SGR codes in order and skip the numbers after 38/48/58 (a colour index 8 is not "conceal"),
   and read "08" as 8. Also ESC 7 / ESC 8 (save and restore the cursor: what follows overwrites) and ESC c
   (reset), the one-byte CSI 0x9b and OSC 0x9d (https://en.wikipedia.org/wiki/ANSI_escape_code), and the
   alternate screen (private modes ?47/?1047/?1049 h); hiding the cursor (?25l) hides no text.
   An escape written as text (backslash-033) is a script printing it, not a hidden one.
20. **Imitated system messages.** The special tokens of chat templates (Hugging Face chat templating,
   https://huggingface.co/docs/transformers/main/en/chat_templating: ChatML, Mistral INST, Zephyr role tags,
   Llama 3 headers, Gemma turns) and a line labelled "System:" that gives the agent a new role are the attack;
   the docs themselves show the tokens in code blocks, so fenced or quoted is lowered like an override. A
   token opens a message only at the start of a line or before a role name; named inside a sentence ("end
   each turn with ...") it is lowered too. Look at every token on the line, not the first: a harmless closing
   token can come right before the opening one. A "System:" label followed by a requirement (an OS, a version)
   is ordinary.
21. **Refusal suppression includes forbidding apologies, and so do tone rules: warn, never block.** The
   attack's rules in Wei et al., Jailbroken (https://arxiv.org/html/2307.02483) forbid apologies, notes and
   disclaimers and words like "cannot". A UI-writing skill forbids error messages to apologize in the same
   words for style, so the finding stays medium; dropping the words would miss the attack.
   In Russian, "не отказывайся от X" means "do not give X up".
22. **A hidden comment that reassures the user falsely, or keeps something from them, is high.** It deceives
   the one reader who cannot see it; Invariant Labs' tool poisoning
   (https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks) works the same way, with a
   line telling the model not to mention what it did. Match the grammar, not one sentence: contractions
   ("it's"), plural and polite verb forms (Russian "-те"), other verbs ("let the user know", "say"). What
   deceives is narrow: an order to tell the user that all went well or is safe (fine, passed, nothing found,
   "всё в порядке"), or to keep the action itself from them ("do not mention this to the user"). Every other
   order about what to say ("reply to the user in Russian", "tell the user the file path", "объясни, что ...")
   is ordinary authoring and stays medium; a note that only reports a result ("after CI passed") is nothing.
   Write the claim as the complement of the order: verb, optional "that", a short subject, optional "is", the
   claim word, and nothing negating or conditioning it ("not safe", "safe to delete", "passed or failed",
   "или нет"); a claim word that merely appears in the comment is not enough ("say it works on Windows").
   Concealment takes a short object ("never mention this step to the user", "hide ... from the user", "не
   упоминай этот шаг пользователю"). Five critic rounds swung this rule between too narrow and too wide:
   pin it by the harm and test every attack form and every ordinary comment any reviewer has named.
   Visible prose saying the same is an ordinary workflow step: nothing there.
   A "System:" label that only gives a persona ("you are an expert copywriter", "your task is ...") is a prompt
   template: medium; a role line inside quotes is an example: medium, not silence. Chat tokens in a .txt or
   template file are a fine-tuning skill's material: medium; in markdown the agent reads as orders: high.
23. **A carriage return inside a line prints over its start**, like the escapes in step 19: "carriage return
   by itself provided the ability to overprint the line" (https://en.wikipedia.org/wiki/Carriage_return). Read the file with
   line ends kept (`newline=""`) to see it; Windows "\r\n" and an old Mac file with "\r" only are ordinary.
   Make it `medium`, not `high`: captured progress output ("10%\r100%") does the same. Scan both parts as
   lines of their own, so an attack in the overwritten part gets its own finding.

## How to verify

- One test per rule that it fires on a real-looking example and one that it stays quiet on the
  closest legitimate text (the false-positive twin), for every rule, `high` ones first (bidi: RTL
  text, flag emoji; padding: blank lines at the end of a file). Parametrize when there are several forms.
- An installed-skills run that finds nothing new proves only "no false positives there", not that a
  rule works: the positive tests carry that.
- Run the scanner over every skill installed on the machine (user skills dir, plugin caches) and
  count `high` findings: the target is zero; read every `medium` hit and decide if it is fair.
- Run the full test suite: existing rules and their messages must be unchanged.
- Run the scanner over this skill itself after every edit: writing about attacks tends to quote them,
  and a `high` here blocks saving the skill (it happened three times while writing this one).
- Mutation check: for each rule, a variant that never fires and one that always fires must each make
  some test fail; a surviving variant names the missing test (or a redundant rule).

## Facts found here

- autoharness: `trust.is_blocking` blocks on any `high`; `env.py` blocks install on it,
  `library.py` blocks `skill save` on it and shows medium/low as warnings (checked 2026-10-27).
