"""
CodeFrame Web — editor no navegador (localhost) + render real via Playwright.

Rodar:
    python app.py
Depois abra http://127.0.0.1:5000 no navegador.

Tudo roda na sua máquina: o Flask serve a página do editor, e quando você
clica em "Renderizar", o próprio servidor local (não o navegador) abre um
Chromium headless via Playwright, tira os screenshots frame a frame e
codifica o vídeo com ffmpeg. O navegador só é usado para a UI e o preview
ao vivo (que continua sendo renderizado no próprio navegador, só para
feedback visual enquanto você edita).
"""

import threading
import uuid
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template

from render_worker import run_render_job

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)

jobs = {}          # job_id -> dict de status
jobs_lock = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/render", methods=["POST"])
def api_render():
    data = request.get_json(force=True)

    required = ["html", "css", "js", "width", "height", "fps", "duration"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"campos faltando: {', '.join(missing)}"}), 400

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "message": "na fila...", "cancel": False}

    thread = threading.Thread(
        target=run_render_job,
        args=(job_id, data, jobs, JOBS_DIR),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job não encontrado"}), 404
    safe = {k: v for k, v in job.items() if k != "cancel"}
    return jsonify(safe)


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job não encontrado"}), 404
    job["cancel"] = True
    return jsonify({"ok": True})


@app.route("/api/download/<job_id>")
def api_download(job_id):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "vídeo ainda não está pronto"}), 404
    filename = request.args.get("filename", "video") + ".mp4"
    return send_file(job["output"], mimetype="video/mp4",
                      as_attachment=False, download_name=filename)


if __name__ == "__main__":
    print("CodeFrame Web rodando em http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
