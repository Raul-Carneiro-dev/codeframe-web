"""
Worker de renderização: recebe HTML/CSS/JS de uma cena + parâmetros,
renderiza frame a frame num Chromium headless (Playwright) e codifica
o vídeo final com ffmpeg. Pensado para rodar em background threads,
atualizando um dicionário de status compartilhado (jobs) para a UI
poder consultar o progresso via polling.
"""

import shutil
import subprocess
import sys
from pathlib import Path

INJECT_HEAD = """
<script>
  window.__fps = {fps};
  window.__totalFrames = {total_frames};
  window.__handlers = [];
  window.onFrame = function(cb) {{ window.__handlers.push(cb); }};
  window.__seek = function(frame) {{
    window.__currentFrame = frame;
    var t = frame / window.__fps;
    window.__handlers.forEach(function(h) {{
      try {{ h(t, frame, window.__totalFrames, window.__fps); }}
      catch (e) {{ console.error(e); }}
    }});
  }};
</script>
"""


def build_page_html(html: str, css: str, js: str, width: int, height: int,
                     fps: int, total_frames: int) -> str:
    head = INJECT_HEAD.format(fps=fps, total_frames=total_frames)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  html,body{{width:{width}px;height:{height}px;overflow:hidden;}}
  {css}
</style>
{head}
</head>
<body>
{html}
<script>
try {{
{js}
}} catch (e) {{ console.error(e); }}
window.__seek(0);
</script>
</body>
</html>"""


def get_ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError(
                "Nem 'imageio-ffmpeg' nem um 'ffmpeg' no PATH foram encontrados. "
                "Rode: pip install imageio-ffmpeg"
            )
        return ffmpeg_bin


def encode_video(frames_dir: Path, output_path: Path, fps: int, crf: int, preset: str):
    ffmpeg_bin = get_ffmpeg_bin()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    cmd = [
        ffmpeg_bin, "-y",
        "-framerate", str(fps),
        "-i", str(frames_dir / "frame_%05d.png"),
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise RuntimeError("Falha no ffmpeg:\n" + result.stdout[-4000:])


def run_render_job(job_id: str, params: dict, jobs: dict, jobs_dir: Path):
    """Roda inteiramente numa thread própria. Atualiza jobs[job_id] com progresso."""
    job = jobs[job_id]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        job["status"] = "error"
        job["message"] = ("O pacote 'playwright' não está instalado no ambiente do servidor. "
                           "Rode: pip install -r requirements.txt && playwright install chromium")
        return

    width = int(params["width"])
    height = int(params["height"])
    fps = int(params["fps"])
    duration = float(params["duration"])
    crf = int(params.get("crf", 18))
    preset = params.get("preset", "medium")
    total_frames = max(1, round(fps * duration))

    job_dir = jobs_dir / job_id
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "output.mp4"

    job["status"] = "rendering"
    job["progress"] = 0
    job["message"] = f"iniciando Chromium headless... ({total_frames} frames)"
    job["total_frames"] = total_frames

    try:
        page_html = build_page_html(params["html"], params["css"], params["js"],
                                     width, height, fps, total_frames)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": height},
                                     device_scale_factor=1)
            page.set_content(page_html, wait_until="load")

            for i in range(total_frames):
                if job.get("cancel"):
                    job["status"] = "cancelled"
                    job["message"] = "cancelado pelo usuário."
                    browser.close()
                    return
                page.evaluate("(frame) => window.__seek(frame)", i)
                page.wait_for_timeout(8)
                page.screenshot(path=str(frames_dir / f"frame_{i:05d}.png"))
                job["progress"] = int(((i + 1) / total_frames) * 70)
                job["message"] = f"capturando frame {i+1}/{total_frames}"

            browser.close()

        job["message"] = "codificando vídeo com ffmpeg..."
        job["progress"] = 72
        encode_video(frames_dir, output_path, fps, crf, preset)

        job["progress"] = 100
        job["status"] = "done"
        job["output"] = str(output_path)
        job["message"] = "concluído."

    except Exception as e:
        job["status"] = "error"
        job["message"] = str(e)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
