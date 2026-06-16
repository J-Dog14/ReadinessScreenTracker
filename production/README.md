# Dashboard facelift — drop-in files

Refreshed styling for the Readiness Screen Tracker **dashboard** page. No new
data, no new endpoints, no new buttons — purely a visual + layout cleanup of what
was already there. Mirror these into your repo at the same paths:

```
production/static/css/theme.css      →  static/css/theme.css
production/static/css/dashboard.css  →  static/css/dashboard.css
production/static/js/dashboard.js     →  static/js/dashboard.js
production/templates/base.html       →  templates/base.html
production/templates/dashboard.html  →  templates/dashboard.html
```

Every element `id` the JS targets is unchanged, so charts and the athlete search
keep working exactly as before.

## What changed

**Foundations (`theme.css`)**
- Deeper, calmer surface palette (`#0b0f15` app → layered card gradients) and a
  single refined Octane blue accent (`#34a9e6`) with soft tints for focus/hover.
- Sticky, blurred header with a small "O" brand mark and a live status pip.
- Cards: 14px radius, subtle border + shadow, gradient fill, more generous padding.
- New reusable pieces: `.section-head` (eyebrow + label + rule), `.tier-chip`
  legend, `.switch` toggle, `.plot-2`/`.plot-4` plot grids, `.band-pill`.
- Stat boxes get a status-colored left accent and tighter type.

**Dashboard (`dashboard.css` + `dashboard.html`)**
- Page is now grouped into numbered sections — *Composite · Today · Force-plate ·
  Isometric & grip · Power & trends · Flags* — instead of one flat stack.
- The score card finally uses the intended 2-column hero grid (gauge | sub-scores),
  with a clean "Score history" strip below. The old unused `.score-card` grid rule
  was the formatting bug; it's wired up now.
- The wall-of-text scoring-tier note became three compact chips in the control bar.
- Paired charts (CMJ/PPU jump-height + F-v, grip, movement strategy) sit in tidy
  inset frames on a responsive grid.
- Heatmap restyled with rounded, spaced cells + a color legend.
- Isometric "Show historical" checkbox is now a proper toggle switch.

**Charts (`dashboard.js`)**
- Harmonized Plotly grid/axis/hover colors to the new theme; score-history line is
  now a smooth, softly-filled spline. Trace logic and data untouched.

## Preview

`Dashboard.html` (project root) is a standalone render of this redesign with
**representative sample data** so you can see it without the backend running.
