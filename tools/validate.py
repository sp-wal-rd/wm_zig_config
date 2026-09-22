"""
validate.py — CI guard for wm_zig_config. READ-ONLY: it never writes or pushes.

Run by .github/workflows/validate.yml on every push and PR, and useful locally:

    python tools/validate.py

Checks, in order of how much they matter:

  1. EVALUATOR DRIFT. Every published entry records the sha256 of the
     wm_zig_eval.py that produced it. If that differs from this repo's copy,
     someone is publishing with a stale or modified evaluator and the specs it
     produced are no longer comparable with what the Installer reads off a jig.
     This is the check that stops the central repo being quietly wrong.
  2. Secret scan, re-run server-side, so a hand-patched publisher still cannot
     land a token on main.
  3. index.json is byte-identical to a regeneration — catches an old publisher,
     a hand-edit, or a bad merge.
  4. Schema validity, and filename matching the entry's own prefix.

Exit code 0 = clean, 1 = at least one failure.
"""

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# The publisher is a single file in wm_zig/ - the same one every ZIG repo copies.
sys.path.insert(0, os.path.join(ROOT, "wm_zig"))

import wm_zig as pub                                            # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None


class Report(object):
    def __init__(self):
        self.failures = []
        self.checks = 0

    def check(self, ok, label, detail=""):
        self.checks += 1
        if ok:
            print("  ok    {0}".format(label))
        else:
            print("  FAIL  {0}{1}".format(label, (" - " + detail) if detail else ""))
            self.failures.append(label)
        return ok


def load(path):
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def zig_files():
    out = []
    for sub in ("zigs", os.path.join("zigs", "released")):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json"):
                out.append(os.path.join(d, fn))
    return out


def main():
    rep = Report()
    files = zig_files()
    print("wm_zig_config validation - {0} published spec(s)\n".format(len(files)))

    eval_path = os.path.join(ROOT, "wm_zig", "wm_zig.py")
    canonical_sha = pub.sha256_file(eval_path) if os.path.isfile(eval_path) else None
    print("canonical wm_zig.py sha256: {0}\n".format(canonical_sha))

    schema = None
    schema_path = os.path.join(ROOT, "schema", "zig.schema.json")
    if jsonschema and os.path.isfile(schema_path):
        schema = load(schema_path)
    elif not jsonschema:
        print("  note  jsonschema not installed - skipping schema validation\n")

    for path in files:
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        print(rel)
        try:
            doc = load(path)
        except Exception as e:
            rep.check(False, rel + " : parses as JSON", str(e))
            continue

        prefix = os.path.basename(path)[:-5]
        rep.check(doc.get("prefix") == prefix, rel + " : filename matches prefix field",
                  "file says {0!r}, document says {1!r}".format(prefix, doc.get("prefix")))

        expected_channel = "released" if "/released/" in rel else "dev"
        if "channel" in doc:
            rep.check(doc["channel"] == expected_channel,
                      rel + " : channel matches its directory",
                      "expected {0}, got {1}".format(expected_channel, doc.get("channel")))

        if schema is not None:
            try:
                jsonschema.validate(doc, schema)
                rep.check(True, rel + " : conforms to zig.schema.json")
            except jsonschema.ValidationError as e:
                loc = "/".join(str(x) for x in e.absolute_path) or "(root)"
                rep.check(False, rel + " : conforms to zig.schema.json",
                          "{0}: {1}".format(loc, e.message[:160]))

        # 1. Evaluator drift.
        got = (doc.get("publisher") or {}).get("eval_sha256")
        rep.check(canonical_sha is not None and got == canonical_sha,
                  rel + " : published with the canonical evaluator",
                  "entry was built by evaluator {0}... - that publisher is stale, "
                  "re-run it after updating wm_zig/wm_zig.py"
                  .format((got or "?")[:12]))

        # 2. Secret scan (generic rules only; the repo-aware denylist is not
        #    available server-side, which is exactly why the publisher runs it
        #    locally where config.py is in reach).
        reasons = pub.scan_for_secrets(doc, set())
        rep.check(not reasons, rel + " : contains no secret-shaped values",
                  "; ".join(reasons[:3]))
        print("")

    # 3. index.json is what a regeneration would produce.
    idx_path = os.path.join(ROOT, "index.json")
    if os.path.isfile(idx_path):
        with io.open(idx_path, "rb") as fh:
            on_disk = fh.read()
        regenerated = pub.regenerate_index(ROOT)
        ok = rep.check(on_disk == regenerated,
                       "index.json : matches a regeneration",
                       "stale or hand-edited - it is generated by the publisher, "
                       "never edited")
        if not ok:
            try:
                a = json.loads(on_disk.decode("utf-8"))
                b = json.loads(regenerated.decode("utf-8"))
                print("        on disk : {0} entries {1}".format(
                    a.get("count"), sorted(z.get("prefix") for z in a.get("zigs", []))))
                print("        expected: {0} entries {1}".format(
                    b.get("count"), sorted(z.get("prefix") for z in b.get("zigs", []))))
            except Exception:
                pass
    else:
        rep.check(False, "index.json : exists")

    # 4. No duplicate prefixes within a channel (structurally impossible, cheap).
    for sub in ("zigs", "zigs/released"):
        seen = {}
        for p in files:
            r = os.path.relpath(p, ROOT).replace(os.sep, "/")
            if os.path.dirname(r) != sub:
                continue
            seen.setdefault(os.path.basename(p), []).append(r)
        dupes = [v for v in seen.values() if len(v) > 1]
        if dupes:
            rep.check(False, sub + " : no duplicate prefixes", str(dupes))

    print("\n{0} check(s), {1} failure(s)".format(rep.checks, len(rep.failures)))
    if rep.failures:
        print("\nFAILED:")
        for f in rep.failures:
            print("  - " + f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
