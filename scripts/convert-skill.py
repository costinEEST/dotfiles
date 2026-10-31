#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Convert agent skills and rule files from any common format into the format
used by a target coding agent: Codex (default), Claude Code or Kiro.

Python 3.9+, standard library only.

The script never modifies its source. It adds what the target agent needs next
to it, and avoids duplicating files whenever the target can use them as they are.

How a skill reaches another agent
  - Codex follows symlinks, so a skill that already lives in another agent's folder
    (.claude/skills/x, .kiro/skills/x) is LINKED: .agents/skills/x -> ../../.kiro/skills/x.
    No files are duplicated. Codex-only files (agents/openai.yaml) are created inside
    the shared folder, where the other agents ignore them.
  - The Kiro IDE ignores symlinked skills (kirodotdev/Kiro#6401), so Kiro gets real
    files: .kiro/skills/x/SKILL.md plus the bundled files it can use. This is the one
    case where a copy is needed. Codex-only files are not copied.
  - Claude Code gets a copy as well (symlink support is not assumed).
  - `--symlinks always|never` overrides this choice.

Example: `my-project/.agents` holds Codex skills. Running

    python3 convert-skill.py my-project/.agents --target kiro

creates `my-project/.kiro/skills/<name>/SKILL.md` and leaves `.agents` exactly as it
was. Running it with `--target codex` changes nothing (every file is `unchanged`).

What to commit to Git
  Commit the ORIGINAL skill folder only. The links and copies this script generates
  for other agents are written to a managed block in `.git/info/exclude` (per clone,
  never committed), so `git status` stays clean. The block also records which skills
  the script owns: a generated copy is refreshed when the original changes, while a
  skill you wrote by hand is never overwritten without --force. `.gitignore` is not
  used because it is shared and cannot ignore .kiro/ or .claude/ as a whole (they hold
  specs, steering and settings that belong in Git). `--no-exclude` turns this off.

Make a whole project work for one agent
    python3 convert-skill.py --sync --target codex      # run in the project root
looks for .agents/skills, .claude/skills and .kiro/skills, converts the skills that
the target does not have yet and leaves existing ones alone. This is the command
the "sync skills" rule in the dotfiles readme tells an agent to run.

Run it straight from GitHub:

    curl -fsSL https://raw.githubusercontent.com/costinEEST/dotfiles/main/scripts/convert-skill.py \\
      | python3 - <SOURCE> [options]

If the dotfiles repo is private, fetch the script with the GitHub CLI instead:

    gh api -H 'Accept: application/vnd.github.raw' \\
      repos/costinEEST/dotfiles/contents/scripts/convert-skill.py | python3 - <SOURCE> [options]

SOURCE is a local path or a URL. Supported URLs:
  - https://github.com/OWNER/REPO[/tree|blob/REF/PATH]
  - https://raw.githubusercontent.com/OWNER/REPO/REF/PATH
  - any other http(s) URL that serves a single file
Private repositories work when GITHUB_TOKEN or GH_TOKEN is set, or when the
GitHub CLI (`gh auth login`) is signed in. The token is only sent to GitHub.

Recognised sources (detected automatically)
  Agent Skills folders        SKILL.md plus bundled files (.claude/skills, .agents/skills, ...)
  Cursor rules                *.mdc, .cursorrules
  Copilot instructions        copilot-instructions.md, *.instructions.md
  Windsurf / Cline rules      .windsurf/rules, .windsurfrules, .clinerules
  Claude / Gemini / generic   CLAUDE.md, .claude/rules/*.md, GEMINI.md, AGENTS.md
  Kiro steering               .kiro/steering/*.md

What each target produces
  codex   skills                 .agents/skills/<name>/                (--global: ~/.agents/skills)
          always-on rules        a managed block in AGENTS.md          (--global: ~/.codex/AGENTS.md)
          other rules            a skill with a generated description
  claude  skills                 .claude/skills/<name>/                (--global: ~/.claude/skills)
          always-on rules        a managed block in CLAUDE.md          (--global: ~/.claude/CLAUDE.md)
          other rules            a skill with a generated description
  kiro    skills                 .kiro/skills/<name>/                  (--global: ~/.kiro/skills)
          rules                  .kiro/steering/<name>.md with the matching `inclusion:` mode

Where the output goes (project-level installs)
  1. `--dest DIR` if given (with --sync: the project).
  2. Otherwise, for a local source inside a tool folder (.agents, .claude, .codex, .kiro,
     .cursor, .windsurf, .github), the folder that contains that tool folder. So
     `.../my-project/.agents` writes into `.../my-project`, wherever you run the command from.
  3. Otherwise the current directory.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Agent Skills spec limits (enforced by both Codex and Kiro).
MAX_NAME = 64
MAX_DESCRIPTION = 1024

# Safety valve for the slow GitHub fallback that lists one folder per request.
MAX_GITHUB_DIRS = 300

# Never descend into these folders, locally or on GitHub.
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".DS_Store"}

# Extensions that can hold rule text.
TEXT_EXTS = (".md", ".mdc", ".txt")

# File names (lower-cased) that are rule files wherever they appear.
KNOWN_RULE_FILES = {
    "agents.md",
    "claude.md",
    "gemini.md",
    "copilot-instructions.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
}

# Folders whose text files are rule files, one entry per tool.
RULE_DIRS = (
    ".cursor/rules/",
    ".windsurf/rules/",
    ".clinerules/",
    ".claude/rules/",
    ".kiro/steering/",
    ".github/instructions/",
)

# Frontmatter keys that mark a file as a rule (not a skill) when deciding what a
# lone Markdown file with name+description frontmatter is.
RULE_KEYS = {"alwaysApply", "globs", "applyTo", "trigger", "inclusion", "paths", "fileMatchPattern"}

# Globs that mean "every file", i.e. the rule is effectively always on.
GLOBAL_GLOBS = {"*", "**", "**/*"}

# Hidden tool folders. Used to guess the project root from a source path.
TOOL_DIRS = {".agents", ".claude", ".codex", ".kiro", ".cursor", ".windsurf", ".github"}


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #


def die(message: str) -> None:
    """Print an error and exit. Declared as returning None so callers read naturally."""
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Source loading
#
# Every source (local folder, GitHub URL, single file URL) is turned into the
# same in-memory `Tree`: a mapping from relative path to a function that returns
# the file's bytes. File contents are fetched lazily, so we only download what
# we actually convert.
# --------------------------------------------------------------------------- #


class Tree:
    """A read-only set of files: relative POSIX path -> function returning bytes."""

    def __init__(self, root_name: str, label: str) -> None:
        # Name of the folder/file the user pointed at; used as a fallback skill name.
        self.root_name = root_name
        # Human-readable origin (path or URL), shown in generated comments.
        self.label = label
        self.files: dict[str, Callable[[], bytes]] = {}
        # Relative paths that carry the executable bit at the source.
        self.executable: set[str] = set()
        # Absolute paths of the source files on disk. Empty for remote sources.
        # The writer uses this to guarantee it never modifies a source file.
        self.local_paths: set[Path] = set()
        # The folder on disk this tree was read from (None for remote sources and
        # single files). Skills found in it can be symlinked instead of copied.
        self.local_root: Path | None = None


def load_local(src: str) -> Tree:
    p = Path(src).expanduser()
    if not p.exists():
        die(f"path does not exist: {src}")
    if p.is_file() and p.name == "SKILL.md":
        p = p.parent  # a SKILL.md always means "the whole skill folder"
    p = p.resolve()

    if p.is_file():
        tree = Tree(p.stem, str(p))
        tree.files[p.name] = functools.partial(p.read_bytes)
        tree.local_paths.add(p)
        return tree

    tree = Tree(p.name, str(p))
    tree.local_root = p
    for dirpath, dirnames, filenames in os.walk(p):
        # Prune in place so os.walk skips ignored folders. os.walk does not
        # follow symlinked folders by default, which is what we want.
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            if full.is_symlink():
                warn(f"skipping symlink {full}")
                continue
            rel = full.relative_to(p).as_posix()
            tree.files[rel] = functools.partial(full.read_bytes)
            tree.local_paths.add(full.resolve())
            if os.access(full, os.X_OK):
                tree.executable.add(rel)
    return tree


class _KeepAuthOnSameHostOnly(urllib.request.HTTPRedirectHandler):
    """Never forward the Authorization header to a different host after a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlparse(req.full_url).hostname
            new_host = urllib.parse.urlparse(newurl).hostname
            if old_host != new_host:
                new.headers.pop("Authorization", None)
                new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_KeepAuthOnSameHostOnly)


def http_get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """GET a URL and return (status, body). HTTP errors are returned, not raised."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        die(f"network error for {url}: {exc.reason}")
    return 0, b""  # unreachable: die() exits


def find_github_token() -> str | None:
    """Look for a GitHub token: environment first, then the GitHub CLI."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var].strip()
    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class GitHub:
    """Minimal GitHub REST client. Works anonymously for public repos."""

    API = "https://api.github.com"

    def __init__(self) -> None:
        self.token = find_github_token()

    def get(self, url: str, accept: str = "application/vnd.github+json") -> tuple[int, bytes]:
        headers = {
            "Accept": accept,
            "User-Agent": "convert-skill",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return http_get(url, headers)

    def contents(self, owner: str, repo: str, path: str, ref: str | None) -> tuple[int, Any]:
        """Contents API: a dict for a file, a list for a folder."""
        url = f"{self.API}/repos/{owner}/{repo}/contents"
        if path:
            url += "/" + urllib.parse.quote(path, safe="/")
        if ref:
            url += "?" + urllib.parse.urlencode({"ref": ref})
        status, data = self.get(url)
        if status == 200:
            return status, json.loads(data)
        return status, data

    def hint(self) -> str:
        if self.token:
            return "Check the URL and that your token can read the repository."
        return (
            "For private repositories set GITHUB_TOKEN (or GH_TOKEN), or sign in with "
            "`gh auth login`. Unauthenticated requests are also rate limited."
        )


def parse_github_url(url: str) -> dict[str, Any] | None:
    """Split a GitHub URL into owner, repo, kind and the ref/path segments.

    The ref and path are returned together in `rest` because a branch name may
    contain slashes; `load_github` works out where the ref ends.
    """
    u = urllib.parse.urlparse(url)
    parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/") if p]
    if u.hostname == "raw.githubusercontent.com" and len(parts) >= 3:
        return {"owner": parts[0], "repo": parts[1], "kind": "raw", "rest": parts[2:]}
    if u.hostname in ("github.com", "www.github.com") and len(parts) >= 2:
        owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
        if len(parts) == 2:
            return {"owner": owner, "repo": repo, "kind": "repo", "rest": []}
        if parts[2] in ("blob", "tree", "raw") and len(parts) >= 4:
            return {"owner": owner, "repo": repo, "kind": parts[2], "rest": parts[3:]}
    return None


def load_github(info: dict[str, Any], label: str) -> Tree:
    gh = GitHub()
    owner, repo, rest = info["owner"], info["repo"], info["rest"]

    if info["kind"] == "repo":
        candidates: list[tuple[str | None, str]] = [(None, "")]  # default branch, repo root
    else:
        # A ref may contain slashes (feature/x), so try every ref/path split
        # until GitHub finds something. Most refs have no slash and hit first try.
        candidates = [("/".join(rest[:i]), "/".join(rest[i:])) for i in range(1, len(rest) + 1)]

    result: Any = None
    ref: str | None = None
    path = ""
    for ref, path in candidates:
        status, result = gh.contents(owner, repo, path, ref)
        if status == 200:
            break
        if status in (401, 403):
            die(f"GitHub refused access ({status}) for {label}. {gh.hint()}")
    else:
        # GitHub answers 404 (not 401) for private repos you cannot see.
        die(f"not found on GitHub: {label}. {gh.hint()}")

    # Pointing at a SKILL.md means "the whole skill folder": list its parent.
    if isinstance(result, dict) and result.get("name") == "SKILL.md":
        path = posixpath.dirname(path)
        status, result = gh.contents(owner, repo, path, ref)
        if status != 200:
            die(f"could not list the folder that contains {label} (HTTP {status})")

    def loader(url: str) -> Callable[[], bytes]:
        """Return a function that downloads `url` once and caches the bytes."""

        @functools.cache
        def load() -> bytes:
            status, data = gh.get(url, accept="application/vnd.github.raw+json")
            if status != 200:
                die(f"could not download {url} (HTTP {status})")
            return data

        return load

    if isinstance(result, dict):  # a single file, not a folder
        name = result["name"]
        tree = Tree(posixpath.splitext(name)[0], label)
        tree.files[name] = loader(result["url"])
        return tree

    root_name = posixpath.basename(path) or repo
    tree = Tree(root_name, label)

    # Fast path: one recursive Git Trees call instead of one request per folder.
    # It does not resolve every ref that contains a slash, so fall back to walking.
    tree_url = f"{gh.API}/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(ref or 'HEAD', safe='')}"
    if path:
        tree_url += ":" + urllib.parse.quote(path, safe="/")
    status, data = gh.get(tree_url + "?recursive=1")
    if status == 200:
        listing = json.loads(data)
        if not listing.get("truncated"):  # truncated = repo too big for one call
            for entry in listing["tree"]:
                rel = entry["path"]
                if entry["type"] != "blob" or any(part in IGNORED_DIRS for part in rel.split("/")):
                    continue
                if entry.get("mode") == "120000":  # symlink
                    warn(f"skipping symlink {rel}")
                    continue
                tree.files[rel] = loader(entry["url"])
                if entry.get("mode") == "100755":
                    tree.executable.add(rel)
            return tree

    # Slow path: list folder by folder with the Contents API.
    budget = [MAX_GITHUB_DIRS]

    def walk(entries: list[dict[str, Any]], prefix: str) -> None:
        for entry in entries:
            kind, name = entry.get("type"), entry["name"]
            rel = prefix + name
            if kind == "file":
                tree.files[rel] = loader(entry["url"])
            elif kind == "dir":
                if name in IGNORED_DIRS:
                    continue
                if budget[0] <= 0:
                    warn(
                        f"directory limit ({MAX_GITHUB_DIRS}) reached; skipping {rel}. "
                        "Point at a deeper folder to include it."
                    )
                    continue
                budget[0] -= 1
                status, data = gh.get(entry["url"])
                if status != 200:
                    die(f"could not list {rel} (HTTP {status})")
                walk(json.loads(data), rel + "/")
            else:
                warn(f"skipping {kind or 'unknown'} entry {rel}")

    walk(result, "")
    return tree


def load_url(url: str) -> Tree:
    """A plain URL to one file (not GitHub)."""
    u = urllib.parse.urlparse(url)
    name = posixpath.basename(urllib.parse.unquote(u.path)) or "download"
    if name == "SKILL.md":
        warn("a plain URL to SKILL.md cannot fetch bundled files; use a GitHub URL to get the whole folder")
    tree = Tree(posixpath.splitext(name)[0], url)

    def load() -> bytes:
        status, data = http_get(url, {"User-Agent": "convert-skill"})
        if status != 200:
            die(f"could not download {url} (HTTP {status})")
        return data

    tree.files[name] = load
    return tree


def load_source(src: str) -> Tree:
    if re.match(r"^https?://", src):
        info = parse_github_url(src)
        if info:
            return load_github(info, src)
        if (urllib.parse.urlparse(src).hostname or "").endswith("github.com"):
            die(f"unsupported GitHub URL (use a repo, tree, blob or raw URL): {src}")
        return load_url(src)
    return load_local(src)


def infer_dest(sources: list[str]) -> Path | None:
    """Guess the project root from local sources that live inside a tool folder.

    `/work/proj/.agents/skills/foo` -> `/work/proj`. Returns None unless every
    source is local, sits inside a tool folder, and they all agree on one root.
    """
    roots: set[Path] = set()
    for src in sources:
        if re.match(r"^https?://", src):
            return None
        p = Path(src).expanduser().resolve()
        for ancestor in [p, *p.parents]:  # nearest enclosing tool folder wins
            if ancestor.name in TOOL_DIRS:
                roots.add(ancestor.parent)
                break
        else:
            return None
    return roots.pop() if len(roots) == 1 else None


# --------------------------------------------------------------------------- #
# Frontmatter
#
# SKILL.md and most rule files start with a YAML block between `---` lines. We
# only need a small YAML subset (scalars, flow lists, block lists, block
# scalars), so we parse it by hand to stay dependency-free.
# --------------------------------------------------------------------------- #

_FM_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.S)


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str | None, str]:
    """Return (parsed frontmatter or None, raw frontmatter text or None, body)."""
    m = _FM_RE.match(text)
    if not m:
        return None, None, text
    raw = m.group(1) or ""
    return parse_yaml(raw), raw, text[m.end() :]


def unquote(value: str) -> str:
    """Remove YAML quotes around a scalar and decode escapes."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        if v[0] == '"':
            try:
                return str(json.loads(v))  # YAML double-quoted == JSON string for our purposes
            except ValueError:
                return v[1:-1]
        return v[1:-1].replace("''", "'")  # single quotes: only '' is an escape
    return v


def split_top(s: str) -> list[str]:
    """Split on commas that are outside quotes, braces and brackets.

    Needed because a glob like `**/*.{ts,tsx}` contains a comma that must not split it.
    """
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _dedent(lines: list[str]) -> list[str]:
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    cut = min(indents) if indents else 0
    return [line[cut:] if line.strip() else "" for line in lines]


def parse_yaml(raw: str) -> dict[str, Any]:
    """Parse the YAML subset used by skill and rule frontmatter."""
    lines = raw.splitlines()
    out: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z_][\w.-]*)\s*:\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue  # blank line, comment, or something we do not understand
        key, val = m.group(1), m.group(2).strip()

        # Gather the lines that belong to this key: indented lines, blank lines,
        # and (for `key:` followed by `- item`) unindented list items.
        cont: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip() or nxt[0] in " \t" or (val == "" and re.match(r"^-(\s|$)", nxt)):
                cont.append(nxt)
                i += 1
            else:
                break
        while cont and not cont[-1].strip():
            cont.pop()

        if re.match(r"^[>|][+-]?\d*$", val):
            # Block scalar: `>` folds lines into one paragraph, `|` keeps newlines.
            block = _dedent(cont)
            if val.startswith("|"):
                out[key] = "\n".join(block).strip()
            else:
                paras = re.split(r"\n\s*\n", "\n".join(block))
                out[key] = "\n".join(" ".join(p.split()) for p in paras).strip()
        elif val == "":
            items = [c.strip() for c in cont if c.strip()]
            if items and all(c.startswith("-") for c in items):
                out[key] = [unquote(c[1:].strip()) for c in items]  # block list
            else:
                # Nested mapping (e.g. `metadata:`). We keep it as raw text; we
                # never need its structure, and the original text is preserved
                # separately when the frontmatter is rewritten.
                out[key] = "\n".join(_dedent(cont)).strip() if cont else ""
        elif val.startswith("[") and val.endswith("]"):
            out[key] = [unquote(x) for x in split_top(val[1:-1])]  # flow list
        else:
            if cont:  # plain scalar continued on indented lines
                val = " ".join([val] + [c.strip() for c in cont if c.strip()])
            if val[:1] in "\"'":
                scalar: Any = unquote(val)
            else:
                scalar = re.sub(r"\s+#.*$", "", val).strip()  # drop trailing comment
                if scalar.lower() in ("true", "false"):
                    scalar = scalar.lower() == "true"
            out[key] = scalar
    return out


def drop_keys(raw_fm: str, keys: set[str]) -> str:
    """Remove top-level keys (and their continuation lines) from raw frontmatter text.

    Used when rewriting `name`/`description` while keeping every other field
    (license, metadata, ...) exactly as the author wrote it.
    """
    kept: list[str] = []
    skipping = False
    for line in raw_fm.splitlines():
        m = re.match(r"^([A-Za-z_][\w.-]*)\s*:", line)
        if m:
            skipping = m.group(1) in keys
            if skipping:
                continue
        elif skipping and (not line.strip() or line[0] in " \t"):
            continue  # continuation of a dropped key
        else:
            skipping = False
        kept.append(line)
    return "\n".join(kept).strip("\n")


def to_list(value: Any) -> list[str]:
    """Normalise a glob field (string, comma-separated string or list) to a list."""
    if value is None or value is False:
        return []
    items = [str(v) for v in value] if isinstance(value, list) else split_top(str(value))
    return [unquote(i) for i in items if unquote(i)]


def truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def slugify(s: str) -> str:
    """Lowercase letters, digits and single hyphens, at most MAX_NAME characters."""
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:MAX_NAME].strip("-") or "converted"


def first_paragraph(body: str) -> str:
    """First paragraph of prose, skipping headings, code fences and comments."""
    for para in re.split(r"\n\s*\n", body.strip()):
        lines = [
            line.strip()
            for line in para.splitlines()
            if line.strip() and not line.strip().startswith(("#", "```", "---", "<!--"))
        ]
        if lines:
            return " ".join(lines)
    return ""


def clip(text: str, limit: int) -> str:
    """Shorten to `limit` characters on a word boundary, ending with '...'."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 3].rsplit(" ", 1)[0] or text[: limit - 3]
    return cut.rstrip() + "..."


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Normalised model
#
# Whatever the source format, we reduce it to two shapes:
#   Skill - a folder with SKILL.md and bundled files
#   Rule  - a block of instructions plus WHEN it applies (always/glob/auto/manual)
# Targets then render those two shapes in their own format.
# --------------------------------------------------------------------------- #


@dataclass
class Skill:
    default_name: str  # used when SKILL.md has no `name`
    text: str  # SKILL.md contents
    files: dict[str, Callable[[], bytes]]  # bundled files, relative to the skill folder
    origin: str
    executable: set[str] = field(default_factory=set)  # bundled files with the exec bit
    forced_name: str | None = None  # set by --name
    # The skill folder on disk, when the source is local. Lets a target link to it
    # instead of copying it.
    source_dir: Path | None = None


@dataclass
class Rule:
    name: str
    body: str
    mode: str  # always | glob | auto | manual
    globs: list[str] = field(default_factory=list)
    description: str = ""
    origin: str = ""


def derive_mode(fm: dict[str, Any], is_mdc: bool) -> tuple[str, list[str], str]:
    """Work out when a rule applies, from whichever tool's frontmatter it uses."""
    desc = " ".join(str(fm.get("description") or "").split())
    globs: list[str] = []
    for key in ("globs", "paths", "applyTo", "fileMatchPattern"):
        globs = to_list(fm.get(key))
        if globs:
            break

    if "inclusion" in fm:  # Kiro steering
        mode = {"always": "always", "filematch": "glob", "auto": "auto", "manual": "manual"}.get(
            str(fm["inclusion"]).strip().lower(), "always"
        )
    elif "trigger" in fm:  # Windsurf
        mode = {"always_on": "always", "glob": "glob", "model_decision": "auto", "manual": "manual"}.get(
            str(fm["trigger"]).strip().lower(), "always"
        )
    elif "alwaysApply" in fm or is_mdc:  # Cursor
        if truthy(fm.get("alwaysApply")):
            mode = "always"
        elif globs:
            mode = "glob"
        elif desc:
            mode = "auto"
        else:
            mode = "manual"
    elif globs:  # Copilot `applyTo`, Claude `paths`
        mode = "glob"
    else:  # AGENTS.md, CLAUDE.md, plain Markdown
        mode = "always"

    # Repair combinations that cannot work in the target.
    if mode == "glob" and not globs:
        mode = "always"
    if mode == "auto" and not desc:
        mode = "manual"
    if mode == "glob" and all(g in GLOBAL_GLOBS for g in globs):
        mode = "always"
    return mode, globs, desc


def parse_rule(path: str, text: str, root_name: str, origin: str) -> Rule | None:
    fm, _raw, body = split_frontmatter(text)
    fm = fm or {}
    body = body.strip("\n")
    if not body.strip():
        warn(f"skipping empty rule {path}")
        return None
    base = posixpath.basename(path)
    mode, globs, desc = derive_mode(fm, base.lower().endswith(".mdc"))

    # Rule name: frontmatter `name`, else the file name without extension.
    stem = re.sub(r"\.(md|mdc|txt)$", "", base, flags=re.I)
    stem = re.sub(r"\.instructions$", "", stem, flags=re.I).lstrip(".")
    name = slugify(str(fm.get("name") or stem or root_name))

    # AGENTS.md / CLAUDE.md in a sub-folder only applies to that folder, so turn
    # it into a glob rule for that folder instead of making it global.
    folder = posixpath.dirname(path)
    if (
        base.lower() in ("agents.md", "claude.md", "gemini.md")
        and folder
        and not folder.startswith(".")
        and mode == "always"
    ):
        mode, globs = "glob", [folder.rstrip("/") + "/**"]
        name = slugify(f"{posixpath.basename(folder)}-{stem}")
        desc = desc or f"Instructions from {path}"
    # A single-file source is already named by `origin`; do not repeat the file name.
    same_file = not path or origin.rstrip("/").endswith("/" + path) or origin == path
    return Rule(
        name=name,
        body=body,
        mode=mode,
        globs=globs,
        description=desc,
        origin=origin if same_file else f"{origin}:{path}",
    )


def rule_candidate(path: str) -> bool:
    """Is this path a rule file, judging by its name and location?"""
    low = path.lower()
    base = posixpath.basename(low)
    if base in KNOWN_RULE_FILES or base.endswith(".mdc") or base.endswith(".instructions.md"):
        return True
    return posixpath.splitext(base)[1] in TEXT_EXTS and any(("/" + d) in ("/" + low) for d in RULE_DIRS)


def looks_like_skill(text: str) -> bool:
    """A lone Markdown file with name+description frontmatter and no rule keys."""
    fm, _raw, _body = split_frontmatter(text)
    return bool(fm and fm.get("name") and fm.get("description") and not (RULE_KEYS & set(fm)))


def discover(tree: Tree, mode: str) -> tuple[list[Skill], list[Rule]]:
    """Find every skill and rule in a source tree. `mode` is --as: auto|skill|rule."""
    skills: list[Skill] = []
    rules: list[Rule] = []
    paths = sorted(tree.files)
    owner: dict[str, str] = {}  # file -> the skill folder that owns it

    if mode != "rule":
        # A skill is any folder containing SKILL.md. Each file belongs to the
        # deepest such folder above it, so nested skills do not swallow each other.
        sdirs = sorted(
            {posixpath.dirname(p) for p in paths if posixpath.basename(p) == "SKILL.md"}, key=len, reverse=True
        )
        for p in paths:
            for d in sdirs:
                if d == "" or p.startswith(d + "/"):
                    owner[p] = d
                    break
        for d in sorted(sdirs):
            prefix = d + "/" if d else ""
            files = {p[len(prefix) :]: tree.files[p] for p in paths if owner.get(p) == d}
            skill_md = files.pop("SKILL.md")
            exec_rel = {rel for rel in files if prefix + rel in tree.executable}
            default = posixpath.basename(d) if d else tree.root_name
            source_dir = (tree.local_root / d) if tree.local_root else None
            skills.append(
                Skill(
                    default,
                    decode(skill_md()),
                    files,
                    f"{tree.label}:{d}" if d else tree.label,
                    exec_rel,
                    source_dir=source_dir,
                )
            )

    if len(paths) == 1 and not skills:  # one lone file: skill or rule?
        p = paths[0]
        text = decode(tree.files[p]())
        if mode == "skill" or (mode == "auto" and looks_like_skill(text)):
            skills.append(Skill(slugify(posixpath.splitext(posixpath.basename(p))[0]), text, {}, tree.label))
        elif mode in ("auto", "rule") and (rule_candidate(p) or posixpath.splitext(p)[1].lower() in TEXT_EXTS):
            r = parse_rule(p, text, tree.root_name, tree.label)
            if r:
                rules.append(r)
        return skills, rules

    if mode == "skill" and not skills:
        die(f"--as skill needs a SKILL.md, but none was found in {tree.label}")

    if mode != "skill":
        # Rules: every recognised rule file that is not part of a skill.
        for p in paths:
            if p not in owner and rule_candidate(p):
                r = parse_rule(p, decode(tree.files[p]()), tree.root_name, tree.label)
                if r:
                    rules.append(r)
        if not skills and not rules:
            # Nothing recognised: as a last resort treat plain text files as rules.
            skip = ("readme", "license", "changelog", "contributing", "code_of_conduct")
            fallback = [
                p
                for p in paths
                if posixpath.splitext(p)[1].lower() in TEXT_EXTS and not posixpath.basename(p).lower().startswith(skip)
            ]
            if fallback:
                warn(f"no SKILL.md or known rule files in {tree.label}; treating {len(fallback)} text file(s) as rules")
            for p in fallback:
                r = parse_rule(p, decode(tree.files[p]()), tree.root_name, tree.label)
                if r:
                    rules.append(r)
    return skills, rules


# --------------------------------------------------------------------------- #
# Output actions
#
# Targets do not touch the disk. They return a list of actions, which `apply`
# first plans (create / overwrite / unchanged / conflict ...) and then executes.
# That makes --dry-run exact and lets us refuse a conflict before writing anything.
# --------------------------------------------------------------------------- #


@dataclass
class Write:
    """Write a whole file."""

    path: Path
    data: bytes
    exclusive: bool = False  # differing existing file needs --force
    executable: bool = False
    create_only: bool = False  # leave an existing file alone, whatever it contains
    group: Path | None = None  # the skill folder this file belongs to (for .git/info/exclude)


@dataclass
class Block:
    """Insert or replace a named block inside a shared file (AGENTS.md)."""

    path: Path
    name: str
    body: str
    origin: str


@dataclass
class Skip:
    """Informational: a source file that is deliberately not converted."""

    description: str


@dataclass
class Link:
    """Make `path` a symlink to the skill folder `source` instead of copying it.

    Avoids duplicating files for agents that follow symlinks. If the link cannot
    be created (for example on Windows without symlink rights), or `path` is
    already a real folder, `fallback` (a normal copy) is used instead.
    """

    path: Path
    source: Path
    fallback: list[Any]
    owned: bool = False  # created by this script earlier: may be replaced without --force


@dataclass
class Present:
    """--sync only: a skill folder already exists, so leave it alone.

    Reported as `unchanged` when its SKILL.md matches what we would write, and as
    `differs` otherwise (never overwritten, never an error).
    """

    path: Path
    skill_md: bytes


Action = Any


def normalize_skill(sk: Skill) -> tuple[str, str]:
    """Return (name, SKILL.md text) that satisfy the Agent Skills spec.

    A SKILL.md that is already valid is returned byte-for-byte. Otherwise only
    `name` and `description` are rewritten; every other field and the body stay.
    """
    fm, raw_fm, body = split_frontmatter(sk.text)
    fm = fm or {}

    name_in = str(fm.get("name") or "").strip()
    name = slugify(sk.forced_name or name_in or sk.default_name)
    if not sk.forced_name and name_in and name_in != name:
        warn(f"skill name '{name_in}' changed to '{name}' (lowercase letters, digits, hyphens; max {MAX_NAME})")

    desc_in = fm.get("description")
    desc = " ".join(str(desc_in or "").split())  # description must be a single line
    if not desc:
        desc = clip(" ".join(first_paragraph(body).split()), 300) or f"Instructions for {name}."
        warn(f"skill '{name}' has no description; generated one from its first paragraph. Please review it.")
    if len(desc) > MAX_DESCRIPTION:
        warn(f"description of '{name}' is {len(desc)} characters; shortened to {MAX_DESCRIPTION}")
        desc = clip(desc, MAX_DESCRIPTION)

    if raw_fm is not None and name_in == name and desc_in is not None and str(desc_in) == desc:
        return name, sk.text  # already valid: keep the author's bytes

    rest = drop_keys(raw_fm or "", {"name", "description"})
    # json.dumps gives a valid YAML double-quoted string, escaping colons and quotes.
    header = f"name: {name}\ndescription: {json.dumps(desc, ensure_ascii=False)}"
    if rest:
        header += "\n" + rest
    return name, f"---\n{header}\n---\n{body if raw_fm is not None else sk.text}"


def rule_as_skill(rule: Rule, name: str) -> str:
    """Render a non-always rule as a skill. The description tells the agent when to use it."""
    if rule.mode == "glob":
        desc = f"{rule.description or 'Guidance for ' + name}. Use when working with files matching {', '.join(rule.globs)}."
    elif rule.mode == "auto":
        desc = rule.description
    else:  # manual
        desc = f"{rule.description or 'Reference rule ' + name}. Use only when explicitly requested."
    desc = clip(" ".join(desc.split()), MAX_DESCRIPTION)
    return f"---\nname: {name}\ndescription: {json.dumps(desc, ensure_ascii=False)}\n---\n\n{rule.body}\n"


def safe_parts(rel: str) -> tuple[str, ...] | None:
    """Path components of `rel`, or None if it could escape the skill folder."""
    parts = PurePosixPath(rel).parts
    if not parts or rel.startswith("/") or ".." in parts or "\\" in rel:
        return None
    return parts


class Target:
    """Base class for an output format (one coding agent)."""

    # Skill-relative files that only make sense for another tool and are not copied.
    # Maps relative path -> reason shown to the user.
    foreign_files: ClassVar[dict[str, str]] = {}

    # Does this agent follow a symlinked skill folder? If so, a skill that already
    # lives in another agent's folder is linked instead of duplicated. Codex
    # documents symlink support; the Kiro IDE silently ignores symlinked skills
    # (kirodotdev/Kiro#6401), so Kiro needs real files. Unknown means False.
    follows_symlinks: ClassVar[bool] = False

    # Where this agent keeps project skills, relative to the project root. --sync
    # reads these folders of the OTHER agents.
    project_skills_rel: ClassVar[str] = ""

    def __init__(
        self, dest: Path, use_global: bool, symlinks: str = "auto", sync: bool = False, owned: set[Path] | None = None
    ) -> None:
        self.dest = dest
        self.use_global = use_global
        self.symlinks = symlinks  # auto | always | never (--symlinks)
        self.sync = sync  # --sync: never touch skills that already exist...
        # ...unless this script created them (they are listed in .git/info/exclude).
        # Such generated copies are refreshed when the original changes.
        self.owned: set[Path] = owned or set()
        self._used: dict[str, set[str]] = {"skill": set(), "rule": set()}

    def is_owned(self, path: Path) -> bool:
        """Was `path`, or a folder above it, generated by this script?"""
        return any(p in self.owned for p in [path, *path.parents])

    @property
    def use_symlinks(self) -> bool:
        return self.symlinks == "always" or (self.symlinks == "auto" and self.follows_symlinks)

    def unique(self, kind: str, name: str) -> str:
        """Make `name` unique within this run by appending -2, -3, ..."""
        candidate, n = name, 2
        while candidate in self._used[kind]:
            suffix = f"-{n}"
            candidate = name[: MAX_NAME - len(suffix)] + suffix
            n += 1
        if candidate != name:
            warn(f"{kind} name '{name}' already used in this run; using '{candidate}'")
        self._used[kind].add(candidate)
        return candidate

    def skills_dir(self) -> Path:
        raise NotImplementedError

    def skill(self, sk: Skill) -> list[Action]:
        name, text = normalize_skill(sk)
        name = self.unique("skill", name)
        base = self.skills_dir() / name

        # --sync: a skill with this name is already installed for this agent. Leave
        # it alone, unless this script generated it (then it is refreshed below).
        owned = self.is_owned(base)
        if self.sync and (base.exists() or base.is_symlink()) and not owned:
            return [Present(base, text.encode("utf-8"))]

        copy = self._skill_actions(name, text, sk.files, sk.executable)
        # Link instead of copy when this agent follows symlinks, the source is a
        # folder on disk, and SKILL.md needs no changes (so the link shows exactly
        # the source files).
        if self.use_symlinks and sk.source_dir is not None and text == sk.text:
            actions: list[Action] = [Link(base, sk.source_dir, copy, owned=owned)]
        else:
            actions = copy
        # Agent-specific files go into the skill folder next to the shared ones.
        # When `base` is a link they land in the shared folder, where other agents ignore them.
        actions += self.extra_skill_files(sk, name, text, base)
        return actions

    def extra_skill_files(self, sk: Skill, name: str, text: str, base: Path) -> list[Action]:
        """Files only this agent needs, created if missing. None by default."""
        return []

    def _skill_actions(
        self, name: str, text: str, files: dict[str, Callable[[], bytes]], executable: set[str]
    ) -> list[Action]:
        """Copy a skill: SKILL.md plus every bundled file this agent can use."""
        base = self.skills_dir() / name
        # SKILL.md is the file that must not be clobbered silently, unless this
        # script generated it in an earlier run.
        actions: list[Action] = [
            Write(base / "SKILL.md", text.encode("utf-8"), exclusive=not self.is_owned(base), group=base)
        ]
        for rel in sorted(files):
            if rel in self.foreign_files:
                actions.append(Skip(f"{base / rel}: {self.foreign_files[rel]}"))
                continue
            parts = safe_parts(rel)
            if parts is None:
                warn(f"skipping unsafe path in skill: {rel}")
                continue
            data = files[rel]()
            # Keep scripts runnable: honour the source exec bit, or a #! line.
            is_exec = rel in executable or data.startswith(b"#!")
            actions.append(Write(base.joinpath(*parts), data, executable=is_exec, group=base))
        return actions

    def rule(self, rule: Rule) -> list[Action]:
        raise NotImplementedError


def codex_openai_yaml(name: str, skill_md: str) -> str:
    """Build agents/openai.yaml (Codex UI metadata) from a skill's name and description."""
    fm, _raw, _body = split_frontmatter(skill_md)
    desc = " ".join(str((fm or {}).get("description") or "").split())
    first_sentence = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0].rstrip(".")
    short = clip(first_sentence, 64) if first_sentence else name
    title = " ".join(w.capitalize() for w in name.split("-"))
    q = functools.partial(json.dumps, ensure_ascii=False)
    return (
        "# Generated by convert-skill.py. Codex reads this file; other agents ignore it. Edit freely.\n"
        "interface:\n"
        f"  display_name: {q(title)}\n"
        f"  short_description: {q(short)}\n"
        f"  default_prompt: {q('Use $' + name + ' for this task.')}\n"
    )


class MemoryFileTarget(Target):
    """Agents that read one always-on Markdown memory file (AGENTS.md, CLAUDE.md).

    Always-on rules become a named block in that file. Rules that apply only
    sometimes become skills, because these agents have no per-file rule mode.
    """

    def memory_file(self) -> Path:
        raise NotImplementedError

    def rule(self, rule: Rule) -> list[Action]:
        if rule.mode == "always":
            return [Block(self.memory_file(), self.unique("rule", rule.name), rule.body, rule.origin)]
        name = self.unique("skill", rule.name)
        return self._skill_actions(name, rule_as_skill(rule, name), {}, set())


class CodexTarget(MemoryFileTarget):
    """Codex: skills in .agents/skills, always-on rules in AGENTS.md."""

    follows_symlinks = True  # documented by OpenAI
    project_skills_rel = ".agents/skills"

    def skills_dir(self) -> Path:
        return (Path.home() if self.use_global else self.dest) / ".agents" / "skills"

    def memory_file(self) -> Path:
        if self.use_global:
            return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "AGENTS.md"
        return self.dest / "AGENTS.md"

    def extra_skill_files(self, sk: Skill, name: str, text: str, base: Path) -> list[Action]:
        # Codex-only UI metadata. Skip it when the skill already has one, and when
        # the skill is already in this agent's own folder (converting it must not
        # change a Codex project).
        if "agents/openai.yaml" in sk.files:
            return []
        if sk.source_dir is not None and sk.source_dir.resolve() == base.resolve():
            return []
        return [
            Write(
                base / "agents" / "openai.yaml",
                codex_openai_yaml(name, text).encode("utf-8"),
                create_only=True,
                group=base,
            )
        ]


class ClaudeTarget(MemoryFileTarget):
    """Claude Code: skills in .claude/skills, always-on rules in CLAUDE.md."""

    project_skills_rel = ".claude/skills"

    # Codex-only UI metadata; Claude Code ignores it, so do not duplicate it.
    foreign_files: ClassVar[dict[str, str]] = {
        "agents/openai.yaml": "Codex-only metadata, Claude Code has no equivalent (not copied)",
    }

    def root(self) -> Path:
        if self.use_global:
            return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return self.dest / ".claude"

    def skills_dir(self) -> Path:
        return self.root() / "skills"

    def memory_file(self) -> Path:
        # Project memory is ./CLAUDE.md; user memory is ~/.claude/CLAUDE.md.
        return self.root() / "CLAUDE.md" if self.use_global else self.dest / "CLAUDE.md"


class KiroTarget(Target):
    """Kiro: skills in .kiro/skills, rules as steering files with an inclusion mode."""

    project_skills_rel = ".kiro/skills"

    # Codex-only UI metadata (display name, default prompt, invocation policy).
    foreign_files: ClassVar[dict[str, str]] = {
        "agents/openai.yaml": "Codex-only metadata, Kiro has no equivalent (not copied)",
    }

    def root(self) -> Path:
        return (Path.home() if self.use_global else self.dest) / ".kiro"

    def skills_dir(self) -> Path:
        return self.root() / "skills"

    def rule(self, rule: Rule) -> list[Action]:
        name = self.unique("rule", rule.name)
        j = functools.partial(json.dumps, ensure_ascii=False)  # YAML-safe quoting
        if rule.mode == "always":
            fm = ["inclusion: always"]
        elif rule.mode == "glob":
            pattern = j(rule.globs[0]) if len(rule.globs) == 1 else j(rule.globs)
            fm = ["inclusion: fileMatch", f"fileMatchPattern: {pattern}"]
        elif rule.mode == "auto":
            fm = ["inclusion: auto", f"name: {j(name)}", f"description: {j(rule.description)}"]
        else:
            fm = ["inclusion: manual"]
        text = "---\n" + "\n".join(fm) + "\n---\n\n" + rule.body + "\n"
        return [Write(self.root() / "steering" / f"{name}.md", text.encode("utf-8"), exclusive=True)]


TARGETS = {"codex": CodexTarget, "claude": ClaudeTarget, "kiro": KiroTarget}


def render_block(block: Block, existing: str) -> str:
    """Return `existing` with the named block inserted or replaced (idempotent)."""
    begin = f"<!-- BEGIN converted-rule: {block.name} (from {block.origin}) -->"
    end = f"<!-- END converted-rule: {block.name} -->"
    new = f"{begin}\n{block.body.strip()}\n{end}"
    pattern = re.compile(
        r"<!-- BEGIN converted-rule: " + re.escape(block.name) + r"(?: [^\n]*)? -->.*?"
        r"<!-- END converted-rule: " + re.escape(block.name) + r" -->",
        re.S,
    )
    if pattern.search(existing):
        # lambda: the replacement text must not be scanned for backslash escapes
        return pattern.sub(lambda _m: new, existing, count=1)
    if not existing.strip():
        return new + "\n"
    return existing.rstrip("\n") + "\n\n" + new + "\n"


def read_if_exists(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- #
# Keeping generated skills out of Git
#
# Only the ORIGINAL skill should be committed. The links and copies this script
# creates for other agents are listed in a managed block of `.git/info/exclude`,
# the per-clone ignore file that is never committed. That block has two jobs:
#   1. Git ignores the generated paths, so `git status` stays clean.
#   2. It records which paths the script owns. A later run may refresh an owned
#      copy when the original changes; anything not listed is never overwritten
#      without --force.
# `.gitignore` is not used: it is shared with the team, and the list changes with
# every skill. Folders such as .kiro/ also hold files that must be committed
# (specs, steering, hooks), so they cannot be ignored as a whole.
# --------------------------------------------------------------------------- #


class GitExclude:
    BEGIN = "# BEGIN convert-skill.py generated (managed block: skills created by the script)"
    END = "# END convert-skill.py generated"

    def __init__(self, dest: Path) -> None:
        self.top: Path | None = None  # root of the working tree
        self.file: Path | None = None  # .git/info/exclude
        # `git -C` needs a folder that exists; --dest may not exist yet.
        start = dest
        while not start.exists() and start != start.parent:
            start = start.parent
        try:
            top = subprocess.run(
                ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            exclude = subprocess.run(
                ["git", "-C", str(start), "rev-parse", "--git-path", "info/exclude"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return  # git is not installed
        if top.returncode != 0 or exclude.returncode != 0:
            return  # not inside a Git repository
        self.top = Path(os.path.realpath(top.stdout.strip()))
        path = Path(exclude.stdout.strip())  # relative to `start` unless absolute
        self.file = Path(os.path.realpath(path if path.is_absolute() else start / path))

    @property
    def enabled(self) -> bool:
        return self.top is not None and self.file is not None

    @staticmethod
    def _escape(rel: str) -> str:
        """Escape characters that are special in ignore patterns."""
        rel = re.sub(r"([\\*?\[\]])", r"\\\1", rel)
        rel = re.sub(r"^([#!])", r"\\\1", rel)
        return re.sub(r" $", r"\\ ", rel)

    def _block(self) -> tuple[str, list[str]]:
        """Return the whole file text and the pattern lines inside our block."""
        assert self.file is not None
        text = self.file.read_text(encoding="utf-8") if self.file.exists() else ""
        m = re.search(re.escape(self.BEGIN) + r"\n(.*?)" + re.escape(self.END), text, re.S)
        lines = [line for line in (m.group(1).splitlines() if m else []) if line.strip() and not line.startswith("#")]
        return text, lines

    def owned(self) -> set[Path]:
        """Absolute paths of everything this script generated earlier (and that still exists)."""
        if not self.enabled:
            return set()
        assert self.top is not None
        out: set[Path] = set()
        for line in self._block()[1]:
            rel = re.sub(r"\\(.)", r"\1", line).strip("/")
            if rel and os.path.lexists(self.top / rel):
                out.add(self.top / rel)
        return out

    def add(self, paths: list[Path]) -> int:
        """Add paths to the managed block. Returns the number of entries now listed."""
        if not self.enabled:
            return 0
        assert self.top is not None and self.file is not None
        text, current = self._block()
        entries = {
            line: None for line in current if os.path.lexists(self.top / re.sub(r"\\(.)", r"\1", line).strip("/"))
        }
        for p in paths:
            # Resolve the folder but not the last component, which may be a symlink.
            real = Path(os.path.realpath(p.parent)) / p.name
            try:
                rel = real.relative_to(self.top).as_posix()
            except ValueError:
                continue  # outside this repository
            # A trailing slash would not match a symlink, which Git treats as a file.
            slash = "/" if real.is_dir() and not real.is_symlink() else ""
            entries["/" + self._escape(rel) + slash] = None
        # A listed folder already covers everything inside it, so drop redundant entries.
        folders = {e.rstrip("/") for e in entries if e.endswith("/")}
        entries = {e: None for e in entries if not any(e.rstrip("/").startswith(f + "/") for f in folders)}
        block = "\n".join([self.BEGIN, *sorted(entries), self.END])
        if self.BEGIN in text:
            new = re.sub(
                re.escape(self.BEGIN) + r".*?" + re.escape(self.END), lambda _m: block, text, count=1, flags=re.S
            )
        else:
            new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
        new = new.rstrip("\n") + "\n"
        if new != text:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(new, encoding="utf-8")
        return len(entries)


def write_file(a: Write) -> None:
    a.path.parent.mkdir(parents=True, exist_ok=True)
    a.path.write_bytes(a.data)
    a.path.chmod(0o755 if a.executable else 0o644)


def apply(actions: list[Action], force: bool, dry_run: bool, protected: set[Path]) -> list[Path]:
    """Plan every action, refuse conflicts, then write.

    `protected` holds the resolved paths of the local source files. Those are
    NEVER written: if an output lands on a source file (for example converting
    `.agents` to Codex format into the same project), the file is left alone.

    Statuses: create, overwrite, update, link, relink (written); unchanged, kept,
    present, differs, skip (nothing written); source (refused); conflict (needs --force).

    Returns the paths of the skill links and folders this run generated (not
    source files), so the caller can list them in .git/info/exclude.
    """
    # One plan entry per file or link: (status, label, action, new_text_for_blocks)
    # (A local annotation is never evaluated, so `str | None` is fine on Python 3.9.)
    plan: list[tuple[str, str, Action, str | None]] = []
    pending_blocks: dict[Path, str] = {}  # AGENTS.md text after earlier blocks of this run

    def plan_action(a: Action) -> None:
        # ---- Informational line (a file that is deliberately not converted).
        if isinstance(a, Skip):
            plan.append(("skip", a.description, a, None))

        # ---- A skill folder that --sync found already installed: never touch it.
        elif isinstance(a, Present):
            current = read_if_exists(a.path / "SKILL.md")
            status = "unchanged" if current == a.skill_md else "differs"
            plan.append((status, str(a.path), a, None))

        # ---- A symlink to the shared skill folder, or a copy when that is not possible.
        elif isinstance(a, Link):
            same_folder = a.path.exists() and a.path.resolve() == a.source.resolve()
            label = f"{a.path} -> {os.path.relpath(a.source, a.path.parent)}"
            if same_folder:
                # The skill is already here (it is the source, or a link to it).
                plan.append(("unchanged", str(a.path), a, None))
            elif a.path.is_symlink():
                # A different (or dangling) link. Replacing a link deletes no data.
                plan.append(("relink" if force or a.owned else "conflict", label, a, None))
            elif a.path.exists():
                # A real folder is already there: compare file by file instead.
                for sub in a.fallback:
                    plan_action(sub)
            else:
                plan.append(("link", label, a, None))

        # ---- One file.
        elif isinstance(a, Write):
            current = read_if_exists(a.path)
            if current == a.data:
                status = "unchanged"  # identical content: nothing to do
            elif a.create_only and current is not None:
                status = "kept"  # a file that should only ever be created
            elif a.path.resolve() in protected:
                status = "source"  # would modify a source file: refuse
            elif current is None:
                status = "create"
            elif a.exclusive and not force:
                status = "conflict"
            else:
                status = "overwrite"
            plan.append((status, str(a.path), a, None))

        # ---- A named block inside a shared file such as AGENTS.md.
        else:
            raw = pending_blocks.get(a.path)
            if raw is None:
                existing = read_if_exists(a.path)
                raw = existing.decode("utf-8") if existing is not None else ""
            new_text = render_block(a, raw)
            pending_blocks[a.path] = new_text
            label = f"{a.path} (block '{a.name}')"
            if a.path.resolve() in protected:
                status = "source"
            elif new_text == raw:
                status = "unchanged"
            else:
                status = "update"
            plan.append((status, label, a, new_text))

    for action in actions:
        plan_action(action)

    # Skill folders that do not exist yet: if we create them, the whole folder is
    # generated. Folders that already exist are only partly ours (per-file entries).
    groups = {a.group for _s, _l, a, _t in plan if isinstance(a, Write) and a.group is not None}
    new_groups = {g for g in groups if not g.exists() and not g.is_symlink()}

    # ---- Report.
    writes = ("create", "overwrite", "update", "link", "relink")
    width = max((len(s) + (6 if dry_run and s in writes else 0) for s, *_ in plan), default=0)
    notes = {
        "source": "  (is the source itself, left untouched)",
        "conflict": "  (exists and differs, use --force)",
        "kept": "  (already exists, kept)",
        "differs": "  (exists and differs from the source, left as is)",
    }
    for status, label, _a, _t in plan:
        shown = f"would {status}" if dry_run and status in writes else status
        print(f"{shown:<{width}}  {label}{notes.get(status, '')}")

    if any(status == "conflict" for status, *_ in plan) and not dry_run:
        die("nothing was written because some files already exist and differ; rerun with --force to overwrite them")

    # ---- Execute. Links come first so that agent-specific files planned inside a
    # linked folder (agents/openai.yaml) are written into the shared folder.
    if dry_run:
        return []
    for status, _label, a, _t in plan:
        if status in ("link", "relink") and isinstance(a, Link):
            a.path.parent.mkdir(parents=True, exist_ok=True)
            if a.path.is_symlink():
                a.path.unlink()
            try:
                # Relative target so the project can be moved or cloned elsewhere.
                os.symlink(os.path.relpath(a.source, a.path.parent), a.path, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                warn(f"cannot create a symlink at {a.path} ({exc}); copying the files instead")
                for sub in a.fallback:
                    if isinstance(sub, Write):
                        write_file(sub)
    for status, _label, a, _t in plan:
        if status in ("create", "overwrite") and isinstance(a, Write):
            write_file(a)
    # Shared files are written once, with all of this run's blocks applied.
    for path, text in pending_blocks.items():
        if path.resolve() in protected:
            continue
        if read_if_exists(path) != text.encode("utf-8"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    # What did we generate? A new skill folder counts as one entry. A link counts
    # as one entry, and files written through it (agents/openai.yaml) are listed
    # individually because they live in the shared folder.
    linked = {a.path for status, _l, a, _t in plan if status in ("link", "relink")}
    generated: list[Path] = list(linked)
    for status, _label, a, _t in plan:
        if status in ("create", "overwrite") and isinstance(a, Write) and a.group is not None:
            if a.group in new_groups and a.group not in linked:
                generated.append(a.group)
            else:
                generated.append(a.path)
    return generated


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="convert-skill.py",
        description="Convert skills and rule files from any common agent format to Codex, Claude Code or "
        "Kiro. The source is never modified.",
    )
    p.add_argument(
        "source",
        nargs="*",
        help="local path or URL (GitHub repo/tree/blob/raw URL, or any file URL); not needed with --sync",
    )
    p.add_argument(
        "-t", "--target", choices=sorted(TARGETS), default="codex", help="the agent to convert FOR (default: codex)"
    )
    p.add_argument(
        "--sync",
        nargs="?",
        const=".",
        metavar="PROJECT",
        help="make the skills of the other agents in PROJECT (default: .) available to --target. "
        "Finds .agents/skills, .claude/skills and .kiro/skills, converts the ones that are "
        "missing, and leaves existing ones alone",
    )
    p.add_argument(
        "-g",
        "--global",
        dest="use_global",
        action="store_true",
        help="install into the user-level folders instead of a project",
    )
    p.add_argument(
        "-d",
        "--dest",
        default=None,
        help="project root for project-level installs (default: the folder that contains the "
        "source's .agents/.claude/.kiro/... folder, else the current directory)",
    )
    p.add_argument("-n", "--name", help="name for the converted item (only when the source yields exactly one)")
    p.add_argument(
        "--as",
        dest="as_",
        choices=("auto", "skill", "rule"),
        default="auto",
        help="force how the source is interpreted (default: auto)",
    )
    p.add_argument(
        "--symlinks",
        choices=("auto", "always", "never"),
        default="auto",
        help="link to a local skill folder instead of copying it. auto = only for agents that "
        "follow symlinks (Codex). Kiro ignores symlinked skills, so it always gets real files",
    )
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite existing converted files that differ (source files are never overwritten)",
    )
    p.add_argument("--dry-run", action="store_true", help="show what would be written without writing")
    p.add_argument(
        "--no-exclude",
        action="store_true",
        help="do not list generated skills in .git/info/exclude (this also turns off refreshing generated copies)",
    )
    return p


def prefetch(loaders: list[Callable[[], bytes]]) -> None:
    """Download remote files in parallel; GitHub loaders cache their result."""
    if len(loaders) < 2:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in [pool.submit(load) for load in loaders]:
            future.result()  # re-raises SystemExit from die() inside a worker


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.source and args.sync is None:
        parser.error("give at least one SOURCE, or use --sync")
    if args.sync is not None and (args.source or args.use_global):
        parser.error("--sync works on one project and cannot be combined with SOURCE or --global")

    # 0. --sync: the sources are the other agents' skill folders of the project.
    sources: list[str] = list(args.source)
    if args.sync is not None:
        project = Path(args.sync).expanduser().resolve()
        if not project.is_dir():
            die(f"not a folder: {args.sync}")
        for name, cls in TARGETS.items():
            folder = project / cls.project_skills_rel
            if name != args.target and folder.is_dir():
                sources.append(str(folder))
        if not sources:
            print(f"No skills for other agents found in {project}")
            return 0
        if args.dest is None:
            args.dest = str(project)

    # 1. Decide where to write.
    dest = Path(args.dest).expanduser().resolve() if args.dest else infer_dest(sources) or Path.cwd()

    # Generated skills are listed in .git/info/exclude. That list also tells us which
    # existing skills we created ourselves and may refresh. Project installs only.
    exclude = GitExclude(dest)
    use_exclude = exclude.enabled and not args.use_global and not args.no_exclude
    owned = exclude.owned() if use_exclude else set()

    # 2. Read: load every source and find the skills and rules in it.
    skills: list[Skill] = []
    rules: list[Rule] = []
    protected: set[Path] = set()
    for src in sources:
        tree = load_source(src)
        s, r = discover(tree, args.as_)
        if not s and not r:
            if args.sync is not None:
                continue  # e.g. a folder that only holds symlinks
            die(f"nothing to convert in {src}: no SKILL.md and no recognised rule files")
        skills += s
        rules += r
        protected |= tree.local_paths

    if args.sync is not None:
        # The same skill may exist for two other agents; the first one found wins.
        # Copies this script generated are not sources: they may be older than the
        # original, which would make the original look like it `differs`.
        seen: set[str] = set()
        unique_skills: list[Skill] = []
        for sk in skills:
            if sk.source_dir is not None and any(p in owned for p in [sk.source_dir, *sk.source_dir.parents]):
                continue
            fm = split_frontmatter(sk.text)[0] or {}
            key = slugify(str(fm.get("name") or sk.default_name))
            if key not in seen:
                seen.add(key)
                unique_skills.append(sk)
        skills = unique_skills
        if not skills:
            print(f"No skills of other agents to convert in {Path(args.sync).resolve()}")
            return 0

    if args.name:
        if len(skills) + len(rules) != 1:
            die(f"--name needs exactly one item to convert, found {len(skills) + len(rules)}")
        if skills:
            skills[0].forced_name = args.name
        else:
            rules[0].name = slugify(args.name)

    prefetch([load for sk in skills for load in sk.files.values()])

    # 3. Convert: the target turns skills and rules into write actions.
    target = TARGETS[args.target](dest, args.use_global, args.symlinks, sync=args.sync is not None, owned=owned)
    actions: list[Action] = []
    for sk in skills:
        actions += target.skill(sk)
    for rule in rules:
        actions += target.rule(rule)

    where = "user-level folders" if args.use_global else str(dest)
    print(f"Converting {len(skills)} skill(s) and {len(rules)} rule(s) for {args.target} -> {where}")

    # 4. Write (or only report, with --dry-run).
    generated = apply(actions, args.force, args.dry_run, protected)

    # 5. Keep what we generated out of Git (per clone, never committed).
    if use_exclude and generated:
        count = exclude.add(generated)
        print(f"exclude  {exclude.file} ({count} generated path(s) listed)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(1)
