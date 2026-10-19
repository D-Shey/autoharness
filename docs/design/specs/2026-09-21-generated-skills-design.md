# Generating your own skills: design

Date: 2026-09-21. Status: in review.

## Why

During a task the agent learns things no installed skill has: how to build this particular project,
how to check the result with the means at hand (for example, exporting .pptx slides to PNG through
PowerPoint COM when there is no LibreOffice), what an API's quirks are. Today that knowledge is lost
with the session.

The goal is **carrying experience over**: at the end of a task the agent writes what it learned into
a skill, the skill goes into the autoharness library and is installed in other projects together with
a recipe.

The agent writes the skill: autoharness has no LLM. autoharness gives the template, the checks, the
lockfile records, the user's consent and the storage.

Out of scope: publishing on skills.sh, syncing the library between machines, generating a skill in
the middle of a task in place of one not found (the mechanism is the same, but the agent's
instructions describe only the "at the end of a task" scenario).

## Terms

- **The library**: `~/.autoharness/skills/<name>/` (`AUTOHARNESS_HOME` overrides the root).
- **A generated skill**: a skill created by `skill new` in the current task.
- **A local spec**: `local:<name>`, a reference to a skill in the library. It lives next to
  `owner/repo@skill` in lockfiles, `kept.json` and recipes.

## Commands

All print JSON; exit code `3` means the user's consent is needed.

| Command | Needs an active task | What it does |
|---|---|---|
| `skill new <name> --description "<text>"` | yes | a template in the agent's skills folder + a lockfile entry |
| `skill save <name> [--approve]` | no | checks; with `--approve`, a copy into the library |
| `skill list` | no | the library's contents |
| `skill delete <name>` | no | removal from the library |
| `add skill local:<name>` | yes | install from the library |

### `skill new`

- The name: `^[a-z0-9][a-z0-9-]{1,63}$`. An error if a folder with that name already exists in the
  project's skills or a skill with that name is in the library (the library is overwritten only by
  `skill save` of an installed `local:` skill, see below).
- Writes `<skills_dir>/<name>/SKILL.md`:

  ```markdown
  ---
  name: <name>
  description: <description>
  ---

  # <name>

  ## When to use
  <!-- autoharness: fill in -->

  ## Steps
  <!-- autoharness: fill in -->

  ## How to verify
  <!-- autoharness: fill in -->
  ```

- An entry in the task's `skills.lock.json`: `spec: "local:<name>"`, `source: "generated"`, the
  template's `sha256`, `trust: {"level": "high", "score": 100}`, `findings: []`. From then on this
  skill is like any other for teardown, `keep`/`remove` and `status`.

### `skill save`

Looks the skill up by name: first in the active task's lockfile, then in the project's `kept.json`.
Only a skill with the spec `local:<name>` (created by `skill new` or installed from the library) can
be saved. Otherwise it is an error: other people's skills are not copied into the library.

The checks in order; the first group that finds something decides the answer:

1. **Format** → `status: "invalid"`, code 1.
   No `SKILL.md`; no frontmatter; `name` ≠ the folder name; an empty `description`; the marker
   `<!-- autoharness: fill in -->` left in any file.
2. **Secrets** → `status: "blocked"`, code 1. Consent does not help; the agent must remove them.
   Patterns: `ghp_[A-Za-z0-9]{20,}`, `github_pat_`, `sk-[A-Za-z0-9_-]{20,}`, `AKIA[0-9A-Z]{16}`,
   `xox[abpr]-`, `-----BEGIN [A-Z ]*PRIVATE KEY-----`,
   `(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['"]?[^\s'"$<{]{8,}`
   (values like `${VAR}` and `<...>` do not count).
3. **Static scan** (`trust.scan_skill`) → high findings: `status: "blocked"`, code 1.
4. **Privacy** → does not block, goes into `warnings`: absolute paths `[A-Za-z]:\\Users\\[^\\\s]+`,
   `/home/[^/\s]+/`, `/Users/[^/\s]+/`; email addresses. Medium/low scan findings also go into
   `warnings`.

If nothing blocks:

- without `--approve` → `status: "approval_required"`, code 3, the answer has `name`, `description`,
  `files`, `warnings`, `overwrites` (true if a skill with that name is already in the library), and
  `next`: show it to the user and repeat with `--approve` only after their "yes";
- with `--approve` → a copy (without `RUNTIME_JUNK`) into `~/.autoharness/skills/<name>/`, the previous
  version is replaced entirely. Next to it, `~/.autoharness/skills/<name>.meta.json`:
  `{"name", "description", "capabilities", "task", "project", "saved", "sha256"}`, where
  `capabilities` are the task's capabilities at the time of saving. A line about the save goes into
  the task's log (if there is a task).

`meta.json` lies next to the folder, not inside it, so it does not end up in the skill itself when
installed.

### `add skill local:<name>`

- Not in the library → an error with the hint `skill list`.
- The trust report: `score 100, level high`, the reason "saved to the local library with the user's
  approval"; no consent needed.
- The install goes through the same `Task.add_skill` with another stager: it copies the folder from
  the library instead of `npx skills`. The static scan runs as usual.
- Already kept in the project (`kept.json`) → `already_installed`, as for any skill.

### `skill list`, `skill delete`

- `list`: `[{name, description, capabilities, saved}]` from `*.meta.json`.
- `delete <name>`: removes the folder and `meta.json`. Copies already installed into projects are
  left alone. Not found → an error.

## How it connects to the rest

- **`parse_skill_spec`** accepts `local:<name>` and returns `("local", name)`.
- **Recipes.** `recipes.save` already takes specs from the lockfile, so `local:<name>` gets there by
  itself. `teardown --save-recipe` adds `warnings` to the answer if the recipe has a `local:<name>`
  that is not in the library.
- **Teardown.** For a skill with `source: "generated"` that is not in the library:
  - `keep`: it stays in the project, the answer has `unsaved_generated: [name]`;
  - `remove`: before removal, a copy goes to `.autoharness/tasks/<id>/generated-skills/<name>/`, and
    the answer has `unsaved_generated` with the copy's path.
- **`need <capability>`** adds `local: [{name, description}]`: library skills whose `meta.json` has
  that capability.
- **autoharness's `SKILL.md`**, step 6, a new item before teardown: if you learned something reusable
  that the installed skills do not have, `skill new` → fill it in → `skill save` → show it to the
  user → `--approve` after a "yes"; write it in general terms, without absolute paths, secrets or the
  names of people and clients. Step 2: `need` first, it shows the library as well.
- **README**: a "Your own skills" section, a row in the command table, remove the item from "MVP
  limitations".

## Where it goes in the code

- `src/autoharness/library.py`, a new module: library paths, `save(src_dir, meta)`, `list_skills()`,
  `delete(name)`, `stage_from_library(repo, skill, stage_dir)` (a stager),
  `check(skill_dir) -> CheckResult` (format, secrets, scan, privacy).
- `env.py`: `Task.new_skill(name, description)`; `parse_skill_spec` for `local:`; handling unsaved
  generated skills in `teardown`.
- `cli.py`: the subcommands `skill new|save|list|delete`, the `local:` branch in `resolve_skill` and
  the stager choice in `cmd_add`, `local` in `need`, recipe warnings in `teardown`.
- `trust.py` does not change; the secret and privacy checks live in `library.py`: they serve another
  purpose (a leak out of the project, not a threat to the agent).

## Errors

| Situation | Answer |
|---|---|
| `skill new` without an active task | the error "no active task" (as for `add`) |
| the name is invalid or taken | an error with the reason |
| `skill save` for a non-`local:` skill | the error "only skills created with `skill new` or installed from the library" |
| the skill is found neither in the task nor in `kept.json` | an error |
| a damaged `meta.json` | `skill list` skips the entry and adds it to `warnings` |

## Tests

1. The full cycle: `skill new` → fill in → `skill save` (code 3, `approval_required`) →
   `skill save --approve` → in another project `add skill local:<name>` installs it, and the lockfile
   has `local:<name>`.
2. An unfilled template → `invalid`, code 1.
3. A secret (`ghp_…`) → `blocked`, code 1, even with `--approve`.
4. An absolute path → `approval_required` with `warnings`.
5. Saving again → `overwrites: true`; after `--approve` the library has the new version.
6. A recipe with an unsaved `local:` → `warnings` in the teardown answer.
7. `teardown --remove` with an unsaved generated skill → a copy in `generated-skills/`,
   `unsaved_generated` in the answer.
8. `need` shows a library skill with a matching capability.
9. `skill delete` removes it from the library; the copy installed in a project stays.
10. `skill save` for an ordinary `owner/repo@skill` → an error.
