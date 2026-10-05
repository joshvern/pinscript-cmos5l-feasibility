#!/usr/bin/env python3
"""Non-vacuous CMOS5L supplement to the pinned upstream pin/label DRC.

The original rule subtracts drawing polygons from pin/label polygons. We retain
that exact containment rule over the flattened hierarchy. Text is not polygon
geometry: top-level text anchors and the LEF PIN contract are separate checks.
No layout is modified. Requires the already installed precheck KLayout module.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

PDK_REVISION = "2bbec755dc67ca3db0261c3d6163e15735d66710"
SUPPORT_REVISION = "d66cf179e7bc4d296362ab7e2e3b344dc3c4f665"
PDK_BASE = f"https://github.com/IHP-GmbH/IHP-Open-PDK/blob/{PDK_REVISION}/ihp-sg13cmos5l/libs.tech/klayout/tech/"
UPSTREAM_RULE = {
    "revision": SUPPORT_REVISION,
    "url": f"https://github.com/TinyTapeout/tt-support-tools/blob/{SUPPORT_REVISION}/precheck/tech-files/pin_label_purposes_overlapping_drawing.rb.drc",
    "sha256": "b11c7ef94bb7b6e064b92d6b650e5695c880cbce2429eb58b5014fc6c85580dc",
}
PINNED_HASHES = {
    "sg13cmos5l.lyp": "eca546db8246bcb44f2960577f11a6414048b97955fa10dad143a427c90b389f",
    "sg13cmos5l.map": "b02e4d9966222e13c9fd2b994f1a8951d4d748afea128ce3f5b2360feab12c68",
}


def file_info(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def load_mapping(lyp: Path, stream_map: Path) -> list[dict]:
    """Derive numeric layers from pinned PDK files, never from guessed constants."""
    for path, name in [(lyp, "sg13cmos5l.lyp"), (stream_map, "sg13cmos5l.map")]:
        if file_info(path)["sha256"] != PINNED_HASHES[name]:
            raise ValueError(f"PDK mapping digest mismatch: {path} ({name})")
    purposes: dict[str, dict[str, tuple[int, int]]] = {}
    for prop in ET.parse(lyp).iter("properties"):
        name = prop.findtext("name", "")
        source = prop.findtext("source", "")
        if "." not in name or not re.fullmatch(r"\d+/\d+", source):
            continue
        layer, purpose = name.rsplit(".", 1)
        if purpose in {"drawing", "pin", "label"}:
            purposes.setdefault(layer, {})[purpose] = tuple(map(int, source.split("/")))
    # NAME Metal*/PIN in the stream map is the actual exported text datatype.
    for match in re.finditer(r"^NAME\s+(\w+)/PIN\s+(\d+)\s+(\d+)\s*$", stream_map.read_text(), re.M):
        layer, number, datatype = match.groups()
        if layer not in purposes or "drawing" not in purposes[layer]:
            raise ValueError(f"stream-map label has no LYP drawing definition: {layer}")
        purposes[layer]["stream_label"] = (int(number), int(datatype))
    result = []
    for layer, values in sorted(purposes.items()):
        if layer == "PWell":
            # Original rule explicitly excludes pwell by default: labelling
            # implicit pwell without drawing it is legal. Preserve that scope.
            continue
        selected = {key: value for key, value in values.items() if key != "drawing"}
        if "drawing" in values and selected:
            result.append({"name": layer, "drawing": values["drawing"], "purposes": selected})
    if not result or not any("stream_label" in x["purposes"] for x in result):
        raise ValueError("empty/incomplete PDK pin/label mapping")
    return result


def lef_contract(path: Path) -> tuple[str, list[str]]:
    source = path.read_text()
    macros = re.findall(r"^\s*MACRO\s+(\S+)\s*$", source, re.M)
    names = re.findall(r"^\s*PIN\s+(\S+)\s*$", source, re.M)
    if len(macros) != 1 or not names or len(names) != len(set(names)):
        raise ValueError("LEF must contain one macro and a nonempty unique PIN contract")
    return macros[0], sorted(names)


def inspect_layout(gds: Path, lef: Path, mapping: list[dict]) -> dict:
    import klayout.db as db

    top_name, expected = lef_contract(lef)
    layout = db.Layout()
    layout.read(str(gds))
    top = layout.cell(top_name)
    if top is None or top_name not in [cell.name for cell in layout.top_cells()]:
        raise ValueError(f"LEF macro {top_name} is not a GDS top cell")
    evaluations = []
    violations = []
    labels: Counter[str] = Counter()
    polygon_objects = 0
    top_text_objects = 0
    for layer in mapping:
        drawing_index = layout.find_layer(*layer["drawing"])
        drawing = db.Region(top.begin_shapes_rec(drawing_index)) if drawing_index is not None else db.Region()
        drawing.merge()
        top_drawing = None
        for purpose, pair in sorted(layer["purposes"].items()):
            index = layout.find_layer(*pair)
            pins = db.Region(top.begin_shapes_rec(index)) if index is not None else db.Region()
            count = pins.count()
            polygon_objects += count
            # KLayout Region takes boxes/paths/polygons, and ignores text just
            # as the upstream DRC polygons(...) input does.
            outside = pins - drawing
            errors = outside.count()
            row = {
                "rule": "pin_label_polygon_minus_drawing",
                "layer_name": layer["name"], "purpose": purpose,
                "drawing_layer": list(layer["drawing"]), "selected_layer": list(pair),
                "scope": "whole hierarchy, transformed to top coordinates",
                "drawing_merged_polygon_count": drawing.count(),
                "relevant_object_count": count, "evaluated_object_count": count,
                "violation_count": errors,
            }
            if errors:
                row["violation_bounding_boxes_dbu"] = [str(poly.bbox()) for poly in outside.each()]
                violations.append({"rule": row["rule"], "layer": list(pair), "count": errors})
            evaluations.append(row)
            # Separate coverage strengthening: top-level TEXT origins must sit
            # on drawing, and the complete LEF PIN name set must be present.
            texts = [] if index is None else [shape.text for shape in top.shapes(index).each() if shape.is_text()]
            if texts:
                if top_drawing is None:
                    top_drawing = list(drawing.each())
                misplaced = []
                for text in texts:
                    labels[text.string] += 1
                    point = text.trans.disp
                    if not any(poly.inside(point) for poly in top_drawing):
                        misplaced.append({"label": text.string, "point_dbu": [point.x, point.y]})
                top_text_objects += len(texts)
                evaluations.append({
                    "rule": "top_text_anchor_on_drawing", "layer_name": layer["name"],
                    "purpose": purpose, "drawing_layer": list(layer["drawing"]), "selected_layer": list(pair),
                    "scope": "top cell text origins only; no text glyph geometry implied",
                    "relevant_object_count": len(texts), "evaluated_object_count": len(texts),
                    "violation_count": len(misplaced), "violations": misplaced,
                })
                if misplaced:
                    violations.append({"rule": "top_text_anchor_on_drawing", "layer": list(pair), "count": len(misplaced)})
    missing = sorted(set(expected) - set(labels))
    unexpected = sorted(set(labels) - set(expected))
    duplicated = {name: count for name, count in sorted(labels.items()) if count != 1}
    contract_errors = len(missing) + len(unexpected) + len(duplicated)
    evaluations.append({"rule": "top_labels_match_lef_pin_contract", "relevant_object_count": len(expected),
                        "evaluated_object_count": len(labels), "violation_count": contract_errors})
    incomplete = []
    if not mapping:
        incomplete.append("empty mapping")
    if polygon_objects == 0:
        incomplete.append("no relevant pin/label polygons were evaluated")
    if top_text_objects == 0:
        incomplete.append("no relevant top text labels were evaluated")
    violation_count = sum(row["violation_count"] for row in evaluations)
    return {
        "status": "INCOMPLETE" if incomplete else "FAIL" if violation_count else "PASS",
        "top_cell": top_name, "database_unit_um": layout.dbu, "klayout_version": db.__version__,
        "expected_labels": {"source": "LEF PIN declarations", "count": len(expected), "names": expected,
                            "observed": dict(sorted(labels.items())), "missing": missing,
                            "unexpected": unexpected, "duplicated": duplicated},
        "polygon_object_count": polygon_objects, "top_text_object_count": top_text_objects,
        "relevant_object_count": polygon_objects + top_text_objects,
        "evaluated_object_count": polygon_objects + top_text_objects,
        "rule_evaluation_count": sum(row["evaluated_object_count"] for row in evaluations),
        "nonempty_rule_count": sum(row["evaluated_object_count"] > 0 for row in evaluations),
        "violation_count": violation_count, "violations": violations,
        "incomplete_reasons": incomplete, "evaluations": evaluations,
    }


def check(gds: Path, lef: Path, lyp: Path, stream_map: Path, identity: dict | None = None) -> dict:
    report = {"schema_version": 1, "status": "INCOMPLETE", "identity": identity or {},
              "pdk": "ihp-sg13cmos5l", "pdk_revision": PDK_REVISION,
              "source": {"upstream_rule": UPSTREAM_RULE}, "evaluations": [],
              "relevant_object_count": 0, "evaluated_object_count": 0,
              "rule_evaluation_count": 0, "violation_count": None,
              "scope": "upstream polygon-containment intent with CMOS5L mapping; extra top text/LEF contract checks",
              "excluded": [{"layer": "PWell", "reason": "preserves original rule's default implicit-pwell exemption"}]}
    try:
        for name, path in [("gds", gds), ("lef", lef), ("pdk_lyp", lyp), ("pdk_stream_map", stream_map)]:
            report["source"][name] = file_info(path)
        report["source"]["pdk_lyp"]["url"] = PDK_BASE + "sg13cmos5l.lyp"
        report["source"]["pdk_stream_map"]["url"] = PDK_BASE + "sg13cmos5l.map"
        mapping = load_mapping(lyp, stream_map)
        report["selected_mapping"] = mapping
        report.update(inspect_layout(gds, lef, mapping))
    except Exception as error:
        report["incomplete_reasons"] = [f"{type(error).__name__}: {error}"]
    return report


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def fixture_self_test(output: Path, lyp: Path, stream_map: Path) -> dict:
    """Isolated generated fixtures; never copy or mutate a production GDS."""
    import klayout.db as db

    directory = output.parent / (output.stem + "-inputs")
    directory.mkdir(parents=True, exist_ok=False)
    mapping = load_mapping(lyp, stream_map)
    metal = next(row for row in mapping if row["name"] == "Metal4")
    lef = directory / "fixture.lef"
    lef.write_text("VERSION 5.7 ;\nMACRO fixture\n  PIN signal\n    DIRECTION INPUT ;\n    PORT\n      LAYER Metal4 ;\n        RECT 1 1 2 2 ;\n    END\n  END signal\nEND fixture\nEND LIBRARY\n")
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("fixture")
    top.shapes(layout.layer(*metal["drawing"])).insert(db.Box(0, 0, 10000, 10000))
    pins = top.shapes(layout.layer(*metal["purposes"]["pin"]))
    pins.insert(db.Box(1000, 1000, 2000, 2000))
    top.shapes(layout.layer(*metal["purposes"]["stream_label"])).insert(db.Text("signal", db.Trans(1500, 1500)))
    good = directory / "valid.gds"
    layout.write(str(good))
    # Exact upstream rule mutant: a pin polygon extends beyond all drawing.
    pins.insert(db.Box(20000, 20000, 21000, 21000))
    bad = directory / "pin-outside-drawing.gds"
    layout.write(str(bad))
    cases = {
        "valid": check(good, lef, lyp, stream_map),
        "pin_outside_drawing": check(bad, lef, lyp, stream_map),
        "empty_mapping": inspect_layout(good, lef, []),
        "wrong_mapping": inspect_layout(good, lef, [{"name": "incorrect", "drawing": (200, 0), "purposes": {"pin": (200, 2), "label": (200, 25)}}]),
    }
    wrong_file = directory / "incorrect.map"
    wrong_file.write_text(stream_map.read_text().replace("Metal4", "WrongMetal"))
    cases["wrong_mapping_digest"] = check(good, lef, lyp, wrong_file)
    # Also verify text origins and expected set are independent of polygons.
    layout.read(str(good))
    top = layout.cell("fixture")
    top.shapes(layout.layer(*metal["purposes"]["stream_label"])).clear()
    no_text = directory / "missing-label.gds"
    layout.write(str(no_text))
    cases["missing_label"] = check(no_text, lef, lyp, stream_map)
    expected = {"valid": "PASS", "pin_outside_drawing": "FAIL", "empty_mapping": "INCOMPLETE",
                "wrong_mapping": "INCOMPLETE", "wrong_mapping_digest": "INCOMPLETE", "missing_label": "INCOMPLETE"}
    outcomes = []
    for name, result in cases.items():
        reason_correct = name != "pin_outside_drawing" or any(
            row["rule"] == "pin_label_polygon_minus_drawing" and row["violation_count"] == 1
            for row in result["evaluations"])
        outcomes.append({"name": name, "expected": expected[name], "observed": result["status"],
                         "passed": result["status"] == expected[name] and reason_correct})
        write_json(directory / f"{name}.json", result)
    summary = {"schema_version": 1, "status": "PASS" if all(row["passed"] for row in outcomes) else "FAIL",
               "scope": "checker fixtures only; not physical-run acceptance", "tests": outcomes,
               "test_count": len(outcomes), "skipped": 0, "klayout_version": db.__version__,
               "fixture_directory": str(directory), "source": {"checker": file_info(Path(__file__))}}
    write_json(output, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gds", required=True, type=Path)
    parser.add_argument("--lef", required=True, type=Path)
    parser.add_argument("--pdk-root", type=Path, default=Path(os.environ.get("PDK_ROOT", "/home/runner/pdk")))
    parser.add_argument("--lyp", type=Path, help="explicit pinned LYP file for retained-evidence replay")
    parser.add_argument("--stream-map", type=Path, help="explicit pinned stream-map file for retained-evidence replay")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fixture-self-test", type=Path)
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT"))
    parser.add_argument("--candidate", default="m3c-cts-only")
    parser.add_argument("--source-sha", default=os.environ.get("GITHUB_SHA"))
    args = parser.parse_args()
    tech = args.pdk_root / "ihp-sg13cmos5l/libs.tech/klayout/tech"
    lyp = args.lyp or tech / "sg13cmos5l.lyp"
    stream_map = args.stream_map or tech / "sg13cmos5l.map"
    identity = {"run_id": args.run_id, "run_attempt": args.run_attempt,
                "candidate": args.candidate, "source_sha": args.source_sha}
    report = check(args.gds, args.lef, lyp, stream_map, identity)
    if args.fixture_self_test:
        try:
            fixtures = fixture_self_test(args.fixture_self_test, lyp, stream_map)
            report["fixture_validation"] = {"status": fixtures["status"], **file_info(args.fixture_self_test)}
            if fixtures["status"] != "PASS":
                report["status"] = "INCOMPLETE"
        except Exception as error:
            report["fixture_validation"] = {"status": "INCOMPLETE", "error": str(error)}
            report["status"] = "INCOMPLETE"
    write_json(args.output, report)
    print(f"pin-label {report['status']}: objects={report['evaluated_object_count']}, violations={report['violation_count']}; {args.output}")
    return 0 if report["status"] == "PASS" else 1 if report["status"] == "FAIL" else 2


if __name__ == "__main__":
    sys.exit(main())
