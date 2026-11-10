#!/usr/bin/env python3
"""Scan a tree or a git history for things that must not be published.

    scan.py --tree PATH [--deny-file FILE]
    scan.py --history [--range A..B] [--deny-file FILE]

Binary files are findings too: nothing here should be compiled.
Exit status is 1 when anything is found, 0 when nothing is, 2 on a usage or
git error. A finding is printed as `<where>:<line>: <category>`; the matched
value is never printed, so the output is safe to keep in a public log. A hit on
the optional private deny list prints `deny-list entry N`, N being the line of
that file. Standard library only.
"""
import argparse
import os
import re
import subprocess
import sys

# Highest issue or pull request number that exists in this repository.
# A bare reference above it points somewhere else.
MAX_PUBLIC_ISSUE = 99
OWN_REPO = "hanzhad/agent-dev-slots"

GENERIC_HOME_NAMES = {"node", "user", "runner"}
ALLOWED_EMAILS = {"noreply@github.com", "noreply@anthropic.com"}
ALLOWED_EMAIL_DOMAINS = {
    "users.noreply.github.com",
    "example.com",
    "example.org",
    "example.net",
}

NAME = r"[A-Za-z0-9_.-]+"

# Each pattern is written so that its own source text does not match it.
TOKEN_RES = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
]
UNIX_HOME_RE = re.compile(r"/(?:" + "ho" + "me|" + "Us" + "ers)/(" + NAME + r")/")
WIN_HOME_RE = re.compile(r"[A-Za-z]:[\\/]+" + "Us" + r"ers[\\/]+(" + NAME + ")")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
REPO_REF_RE = re.compile(r"(?<![\w.-])(" + NAME + ")/(" + NAME + r")#\d+\b")
URL_REF_RE = re.compile(
    r"github\.com/(" + NAME + ")/(" + NAME + r")/(?:issues|pull)/\d+"
)
BARE_REF_RE = re.compile(r"(?<![\w/&#.-])#(\d+)(?![\w-])")
ADR_RE = re.compile(r"\bADR-\d+")
DOCS_RE = re.compile(r"\bdocs/00\d+")

REF = "issue reference"


SEPARATORS_RE = re.compile(r"[-_.\s]+")


def squash(text):
    """Lower-case and drop word separators, so `a-b`, `a_b`, `a.b`, `a b` and
    `ab` compare equal."""
    return SEPARATORS_RE.sub("", text.lower())


def load_deny(path):
    terms = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for number, raw in enumerate(fh, 1):
            term = squash(raw)
            if term:
                terms.append((number, term))
    return terms


def email_allowed(addr):
    addr = addr.lower()
    return addr in ALLOWED_EMAILS or addr.split("@", 1)[1] in ALLOWED_EMAIL_DOMAINS


def categories(line, deny):
    """Return the categories found in one line, without the matched text."""
    found = []
    if any(rx.search(line) for rx in TOKEN_RES):
        found.append("token")
    homes = list(UNIX_HOME_RE.finditer(line)) + list(WIN_HOME_RE.finditer(line))
    if any(m.group(1) not in GENERIC_HOME_NAMES for m in homes):
        found.append("home path")
    if any(not email_allowed(m.group(0)) for m in EMAIL_RE.finditer(line)):
        found.append("email")
    refs = list(REPO_REF_RE.finditer(line)) + list(URL_REF_RE.finditer(line))
    bad_ref = any(
        (m.group(1) + "/" + m.group(2)).lower() != OWN_REPO for m in refs
    )
    for m in BARE_REF_RE.finditer(line):
        digits = m.group(1)
        if len(digits) in (6, 8):  # looks like a colour, not an issue
            continue
        if int(digits) > MAX_PUBLIC_ISSUE:
            bad_ref = True
    if bad_ref or ADR_RE.search(line) or DOCS_RE.search(line):
        found.append(REF)
    squashed = squash(line)
    for number, term in deny:
        if term in squashed:
            found.append("deny-list entry %d" % number)
    return found


TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z-]*:\s")


def drop_allowed_emails(line):
    """Blank out allow-listed addresses so the deny list does not see them."""
    return EMAIL_RE.sub(
        lambda m: "" if email_allowed(m.group(0)) else m.group(0), line
    )


def scan_text(where, text, deny, start=1, exempt=None):
    """Scan lines; `exempt(line)` marks lines whose allow-listed emails are
    commit metadata and are not matched against the deny list."""
    findings = []
    for offset, line in enumerate(text.splitlines()):
        if exempt and exempt(line):
            line = drop_allowed_emails(line)
        for cat in categories(line, deny):
            findings.append("%s:%d: %s" % (where, start + offset, cat))
    return findings


def scan_name(where, path, deny):
    """Check a file path for home paths, emails and deny terms. The path is
    never printed, so `where` is a placeholder."""
    return [
        "%s:0: %s" % (where, cat)
        for cat in categories("/" + path, deny)
        if cat in ("home path", "email") or cat.startswith("deny-list")
    ]


def scan_tree(root, deny):
    findings = []
    if os.path.isfile(root):
        files = [(root, root)]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in (".git", "__pycache__")
            )
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                files.append((full, os.path.relpath(full, root).replace(os.sep, "/")))
    named = not os.path.isfile(root)
    for index, (full, label) in enumerate(files, 1):
        if named:
            findings += scan_name("[file name %d]" % index, label, deny)
        if os.path.islink(full):
            continue
        with open(full, "rb") as fh:
            data = fh.read()
        if b"\0" in data[:8000]:
            findings.append("%s:0: binary file" % label)
            continue
        text = data.decode("utf-8", errors="replace")
        findings += scan_text(label, text, deny)
    return findings


def git(*args):
    proc = subprocess.run(["git", *args], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("git %s failed" % args[0])
    return proc.stdout.decode("utf-8", errors="replace")


def added_lines(sha):
    """Yield (new-file line number, text) for every added line of a commit."""
    out = git("show", "--format=", "--root", "-m", "-U0", "--no-color",
              "--no-ext-diff", sha)
    in_hunk = False
    number = 0
    for line in out.splitlines():
        if line.startswith("diff --git"):
            in_hunk = False
        elif line.startswith("@@"):
            match = re.match(r"@@ -\S+ \+(\d+)", line)
            number = int(match.group(1)) if match else 0
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            yield number, line[1:]
            number += 1


def added_paths(sha):
    out = git("show", "--format=", "--root", "-m", "--name-only",
              "--diff-filter=AR", "-z", "--no-color", sha)
    return [name for name in out.split("\0") if name]


def added_binaries(sha):
    out = git("-c", "core.quotepath=off", "show", "--format=", "--root", "-m",
              "--numstat", "--diff-filter=AR", "--no-color", sha)
    # git marks a binary file with dashes in place of the line counts
    return [line for line in out.splitlines() if line.startswith("-\t-\t")]


def scan_history(rev_args, deny):
    findings = []
    log = git("log", "--format=%H%x1f%ae%x1f%ce%x1f%B%x1e", *rev_args)
    for record in log.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, author, committer, message = record.split("\x1f", 3)
        short = sha[:12]
        always = lambda line: True
        findings += scan_text(short + "[author]", author, deny, exempt=always)
        findings += scan_text(short + "[committer]", committer, deny, exempt=always)
        findings += scan_text(short + "[message]", message, deny,
                              exempt=TRAILER_RE.match)
        for name in added_paths(sha):
            findings += scan_name(short + "[path]", name, deny)
        for _ in added_binaries(sha):
            findings.append("%s[path]:0: binary file" % short)
        for number, text in added_lines(sha):
            findings += scan_text(short + "[diff]", text, deny, start=number)
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tree", metavar="PATH")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--range", metavar="A..B")
    parser.add_argument("--deny-file", metavar="PATH")
    args = parser.parse_args(argv)
    if bool(args.tree) == args.history:
        parser.error("give exactly one of --tree and --history")
    if args.range and not args.history:
        parser.error("--range goes with --history")

    deny = []
    deny_path = args.deny_file or os.environ.get("SCAN_DENY_FILE")
    if deny_path:
        try:
            deny = load_deny(deny_path)
        except OSError:
            print("cannot read the deny file", file=sys.stderr)
            return 2
    try:
        if args.tree:
            findings = scan_tree(args.tree, deny)
        else:
            findings = scan_history([args.range] if args.range else ["--all"], deny)
    except (OSError, RuntimeError) as err:
        print("scan failed: %s" % err, file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    print("findings: %d" % len(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
