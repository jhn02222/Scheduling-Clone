import csv
import io
import json
import threading
import traceback

from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .solver import load_data, build_and_solve, analyze, BLOCK_LABEL, BLOCK_HHMM, CORE_COURSES

# ── In-process job state (fine for local single-user dev) ────────────────────
_JOB = {"status": "idle", "log": [], "results": None, "error": None}
_LOCK = threading.Lock()

DEFAULT_WEIGHTS = {
    "w_skeleton_slot":         8,
    "w_skeleton_bldg":         2,
    "w_dead_gap":             10,
    "under_enroll_threshold": 0.60,
    "w_under_enroll":         12,
    "w_block_over":           18,
    "instructor_max_sections": 3,
    "w_instr_overload":       20,
    "w_lower_midday":          5,
    "w_upper_nonmidday":      15,
    "block_min_pct":          0.15,
    "block_max_pct":          0.30,
    "solver_time":            60,
    "num_options":             3,
}


def index(request):
    return render(request, "optimizer/index.html", {
        "defaults": DEFAULT_WEIGHTS,
        "courses":  CORE_COURSES,
    })


@csrf_exempt
@require_POST
def run_optimizer(request):
    with _LOCK:
        if _JOB["status"] == "running":
            return JsonResponse({"ok": False, "msg": "Already running."})
        _JOB.update(status="running", log=[], results=None, error=None)

    try:
        body    = json.loads(request.body)
        weights = {**DEFAULT_WEIGHTS, **body.get("weights", {})}
    except Exception:
        weights = dict(DEFAULT_WEIGHTS)

    def _run():
        try:
            _log("Loading CSV data...")
            sections, rooms = load_data(settings.SCHEDULE_CSV)

            _log(f"Loaded {len(sections)} sections, {len(rooms)} rooms.")
            _log("Starting optimization...")
            _log("Building CP-SAT model...")

            solutions = build_and_solve(
                sections, rooms, weights,
                solver_time=int(weights.get("solver_time", 60)),
                num_opts=int(weights.get("num_options", 3)),
                log_fn=_log,
            )

            _log("Returned from build_and_solve().")

            if not solutions:
                raise RuntimeError("Solver found no feasible solution.")

            _log(f"Solver returned {len(solutions)} option(s).")

            results = []
            _log("Analyzing solutions...")

            for sol in solutions:
                _log(f"Analyzing {sol['label']}...")
                stats = analyze(sol, sections, rooms, weights)
                results.append({"solution": sol, "stats": stats})
                _log(
                    f"  {sol['label']}: score={sol['score']}, "
                    f"moved={stats['moved']} ({stats['moved_pct']}%), "
                    f"dead_gaps={stats['dead_gap_total']}"
                )

            with _LOCK:
                _JOB["results"] = results
                _JOB["status"] = "done"

            _log("Done.")
        except Exception as exc:
            with _LOCK:
                _JOB["error"] = str(exc)
                _JOB["status"] = "error"
                _JOB["log"].append(f"ERROR: {exc}")
            print(traceback.format_exc())

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"ok": True})


def _log(msg):
    with _LOCK:
        _JOB["log"].append(msg)
    print(msg)


def job_status(request):
    with _LOCK:
        status  = _JOB["status"]
        log     = list(_JOB["log"])
        error   = _JOB["error"]
        results = _JOB["results"]

    payload = {"status": status, "log": log, "error": error}
    if results and status == "done":
        payload["results"] = [
            {"label": r["solution"]["label"],
             "score": r["solution"]["score"],
             "stats": r["stats"]}
            for r in results
        ]
    return JsonResponse(payload)


def export_csv(request):
    with _LOCK:
        results = _JOB.get("results")
    if not results:
        return HttpResponse("No results yet.", status=400)

    opt_idx = min(int(request.GET.get("opt", 0)), len(results)-1)
    result  = results[opt_idx]
    sol     = result["solution"]
    stats   = result["stats"]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Option","Score","CRN","Course","Instructor",
                     "Days","Block","Time","Room","Moved"])
    seen = set()
    for ev in sorted(stats["calendar"], key=lambda e:(e["course"],e["start"],e["crn"])):
        key = (ev["crn"], ev["course"])
        if key in seen: continue
        seen.add(key)
        asgn = sol["assignment"][ev["sid"]]
        b    = asgn["block"]
        writer.writerow([sol["label"], sol["score"], ev["crn"],
                         f"MATH {ev['course']}", ev["instructor"],
                         ev["days"], b, BLOCK_LABEL[b], asgn["room"],
                         "YES" if ev["moved"] else "no"])

    output.seek(0)
    resp = HttpResponse(output, content_type="text/csv")
    resp["Content-Disposition"] = (
        f'attachment; filename="schedule_{sol["label"].replace(" ","_")}.csv"'
    )
    return resp