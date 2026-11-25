#!/usr/bin/env python3
"""Generate Altium Designer design rules (.RUL) and layer stacks (.stackup).

Inputs:
    data/capabilities.toml   JLCPCB capability values (hand-maintained)
    data/stackups.json       JLCPCB published stack-ups (tools/fetch_stackups.py)

Outputs (all regenerated from scratch):
    altium/design-rules/rigid/*.RUL
    altium/design-rules/flex/*.RUL
    altium/stackups/rigid/<n>-layer/*.stackup
    altium/stackups/flex/<n>-layer/*.stackup
    altium/stackups/README.md   (index of the stack-ups)

All dimensions are written in millimetres.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAPS = tomllib.loads((ROOT / "data" / "capabilities.toml").read_text())
STACKUPS = json.loads((ROOT / "data" / "stackups.json").read_text())
OUT_RULES = ROOT / "altium" / "design-rules"
OUT_STACKUPS = ROOT / "altium" / "stackups"

OZ_TO_MM = {0.33: 0.012, 0.5: 0.0175, 1: 0.035, 2: 0.07}  # finished copper thickness
MAX_MIDLAYERS = 30  # Altium rule files carry per-layer values for Mid Layer 1..30

# Routing preferences. These are not JLCPCB limits, just sensible defaults for
# the interactive router that sit above the minimums.
PREFERRED_WIDTH = 0.20
PREFERRED_DIFF_GAP = 0.20
MAX_WIDTH = 10.0
MAX_DIFF_GAP = 1.0
RELIEF_WIDTH = 0.25
RELIEF_GAP = 0.25


def mm(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{text}mm"


def oz(value: float) -> str:
    return f"{value:g}oz"


def internal_units(value_mm: float) -> int:
    """Altium clearance-matrix units: 1/10000 mil."""
    return round(value_mm / 0.0254 * 10000)


def short_id(*parts: str) -> str:
    digest = hashlib.sha1("/".join(parts).encode()).digest()
    return "".join(chr(ord("A") + b % 26) for b in digest[:8])


def guid(*parts: str) -> str:
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "jlcpcb-capabilities/" + "/".join(parts))).upper() + "}"


def caps_value(table: dict, copper: float) -> float:
    return table[f"{copper:g}"]


# ---------------------------------------------------------------------------
# Design rules
# ---------------------------------------------------------------------------

@dataclass
class RuleSet:
    """Everything a .RUL file needs, already resolved to numbers."""
    file: Path
    title: str
    layers: int
    outer_gap: float              # min track width/space on outer layers
    inner_gap: float              # min track width/space on inner layers
    pad_gap: float                # pad to track
    smd_pad_gap: float            # SMD pad to SMD pad
    via_gap: float                # via copper to other copper
    hole_gap: float               # via/NPTH hole to copper
    pth_hole_gap: float           # plated through-hole (component pin) hole to copper
    via_hole_to_hole: float
    pad_hole_to_hole: float
    min_hole: float
    max_hole: float
    via_min_hole: float
    via_min_diameter: float
    via_hole: float
    via_diameter: float
    via_ring: float
    pad_ring: float
    plane_clearance_via: float
    plane_clearance_pad: float
    mask_expansion: float
    mask_sliver: float
    silk_to_pad: float
    board_edge: float
    source: str


class RuleWriter:
    def __init__(self, rs: RuleSet):
        self.rs = rs
        self.records: list[str] = []
        self.priorities: dict[str, int] = {}

    def add(self, kind: str, name: str, fields: list[tuple[str, str]], scope1: str = "All",
            scope2: str = "All", netscope: str = "AnyNet", comment: str = "") -> None:
        priority = self.priorities.get(kind, 0) + 1
        self.priorities[kind] = priority
        header = [
            ("SELECTION", "FALSE"), ("LAYER", "TOP"), ("LOCKED", "FALSE"), ("POLYGONOUTLINE", "FALSE"),
            ("USERROUTED", "TRUE"), ("KEEPOUT", "FALSE"), ("UNIONINDEX", "0"), ("RULEKIND", kind),
            ("NETSCOPE", netscope), ("LAYERKIND", "SameLayer"), ("SCOPE1EXPRESSION", scope1),
            ("SCOPE2EXPRESSION", scope2), ("NAME", name), ("ENABLED", "TRUE"), ("PRIORITY", str(priority)),
            ("COMMENT", comment or " "), ("UNIQUEID", short_id(self.rs.file.name, name)),
            ("DEFINEDBYLOGICALDOCUMENT", "FALSE"),
        ]
        for key, value in header + fields:
            assert "|" not in value and "\xb6" not in value, (key, value)
            value.encode("latin-1")
        self.records.append("|".join(f"{k}={v}" for k, v in header + fields))

    def text(self) -> bytes:
        # Altium separates records with a pilcrow (0xB6) followed by CRLF.
        return "\xb6\r\n".join(self.records).encode("latin-1") + b"\xb6\r\n"


def mid_layers(layers: int) -> list[str]:
    return [f"Mid Layer {i}" for i in range(1, layers - 1)]


def clearance_matrix(generic: float, pad: float, smd_pad: float, via: float, hole: float) -> str:
    """OBJECTCLEARANCES entries that differ from the generic clearance."""
    copper = ["Arc", "Track", "SMDPad", "THPad", "Via", "Fill", "Poly", "Region", "Text"]
    values: dict[tuple[str, str], float] = {}
    for i, a in enumerate(copper):
        for b in copper[i:]:
            gap = generic
            if a == "Via" or b == "Via":
                gap = max(gap, via)
            if a in ("SMDPad", "THPad") or b in ("SMDPad", "THPad"):
                gap = max(gap, pad)
            if a == "SMDPad" and b == "SMDPad":
                gap = max(gap, smd_pad)
            values[(a, b)] = gap
        values[(a, "Hole")] = hole
    entries = [f"ClearanceObj_{a}-ClearanceObj_{b}:{internal_units(v)}"
               for (a, b), v in values.items() if abs(v - generic) > 1e-9]
    return ";".join(entries) if entries else " "


def clearance_fields(generic: float, rs: RuleSet, hole: float) -> list[tuple[str, str]]:
    return [
        ("GAP", mm(generic)), ("GENERICCLEARANCE", mm(generic)),
        ("IGNOREPADTOPADCLEARANCEINFOOTPRINT", "FALSE"),
        ("OBJECTCLEARANCES", clearance_matrix(generic, rs.pad_gap, rs.smd_pad_gap, rs.via_gap, hole)),
    ]


def per_layer(rs: RuleSet, suffixes: dict[str, tuple[float, float]]) -> list[tuple[str, str]]:
    """Per-layer fields: suffix -> (outer value, inner value). Unused mid layers get inner values."""
    fields = []
    for prefix, outer in [("TOPLAYER", True)] + [(f"MIDLAYER{i}", False) for i in range(1, MAX_MIDLAYERS + 1)] \
            + [("BOTTOMLAYER", True)]:
        for suffix, (o, i) in suffixes.items():
            fields.append((f"{prefix}_{suffix}", mm(o if outer else i)))
    return fields


def write_rules(rs: RuleSet) -> None:
    w = RuleWriter(rs)
    src = rs.source
    outer_layers = "OnLayer('Top Layer') Or OnLayer('Bottom Layer')"
    max_gap = max(rs.outer_gap, rs.inner_gap)

    # Clearance: plated through-hole pins first, then outer layers, then everything else.
    w.add("Clearance", "Clearance_ThruPinHoles",
          clearance_fields(max(max_gap, rs.pad_gap), rs, rs.pth_hole_gap),
          scope1="IsThruPin", netscope="DifferentNets",
          comment=f"PTH hole to copper {mm(rs.pth_hole_gap)} ({src})")
    if rs.layers > 2 and abs(rs.outer_gap - rs.inner_gap) > 1e-9:
        w.add("Clearance", "Clearance_OuterLayers", clearance_fields(rs.outer_gap, rs, rs.hole_gap),
              scope1=outer_layers, netscope="DifferentNets",
              comment=f"Outer layer track spacing {mm(rs.outer_gap)} ({src})")
    w.add("Clearance", "Clearance", clearance_fields(rs.inner_gap if rs.layers > 2 else rs.outer_gap, rs, rs.hole_gap),
          netscope="DifferentNets",
          comment=f"Track spacing, pad to track >= {mm(rs.pad_gap)}, SMD pad to pad >= {mm(rs.smd_pad_gap)}, "
                  f"via/NPTH hole {mm(rs.hole_gap)} ({src})")

    w.add("Width", "Width", [
        ("MAXLIMIT", mm(MAX_WIDTH)), ("MINLIMIT", mm(rs.outer_gap)),
        ("PREFEREDWIDTH", mm(max(PREFERRED_WIDTH, rs.outer_gap))),
        *per_layer(rs, {
            "MINWIDTH": (rs.outer_gap, rs.inner_gap),
            "MAXWIDTH": (MAX_WIDTH, MAX_WIDTH),
            "PREFWIDTH": (max(PREFERRED_WIDTH, rs.outer_gap), max(PREFERRED_WIDTH, rs.inner_gap)),
        }),
        ("MINIMP", "50.000000"), ("MAXIMP", "50.000000"), ("FAVIMP", "50.000000"),
    ], comment=f"Min track width outer {mm(rs.outer_gap)}, inner {mm(rs.inner_gap)} ({src})")

    w.add("PlaneConnect", "PlaneConnectPowerPads", [
        ("PLANECONNECTSTYLE", "Direct"), ("RELIEFEXPANSION", mm(rs.plane_clearance_pad)),
        ("RELIEFENTRIES", "4"), ("RELIEFCONDUCTORWIDTH", mm(RELIEF_WIDTH)), ("RELIEFAIRGAP", mm(RELIEF_GAP)),
    ], scope1="InPadClass('PowerPads')")
    w.add("PlaneConnect", "PlaneConnect", [
        ("PAD.PLANECONNECTSTYLE", "Relief"), ("PAD.RELIEFEXPANSION", mm(rs.plane_clearance_pad)),
        ("PAD.RELIEFENTRIES", "4"), ("PAD.RELIEFCONDUCTORWIDTH", mm(RELIEF_WIDTH)),
        ("PAD.RELIEFAIRGAP", mm(RELIEF_GAP)),
        ("VIA.PLANECONNECTSTYLE", "Direct"), ("VIA.RELIEFEXPANSION", mm(rs.plane_clearance_via)),
        ("VIA.RELIEFENTRIES", "4"), ("VIA.RELIEFCONDUCTORWIDTH", mm(RELIEF_WIDTH)),
        ("VIA.RELIEFAIRGAP", mm(RELIEF_GAP)),
    ])

    w.add("RoutingTopology", "RoutingTopology", [("TOPOLOGY", "Shortest")])
    w.add("RoutingPriority", "RoutingPriority", [("ROUTINGPRIORITY", "0")])
    w.add("RoutingLayers", "RoutingLayers",
          [("TOP LAYER_V5", "TRUE")] + [(f"MID LAYER {i}_V5", "TRUE") for i in range(1, MAX_MIDLAYERS + 1)]
          + [("BOTTOM LAYER_V5", "TRUE")])
    w.add("RoutingCorners", "RoutingCorners",
          [("CORNERSTYLE", "45-Degree"), ("MINSETBACK", mm(2.54)), ("MAXSETBACK", mm(2.54))])
    w.add("RoutingVias", "RoutingVias", [
        ("HOLEWIDTH", mm(rs.via_hole)), ("WIDTH", mm(rs.via_diameter)), ("VIASTYLE", "Through Hole"),
        ("MINHOLEWIDTH", mm(rs.via_min_hole)), ("MINWIDTH", mm(rs.via_min_diameter)),
        ("MAXHOLEWIDTH", mm(rs.max_hole)), ("MAXWIDTH", mm(rs.max_hole + 2 * rs.pad_ring)),
    ], comment=f"Min via {mm(rs.via_min_hole)}/{mm(rs.via_min_diameter)}, "
               f"preferred {mm(rs.via_hole)}/{mm(rs.via_diameter)} ({src})")

    w.add("PlaneClearance", "PlaneClearanceVias", [("CLEARANCE", mm(rs.plane_clearance_via))], scope1="IsVia",
          comment=f"Inner layer via hole to copper {mm(rs.plane_clearance_via)} ({src})")
    w.add("PlaneClearance", "PlaneClearance", [("CLEARANCE", mm(rs.plane_clearance_pad))],
          comment=f"Inner layer PTH hole to copper {mm(rs.plane_clearance_pad)} ({src})")

    w.add("SolderMaskExpansion", "SolderMaskExpansion", [("EXPANSION", mm(rs.mask_expansion))],
          comment=f"{src}")
    w.add("PasteMaskExpansion", "PasteMaskExpansion", [("EXPANSION", mm(0))])
    w.add("ShortCircuit", "ShortCircuit", [("ALLOWED", "FALSE")])
    w.add("UnRoutedNet", "UnRoutedNet", [])

    w.add("MinimumAnnularRing", "MinimumAnnularRingVias", [("MINIMUMRING", mm(rs.via_ring))], scope1="IsVia",
          comment=f"Via diameter >= hole + {mm(2 * rs.via_ring)} ({src})")
    w.add("MinimumAnnularRing", "MinimumAnnularRing", [("MINIMUMRING", mm(rs.pad_ring))],
          comment=f"PTH annular ring absolute minimum {mm(rs.pad_ring)} ({src})")

    w.add("PolygonConnect", "PolygonConnectPowerPads", [
        ("CONNECTSTYLE", "Direct"), ("RELIEFCONDUCTORWIDTH", mm(RELIEF_WIDTH)), ("RELIEFENTRIES", "4"),
        ("POLYGONRELIEFANGLE", "90 Angle"), ("AIRGAPWIDTH", mm(RELIEF_GAP)),
    ], scope2="(InPadClass('PowerPads'))")
    poly = []
    for obj, style in (("THPAD", "Relief"), ("SMDPAD", "Relief"), ("VIA", "Direct")):
        poly += [(f"{obj}.CONNECTSTYLE", style), (f"{obj}.RELIEFCONDUCTORWIDTH", mm(RELIEF_WIDTH)),
                 (f"{obj}.RELIEFENTRIES", "4"), (f"{obj}.POLYGONRELIEFANGLE", "90 Angle"),
                 (f"{obj}.AIRGAPWIDTH", mm(RELIEF_GAP))]
    w.add("PolygonConnect", "PolygonConnect", poly)

    w.add("ComponentClearance", "ComponentClearance", [
        ("GAP", mm(0.1)), ("COLLISIONCHECKMODE", "3"), ("VERTICALGAP", mm(0.1)), ("SHOWDISTANCES", "TRUE"),
    ])
    w.add("HoleSize", "HoleSize", [
        ("ABSOLUTEVALUES", "TRUE"), ("MAXLIMIT", mm(rs.max_hole)), ("MINLIMIT", mm(rs.min_hole)),
        ("MAXPERCENT", "80.000"), ("MINPERCENT", "20.000"),
    ], comment=f"Drill {mm(rs.min_hole)} - {mm(rs.max_hole)} ({src})")
    w.add("LayerPairs", "LayerPairs", [("ENFORCE", "TRUE")])
    w.add("Height", "Height", [("MINHEIGHT", mm(0)), ("MAXHEIGHT", mm(25.4)), ("PREFHEIGHT", mm(12.7))])

    w.add("DiffPairsRouting", "DiffPairsRouting", [
        ("MAXLIMIT", mm(MAX_DIFF_GAP)), ("MINLIMIT", mm(rs.outer_gap)),
        ("MOSTFREQGAP", mm(max(PREFERRED_DIFF_GAP, rs.outer_gap))), ("MAXUNCOUPLEDLENGTH", mm(12.7)),
        *per_layer(rs, {
            "MINGAP": (rs.outer_gap, rs.inner_gap),
            "MAXGAP": (MAX_DIFF_GAP, MAX_DIFF_GAP),
            "PREFGAP": (max(PREFERRED_DIFF_GAP, rs.outer_gap), max(PREFERRED_DIFF_GAP, rs.inner_gap)),
            "MINWIDTH": (rs.outer_gap, rs.inner_gap),
            "MAXWIDTH": (MAX_WIDTH, MAX_WIDTH),
            "PREFWIDTH": (max(PREFERRED_WIDTH, rs.outer_gap), max(PREFERRED_WIDTH, rs.inner_gap)),
        }),
    ])

    w.add("HoleToHoleClearance", "HoleToHoleClearancePads",
          [("GAP", mm(rs.pad_hole_to_hole)), ("ALLOWSTACKEDMICROVIAS", "FALSE")], scope1="IsPad",
          comment=f"Pad hole-to-hole {mm(rs.pad_hole_to_hole)} ({src})")
    w.add("HoleToHoleClearance", "HoleToHoleClearance",
          [("GAP", mm(rs.via_hole_to_hole)), ("ALLOWSTACKEDMICROVIAS", "FALSE")],
          comment=f"Via hole-to-hole {mm(rs.via_hole_to_hole)} ({src})")
    w.add("MinimumSolderMaskSliver", "MinimumSolderMaskSliver", [("MINSOLDERMASKWIDTH", mm(rs.mask_sliver))],
          comment=f"Min soldermask bridge {mm(rs.mask_sliver)} ({src})")
    w.add("SilkToSolderMaskClearance", "SilkToSolderMaskClearance", [
        ("MINSILKSCREENTOMASKGAP", mm(rs.silk_to_pad)), ("CLEARANCETOEXPOSEDCOPPER", "TRUE"),
    ], scope1="IsPad", comment=f"Pad to silkscreen {mm(rs.silk_to_pad)} ({src})")
    w.add("SilkToSilkClearance", "SilkToSilkClearance", [("SILKTOSILKCLEARANCE", mm(0))])
    w.add("NetAntennae", "NetAntennae", [("NETANTENNAETOLERANCE", mm(0))])
    w.add("UnpouredPolygon", "UnpouredPolygon", [])
    w.add("BoardOutlineClearance", "BoardOutlineClearance", [
        ("GAP", mm(rs.board_edge)), ("GENERICCLEARANCE", mm(rs.board_edge)),
        ("IGNOREPADTOPADCLEARANCEINFOOTPRINT", "FALSE"), ("OBJECTCLEARANCES", " "),
    ], netscope="DifferentNets", comment=f"Copper to board edge {mm(rs.board_edge)} ({src})")

    rs.file.parent.mkdir(parents=True, exist_ok=True)
    rs.file.write_bytes(w.text())


def rigid_rulesets() -> list[RuleSet]:
    r = CAPS["rigid"]
    src = f"JLCPCB rigid capabilities {CAPS['retrieved']}"
    out = []
    for spec in r["rulesets"]:
        layers = spec["layers"]
        cls = "two_layer" if layers == 2 else "multilayer"
        for outer in spec["outer_oz"]:
            for inner in spec.get("inner_oz", [None]):
                for tier_name, tier in r["via_tiers"].items():
                    ts = r["trace_space"][cls]
                    outer_gap = caps_value(ts, outer)
                    inner_gap = caps_value(ts, inner) if inner is not None else outer_gap
                    heavy = max(outer, inner or 0)
                    copper = oz(outer) + (f"-{oz(inner)}" if inner is not None else "")
                    pth_gap = r["pth_hole_to_copper"][cls]
                    out.append(RuleSet(
                        file=OUT_RULES / "rigid" / f"{layers}layer-{copper}-{tier_name}.RUL",
                        title=f"{layers}-layer {copper} {tier_name}",
                        layers=layers,
                        outer_gap=outer_gap,
                        inner_gap=inner_gap,
                        # Pad/via minimums; each clearance rule raises them to its own track spacing.
                        pad_gap=r["pad_to_track"],
                        smd_pad_gap=r["smd_pad_to_pad"],
                        via_gap=0.0,
                        hole_gap=max(r["via_hole_to_copper"], r["npth_to_copper"]),
                        pth_hole_gap=pth_gap,
                        via_hole_to_hole=r["via_hole_to_hole"],
                        pad_hole_to_hole=r["pad_hole_to_hole"],
                        min_hole=tier["min_hole"],
                        max_hole=r["max_hole"],
                        via_min_hole=tier["min_hole"],
                        via_min_diameter=tier["min_diameter"],
                        via_hole=tier["preferred_hole"],
                        via_diameter=tier["preferred_diameter"],
                        via_ring=r["via_annular_ring"],
                        pad_ring=caps_value(r["pth_annular_ring"][cls], 2 if heavy == 2 else 1),
                        plane_clearance_via=r["via_hole_to_copper"],
                        plane_clearance_pad=pth_gap,
                        mask_expansion=r["soldermask_expansion"],
                        mask_sliver=caps_value(r["soldermask_bridge"], 2 if heavy == 2 else 1),
                        silk_to_pad=r["silk_to_pad"],
                        board_edge=r["board_edge"],
                        source=src,
                    ))
    return out


def flex_rulesets() -> list[RuleSet]:
    f = CAPS["flex"]
    src = f"JLCPCB flex capabilities {CAPS['retrieved']}"
    out = []
    for spec in f["rulesets"]:
        layers = spec["layers"]
        tiers = {"standard": f["via_tiers"]["standard"]}
        if layers > 1:
            tiers["advanced"] = f["via_tiers"]["advanced"][str(layers)]
        for copper in spec["copper_oz"]:
            gap = caps_value(f["trace_space"], copper)
            for tier_name, tier in tiers.items():
                out.append(RuleSet(
                    file=OUT_RULES / "flex" / f"flex-{layers}layer-{oz(copper)}-{tier_name}.RUL",
                    title=f"flex {layers}-layer {oz(copper)} {tier_name}",
                    layers=layers,
                    outer_gap=gap,
                    inner_gap=gap,
                    pad_gap=max(f["pad_to_track"], gap),
                    smd_pad_gap=max(f["pad_to_track"], gap),
                    via_gap=max(f["via_to_track"], gap),
                    hole_gap=f["hole_to_copper"],
                    pth_hole_gap=f["hole_to_copper"],
                    via_hole_to_hole=f["via_hole_to_hole"],
                    pad_hole_to_hole=f["pad_hole_to_hole"],
                    min_hole=f["min_hole"],
                    max_hole=f["max_hole"],
                    via_min_hole=tier["min_hole"],
                    via_min_diameter=tier["min_diameter"],
                    via_hole=tier["preferred_hole"],
                    via_diameter=tier["preferred_diameter"],
                    via_ring=f["via_annular_ring"],
                    pad_ring=f["pth_annular_ring"],
                    plane_clearance_via=f["hole_to_copper"],
                    plane_clearance_pad=f["hole_to_copper"],
                    mask_expansion=f["coverlay_expansion"],
                    mask_sliver=f["coverlay_bridge"],
                    silk_to_pad=f["silk_to_pad"],
                    board_edge=f["board_edge"],
                    source=src,
                ))
    return out


# ---------------------------------------------------------------------------
# Layer stacks
# ---------------------------------------------------------------------------

LAYER_IDS = {
    "Top Paste": 16973832, "Top Overlay": 16973830, "Top Solder": 16973834, "Top Layer": 16777217,
    "Bottom Layer": 16842751, "Bottom Solder": 16973835, "Bottom Overlay": 16973831, "Bottom Paste": 16973833,
}
DIELTYPE = {"core": 1, "prepreg": 2, "adhesive": 2, "coverlay-inner": 2, "soldermask": 3}


def stack_layers(stack: list[dict], mask: tuple[dict, dict]) -> list[tuple[str, int, list[tuple[str, str]]]]:
    """Convert a normalised stack (top to bottom) into Altium layer entries."""
    copper_count = sum(1 for l in stack if l["kind"] == "copper")

    def dielectric(kind, dk, height, material, extra=()):
        return [("DIELTYPE", str(DIELTYPE[kind])), ("DIELCONST", f"{dk:g}"), ("DIELHEIGHT", mm(height)),
                ("DIELMATERIAL", material), *extra]

    top_mask, bottom_mask = mask
    entries = [("Top Paste", LAYER_IDS["Top Paste"], []), ("Top Overlay", LAYER_IDS["Top Overlay"], []),
               ("Top Solder", LAYER_IDS["Top Solder"],
                dielectric("soldermask", top_mask["dk"], top_mask["thickness"], top_mask["material"],
                           [("COVERLAY_EXPANSION", mm(0))]))]
    copper_index = dielectric_index = 0
    for layer in stack:
        if layer["kind"] == "copper":
            copper_index += 1
            if copper_index == 1:
                name, placement = "Top Layer", "1"
            elif copper_index == copper_count:
                name, placement = "Bottom Layer", "2"
            else:
                name, placement = f"Mid Layer {copper_index - 1}", "0"
            layer_id = LAYER_IDS.get(name, LAYER_IDS["Top Layer"] + copper_index - 1)
            entries.append((name, layer_id, [("COPTHICK", mm(layer["thickness"])),
                                             ("COMPONENTPLACEMENT", placement)]))
        else:
            dielectric_index += 1
            entries.append((f"Dielectric {dielectric_index}", 17039360 + dielectric_index,
                            dielectric(layer["kind"], layer["dk"], layer["thickness"], layer["material"])))
    entries += [("Bottom Solder", LAYER_IDS["Bottom Solder"],
                 dielectric("soldermask", bottom_mask["dk"], bottom_mask["thickness"], bottom_mask["material"],
                            [("COVERLAY_EXPANSION", mm(0))])),
                ("Bottom Overlay", LAYER_IDS["Bottom Overlay"], []),
                ("Bottom Paste", LAYER_IDS["Bottom Paste"], [])]
    entries += [(f"Mechanical {i}", 16908288 + i, [("MECHENABLED", "True" if i == 1 else "False")])
                for i in range(1, 33)]
    return entries


def write_stackup(path: Path, name: str, stack: list[dict], mask: tuple[dict, dict]) -> None:
    master, sub = guid(name, "master"), guid(name, "substack")
    fields = [
        ("STACKUPVERSION", "1"), ("LAYERMASTERSTACK_V8ID", master),
        ("LAYERMASTERSTACK_V8NAME", "Master layer stack"), ("LAYERMASTERSTACK_V8ISFLEX", "False"),
        ("LAYERMASTERSTACK_V8SHOWTOPDIELECTRIC", "False"), ("LAYERMASTERSTACK_V8SHOWBOTTOMDIELECTRIC", "False"),
        ("LAYERMASTERSTACK_V8STYLE", "0"), ("DISPLAYUNIT", "0"), ("LAYERMASTERSTACK_V8ID", master),
        ("LAYERMASTERSTACK_VAULT_GUID", ""), ("LAYERMASTERSTACK_REVISION_GUID", ""),
        ("LAYERSUBSTACK_V8_0ID", sub), ("LAYERSUBSTACK_V8_0NAME", name),
        ("LAYERSUBSTACK_V8_0ISFLEX", "False"), ("LAYERSUBSTACK_V8_0SHOWTOPDIELECTRIC", "False"),
        ("LAYERSUBSTACK_V8_0SHOWBOTTOMDIELECTRIC", "False"), ("LAYERSUBSTACK_V8_0TYPE", "1"),
        ("LAYERSUBSTACK_V8_0USEDBYPRIMS", "TRUE"), ("LAYERSUBSTACK_V8_0SERVICE", "FALSE"),
    ]
    for i, (layer_name, layer_id, extra) in enumerate(stack_layers(stack, mask)):
        p = f"LAYER_V8_{i}"
        fields += [(f"{p}LAYERID", str(layer_id)), (f"{p}ID", guid(name, "layer", layer_name)),
                   (f"{p}NAME", layer_name), (f"{p}USEDBYPRIMS", "FALSE"),
                   (f"{p}_{sub}CONTEXT", "0"), (f"{p}_{sub}USEDBYPRIMS", "FALSE")]
        fields += [(f"{p}{k}", v) for k, v in extra]
    text = "|" + "|".join(f"{k}={v}" for k, v in fields) + "|"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("latin-1"))


def rigid_mask() -> dict:
    r = CAPS["rigid"]
    return {"dk": r["soldermask_dk"], "thickness": r["soldermask_thickness"], "material": "Solder Resist"}


def two_layer_stackups() -> list[dict]:
    r = CAPS["rigid"]
    out = []
    for outer in r["two_layer_stackups"]["outer_oz"]:
        for t in r["two_layer_stackups"]["thicknesses"]:
            cu = OZ_TO_MM[outer]
            out.append({
                "name": f"2L-{t:g}mm-{oz(outer)}", "layers": 2, "thickness": t, "outer_copper_oz": outer,
                "inner_copper_oz": None, "finished_thickness": None, "category": None,
                "notes": ["built from published thickness; core = board - copper"],
                "stack": [{"kind": "copper", "thickness": cu},
                          {"kind": "core", "material": "FR-4 core", "thickness": round(t - 2 * cu, 4),
                           "dk": r["two_layer_core_dk"]},
                          {"kind": "copper", "thickness": cu}],
            })
    return out


def one_layer_flex_stackups() -> list[dict]:
    f = CAPS["flex"]
    out = []
    for spec in f["one_layer_stackups"]:
        thin_cover = spec["copper_oz"] <= 0.5 and spec["polyimide"] < 0.05
        pi_um, glue_um = (12.5, 15) if thin_cover else (25, 25)
        cu = {0.33: 0.012, 0.5: 0.018, 1: 0.035}[spec["copper_oz"]]
        out.append({
            "name": f"FPC1L-{spec['finished']:g}mm-Cu{round(cu * 1000)}um-PI{round(spec['polyimide'] * 1000)}um",
            "jlc_names": [], "layers": 1, "thickness": spec["finished"], "outer_copper_oz": spec["copper_oz"],
            "inner_copper_oz": None, "finished_thickness": spec["finished"],
            "notes": ["built from the flex capability page; bottom side has no coverlay"],
            "stack": [
                {"kind": "coverlay", "material": f"Coverlay PI {pi_um:g}um + adhesive {glue_um:g}um",
                 "thickness": (pi_um + glue_um) / 1000, "dk": f["coverlay_dk"]},
                {"kind": "copper", "thickness": cu},
                {"kind": "core", "material": f"Polyimide {round(spec['polyimide'] * 1000)}um (adhesiveless)",
                 "thickness": spec["polyimide"], "dk": f["polyimide_dk"]},
            ],
        })
    return out


def flex_mask_and_stack(s: dict) -> tuple[tuple[dict, dict], list[dict]]:
    """Outer coverlays become the Top/Bottom Solder layers; anything else stays in the stack."""
    stack = list(s["stack"])
    none = {"dk": CAPS["flex"]["coverlay_dk"], "thickness": 0.0, "material": "None"}
    top = stack.pop(0) if stack[0]["kind"] == "coverlay" else none
    bottom = stack.pop() if stack[-1]["kind"] == "coverlay" else none
    return (top, bottom), stack


def total_thickness(s: dict) -> float:
    return round(sum(l["thickness"] for l in s["stack"]), 3)


def write_all_stackups() -> list[tuple[str, Path, dict]]:
    written = []
    rigid = two_layer_stackups() + STACKUPS["rigid"]
    for s in rigid:
        inner = f"-{oz(s['inner_copper_oz'])}" if s["inner_copper_oz"] is not None else ""
        name = s["name"] if s["layers"] == 2 else \
            f"{s['layers']}L-{s['thickness']:g}mm-{oz(s['outer_copper_oz'])}{inner}-{s['name']}"
        path = OUT_STACKUPS / "rigid" / f"{s['layers']}-layer" / f"{name}.stackup"
        write_stackup(path, name, s["stack"], (rigid_mask(), rigid_mask()))
        written.append(("rigid", path, s))
    for s in one_layer_flex_stackups() + STACKUPS["flex"]:
        mask, stack = flex_mask_and_stack(s)
        if s["layers"] == 1:
            # Altium always has a Top and a Bottom Layer. The Bottom Layer here is a
            # placeholder: 1-layer flex has no bottom copper, so do not route on it.
            stack = stack + [{"kind": "copper", "thickness": stack[0]["thickness"]}]
        path = OUT_STACKUPS / "flex" / f"{s['layers']}-layer" / f"{s['name']}.stackup"
        write_stackup(path, s["name"], stack, mask)
        written.append(("flex", path, s))
    return written


def write_stackup_index(written: list[tuple[str, Path, dict]]) -> None:
    lines = [
        "# Stack-up index",
        "",
        "Generated by `tools/generate.py` from `data/stackups.json` and `data/capabilities.toml`."
        " Do not edit by hand.",
        "",
        "- **Nominal** is the board thickness you select when ordering. **Stack** is the sum of the layers"
        " in the file. **Finished** is the finished thickness JLCPCB quotes for the template, where given.",
        "- **Category** is JLCPCB's own label: *general* stack-ups are their standard builds; *special*"
        " stack-ups are alternatives they mark as special.",
        "",
    ]
    for group, title in (("rigid", "Rigid FR-4"), ("flex", "Flex (polyimide)")):
        rows = [(p, s) for g, p, s in written if g == group]
        for layers in sorted({s["layers"] for _, s in rows}):
            lines += [f"## {title}, {layers} layer", ""]
            if group == "rigid":
                lines += ["| File | JLCPCB template | Nominal | Stack | Finished | Outer Cu | Inner Cu | Category | Notes |",
                          "|---|---|---|---|---|---|---|---|---|"]
            else:
                lines += ["| File | JLCPCB template(s) | Nominal | Stack | Finished | Outer Cu | Inner Cu | Notes |",
                          "|---|---|---|---|---|---|---|---|"]
            for p, s in rows:
                if s["layers"] != layers:
                    continue
                rel = p.relative_to(OUT_STACKUPS).as_posix()
                inner = oz(s["inner_copper_oz"]) if s["inner_copper_oz"] is not None else "-"
                fin = f"{s['finished_thickness']:g} mm" if s["finished_thickness"] else "-"
                notes = "; ".join(s["notes"]) or ""
                stack_total = round(total_thickness(s), 3)
                if group == "rigid":
                    jlc = s["name"] if layers > 2 else "-"
                    lines.append(f"| [{p.stem}]({rel}) | {jlc} | {s['thickness']:g} mm | {stack_total:g} mm | {fin} "
                                 f"| {oz(s['outer_copper_oz'])} | {inner} | {s['category'] or '-'} | {notes} |")
                else:
                    jlc = ", ".join(n.replace("|", "/") for n in s["jlc_names"]) or "-"
                    lines.append(f"| [{p.stem}]({rel}) | {jlc} | {s['thickness']:g} mm | {stack_total:g} mm | {fin} "
                                 f"| {oz(s['outer_copper_oz'])} | {inner} | {notes} |")
            lines.append("")
    (OUT_STACKUPS / "README.md").write_text("\n".join(lines))


def main() -> None:
    for d in (OUT_RULES, OUT_STACKUPS):
        if d.exists():
            shutil.rmtree(d)
    rules = rigid_rulesets() + flex_rulesets()
    for rs in rules:
        write_rules(rs)
    written = write_all_stackups()
    write_stackup_index(written)
    print(f"wrote {len(rules)} rule files and {len(written)} stack-ups")


if __name__ == "__main__":
    main()
