jlcpcb-capabilities
===================

JLCPCB design rules and layer stack-ups for Altium Designer, built from the
capabilities JLCPCB publishes (retrieved 2026-11-24). Every dimension is in
millimetres.

- `altium/design-rules/` holds design rules (`.RUL`). Import them in the PCB
  Rules and Constraints Editor: right-click, then **Import Rules...**.
- `altium/stackups/` holds layer stacks (`.stackup`). Load one in the Layer
  Stack Manager: **File » Load Stackup from file**.
  [`altium/stackups/README.md`](altium/stackups/README.md) lists them all.

Everything under `altium/` is generated. Edit the data files instead and
regenerate (see [Regenerating](#regenerating)).

Design rules
------------

### Choosing a rule file

Rigid rule files are named `<layers>layer-<outer Cu>[-<inner Cu>]-<tier>.RUL`.
For example, `4layer-1oz-0.5oz-standard.RUL` is a 4-layer board with 1 oz outer
copper, 0.5 oz inner copper (JLCPCB's default) and standard vias.

Flex rule files are named `flex-<layers>layer-<Cu>-<tier>.RUL`.

Each board type comes in two via tiers:

| Tier | Rigid min via (hole / diameter) | Rigid preferred via | Flex min via | Cost |
|---|---|---|---|---|
| `standard` | 0.20 / 0.45 mm | 0.30 / 0.50 mm | 0.30 / 0.55 mm | No via surcharge |
| `advanced` | 0.15 / 0.25 mm | 0.20 / 0.35 mm | 2-layer 0.10 / 0.30 mm, 4-layer 0.15 / 0.35 mm | JLCPCB charges extra |

On rigid boards JLCPCB charges extra for any via with a 0.15 mm hole, or a
0.20–0.25 mm hole with a pad under 0.45 mm. That is why the standard tier stops
at 0.20 / 0.45 mm. The rules use the smallest via as the minimum and the
preferred via as the router default. The `HoleSize` minimum follows the tier,
so a 0.15 mm drill anywhere on a standard-tier board fails DRC.

1-layer flex has no vias, so it only has a `standard` file.

### Rigid FR-4 values

| | 2-layer 1 oz | 2-layer 2 oz | Multilayer ≤1 oz | Multilayer 2 oz |
|---|---|---|---|---|
| Min track width / spacing | 0.10 mm | 0.16 mm | 0.09 mm | 0.15 mm |
| PTH annular ring (absolute min) | 0.18 mm | 0.254 mm | 0.15 mm | 0.254 mm |
| PTH hole to copper | 0.28 mm | 0.28 mm | 0.30 mm | 0.30 mm |
| Min soldermask bridge | 0.10 mm | 0.20 mm | 0.10 mm | 0.20 mm |

Multilayer files set track width and spacing per layer, so a 2 oz outer / 0.5 oz
inner board gets 0.15 mm on the outer layers and 0.09 mm inside. If any layer is
2 oz, the PTH annular ring and soldermask bridge use the 2 oz values.

These values are the same for every rigid file:

| Rule | Value |
|---|---|
| Pad to track | 0.10 mm (raised to the track spacing on 2 oz layers) |
| SMD pad to SMD pad (different nets) | 0.15 mm |
| Via hole / NPTH to copper | 0.20 mm |
| Via annular ring | 0.05 mm (via diameter ≥ hole + 0.1 mm) |
| Via hole to via hole | 0.20 mm |
| Pad hole to any hole | 0.45 mm |
| Drill range | tier minimum – 6.3 mm |
| Copper to board edge | 0.20 mm (routed; V-cut needs 0.4 mm) |
| Soldermask expansion | 0 mm (JLCPCB supports 1:1 openings since June 2025) |
| Silkscreen to pad | 0.15 mm |
| Plane clearance | 0.20 mm for vias; pads use the PTH hole-to-copper value above |

### Flex values

| | 1/3 oz (12 µm) | 0.5 oz (18 µm) | 1 oz (35 µm) |
|---|---|---|---|
| Min track width / spacing | 0.08 mm | 0.09 mm | 0.10 mm |

| Rule | Value |
|---|---|
| Exposed pad to track | 0.15 mm |
| Via to track | 0.10 mm |
| Hole to copper | 0.20 mm |
| PTH annular ring | 0.18 mm (0.25 mm recommended) |
| Via annular ring | 0.10 mm (via diameter ≥ hole + 0.2 mm) |
| Drill range | 0.10 – 6.5 mm |
| Copper to board edge | 0.30 mm |
| Coverlay expansion | 0.10 mm (written as soldermask expansion) |
| Min coverlay bridge | 0.50 mm (written as soldermask sliver) |
| Silkscreen to pad | 0.15 mm |

Flex hole-to-hole spacing is not published separately. JLCPCB says flex
follows the rigid rules, so the files use the rigid values.

### Pads and pours

All rule files include a `PowerPads` pad class rule. Pads in that class connect
directly to polygon pours and planes. Other pads use thermal reliefs (0.25 mm
spokes, 0.25 mm gap), and vias connect directly.

### Not covered by the rules

Altium can't express these, or JLCPCB attaches conditions to them. Check them by
hand:

- **Smaller vias.** 0.10 / 0.20 mm vias are possible on boards ≤1.0 mm thick with
  ENIG or OSP. No tier uses them.
- **Black and white soldermask.** These need a 0.13 mm bridge at 1 oz, not 0.10 mm.
- **NPTH and slots.** NPTH holes must be at least 0.50 mm. Draw them on a mechanical
  or keep-out layer, not as pads. Drawn as pads with no copper, they fail the
  annular ring rule. Minimum plated slot width is 0.50 mm on 2-layer and 0.35 mm
  on multilayer boards. Minimum non-plated slot width is 1.0 mm.
- **BGA.** The minimum BGA pad is 0.20 mm, and pads under 0.25 mm need ENIG.
  Pad-to-track clearance can drop to 0.09 mm locally on multilayer boards.
  The rules keep the general 0.10 mm.
- **Silkscreen.** Line width must be at least 0.15 mm and text height at least 1.0 mm.
- **Castellated holes.** Hole at least 0.5 mm, at least 1 mm from the board edge
  and 0.5 mm from the next hole. The board must be at least 10 × 10 mm and 0.6 mm thick.
- **Heavy copper.** 2-layer boards are also available with 2.5, 3.5 and 4.5 oz
  copper (0.20, 0.25 and 0.30 mm track and space). This repo has no rule files
  for these.

Stack-ups
---------

There are 632 stack-ups. [`altium/stackups/README.md`](altium/stackups/README.md)
lists each one with its JLCPCB template name, thicknesses and copper weights.

- **Rigid 4, 6 and 8 layer** (584 files). These are every template JLCPCB's
  impedance calculator offers at the standard thicknesses (0.4–2.0 mm), with
  1 or 2 oz outer copper and 0.5, 1 or 2 oz inner copper. The prepreg and core
  thicknesses and dielectric constants are JLCPCB's figures. Templates JLCPCB
  labels "general" are their standard builds; "special" ones are alternatives.
  Name the template (e.g. `JLC04161H-7628`) when you order impedance control.
- **Rigid 2 layer** (14 files). JLCPCB's calculator data for 2-layer boards uses
  a placeholder 1.5 mm core for most thicknesses. Instead, these files use a
  single FR-4 core (εr 4.5) with core thickness = board thickness − copper.
- **Flex 2 and 4 layer** (31 files). These are from JLCPCB's flex calculator
  data. Builds that differ only in copper foil type (rolled-annealed,
  high-elongation) are merged into one file. The outer coverlay is the Top and
  Bottom Solder layer; inner coverlay and bonding adhesive are dielectrics.
- **Flex 1 layer** (3 files). These are built from the flex capability page:
  coverlay, copper and polyimide. Altium needs a Bottom Layer, so these files
  have an unused placeholder Bottom Layer. Do not route on it.

All stack-ups use soldermask εr 3.8 and 0.0152 mm thickness over copper
(JLCPCB's impedance figures).

The flex capability page gives coverlay εr as 2.9, but JLCPCB's calculator data
uses 3.9. The flex stack-ups use the calculator value. JLCPCB doesn't offer
impedance control on flex.

Regenerating
------------

The data files are the source of truth:

- `data/capabilities.toml` holds the capability values. They are entered by hand
  from JLCPCB's pages, with a comment on each giving its source.
- `data/stackups.json` holds the stack-ups, normalised from JLCPCB's impedance
  calculator API by `tools/fetch_stackups.py`.

```sh
python3 tools/fetch_stackups.py   # refresh data/stackups.json from jlcpcb.com
python3 tools/generate.py         # rebuild everything under altium/
python3 tools/check.py            # check the generated files
```

Python 3.11 or newer is needed. There are no other dependencies.

`tools/check.py` checks the generated files:

- Every rule has the keys Altium wrote for that rule kind in the original
  Altium-exported files (commit `668069d`), and no keys Altium doesn't use.
- No values are in mils.
- Via sizes satisfy the annular ring rules.
- Each tier's via minimums match `capabilities.toml`.
- Each stack-up has the right copper layer count and Altium layer names.

These checks don't replace importing a file into Altium.

References
----------

- Rigid PCB capabilities: https://jlcpcb.com/capabilities/pcb-capabilities
- Flex PCB capabilities: https://jlcpcb.com/capabilities/flex-pcb-capabilities
- Impedance stack-ups: https://jlcpcb.com/impedance
- Impedance calculator: https://jlcpcb.com/pcb-impedance-calculator
