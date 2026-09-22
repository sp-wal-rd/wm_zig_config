# wm_zig_config

The single source of truth for every Walnut Medical test-jig (ZIG) program spec.

Each ZIG repository publishes its own spec here **automatically on `git push`**. Nothing in this
repository is edited by hand. The ZIG Installer reads it to verify a converted jig.

---

## Why this exists

The Installer's *Verify Conversion* check compares the 7 parameters read off a jig against an
"approved master". That master used to be a `key = value` text file kept locally on each operator PC
(`master_files/<PREFIX>.txt`), gitignored and copied around by hand.

It drifted, silently. At the time this repo was created, `master_files/PTM.txt` disagreed with the PTM
repo on **3 of 7** parameters — and none of those three was a real defect. The check was reporting
failures that were really just a stale file, which is the fastest way to train operators to ignore a
quality gate.

Now the spec is generated from the repo that actually defines it, so a verification mismatch means
what it says.

---

## Layout

```
index.json                  every ZIG in one file (what the Installer fetches)
schema/zig.schema.json      the per-ZIG contract
schema/index.schema.json
zigs/<PREFIX>.json          dev tip   - published on a branch push
zigs/released/<PREFIX>.json approved  - published on an annotated tag push
tools/                      CANONICAL publisher + evaluator + installer
```

`<PREFIX>` is the program prefix: the ZIG repo's directory name minus `_TEST_ZIG` / `_TEST_ZIG_FAST`
(`PTM_TEST_ZIG_FAST` -> `PTM`). This is the join key: the Installer derives the identical prefix from
the jig's own `~/<PREFIX>_TEST_ZIG_FAST` directory, so the two sides line up without a lookup table.

### dev and released — what the two modes mean

Every program has up to two published specifications:

| | `zigs/<PREFIX>.json` | `zigs/released/<PREFIX>.json` |
|---|---|---|
| written when | you push a **branch** | you push an **annotated tag** |
| means | what R&D has right now | a build someone approved |
| changes | on every push that alters a value | only when you make a release |

**The ZIG Installer checks jigs against `released/` by default.** That is the
whole point of the split. Without it, the moment R&D pushes a version bump every
jig on the floor still running last month's *approved* build starts reporting
NOT OK — production blocked by a change nobody authorised for manufacturing.

Until a program has its first release, the Installer falls back to the dev spec
and says so on every report:

```
Checked against:  PTM e560d97 (engineering version)
```

so nobody mistakes an unapproved build for an approved one.

## How to make a release

When a build has been approved for manufacturing, from inside that ZIG repo:

```
python wm_zig/wm_zig.py --release v1.2.0
```

That is the whole procedure. It will:

1. refuse if anything is uncommitted — the released spec must match exactly what
   the jigs will pull;
2. create an annotated tag `v1.2.0` on the current commit;
3. push the tag, which fires the hook and writes
   `zigs/released/<PREFIX>.json`.

From that moment every conversion check measures jigs against that build, and the
report footer changes to `PTM v1.2.0 (released)`.

Leave the tag off (`--release`) and it uses today's date, e.g. `v2026.09.22`.

**Releasing a fix later** is the same command with a new tag. The old released
spec is replaced; the tag stays in the repo as the record of what was approved
when.

**If the push fails** (no network, no credentials) the tag is still created
locally and the command tells you exactly what to run:
`git push origin v1.2.0`.

### Switching the floor over to released-only

Once the programs you manufacture are being tagged, set Settings → Central ZIG
Config → **Channel** to *Released only*. The Installer then refuses to check a
jig against an untagged engineering build at all, instead of quietly falling
back to it.

---

## Onboarding a ZIG repo

**Copy one file. Run one command.** There is no config file to write.

```
wm_zig/wm_zig.py            <- copy this into the repo, identical everywhere
```

```
python wm_zig/wm_zig.py --install
git add wm_zig .githooks .gitattributes
git commit -m "chore: wm_zig publisher"
git push
```

`--install` writes `.githooks/pre-push`, adds the `.gitattributes` lines that
keep it LF, and runs `git config core.hooksPath .githooks`. Every push from then
on republishes automatically.

For many repos at once: `powershell -File tools/rollout.ps1 -All <root>` (add
`-Apply` once the report looks right).

### The commands

| Command | What it does |
|---|---|
| `--install` | set the repo up; run once per clone, safe to re-run |
| `--status` | what this repo publishes and where |
| `--dry-run [--print]` | show the spec that would be published, change nothing |
| `--release [TAG]` | tag this commit and publish it as the **approved** spec |
| `--publish` | what the hook calls; you never type this |

### What it works out on its own

| Value | Where it comes from |
|---|---|
| program prefix | folder name minus `_TEST_ZIG` / `_TEST_ZIG_FAST` |
| repository, branch, commit | `git` |
| customer name | the prefix (only exceptions are listed in `CUSTOMER_NAMES`) |
| the 6 version parameters | `config.py`, statically evaluated |
| which parameters cannot be checked | detected — see below |

### Parameters a program cannot report

Most ZIG programs do not define all six. Across the fleet: 13 of 18 have no
`ZIG_APP_VER`, 7 no `BATCH_ID`, 3 no `valid_prd_app_ver`.

The publisher detects this and publishes those parameters as **not checkable**,
so the Installer shows them as "not checked on this program" instead of failing
every jig forever. Nobody has to declare anything.

It draws a hard line between two cases:

- the variable is **not in `config.py` at all** → published as not-checkable;
- the variable **is** in `config.py` but could not be resolved → the publisher
  **refuses to publish**.

The second case means the evaluator has a gap, and quietly marking it
"not checkable" would stop checking something that is meant to be checked.
`APCRV2_TEST_ZIG` is the live example: it sets
`ACTIVE_BOARD_TYPE = _resolve_board_type()`, a function call the static evaluator
cannot execute, so every `if ACTIVE_BOARD_TYPE == ...` branch is skipped. That
repo needs values in `wm_zig.json` (it also builds two board variants, so one
spec cannot describe both).

### `wm_zig.json` — optional, overrides only

Add one only to override a default:

```json
{
  "customer_name": "APC",
  "publish": false,
  "params": { "test_jig_app": "PTM_0_0_7" }
}
```

- `customer_name` — when the customer is not the folder prefix.
- `publish: false` — for `Backup/` and `Ref/` copies that must never publish.
- `params` — a value for something `config.py` cannot supply, when you *do* want
  it checked rather than skipped.
- `expect_remote` — set automatically on first publish; it stops a second
  checkout of the same program overwriting the first one's spec.

## Rules

1. **Never hand-edit `zigs/` or `index.json`.** The next push overwrites you, and CI will fail first.
2. **Never put a secret in `wm_zig.json`.** This repository is public. The publisher runs a four-layer
   scan and refuses to publish anything resembling a token, URL, key or high-entropy blob — but the
   first line of defence is not pasting one.
3. **Do not edit `tools/wm_zig_eval.py` in a ZIG repo.** It is a byte-copy of the canonical file here,
   and the Installer runs the same bytes on the jig. If they diverge, the published spec and the
   device reading stop being comparable. CI checks the sha256 of every published entry against this
   repo's copy and fails on drift.

## Troubleshooting

The publisher never blocks a push. If a spec is not updating, look at `~/.wm_zig/publish.log`.

| Message | Meaning |
|---|---|
| `not onboarded` | no `wm_zig.json` in the repo |
| `no change` | params are identical to what is already published (normal) |
| `not the publisher for <PREFIX>` | wrong clone — `expect_remote` does not match this `origin` |
| `finish onboarding` | `_TODO` in `wm_zig.json` is not empty yet |
| `REFUSING to publish` | a value failed the safety scan; the reason names the field |
| `cannot authenticate` | run `git -C ~/.wm_zig/wm_zig_config push` once to refresh your credential |

Disable it for one push with `WM_ZIG_PUBLISH=0 git push`.
