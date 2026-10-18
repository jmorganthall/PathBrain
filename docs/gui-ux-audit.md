# GUI / UX audit — read as a first-time user

**Scope.** Every view in `frontend/src`, read as someone who knows what PathBrain is for
(rank firewall shaper profiles by how fast the internet feels, and keep the firewall on the
best one) but has never opened it. Presentation only: nothing here changes an API call, a
stored value or a computation. The changes described under *Done* are in this branch; the
ones under *Still to do* are recommendations that need either a design decision or a
backend change and were deliberately left alone.

## The three problems that span every page

1. **No shared vocabulary, no place to learn it.** The app has a working language —
   profile, crown, ring, belt, heir, weather, pipeline, lever, pooled, fused — and no page
   defines any of it. The nav offered "Dueling Champions", "Settings Impact", "Shotgun
   Sweep" and "Overall" as four unrelated nouns. *Done:* a glossary (`utils/glossary.ts`,
   20 terms, one or two plain sentences each) behind a **?** button in the top bar,
   searchable, each entry linking to the page where the term lives; a `<Term>` component
   for inline use. The top-bar switch is now **Auto-apply best** (it was "Follow best",
   which never said what was being followed).
2. **Twenty pages in one flat list, named after their engines.** *Done:* the drawer is
   grouped — Dashboard · **Results** (Best profile, Profile standings, Run history, Trends,
   Compare runs, Weather) · **Find a better profile** (Duels, Lever duels, Explore, Shotgun
   sweep, Experiments, AI suggestions) · **Other tests** (No-shaper baseline, Away test) ·
   **System** (Firewall, Configuration, Methodology, Plugins, Data export). Every item has
   a one-line "what you'll find here" hover. Routes are unchanged; page titles were renamed
   to match the nav where they differed (Settings Impact → Profile standings, Overall →
   Best profile, Dueling Champions → Duels, Data Dump → Data export, Historical Trends →
   Trends, Compare Runs → Compare runs, Test baseline behavior → No-shaper baseline, AI →
   AI suggestions).
3. **Nine different page headers.** h4 on most pages, h5 on some, an icon on a few, the
   lead sentence as a caption here and a three-sentence paragraph there, actions beside the
   title or under it. *Done:* one `PageHeader` (title · one plain lead sentence · the old
   paragraph folded behind "More" · the page's actions on the right, wrapping on a phone)
   on all 22 pages, plus a `SectionLabel` for long pages.

## Page by page

### Dashboard (`/`)
*Found:* the answer (VerdictCard) leads, which is right, but the tagline read "live stance
of the tuner", seven KPI tiles carried captions like "nothing holds the benchmark lock" and
"policy fused · reign 3d", and three further verdict cards ("The two crowns", "Top
profiles by Overall", "In the ring") competed with the answer above them with no label
saying how they relate. *Done:* plain tile captions and help text; the **Follow best** tile
is **Auto-apply**; four section labels (Status · Scores, the firewall now · Where the answer
comes from · Recent measurements) so the long page scans; card titles say what they rank
("Standings · all measurements pooled", "Standings · head-to-head duels"); "The two crowns"
is "Two ways of naming a winner". *Still to do:* the two standings cards and TwoCrowns
could collapse into one card with a pooled/head-to-head toggle, since the VerdictCard
already combines them.

### Profile standings (`/settings`, was "Settings Impact")
*Found:* the title described only a conditional banner; the two header buttons were
data-repair tools ("Attribute unstamped runs", "Merge SQM-off profiles"); the table — the
page's reason to exist — sat under up to ten stacked alerts, pointer cards and the
scatter; filters were four checkboxes stacked vertically; every row printed the raw
fingerprint hash under the name; "Race challengers" appeared three times with two
different labels; alert titles read "The crown's bar may be a ghost". *Done:* renamed;
**Race challengers** (with the contender-count badge) is the one header action; order is
now table → scatter → contenders → what the best profile changed → related links → a
folded **Data health** card that holds the two repair buttons; filters are one wrapped row
(including the methodology checkbox that used to sit alone under the title); fingerprint
moved into the label's hover; action labels unified (Test / Apply / Open profile); alert
titles rewritten in plain words; the "#" standing column sorts ascending on first click and
turning the weather lens off returns to the default order (both were presentation bugs:
clicking the rank header showed the worst profile first). *Still to do:* the table still
shows ~14 columns by default; hiding "Overall (100)", "Overall IQR" and "Min–Max" behind
Columns is the next cut.

### Duels (`/duels`, was "Dueling Champions")
*Found:* the champion — the first thing a newcomer wants — sat below a health alert, a
blurb, a statistical "What this ladder can settle" card and the rules accordion. The
verdict on "best" appeared in six places under different nouns. The tape's legend said
green = holder, amber = challenger while the bars drew the holder in blue and the
challenger in green. A "Duel now until" time field in the rules did nothing — the Duel now
dialog asks for a duration — yet the page's lead sentence quoted it. Fourteen standings
columns. *Done:* champion card first; "What a duel can settle" folded with its number in
the summary and a plain button label; "Rules of the ring" → **Duel settings**, moved under
the standings; the dead time field removed and the lead sentence says what the button does;
legend fixed to the bars' real colours (and the verdict chip now uses the same two
colours); "Who's fighting" is a folded card that opens when the line-up is fetched;
"Reigning duel champion" → "Current duel champion", "(holder)" → "(defending)", "Crowning
policy: …" chips → "Auto-apply follows …". *Still to do:* hide half the standings columns
behind a toggle; fold ThreeClaims into the champion card as one line when the verdicts
agree.

### Best profile (`/overall`, was "Overall")
*Found:* seven open cards, three of them diagnostics ("What went in", "What the ring
changed", "Which verdict predicts the next night?") placed between the answer and the
table; the answer card said "Run this profile" with no way to get to it; chips read
"+0.41 over X · noise ±0.30". *Done:* order is answer → chart → every profile → do the
records agree → (folded) what the duels changed → (folded) what went in → (folded)
predictive check → how it is built; an **Open profile** button on the answer; chips read
"leads X by +0.41 (noise ±0.30)" and "~N more head-to-head rounds to confirm"; tiles are
"Long-run average / Head to head / Combined (used)"; the "Pull" column is "Shift".

### Explore (`/explore`)
*Found:* the only first-visit action ("Map the space") was an outlined button; every
candidate, crown-leg move and coverage gap carried two buttons ("Test now (5)" + "Test to
minimum"), dozens open by default; the recommendation ledger ("Was the data right?")
rendered third, open, although the code's own comment says it belongs at the bottom; a
warning pointed at "coverage gaps" while the section is "Holes in coverage". *Done:*
"Map the space" is the contained button until the map exists; one contained **Quick test**
per row with **Full test** as a text button; "Leaders per crown leg" is a folded "Where the
best profile is weakest" with a one-line summary; the ledger is folded, renamed "How past
suggestions turned out" and closes the page; "Local optima" → "Dead ends — profiles no
single change improves"; cross-references fixed; a duplicate caption and a footnote that
referred to a chart in another card removed.

### Profile detail (`/profiles/:fp`)
*Found:* "In the ring" (four tiles, up to 40 opponent rows, an eight-row bout table) was
open and sat above the score chart and run history; the maintenance "Data integrity" card
came before them too; the raw fingerprint hash was printed under the name; "Back to
Settings Impact"; "Test this profile (5)" with an unexplained "(5)". *Done:* the head-to-
head record is a folded card (open when this profile is champion) with its rank and record
in the summary; scores over time moved up under Standings; Data integrity moved to the
bottom; hash moved to a hover; labels are "All profiles", "Quick test (5)", "Test until
confident", "Apply to firewall", "Open duels"; the apply dialog names the profile by its
call sign.

### Lever duels (`/levers`)
*Found:* a three-sentence jargon paragraph always visible; the main card had no title; with
no campaign the first control met was a disabled Start, while the control that unblocks it
("New campaign on…") was last; the preview card was open but empty until pressed. *Done:*
PageHeader with a one-sentence lead; the card is titled "Run a lever session" / "Live
session"; the new-campaign row comes first as "Step 1", the run row as "Step 2"; the
preview is closed by default and named "Preview the next session's matchups"; "Full board"
→ "Open duels".

### Weather (`/weather`)
*Found:* the verdict read "Weather moves the crown: 3 clean covariate↔metric pair(s) over
|ρ| 0.3". *Done:* verdict sentences in plain words from the same fields; card and column
titles renamed ("Which conditions affect which scores", "Effect (same profile)", "Effect
(all runs)"); the lead links to the per-profile readings on Profile standings.

### Smaller pages
- **Run history:** lead sentence; legacy toggle says "Show legacy runs"; the hidden-count
  caption explains that page counts include them.
- **Run detail:** "Back to History" → "All runs"; metric rows show the metric's label with
  the raw key in the tooltip (they showed the key); "SOPS" → "Overall" (SOPS is the retired
  score's name).
- **Trends, Compare runs, Experiments, Shotgun sweep, Plugins, Data export, Firewall:**
  PageHeader with a lead; "SOPS" → "Overall" in Experiments and Shotgun sweep; Compare no
  longer prints "(lower better)" on every metric (it was wrong for higher-is-better ones);
  Experiments says "Configured to sweep" when disarmed; plugin names are capitalised; Data
  export's shared error alert moved under the header and the two identical "Generate"
  buttons are named; Firewall shows the write ledger first, then the instruments.
- **AI suggestions:** suggestions render before the three analysis cards; the "View the
  run" link pointed at a route that does not exist (`/results/…`) and now opens the run;
  "Test to minimum" → "Trial (restores after)".
- **Away test:** PageHeader (it was the only h5 with an icon); the odd quoted opening line
  replaced; the detection caption no longer sends people to a Config field that does not
  exist; the location map sits below this device's own runs.
- **No-shaper baseline:** "Run now" comes before the nightly schedule; the lead says the
  link runs unshaped during the test; links say "Profile standings".
- **Configuration:** the Save/Reset bar is sticky; the viewport fields are disabled when
  the methodology owns the browser client like their siblings; "SOPS" → "Overall".
- **Methodology:** PageHeader; the two history-wide buttons have shorter labels.

## Still to do (needs a decision or a backend change)
- **Standings column budget** on Profile standings and Duels: pick five default columns
  and put the rest behind the existing Columns menu.
- **Compare runs** marks every non-zero difference as improved/regressed with no noise
  threshold, and treats every metric as lower-is-better. The label is gone; the verdict
  chip is a computation and was left.
- **Overall table caption** hard-codes "under 8 rounds"; the server exposes
  `min_iterations` but not the round minimum.
- **Plugins** shows a disabled "Test connection" and an empty history when Watchtower is
  not configured; hiding them needs the component's state read.
- **Duels health banner** and **Profile standings seed alert** are still long; both carry
  server sentences that would need rewording on the backend.
- **Dashboard**: collapse the three verdict cards into one with a toggle.
