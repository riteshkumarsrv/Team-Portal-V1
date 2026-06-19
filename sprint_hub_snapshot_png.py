"""
PNG snapshot of the Sprint hub / sprint team view for embedding in Excel.
Simplified dark layout (metrics + burndown + per-member cards) using Pillow only.
"""

from __future__ import annotations

import io
import re
from typing import Any, Sequence

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[misc, assignment]
    ImageDraw = None  # type: ignore[misc, assignment]
    ImageFont = None  # type: ignore[misc, assignment]

_BG = (15, 23, 42)
_PANEL = (30, 41, 59)
_FG = (248, 250, 252)
_SUB = (148, 163, 184)
_IDEAL = (96, 165, 250)
_ACTUAL = (248, 113, 113)
_ACCENT = (45, 212, 191)
_BORDER = (71, 85, 105)


def _font(size: int) -> Any:
    if ImageFont is None:
        return None
    for path in (
        "arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _parse_svg_ml_path(path: str) -> list[tuple[float, float]]:
    if not path or not path.strip():
        return []
    pts: list[tuple[float, float]] = []
    for m in re.finditer(r"([ML])\s*([\d.-]+)\s*,\s*([\d.-]+)", path, re.I):
        pts.append((float(m.group(2)), float(m.group(3))))
    return pts


def build_sprint_hub_snapshot_png_bytes(
    *,
    team_name: str,
    sprint_name: str,
    sprint_start: str,
    sprint_end: str,
    capacity_h: float | None,
    bd_ctx: dict[str, Any],
    member_rows: Sequence[dict[str, Any]],
    max_members: int = 12,
) -> bytes | None:
    """
    Render a tall PNG similar in content to the Sprint team hub (burndown + member overview).
    Returns None if Pillow is unavailable.
    """
    if Image is None or ImageDraw is None:
        return None

    W = 1100
    pad = 28
    y = pad
    title_font = _font(28)
    head_font = _font(18)
    body_font = _font(15)
    cap_txt = f" ({capacity_h:.1f}h)" if capacity_h is not None else ""
    title = f"{sprint_name}{cap_txt}"
    sub = f"{sprint_start} → {sprint_end}  ·  {team_name or 'Team'}"

    members = list(member_rows)[:max_members]
    row_h = 132
    chart_h = 220 if bd_ctx.get("burndown_has_chart") else 72
    est_h = (
        pad * 2
        + 110
        + chart_h
        + 40
        + len(members) * row_h
        + pad
    )
    img = Image.new("RGB", (W, est_h), _BG)
    draw = ImageDraw.Draw(img)

    def tx(x: int, yy: int, t: str, fill: tuple[int, int, int] = _FG, font: Any = body_font) -> None:
        draw.text((x, yy), t, fill=fill, font=font)

    tx(pad, y, title, font=title_font)
    y += 38
    tx(pad, y, sub, fill=_SUB, font=head_font)
    y += 32

    # Burndown summary + mini chart
    box = (pad, y, W - pad, y + chart_h)
    draw.rounded_rectangle(box, radius=12, fill=_PANEL, outline=_BORDER, width=1)
    inner_x = pad + 16
    inner_y = y + 16
    if bd_ctx.get("burndown_has_chart"):
        tot = float(bd_ctx.get("burndown_total_hours") or 0)
        rem = float(bd_ctx.get("burndown_remaining_hours") or 0)
        tx(inner_x, inner_y, f"SCOPE (START): {tot:.1f}h", font=head_font)
        tx(inner_x + 320, inner_y, f"REMAINING: {rem:.1f}h", fill=_ACCENT, font=head_font)
        inner_y += 36
        tx(inner_x, inner_y, "SPRINT BURNDOWN  (ideal vs remaining)", fill=_SUB, font=small_font)
        inner_y += 26
        cw = W - 2 * pad - 32
        ch = chart_h - 70
        chart_left = inner_x
        chart_top = inner_y
        svg_w = float(bd_ctx.get("burndown_svg_w") or 520)
        svg_h = float(bd_ctx.get("burndown_svg_h") or 228)

        def sx(px: float) -> int:
            return int(chart_left + (px / svg_w) * cw)

        def sy(py: float) -> int:
            return int(chart_top + (py / svg_h) * ch)

        ideal_pts = _parse_svg_ml_path(str(bd_ctx.get("burndown_ideal_d") or ""))
        if len(ideal_pts) >= 2:
            for i in range(len(ideal_pts) - 1):
                draw.line([sx(ideal_pts[i][0]), sy(ideal_pts[i][1]), sx(ideal_pts[i + 1][0]), sy(ideal_pts[i + 1][1])], fill=_IDEAL, width=3)
        act_pts = _parse_svg_ml_path(str(bd_ctx.get("burndown_actual_d") or ""))
        if len(act_pts) >= 2:
            for i in range(len(act_pts) - 1):
                draw.line([sx(act_pts[i][0]), sy(act_pts[i][1]), sx(act_pts[i + 1][0]), sy(act_pts[i + 1][1])], fill=_ACTUAL, width=3)
        for d in bd_ctx.get("burndown_actual_dots") or []:
            try:
                cx, cy = float(d["x"]), float(d["y"])
                r = 5
                ax, ay = sx(cx), sy(cy)
                draw.ellipse((ax - r, ay - r, ax + r, ay + r), fill=_ACTUAL, outline=_FG, width=1)
            except (TypeError, KeyError, ValueError):
                pass
        tx(chart_left, chart_top + ch + 6, "Blue = ideal  ·  Red = remaining effort", fill=_SUB, font=small_font)
    else:
        tx(inner_x, inner_y, str(bd_ctx.get("burndown_message") or "No burndown chart."), fill=_SUB, font=body_font)

    y = box[3] + 24

    tx(pad, y, "TEAM OVERVIEW", font=head_font)
    y += 36

    for m in members:
        card = (pad, y, W - pad, y + row_h - 8)
        draw.rounded_rectangle(card, radius=10, fill=_PANEL, outline=_BORDER, width=1)
        cx, cy = pad + 16, y + 14
        nm = str(m.get("name") or "")
        tx(cx, cy, nm, font=head_font)
        est = float(m.get("est_total_hours") or 0)
        com = float(m.get("committed_total_hours") or 0)
        pp = m.get("progress_pct")
        if pp is not None:
            burnt = f"{float(pp):.0f}% Sprint burnt ({com:.1f}/{est:.1f} h)"
        else:
            burnt = "Sprint burnt —"
        tx(cx + min(420, W - pad - 420), cy, burnt, fill=_ACCENT, font=head_font)
        cy += 30
        ndy = m.get("burn_kind_ndy_pct")
        fsy = m.get("burn_kind_fsy_pct")
        code = m.get("burn_kind_code_pct")
        imp = m.get("burn_kind_improvement_pct")
        pt = m.get("burn_kind_process_tools_pct")

        def _kind_burn_hrs(com_k: str, est_k: str) -> str:
            com = float(m.get(com_k) or 0)
            est = float(m.get(est_k) or 0)
            return f"({com:.1f}/{est:.1f} h)"

        def _kind_seg(label: str, pct: object, com_k: str, est_k: str) -> str:
            hrs = _kind_burn_hrs(com_k, est_k)
            if pct is not None:
                return f"{label} {float(pct):.0f}% burnt {hrs}"
            return f"{label} — {hrs}"

        line_a = (
            _kind_seg("NDY", ndy, "burn_kind_ndy_com", "burn_kind_ndy_est")
            + "   ·   "
            + _kind_seg("FSY", fsy, "burn_kind_fsy_com", "burn_kind_fsy_est")
            + "   ·   "
            + _kind_seg("CODE", code, "burn_kind_code_com", "burn_kind_code_est")
        )
        line_b = (
            _kind_seg("Imp", imp, "burn_kind_improvement_com", "burn_kind_improvement_est")
            + "   ·   "
            + _kind_seg("P&T", pt, "burn_kind_process_tools_com", "burn_kind_process_tools_est")
        )
        tx(cx, cy, line_a, fill=_SUB, font=small_font)
        cy += 20
        tx(cx, cy, line_b, fill=_SUB, font=small_font)
        cy += 22
        cnt = m.get("counts") or {}
        q = f"QUEUE  B {cnt.get('backlog', 0)}  ·  D {cnt.get('do', 0)}  ·  Dg {cnt.get('doing', 0)}  ·  Dn {cnt.get('done', 0)}"
        tx(cx, cy, q, font=small_font)
        cy += 22
        tx(cx, cy, f"Est. {est:.1f}h   ·   Logged {com:.1f}h", font=small_font)
        cy += 22
        doing = m.get("doing_preview") or []
        if doing:
            tx(cx, cy, "In progress: " + "; ".join(str(t) for t in doing[:4]), fill=_FG, font=small_font)
            cy += 20
        notes = m.get("last_notes") or []
        for ni, note in enumerate(notes[:3]):
            ts = str(note.get("created_at") or "")[:10]
            ch = float(note.get("committed_hours") or 0)
            tit = (note.get("title") or "").strip()
            body = (note.get("body") or "").strip().replace("\n", " ")[:80]
            snippet = body if body else tit
            tx(cx, cy, f"{ts}  +{ch:.1f}h  {snippet}", fill=_SUB, font=small_font)
            cy += 18
        y += row_h

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()


def sanitize_excel_sheet_title(name: str) -> str:
    """Excel worksheet name: max 31 chars, no : \\ / ? * [ ]"""
    s = (name or "").strip() or "Sprint"
    s = re.sub(r'[\[\]:*?/\\]', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:31]
