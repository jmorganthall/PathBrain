# Vendored design skills

This repo carries three design toolkits as plain Claude Code project skills, so they
load in Claude Code on the web as well as locally.

Why vendored: Claude Code on the web does not install plugins that a repo turns on
in `.claude/settings.json` (`enabledPlugins` / `extraKnownMarketplaces`). It does load
`.claude/skills/`, `.claude/agents/` and `.claude/commands/` from the clone.

| Toolkit | Upstream | License | What lands here |
|---|---|---|---|
| Impeccable (visual design) | https://github.com/pbakaus/impeccable | Apache-2.0 | `skills/impeccable/`, `agents/impeccable-*.md` |
| Intent (UX design and strategy) | https://github.com/ghaida/intent | CC0-1.0 | 18 skills under `skills/` (`intent`, `evaluate`, `journey`, ...), agents `ember`, `noor`, `rune`, `sage`, `vigil`, `wren` |
| Perception-First Design | https://github.com/skovalik/perception-first-design | CC BY-SA 4.0, Stefan Kovalik / Aurochs | `skills/pfd/` (with its `framework/` and `corpus/`), commands `/pfd-solve`, `/pfd-evaluate`, `/pfd-analyze`, `/pfd-all` |

Pinned upstream commits are in `design-plugins.lock`. License texts are in `licenses/`.

## Use

- `/impeccable` (then e.g. `/impeccable audit`, `/impeccable polish`). Run `/impeccable init` once per repo to write `PRODUCT.md` and `DESIGN.md`. On first use the skill downloads its engine binary from Impeccable's GitHub releases and checks it against the checksums shipped in `skills/impeccable/scripts/engine-checksums.txt`.
- `/intent` routes to the right Intent skill; the individual skills also trigger on their own.
- `/pfd-solve`, `/pfd-evaluate`, `/pfd-analyze`, `/pfd-all`, or just ask to "pfd this".

## Changes from upstream

- Perception-First Design: `framework/` and `corpus/` moved inside `skills/pfd/`; path notes in `SKILL.md` updated to match; commands renamed from `/perception-first-design:<mode>` to `/pfd-<mode>` with paths pointing at `.claude/skills/pfd/`.
- Impeccable and Intent: copied unchanged.

## Not installed: Impeccable's design-check hooks

Impeccable can also run hooks that check UI files automatically: at session start, after
every Edit/Write, and a deeper pass when Claude finishes a turn. Each runs
`.claude/skills/impeccable/scripts/impeccable hook`, which downloads and runs Impeccable's
engine binary. They are off here. To turn them on, merge the `hooks` block from
`impeccable-hooks.settings.json` into `.claude/settings.json`.

## Update

From the repo root: `bash .claude/vendor/update-design-plugins.sh`, review `git diff`, commit.
