"""3D 模型资产展厅画廊生成器 (Showcase Gallery)。

扫描输出目录，自动生成现代化、响应式的便携式 gallery.html：
- 聚合所有生成的 3D 模型卡片（缩略图、工程名、规格描述、部件数、包围盒尺寸、适印性体检分）
- 全局统计指标看板（模型总数、部件总数、打印就绪率）
- 前端实时模糊搜索与标签过滤
- 一键跳转各模型专属的 3D 网页交互查看器，提供 STL、3MF、SCAD 资源一键下载
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from .doctor import check_printability
from .log import get_logger as _log_module

_log = _log_module()


def generate_gallery(output_dir: str | Path, html_filename: str = "gallery.html") -> Path:
    """扫描指定目录下的全部模型产物，生成独立的 3D 展厅画廊网页。"""
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    gallery_file = out_dir / html_filename

    spec_files = sorted(out_dir.glob("*_spec.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    models: list[dict[str, Any]] = []

    total_parts = 0
    passed_print = 0

    for sf in spec_files:
        try:
            spec = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            continue

        name = spec.get("name") or sf.stem.replace("_spec", "")
        desc = spec.get("description", "")
        parts = spec.get("parts") or []
        total_parts += len(parts)

        # 关联文件
        stem = sf.stem.replace("_spec", "")
        preview_file = out_dir / f"{stem}_preview.png"
        viewer_file = out_dir / f"{stem}_viewer.html"
        stl_file = out_dir / f"{stem}.stl"
        mf_file = out_dir / f"{stem}.3mf"
        scad_file = out_dir / f"{stem}.scad"

        doc = check_printability(spec)
        if doc.printable:
            passed_print += 1

        models.append({
            "name": name,
            "stem": stem,
            "desc": desc,
            "parts_count": len(parts),
            "mtime": datetime.datetime.fromtimestamp(sf.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "doc_score": doc.score,
            "doc_summary": doc.summary(),
            "bbox": f"{doc.bounding_box[0]} × {doc.bounding_box[1]} × {doc.bounding_box[2]} mm",
            "has_preview": preview_file.exists(),
            "preview_url": preview_file.name if preview_file.exists() else "",
            "has_viewer": viewer_file.exists(),
            "viewer_url": viewer_file.name if viewer_file.exists() else "",
            "has_stl": stl_file.exists(),
            "stl_url": stl_file.name if stl_file.exists() else "",
            "has_3mf": mf_file.exists(),
            "mf_url": mf_file.name if mf_file.exists() else "",
            "has_scad": scad_file.exists(),
            "scad_url": scad_file.name if scad_file.exists() else "",
        })

    model_count = len(models)
    pass_rate = round((passed_print / model_count * 100), 1) if model_count else 100.0
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # 卡片 HTML
    cards_html = []
    for m in models:
        badge_cls = "badge-pass" if m["doc_score"] >= 90 else ("badge-warn" if m["doc_score"] >= 70 else "badge-risk")
        img_tag = (f'<img src="{m["preview_url"]}" alt="{m["name"]}" class="card-thumb">'
                   if m["has_preview"] else
                   '<div class="card-thumb-placeholder">3D 模型</div>')

        btn_viewer = f'<a href="{m["viewer_url"]}" target="_blank" class="btn btn-primary">🌐 3D 旋转检视</a>' if m["has_viewer"] else ''
        btn_stl = f'<a href="{m["stl_url"]}" download class="btn">⬇️ STL</a>' if m["has_stl"] else ''
        btn_3mf = f'<a href="{m["mf_url"]}" download class="btn">⬇️ 3MF</a>' if m["has_3mf"] else ''
        btn_scad = f'<a href="{m["scad_url"]}" download class="btn">💻 SCAD</a>' if m["has_scad"] else ''

        cards_html.append(f"""
    <div class="card" data-name="{m['name'].lower()}" data-desc="{m['desc'].lower()}">
      <div class="card-img-wrap">
        {img_tag}
        <span class="badge {badge_cls}">{m['doc_score']}分</span>
      </div>
      <div class="card-body">
        <h3 class="card-title">{m['name']}</h3>
        <p class="card-desc">{m['desc'] or 'OpenSCAD Agent 自动生成三维模型'}</p>
        <div class="card-meta">
          <span>🧩 {m['parts_count']} 部件</span>
          <span>📐 {m['bbox']}</span>
        </div>
        <div class="card-actions">
          {btn_viewer}
          {btn_stl}
          {btn_3mf}
          {btn_scad}
        </div>
      </div>
    </div>""")

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenSCAD Agent 3D 模型展厅画廊</title>
<style>
  :root {{
    --bg: #0f172a;
    --card-bg: #1e293b;
    --text: #f8fafc;
    --muted: #94a3b8;
    --primary: #2563eb;
    --primary-hover: #1d4ed8;
    --accent: #38bdf8;
    --border: rgba(255, 255, 255, 0.1);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: radial-gradient(circle at 50% 0%, #1e293b 0%, var(--bg) 100%);
    color: var(--text);
    font-family: system-ui, -apple-system, sans-serif;
    min-height: 100vh;
    padding-bottom: 60px;
  }}
  header {{
    padding: 40px 24px 24px;
    text-align: center;
    max-width: 1200px;
    margin: 0 auto;
  }}
  header h1 {{
    font-size: 32px;
    font-weight: 800;
    background: linear-gradient(135deg, #38bdf8 0%, #818cf8 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 8px;
  }}
  header p {{
    color: var(--muted);
    font-size: 14px;
  }}
  .stats-bar {{
    display: flex;
    justify-content: center;
    gap: 16px;
    margin: 28px 0;
    flex-wrap: wrap;
  }}
  .stat-card {{
    background: rgba(30, 41, 59, 0.7);
    border: 1px solid var(--border);
    backdrop-filter: blur(8px);
    border-radius: 12px;
    padding: 14px 24px;
    min-width: 140px;
    text-align: center;
  }}
  .stat-num {{
    font-size: 26px;
    font-weight: 800;
    color: var(--accent);
  }}
  .stat-label {{
    font-size: 12px;
    color: var(--muted);
    margin-top: 2px;
  }}
  .controls {{
    max-width: 1200px;
    margin: 0 auto 30px;
    padding: 0 24px;
    display: flex;
    gap: 12px;
    justify-content: center;
  }}
  .search-input {{
    width: 100%;
    max-width: 420px;
    background: var(--card-bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 10px 16px;
    border-radius: 25px;
    font-size: 14px;
    outline: none;
    transition: all 0.2s;
  }}
  .search-input:focus {{
    border-color: var(--accent);
    box-shadow: 0 0 15px rgba(56, 189, 248, 0.2);
  }}
  .grid {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 0 24px;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 24px;
  }}
  .card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 14px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: transform 0.2s, box-shadow 0.2s;
  }}
  .card:hover {{
    transform: translateY(-4px);
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.4);
    border-color: rgba(56, 189, 248, 0.4);
  }}
  .card-img-wrap {{
    position: relative;
    width: 100%;
    height: 180px;
    background: #0f172a;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }}
  .card-thumb {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.3s;
  }}
  .card:hover .card-thumb {{
    transform: scale(1.04);
  }}
  .card-thumb-placeholder {{
    color: var(--muted);
    font-size: 13px;
    font-weight: 600;
  }}
  .badge {{
    position: absolute;
    top: 10px;
    right: 10px;
    font-size: 11px;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 12px;
    backdrop-filter: blur(4px);
  }}
  .badge-pass {{ background: rgba(22, 163, 74, 0.85); color: #fff; }}
  .badge-warn {{ background: rgba(234, 179, 8, 0.85); color: #000; }}
  .badge-risk {{ background: rgba(239, 68, 68, 0.85); color: #fff; }}
  .card-body {{
    padding: 16px;
    display: flex;
    flex-direction: column;
    flex: 1;
  }}
  .card-title {{
    font-size: 16px;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 6px;
    word-break: break-all;
  }}
  .card-desc {{
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 12px;
    line-height: 1.4;
    min-height: 34px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  .card-meta {{
    font-size: 11px;
    color: #cbd5e1;
    display: flex;
    justify-content: space-between;
    margin-bottom: 14px;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
    padding-top: 8px;
  }}
  .card-actions {{
    margin-top: auto;
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }}
  .btn {{
    font-size: 11px;
    font-weight: 600;
    color: var(--text);
    background: #334155;
    text-decoration: none;
    padding: 6px 10px;
    border-radius: 6px;
    transition: background 0.2s;
    display: inline-flex;
    align-items: center;
  }}
  .btn:hover {{ background: #475569; }}
  .btn-primary {{
    background: var(--primary);
    color: #fff;
    flex: 1;
    justify-content: center;
  }}
  .btn-primary:hover {{ background: var(--primary-hover); }}
  footer {{
    text-align: center;
    color: var(--muted);
    font-size: 12px;
    margin-top: 50px;
  }}
</style>
</head>
<body>
<header>
  <h1>OpenSCAD Agent 3D 展厅画廊</h1>
  <p>AI 驱动的多模态 3D CAD 工程资产库 · 生成时间: {now_str}</p>
  <div class="stats-bar">
    <div class="stat-card">
      <div class="stat-num">{model_count}</div>
      <div class="stat-label">工程模型总数</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{total_parts}</div>
      <div class="stat-label">三维实体部件</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{pass_rate}%</div>
      <div class="stat-label">切片适印就绪率</div>
    </div>
  </div>
</header>

<div class="controls">
  <input type="text" id="search" class="search-input" placeholder="🔍 实时搜索模型名称或中英文描述…">
</div>

<div class="grid" id="grid">
  {''.join(cards_html) if cards_html else '<p style="grid-column:1/-1;text-align:center;color:#64748b;padding:40px;">暂无历史模型，请先完成一次建模</p>'}
</div>

<footer>
  由 OpenSCAD AI Agent 自动归档构建
</footer>

<script>
const search = document.getElementById('search');
const cards = document.querySelectorAll('.card');

search.addEventListener('input', (e) => {{
  const query = e.target.value.toLowerCase().trim();
  cards.forEach(c => {{
    const name = c.dataset.name || '';
    const desc = c.dataset.desc || '';
    const match = name.includes(query) || desc.includes(query);
    c.style.display = match ? 'flex' : 'none';
  }});
}});
</script>
</body>
</html>"""

    gallery_file.write_text(html_content, encoding="utf-8")
    _log.info("[gallery] 3D 模型展厅画廊已生成: %s (包含 %d 个模型)", gallery_file, model_count)
    return gallery_file


__all__ = ["generate_gallery"]
