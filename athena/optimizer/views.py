import csv
import io
import json
import threading
import traceback

from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required
from .models import SavedSchedule
from .solver import load_data, build_and_solve, analyze, BLOCK_LABEL, BLOCK_HHMM
from django.views.decorators.http import require_http_methods
from .models import Professor, ProfessorPreference, CourseConfig
from django.contrib.auth import logout as auth_logout

# ── In-process job state ─────────────────────────────────────────────────────
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
    "w_prof_pref":             6,
}


def _log(msg):
    with _LOCK:
        _JOB["log"].append(msg)
    print(msg)


@login_required
def index(request):
    return render(request, "optimizer/index.html", {
        "defaults": DEFAULT_WEIGHTS,
    })


@csrf_exempt
@require_POST
def run_optimizer(request):
    with _LOCK:
        if _JOB["status"] == "running":
            return JsonResponse({"ok": False, "msg": "Already running."})
        _JOB.update(status="running", log=[], results=None, error=None)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        with _LOCK:
            _JOB.update(status="error", error=f"Invalid JSON payload: {exc}")
        return JsonResponse({"ok": False, "msg": f"Invalid JSON payload: {exc}"}, status=400)

    if not isinstance(body, dict):
        with _LOCK:
            _JOB.update(status="error", error="Request body must be a JSON object.")
        return JsonResponse({"ok": False, "msg": "Request body must be a JSON object."}, status=400)

    user_weights = body.get("weights", {})
    if user_weights is None:
        user_weights = {}
    if not isinstance(user_weights, dict):
        with _LOCK:
            _JOB.update(status="error", error="weights must be a JSON object.")
        return JsonResponse({"ok": False, "msg": "weights must be a JSON object."}, status=400)

    weights = {**DEFAULT_WEIGHTS, **user_weights}

    def _run():
        try:
            data_source = getattr(settings, "SCHEDULE_DATA_SOURCE", "db").lower()
            if data_source == "db":
                _log("Loading DB data...")
                sections, rooms = load_data(
                    source="db",
                    db_path=settings.DATABASES["default"]["NAME"],
                    semester=getattr(settings, "SCHEDULE_SEMESTER", "202602"),
                    course_scope=getattr(settings, "SCHEDULE_COURSE_SCOPE", "all_math"),
                )
            elif data_source == "csv":
                _log("Loading CSV data...")
                sections, rooms = load_data(
                    source="csv",
                    csv_path=settings.SCHEDULE_CSV,
                    course_scope=getattr(settings, "SCHEDULE_COURSE_SCOPE", "all_math"),
                )
            else:
                raise ValueError(
                    f"Unsupported SCHEDULE_DATA_SOURCE '{data_source}'. Use 'db' or 'csv'."
                )

            _log(f"Loaded {len(sections)} sections, {len(rooms)} rooms.")
            courses = set(s["course"] for s in sections)
            _log(f"Courses: {sorted(courses)}")
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

    opt_idx = min(int(request.GET.get("opt", 0)), len(results) - 1)
    result  = results[opt_idx]
    sol     = result["solution"]
    stats   = result["stats"]

    # Normalize assignment keys — JSON round-trip converts int keys to strings
    assignment = {int(k): v for k, v in sol["assignment"].items()}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Option", "Score", "CRN", "Course", "Instructor",
                     "Days", "Block", "Time", "Room", "Moved"])
    seen = set()
    for ev in sorted(stats["calendar"], key=lambda e: (e["course"], e["start"], e["crn"])):
        key = (ev["crn"], ev["course"])
        if key in seen:
            continue
        seen.add(key)
        asgn = assignment.get(ev["sid"])
        if not asgn:
            continue
        b = asgn["block"]
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


def register(request):
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("index")
    else:
        form = UserCreationForm()
    return render(request, "optimizer/register.html", {"form": form})


@csrf_exempt
@require_POST
@login_required
def save_schedule(request):
    with _LOCK:
        results = _JOB.get("results")
    if not results:
        return JsonResponse({"ok": False, "msg": "No results to save."})

    body = json.loads(request.body or b"{}")
    opt_idx = min(int(body.get("opt", 0)), len(results) - 1)
    name = body.get("name", f"Schedule {opt_idx + 1}")
    result = results[opt_idx]

    SavedSchedule.objects.create(
        user=request.user,
        name=name,
        semester=getattr(settings, "SCHEDULE_SEMESTER", "202602"),
        solution_data=result["solution"],
        stats_data=result["stats"],
        score=result["solution"]["score"],
    )

    # Keep only last 10 per user
    all_schedules = SavedSchedule.objects.filter(user=request.user).order_by('-created_at')
    if all_schedules.count() > 10:
        ids_to_delete = list(all_schedules.values_list('id', flat=True)[10:])
        SavedSchedule.objects.filter(id__in=ids_to_delete).delete()

    return JsonResponse({"ok": True})


@login_required
def schedules_json(request):
    schedules = SavedSchedule.objects.filter(user=request.user).order_by('-created_at')[:10]
    data = []
    for s in schedules:
        stats = s.stats_data or {}
        data.append({
            'id': s.id,
            'name': s.name,
            'semester': s.semester,
            'score': s.score,
            'created_at': s.created_at.strftime('%b %d, %Y %I:%M %p'),
            'total_sections': stats.get('total', 0),
            'moved': stats.get('moved', 0),
            'moved_pct': stats.get('moved_pct', 0),
            'dead_minutes': stats.get('total_dead_minutes', 0),
            'under_count': stats.get('under_count', 0),
            'bldg_changes': stats.get('bldg_changes', 0),
        })
    return JsonResponse({'schedules': data})


@login_required
def load_schedule(request, schedule_id):
    try:
        s = SavedSchedule.objects.get(id=schedule_id, user=request.user)
        
        # Rebuild a result object so export_csv can find it
        result = {
            "solution": {
                "label": s.name,
                "score": s.score,
                "assignment": s.solution_data.get("assignment", {}),
            },
            "stats": s.stats_data,
        }
        with _LOCK:
            _JOB["results"] = [result]
            _JOB["status"] = "done"
        
        return JsonResponse({
            'ok': True,
            'label': s.name,
            'score': s.score,
            'stats': s.stats_data,
        })
    except SavedSchedule.DoesNotExist:
        return JsonResponse({'ok': False, 'msg': 'Schedule not found.'}, status=404)


@login_required
@require_POST
def delete_schedule(request, schedule_id):
    SavedSchedule.objects.filter(id=schedule_id, user=request.user).delete()
    return JsonResponse({'ok': True})


@login_required
@require_POST
def professor_delete(request, prof_id):
    Professor.objects.filter(id=prof_id).delete()
    return redirect('professors')

@login_required
def professors_json(request):
    profs = Professor.objects.all().order_by('last_name', 'first_name')
    data = []
    for p in profs:
        try:
            pref = p.preference
            pref_data = {
                'tenured': pref.tenured,
                'time_of_day': pref.time_of_day,
                'day_pattern': pref.day_pattern,
                'level_preference': pref.level_preference,
                'max_sections': pref.max_sections,
                'avoid_back_to_back': pref.avoid_back_to_back,
            }
        except ProfessorPreference.DoesNotExist:
            pref_data = None
        data.append({
            'id': p.id,
            'first_name': p.first_name,
            'last_name': p.last_name,
            'is_active': p.is_active,
            'preference': pref_data,
        })
    return JsonResponse({'professors': data})


@login_required
def professor_pref_json(request, prof_id):
    prof = Professor.objects.get(id=prof_id)
    try:
        pref = prof.preference
        pref_data = {
            'tenured': pref.tenured,
            'time_of_day': pref.time_of_day,
            'day_pattern': pref.day_pattern,
            'level_preference': pref.level_preference,
            'max_sections': pref.max_sections,
            'avoid_back_to_back': pref.avoid_back_to_back,
        }
    except ProfessorPreference.DoesNotExist:
        pref_data = None
    return JsonResponse({'preference': pref_data})


@login_required
@csrf_exempt
@require_POST
def professor_save_pref(request, prof_id):
    prof = Professor.objects.get(id=prof_id)
    body = json.loads(request.body)
    try:
        pref = prof.preference
    except ProfessorPreference.DoesNotExist:
        pref = ProfessorPreference(professor=prof)
    pref.tenured             = body.get('tenured', 'unknown')
    pref.time_of_day         = body.get('time_of_day', 'any')
    pref.day_pattern         = body.get('day_pattern', 'any')
    pref.level_preference    = body.get('level_preference', 'any')
    pref.max_sections        = body.get('max_sections', 'any')
    pref.avoid_back_to_back  = body.get('avoid_back_to_back', False)
    pref.save()
    return JsonResponse({'ok': True})


@login_required
@csrf_exempt  
@require_POST
def professor_add_json(request):
    body = json.loads(request.body)
    first = body.get('first_name', '').strip()
    last  = body.get('last_name', '').strip()
    if first and last:
        Professor.objects.get_or_create(first_name=first, last_name=last)
    return JsonResponse({'ok': True})

@login_required
@csrf_exempt
@require_POST
def professor_toggle_active(request, prof_id):
    prof = Professor.objects.get(id=prof_id)
    prof.is_active = not prof.is_active
    prof.save()
    return JsonResponse({'ok': True, 'is_active': prof.is_active})
@login_required
def courses_json(request):
    """Return all CourseConfig rows as JSON for the Courses tab."""
    from .models import CourseConfig
    configs = CourseConfig.objects.all().order_by('course_number')
    data = []
    for c in configs:
        data.append({
            'id':                 c.id,
            'course_number':      c.course_number,
            'display_name':       c.display_name,
            'is_active':          c.is_active,
            'min_sections':       c.min_sections,
            'max_sections':       c.max_sections,
            'banned_blocks':      c.banned_blocks,
            'max_per_block':      c.max_per_block,
            'preferred_building': c.preferred_building,
            'required_room_type': c.required_room_type,
            'min_room_capacity':  c.min_room_capacity,
        })
    return JsonResponse({'courses': data})
 
 
@login_required
@csrf_exempt
@require_POST
def course_add(request):
    """Create or update a CourseConfig row."""
    from .models import CourseConfig
    body = json.loads(request.body)
    cn = int(body.get('course_number', 0))
    if not cn:
        return JsonResponse({'ok': False, 'msg': 'course_number required'}, status=400)
 
    obj, _ = CourseConfig.objects.get_or_create(course_number=cn)
    obj.display_name       = body.get('display_name', obj.display_name)
    obj.is_active          = body.get('is_active', obj.is_active)
    obj.min_sections       = body.get('min_sections') or None
    obj.max_sections       = body.get('max_sections') or None
    obj.banned_blocks      = body.get('banned_blocks', '')
    obj.max_per_block      = body.get('max_per_block') or None
    obj.preferred_building = body.get('preferred_building', '')
    obj.required_room_type = body.get('required_room_type', 'any')
    obj.min_room_capacity  = body.get('min_room_capacity') or None
    obj.save()
    return JsonResponse({'ok': True, 'id': obj.id})
 
 
@login_required
@csrf_exempt
@require_POST
def course_toggle_active(request, course_id):
    """Toggle is_active for a CourseConfig."""
    from .models import CourseConfig
    try:
        c = CourseConfig.objects.get(id=course_id)
        c.is_active = not c.is_active
        c.save()
        return JsonResponse({'ok': True, 'is_active': c.is_active})
    except CourseConfig.DoesNotExist:
        return JsonResponse({'ok': False, 'msg': 'Not found'}, status=404)
 
 
@login_required
@csrf_exempt
@require_POST
def course_delete(request, course_id):
    """Delete a CourseConfig row."""
    from .models import CourseConfig
    CourseConfig.objects.filter(id=course_id).delete()
    return JsonResponse({'ok': True})
 
 
@login_required
def course_detail_json(request, course_id):
    """Return a single CourseConfig as JSON."""
    from .models import CourseConfig
    try:
        c = CourseConfig.objects.get(id=course_id)
        return JsonResponse({
            'id':                 c.id,
            'course_number':      c.course_number,
            'display_name':       c.display_name,
            'is_active':          c.is_active,
            'min_sections':       c.min_sections,
            'max_sections':       c.max_sections,
            'banned_blocks':      c.banned_blocks,
            'max_per_block':      c.max_per_block,
            'preferred_building': c.preferred_building,
            'required_room_type': c.required_room_type,
            'min_room_capacity':  c.min_room_capacity,
        })
    except CourseConfig.DoesNotExist:
        return JsonResponse({'ok': False}, status=404)
 
def logout_view(request):
    auth_logout(request)
    return redirect('/login/')


@login_required
@csrf_exempt
@require_POST
def save_editor(request):
    """Save manual editor overrides to a SavedSchedule row."""
    with _LOCK:
        results = _JOB.get("results")

    body = json.loads(request.body or b"{}")
    name = body.get("name", f"Edited Schedule {__import__('datetime').date.today()}")
    editor_data = body.get("editor_data", {})
    # sections_data is the full editor section list (for CSV export)
    sections_snapshot = body.get("sections_snapshot", [])

    # If there's a current result in memory, use it as base
    if results:
        result = results[0]
        solution_data = result["solution"]
        stats_data = result["stats"]
        score = result["solution"].get("score", 0)
    else:
        # No current result — load from existing saved if schedule_id provided
        schedule_id = body.get("schedule_id")
        if schedule_id:
            try:
                existing = SavedSchedule.objects.get(id=schedule_id, user=request.user)
                solution_data = existing.solution_data
                stats_data = existing.stats_data
                score = existing.score or 0
            except SavedSchedule.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Base schedule not found'}, status=404)
        else:
            return JsonResponse({'ok': False, 'msg': 'No schedule loaded'}, status=400)

    # Store editor_data alongside solution_data
    saved = SavedSchedule.objects.create(
        user=request.user,
        name=name,
        semester=getattr(settings, "SCHEDULE_SEMESTER", "202602"),
        solution_data=solution_data,
        stats_data=stats_data,
        score=score,
        editor_data={"overrides": editor_data, "sections_snapshot": sections_snapshot},
        is_edited=True,
    )

    # Keep only last 10 per user
    all_schedules = SavedSchedule.objects.filter(user=request.user).order_by('-created_at')
    if all_schedules.count() > 10:
        ids_to_delete = list(all_schedules.values_list('id', flat=True)[10:])
        SavedSchedule.objects.filter(id__in=ids_to_delete).delete()

    return JsonResponse({'ok': True, 'id': saved.id})


@login_required
def load_editor(request, schedule_id):
    """Load a saved schedule including its editor overrides."""
    try:
        s = SavedSchedule.objects.get(id=schedule_id, user=request.user)
        editor_data = s.editor_data or {}
        return JsonResponse({
            'ok': True,
            'label': s.name,
            'score': s.score,
            'stats': s.stats_data,
            'is_edited': s.is_edited,
            'editor_overrides': editor_data.get('overrides', {}),
            'sections_snapshot': editor_data.get('sections_snapshot', []),
        })
    except SavedSchedule.DoesNotExist:
        return JsonResponse({'ok': False, 'msg': 'Schedule not found.'}, status=404)


def export_editor_csv(request):
    """Export the manually edited schedule as CSV."""
    # Get editor sections from POST body or from saved schedule
    if request.method == 'POST':
        body = json.loads(request.body or b'{}')
        sections = body.get('sections', [])
        label = body.get('label', 'Edited Schedule')
        score = body.get('score', '')
    else:
        return HttpResponse("Use POST", status=405)

    BLOCK_TIMES = {
        0: '8:15 AM', 1: '9:55 AM', 2: '11:35 AM',
        3: '1:15 PM', 4: '2:55 PM', 5: '4:35 PM'
    }

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Schedule", "Score", "CRN", "Course", "Instructor",
                     "Days", "Block", "Time", "Room", "Duration", "Capacity", "Note"])

    seen = set()
    for sec in sorted(sections, key=lambda s: (s.get('course', 0), s.get('block', 0))):
        key = (sec.get('crn'), sec.get('course'))
        if key in seen:
            continue
        seen.add(key)
        block = sec.get('block', 0)
        writer.writerow([
            label,
            score,
            sec.get('crn', ''),
            f"MATH {sec.get('course', '')}",
            sec.get('instructor', 'TBA'),
            sec.get('days', ''),
            block,
            BLOCK_TIMES.get(int(block), ''),
            sec.get('room', ''),
            sec.get('duration', ''),
            sec.get('capacity', ''),
            sec.get('note', ''),
        ])

    output.seek(0)
    resp = HttpResponse(output, content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="edited_schedule.csv"'
    return resp