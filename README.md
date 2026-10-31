# TODO 

- https://github.com/rajrawat37/dotfiles/blob/main/bootstrap.sh

# Scripts

## `scripts/convert-skill.py`

Makes the skills and rules of one coding agent available to another. Targets: **Codex** (default), **Claude Code** and **Kiro**. It needs Python 3.9+ and nothing else.

The source is never modified. The script adds what the target agent needs next to it, and it avoids duplicating files when the target can use them as they are.

### Goal: Codex and Kiro in the same project

```text
my-project/
├── .agents/skills/my-skill/        <- real files (Codex)
│   ├── SKILL.md
│   └── agents/openai.yaml          <- Codex-only file
└── .kiro/skills/my-skill/          <- real files (Kiro)
    └── SKILL.md
```

```sh
python3 scripts/convert-skill.py my-project/.agents --target kiro
```

This creates the `.kiro` part and leaves `.agents` exactly as it was. Running it again, or with `--target codex`, changes nothing: every file is reported as `unchanged`.

### When files are shared and when they are copied

| Situation | What happens |
|---|---|
| Codex needs a skill that lives in `.kiro/skills/x` or `.claude/skills/x` | `.agents/skills/x` becomes a **symlink** to that folder. Nothing is duplicated. Codex [follows symlinked skill folders](https://learn.chatgpt.com/docs/build-skills). |
| Codex also wants UI metadata | `agents/openai.yaml` is **created inside the shared folder** if it is missing. Kiro and Claude ignore it. |
| Kiro needs a skill that lives in `.agents/skills/x` | A **real copy** of `SKILL.md` (and the bundled files Kiro can use) is written to `.kiro/skills/x`. The Kiro IDE silently ignores symlinked skills ([kirodotdev/Kiro#6401](https://github.com/kirodotdev/Kiro/issues/6401)), so this is the one case where a copy is needed. `agents/openai.yaml` is not copied. |
| Claude Code needs a skill from another agent | A real copy, without `agents/openai.yaml`. Symlink support is not assumed. |
| `SKILL.md` has to change (invalid name, missing description) | A real, corrected copy is written instead of a link. |
| The skill folder already exists for the target | A real folder is compared file by file. Files that differ are reported as `conflict` until you pass `--force`. |
| The link cannot be created (for example Windows without symlink rights) | The script falls back to a copy and says so. |

`--symlinks always|never` overrides the choice (`auto` is the default).

A Kiro copy is a real file, so it does not follow later edits of the original by itself. Run the script again (or `--sync`) and it refreshes the copies it generated; see the next section.

Symlinks inside a Git repository are stored as links. They work on macOS and Linux. On Windows Git needs `core.symlinks=true`.

### What to commit to Git

Commit the **original** skill folder (the one the skill is written in). Do not commit the links and copies the script generates for other agents.

- Generated paths are written to a managed block in **`.git/info/exclude`**, the per-clone ignore file that is never committed. `git status` stays clean.
- `.gitignore` is not used. It is shared with the team, its list would change with every skill, and `.kiro/` or `.claude/` cannot be ignored as a whole because they also hold files that belong in Git (`.kiro/specs`, `.kiro/steering`, `.kiro/hooks`, `.kiro/settings`).
- The block also records which skills the script **owns**. When the original changes, a later run refreshes an owned copy (`overwrite`, no `--force` needed). A skill that is not listed, for example one you wrote by hand, is never overwritten without `--force`.
- A folder the script creates is listed as one entry (`/.kiro/skills/my-skill/`). A symlink is listed as one entry. A file added to a folder that already existed, such as `agents/openai.yaml` in a shared folder, is listed on its own. Entries whose path no longer exists are dropped the next time the script adds entries.
- This applies to project installs inside a Git repository. It is skipped with `--global`, outside Git, and with `--no-exclude` (which also turns off refreshing).
- The block lives in this clone only. A teammate who clones the repository gets the originals and runs the script (or the agent rule) to generate their own.

Limits:

- A refresh only rewrites files that changed. A file you delete from the original stays in the generated copy until you delete that copy.
- An entry in `.git/info/exclude` does not untrack a file that is already committed. If you commit a generated file by hand, Git keeps tracking it and the script may still overwrite it.

When to commit the generated files instead: a shared or open-source repository where every contributor must have working skills for several agents right after cloning, without running anything. Then commit all the agent folders, treat one as the original, and regenerate the others in the same commit as any change to it. Use `--no-exclude` so nothing is hidden.

### Make a whole project work for one agent (`--sync`)

```sh
python3 scripts/convert-skill.py --sync --target codex
```

Run from a project root. `--sync` looks for `.agents/skills`, `.claude/skills` and `.kiro/skills`, ignores the target's own folder, and converts the skills the target does not have yet. A skill that already exists is not touched: it is reported `unchanged`, or `differs` when its `SKILL.md` is not the same as the other agent's. The exception is a skill this script generated earlier (see above), which is refreshed. When two other agents have the same skill, the first one found wins (Codex, then Claude, then Kiro). `--sync PROJECT` works on another folder. Rules are not part of `--sync`; convert them by naming the file (see below).

### Run it straight from GitHub

```sh
curl -fsSL https://raw.githubusercontent.com/costinEEST/dotfiles/main/scripts/convert-skill.py \
  | python3 - <SOURCE> --target kiro
```

If this repository is private, `curl` cannot download the script. Use the GitHub CLI instead:

```sh
gh api -H 'Accept: application/vnd.github.raw' repos/costinEEST/dotfiles/contents/scripts/convert-skill.py \
  | python3 - <SOURCE> --target kiro
```

### Source

`SOURCE` can be one or more of:

- a local file or folder
- a GitHub URL: `https://github.com/OWNER/REPO`, `.../tree/REF/PATH` or `.../blob/REF/PATH`
- a `raw.githubusercontent.com` URL
- any other `http(s)` URL that serves a single file

A link to a `SKILL.md` converts the whole skill folder, including `scripts/` and `references/`. Private GitHub repositories work when `GITHUB_TOKEN` or `GH_TOKEN` is set, or when `gh auth login` has been run. The token is only sent to GitHub. Skills from a GitHub URL are always copied, because there is no local folder to link to.

The format is detected automatically:

| Source | Detected by |
|---|---|
| Agent Skills folder (`.agents`, `.claude`, `.codex`, `.kiro`) | a `SKILL.md` file |
| Cursor rules | `*.mdc`, `.cursorrules` |
| Copilot instructions | `copilot-instructions.md`, `*.instructions.md` |
| Windsurf / Cline rules | `.windsurf/rules`, `.windsurfrules`, `.clinerules` |
| Claude, Gemini, generic | `CLAUDE.md`, `.claude/rules/*.md`, `GEMINI.md`, `AGENTS.md` |
| Kiro steering | `.kiro/steering/*.md` |

### What is produced

| Source | Codex | Claude Code | Kiro |
|---|---|---|---|
| Skill | `.agents/skills/<name>/` | `.claude/skills/<name>/` | `.kiro/skills/<name>/` |
| Always-on rule | block in `AGENTS.md` | block in `CLAUDE.md` | `.kiro/steering/<name>.md`, `inclusion: always` |
| Rule for certain files | skill whose description lists the globs | same | `inclusion: fileMatch` |
| Rule chosen by description | skill | skill | `inclusion: auto` |
| Manual rule | skill | skill | `inclusion: manual` |

Skill names are made lowercase with hyphens (max 64 characters) and descriptions are limited to 1024 characters, as the tools require. A `SKILL.md` that is already valid is copied byte for byte. An `AGENTS.md` or `CLAUDE.md` in a sub-folder becomes a rule limited to that folder.

### Where the output goes

1. `--dest DIR`, if given (with `--sync`: the project).
2. Otherwise, for a local source inside a tool folder (`.agents`, `.claude`, `.codex`, `.kiro`, `.cursor`, `.windsurf`, `.github`), the folder that contains it. `my-project/.agents` writes into `my-project`, whatever the current directory is.
3. Otherwise the current directory.

With `--global` the output goes to the user-level folders instead:

| Target | Skills | Always-on rules |
|---|---|---|
| Codex | `~/.agents/skills` | `~/.codex/AGENTS.md` (or `$CODEX_HOME/AGENTS.md`) |
| Claude Code | `~/.claude/skills` | `~/.claude/CLAUDE.md` (or `$CLAUDE_CONFIG_DIR/CLAUDE.md`) |
| Kiro | `~/.kiro/skills` | `~/.kiro/steering/<name>.md` |

### Options

| Option | Meaning |
|---|---|
| `-t, --target {codex,claude,kiro}` | The agent to convert for. Default `codex`. |
| `--sync [PROJECT]` | Convert the other agents' skills of a project (default `.`). No `SOURCE` needed. |
| `-d, --dest DIR` | Project root to write into. |
| `-g, --global` | Write to the user-level folders. |
| `-n, --name NAME` | Name for the result. Only when the source yields exactly one item. |
| `--as {auto,skill,rule}` | Force how the source is interpreted. Default `auto`. |
| `--symlinks {auto,always,never}` | Link to local skill folders instead of copying. Default `auto`. |
| `-f, --force` | Overwrite converted files that already exist and differ. |
| `--dry-run` | Show what would happen without writing. |
| `--no-exclude` | Do not list generated skills in `.git/info/exclude`. |

### Output status

Every file gets one status line:

| Status | Meaning |
|---|---|
| `create` / `overwrite` / `update` | The file is written (`would ...` with `--dry-run`). |
| `link` / `relink` | A symlink is created, or an existing link is replaced (`--force`). |
| `unchanged` | The destination already has identical content. Nothing is written. |
| `kept` | A file that is only ever created (`agents/openai.yaml`) already exists. |
| `differs` | `--sync` only: the skill exists, was not generated by the script, and differs from the other agent's copy. Left as is. |
| `conflict` | The destination exists and differs. Nothing is written until you pass `--force`. |
| `source` | The destination is a source file with different content. It is never modified. |
| `skip` | The file is deliberately not copied (`agents/openai.yaml` for Kiro and Claude). |

After the file lines, an `exclude` line shows the `.git/info/exclude` file that was updated. Re-running the script is safe. Rules written to `AGENTS.md` or `CLAUDE.md` are wrapped in `<!-- BEGIN converted-rule: NAME -->` and `<!-- END converted-rule: NAME -->` comments, so a second run replaces the block instead of adding another one.

### Limits

- Only the front matter and file layout are converted. The text of a skill is copied as written, so a Codex-style `$skill-name` mention inside it stays as is (Kiro uses `/skill-name`).
- References to tools the target does not have, such as an MCP server, are not translated.
- Kiro has no skill file of its own beyond `SKILL.md`, so nothing Kiro-specific is created for skills.

## Make agents convert skills on their own

Goal: when you open a project in Codex, Claude Code or Kiro and it contains skills written for one of the others, the agent runs `convert-skill.py --sync` once and the skills become available.

The instruction is a short Markdown rule, [`scripts/sync-skills-rule.md`](scripts/sync-skills-rule.md). It is the same for all agents; each agent picks its own `--target`. Put it in the agent's **user-level** instruction file, so it applies to every project:

| Agent | macOS / Linux | Windows | Notes |
|---|---|---|---|
| Codex | `~/.codex/AGENTS.md` | `%USERPROFILE%\.codex\AGENTS.md` | Read at every start. Uses `$CODEX_HOME` instead of `~/.codex` when it is set. |
| Claude Code | `~/.claude/CLAUDE.md` | `%USERPROFILE%\.claude\CLAUDE.md` | Read at every start. Uses `$CLAUDE_CONFIG_DIR` when it is set. |
| Kiro | `~/.kiro/steering/sync-skills-rule.md` | `%USERPROFILE%\.kiro\steering\sync-skills-rule.md` | A steering file with `inclusion: always` at the top. It must be a real file: Kiro ignores symlinked steering files. |

### Install the rule with the script

`convert-skill.py` can install the rule itself. It writes to the right place for each agent and operating system, adds the Kiro front matter, and replaces its own block when you run it again:

```sh
python3 scripts/convert-skill.py scripts/sync-skills-rule.md --as rule --global --target codex
python3 scripts/convert-skill.py scripts/sync-skills-rule.md --as rule --global --target claude
python3 scripts/convert-skill.py scripts/sync-skills-rule.md --as rule --global --target kiro
```

Without a checkout of this repository:

```sh
curl -fsSL https://raw.githubusercontent.com/costinEEST/dotfiles/main/scripts/convert-skill.py \
  | python3 - https://raw.githubusercontent.com/costinEEST/dotfiles/main/scripts/sync-skills-rule.md --as rule --global --target codex
```

### What the rule makes the agent do

1. At the start of a session, look in the project for the skills folders of the other two agents (`.agents/skills`, `.claude/skills`, `.kiro/skills`). If there are none, do nothing.
2. Otherwise run `curl -fsSL .../convert-skill.py | python3 - --sync --target <itself>` from the project root.
3. Report which skills were added. If a skill `differs`, tell the user and do not overwrite it.
4. Never use `--force`, `--global` or `--dest`, and never edit converted files by hand. Do not commit them: they are listed in `.git/info/exclude`, so only the original skill folder is committed.

### Security

The rule makes the agent run a script downloaded from this repository every session. Anyone who can change `main` can change what runs. To limit that, replace `main` in the URLs of the rule with a commit hash, or save `convert-skill.py` on the machine and change the command to `python3 <path>/convert-skill.py --sync --target <agent>`. For a private repository `curl` cannot download the script; use the local copy or `gh api` as shown above.
