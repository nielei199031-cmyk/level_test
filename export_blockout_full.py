# BlockoutTools Full Export Script
# Run in UE Editor: File > Execute Python Script
# Exports all BlockoutTools actors with full geometry, floor, and path data to JSON

import unreal
import json
import os
import re
import bisect
from datetime import datetime
from collections import defaultdict

# ============================================================
# Configuration
# ============================================================
OUTPUT_FILENAME = "level_data.json"

# Tag patterns
PATH_TAG_PATTERN = re.compile(r'^(main|sub)path([A-Za-z]*)_(\d+)$', re.IGNORECASE)
FLOOR_TAG_PATTERN = re.compile(r'^floor_(\d+)$', re.IGNORECASE)

# BoxSize property names to try (UE Python may use snake_case or CamelCase)
BOX_SIZE_PROPS = ["box_size", "BoxSize", "box_extent", "BoxExtent", "Size"]

# Shape types supported by BlockoutTools
SHAPE_KEYWORDS = ['box', 'cone', 'cylinder', 'sphere', 'ramp', 'stairs', 'doorway', 'window']


# ============================================================
# Utility Functions
# ============================================================
def vector_to_dict(vec, precision=2):
    """Convert unreal.Vector to dict."""
    return {
        "x": round(vec.x, precision),
        "y": round(vec.y, precision),
        "z": round(vec.z, precision)
    }

def rotator_to_dict(rot, precision=2):
    """Convert unreal.Rotator to dict."""
    return {
        "pitch": round(rot.pitch, precision),
        "yaw": round(rot.yaw, precision),
        "roll": round(rot.roll, precision)
    }

def safe_str(fname):
    """Safely convert FName to string."""
    try:
        return str(fname)
    except:
        return ""

def compute_actual_size(box_size, scale):
    """Compute actual size = boxSize * scale (element-wise)."""
    if box_size is None:
        return None
    return {
        "x": round(box_size["x"] * scale["x"], 2),
        "y": round(box_size["y"] * scale["y"], 2),
        "z": round(box_size["z"] * scale["z"], 2)
    }

def detect_shape_type(class_name):
    """Detect BlockoutTools shape type from class name."""
    lower = class_name.lower()
    for shape in SHAPE_KEYWORDS:
        if shape in lower:
            return shape
    return 'box'

def is_decoration_actor(tags):
    """Actor with no meaningful tags is considered decoration layer."""
    meaningful = [t for t in tags if t.strip()]
    return len(meaningful) == 0


# ============================================================
# BoxSize Extraction (multi-fallback)
# ============================================================
_box_size_strategy_cache = {"strategy": None}  # remember which strategy worked

def extract_box_size(actor):
    """
    Try multiple strategies to extract BoxSize from a BlockoutTools actor.
    Returns dict {"x","y","z"} or None.
    """
    label = actor.get_actor_label()

    # Strategy 1: Direct editor property on actor
    for prop in BOX_SIZE_PROPS:
        try:
            val = actor.get_editor_property(prop)
            if val is not None:
                if _box_size_strategy_cache["strategy"] is None:
                    _box_size_strategy_cache["strategy"] = f"actor.{prop}"
                    unreal.log(f"[BoxSize] Strategy found: actor.get_editor_property('{prop}')")
                return vector_to_dict(val)
        except:
            continue

    # Strategy 2: Root component property
    try:
        root = actor.root_component
        if root is not None:
            for prop in BOX_SIZE_PROPS:
                try:
                    val = root.get_editor_property(prop)
                    if val is not None:
                        if _box_size_strategy_cache["strategy"] is None:
                            _box_size_strategy_cache["strategy"] = f"root.{prop}"
                            unreal.log(f"[BoxSize] Strategy found: root_component.get_editor_property('{prop}')")
                        return vector_to_dict(val)
                except:
                    continue
    except:
        pass

    # Strategy 3: BoxComponent -> box_extent (half-extents, multiply by 2)
    try:
        box_comps = actor.get_components_by_class(unreal.BoxComponent)
        if box_comps and len(box_comps) > 0:
            extent = box_comps[0].get_editor_property("box_extent")
            if extent is not None:
                if _box_size_strategy_cache["strategy"] is None:
                    _box_size_strategy_cache["strategy"] = "BoxComponent.box_extent"
                    unreal.log("[BoxSize] Strategy found: BoxComponent.box_extent (x2)")
                return {
                    "x": round(extent.x * 2, 2),
                    "y": round(extent.y * 2, 2),
                    "z": round(extent.z * 2, 2)
                }
    except:
        pass

    # Strategy 4: Any ShapeComponent
    try:
        shape_comps = actor.get_components_by_class(unreal.ShapeComponent)
        if shape_comps:
            for comp in shape_comps:
                try:
                    extent = comp.get_editor_property("box_extent")
                    if extent is not None:
                        if _box_size_strategy_cache["strategy"] is None:
                            _box_size_strategy_cache["strategy"] = "ShapeComponent.box_extent"
                            unreal.log("[BoxSize] Strategy found: ShapeComponent.box_extent (x2)")
                        return {
                            "x": round(extent.x * 2, 2),
                            "y": round(extent.y * 2, 2),
                            "z": round(extent.z * 2, 2)
                        }
                except:
                    continue
    except:
        pass

    # Strategy 5: Actor bounding box (ultimate fallback)
    try:
        origin, extent = actor.get_actor_bounds(False)
        if extent is not None:
            if _box_size_strategy_cache["strategy"] is None:
                _box_size_strategy_cache["strategy"] = "ActorBounds(fallback)"
                unreal.log_warning("[BoxSize] Using actor bounding box as fallback")
            return {
                "x": round(extent.x * 2, 2),
                "y": round(extent.y * 2, 2),
                "z": round(extent.z * 2, 2)
            }
    except:
        pass

    # Strategy 6: Diagnostic — log available properties
    unreal.log_warning(f"[BoxSize] All strategies failed for '{label}', running diagnostics...")
    try:
        props = [p for p in dir(actor) if any(kw in p.lower() for kw in ["box", "size", "extent"])]
        unreal.log_warning(f"  Actor props: {props}")
        root = actor.root_component
        if root:
            root_props = [p for p in dir(root) if any(kw in p.lower() for kw in ["box", "size", "extent"])]
            unreal.log_warning(f"  Root component props: {root_props}")
    except:
        pass

    return None


# ============================================================
# Actor Data Extraction
# ============================================================
def get_blockout_actors():
    """Filter all level actors for BlockoutTools actors."""
    all_actors = unreal.EditorLevelLibrary.get_all_level_actors()
    blockout = []
    for actor in all_actors:
        try:
            class_name = actor.get_class().get_name()
            if "blockout" in class_name.lower():
                blockout.append(actor)
        except:
            continue
    unreal.log(f"[Scan] Total actors: {len(all_actors)}, BlockoutTools actors: {len(blockout)}")
    return blockout

def extract_actor_data(actor):
    """Extract full data from a single BlockoutTools actor."""
    try:
        loc = actor.get_actor_location()
        rot = actor.get_actor_rotation()
        scale = actor.get_actor_scale3d()

        location = vector_to_dict(loc)
        rotation = rotator_to_dict(rot)
        scale_dict = vector_to_dict(scale, 4)
        tags = [safe_str(tag) for tag in actor.tags]
        box_size = extract_box_size(actor)
        actual_size = compute_actual_size(box_size, scale_dict)

        class_name = actor.get_class().get_name()
        shape_type = detect_shape_type(class_name)
        is_deco = is_decoration_actor(tags)

        # Shape-specific properties
        shape_props = {}
        if shape_type == 'cylinder':
            for name in ['CylinderRadius', 'cylinder_radius', 'cylinderradius']:
                try:
                    val = actor.get_editor_property(name)
                    if val is not None:
                        shape_props['cylinderRadius'] = round(float(val), 2)
                        break
                except:
                    continue
            for name in ['CylinderHeight', 'cylinder_height', 'cylinderheight']:
                try:
                    val = actor.get_editor_property(name)
                    if val is not None:
                        shape_props['cylinderHeight'] = round(float(val), 2)
                        break
                except:
                    continue
            for name in ['CylinderQuality', 'cylinder_quality', 'cylinderquality']:
                try:
                    val = actor.get_editor_property(name)
                    if val is not None:
                        shape_props['cylinderQuality'] = int(val)
                        break
                except:
                    continue

        if shape_type == 'stairs':
            for name in ['StairsSize', 'stairs_size', 'stairssize']:
                try:
                    val = actor.get_editor_property(name)
                    if val is not None:
                        shape_props['stairsSize'] = vector_to_dict(val)
                        break
                except:
                    continue
            for name in ['NumberOfSteps', 'number_of_steps', 'numberofsteps']:
                try:
                    val = actor.get_editor_property(name)
                    if val is not None:
                        shape_props['numberOfSteps'] = int(val)
                        break
                except:
                    continue

        # Always extract world-space bounds as fallback for non-box types
        bounds = None
        try:
            b_origin, b_extent = actor.get_actor_bounds(False)
            bounds = {
                "origin": vector_to_dict(b_origin),
                "extent": vector_to_dict(b_extent)
            }
        except:
            pass

        return {
            "name": actor.get_actor_label(),
            "class": class_name,
            "shapeType": shape_type,
            "isDecoration": is_deco,
            "location": location,
            "rotation": rotation,
            "scale": scale_dict,
            "boxSize": box_size,
            "actualSize": actual_size,
            "shapeProperties": shape_props if shape_props else None,
            "bounds": bounds,
            "tags": tags,
            "floor": None  # assigned later
        }
    except Exception as e:
        unreal.log_warning(f"[Extract] Failed for '{actor.get_actor_label()}': {e}")
        return None


# ============================================================
# Floor Detection (Floor Marker Board System)
# ============================================================
def assign_floors(actor_data_list):
    """
    Assign floor index using floor marker boards.

    Floor markers are BlockoutBox actors with tag 'floor_N'.
    They define Z boundaries between floors:
      - Below floor_0           -> Floor 1
      - Between floor_0 ~ floor_1 -> Floor 2
      - ...
      - Above highest marker    -> top floor

    No markers => all actors on Floor 0 (single layer).

    Returns (floors_list, floor_marker_names_set).
    """
    # Pass 1: identify floor markers
    markers = []  # (tag_index, z_position, actor_name)
    for data in actor_data_list:
        for tag in data["tags"]:
            match = FLOOR_TAG_PATTERN.match(tag)
            if match:
                marker_idx = int(match.group(1))
                markers.append((marker_idx, data["location"]["z"], data["name"]))
                data["_isFloorMarker"] = True
                break
        else:
            data["_isFloorMarker"] = False

    floor_marker_names = set(m[2] for m in markers)

    if not markers:
        # No markers -> all actors on floor 0, single layer
        for data in actor_data_list:
            data["floor"] = 0
        unreal.log("[Floors] No floor markers found, all actors assigned to Floor 0")
        return [0], floor_marker_names

    # Sort markers by Z position (ascending)
    markers.sort(key=lambda m: m[1])
    marker_z_values = [m[1] for m in markers]

    unreal.log(f"[Floors] Found {len(markers)} floor markers at Z: {marker_z_values}")

    # Pass 2: assign floors to non-marker actors
    for data in actor_data_list:
        if data.get("_isFloorMarker", False):
            continue
        z = data["location"]["z"]
        zone = bisect.bisect_right(marker_z_values, z)
        data["floor"] = zone + 1   # zone 0 -> Floor 1, zone 1 -> Floor 2, ...

    # Collect floor numbers (excluding markers)
    floors = sorted(set(
        d["floor"] for d in actor_data_list
        if not d.get("_isFloorMarker", False) and d["floor"] is not None
    ))

    unreal.log(f"[Floors] Assigned {len(floors)} floors: {floors}")
    return floors, floor_marker_names


# ============================================================
# Path Extraction
# ============================================================
def extract_paths(actor_data_list):
    """
    Parse path tags from actors and build ordered path waypoints.
    Tag format: mainpath_01, subpathA_01, etc.
    """
    # Collect path waypoints: key=(type, name), value=list of (seq, actor_name, position)
    path_map = defaultdict(list)

    for data in actor_data_list:
        for tag in data["tags"]:
            match = PATH_TAG_PATTERN.match(tag)
            if match:
                path_type = match.group(1).lower()   # "main" or "sub"
                path_name = match.group(2) or ""       # "A", "B", "" etc.
                seq = int(match.group(3))
                full_name = f"{path_type}path{path_name}"
                loc = data["location"]
                path_map[(path_type, full_name)].append({
                    "seq": seq,
                    "actor": data["name"],
                    "position": [loc["x"], loc["y"], loc["z"]]
                })

    # Sort waypoints by sequence number and build output
    result = {"main": [], "sub": []}
    for (path_type, full_name), waypoints in sorted(path_map.items()):
        waypoints.sort(key=lambda w: w["seq"])

        # Check for sequence gaps
        seqs = [w["seq"] for w in waypoints]
        for i in range(1, len(seqs)):
            if seqs[i] - seqs[i - 1] > 1:
                unreal.log_warning(
                    f"[Paths] Gap in sequence for '{full_name}': {seqs[i-1]} -> {seqs[i]}"
                )

        # Remove seq field from output
        clean_waypoints = [{"actor": w["actor"], "position": w["position"]} for w in waypoints]
        path_obj = {"name": full_name, "waypoints": clean_waypoints}
        result[path_type].append(path_obj)

    total = sum(len(p["waypoints"]) for paths in result.values() for p in paths)
    unreal.log(f"[Paths] Found {len(result['main'])} main + {len(result['sub'])} sub paths, {total} total waypoints")
    return result


# ============================================================
# Output Assembly
# ============================================================
def build_output(actor_data_list, paths, floors):
    """Assemble the final JSON structure."""
    level_name = "Unknown"
    try:
        level_name = unreal.EditorLevelLibrary.get_editor_world().get_name()
    except:
        unreal.log_warning("[Output] Could not get level name")

    return {
        "levelName": level_name,
        "exportTime": datetime.now().isoformat(),
        "floors": floors,
        "actors": actor_data_list,
        "paths": paths
    }

def write_json(data, output_dir=None):
    """Write JSON to disk."""
    if output_dir is None:
        output_dir = unreal.Paths.project_dir()
    output_path = os.path.join(output_dir, OUTPUT_FILENAME)

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        unreal.log(f"[Output] Saved to: {output_path}")
        return output_path
    except Exception as e:
        unreal.log_error(f"[Output] Failed to write: {e}")
        # Fallback: try user desktop
        try:
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            fallback_path = os.path.join(desktop, OUTPUT_FILENAME)
            with open(fallback_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            unreal.log_warning(f"[Output] Fallback saved to: {fallback_path}")
            return fallback_path
        except Exception as e2:
            unreal.log_error(f"[Output] Fallback also failed: {e2}")
            return None


# ============================================================
# Summary Report
# ============================================================
def print_summary(actor_data_list, paths, floors, output_path):
    """Print export summary to Output Log."""
    total = len(actor_data_list)
    box_ok = sum(1 for d in actor_data_list if d["boxSize"] is not None)
    box_fail = total - box_ok
    deco_count = sum(1 for d in actor_data_list if d.get("isDecoration", False))
    main_count = total - deco_count

    # Count shape types
    shape_counts = defaultdict(int)
    for d in actor_data_list:
        shape_counts[d.get("shapeType", "box")] += 1

    unreal.log("=" * 50)
    unreal.log("  EXPORT SUMMARY")
    unreal.log("=" * 50)
    unreal.log(f"  Blockout actors exported : {total}")
    unreal.log(f"    Main (tagged)          : {main_count}")
    unreal.log(f"    Decoration (untagged)  : {deco_count}")
    unreal.log(f"  Shape types              :")
    for shape, count in sorted(shape_counts.items()):
        unreal.log(f"    - {shape}: {count}")
    unreal.log(f"  BoxSize extracted        : {box_ok}")
    if box_fail > 0:
        unreal.log_warning(f"  BoxSize failed           : {box_fail}")
    unreal.log(f"  BoxSize strategy used    : {_box_size_strategy_cache['strategy'] or 'N/A'}")
    unreal.log(f"  Floors detected          : {len(floors)} {floors}")
    unreal.log(f"  Main paths               : {len(paths['main'])}")
    for p in paths["main"]:
        unreal.log(f"    - {p['name']}: {len(p['waypoints'])} waypoints")
    unreal.log(f"  Sub paths                : {len(paths['sub'])}")
    for p in paths["sub"]:
        unreal.log(f"    - {p['name']}: {len(p['waypoints'])} waypoints")
    unreal.log(f"  Output file              : {output_path}")
    unreal.log("=" * 50)


# ============================================================
# Main Entry Point
# ============================================================
def main():
    unreal.log("=" * 50)
    unreal.log("  BlockoutTools Full Export - START")
    unreal.log("=" * 50)

    # Step 1: Find all blockout actors
    actors = get_blockout_actors()
    if not actors:
        unreal.log_warning("[Main] No BlockoutTools actors found in level. Aborting.")
        return

    # Step 2: Extract data from each actor
    actor_data_list = []
    for actor in actors:
        data = extract_actor_data(actor)
        if data is not None:
            actor_data_list.append(data)

    if not actor_data_list:
        unreal.log_warning("[Main] No actor data extracted. Aborting.")
        return

    unreal.log(f"[Main] Extracted data for {len(actor_data_list)} actors")

    # Step 3: Assign floors (marker board system)
    result = assign_floors(actor_data_list)
    if isinstance(result, tuple):
        floors, floor_marker_names = result
    else:
        floors, floor_marker_names = result, set()

    # Step 3.5: Remove floor markers from actor list (they are hidden in viewer)
    if floor_marker_names:
        before = len(actor_data_list)
        actor_data_list = [d for d in actor_data_list if d["name"] not in floor_marker_names]
        unreal.log(f"[Main] Removed {before - len(actor_data_list)} floor marker(s) from output")

    # Clean up internal flags
    for d in actor_data_list:
        d.pop("_isFloorMarker", None)

    # Step 4: Extract paths
    paths = extract_paths(actor_data_list)

    # Step 5: Build output and write
    output = build_output(actor_data_list, paths, floors)
    output_path = write_json(output)

    # Step 6: Summary
    if output_path:
        print_summary(actor_data_list, paths, floors, output_path)
    else:
        unreal.log_error("[Main] Export failed - no output file created")

    unreal.log("  BlockoutTools Full Export - DONE")


# Run
main()
