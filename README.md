# TODO 

- https://github.com/rajrawat37/dotfiles/blob/main/bootstrap.sh

# Scripts

| File | What it is |
|---|---|
| [`scripts/convert-skill.py`](scripts/convert-skill.py) | Makes the skills and rules of one coding agent (Codex, Claude Code, Kiro, Cursor, Copilot, ...) available to Codex, Claude Code or Kiro. Python 3.9+, no dependencies. **Documentation: [`scripts/convert-skill.md`](scripts/convert-skill.md).** |
| [`scripts/sync-skills-rule.md`](scripts/sync-skills-rule.md) | A user-level rule that makes Codex, Claude Code and Kiro run `convert-skill.py --sync` once per session, so a project's skills work in whichever agent opens it. See [Make agents convert skills on their own](scripts/convert-skill.md#make-agents-convert-skills-on-their-own). |

Quick start, from a project root:

```sh
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/costinEEST/dotfiles/main/scripts/convert-skill.py | python3 - --sync --target kiro'
```
