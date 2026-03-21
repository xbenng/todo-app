"""Job management routes: list, stream, kill."""
import json, time
from flask import Blueprint, request, jsonify, Response
from routes.auth import get_current_user
import state
from services.shell_utils import _kill_process_tree

bp = Blueprint('jobs', __name__)


@bp.route("/api/jobs")
def list_jobs():
    """List all jobs, purging completed/killed entries older than 30 minutes."""
    cutoff = time.time() - 1800
    stale = [jid for jid, j in state._jobs.items()
             if j["status"] in ("done", "error", "killed") and j["created_at"] < cutoff]
    for jid in stale:
        del state._jobs[jid]
    return jsonify([{
        "id": j["id"], "label": j["label"], "job_key": j["job_key"],
        "status": j["status"], "line_count": len(j["output_lines"]), "created_at": j["created_at"],
        "conversation_id": j.get("conversation_id"),
    } for j in state._jobs.values()])


@bp.route("/api/jobs/<job_id>/stream")
def stream_job(job_id):
    """SSE stream of raw output lines for a job."""
    job = state._jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404

    def generate():
        sent = 0
        while True:
            while sent < len(job["output_lines"]):
                yield f"data: {json.dumps(job['output_lines'][sent])}\n\n"
                sent += 1
            if job["status"] in ("done", "error", "killed"):
                done_msg = {'__done__': True, 'status': job['status']}
                if job.get('conversation_id'):
                    done_msg['conversation_id'] = job['conversation_id']
                yield f"data: {json.dumps(done_msg)}\n\n"
                break
            time.sleep(0.05)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/api/jobs/<job_id>/kill", methods=["POST"])
def kill_job(job_id):
    """Cancel a running job (supports both local subprocess and API stream)."""
    job = state._jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    # Cancel API stream if present
    api_stream = job.get("_stream")
    if api_stream:
        try:
            api_stream.close()
        except Exception:
            pass
    # Kill local subprocess if present
    proc = job.get("proc")
    if proc and proc.poll() is None:
        _kill_process_tree(proc.pid)
    job["status"] = "killed"
    return jsonify({"ok": True})
