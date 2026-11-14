#!/usr/bin/env bash
# Refresh the vendored design skills (Impeccable, Intent, Perception-First Design)
# in this repo's .claude/ folder from their upstream GitHub repos.
#
# Claude Code on the web does not install plugins listed in .claude/settings.json,
# but it does load .claude/skills, .claude/agents and .claude/commands from the
# clone. So these plugins are copied in as plain project skills.
#
# Usage, from the repo root:  bash .claude/vendor/update-design-plugins.sh
# Then review `git diff` and commit.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
CLAUDE_DIR="$ROOT/.claude"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

clone() { git clone --quiet --depth 1 "https://github.com/$1.git" "$WORK/$2"; git -C "$WORK/$2" rev-parse --short HEAD; }

IMP_SHA=$(clone pbakaus/impeccable impeccable)
INT_SHA=$(clone ghaida/intent intent)
PFD_SHA=$(clone skovalik/perception-first-design pfd)

mkdir -p "$CLAUDE_DIR/skills" "$CLAUDE_DIR/agents" "$CLAUDE_DIR/commands" "$CLAUDE_DIR/vendor/licenses"

# --- Impeccable (Apache-2.0): the project-scope Claude build committed at its repo root.
# Skill and subagents only. Its design-check hooks are NOT installed (see DESIGN-PLUGINS.md).
rm -rf "$CLAUDE_DIR/skills/impeccable"
cp -R "$WORK/impeccable/.claude/skills/impeccable" "$CLAUDE_DIR/skills/impeccable"
rm -f "$CLAUDE_DIR"/agents/impeccable-*.md
cp "$WORK/impeccable/.claude/agents/"impeccable-*.md "$CLAUDE_DIR/agents/"
cp "$WORK/impeccable/LICENSE" "$CLAUDE_DIR/vendor/licenses/impeccable-LICENSE"
cp "$WORK/impeccable/NOTICE.md" "$CLAUDE_DIR/vendor/licenses/impeccable-NOTICE.md"
cp "$WORK/impeccable/.claude/settings.json" "$CLAUDE_DIR/vendor/impeccable-hooks.settings.json"

# --- Intent (CC0-1.0): all skills and its six named agents, same flat layout as `npx skills add`.
for d in "$WORK/intent/skills/"*/; do
  name=$(basename "$d"); rm -rf "$CLAUDE_DIR/skills/$name"; cp -R "$d" "$CLAUDE_DIR/skills/$name"
done
for f in "$WORK/intent/agents/"*.md; do cp "$f" "$CLAUDE_DIR/agents/"; done
cp "$WORK/intent/LICENSE" "$CLAUDE_DIR/vendor/licenses/intent-LICENSE"

# --- Perception-First Design (CC BY-SA 4.0): skill plus its framework and corpus, which the
# skill reads from its plugin root, now kept inside the skill folder. Commands become /pfd-*.
P="$CLAUDE_DIR/skills/pfd"
rm -rf "$P"
cp -R "$WORK/pfd/skills/pfd" "$P"
cp -R "$WORK/pfd/framework" "$WORK/pfd/corpus" "$P/"
cp "$WORK/pfd/LICENSE" "$WORK/pfd/NOTICE" "$P/"
cp "$WORK/pfd/LICENSE" "$CLAUDE_DIR/vendor/licenses/perception-first-design-LICENSE"
cp "$WORK/pfd/NOTICE" "$CLAUDE_DIR/vendor/licenses/perception-first-design-NOTICE"
python3 - "$P/SKILL.md" <<'PY'
import sys, re
p = sys.argv[1]; s = open(p).read()
s = s.replace(
  "All paths in this document are plugin-root-relative. Skill-local paths (`references/...`) resolve from this skill's directory; repo-level paths (`framework/...`, `corpus/...`) resolve from the plugin root.",
  "All paths in this document resolve from this skill's directory (`.claude/skills/pfd/`), including `references/...`, `framework/...` and `corpus/...`. [Vendored copy: the upstream plugin keeps `framework/` and `corpus/` at its plugin root; they were moved into this skill folder.]")
s = s.replace("in the plugin root", "in this skill's folder")
s = re.sub(r"/perception-first-design:(solve|evaluate|analyze|all)", r"/pfd-\1", s)
open(p, "w").write(s)
PY
for f in "$WORK/pfd/commands/"*.md; do
  name=$(basename "$f" .md)
  python3 - "$f" "$CLAUDE_DIR/commands/pfd-$name.md" "$name" <<'PY'
import sys, re
src, dst, name = sys.argv[1:]
s = open(src).read()
s = re.sub(r"^name: .*$", f"name: pfd-{name}", s, count=1, flags=re.M)
s = re.sub(r"(?<![\w/.-])(corpus|framework)/", r".claude/skills/pfd/\1/", s)
s = re.sub(r"/perception-first-design:(solve|evaluate|analyze|all)", r"/pfd-\1", s)
s += "\n\n<!-- Vendored from github.com/skovalik/perception-first-design (CC BY-SA 4.0, Stefan Kovalik / Aurochs). Changed: command renamed to pfd-" + name + ", corpus/framework paths point at .claude/skills/pfd/. -->\n"
open(dst, "w").write(s)
PY
done

cat > "$CLAUDE_DIR/vendor/design-plugins.lock" <<EOF
impeccable              pbakaus/impeccable                $IMP_SHA
intent                  ghaida/intent                     $INT_SHA
perception-first-design skovalik/perception-first-design  $PFD_SHA
EOF
echo "Updated: impeccable@$IMP_SHA intent@$INT_SHA perception-first-design@$PFD_SHA"
