#!/usr/bin/env python3
"""Fetch JLCPCB's published impedance stack-ups and normalise them to data/stackups.json.

The stack-ups come from the same endpoint the JLCPCB impedance calculator
(https://jlcpcb.com/pcb-impedance-calculator) and the stack-up page
(https://jlcpcb.com/impedance) use. The raw response is ~50 MB of mostly
bookkeeping fields, so only what is needed to build a layer stack is kept.

Usage:
    python3 tools/fetch_stackups.py                 # fetch from jlcpcb.com
    python3 tools/fetch_stackups.py --rigid r.json --flex f.json   # use saved responses
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

API = "https://jlcpcb.com/api/jlcTools/impedance/selectPageImpedanceDefaultTemplate"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "stackups.json"

# Selection of what we keep. Thicknesses match JLCPCB's standard FR-4 options
# (https://jlcpcb.com/capabilities/pcb-capabilities, "Thickness").
RIGID_LAYERS = {4, 6, 8}
RIGID_THICKNESSES = {0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0}
RIGID_OUTER_OZ = {1.0, 2.0}
RIGID_INNER_OZ = {0.5, 1.0, 2.0}

# Flex copper is given in oz in the API; JLCPCB's flex page gives the foil thicknesses.
FLEX_OZ_TO_MM = {0.33: 0.012, 0.5: 0.018, 1.0: 0.035}

CN = re.compile(r"[　-〿一-鿿＀-￯]")

# Translations of the Chinese notes JLCPCB attaches to some templates.
NOTE_TRANSLATIONS = [
    ("内层有覆盖膜", "coverlay on inner layers"),
    ("可做分层板", "suitable for air-gap (bookbinder) construction"),
    ("压延铜专用", "rolled-annealed copper only"),
    ("压延铜箔", "rolled-annealed copper"),
    ("压延铜", "rolled-annealed copper"),
    ("高延展铜箔专用", "high-elongation copper only"),
    ("高延展铜箔", "high-elongation copper"),
    ("高延展", "high-elongation copper"),
    ("内层线路较少时不建议使用", "not recommended when inner layers carry little copper"),
    ("内层PI厚50um", "50 um inner polyimide"),
    ("core+core结构", "core + core construction"),
    ("双+双", "double-sided + double-sided"),
    ("单+单", "single-sided + single-sided"),
]


def fetch(board_type: int) -> dict:
    body = json.dumps({"pageNum": 1, "pageSize": 99999, "boardType": board_type, "usePurpose": 1}).encode()
    req = urllib.request.Request(
        API, data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def templates(response: dict) -> list[dict]:
    if response.get("result") != "success":
        raise SystemExit(f"unexpected API response: {response.get('message')!r}")
    return response["body"]["list"]


def r4(x: float) -> float:
    return round(float(x), 4)


def is_custom(t: dict) -> bool:
    """'自定义' templates are display-only placeholders for a custom build."""
    return "自定义" in t["appointName"] or any("自定义" in b["materialName"] for b in t["basicDataList"])


def notes_from(text: str) -> list[str]:
    notes = []
    for cn, en in NOTE_TRANSLATIONS:
        if cn in text:
            notes.append(en)
            text = text.replace(cn, "")
    return notes


def finished_thickness(display: str) -> float | None:
    m = re.search(r"(?:成品板厚|理论厚度|理论板厚|实际板厚|成品厚度)\s*([0-9.]+)\s*mm", display)
    return float(m.group(1)) if m else None


def rigid_category(display: str) -> str | None:
    if "通用" in display:
        return "general"
    if "特殊" in display:
        return "special"
    return None


def prepreg_material(b: dict) -> str:
    # e.g. "7628 RC49% 8.6mil" -> "7628 RC49%"
    name = " ".join(b["materialName"].split())
    m = re.match(r"(\d+)\s+(RC\d+%)", name)
    return f"{m.group(1)} {m.group(2)}" if m else name


def normalise_rigid(t: dict) -> dict:
    stack: list[dict] = []
    for b in t["basicDataList"]:
        kind = b["materialType"]
        if kind == 1:  # copper foil (outer layers)
            stack.append({"kind": "copper", "thickness": r4(b["topConductorThick"])})
        elif kind == 0:  # prepreg
            stack.append({
                "kind": "prepreg",
                "material": prepreg_material(b),
                "thickness": r4(b["dielectricThick"]),
                "dk": b["dielectricConstant"],
            })
        elif kind in (2, 3):  # core with copper / core etched bare
            brand = b.get("brandType") or "FR-4"
            if kind == 2 and b["topConductorThick"]:
                stack.append({"kind": "copper", "thickness": r4(b["topConductorThick"])})
            stack.append({
                "kind": "core",
                "material": f"{brand} core" if kind == 2 else f"{brand} core (copper etched off)",
                "thickness": r4(b["dielectricThick"]),
                "dk": b["dielectricConstant"],
            })
            if kind == 2 and b["botConductorThick"]:
                stack.append({"kind": "copper", "thickness": r4(b["botConductorThick"])})
        else:
            raise ValueError(f"{t['appointName']}: unknown material type {kind}")
    display = t["receptionDisplayName"]
    return {
        "name": t["appointName"],
        "layers": t["plateLayerNumber"],
        "thickness": float(t["plateThickness"]),
        "outer_copper_oz": float(t["cuprumThickness"]),
        "inner_copper_oz": float(t["innerCopperThickness"]),
        "finished_thickness": finished_thickness(display),
        "category": rigid_category(display),
        "notes": notes_from(display),
        "stack": stack,
    }


def normalise_flex(t: dict) -> dict:
    stack: list[dict] = []
    raw = t["basicDataList"]
    for i, b in enumerate(raw):
        mat = b["material"]
        name = b["materialName"]
        if mat == "覆盖膜":  # coverlay: polyimide + adhesive
            m = re.search(r"PI厚([0-9.]+)um/胶厚([0-9.]+)um", name)
            pi, glue = (float(m.group(1)), float(m.group(2))) if m else (None, None)
            outer = i in (0, len(raw) - 1)
            stack.append({
                "kind": "coverlay" if outer else "coverlay-inner",
                "material": f"Coverlay PI {pi:g}um + adhesive {glue:g}um",
                "thickness": r4(b["dielectricThick"]),
                "dk": b["dielectricConstant"],
            })
        elif mat == "AD胶":  # bonding adhesive
            stack.append({
                "kind": "adhesive",
                "material": "Bonding adhesive",
                "thickness": r4(b["dielectricThick"]),
                "dk": b["dielectricConstant"],
            })
        elif mat in ("芯板", "基板"):  # double-sided core / single-sided base
            pi = re.search(r"PI厚([0-9.]+)um", name)
            pi_txt = f"Polyimide {float(pi.group(1)):g}um" if pi else "Polyimide"
            top = FLEX_OZ_TO_MM[round(b["topConductorThick"], 2)] if b["topConductorThick"] else None
            bot = FLEX_OZ_TO_MM[round(b["botConductorThick"], 2)] if b["botConductorThick"] else None
            if top:
                stack.append({"kind": "copper", "thickness": top})
            stack.append({
                "kind": "core",
                "material": pi_txt + (" (single-sided base)" if mat == "基板" else " (adhesiveless core)"),
                "thickness": r4(b["dielectricThick"]),
                "dk": b["dielectricConstant"],
            })
            if bot:
                stack.append({"kind": "copper", "thickness": bot})
        else:
            raise ValueError(f"{t['appointName']}: unknown flex material {mat!r}")
    display = t["receptionDisplayName"]
    return {
        "jlc_name": t["appointName"],
        "layers": t["plateLayerNumber"],
        "thickness": float(t["plateThickness"]),
        "outer_copper_oz": float(t["cuprumThickness"]),
        "inner_copper_oz": float(t["innerCopperThickness"]),
        "finished_thickness": finished_thickness(display),
        "notes": notes_from(t["appointName"] + display),
        "stack": stack,
    }


def flex_name(s: dict) -> str:
    """English file-friendly name. 4-layer templates have a usable code; 2-layer ones don't."""
    code = re.sub(r"[（(][^）)]*[）)]", "", s["jlc_name"])
    code = CN.sub("", code).strip(" -")
    if s["layers"] == 4 and re.fullmatch(r"FPC[0-9A-Z]+-[0-9A-Z]+", code):
        return code
    copper = [l["thickness"] for l in s["stack"] if l["kind"] == "copper"]
    pi = [l for l in s["stack"] if l["kind"] == "core"]
    pi_um = round(pi[0]["thickness"] * 1000)
    adhesive = any(l["kind"] == "adhesive" for l in s["stack"])
    cover = "-".join(str(round(l["thickness"] * 1000, 1)).rstrip("0").rstrip(".")
                     for l in s["stack"] if l["kind"] == "coverlay")
    return (f"FPC{s['layers']}L-{s['thickness']:g}mm-Cu{round(copper[0] * 1000)}um-PI{pi_um}um"
            f"-CL{cover}um" + ("-bonded" if adhesive else ""))


def build(rigid_resp: dict, flex_resp: dict, source: str) -> dict:
    rigid = []
    for t in templates(rigid_resp):
        if (t["plateLayerNumber"] in RIGID_LAYERS
                and float(t["plateThickness"]) in RIGID_THICKNESSES
                and float(t["cuprumThickness"]) in RIGID_OUTER_OZ
                and float(t["innerCopperThickness"]) in RIGID_INNER_OZ
                and t["receptionDisplayName"] != "无要求"  # "no requirement" duplicate of the default
                and not is_custom(t)):
            rigid.append(normalise_rigid(t))

    flex_by_structure: dict[str, dict] = {}
    for t in templates(flex_resp):
        if is_custom(t):
            continue
        s = normalise_flex(t)
        key = json.dumps([s["layers"], s["thickness"], s["stack"]])
        if key in flex_by_structure:  # same build, different copper foil type: merge notes
            existing = flex_by_structure[key]
            existing["jlc_names"].append(s["jlc_name"])
            existing["notes"] = sorted(set(existing["notes"]) | set(s["notes"]))
            continue
        s["jlc_names"] = [s.pop("jlc_name")]
        flex_by_structure[key] = s
    flex = list(flex_by_structure.values())
    for s in flex:
        s["name"] = flex_name({**s, "jlc_name": s["jlc_names"][0]})

    for group in (rigid, flex):
        names = [s["name"] for s in group]
        dupes = {n for n in names if names.count(n) > 1}
        for s in group:
            if s["name"] in dupes:  # same template name used for different copper options
                s["name"] += f"-{s['outer_copper_oz']:g}oz-{s['inner_copper_oz']:g}oz"

    key = lambda s: (s["layers"], s["thickness"], s["outer_copper_oz"], s["inner_copper_oz"], s["name"])
    return {
        "source": source,
        "retrieved": dt.date.today().isoformat(),
        "rigid": sorted(rigid, key=key),
        "flex": sorted(flex, key=key),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rigid", type=Path, help="saved API response for boardType=1 (rigid FR-4)")
    ap.add_argument("--flex", type=Path, help="saved API response for boardType=2 (flex)")
    args = ap.parse_args()
    rigid = json.loads(args.rigid.read_text()) if args.rigid else fetch(1)
    flex = json.loads(args.flex.read_text()) if args.flex else fetch(2)
    data = build(rigid, flex, API)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(data['rigid'])} rigid, {len(data['flex'])} flex stack-ups",
          file=sys.stderr)


if __name__ == "__main__":
    main()
