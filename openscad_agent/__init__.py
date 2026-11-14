"""OpenSCAD Agent 包：图片识别 → OpenSCAD 3D 模型。

公开 API：
  OpenscadAgent(config).build_from_image(image, hint)
  OpenscadAgent(config).build_from_description(text)
"""

from .core import AgentConfig, OpenscadRenderer, preprocess_image, SPEC_SCHEMA
from .agent import AgentResult, OpenscadAgent
from .vision import AgnesVision, SpecParseError
from .geometry import spec_to_scad, write_spec_and_scad

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
    "SPEC_SCHEMA",
]
