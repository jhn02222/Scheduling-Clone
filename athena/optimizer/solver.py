"""
Core CP-SAT solver logic — decoupled from Django views.
All constraint weights are passed in at runtime via the `weights` dict.

Pattern-based assignment model
────────────────────────────────
Each section is assigned a (pattern, room, block) triple.

Patterns are built from:
  - Duration: 55 or 80 minutes ONLY (institutional rule)
  - Days: any valid combo of MTWRF (TR, MWF, MW, TWR, MTR, TRF, etc.)
  - Credit-hour rules filter which (days × duration) pairs are valid

This gives ~20 patterns total, but each section only gets the subset
that satisfies its credit-hour constraints.

Memory optimisations
────────────────────
- Eligible rooms capped to N smallest adequate per section (default 20)
- HC-4 + HC-6 use sections_by_room index (skips empty room combos)
- HC-6 only checks adjacent block pairs

Day-family balance (HC-9)
─────────────────────────
Hard ceiling prevents any single day family from dominating.
Soft overflow penalty steers toward even distribution.

Dead-minutes model (SC-9)
─────────────────────────
Per occupied (room, block, day): dead = booking_window - class_duration.
TR·80 → 0 waste, MWF·55 → 25 waste per meeting. Directly penalised.

Credit-hour rules
──────────────────
3-credit: weekly minutes 140–175
4-credit: weekly minutes 150–250
"""

import math
import sqlite3
from collections import defaultdict

import pandas as pd
from ortools.sat.python import cp_model

# ── Universal time blocks ────────────────────────────────────────────────────
TIME_BLOCKS = [
    (0, 815,  "8:15 AM"),
    (1, 955,  "9:55 AM"),
    (2, 1135, "11:35 AM"),
    (3, 1315, "1:15 PM"),
    (4, 1455, "2:55 PM"),
    (5, 1635, "4:35 PM"),
]
BLOCK_IDS       = [b[0] for b in TIME_BLOCKS]
BLOCK_HHMM      = {b[0]: b[1] for b in TIME_BLOCKS}
BLOCK_LABEL     = {b[0]: b[2] for b in TIME_BLOCKS}
BLOCK_START_MIN = {b[0]: (b[1] // 100) * 60 + (b[1] % 100) for b in TIME_BLOCKS}

BOOKING_WINDOW = 80
MIN_TURNAROUND = 20

HIST_FILL = {
    1113: 0.932, 2250: 0.934, 2260: 0.938, 2270: 0.931,
    2500: 0.949, 2700: 0.978, 3300: 0.931,
}

# ── Duration rules ────────────────────────────────────────────────────────────
# Only two legal durations. No 67, 75, 100 etc.
LEGAL_DURATIONS = [55, 80]

# ── Day combos ────────────────────────────────────────────────────────────────
# All reasonable day combos for scheduling. Solver can pick any of these.
DAY_COMBOS = [
    # 2-day
    "MW", "TR", "MR", "TW", "MF", "WF", "TF", "RF", "WR",
    # 3-day
    "MWF", "MWR", "MRF", "TWR", "TWF", "TRF", "WRF", "MTW", "MTR", "MTF",
    # single day (for seminars/grad)
    "M", "T", "W", "R", "F",
]

ACTIVE_DAYS = ("M", "T", "W", "R", "F")

# ── Meeting patterns (generated from day combos × durations) ──────────────────
MEETING_PATTERNS = []
_pid = 0
for days in DAY_COMBOS:
    dc = len(days)
    for dur in LEGAL_DURATIONS:
        wm = dc * dur
        MEETING_PATTERNS.append({
            "pid": _pid,
            "days": days,
            "day_count": dc,
            "duration_mins": dur,
            "weekly_mins": wm,
            "label": f"{days}·{dur}",
            "booking": BOOKING_WINDOW,
        })
        _pid += 1

_PAT_BY_PID = {p["pid"]: p for p in MEETING_PATTERNS}

# ── Credit-hour rules ────────────────────────────────────────────────────────
CREDIT_MINUTE_RULES   = {3: {"min": 140, "max": 175},
                         4: {"min": 150, "max": 250}}
CREDIT_DAYCOUNT_RULES = {3: {2, 3}, 4: {2, 3, 4}}

# Only adjacent block pairs can violate 20-min turnaround
ADJACENT_BLOCK_PAIRS = [
    (b1, b2)
    for b1 in BLOCK_IDS for b2 in BLOCK_IDS
    if b2 > b1
    and BLOCK_START_MIN[b2] - BLOCK_START_MIN[b1] < BOOKING_WINDOW + MIN_TURNAROUND + 10
]

# ── Day-family classification ────────────────────────────────────────────────
def classify_day_family(days_str):
    """Classify pattern days into TR, MWF, MW, or OTHER for balance constraints."""
    s = set(days_str)
    if s == {"T", "R"}: return "TR"
    if s == {"M", "W", "F"}: return "MWF"
    if s == {"M", "W"}: return "MW"
    return "OTHER"

DAY_FAMILIES_LIST = ["TR", "MWF", "MW", "OTHER"]

# Pre-classify all patterns
_PAT_FAMILY = {p["pid"]: classify_day_family(p["days"]) for p in MEETING_PATTERNS}


def valid_patterns_for_section(credits, skel_days, skel_duration, max_candidates=15):
    """
    Return pattern candidates ranked by proximity to skeleton.
    Only allows durations of 55 or 80. Filters by credit-hour rules.
    """
    skel_day_count = sum(1 for c in skel_days if c in ACTIVE_DAYS)

    def pattern_distance(p):
        if p["days"] == skel_days:
            day_diff = 0
        elif set(p["days"]) == set(skel_days):
            day_diff = 0
        elif p["day_count"] == skel_day_count:
            day_diff = 1
        else:
            day_diff = 3
        dur_diff = 0 if p["duration_mins"] == skel_duration else 2
        return day_diff + dur_diff

    credit_valid = [
        pat for pat in MEETING_PATTERNS
        if (credits not in CREDIT_MINUTE_RULES or
            CREDIT_MINUTE_RULES[credits]["min"] <= pat["weekly_mins"] <= CREDIT_MINUTE_RULES[credits]["max"])
        and (credits not in CREDIT_DAYCOUNT_RULES or
             pat["day_count"] in CREDIT_DAYCOUNT_RULES[credits])
    ]

    if not credit_valid:
        # Fallback: anything with 2-3 days
        credit_valid = [p for p in MEETING_PATTERNS if p["day_count"] in {2, 3}]
    if not credit_valid:
        credit_valid = list(MEETING_PATTERNS)

    ranked = sorted(credit_valid, key=pattern_distance)
    return ranked[:max_candidates]


# ── Colour palette ────────────────────────────────────────────────────────────
COURSE_COLORS = {
    1113: {"face": "#f472b6", "edge": "#db2777"},
    2250: {"face": "#4ade80", "edge": "#16a34a"},
    2260: {"face": "#a78bfa", "edge": "#7c3aed"},
    2270: {"face": "#fb923c", "edge": "#ea580c"},
    2500: {"face": "#60a5fa", "edge": "#2563eb"},
    2700: {"face": "#34d399", "edge": "#059669"},
    3300: {"face": "#fbbf24", "edge": "#d97706"},
}

def course_color(course_number):
    if course_number in COURSE_COLORS:
        return COURSE_COLORS[course_number]
    palette = [
        {"face": "#93c5fd", "edge": "#2563eb"}, {"face": "#86efac", "edge": "#16a34a"},
        {"face": "#fcd34d", "edge": "#d97706"}, {"face": "#fca5a5", "edge": "#dc2626"},
        {"face": "#c4b5fd", "edge": "#7c3aed"}, {"face": "#67e8f9", "edge": "#0891b2"},
        {"face": "#f9a8d4", "edge": "#db2777"}, {"face": "#a7f3d0", "edge": "#059669"},
        {"face": "#fdba74", "edge": "#ea580c"}, {"face": "#bae6fd", "edge": "#0284c7"},
    ]
    return palette[abs(int(course_number)) % len(palette)]


# ── Helpers ───────────────────────────────────────────────────────────────────

def hhmm_to_min(x):
    x = int(x); return 60 * (x // 100) + (x % 100)

def snap_block(t):
    return min(BLOCK_IDS, key=lambda b: abs(BLOCK_HHMM[b] - int(t)))

def count_days(days_str):
    return sum(1 for c in str(days_str) if c in "MTWRFSU")

def weekly_mins(begin, end, days):
    dur = hhmm_to_min(end) - hhmm_to_min(begin)
    return dur * count_days(days), dur

def safe_int(x, default=None):
    if pd.isna(x): return default
    try: return int(round(float(x)))
    except (TypeError, ValueError): return default

def normalize_days(row):
    mapping = [("M", "MONDAY_IND"), ("T", "TUESDAY_IND"), ("W", "WEDNESDAY_IND"),
               ("R", "THURSDAY_IND"), ("F", "FRIDAY_IND"), ("S", "SATURDAY_IND"), ("U", "SUNDAY_IND")]
    out = []
    for letter, col in mapping:
        v = str(row.get(col, "")).strip().upper()
        if v in {letter, "Y", "YES", "TRUE", "1"}: out.append(letter)
    return "".join(out)

def parse_time_text_to_hhmm(text_value):
    parts = str(text_value).strip().split(":")
    if len(parts) != 2: raise ValueError(f"Invalid time format '{text_value}'.")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59): raise ValueError(f"Invalid time '{text_value}'.")
    return h * 100 + m

def pattern_days_to_letters(pattern_days):
    raw = str(pattern_days).strip()
    if not raw: return ""
    if raw.startswith("[") and raw.endswith("]"):
        dm = {0: "M", 1: "T", 2: "W", 3: "R", 4: "F", 5: "S", 6: "U"}
        return "".join(dm[int(c.strip())] for c in raw[1:-1].split(",") if c.strip() and int(c.strip()) in dm)
    return "".join(ch for ch in raw.upper() if ch in "MTWRFSU")

def minutes_to_hhmm(m): return (m // 60) * 100 + (m % 60)

def normalize_course_scope(cs):
    s = str(cs or "core").strip().lower()
    if s not in {"core", "all_math"}: raise ValueError(f"Unsupported course_scope '{cs}'.")
    return s

def snap_duration(raw_dur):
    """Snap any duration to the nearest legal duration (55 or 80)."""
    return min(LEGAL_DURATIONS, key=lambda d: abs(d - raw_dur))

def load_professor_preferences():
    try:
        from optimizer.models import ProfessorPreference
        prefs = {}
        for pref in ProfessorPreference.objects.select_related('professor').all():
            name = f"{pref.professor.last_name}, {pref.professor.first_name}".strip(", ")
            prefs[name] = {
                'time_of_day': pref.time_of_day, 'day_pattern': pref.day_pattern,
                'level_preference': pref.level_preference, 'max_sections': pref.max_sections,
                'avoid_back_to_back': pref.avoid_back_to_back, 'tenured': pref.tenured,
            }
        return prefs
    except Exception:
        return {}

def load_course_configs():
    """
    Returns dict: course_number (int) -> config dict.
    Safe to call even if the table doesn't exist yet.
    """
    try:
        from optimizer.models import CourseConfig
        configs = {}
        for c in CourseConfig.objects.all():
            configs[c.course_number] = {
                'is_active':          c.is_active,
                'min_sections':       c.min_sections,
                'max_sections':       c.max_sections,
                'banned_blocks':      c.get_banned_block_list(),
                'max_per_block':      c.max_per_block,
                'preferred_building': c.preferred_building,
                'required_room_type': c.required_room_type,
                'min_room_capacity':  c.min_room_capacity,
            }
        return configs
    except Exception:
        return {}
 
# ── Section builder ───────────────────────────────────────────────────────────

def _build_section_entry(i, crn, course, title, instructor, days, duration_mins,
                          credits, cap, actual, exp, skel_block, skel_room, skel_bldg,
                          db_section_id=None, max_candidates=15):
    cr = credits or 3
    # Snap duration to legal values
    dur = snap_duration(duration_mins)
    pats = valid_patterns_for_section(cr, days, dur, max_candidates)
    skel_pat = min(pats, key=lambda p:
        (0 if p["days"] == days else 1) + (0 if p["duration_mins"] == dur else 2))
    return {
        "id": i, "db_section_id": db_section_id, "crn": crn,
        "course": course, "title": title, "instructor": instructor,
        "credits": cr, "capacity": cap, "actual_enroll": actual, "exp_enroll": exp,
        "skel_block": skel_block, "skel_room": skel_room, "skel_bldg": skel_bldg,
        "skel_days": days, "skel_duration": dur,
        "skel_pid": skel_pat["pid"],
        "valid_pids": [p["pid"] for p in pats],
        "tail_waste": max(0, BOOKING_WINDOW - dur),
        "color": course_color(course),
    }


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data_from_csv(csv_path, max_candidates=15):
    df = pd.read_csv(csv_path)
    for col in ["COURSE_NUMBER", "ACADEMIC_PERIOD", "BEGIN_TIME", "END_TIME",
                "MAXIMUM_ENROLLMENT", "ACTUAL_ENROLLMENT", "TOTAL_CREDITS_SECTION",
                "MIN_CREDITS", "MAX_CREDITS"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    sp26 = df[(df["SUBJECT"] == "MATH") & (df["ACADEMIC_PERIOD"] == 202602) &
               df["BEGIN_TIME"].notna() & df["END_TIME"].notna()].copy()
    if sp26.empty: raise ValueError("No Spring 2026 MATH sections found.")

    sp26["DAYS"]      = sp26.apply(normalize_days, axis=1).replace("", "MWF")
    sp26["BEGIN_INT"] = sp26["BEGIN_TIME"].astype(int)
    sp26["END_INT"]   = sp26["END_TIME"].astype(int)

    rooms_raw = (sp26[sp26["BUILDING"].astype(str) != "NCRR"]
                 .groupby(["BUILDING", "ROOM"], dropna=False)["MAXIMUM_ENROLLMENT"]
                 .max().reset_index())
    rooms = [{"id": f"{r.BUILDING}-{r.ROOM}", "building": str(r.BUILDING),
               "room": str(r.ROOM), "capacity": safe_int(r.MAXIMUM_ENROLLMENT, 0)}
             for r in rooms_raw.itertuples() if safe_int(r.MAXIMUM_ENROLLMENT, 0) > 0]

    sections = []
    for i, (_, row) in enumerate(sp26.iterrows()):
        course = safe_int(row["COURSE_NUMBER"])
        if course is None: continue
        cap = max(safe_int(row["MAXIMUM_ENROLLMENT"], 20), 1)
        actual = max(safe_int(row["ACTUAL_ENROLLMENT"], 0), 0)
        exp = max(1, round(HIST_FILL.get(course, 0.85) * cap))
        credits = safe_int(row["TOTAL_CREDITS_SECTION"]) or safe_int(row["MAX_CREDITS"]) or safe_int(row["MIN_CREDITS"])
        days = str(row["DAYS"]); bi = safe_int(row["BEGIN_INT"]); ei = safe_int(row["END_INT"])
        if bi is None or ei is None: continue
        _, dmins = weekly_mins(bi, ei, days)
        bldg = str(row["BUILDING"])
        il   = str(row.get("PRIMARY_INSTRUCTOR_LAST_NAME", "")).strip() or "TBA"
        ifi  = str(row.get("PRIMARY_INSTRUCTOR_FIRST_NAME", "")).strip()
        instr = f"{il}, {ifi}".strip(", ") or "TBA"
        skel_room = None if bldg == "NCRR" else f"{bldg}-{row['ROOM']}"
        sec = _build_section_entry(i, safe_int(row["CRN"], i), course,
              str(row["TITLE_SHORT_DESC"]), instr, days, dmins, credits,
              cap, actual, exp, snap_block(bi), skel_room, bldg,
              max_candidates=max_candidates)
        if sec["valid_pids"]: sections.append(sec)
    return sections, rooms


def load_data_from_db(db_path, semester, course_scope="core", max_candidates=15):
    CORE_COURSES = [1113, 2250, 2260, 2270, 2500, 2700, 3300]
    scope = normalize_course_scope(course_scope)
    from optimizer.models import CourseSection, Classroom

    rooms = [{"id": f"{r.building_name}-{r.room_number}", "building": str(r.building_name),
               "room": str(r.room_number), "capacity": int(r.max_enrollment)}
             for r in Classroom.objects.filter(max_enrollment__gt=0)]
    if not rooms: raise ValueError("No classrooms found.")

    qs = CourseSection.objects.select_related(
        "course", "schedule", "schedule__professor", "schedule__classroom",
    ).filter(semester=semester, maximum_enrollment__gt=0
    ).prefetch_related("schedule__schedulemeetingblock_set__time_slot",
                       "schedule__schedulemeetingblock_set__meeting_pattern")
    if scope == "core":
        qs = qs.filter(course__course_number__in=CORE_COURSES)

    rows = qs.all()
    if not rows: raise ValueError(f"No sections found for {semester}.")

    sections = []
    for i, cs in enumerate(rows):
        course = cs.course.course_number if cs.course else None
        if course is None: continue
        try: schedule = cs.schedule
        except Exception: continue
        blocks = list(schedule.schedulemeetingblock_set.all())
        if not blocks: continue
        smb = blocks[0]; ts = smb.time_slot; mp = smb.meeting_pattern
        if not ts or not mp: continue

        begin_int = parse_time_text_to_hhmm(ts.start_time)
        slot_dur  = hhmm_to_min(parse_time_text_to_hhmm(ts.end_time)) - hhmm_to_min(begin_int)
        dur       = smb.class_duration_minutes or slot_dur
        days      = pattern_days_to_letters(mp.days) or "MWF"
        credits   = safe_int(cs.course.max_credits) or safe_int(cs.course.min_credits)
        cap       = max(cs.maximum_enrollment or 20, 1)
        actual    = cs.actual_enrollment or 0
        exp       = max(1, round(HIST_FILL.get(course, 0.85) * cap))
        prof      = schedule.professor
        instructor = (f"{prof.last_name}, {prof.first_name}".strip(", ")
                      if prof and prof.is_active else "TBA")
        classroom  = schedule.classroom
        skel_bldg  = str(classroom.building_name) if classroom else ""
        skel_room_v = str(classroom.room_number)  if classroom else ""
        skel_room  = f"{skel_bldg}-{skel_room_v}" if skel_bldg and skel_room_v else None

        sec = _build_section_entry(i, cs.crn, course, cs.course.course_name,
              instructor, days, dur, credits, cap, actual, exp,
              snap_block(begin_int), skel_room, skel_bldg, db_section_id=cs.id,
              max_candidates=max_candidates)
        if sec["valid_pids"]: sections.append(sec)

    # ── Apply course configs ──────────────────────────────────────────────────
    course_cfgs = load_course_configs()
    print(f"  Course configs loaded: {len(course_cfgs)} — {list(course_cfgs.keys())}")
    print(f"  Inactive: {[k for k,v in course_cfgs.items() if not v['is_active']]}")
    # Step 1: Remove inactive courses
    if course_cfgs:
        before = len(sections)
        sections = [s for s in sections
                    if course_cfgs.get(s["course"], {}).get("is_active", True)]
        removed = before - len(sections)
        if removed:
            print(f"  Removed {removed} sections for inactive courses.")

    # Step 2: Trim to max_sections per course
    if course_cfgs:
        from collections import defaultdict as _dd
        course_counts = _dd(int)
        trimmed = []
        for s in sections:
            cfg = course_cfgs.get(s["course"], {})
            max_s = cfg.get('max_sections')
            if max_s is not None and course_counts[s["course"]] >= max_s:
                continue
            trimmed.append(s)
            course_counts[s["course"]] += 1
        trimmed_count = len(sections) - len(trimmed)
        if trimmed_count:
            print(f"  Trimmed {trimmed_count} sections to meet max_sections limits.")
        sections = trimmed

    # Step 3: Generate placeholder sections to meet min_sections
    # Collect active professor names for round-robin assignment to placeholders
    if course_cfgs:
        try:
            from optimizer.models import Professor as ProfModel
            active_prof_names = [
                f"{p.last_name}, {p.first_name}".strip(", ")
                for p in ProfModel.objects.filter(is_active=True).order_by('last_name', 'first_name')
            ]
        except Exception:
            active_prof_names = []

        # Track which professors are already scheduled so we can distribute load
        prof_load = {name: 0 for name in active_prof_names}
        for s in sections:
            if s["instructor"] != "TBA" and s["instructor"] in prof_load:
                prof_load[s["instructor"]] += 1

        from collections import defaultdict as _dd2
        sections_by_course = _dd2(list)
        for s in sections:
            sections_by_course[s["course"]].append(s)

        placeholder_count = 0
        for course_num, cfg in course_cfgs.items():
            min_s = cfg.get('min_sections')
            if min_s is None:
                continue
            current = sections_by_course.get(course_num, [])
            shortage = min_s - len(current)
            if shortage <= 0:
                continue

            print(f"  MATH {course_num}: need {min_s}, have {len(current)}, "
                  f"generating {shortage} placeholder section(s).")

            # Use typical values from existing sections of this course if available
            if current:
                ref = current[0]
                ref_credits = ref["credits"]
                ref_cap     = ref["capacity"]
                ref_exp     = ref["exp_enroll"]
                ref_days    = ref["skel_days"]
                ref_dur     = ref["skel_duration"]
                ref_block   = ref["skel_block"]
            else:
                # No existing sections — use sensible defaults
                ref_credits = 3
                ref_cap     = 30
                ref_exp     = 25
                ref_days    = "TR"
                ref_dur     = 80
                ref_block   = 1  # 9:55 AM

            for j in range(shortage):
                # Pick the active professor with the lowest current load
                if active_prof_names:
                    instructor = min(prof_load, key=prof_load.get)
                    prof_load[instructor] += 1
                else:
                    instructor = "TBA"

                sec = _build_section_entry(
                    i=len(sections) + 20000 + placeholder_count,
                    crn=88000 + len(sections) + placeholder_count,
                    course=course_num,
                    title="Placeholder Section",
                    instructor=instructor,
                    days=ref_days,
                    duration_mins=ref_dur,
                    credits=ref_credits,
                    cap=ref_cap,
                    actual=0,
                    exp=ref_exp,
                    skel_block=ref_block,
                    skel_room=None,   # no room preference — solver picks freely
                    skel_bldg="",
                    db_section_id=None,
                    max_candidates=max_candidates,
                )
                sections.append(sec)
                placeholder_count += 1

        if placeholder_count:
            print(f"  Generated {placeholder_count} total placeholder section(s).")

    # ── Floating sections for active professors not in any surviving section ──
    try:
        from optimizer.models import Professor as ProfModel
        active_names = set(f"{p.last_name}, {p.first_name}".strip(", ")
                           for p in ProfModel.objects.filter(is_active=True))
        scheduled = {s["instructor"] for s in sections if s["instructor"] != "TBA"}
        for name in active_names - scheduled:
            parts = name.split(", ", 1)
            last = parts[0]; first = parts[1] if len(parts) > 1 else ""
            from optimizer.models import Professor as PM, Schedule as SM
            try:
                prof = PM.objects.get(last_name=last, first_name=first)
                sched = SM.objects.filter(professor=prof,
                    course_section__semester=semester,
                ).select_related('course_section__course').first()
                if sched:
                    cs2 = sched.course_section; course = cs2.course.course_number
                    cap = max(cs2.maximum_enrollment or 20, 1)
                    exp = max(1, round(HIST_FILL.get(course, 0.85) * cap))
                    credits = cs2.course.max_credits or cs2.course.min_credits or 3
                else:
                    course = 1113; cap = 20; exp = 17; credits = 3
            except Exception:
                course = 1113; cap = 20; exp = 17; credits = 3
            sec = _build_section_entry(len(sections) + 10000, 99000 + len(sections),
                  course, "Floating Section", name, "TR", 80, credits, cap, 0, exp, 1, None, "",
                  max_candidates=max_candidates)
            sections.append(sec)
            print(f"  Injected floating section for active professor: {name}")
    except Exception as e:
        print(f"  Warning: could not inject floating sections: {e}")

    if not sections: raise ValueError("No sections survived filters.")
    return sections, rooms

def load_data(*, source="csv", csv_path=None, db_path=None,
              semester="202602", course_scope="core", max_candidates=15):
    s = str(source).strip().lower()
    if s == "db": return load_data_from_db(db_path=db_path, semester=str(semester),
                                            course_scope=course_scope, max_candidates=max_candidates)
    if s == "csv": return load_data_from_csv(csv_path, max_candidates=max_candidates)
    raise ValueError(f"Unsupported source '{source}'.")


# ── Solve ─────────────────────────────────────────────────────────────────────

def build_and_solve(sections, rooms, weights, solver_time=60, num_opts=3, log_fn=print):
    # ── Weight extraction ─────────────────────────────────────────────────────
    W_SKEL_SLOT    = int(weights.get("w_skeleton_slot",    12))
    W_SKEL_BLDG    = int(weights.get("w_skeleton_bldg",    2))
    UE_THRESH      = float(weights.get("under_enroll_threshold", 0.60))
    W_UNDER_ENROLL = int(weights.get("w_under_enroll",    12))
    W_BLOCK_OVER   = int(weights.get("w_block_over",      18))
    INSTR_MAX      = int(weights.get("instructor_max_sections", 3))
    W_INSTR_OVL    = int(weights.get("w_instr_overload",  20))
    W_LOWER_MID    = int(weights.get("w_lower_midday",     5))
    W_UPPER_NOMID  = int(weights.get("w_upper_nonmidday", 15))
    BLK_MIN_PCT    = float(weights.get("block_min_pct",   0.15))
    BLK_MAX_PCT    = float(weights.get("block_max_pct",   0.30))
    W_DEAD_MIN     = int(weights.get("w_dead_minutes",     3))
    W_PREF         = int(weights.get("w_prof_pref",        6))

    # Day-family balance
    DAY_FAM_MAX_PCT = float(weights.get("day_family_max_pct", 0.45))
    DAY_FAM_MIN_PCT = float(weights.get("day_family_min_pct", 0.10))
    W_DAY_FAM_OVER  = int(weights.get("w_day_family_over",   10))

    # Memory control
    MAX_ELIGIBLE_ROOMS = int(weights.get("max_eligible_rooms", 20))

    MIDDAY_BLOCKS = {1, 2, 3}
    LOWER_MAX     = 2250
    nr    = len(rooms)
    total = len(sections)

    # ── Pre-compute eligible rooms (CAPPED) ───────────────────────────────────
    eligible_rooms = {}
    for s in sections:
        fitting = [(ri, rooms[ri]["capacity"]) for ri in range(nr)
                   if rooms[ri]["capacity"] >= s["exp_enroll"]]
        fitting.sort(key=lambda x: x[1])
        eligible_rooms[s["id"]] = [ri for ri, _ in fitting[:MAX_ELIGIBLE_ROOMS]]

    # Ensure skeleton room always included
    room_id_to_ri = {rooms[ri]["id"]: ri for ri in range(nr)}
    for s in sections:
        if s["skel_room"] and s["skel_room"] in room_id_to_ri:
            sri = room_id_to_ri[s["skel_room"]]
            if sri not in eligible_rooms[s["id"]]:
                eligible_rooms[s["id"]].append(sri)

    sid_to_pids = {s["id"]: s["valid_pids"] for s in sections}

    # ── Pre-index: sections by room ───────────────────────────────────────────
    sections_by_room = defaultdict(list)
    for s in sections:
        for ri in eligible_rooms[s["id"]]:
            sections_by_room[ri].append(s)

    # Days-set cache
    _dc = {}
    def days_set(pid):
        if pid not in _dc:
            _dc[pid] = set(d for d in _PAT_BY_PID[pid]["days"] if d in ACTIVE_DAYS)
        return _dc[pid]

    avg_rooms = sum(len(eligible_rooms[s["id"]]) for s in sections) / max(total, 1)
    max_pats = max(len(s["valid_pids"]) for s in sections)
    log_fn(f"Setup: {total} sections, {nr} rooms (avg {avg_rooms:.0f} eligible/section), ≤{max_pats} patterns/section")

    log_fn("Creating CP-SAT model...")
    model = cp_model.CpModel()

    # ── Variables: assign[sid, pid, ri, b] ────────────────────────────────────
    assign = {}
    for s in sections:
        sid = s["id"]
        for pid in sid_to_pids[sid]:
            for ri in eligible_rooms[sid]:
                for b in BLOCK_IDS:
                    assign[sid, pid, ri, b] = model.NewBoolVar(f"x_{sid}_{pid}_{ri}_{b}")
    log_fn(f"  {len(assign):,} variables created.")

    # ══════════════════════════════════════════════════════════════════════════
    # HARD CONSTRAINTS
    # ══════════════════════════════════════════════════════════════════════════

    # HC-1: each section exactly once
    for s in sections:
        sid = s["id"]
        choices = [assign[sid, pid, ri, b]
                   for pid in sid_to_pids[sid]
                   for ri in eligible_rooms[sid]
                   for b in BLOCK_IDS
                   if (sid, pid, ri, b) in assign]
        if not choices:
            raise ValueError(f"No feasible assignment for CRN {s['crn']}")
        model.AddExactlyOne(choices)

    # HC-4: room conflict — one section per (room, block, day)
    log_fn("Adding HC-4 room conflict constraints...")
    hc4_count = 0
    for ri, ri_sections in sections_by_room.items():
        if len(ri_sections) < 2: continue
        for b in BLOCK_IDS:
            for day in ACTIVE_DAYS:
                occ = [assign[s["id"], pid, ri, b]
                       for s in ri_sections
                       for pid in sid_to_pids[s["id"]]
                       if day in days_set(pid)
                       and (s["id"], pid, ri, b) in assign]
                if len(occ) > 1:
                    model.AddAtMostOne(occ)
                    hc4_count += 1
    log_fn(f"  HC-4: {hc4_count} constraints.")

    # HC-5: instructor conflict — one section per (instructor, block, day)
    log_fn("Adding HC-5 instructor conflict constraints...")
    by_instr = defaultdict(list)
    for s in sections:
        if s["instructor"] != "TBA": by_instr[s["instructor"]].append(s["id"])

    hc5_count = 0
    for instr, sids in by_instr.items():
        if len(sids) < 2: continue
        for b in BLOCK_IDS:
            for day in ACTIVE_DAYS:
                vv = [assign[sid, pid, ri, b]
                      for sid in sids
                      for pid in sid_to_pids[sid]
                      for ri in eligible_rooms[sid]
                      if day in days_set(pid) and (sid, pid, ri, b) in assign]
                if len(vv) > 1:
                    model.AddAtMostOne(vv)
                    hc5_count += 1
    log_fn(f"  HC-5: {hc5_count} constraints.")

    # HC-6: turnaround (adjacent blocks, room-indexed)
    log_fn("Adding HC-6 turnaround constraints...")
    hc6_count = 0
    for b1, b2 in ADJACENT_BLOCK_PAIRS:
        for ri, ri_sections in sections_by_room.items():
            if len(ri_sections) < 2: continue
            for i_s1, s1 in enumerate(ri_sections):
                sid1 = s1["id"]
                for pid1 in sid_to_pids[sid1]:
                    if (sid1, pid1, ri, b1) not in assign: continue
                    pat1 = _PAT_BY_PID[pid1]
                    end_s1 = BLOCK_START_MIN[b1] + pat1["duration_mins"]
                    gap = BLOCK_START_MIN[b2] - end_s1
                    if gap >= MIN_TURNAROUND: continue
                    days1 = days_set(pid1)
                    for s2 in ri_sections[i_s1 + 1:]:
                        sid2 = s2["id"]
                        for pid2 in sid_to_pids[sid2]:
                            if (sid2, pid2, ri, b2) not in assign: continue
                            if not (days1 & days_set(pid2)): continue
                            model.Add(assign[sid1, pid1, ri, b1]
                                      + assign[sid2, pid2, ri, b2] <= 1)
                            hc6_count += 1
    log_fn(f"  HC-6: {hc6_count} constraints.")

    # HC-8: active professor must teach >= 1 section
    log_fn("Adding HC-8 active professor constraints...")
    try:
        from optimizer.models import Professor
        active_profs = set(f"{p.last_name}, {p.first_name}".strip(", ")
                           for p in Professor.objects.filter(is_active=True))
    except Exception:
        active_profs = set()

    hc8_count = 0
    for instr, sids in by_instr.items():
        if instr not in active_profs: continue
        all_vars = [assign[sid, pid, ri, b]
                    for sid in sids
                    for pid in sid_to_pids[sid]
                    for ri in eligible_rooms[sid]
                    for b in BLOCK_IDS
                    if (sid, pid, ri, b) in assign]
        if all_vars: model.Add(sum(all_vars) >= 1); hc8_count += 1
    log_fn(f"  HC-8: {hc8_count} constraints.")

    # HC-2: block distribution band
    log_fn("Adding HC-2 block distribution constraints...")
    blk_floor = max(1, math.floor(total * BLK_MIN_PCT))
    blk_ceil  = math.ceil(total * BLK_MAX_PCT)

    in_block = {}
    for s in sections:
        sid = s["id"]
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"ib_{sid}_{b}")
            choices = [assign[sid, pid, ri, b]
                       for pid in sid_to_pids[sid]
                       for ri in eligible_rooms[sid]
                       if (sid, pid, ri, b) in assign]
            if choices: model.AddMaxEquality(v, choices)
            else:       model.Add(v == 0)
            in_block[sid, b] = v

    blk_over = {}
    for b in BLOCK_IDS:
        sb = model.NewIntVar(0, total, f"sb_{b}")
        model.Add(sb == sum(in_block[s["id"], b] for s in sections))
        model.Add(sb >= blk_floor); model.Add(sb <= blk_ceil)
        ov = model.NewIntVar(0, total, f"ov_{b}")
        model.Add(ov >= sb - math.floor(total * BLK_MAX_PCT)); model.Add(ov >= 0)
        blk_over[b] = ov

    # HC-9: day-family distribution band
    log_fn("Adding HC-9 day-family distribution constraints...")
    fam_ceil  = math.ceil(total * DAY_FAM_MAX_PCT)
    fam_floor = max(1, math.floor(total * DAY_FAM_MIN_PCT))

    fam_over = {}
    for fam_name in DAY_FAMILIES_LIST:
        fam_pids = {pid for pid, fam in _PAT_FAMILY.items() if fam == fam_name}
        in_family = []
        for s in sections:
            sid = s["id"]
            eligible_pids = [p for p in sid_to_pids[sid] if p in fam_pids]
            if not eligible_pids: continue
            v = model.NewBoolVar(f"fam_{fam_name}_{sid}")
            choices = [assign[sid, pid, ri, b]
                       for pid in eligible_pids
                       for ri in eligible_rooms[sid]
                       for b in BLOCK_IDS
                       if (sid, pid, ri, b) in assign]
            if choices:
                model.AddMaxEquality(v, choices)
                in_family.append(v)

        if not in_family:
            log_fn(f"  {fam_name}: 0 eligible, skipped.")
            continue

        fam_sum = model.NewIntVar(0, total, f"fam_sum_{fam_name}")
        model.Add(fam_sum == sum(in_family))
        model.Add(fam_sum <= fam_ceil)

        if len(in_family) >= fam_floor * 2:
            model.Add(fam_sum >= fam_floor)
            log_fn(f"  {fam_name}: {len(in_family)} eligible, hard=[{fam_floor},{fam_ceil}]")
        else:
            log_fn(f"  {fam_name}: {len(in_family)} eligible, ceil={fam_ceil} only")

        ov = model.NewIntVar(0, total, f"fam_ov_{fam_name}")
        ideal = math.floor(total / len(DAY_FAMILIES_LIST))
        model.Add(ov >= fam_sum - ideal); model.Add(ov >= 0)
        fam_over[fam_name] = ov

    course_cfgs = load_course_configs()
    log_fn(f"Loaded {len(course_cfgs)} course configurations.")
 
    # Group section IDs by course number for course-level constraints
    sections_by_course = defaultdict(list)
    for s in sections:
        sections_by_course[s["course"]].append(s)
 
    # HC-10: Section count floor/ceiling per course
    # If CourseConfig.min_sections / max_sections is set, enforce it.
    log_fn("Adding HC-10 course section count constraints...")
    hc10_count = 0
    for course_num, cfg in course_cfgs.items():
        course_sections = sections_by_course.get(course_num, [])
        if not course_sections:
            continue
 
        # Build one bool var per section: is this section actually assigned?
        # (It always is via HC-1, so sum = len(course_sections))
        # We use in_block-style vars: assigned[sid] = OR over all (pid,ri,b)
        assigned_vars = []
        for s in course_sections:
            sid = s["id"]
            v = model.NewBoolVar(f"assigned_{sid}")
            choices = [assign[sid, pid, ri, b]
                       for pid in sid_to_pids[sid]
                       for ri in eligible_rooms[sid]
                       for b in BLOCK_IDS
                       if (sid, pid, ri, b) in assign]
            if choices:
                model.AddMaxEquality(v, choices)
            else:
                model.Add(v == 0)
            assigned_vars.append(v)
 
        section_count = model.NewIntVar(0, len(course_sections), f"cnt_{course_num}")
        model.Add(section_count == sum(assigned_vars))
 
        if cfg.get('min_sections') is not None:
            floor = min(cfg['min_sections'], len(course_sections))
            model.Add(section_count >= floor)
            hc10_count += 1
            log_fn(f"  MATH {course_num}: min_sections={floor}")
 
        if cfg.get('max_sections') is not None:
            ceil_ = min(cfg['max_sections'], len(course_sections))
            model.Add(section_count <= ceil_)
            hc10_count += 1
            log_fn(f"  MATH {course_num}: max_sections={ceil_}")
 
    log_fn(f"  HC-10: {hc10_count} constraints added.")

    # HC-11: Banned time blocks per course
    log_fn("Adding HC-11 banned block constraints...")
    hc11_count = 0
    for course_num, cfg in course_cfgs.items():
        banned = cfg.get('banned_blocks', [])
        if not banned:
            continue
        for s in sections_by_course.get(course_num, []):
            sid = s["id"]
            for b in banned:
                if b not in BLOCK_IDS:
                    continue
                bad_vars = [assign[sid, pid, ri, b]
                            for pid in sid_to_pids[sid]
                            for ri in eligible_rooms[sid]
                            if (sid, pid, ri, b) in assign]
                for v in bad_vars:
                    model.Add(v == 0)
                    hc11_count += 1
    log_fn(f"  HC-11: {hc11_count} ban constraints added.")
 
    # HC-12: Max sections per block per course
    log_fn("Adding HC-12 max-per-block constraints...")
    hc12_count = 0
    for course_num, cfg in course_cfgs.items():
        mpb = cfg.get('max_per_block')
        if mpb is None:
            continue
        course_sections = sections_by_course.get(course_num, [])
        if not course_sections:
            continue
        for b in BLOCK_IDS:
            block_vars = []
            for s in course_sections:
                sid = s["id"]
                v = in_block.get((sid, b))   # already built in HC-2
                if v is not None:
                    block_vars.append(v)
            if len(block_vars) > mpb:
                model.Add(sum(block_vars) <= mpb)
                hc12_count += 1
    log_fn(f"  HC-12: {hc12_count} max-per-block constraints added.")
 
    # HC-13: Room type requirement
    # lecture = capacity >= 60, seminar = capacity < 40, lab = building contains 'LAB'
    log_fn("Adding HC-13 room type constraints...")
    hc13_count = 0
    for course_num, cfg in course_cfgs.items():
        rtype = cfg.get('required_room_type', 'any')
        min_cap = cfg.get('min_room_capacity')
        if rtype == 'any' and min_cap is None:
            continue
        for s in sections_by_course.get(course_num, []):
            sid = s["id"]
            for ri in eligible_rooms[sid]:
                r = rooms[ri]
                cap = r["capacity"]
                bldg = r.get("building", "")
                room_ok = True
                if rtype == 'lecture'  and cap < 60:   room_ok = False
                if rtype == 'seminar'  and cap >= 40:  room_ok = False
                if rtype == 'lab'      and 'LAB' not in bldg.upper(): room_ok = False
                if min_cap is not None and cap < min_cap: room_ok = False
                if not room_ok:
                    for pid in sid_to_pids[sid]:
                        for b in BLOCK_IDS:
                            if (sid, pid, ri, b) in assign:
                                model.Add(assign[sid, pid, ri, b] == 0)
                                hc13_count += 1
    log_fn(f"  HC-13: {hc13_count} room type exclusions added.")

    # ══════════════════════════════════════════════════════════════════════════
    # SOFT CONSTRAINTS (OBJECTIVE)
    # ══════════════════════════════════════════════════════════════════════════
    obj = []

    # SC-1: skeleton block fidelity
    for s in sections:
        sid = s["id"]; sb = s["skel_block"]
        for pid in sid_to_pids[sid]:
            for ri in eligible_rooms[sid]:
                for b in BLOCK_IDS:
                    if (sid, pid, ri, b) not in assign: continue
                    d = abs(b - sb)
                    if d: obj.append(W_SKEL_SLOT * d * assign[sid, pid, ri, b])

    # SC-1b: skeleton pattern fidelity — proportional disruption cost
    for s in sections:
        sid = s["id"]; spid = s["skel_pid"]; skel_p = _PAT_BY_PID[spid]
        for pid in sid_to_pids[sid]:
            if pid == spid: continue
            pat = _PAT_BY_PID[pid]
            day_penalty = 3 if pat["days"] != skel_p["days"] else 0
            dur_penalty = 2 if pat["duration_mins"] != skel_p["duration_mins"] else 0
            disruption = max(1, day_penalty + dur_penalty)
            for ri in eligible_rooms[sid]:
                for b in BLOCK_IDS:
                    if (sid, pid, ri, b) not in assign: continue
                    obj.append(W_SKEL_SLOT * disruption * assign[sid, pid, ri, b])

    # SC-2: building continuity
    for s in sections:
        sid = s["id"]; sbld = s["skel_bldg"]
        for pid in sid_to_pids[sid]:
            for ri in eligible_rooms[sid]:
                if rooms[ri]["building"] == sbld: continue
                for b in BLOCK_IDS:
                    if (sid, pid, ri, b) in assign:
                        obj.append(W_SKEL_BLDG * assign[sid, pid, ri, b])

    # SC-2b: preferred_building from CourseConfig (soft, same weight as SC-2)
    for course_num, cfg in course_cfgs.items():
        pref_bldg = cfg.get('preferred_building', '')
        if not pref_bldg:
            continue
        for s in sections_by_course.get(course_num, []):
            sid = s["id"]
            for pid in sid_to_pids[sid]:
                for ri in eligible_rooms[sid]:
                    if rooms[ri]["building"].upper() == pref_bldg.upper():
                        continue
                    for b in BLOCK_IDS:
                        if (sid, pid, ri, b) in assign:
                            obj.append(W_SKEL_BLDG * assign[sid, pid, ri, b])

    # SC-4: under-enrollment
    for s in sections:
        if s["exp_enroll"] / max(s["capacity"], 1) >= UE_THRESH: continue
        flag = model.NewBoolVar(f"ue_{s['id']}")
        choices = [assign[s["id"], pid, ri, b]
                   for pid in sid_to_pids[s["id"]]
                   for ri in eligible_rooms[s["id"]]
                   for b in BLOCK_IDS if (s["id"], pid, ri, b) in assign]
        model.AddMaxEquality(flag, choices)
        obj.append(W_UNDER_ENROLL * flag)

    # SC-5: block over-cap
    for b in BLOCK_IDS: obj.append(W_BLOCK_OVER * blk_over[b])

    # SC-5b: day-family overflow
    for fam_name, ov in fam_over.items():
        obj.append(W_DAY_FAM_OVER * ov)

    # SC-6: instructor overload
    for instr, sids in by_instr.items():
        if len(sids) > INSTR_MAX:
            obj.append(W_INSTR_OVL * (len(sids) - INSTR_MAX))

    # SC-8: level distribution
    for s in sections:
        sid = s["id"]; is_lower = (s["course"] <= LOWER_MAX)
        for pid in sid_to_pids[sid]:
            for ri in eligible_rooms[sid]:
                for b in BLOCK_IDS:
                    if (sid, pid, ri, b) not in assign: continue
                    mid = b in MIDDAY_BLOCKS
                    if is_lower and mid:             obj.append(W_LOWER_MID   * assign[sid, pid, ri, b])
                    elif not is_lower and not mid:   obj.append(W_UPPER_NOMID * assign[sid, pid, ri, b])

    # SC-9: dead-minutes — direct tail waste per occupied block
    # 80-min class in 80-min booking → 0 dead. 55-min class → 25 dead per meeting.
    log_fn("Adding SC-9 dead-minutes penalties...")
    sc9_terms = 0
    for s in sections:
        sid = s["id"]
        for pid in sid_to_pids[sid]:
            pat = _PAT_BY_PID[pid]
            tail = max(0, BOOKING_WINDOW - pat["duration_mins"])
            if tail == 0: continue
            weekly_waste = tail * pat["day_count"]
            for ri in eligible_rooms[sid]:
                for b in BLOCK_IDS:
                    if (sid, pid, ri, b) not in assign: continue
                    obj.append(W_DEAD_MIN * weekly_waste * assign[sid, pid, ri, b])
                    sc9_terms += 1
    log_fn(f"  SC-9: {sc9_terms} terms.")

    # SC-10: professor preferences
    log_fn("Adding SC-10 professor preferences...")
    prof_prefs = load_professor_preferences()
    MORNING_BLOCKS = {0, 1}; MIDDAY_BLOCKS_P = {1, 2, 3}; AFTERNOON_BLOCKS = {3, 4, 5}
    EARLY_BLOCK = {0}; LATE_BLOCK = {5}

    sc10_count = 0
    for s in sections:
        instr = s["instructor"]
        if instr == "TBA" or instr not in prof_prefs: continue
        pref = prof_prefs[instr]; sid = s["id"]
        time_pref = pref.get("time_of_day", "any")
        day_pref = pref.get("day_pattern", "any")
        level_pref = pref.get("level_preference", "any")
        max_sec = pref.get("max_sections", "any")

        for pid in sid_to_pids[sid]:
            pat = _PAT_BY_PID[pid]
            for ri in eligible_rooms[sid]:
                for b in BLOCK_IDS:
                    if (sid, pid, ri, b) not in assign: continue
                    v = assign[sid, pid, ri, b]
                    if ((time_pref == "morning"   and b not in MORNING_BLOCKS)   or
                        (time_pref == "midday"    and b not in MIDDAY_BLOCKS_P)  or
                        (time_pref == "afternoon" and b not in AFTERNOON_BLOCKS) or
                        (time_pref == "no_early"  and b in EARLY_BLOCK)          or
                        (time_pref == "no_late"   and b in LATE_BLOCK)):
                        obj.append(W_PREF * v); sc10_count += 1
                    if day_pref != "any":
                        pd_set = {"MWF": {"M", "W", "F"}, "TR": {"T", "R"}, "MW": {"M", "W"}}.get(day_pref, set())
                        if pd_set and set(pat["days"]) != pd_set:
                            obj.append(W_PREF * v); sc10_count += 1
                    cn = s["course"]
                    if ((level_pref == "lower" and cn >= 3000) or
                        (level_pref == "upper" and (cn < 3000 or cn >= 5000)) or
                        (level_pref == "grad"  and cn < 5000)):
                        obj.append(W_PREF * v); sc10_count += 1

        if max_sec != "any":
            n_secs = len([sx for sx in sections if sx["instructor"] == instr])
            if n_secs > int(max_sec):
                obj.append(W_PREF * (n_secs - int(max_sec)))

    log_fn(f"  SC-10: {sc10_count} terms.")
    log_fn(f"Minimising objective ({len(obj):,} terms)...")
    model.Minimize(sum(obj) if obj else model.NewIntVar(0, 0, "zero"))

    # ══════════════════════════════════════════════════════════════════════════
    # SOLVE
    # ══════════════════════════════════════════════════════════════════════════
    room_ids = [r["id"] for r in rooms]
    solutions = []

    for opt_i in range(num_opts):
        log_fn(f"Solving option {chr(65 + opt_i)} (seed={17 + opt_i * 31}, limit={solver_time}s)...")
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(solver_time)
        solver.parameters.num_search_workers  = 4
        solver.parameters.random_seed         = 17 + opt_i * 31
        solver.parameters.log_search_progress = False
        status = solver.Solve(model)
        log_fn(f"  Status: {solver.StatusName(status)}")
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE): break

        sol = {}; chosen = []
        for s in sections:
            sid = s["id"]; placed = False
            for pid in sid_to_pids[sid]:
                for ri in eligible_rooms[sid]:
                    for b in BLOCK_IDS:
                        if (sid, pid, ri, b) in assign and solver.Value(assign[sid, pid, ri, b]):
                            pat = _PAT_BY_PID[pid]
                            sol[sid] = {"block": b, "room": room_ids[ri], "pid": pid,
                                        "days": pat["days"], "duration_mins": pat["duration_mins"],
                                        "pattern_label": pat["label"]}
                            chosen.append(assign[sid, pid, ri, b]); placed = True; break
                    if placed: break
                if placed: break
            if not placed:
                dpid = s["skel_pid"] if s["skel_pid"] in sid_to_pids[sid] else sid_to_pids[sid][0]
                pat = _PAT_BY_PID[dpid]
                sol[sid] = {"block": s["skel_block"], "room": s["skel_room"] or "UNASSIGNED",
                            "pid": dpid, "days": pat["days"], "duration_mins": pat["duration_mins"],
                            "pattern_label": pat["label"]}

        score = int(round(solver.ObjectiveValue()))

        # Log distributions
        fam_counts = defaultdict(int)
        dur_counts = defaultdict(int)
        for sid, asgn in sol.items():
            fam_counts[_PAT_FAMILY.get(asgn["pid"], "?")] += 1
            dur_counts[asgn["duration_mins"]] += 1
        log_fn(f"  Day families: {dict(fam_counts)}")
        log_fn(f"  Durations: {dict(dur_counts)}")

        solutions.append({"label": f"Option {chr(65 + opt_i)}", "score": score, "assignment": sol})
        log_fn(f"  Option {chr(65 + opt_i)} score={score}")
        if chosen: model.Add(sum(chosen) <= len(chosen) - 3)

    log_fn(f"Finished: {len(solutions)} solution(s) found.")
    return solutions


# ── Analytics ─────────────────────────────────────────────────────────────────

def analyze(solution, sections, rooms, weights=None):
    w        = weights or {}
    UE_THR   = float(w.get("under_enroll_threshold", 0.60))
    BLK_MAX  = float(w.get("block_max_pct", 0.30))
    sec_idx  = {s["id"]: s for s in sections}
    room_idx = {r["id"]: r for r in rooms}
    total    = len(sections)
    sol      = solution["assignment"]

    moved = 0; bldg_changes = 0; under_list = []
    block_dist = defaultdict(int); by_instr = defaultdict(list)

    for sid, asgn in sol.items():
        s = sec_idx[sid]; b = asgn["block"]
        block_dist[b] += 1
        assigned_days = asgn.get("days", s["skel_days"])
        if b != s["skel_block"] or assigned_days != s["skel_days"]: moved += 1
        r = room_idx.get(asgn["room"])
        if r and r["building"] != s["skel_bldg"]: bldg_changes += 1
        fill = s["exp_enroll"] / max(s["capacity"], 1)
        if fill < UE_THR:
            under_list.append({"crn": s["crn"], "course": s["course"],
                                "instructor": s["instructor"],
                                "fill_pct": round(fill * 100, 1),
                                "exp": s["exp_enroll"], "cap": s["capacity"]})
        if s["instructor"] != "TBA": by_instr[s["instructor"]].append(b)

    soft_cap  = math.floor(total * BLK_MAX)
    blk_floor = max(1, math.floor(total * float(w.get("block_min_pct", 0.15))))
    block_rows = []
    for b in BLOCK_IDS:
        cnt = block_dist.get(b, 0); pct = round(100 * cnt / total, 1) if total else 0
        block_rows.append({"id": b, "label": BLOCK_LABEL[b], "count": cnt, "pct": pct,
                           "over": max(0, cnt - soft_cap),
                           "floor_ok": cnt >= blk_floor, "ceil_ok": cnt <= soft_cap})

    course_stats = defaultdict(lambda: {"sections": 0, "exp": 0, "cap": 0})
    for sid, asgn in sol.items():
        s = sec_idx[sid]; cs = course_stats[s["course"]]
        cs["sections"] += 1; cs["exp"] += s["exp_enroll"]; cs["cap"] += s["capacity"]
    course_rows = [{"course": k, "sections": v["sections"],
                    "avg_fill": round(100 * v["exp"] / max(v["cap"], 1), 1)}
                   for k, v in sorted(course_stats.items())]

    # Day-family distribution
    fam_counts = defaultdict(int)
    for sid, asgn in sol.items():
        pid = asgn.get("pid")
        if pid is not None:
            fam_counts[_PAT_FAMILY.get(pid, "OTHER")] += 1
        else:
            fam_counts[classify_day_family(asgn.get("days", sec_idx[sid]["skel_days"]))] += 1
    day_family_rows = [
        {"family": fn, "count": fam_counts.get(fn, 0),
         "pct": round(100 * fam_counts.get(fn, 0) / total, 1) if total else 0}
        for fn in DAY_FAMILIES_LIST
    ]

    instr_table = {}
    for sid, asgn in sol.items():
        s = sec_idx[sid]
        if s["instructor"] == "TBA": continue
        instr_table.setdefault(s["instructor"], []).append({
            "block": asgn["block"], "block_label": BLOCK_LABEL[asgn["block"]],
            "course": s["course"], "crn": s["crn"],
            "days": asgn.get("days", s["skel_days"]), "room": asgn["room"],
            "pattern": asgn.get("pattern_label", ""),
        })
    for k in instr_table: instr_table[k].sort(key=lambda x: x["block"])

    # Calendar
    calendar = []
    for sid, asgn in sol.items():
        s = sec_idx[sid]; b = asgn["block"]
        assigned_days = asgn.get("days", s["skel_days"])
        assigned_dur  = asgn.get("duration_mins", s["skel_duration"])
        tail_waste = max(0, BOOKING_WINDOW - assigned_dur)
        sh, sm = divmod(BLOCK_HHMM[b], 100)
        start = sh * 60 + sm; end = start + assigned_dur
        for day in assigned_days:
            if day not in "MTWRF": continue
            calendar.append({
                "sid": sid, "crn": s["crn"], "course": s["course"],
                "title": s["title"], "instructor": s["instructor"],
                "days": assigned_days, "day": day, "start": start, "end": end,
                "duration_mins": assigned_dur, "tail_waste": tail_waste,
                "room": asgn["room"], "face": s["color"]["face"], "edge": s["color"]["edge"],
                "moved": (b != s["skel_block"]) or (assigned_days != s["skel_days"]),
                "pattern": asgn.get("pattern_label", ""),
            })

    # Dead-minutes analysis
    room_day_blocks = defaultdict(dict); total_dead_minutes = 0

    for sid, asgn in sol.items():
        s = sec_idx[sid]; rid = asgn["room"]; b = asgn["block"]
        dur = asgn.get("duration_mins", s["skel_duration"])
        assigned_days = asgn.get("days", s["skel_days"])
        tail_waste = max(0, BOOKING_WINDOW - dur)
        start_min = BLOCK_START_MIN[b]
        slot_info = {"start": start_min, "booking_end": start_min + BOOKING_WINDOW,
                     "actual_end": start_min + dur, "tail_waste": tail_waste,
                     "gap_dead": 0, "slot_dead": tail_waste, "course": s["course"],
                     "crn": s["crn"], "instructor": s["instructor"], "duration_mins": dur,
                     "block": b, "is_idle": False, "pattern": asgn.get("pattern_label", "")}
        for day in assigned_days:
            if day not in "MTWRF": continue
            room_day_blocks[(rid, day)][b] = slot_info

    room_dead_summary = {}
    for (room_id, day), block_map in room_day_blocks.items():
        if room_id not in room_dead_summary:
            room_dead_summary[room_id] = {"room_id": room_id,
                "building": room_idx.get(room_id, {}).get("building", "?"),
                "capacity": room_idx.get(room_id, {}).get("capacity", 0),
                "total_dead": 0, "total_booked": 0, "days": defaultdict(list)}
        rs = room_dead_summary[room_id]
        for b in BLOCK_IDS:
            if b not in block_map: continue
            slot = dict(block_map[b])
            rs["days"][day].append(slot)
            rs["total_dead"]   += slot["slot_dead"]
            rs["total_booked"] += slot["duration_mins"]
            total_dead_minutes += slot["slot_dead"]

    room_dead_list = []
    for rid, rs in sorted(room_dead_summary.items(), key=lambda x: -x[1]["total_dead"]):
        booked = rs["total_booked"]; dead = rs["total_dead"]
        room_dead_list.append({
            "room_id": rid, "building": rs["building"], "capacity": rs["capacity"],
            "total_dead": dead, "total_booked": booked,
            "utilization": round(100 * (booked - dead) / max(booked, 1), 1),
            "days": {d: slots for d, slots in rs["days"].items()},
        })

    return {
        "total": total, "score": solution["score"],
        "moved": moved, "moved_pct": round(100 * moved / total, 1) if total else 0,
        "bldg_changes": bldg_changes,
        "under_count": len(under_list), "under_list": under_list,
        "total_dead_minutes": total_dead_minutes, "dead_gap_total": total_dead_minutes,
        "room_dead_list": room_dead_list, "block_rows": block_rows,
        "day_family_rows": day_family_rows,
        "course_rows": course_rows, "instr_table": instr_table, "calendar": calendar,
    }