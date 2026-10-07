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

### How a spec gets published

**By default every push publishes the approved spec.** Copy the file in, push,
and the ZIG Installer checks jigs against what you just pushed. No tagging step,
no config file.

```
git push origin main     ->  zigs/released/<PREFIX>.json
```

`zigs/released/` is what the Installer reads, so that is where a repository
writes by default.

Two guards apply, both automatic:

- **Only the repository's own publishing branch publishes.** That is whatever
  `origin/HEAD` points at (else `main`/`master`). Pushing a scratch branch
  changes nothing, so an experiment cannot redefine what the factory checks
  against.
- **A tag also publishes as approved**, so a tag remains a permanent record of
  what was approved and when.

### Optional: the two-stage approval flow

Some programs will eventually want engineering and the factory to move
independently — R&D pushing freely while the floor stays on an approved build.
Turn that on per repository with one line in `wm_zig.json`:

```json
{ "channel": "dev" }
```

That repository then behaves like this:

| You push | Writes | The Installer uses |
|---|---|---|
| a **branch** | `zigs/<PREFIX>.json` | ignored once a released spec exists |
| a **tag** | `zigs/released/<PREFIX>.json` | **this** |

Engineering can then push all day without touching the floor, and
`python wm_zig/wm_zig.py --release v1.2.0` promotes a build when it is approved.

**The trade-off, stated plainly.** With the default, a jig running last month's
build starts reporting NOT OK the moment R&D pushes a version change — the floor
always tracks the newest push. The two-stage flow is what prevents that. Start
with the default; switch a program over the day that becomes a problem.

### What the Installer selects

Settings → Central ZIG Config → **Channel**, default **Auto**: use the released
spec if there is one, otherwise fall back to the dev spec and label it
`(engineering version)` on the report. *Released only* refuses that fallback.

## Tagging a release (optional)

Normal pushes already publish the approved spec, so this is only for keeping a
permanent record of an approved build — or required if the repository uses the
two-stage flow above.

```
python wm_zig/wm_zig.py --release v1.2.0
```

It refuses if anything is uncommitted (the published spec must match exactly what
the jigs will pull), creates an annotated tag and pushes it. Omit the tag for a
dated one, e.g. `v2026.10.07`. If the push fails the tag still exists locally and
the command prints the `git push origin <tag>` to run.

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
- `channel` — `"dev"` switches this repository to the two-stage approval flow
  described above. Omit it and every push publishes the approved spec.
- `branch` — pin the publishing branch. Omit it and the repository's default
  branch is detected automatically.

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
