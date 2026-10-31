# Keep skills available across coding agents

This rule applies in every project you open, whichever coding agent you are (Codex, Claude Code or Kiro).

Each agent reads skills from its own folder in the project:

- Codex: `.agents/skills/`
- Claude Code: `.claude/skills/`
- Kiro: `.kiro/skills/`

A project may only have skills for another agent. When that happens, convert them for yourself with `convert-skill.py`. It leaves the other agent's files untouched.

## When

Once at the start of a session, from the project root (the folder that contains `.git`; if there is none, the folder you were opened in).

1. Look for the skills folders of the two other agents. If none exists, do nothing and do not mention this rule.
2. If at least one exists, run the command for your own agent from the project root.

## Command

Codex:

```sh
curl -fsSL https://raw.githubusercontent.com/costinEEST/dotfiles/main/scripts/convert-skill.py | python3 - --sync --target codex
```

Claude Code:

```sh
curl -fsSL https://raw.githubusercontent.com/costinEEST/dotfiles/main/scripts/convert-skill.py | python3 - --sync --target claude
```

Kiro:

```sh
curl -fsSL https://raw.githubusercontent.com/costinEEST/dotfiles/main/scripts/convert-skill.py | python3 - --sync --target kiro
```

On Windows use `python` (or `py -3`) instead of `python3`, and `curl.exe` in PowerShell.

If the script is saved on this machine, run `python3 <path>/convert-skill.py --sync --target <your agent>` instead of using curl.

## What to do with the result

- The script prints one line per file. `link` and `create` mean a skill was added, `overwrite` means a generated copy was refreshed because the original changed, and `unchanged` means it was already there.
- `differs` means a skill with that name already exists and its `SKILL.md` is not the same as the other agent's copy. Tell the user which skill and stop there. Do not overwrite it.
- Tell the user in one or two sentences which skills were added. If none were, say nothing.
- A skill added during the session may not be available until the agent restarts. If you need it now, read its `SKILL.md` directly.

## Limits

- Never add `--force`, `--global` or `--dest`, and do not edit the converted files by hand. Change the original skill and run the command again.
- Do not commit the new files. They are listed in `.git/info/exclude`, so `git status` does not show them. Only the original skill folder belongs in Git.
- If the command fails (no network, no Python, no curl), say so in one sentence and carry on with the task.
