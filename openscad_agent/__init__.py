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
from .packager import package_project
from .server import serve_assets, stop_server, is_port_in_use
from .slicer import probe_slicers, slice_model, SlicingConfig, SlicingResult, parse_gcode_metadata
from .dispatcher import PrinterDispatcher, DispatchConfig, DispatchResult

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
    "package_project",
    "upload_to_3d_printer",
    "serve_assets",
    "stop_server",
    "is_port_in_use",
    "probe_slicers",
    "slice_model",
    "SlicingConfig",
    "SlicingResult",
    "parse_gcode_metadata",
    "PrinterDispatcher",
    "DispatchConfig",
    "DispatchResult",
]
