"""
wm_zig — the whole ZIG config publisher in one file.

COPY THIS ONE FILE into any ZIG repository (as wm_zig/wm_zig.py) and run:

    python wm_zig/wm_zig.py --install

That writes the pre-push hook, points git at it and adds the .gitattributes
lines. Nothing else to copy, nothing to configure: the prefix comes from the
folder name, the repo/branch/commit from git, and the six version parameters
from config.py. Parameters config.py does not define are published as
"not checkable" automatically.

Commands
    --install            set the repo up (run once per clone)
    --dry-run [--print]  show what would be published, change nothing
    --release [TAG]      tag the current commit and publish it as APPROVED
    --status             what this repo would publish, and to where
    --eval <config.py>   print the six parameters (used by the ZIG Installer
                         over SSH; do not call by hand)
    --publish            what the hook calls

An optional wm_zig.json next to this folder overrides a default; see the
central repo's README. Most repositories never need one.

It NEVER blocks a push: every failure path exits 0.
"""

import argparse
import ast
import errno
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

try:
    from datetime import timezone

    def _utcnow():
        return datetime.now(timezone.utc)
except ImportError:                                        # pragma: no cover
    def _utcnow():
        return datetime.utcnow()




EVAL_VERSION = 1

# (print tag, json key, config.py variable, dict subkey or None)
_OUT_MAP = (
    ("TESTJIGAPP",  "test_jig_app",            "ZIG_APP_VER",       None),
    ("CUSTOMERAPP", "customer_app",            "capp_ver",          "ver_app"),
    ("PRODAPP",     "test_jig_production_app", "valid_prd_app_ver", None),
    ("SOUND",       "sound_file_version",      "res_ver",           "ver_res"),
    ("SDK",         "sdk_version",             "sdk_version",       None),
    ("BATCH",       "pcb_batch_id",            "BATCH_ID",          None),
)

# The 6 keys this evaluator can supply. customer_name is NOT here — it comes
# from the program prefix (device side) or wm_zig.json (publisher side).
CONFIG_KEYS = tuple(key for _, key, _, _ in _OUT_MAP)


def build_env(source_text):
    """Statically evaluate config.py's module body -> {name: value}.

    Resolves plain literals, f-strings, string concatenation, dicts/lists and
    subscripts, and follows if/elif branches whose test is a simple ==/!=
    comparison of already-known values (this is how PTM's `LANG` selects
    BATCH_ID and ver_res). Anything it cannot resolve is skipped, not guessed.
    """
    tree = ast.parse(source_text)
    env = {}

    def ev(n):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in env:
                return env[n.id]
            raise KeyError(n.id)
        if isinstance(n, ast.JoinedStr):
            s = ""
            for v in n.values:
                if isinstance(v, ast.Constant):
                    s += str(v.value)
                elif isinstance(v, ast.FormattedValue):
                    s += str(ev(v.value))
                else:
                    raise ValueError("jstr")
            return s
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            return ev(n.left) + ev(n.right)
        if isinstance(n, ast.Dict):
            return {ev(k): ev(v) for k, v in zip(n.keys, n.values)}
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.Subscript):
            return ev(n.value)[ev(n.slice)]
        raise ValueError(type(n).__name__)

    def cond(n):
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            l = ev(n.left)
            r = ev(n.comparators[0])
            op = n.ops[0]
            if isinstance(op, ast.Eq):
                return l == r
            if isinstance(op, ast.NotEq):
                return l != r
        raise ValueError("cond")

    def run(body):
        for st in body:
            if isinstance(st, ast.Assign):
                try:
                    val = ev(st.value)
                except Exception:
                    continue
                for t in st.targets:
                    if isinstance(t, ast.Name):
                        env[t.id] = val
            elif isinstance(st, ast.If):
                try:
                    c = cond(st.test)
                except Exception:
                    continue
                run(st.body if c else st.orelse)

    run(tree.body)
    return env


def extract_params(source_text):
    """-> {'test_jig_app': 'PTM_0_0_5'|'', ...}. Exactly CONFIG_KEYS, always.

    A value this evaluator could not resolve comes back as '' — the caller
    decides what to do about it. The publisher refuses to publish a blank;
    the device side reports it as unread.
    """
    env = build_env(source_text)
    out = {}
    for _, key, var, sub in _OUT_MAP:
        val = env.get(var)
        if sub is not None:
            try:
                val = val.get(sub, "")
            except Exception:
                val = ""
        out[key] = "" if val is None else str(val)
    return out


def read_source(path):
    """errors='replace' — a mojibake byte becomes a visible U+FFFD that the
    publisher's value-shape check then rejects, rather than silently deleting
    a character from a version string."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _eval_main(argv):
    try:
        src = read_source(argv[1])
        params = extract_params(src)
    except Exception as e:
        print("ERROR:" + str(e))
        return 0
    for tag, key, _, _ in _OUT_MAP:
        print(tag + ":" + params[key])
    return 0

PRE_PUSH_HOOK = r'''#!/bin/sh
# Walnut Medical - publish this ZIG's verification spec to wm_zig_config.
#
# CONTRACT: this hook NEVER blocks a push. It always exits 0.
# If anything at all goes wrong, the push proceeds and the central repo simply
# keeps its previous good state.
#
# Enable once per clone:   git config core.hooksPath .githooks
# Disable for one push:    WM_ZIG_PUBLISH=0 git push
# Log:                     ~/.wm_zig/publish.log

set +e
trap 'exit 0' EXIT INT TERM HUP

# git writes "<local ref> <local sha> <remote ref> <remote sha>" on stdin.
# Drain it even if we bail early, or git can see EPIPE.
REFS=$(cat)

[ "$WM_ZIG_PUBLISH" = "0" ] && exit 0

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -f "$ROOT/wm_zig/wm_zig.py" ] || exit 0

# Git exports these into the hook; inheriting them would make `git -C <cache>`
# operate on THIS repo's object store instead of the cache clone.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
      GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX GIT_COMMON_DIR \
      GIT_NAMESPACE GIT_QUARANTINE_PATH GIT_REFLOG_ACTION

# The `-c` smoke test defeats the Windows Store python.exe stub: a 0-byte App
# Execution Alias that satisfies `command -v` and then opens the Microsoft Store.
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "wm_zig: no working python on PATH - skipping publish" >&2
  exit 0
fi

printf '%s\n' "$REFS" | while read -r LREF LSHA RREF RSHA; do
  [ -z "$LREF" ] && continue
  # A branch deletion pushes the all-zero sha; nothing to publish.
  case "$LSHA" in *[!0]*) ;; *) continue ;; esac
  "$PY" "$ROOT/wm_zig/wm_zig.py" --publish --ref "$RREF" --sha "$LSHA" >&2 2>&1
done

exit 0
'''



import argparse
import errno
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

try:
    from datetime import timezone

    def _utcnow():
        return datetime.now(timezone.utc)
except ImportError:                                        # pragma: no cover
    def _utcnow():
        return datetime.utcnow()


TOOL_VERSION = "1.0.0"
SCHEMA_VERSION = 1
SCHEMA_URL = ("https://raw.githubusercontent.com/sp-wal-rd/wm_zig_config"
              "/main/schema/zig.schema.json")
DEFAULT_CONFIG_URL = "https://github.com/sp-wal-rd/wm_zig_config.git"

PARAM_KEYS = ("test_jig_app", "customer_app", "test_jig_production_app",
              "sound_file_version", "sdk_version", "pcb_batch_id", "customer_name")
CONFIG_KEYS = PARAM_KEYS[:6]          # customer_name is never from config.py

_SKIP_DIRS = {".git", "__pycache__", "arm", "Backup", "BackUp", "Ref", "Extra",
              ".vs", ".vscode", "node_modules", "venv", ".venv"}

# Only the programs whose customer name differs from the folder prefix need an
# entry; everything else defaults to the prefix itself.
CUSTOMER_NAMES = {
    "WMZ_APC": "APC",
}


# ─────────────────────────────────────────────────────────────── logging ──
class Log(object):
    """Everything goes to stderr: git swallows/reorders hook stdout in some
    clients, while stderr reliably surfaces in the VS Code Git pane."""

    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        self.lines.append(msg)
        sys.stderr.write("wm_zig: " + msg + "\n")

    def to_file(self, path, denylist=None):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            blob = "\n".join(self.lines)
            for secret in sorted(denylist or (), key=len, reverse=True):
                blob = blob.replace(secret, "<redacted>")
            stamp = _utcnow().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("[{0}] {1}\n".format(stamp, blob.replace("\n", " | ")[:2048]))
        except Exception:
            pass


log = Log()


class GitError(Exception):
    pass


# ──────────────────────────────────────────────────────────────── git ──
_STRIP_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_COMMON_DIR",
              "GIT_NAMESPACE", "GIT_QUARANTINE_PATH", "GIT_INDEX_VERSION",
              "GIT_REFLOG_ACTION", "GIT_EDITOR", "GIT_PAGER")


def _git_env():
    """Git exports GIT_DIR/GIT_INDEX_FILE/... into a hook. A `git -C <cache>`
    that inherits them operates on the ZIG repo's object store instead of the
    cache clone. Strip them all.

    GIT_TERMINAL_PROMPT/GIT_ASKPASS are the two highest-value lines here:
    without them a developer with an expired credential gets a modal dialog
    *inside a git hook* and the push appears to hang with no explanation.
    """
    env = dict((k, v) for k, v in os.environ.items() if k not in _STRIP_ENV)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def git(repo, *args, **kw):
    timeout = kw.get("timeout", 20)
    check = kw.get("check", True)
    cmd = ["git"]
    if repo:
        cmd += ["-C", str(repo)]
    cmd += [str(a) for a in args]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=_git_env(), timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise GitError("timed out: git " + " ".join(str(a) for a in args))
    except OSError as e:
        raise GitError("cannot run git: {0}".format(e))
    if check and p.returncode != 0:
        raise GitError((p.stderr or p.stdout or "").strip() or
                       "git exited {0}".format(p.returncode))
    return (p.stdout or "").strip(), (p.stderr or "").strip(), p.returncode


# ───────────────────────────────────────────────────────── discovery ──
def find_repo_root(start):
    try:
        out, _, _ = git(start, "rev-parse", "--show-toplevel", timeout=10)
        return os.path.normpath(out) if out else None
    except GitError:
        return None


def find_config_py(root):
    """Top-level config.py wins. Otherwise search, skipping vendored/backup
    trees. Ambiguity is an error, never a guess — picking the wrong config.py
    would publish another program's numbers under this prefix."""
    top = os.path.join(root, "config.py")
    if os.path.isfile(top):
        return top
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if "config.py" in filenames:
            found.append(os.path.join(dirpath, "config.py"))
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError("no config.py in this repository")
    rel = ", ".join(os.path.relpath(f, root) for f in sorted(found)[:5])
    raise ValueError("ambiguous config.py ({0} candidates: {1})".format(len(found), rel))


# ─────────────────────────────────────────────────────── declaration ──
def load_declaration(root):
    """wm_zig.json is OPTIONAL. A repo with none publishes on defaults alone -
    that is what makes the tooling copy-paste across all the ZIG repos."""
    path = os.path.join(root, "wm_zig.json")
    if not os.path.isfile(path):
        return {}, path
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), path


def derive_prefix(root, decl):
    """Must match ZigInstaller's infer_prefix_scores() exactly — it derives the
    same prefix from the jig's ~/<PREFIX>_TEST_ZIG_FAST directory. A mismatch
    here silently breaks the join."""
    if decl and decl.get("prefix"):
        return str(decl["prefix"]).strip()
    name = os.path.basename(os.path.normpath(root))
    for suffix in ("_TEST_ZIG_FAST", "_TEST_ZIG"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return ""


def normalize_remote(url):
    """github.com/Org/Repo(.git), scp-style or https, with or without userinfo
    -> 'github.com/org/repo'. Used only for the expect_remote comparison."""
    u = (url or "").strip()
    u = re.sub(r"^[a-zA-Z0-9+.-]+://", "", u)
    u = re.sub(r"^[^/@]*@", "", u)
    u = u.replace(":", "/", 1) if "/" not in u.split(":")[0] else u
    if u.endswith(".git"):
        u = u[:-4]
    return u.rstrip("/").lower()


# A parameter that config.py never assigns cannot be verified on that program.
# Detected rather than declared: 13 of 18 ZIG programs have no ZIG_APP_VER, 7 no
# BATCH_ID, 3 no valid_prd_app_ver. Requiring a human to list those per repo
# would mean editing most of the fleet by hand.
def unresolvable_keys(cfg, config_text):
    """-> (absent, broken)

    absent: the variable is not assigned anywhere in config.py, so the program
            genuinely cannot report it -> safe to mark unverifiable.
    broken: the variable IS assigned but the evaluator could not resolve it ->
            NEVER auto-marked. That is an evaluator gap, and silently excusing
            it would weaken the check without anyone noticing.
    """
    absent, broken = [], []
    for _, key, var, _sub in _OUT_MAP:
        if (cfg.get(key) or "").strip():
            continue
        assigned = re.search(r"^\s*%s\s*=" % re.escape(var), config_text or "", re.M)
        (broken if assigned else absent).append(key)
    return absent, broken


def merge_params(cfg, decl):
    """config.py wins wherever it has a non-empty value; wm_zig.json fills the
    gaps. customer_name is always declared — config.py cannot express it."""
    declared = (decl or {}).get("params") or {}
    params, sources = {}, {}
    for key in CONFIG_KEYS:
        val = str(cfg.get(key) or "").strip()
        dec = str(declared.get(key) or "").strip()
        if val:
            params[key], sources[key] = val, "config.py"
        elif dec:
            params[key], sources[key] = dec, "declared"
        else:
            params[key], sources[key] = "", ""
    # customer_name is not in config.py. Default it to the prefix, which is right
    # for every program except the few with a separate trading name.
    params["customer_name"] = str(
        (decl or {}).get("customer_name") or declared.get("customer_name") or "").strip()
    sources["customer_name"] = "declared" if params["customer_name"] else ""
    return params, sources


# ────────────────────────────────────────────────────────── SECURITY ──
# Layer 2: value shape whitelist, calibrated on the real corpus. U+2013 EN DASH
# is permitted because PTM's BATCH_ID contains one. ':' and '?' are excluded,
# so no URL can pass. Longest legitimate value observed across 24 ZIGs is 30.
VALUE_RE = re.compile("^[A-Za-z0-9][A-Za-z0-9._/\u2013\\- ]{0,119}$")
MAX_LEN = 120

SECRET_NAME_RE = re.compile(
    "(?i)(token|secret|passwd|password|api[_-]?key|apikey|auth|bearer|credential|"
    "private[_-]?key|privkey|cert|signature|\\bsig\\b|salt|seed|licen[cs]e)")

# Layer 4: generic rules, applied to every string in the final rendered payload.
GENERIC_RULES = (
    ("pem",         re.compile("-----BEGIN[ A-Z]*(KEY|CERTIFICATE)")),
    ("url",         re.compile("[a-zA-Z][a-zA-Z0-9+.-]*://")),
    ("query_key",   re.compile("(?i)[?&](token|key|sig|signature|auth|pass)=")),
    ("secret_word", re.compile("(?i)(token|secret|password|passwd|api[_-]?key|bearer)")),
    ("b64_blob",    re.compile("[A-Za-z0-9+/]{32,}={0,2}")),
    ("hex_blob",    re.compile("(?i)\\b[0-9a-f]{32,}\\b")),
    ("control",     re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f]")),
)
ENTROPY_MIN_RUN = 16
ENTROPY_THRESHOLD = 3.6
_RUN_RE = re.compile("[A-Za-z0-9+/=]{%d,}" % ENTROPY_MIN_RUN)
# Fields that legitimately contain a URL or a 40-hex sha and must skip the
# generic rules. They are machine-generated, never operator input.
_EXEMPT_PATHS = {"schema", "repo", "commit", "publisher.eval_sha256"}


def _strings_in(value, out=None, depth=0):
    if out is None:
        out = set()
    if depth > 6:
        return out
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            _strings_in(k, out, depth + 1)
            _strings_in(v, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _strings_in(v, out, depth + 1)
    return out


def build_denylist(env, config_text):
    """Layer 3 — the strongest check. Built from THIS repo's own config.py, so
    it catches the real secrets by identity rather than by heuristic.

    `env` contains every value the evaluator resolved, including server_token.
    It is consumed here and NEVER reaches the payload.
    """
    out = set()
    for name, val in (env or {}).items():
        if SECRET_NAME_RE.search(str(name)):
            out |= _strings_in(val)
        else:
            for s in _strings_in(val):
                if "://" in s or "-----BEGIN" in s:
                    out.add(s)
                    for m in re.finditer("[?&](?:token|key|sig|signature|auth)=([^&\\s'\"]+)", s):
                        out.add(m.group(1))
    # Raw-text sweep: catches values the AST walker could not resolve (e.g.
    # APCRV2's ACTIVE_BOARD_TYPE = _resolve_board_type() call), which would
    # otherwise never enter `env` at all.
    text = config_text or ""
    for m in re.finditer("(?im)^\\s*(\\w*(?:token|secret|key|pass|auth)\\w*)\\s*="
                         "\\s*['\"]([^'\"\\n]{6,})['\"]", text):
        out.add(m.group(2))
    for m in re.finditer("[?&](?:token|key|sig|signature|auth)=([^'\"&\\s]{6,})", text):
        out.add(m.group(1))
    return set(s for s in out if isinstance(s, str) and len(s) >= 6)


def _entropy(s):
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = float(len(s))
    return -sum((c / n) * math.log(c / n, 2) for c in counts.values())


def check_value_shape(params):
    bad = []
    for key in PARAM_KEYS:
        val = params.get(key, "")
        if not val:
            continue   # blank == this program cannot report it (see unresolvable_keys)
        if not VALUE_RE.match(val):
            bad.append("params.{0} : fails the value shape whitelist".format(key))
    return bad


def scan_for_secrets(payload, denylist):
    """Walk every string in the FINAL payload. Non-empty return => refuse.

    Run against the rendered bytes re-parsed, so it inspects literally what
    would be committed and automatically covers anything a future contributor
    adds to the payload.
    """
    reasons = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, "{0}.{1}".format(path, k) if path else str(k))
            return
        if isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, "{0}[{1}]".format(path, i))
            return
        if not isinstance(node, str):
            return
        s = node
        if path in _EXEMPT_PATHS:
            return
        if len(s) > MAX_LEN:
            reasons.append("{0} : longer than {1} characters".format(path, MAX_LEN))
            return
        for name, rx in GENERIC_RULES:
            if rx.search(s):
                reasons.append("{0} : matches rule '{1}'".format(path, name))
                return
        for secret in denylist:
            if secret and secret in s:
                # Deliberately does not echo the secret.
                reasons.append("{0} : contains a secret defined in config.py".format(path))
                return
        for run in _RUN_RE.findall(s):
            if _entropy(run) > ENTROPY_THRESHOLD:
                reasons.append("{0} : high-entropy run looks like a key".format(path))
                return

    walk(payload, "")
    return reasons


# ───────────────────────────────────────────────────────────── payload ──
def render_json(obj):
    """Deterministic bytes: sort_keys+indent make them stable across Python
    versions and platforms (idempotency depends on it), ensure_ascii keeps
    every file pure 7-bit ASCII so there is no BOM, locale or diff mojibake."""
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


_VOLATILE = ("commit", "published_at", "publisher")


def content_key(payload):
    """Idempotency is judged on content only. commit/published_at change on
    every push, so comparing whole files would commit forever. The useful
    consequence: `commit` means 'the commit at which these params last
    changed', which is what an operator actually wants to see."""
    if not payload:
        return b""
    trimmed = dict((k, v) for k, v in payload.items() if k not in _VOLATILE)
    return render_json(trimmed)


def build_payload(prefix, params, sources, decl, facts, channel):
    payload = {
        "schema": SCHEMA_URL,
        "schema_version": SCHEMA_VERSION,
        "prefix": prefix,
        "customer_name": params["customer_name"],
        "repo": facts["repo"],
        "branch": facts["branch"],
        "commit": facts["commit"],
        "published_at": facts["published_at"],
        "channel": channel,
        "publisher": {
            "tool_version": TOOL_VERSION,
            "eval_version": EVAL_VERSION,
            "eval_sha256": facts["eval_sha256"],
        },
        # Explicit literal keys. There is no code path by which an arbitrary
        # config.py name can become a published field.
        "params": dict((k, params[k]) for k in PARAM_KEYS),
        "sources": dict((k, sources.get(k) or "unverifiable") for k in PARAM_KEYS),
    }
    extra = dict((decl or {}).get("extra") or {})
    auto_unver = [k for k in PARAM_KEYS if sources.get(k) == "unverifiable"]
    declared_unver = [x for x in (extra.get("unverifiable_keys") or []) if x in PARAM_KEYS]
    merged_unver = sorted(set(auto_unver) | set(declared_unver))
    clean = {}
    for k, v in extra.items():
        if k == "unverifiable_keys":
            continue
        clean[k] = str(v)
    if merged_unver:
        clean["unverifiable_keys"] = merged_unver
    if clean:
        payload["extra"] = clean
    approval = (decl or {}).get("approval") or {}
    if approval.get("ref") or approval.get("approved_on"):
        payload["approval"] = dict((k, str(approval[k]))
                                   for k in ("ref", "approved_on") if approval.get(k))
    assert set(payload["params"]) == set(PARAM_KEYS), "params key set drifted"
    return payload


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def repo_facts(root, pushed_sha, eval_path):
    try:
        url, _, _ = git(root, "remote", "get-url", "origin", timeout=10)
    except GitError:
        url = ""
    try:
        branch, _, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    except GitError:
        branch = ""
    sha = pushed_sha
    if not sha or not re.match("^[0-9a-f]{40}$", sha or ""):
        try:
            sha, _, _ = git(root, "rev-parse", "HEAD", timeout=10)
        except GitError:
            sha = ""
    return {
        "repo": url,
        "branch": branch,
        "commit": sha,
        "published_at": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eval_sha256": sha256_file(eval_path),
    }


# ───────────────────────────────────────────────── cache clone + push ──
def cache_dir(override=None):
    if override:
        return os.path.abspath(override)
    if os.environ.get("WM_ZIG_CACHE"):
        return os.path.abspath(os.environ["WM_ZIG_CACHE"])
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".wm_zig")


def acquire_lock(base, budget):
    """os.mkdir is the atomic primitive. A lock older than 120s is assumed to
    belong to a killed process and is broken."""
    path = os.path.join(base, "publish.lock")
    deadline = time.time() + budget
    while True:
        try:
            os.makedirs(path)
            return path
        except OSError as e:
            if e.errno != errno.EEXIST:
                return None
            try:
                if time.time() - os.path.getmtime(path) > 120:
                    shutil.rmtree(path, ignore_errors=True)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.25)


def ensure_clone(clone, url):
    if os.path.isdir(os.path.join(clone, ".git")):
        return
    parent = os.path.dirname(clone)
    os.makedirs(parent, exist_ok=True)
    shutil.rmtree(clone, ignore_errors=True)
    git(None, "clone", "--depth=1", url, clone, timeout=90)


def sync_clone(clone):
    """The clone is a scratch buffer: fetch, hard reset, clean. Never merge,
    never rebase, never pull."""
    try:
        git(clone, "fetch", "--depth=1", "origin", "main", timeout=60)
    except GitError:
        pass
    _, _, rc = git(clone, "rev-parse", "--verify", "--quiet", "FETCH_HEAD",
                   check=False, timeout=15)
    if rc == 0:
        git(clone, "reset", "--hard", "FETCH_HEAD", timeout=30)
    else:
        # Day-0 path: the central repo has no commits yet.
        git(clone, "symbolic-ref", "HEAD", "refs/heads/main", check=False, timeout=10)
    git(clone, "clean", "-fdx", check=False, timeout=30)


def regenerate_index(clone):
    entries = []
    dev_dir = os.path.join(clone, "zigs")
    rel_dir = os.path.join(dev_dir, "released")

    def load(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as e:
            log("skipping unreadable {0}: {1}".format(os.path.basename(path), e))
            return None

    prefixes = set()
    for d in (dev_dir, rel_dir):
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.endswith(".json"):
                    prefixes.add(fn[:-5])

    for prefix in sorted(prefixes):
        dev = load(os.path.join(dev_dir, prefix + ".json")) if \
            os.path.isfile(os.path.join(dev_dir, prefix + ".json")) else None
        rel = load(os.path.join(rel_dir, prefix + ".json")) if \
            os.path.isfile(os.path.join(rel_dir, prefix + ".json")) else None
        src = rel or dev
        if not src:
            continue
        entry = {
            "prefix": prefix,
            "customer_name": src.get("customer_name", ""),
            "file": "zigs/released/{0}.json".format(prefix) if rel
                    else "zigs/{0}.json".format(prefix),
            "released_file": "zigs/released/{0}.json".format(prefix) if rel else None,
            "channel": "released" if rel else "dev",
            "commit": src.get("commit", ""),
            "published_at": src.get("published_at", ""),
            "params": src.get("params", {}),
            # Carried so ZigInstaller needs exactly ONE fetch for the whole
            # fleet: extra holds unverifiable_keys, publisher holds the
            # evaluator sha the drift banner compares against.
            "extra": src.get("extra", {}),
            "publisher": src.get("publisher", {}),
        }
        entries.append(entry)
    return render_json({"schema_version": SCHEMA_VERSION,
                        "count": len(entries), "zigs": entries})


def commit_and_push(clone, rel_path, blob, msg, ident, attempts=5):
    """Each attempt re-runs sync -> write -> regenerate index -> commit -> push.
    Deliberately not rebase: our commit is a deterministic full-file write, so
    re-applying is simpler and can never strand a .git/rebase-merge directory
    on a developer's machine (and rebase misbehaves on a shallow clone)."""
    for attempt in range(1, attempts + 1):
        target = os.path.join(clone, rel_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(blob)
        with open(os.path.join(clone, "index.json"), "wb") as fh:
            fh.write(regenerate_index(clone))
        git(clone, "add", "--", rel_path, "index.json", timeout=20)
        _, _, rc = git(clone, "diff", "--cached", "--quiet", check=False, timeout=20)
        if rc == 0:
            return True, "no change"
        git(clone, "-c", "user.name=" + ident[0], "-c", "user.email=" + ident[1],
            "commit", "-m", msg, timeout=30)
        out, err, rc = git(clone, "push", "origin", "HEAD:main", check=False, timeout=60)
        if rc == 0:
            return True, "pushed"
        blob_err = (err + " " + out).lower()
        if ("non-fast-forward" in blob_err or "fetch first" in blob_err
                or "rejected" in blob_err):
            if attempt == attempts:
                return False, "push rejected after {0} attempts".format(attempts)
            time.sleep(0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.3))
            git(clone, "reset", "--hard", "HEAD~1", check=False, timeout=20)
            sync_clone(clone)
            continue
        if ("authentication failed" in blob_err or "could not read username" in blob_err
                or "terminal prompts disabled" in blob_err or "403" in blob_err):
            return False, ("cannot authenticate to wm_zig_config. Run once:  "
                           "git -C {0} push".format(clone))
        if "write access" in blob_err or "permission" in blob_err:
            return False, "no write access to wm_zig_config - ask for collaborator access"
        return False, (err or out or "push failed").splitlines()[0][:200]
    return False, "exhausted attempts"


# ──────────────────────────────────────────────────────────────── main ──
def emit_declaration(root, cfg, prefix, config_text=""):
    """A starter wm_zig.json. With zero-config publishing this is only needed to
    override a default, so it is emitted empty of guesses - no _TODO gate to
    clear before a repo can publish."""
    absent, broken = unresolvable_keys(cfg, config_text)
    params = {}
    todo = list(broken)
    try:
        url, _, _ = git(root, "remote", "get-url", "origin", timeout=10)
    except GitError:
        url = ""
    try:
        branch, _, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    except GitError:
        branch = "main"
    decl = {
        "schema_version": 1,
        "prefix": prefix,
        "customer_name": CUSTOMER_NAMES.get(prefix, prefix),
        "publish": True,
        "expect_remote": url,
        "branch": branch,
    }
    if params:
        decl["params"] = params
    if absent:
        decl["_note"] = ("These parameters are not defined in config.py and are "
                         "published as not-checkable automatically: "
                         + ", ".join(absent) +
                         ". Add a value under \"params\" only if you want them checked.")
    if todo:
        decl["_TODO"] = todo
    return json.dumps(decl, indent=2, sort_keys=True) + "\n"


def publish(args):

    fail = 1 if args.strict else 0
    budget = float(os.environ.get("WM_ZIG_BUDGET_S", "30"))
    deadline = time.time() + budget
    base = cache_dir(args.cache_dir)
    denylist = set()

    def done(code=0):
        log.to_file(os.path.join(base, "publish.log"), denylist)
        return code

    if os.environ.get("WM_ZIG_PUBLISH") == "0":
        log("publishing disabled (WM_ZIG_PUBLISH=0)")
        return 0

    root = find_repo_root(os.getcwd()) or find_repo_root(
        os.path.dirname(os.path.abspath(__file__)))
    if not root:
        log("not a git repository - skipping")
        return done(fail)

    decl, decl_path = load_declaration(root)
    if decl is None and not args.emit_declaration:
        log("not onboarded (no wm_zig.json) - skipping")
        return done(0)
    if decl is not None and not isinstance(decl, dict):
        log("wm_zig.json is not a JSON object - skipping")
        return done(fail)

    try:
        config_py = find_config_py(root)
    except ValueError as e:
        log("{0} - skipping".format(e))
        return done(fail)

    try:
        source = read_source(config_py)
        env = build_env(source)
        cfg = extract_params(source)
    except SyntaxError as e:
        log("config.py has a syntax error ({0}) - refusing to publish a partial spec".format(e))
        return done(fail)
    except Exception as e:
        log("cannot evaluate config.py: {0}".format(e))
        return done(fail)

    denylist = build_denylist(env, source)
    prefix = derive_prefix(root, decl)

    if args.emit_declaration:
        sys.stdout.write(emit_declaration(root, cfg, prefix or "ZIG", source))
        return 0

    if not prefix:
        log("cannot derive a program prefix from '{0}' - set \"prefix\" in wm_zig.json"
            .format(os.path.basename(root)))
        return done(fail)
    # Defaults good enough to publish with no wm_zig.json at all.
    if not decl.get("customer_name"):
        decl["customer_name"] = CUSTOMER_NAMES.get(prefix, prefix)
    if not decl.get("expect_remote"):
        try:
            decl["expect_remote"], _, _ = git(root, "remote", "get-url", "origin", timeout=10)
        except GitError:
            decl["expect_remote"] = ""
    if decl.get("publish") is False:
        log("publish disabled for this checkout (wm_zig.json publish=false)")
        return done(0)
    if decl.get("_TODO"):
        log("finish onboarding: wm_zig.json still has _TODO {0} - not publishing"
            .format(list(decl["_TODO"])))
        return done(fail)

    # Duplicate-checkout guard. Several prefixes have two clones on disk with
    # different content; without this they overwrite each other on alternate
    # pushes and the published spec silently flip-flops.
    expect = decl.get("expect_remote")
    if expect:
        try:
            actual, _, _ = git(root, "remote", "get-url", "origin", timeout=10)
        except GitError:
            actual = ""
        if normalize_remote(expect) != normalize_remote(actual):
            log("this checkout is not the publisher for '{0}' (expected {1}, got {2}) - skipping"
                .format(prefix, expect, actual or "<none>"))
            return done(0)

    # Ref gate: branch push -> dev channel, annotated tag -> released channel.
    channel, ref = "dev", (args.ref or "")
    if ref.startswith("refs/tags/"):
        channel = "released"
    elif ref.startswith("refs/heads/"):
        want = decl.get("branch")
        if want and ref != "refs/heads/" + str(want):
            log("push to {0} is not the publishing branch ({1}) - skipping".format(ref, want))
            return done(0)
    elif ref:
        log("ref {0} is neither a branch nor a tag - skipping".format(ref))
        return done(0)

    params, sources = merge_params(cfg, decl)
    absent, broken = unresolvable_keys(cfg, source)

    if broken:
        # Assigned in config.py but not resolvable: an evaluator gap, not a
        # property of the program. Publishing it as "unverifiable" would quietly
        # stop checking something that is meant to be checked.
        log("REFUSING to publish {0} - config.py assigns {1} but the evaluator "
            "cannot resolve it. Declare the value in wm_zig.json or tell "
            "engineering.".format(prefix, ", ".join(broken)))
        return done(fail)

    # Anything config.py never defines is published as declared-unverifiable, so
    # the Installer shows it as "not checked on this program" instead of failing
    # every jig forever.
    unverifiable = sorted(k for k in absent if not params.get(k))
    params = {k: v for k, v in params.items()}
    for k in unverifiable:
        sources[k] = "unverifiable"
    still_missing = [k for k in PARAM_KEYS
                     if not params[k] and k not in unverifiable]
    if still_missing:
        log("REFUSING to publish {0} - no value for: {1}"
            .format(prefix, ", ".join(still_missing)))
        log("  set them under \"params\" in wm_zig.json")
        return done(fail)
    if unverifiable:
        log("{0}: {1} parameter(s) are not defined in config.py and will be "
            "published as not-checkable: {2}"
            .format(prefix, len(unverifiable), ", ".join(unverifiable)))

    shape = check_value_shape(params)
    facts = repo_facts(root, args.sha, os.path.abspath(__file__))
    payload = build_payload(prefix, params, sources, decl, facts, channel)
    blob = render_json(payload)

    reasons = shape + scan_for_secrets(json.loads(blob.decode("ascii")), denylist)
    if reasons:
        log("REFUSING to publish {0} - {1} value(s) failed the safety scan:"
            .format(prefix, len(reasons)))
        for r in reasons:
            log("  " + r)
        log("Nothing was written or pushed.")
        return done(fail)

    rel_path = ("zigs/released/{0}.json" if channel == "released" else "zigs/{0}.json").format(prefix)

    if args.do_print or args.dry_run:
        if args.do_print:
            sys.stdout.write(blob.decode("ascii"))
        log("dry-run OK - would write {0} ({1} channel)".format(rel_path, channel))
        if args.do_print:
            return 0

    if args.dry_run:
        return done(0)

    if time.time() >= deadline:
        log("time budget exhausted before publish - skipping")
        return done(0)

    lock = acquire_lock(base, min(5.0, max(0.0, deadline - time.time())))
    if not lock:
        log("another publish is in progress - skipping")
        return done(0)
    try:
        clone = os.path.join(base, "wm_zig_config")
        url = decl.get("config_url") or os.environ.get("WM_ZIG_CONFIG_URL") or DEFAULT_CONFIG_URL
        try:
            ensure_clone(clone, url)
            sync_clone(clone)
        except GitError as e:
            log("cache clone unusable ({0}) - re-cloning once".format(e))
            shutil.rmtree(clone, ignore_errors=True)
            try:
                ensure_clone(clone, url)
                sync_clone(clone)
            except GitError as e2:
                log("cannot reach wm_zig_config ({0}) - will publish on the next push".format(e2))
                return done(0)

        existing = None
        target = os.path.join(clone, rel_path)
        if os.path.isfile(target):
            try:
                with open(target, encoding="utf-8") as fh:
                    existing = json.load(fh)
            except Exception:
                existing = None
        if existing is not None and content_key(existing) == content_key(payload):
            log("no change for {0} ({1}) - nothing to publish".format(prefix, channel))
            return done(0)

        if args.no_push:
            log("--no-push: would publish {0}".format(rel_path))
            return done(0)

        try:
            name, _, _ = git(root, "config", "user.name", timeout=10)
        except GitError:
            name = "wm_zig publisher"
        try:
            email, _, _ = git(root, "config", "user.email", timeout=10)
        except GitError:
            email = "wm-zig@walnutmedical.in"
        msg = "{0}({1}): {2} @ {3}".format(
            "release" if channel == "released" else "publish",
            prefix, os.path.basename(rel_path), (facts["commit"] or "")[:7])

        ok, detail = commit_and_push(clone, rel_path, blob, msg,
                                     (name or "wm_zig publisher",
                                      email or "wm-zig@walnutmedical.in"))
        if ok:
            log("{0} {1} -> {2}".format(detail, prefix, rel_path))
        else:
            log("publish skipped - {0}".format(detail))
        return done(0)
    finally:
        shutil.rmtree(lock, ignore_errors=True)


GITATTRIBUTES_LINES = [
    "",
    "# wm_zig publisher - MUST stay LF or the hook dies with \"bad interpreter: /bin/sh^M\"",
    "wm_zig/**     text eol=lf",
    ".githooks/**  text eol=lf",
    "wm_zig.json   text eol=lf",
]


def cmd_install(root):
    """Set this repository up. Idempotent - safe to re-run after an update."""
    hooks = os.path.join(root, ".githooks")
    os.makedirs(hooks, exist_ok=True)
    hook_path = os.path.join(hooks, "pre-push")
    with open(hook_path, "wb") as fh:                  # LF, never CRLF
        fh.write(PRE_PUSH_HOOK.lstrip("\n").encode("utf-8"))
    try:
        os.chmod(hook_path, 0o755)
    except Exception:
        pass
    log("wrote .githooks/pre-push")

    ga = os.path.join(root, ".gitattributes")
    text = ""
    if os.path.isfile(ga):
        with open(ga, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    if "wm_zig/**" not in text:
        with open(ga, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(GITATTRIBUTES_LINES) + "\n")
        log("added the eol=lf lines to .gitattributes")

    try:
        git(root, "config", "core.hooksPath", ".githooks", timeout=10)
        log("git config core.hooksPath .githooks")
    except GitError as e:
        log("could not set core.hooksPath: {0}".format(e))
        return 1
    try:
        git(root, "update-index", "--add", "--chmod=+x", ".githooks/pre-push",
            check=False, timeout=10)
    except GitError:
        pass

    log("")
    log("This repository is set up. Now commit the files and push:")
    log("    git add wm_zig .githooks .gitattributes")
    log('    git commit -m "chore: wm_zig publisher"')
    log("    git push")
    return 0


def cmd_release(root, tag, decl):
    """Tag the current commit and publish it as the APPROVED specification."""
    prefix = derive_prefix(root, decl) or "ZIG"
    if not tag:
        tag = _utcnow().strftime("v%Y.%m.%d")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,59}$", tag):
        log("'{0}' is not a usable tag name".format(tag))
        return 1
    try:
        out, _, _ = git(root, "status", "--porcelain", timeout=15)
    except GitError as e:
        log("cannot read the repository state: {0}".format(e))
        return 1
    if out.strip():
        log("there are uncommitted changes - commit them first, so the released")
        log("specification matches exactly what is on the jigs:")
        for line in out.splitlines()[:8]:
            log("    " + line)
        return 1
    try:
        git(root, "rev-parse", "--verify", "refs/tags/" + tag, timeout=10)
        log("tag {0} already exists. Pick another name, or push it again with:".format(tag))
        log("    git push origin {0}".format(tag))
        return 1
    except GitError:
        pass

    msg = "{0} approved build {1}".format(prefix, tag)
    try:
        git(root, "tag", "-a", tag, "-m", msg, timeout=15)
    except GitError as e:
        log("could not create the tag: {0}".format(e))
        return 1
    log("created tag {0}".format(tag))
    try:
        git(root, "push", "origin", tag, timeout=90)
    except GitError as e:
        log("tag created locally but the push failed: {0}".format(e))
        log("run:  git push origin {0}".format(tag))
        return 1
    log("pushed {0} - the approved specification is now published as".format(tag))
    log("zigs/released/{0}.json, and the ZIG Installer will check jigs".format(prefix))
    log("against it from now on.")
    return 0


def cmd_status(root, decl):
    prefix = derive_prefix(root, decl) or "?"
    try:
        branch, _, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    except GitError:
        branch = "?"
    try:
        url, _, _ = git(root, "remote", "get-url", "origin", timeout=10)
    except GitError:
        url = "?"
    hooks = ""
    try:
        hooks, _, _ = git(root, "config", "--get", "core.hooksPath", timeout=10)
    except GitError:
        pass
    log("program        : {0}".format(prefix))
    log("repository     : {0}".format(url))
    log("branch         : {0}".format(branch))
    log("hook installed : {0}".format(
        "yes" if hooks.strip() == ".githooks" else "NO - run --install"))
    log("a push of this branch updates : zigs/{0}.json  (engineering tip)".format(prefix))
    log("a push of an annotated tag updates: zigs/released/{0}.json  (approved)".format(prefix))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--release", nargs="?", const="", default=None, metavar="TAG")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--eval", dest="eval_path", default=None, metavar="CONFIG_PY")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--ref", default="")
    ap.add_argument("--sha", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print", dest="do_print", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--emit-declaration", action="store_true")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args(argv)

    # The ZIG Installer streams this file to the jig and runs it with --eval.
    if args.eval_path:
        return _eval_main(["-", args.eval_path])

    root = find_repo_root(os.getcwd()) or find_repo_root(
        os.path.dirname(os.path.abspath(__file__)))
    if not root:
        log("not a git repository - nothing to do")
        return 0

    if args.install:
        return cmd_install(root)
    decl, _ = load_declaration(root)
    if args.status:
        return cmd_status(root, decl)
    if args.release is not None:
        return cmd_release(root, args.release, decl)
    return publish(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:                            # never block a push
        sys.stderr.write("wm_zig: unexpected error ({0}) - push continues\n".format(exc))
        sys.exit(0)
