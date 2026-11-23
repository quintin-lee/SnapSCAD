"""SnapSCAD: Visual Reverse Engineering & AI CAD Agent (照片/草图/文字 -> 参数化 OpenSCAD 3D 模型)。

公开 API:
  OpenscadAgent(config).build_from_image(images, hint)
  OpenscadAgent(config).build_from_description(text)
"""

from .core import AgentConfig, OpenscadRenderer, preprocess_image, extract_edge_map, SPEC_SCHEMA, generate_html_viewer, upload_to_3d_printer
from .agent import AgentResult, OpenscadAgent
from .vision import AgnesVision, SpecParseError
from .geometry import spec_to_scad, write_spec_and_scad
from .log import setup_logging, get_logger
from .spec import validate_spec, normalize_part
from .doctor import check_printability, PrintabilityReport, generate_print_guide, MATERIAL_PROFILES
from .gallery import generate_gallery
from .packager import package_project, export_scad_library
from .server import serve_assets, stop_server, is_port_in_use, create_app
from .workspace import generate_workspace_html
from .slicer import probe_slicers, slice_model, SlicingConfig, SlicingResult, parse_gcode_metadata
from .dispatcher import PrinterDispatcher, DispatchConfig, DispatchResult
from .blueprint import generate_technical_blueprint
from .kinematics import KinematicJoint, parse_joints, compute_joint_pose
from .fea import (
    estimate_thermal_warping,
    apply_tolerance_compensation,
    compensate_and_recompile_model,
    ThermalWarpingReport,
)
from .multimaterial import export_multimaterial_3mf
from .lattice import (
    generate_honeycomb_scad,
    generate_orthogrid_scad,
    generate_diamond_lattice_scad,
    generate_gyroid_infill_scad,
    estimate_lattice_weight_savings,
    apply_lattice_lightweighting,
    LatticeReport,
)
from .assembly_sop import (
    AssemblyStep,
    AssemblySequencePlan,
    derive_assembly_sequence,
    generate_assembly_animation_scad,
    generate_sop_guide_html,
)
from .interference import (
    AABB3D,
    ClashRecord,
    InterferenceReport,
    compute_part_aabb,
    check_assembly_interference,
)
from .fasteners import (
    FastenerStandard,
    FASTENER_CATALOG,
    generate_bolt_scad,
    generate_nut_scad,
    generate_washer_scad,
    generate_heat_set_insert_scad,
    generate_counterbore_cutter,
    generate_countersink_cutter,
    generate_hex_pocket_cutter,
    generate_heat_set_hole_cutter,
    match_fastener_for_hole,
    generate_fasteners_bom,
    apply_auto_counterbores,
)

__all__ = [
    "AgentConfig",
    "OpenscadAgent",
    "AgentResult",
    "OpenscadRenderer",
    "AgnesVision",
    "SpecParseError",
    "spec_to_scad",
    "write_spec_and_scad",
    "preprocess_image",
    "extract_edge_map",
    "SPEC_SCHEMA",
    "setup_logging",
    "get_logger",
    "validate_spec",
    "normalize_part",
    "generate_html_viewer",
    "check_printability",
    "PrintabilityReport",
    "generate_print_guide",
    "MATERIAL_PROFILES",
    "generate_gallery",
    "generate_workspace_html",
    "generate_technical_blueprint",
    "KinematicJoint",
    "parse_joints",
    "compute_joint_pose",
    "estimate_thermal_warping",
    "apply_tolerance_compensation",
    "compensate_and_recompile_model",
    "ThermalWarpingReport",
    "export_multimaterial_3mf",
    "generate_honeycomb_scad",
    "generate_orthogrid_scad",
    "generate_diamond_lattice_scad",
    "generate_gyroid_infill_scad",
    "estimate_lattice_weight_savings",
    "apply_lattice_lightweighting",
    "LatticeReport",
    "AssemblyStep",
    "AssemblySequencePlan",
    "derive_assembly_sequence",
    "generate_assembly_animation_scad",
    "generate_sop_guide_html",
    "AABB3D",
    "ClashRecord",
    "InterferenceReport",
    "compute_part_aabb",
    "check_assembly_interference",
    "FastenerStandard",
    "FASTENER_CATALOG",
    "generate_bolt_scad",
    "generate_nut_scad",
    "generate_washer_scad",
    "generate_heat_set_insert_scad",
    "generate_counterbore_cutter",
    "generate_countersink_cutter",
    "generate_hex_pocket_cutter",
    "generate_heat_set_hole_cutter",
    "match_fastener_for_hole",
    "generate_fasteners_bom",
    "apply_auto_counterbores",
    "package_project",
    "export_scad_library",
    "upload_to_3d_printer",
    "serve_assets",
    "stop_server",
    "is_port_in_use",
    "create_app",
    "probe_slicers",
    "slice_model",
    "SlicingConfig",
    "SlicingResult",
    "parse_gcode_metadata",
    "PrinterDispatcher",
    "DispatchConfig",
    "DispatchResult",
]
