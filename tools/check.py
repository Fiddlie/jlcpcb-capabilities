#!/usr/bin/env python3
"""Sanity checks for the generated Altium files.

- Every .RUL parses, and each rule kind carries the keys Altium wrote for that
  kind in the original (Altium-exported) rule files, so the format matches.
- Rule values are internally consistent with data/capabilities.toml.
- Every .stackup has the expected copper layer count and Altium layer keys.

Run after tools/generate.py. Exits non-zero on any failure.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAPS = tomllib.loads((ROOT / "data" / "capabilities.toml").read_text())
# An Altium-exported rule file from the repo history, used as the format reference.
REFERENCE_COMMIT = "668069d"
REFERENCE_FILES = ["design-rules/altium/2layer-1oz.RUL", "design-rules/altium/4layer-2oz.RUL"]

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def parse_rul(data: bytes) -> list[dict[str, str]]:
    text = data.decode("latin-1")
    records = [r for r in text.split("\xb6\r\n") if r.strip()]
    out = []
    for r in records:
        fields = {}
        for part in r.split("|"):
            k, _, v = part.partition("=")
            fields[k] = v
        out.append(fields)
    return out


def to_mm(value: str) -> float:
    m = re.fullmatch(r"(-?[0-9.]+)(mm|mil)", value)
    if not m:
        raise ValueError(value)
    return float(m.group(1)) * (0.0254 if m.group(2) == "mil" else 1)


def is_per_layer(key: str) -> bool:
    return bool(re.match(r"(TOPLAYER|BOTTOMLAYER|MIDLAYER\d+)_", key) or re.match(r"(MID LAYER \d+|TOP LAYER|BOTTOM LAYER)_V5", key))


def reference_keys() -> dict[str, list[set[str]]]:
    """Key sets per rule kind. Some kinds have two forms (e.g. one connect style, or one per object)."""
    keys: dict[str, list[set[str]]] = {}
    for f in REFERENCE_FILES:
        data = subprocess.run(["git", "show", f"{REFERENCE_COMMIT}:{f}"], cwd=ROOT, capture_output=True,
                              check=True).stdout
        for rec in parse_rul(data):
            ks = {k for k in rec if not is_per_layer(k)}
            if ks not in keys.setdefault(rec["RULEKIND"], []):
                keys[rec["RULEKIND"]].append(ks)
    return keys


def check_rules() -> int:
    ref = reference_keys()
    files = sorted((ROOT / "altium" / "design-rules").rglob("*.RUL"))
    for path in files:
        recs = parse_rul(path.read_bytes())
        by_name = {r["NAME"]: r for r in recs}
        if len(by_name) != len(recs):
            fail(f"{path.name}: duplicate rule names")
        ids = [r["UNIQUEID"] for r in recs]
        if len(set(ids)) != len(ids):
            fail(f"{path.name}: duplicate UNIQUEIDs")
        for r in recs:
            kind = r["RULEKIND"]
            if kind not in ref:
                fail(f"{path.name}:{r['NAME']}: rule kind {kind} not in the reference files")
                continue
            ours = {k for k in r if not is_per_layer(k)}
            if not any(form <= ours for form in ref[kind]):
                best = min(ref[kind], key=lambda form: len(form - ours))
                fail(f"{path.name}:{r['NAME']}: missing keys {sorted(best - ours)}")
            known = set().union(*ref[kind])
            if ours - known:
                fail(f"{path.name}:{r['NAME']}: keys not in reference {sorted(ours - known)}")
            for k, v in r.items():
                if re.fullmatch(r"-?[0-9.]+mil", v):
                    fail(f"{path.name}:{r['NAME']}: {k} is in mil ({v})")
        # Priorities must be 1..n per rule kind.
        kinds: dict[str, list[int]] = {}
        for r in recs:
            kinds.setdefault(r["RULEKIND"], []).append(int(r["PRIORITY"]))
        for kind, pr in kinds.items():
            if pr != list(range(1, len(pr) + 1)):
                fail(f"{path.name}: {kind} priorities {pr}")

        v = by_name["RoutingVias"]
        ring = to_mm(by_name["MinimumAnnularRingVias"]["MINIMUMRING"])
        for hole, dia in (("MINHOLEWIDTH", "MINWIDTH"), ("HOLEWIDTH", "WIDTH")):
            if to_mm(v[dia]) - to_mm(v[hole]) < 2 * ring - 1e-9:
                fail(f"{path.name}: via {v[hole]}/{v[dia]} below annular ring {ring}")
        if to_mm(by_name["HoleSize"]["MINLIMIT"]) > to_mm(v["MINHOLEWIDTH"]) + 1e-9:
            fail(f"{path.name}: HoleSize minimum above via minimum hole")
        min_width = to_mm(by_name["Width"]["MINLIMIT"])
        for k in ("PAD.RELIEFCONDUCTORWIDTH",):
            if to_mm(by_name["PlaneConnect"][k]) < min_width:
                fail(f"{path.name}: {k} below minimum track width")
        # The tier names must match the via minimums in capabilities.toml.
        tier = "advanced" if "advanced" in path.stem else "standard"
        if path.parent.name == "rigid":
            want = CAPS["rigid"]["via_tiers"][tier]
            if (to_mm(v["MINHOLEWIDTH"]), to_mm(v["MINWIDTH"])) != (want["min_hole"], want["min_diameter"]):
                fail(f"{path.name}: via minimum {v['MINHOLEWIDTH']}/{v['MINWIDTH']} does not match {tier} tier")
    return len(files)


def check_stackups() -> int:
    files = sorted((ROOT / "altium" / "stackups").rglob("*.stackup"))
    layer_key = re.compile(r"LAYER_V8_(\d+)([A-Z_]+|_\{[^}]+\}[A-Z]+)$")
    for path in files:
        text = path.read_text("latin-1")
        if not (text.startswith("|STACKUPVERSION=1|") and text.endswith("|")):
            fail(f"{path.name}: bad framing")
        fields = dict(p.partition("=")[::2] for p in text.strip("|").split("|"))
        layers: dict[int, dict[str, str]] = {}
        for k, v in fields.items():
            m = layer_key.match(k)
            if m:
                layers.setdefault(int(m.group(1)), {})[m.group(2)] = v
        copper = [l for l in layers.values() if "COPTHICK" in l]
        expected = int(re.match(r"(\d+)-layer", path.parent.name).group(1))
        if path.parent.parent.name == "flex" and expected == 1:
            expected = 2  # 1-layer flex carries a placeholder Bottom Layer
        if len(copper) != expected:
            fail(f"{path.name}: {len(copper)} copper layers, expected {expected}")
        names = [l["NAME"] for l in copper]
        want = ["Top Layer"] + [f"Mid Layer {i}" for i in range(1, expected - 1)] + ["Bottom Layer"]
        if names != want:
            fail(f"{path.name}: copper layer names {names}")
        for l in layers.values():
            for key in ("DIELHEIGHT", "COPTHICK"):
                if key in l and to_mm(l[key]) < 0:
                    fail(f"{path.name}: negative {key}")
            if "DIELTYPE" in l and l["DIELTYPE"] != "3" and to_mm(l["DIELHEIGHT"]) <= 0:
                fail(f"{path.name}: {l['NAME']} has no thickness")
    return len(files)


def main() -> None:
    n_rules = check_rules()
    n_stack = check_stackups()
    if failures:
        print("\n".join(failures[:50]))
        print(f"{len(failures)} failures")
        sys.exit(1)
    print(f"ok: {n_rules} rule files, {n_stack} stack-ups")


if __name__ == "__main__":
    main()
