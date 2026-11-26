#!/usr/bin/env node
// archguard: a ratchet on data-layer drift. No dependencies.
//
// Each rule counts places where a data shape escapes the domain model: a
// hand-written shape outside src/domain, an untyped JSON column, an `any`, a
// runtime ALTER TABLE, a defensive read. The counts are compared with a
// committed baseline. A count may go down, never up. A new app starts at zero
// everywhere; an existing app commits today's counts and can only improve.
//
//   node scripts/archguard.mjs                  check against the baseline (exit 1 on any increase)
//   node scripts/archguard.mjs --report         print every hit, file:line
//   node scripts/archguard.mjs --lower-baseline write counts that went down (never raises one)
//   node scripts/archguard.mjs --init-baseline  write today's counts (first install only)
//
// Config: archguard.config.json in the repo root. Baseline: .archguard-baseline.json.

import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { execSync } from 'node:child_process'

const root = process.cwd()
const args = new Set(process.argv.slice(2))
const cfgPath = join(root, 'archguard.config.json')
if (!existsSync(cfgPath)) {
  console.error('archguard: no archguard.config.json in ' + root)
  process.exit(2)
}
const cfg = JSON.parse(readFileSync(cfgPath, 'utf8'))
const baselinePath = join(root, cfg.baselineFile ?? '.archguard-baseline.json')
const skipDirs = new Set(cfg.skipDirs ?? ['node_modules', '.git', 'dist', 'build', '.next', '__pycache__', '.venv'])

function walk(dir, out) {
  for (const name of readdirSync(dir)) {
    if (skipDirs.has(name)) continue
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) walk(p, out)
    else out.push(relative(root, p).split(sep).join('/'))
  }
  return out
}

// Minimal glob: ** = any depth, * = within one path segment.
function globToRe(g) {
  const re = g
    .replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*\*\//g, '\u0000')
    .replace(/\*\*/g, '.*')
    .replace(/\*/g, '[^/]*')
    .replace(/\u0000/g, '(?:.*/)?')
  return new RegExp('^' + re + '$')
}
const matchesAny = (file, globs) => (globs ?? []).some((g) => globToRe(g).test(file))

const files = walk(root, [])
const counts = {}
const hits = {}

for (const rule of cfg.rules) {
  counts[rule.id] = 0
  hits[rule.id] = []
  const re = new RegExp(rule.pattern, 'g')
  const next = rule.unlessNextLines ? new RegExp(rule.unlessNextLines.pattern, "i") : null
  const prev = rule.unlessPrevLines ? new RegExp(rule.unlessPrevLines.pattern, "i") : null
  for (const file of files) {
    if (!matchesAny(file, rule.include)) continue
    if (matchesAny(file, rule.exclude)) continue
    const lines = readFileSync(join(root, file), 'utf8').split('\n')
    if (rule.perFile) {
      // Count the file once if any line matches.
      const i = lines.findIndex((l) => new RegExp(rule.pattern).test(l))
      if (i >= 0) {
        counts[rule.id] += 1
        hits[rule.id].push(`${file}:${i + 1}`)
      }
      continue
    }
    lines.forEach((line, i) => {
      // Comments describe code; they are not code. Skip whole-line comments.
      if (rule.countComments !== true && /^\s*(\/\/|\/\*|\*|#)/.test(line)) return
      if (rule.skipLines && new RegExp(rule.skipLines).test(line)) return
      re.lastIndex = 0
      const n = (line.match(re) ?? []).length
      if (!n) return
      if (next) {
        const ahead = lines.slice(i, i + 1 + rule.unlessNextLines.lines).join('\n')
        if (next.test(ahead)) return
      }
      if (prev) {
        const behind = lines.slice(Math.max(0, i - rule.unlessPrevLines.lines), i + 1).join('\n')
        if (prev.test(behind)) return
      }
      counts[rule.id] += n
      hits[rule.id].push(`${file}:${i + 1}: ${line.trim().slice(0, 120)}`)
    })
  }
}

if (args.has('--report')) {
  for (const rule of cfg.rules) {
    console.log(`\n## ${rule.id} (${counts[rule.id]}) ${rule.title}`)
    for (const h of hits[rule.id]) console.log('  ' + h)
  }
  process.exit(0)
}

if (args.has('--init-baseline')) {
  writeFileSync(baselinePath, JSON.stringify(counts, null, 2) + '\n')
  console.log('archguard: baseline written', counts)
  process.exit(0)
}

function changedFiles() {
  try {
    const out = execSync('git diff --name-only HEAD && git ls-files --others --exclude-standard', {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    })
    return new Set(out.split('\n').filter(Boolean))
  } catch {
    return new Set()
  }
}

const baseline = existsSync(baselinePath) ? JSON.parse(readFileSync(baselinePath, 'utf8')) : {}
let failed = false
const lowered = { ...baseline }
for (const rule of cfg.rules) {
  const now = counts[rule.id]
  const was = baseline[rule.id] ?? 0
  if (now > was) {
    failed = true
    console.error(`\narchguard FAIL ${rule.id}: ${now} (baseline ${was}). ${rule.title}`)
    console.error(`  Fix: ${rule.fix}`)
    // Show hits in files changed since HEAD first: that is where the new one almost always is.
    const changed = changedFiles()
    const fresh = hits[rule.id].filter((h) => changed.has(h.slice(0, h.indexOf(':'))))
    const shown = fresh.length ? fresh : hits[rule.id]
    if (fresh.length) console.error('  In files you changed:')
    for (const h of shown.slice(0, 10)) console.error('  ' + h)
    if (shown.length > 10) console.error(`  … ${shown.length - 10} more (run with --report)`)
  } else if (now < was) {
    lowered[rule.id] = now
    console.log(`archguard: ${rule.id} improved ${was} → ${now}`)
  }
}
if (args.has('--lower-baseline') && !failed) {
  writeFileSync(baselinePath, JSON.stringify(lowered, null, 2) + '\n')
  console.log('archguard: baseline lowered')
}
if (failed) {
  console.error('\nA data shape escaped the domain model. Do not raise the baseline to pass;')
  console.error('move the shape into the domain model, or record a decision that allows it (see CLAUDE.md, "Data law").')
  process.exit(1)
}
console.log('archguard: ok')
