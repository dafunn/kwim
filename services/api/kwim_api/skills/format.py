"""The SKILL.md format and the checks every write runs.

    ---
    name: check-host-cpu
    description: Check a host's CPU usage and which processes are using it.
    agents: ops
    tags: host, cpu
    refs: org/repo:path/to/playbook.yml, org/repo:path/to/other.yml
    ---
    <instructions>

Frontmatter is flat `key: value` lines; `agents`, `tags` and `refs` are
comma-separated lists. `name` and `description` are required, and so is a body.
"""
import re
from dataclasses import dataclass, field

from ..config import settings

FENCE = "---"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
_KNOWN_KEYS = frozenset({"name", "description", "agents", "tags", "refs"})
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*:[^\s:]+$")

# Credential-shaped values, not credential words: a skill may name the password
# file, but not carry a key, a token or a password itself.
_CREDENTIAL_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("encrypted secrets file", re.compile(r"(?m)^\$[A-Z_]+;\d+\.\d+;AES256")),
    ("assigned secret", re.compile(
        r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?"
        r"(?![<{$])[^\s'\"<>{}]{6,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("credentials in a URL", re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@")),
    ("long encoded value", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])")),
    ("long hex value", re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])")),
)


class SkillFormatError(ValueError):
    """A SKILL.md that cannot be stored."""


@dataclass
class ParsedSkill:
    name: str
    description: str
    body: str
    agents: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        raise SkillFormatError(f"must open with a {FENCE} frontmatter fence")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == FENCE)
    except StopIteration:
        raise SkillFormatError(f"unterminated frontmatter: no closing {FENCE}") from None
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not key:
            raise SkillFormatError(f"frontmatter line is not `key: value`: {line.strip()!r}")
        if key not in _KNOWN_KEYS:
            raise SkillFormatError(f"unknown frontmatter key {key!r}")
        if key in meta:
            raise SkillFormatError(f"frontmatter key {key!r} appears twice")
        meta[key] = value.strip().strip("\"'")
    return meta, "\n".join(lines[end + 1:]).strip()


def credential_findings(text: str) -> list[str]:
    """The kinds of credential found in `text`, without the values."""
    return [kind for kind, pattern in _CREDENTIAL_PATTERNS if pattern.search(text)]


def parse_skill(text: str) -> ParsedSkill:
    """Parse and check a SKILL.md. Raises SkillFormatError with every problem."""
    if len(text) > settings.skills_max_chars:
        raise SkillFormatError(
            f"is {len(text)} characters; the limit is {settings.skills_max_chars}")
    meta, body = split_frontmatter(text)
    problems: list[str] = []
    name = meta.get("name", "")
    description = meta.get("description", "")
    if not name:
        problems.append("frontmatter has no name")
    elif len(name) > MAX_NAME or not NAME_RE.match(name):
        problems.append(f"name {name!r} must be lowercase letters, digits and hyphens, "
                        f"at most {MAX_NAME} characters")
    if not description:
        problems.append("frontmatter has no description")
    elif len(description) > MAX_DESCRIPTION:
        problems.append(f"description is over {MAX_DESCRIPTION} characters")
    if not body:
        problems.append("has no instructions after the frontmatter")
    agents = _list(meta.get("agents", ""))
    tags = _list(meta.get("tags", ""))
    refs = _list(meta.get("refs", ""))
    for label, values in (("agents", agents), ("tags", tags)):
        bad = [v for v in values if not _TAG_RE.match(v)]
        if bad:
            problems.append(f"{label} must be lowercase words: {', '.join(bad)}")
    bad_refs = [r for r in refs if not _REF_RE.match(r) or ".." in r.split(":", 1)[1]]
    if bad_refs:
        problems.append("refs must be `owner/repo:relative/path`: " + ", ".join(bad_refs))
    found = credential_findings(text)
    if found:
        problems.append("contains what looks like a credential (" + ", ".join(found)
                        + "); refer to where it is stored instead")
    if problems:
        raise SkillFormatError("; ".join(problems))
    return ParsedSkill(name=name, description=description, body=body,
                       agents=agents, tags=tags, refs=refs)
