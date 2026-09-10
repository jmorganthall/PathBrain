## What changed, and what it replaced

<!-- The rule or behaviour that stood before, why it failed, and what now stands in its
     place. A change to a decision is only reviewable against the decision it overturns —
     and it is what makes CLAUDE.md readable a year later. -->

## The evidence

<!-- Any number claimed above — a threshold, a timing, a calibration, a false-verdict rate
     — and how it was measured. Measured, not asserted. -->

## Follow-through

<!-- Delete what doesn't apply. These are silent when skipped: nothing fails, history just
     goes stale or a metric quietly out-ranks real measurements. -->

- [ ] Published a new methodology → re-graded history (Methodology page → "Re-grade history under current")
- [ ] Changed a derivation formula → re-derived from raw **first**, then re-graded
- [ ] Added a crown/required metric → its derive function omits on absent input, never defaults to a sentinel
- [ ] Added a shaper field or metric → one entry in the registry (`shaper_fields.py` / `metrics.py`); no call site re-lists it

## Firewall interaction

<!-- REQUIRED when the diff touches anything that can write the firewall (providers/,
     session_runtime.py, firewall_guard.py, any engine's apply path, routes_settings /
     routes_config / routes_sweep). CI (firewall-gate) refuses the PR without it. State,
     measured with tests/faults.py's FaultyProvider, not asserted:
     - Writes per session before / after: how many setPipe calls and how many reconfigures
       a leg, a profile switch, and a restore cost, before this change and after it.
     - On a timeout: what happens when a write does not answer. (The answer must never be
       "it is reissued".)
     - On a reboot: what happens when the firewall goes away mid-session and comes back. -->

- Writes per session before / after:
- On a timeout:
- On a reboot:

## Pinned by

<!-- The test that fails if this regresses. Prefer an executable invariant over a comment. -->

- [ ] `cd backend && python -m pytest` passes
- [ ] `cd frontend && npm run build` passes
