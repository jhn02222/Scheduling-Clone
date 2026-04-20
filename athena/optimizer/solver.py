"""
Core CP-SAT solver logic — decoupled from Django views.
All constraint weights are passed in at runtime via the `weights` dict.

Dead-minutes model (SC-9)
─────────────────────────
Each active room/day is evaluated over the full set of 80-minute universal
blocks. Dead minutes are unused time inside those blocks:

    if block occupied by a class of duration d: dead = 80 - d
    if block unoccupied (but room/day is active): dead = 80

This counts unused time before, between, and after classes within the active
room/day horizon.

Credit-hour rules (updated)
────────────────────────────
3-credit: 2x80 min (TR) = 160 min  OR  3x55 min (MWF) = 165 min → range 140-175
4-credit: 2×100 min (TR) = 200 min OR  3×67 min = 201 min → range 190–230
Both patterns satisfy the weekly-minutes band; neither is preferred over the
other (user answered "either is fine").
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
BLOCK_IDS   = [b[0] for b in TIME_BLOCKS]
BLOCK_HHMM  = {b[0]: b[1] for b in TIME_BLOCKS}
BLOCK_LABEL = {b[0]: b[2] for b in TIME_BLOCKS}

# Block start times in minutes-since-midnight
BLOCK_START_MIN = {b[0]: (b[1]//100)*60 + (b[1]%100) for b in TIME_BLOCKS}

# Institutional booking window (minutes) — every section occupies this slot
BOOKING_WINDOW = 80
# Minimum desired turnaround between back-to-back sections in the same room
MIN_TURNAROUND = 20

HIST_FILL    = {1113:0.932, 2250:0.934, 2260:0.938, 2270:0.931,
                2500:0.949, 2700:0.978, 3300:0.931}

# ── Credit-hour rules (updated) ──────────────────────────────────────────────
# 3-credit: 2x80=160 (TR) or 3x55=165 (MWF) -> allow 140-175 (historical tolerance)
# 4-credit sections in this dataset often use 2x80 lecture windows (160 mins),
# so keep that viable while still allowing heavier weekly-minute patterns.
CREDIT_MINUTE_RULES   = {3: {"min": 140, "max": 175},
                          4: {"min": 150, "max": 230}}
CREDIT_DAYCOUNT_RULES = {3: {2, 3}, 4: {2, 3, 4}}

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

    # Deterministic palette fallback for non-core courses.
    palette = [
        {"face": "#93c5fd", "edge": "#2563eb"},
        {"face": "#86efac", "edge": "#16a34a"},
        {"face": "#fcd34d", "edge": "#d97706"},
        {"face": "#fca5a5", "edge": "#dc2626"},
        {"face": "#c4b5fd", "edge": "#7c3aed"},
        {"face": "#67e8f9", "edge": "#0891b2"},
        {"face": "#f9a8d4", "edge": "#db2777"},
        {"face": "#a7f3d0", "edge": "#059669"},
        {"face": "#fdba74", "edge": "#ea580c"},
        {"face": "#bae6fd", "edge": "#0284c7"},
    ]
    idx = abs(int(course_number)) % len(palette)
    return palette[idx]


# ── Helpers ──────────────────────────────────────────────────────────────────

def hhmm_to_min(x):
    x = int(x)
    return 60 * (x // 100) + (x % 100)

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
    mapping = [("M","MONDAY_IND"),("T","TUESDAY_IND"),("W","WEDNESDAY_IND"),
               ("R","THURSDAY_IND"),("F","FRIDAY_IND"),("S","SATURDAY_IND"),("U","SUNDAY_IND")]
    out = []
    for letter, col in mapping:
        v = str(row.get(col,"")).strip().upper()
        if v in {letter,"Y","YES","TRUE","1"}: out.append(letter)
    return "".join(out)


def parse_time_text_to_hhmm(text_value):
    parts = str(text_value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format '{text_value}'. Expected HH:MM.")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid time value '{text_value}'.")
    return (hour * 100) + minute


def pattern_days_to_letters(pattern_days):
    raw = str(pattern_days).strip()
    if not raw:
        return ""

    # Supports serialized index list style like "[0, 2, 4]".
    if raw.startswith("[") and raw.endswith("]"):
        day_map = {0: "M", 1: "T", 2: "W", 3: "R", 4: "F", 5: "S", 6: "U"}
        items = [chunk.strip() for chunk in raw[1:-1].split(",") if chunk.strip()]
        letters = []
        for item in items:
            idx = int(item)
            if idx not in day_map:
                raise ValueError(f"Unsupported day index '{idx}' in meeting_pattern.days")
            letters.append(day_map[idx])
        return "".join(letters)

    filtered = [ch for ch in raw.upper() if ch in "MTWRFSU"]
    return "".join(filtered)


def minutes_to_hhmm(total_minutes):
    hour = total_minutes // 60
    minute = total_minutes % 60
    return hour * 100 + minute

def normalize_course_scope(course_scope):
    scope = str(course_scope or "core").strip().lower()
    if scope not in {"core", "all_math"}:
        raise ValueError(
            f"Unsupported course_scope '{course_scope}'. Expected 'core' or 'all_math'."
        )
    return scope

# ── Data loading ─────────────────────────────────────────────────────────────

def load_data_from_csv(csv_path):
    df = pd.read_csv(csv_path)
    for col in ["COURSE_NUMBER","ACADEMIC_PERIOD","BEGIN_TIME","END_TIME",
                "MAXIMUM_ENROLLMENT","ACTUAL_ENROLLMENT","TOTAL_CREDITS_SECTION",
                "MIN_CREDITS","MAX_CREDITS"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    sp26 = df[
        (df["SUBJECT"] == "MATH") &
        (df["ACADEMIC_PERIOD"] == 202602) &
        (df["BEGIN_TIME"].notna()) &
        (df["END_TIME"].notna())
    ].copy()

    if sp26.empty:
        raise ValueError("No Spring 2026 MATH sections found for all-course scope.")

    sp26["DAYS"]       = sp26.apply(normalize_days, axis=1).replace("", "MWF")
    sp26["BEGIN_INT"]  = sp26["BEGIN_TIME"].astype(int)
    sp26["END_INT"]    = sp26["END_TIME"].astype(int)
    sp26["SKEL_BLOCK"] = sp26["BEGIN_INT"].apply(snap_block)

    rooms_raw = (
        sp26[sp26["BUILDING"].astype(str) != "NCRR"]
        .groupby(["BUILDING","ROOM"], dropna=False)["MAXIMUM_ENROLLMENT"]
        .max().reset_index()
    )
    rooms = []
    for r in rooms_raw.itertuples():
        cap = safe_int(r.MAXIMUM_ENROLLMENT, 0)
        if cap > 0:
            rooms.append({"id": f"{r.BUILDING}-{r.ROOM}",
                          "building": str(r.BUILDING), "room": str(r.ROOM), "capacity": cap})

    sections = []
    for i, (_, row) in enumerate(sp26.iterrows()):
        course = safe_int(row["COURSE_NUMBER"])
        if course is None: continue
        cap    = max(safe_int(row["MAXIMUM_ENROLLMENT"], 20), 1)
        actual = max(safe_int(row["ACTUAL_ENROLLMENT"], 0), 0)
        exp    = max(1, round(HIST_FILL.get(course, 0.85) * cap))
        credits = (safe_int(row["TOTAL_CREDITS_SECTION"])
                   or safe_int(row["MAX_CREDITS"])
                   or safe_int(row["MIN_CREDITS"]))
        days  = str(row["DAYS"])
        bi    = safe_int(row["BEGIN_INT"])
        ei    = safe_int(row["END_INT"])
        if bi is None or ei is None: continue
        wmins, dmins = weekly_mins(bi, ei, days)
        dc = count_days(days)
        if credits in CREDIT_MINUTE_RULES:
            r2 = CREDIT_MINUTE_RULES[credits]
            if not (r2["min"] <= wmins <= r2["max"]): continue
        if credits in CREDIT_DAYCOUNT_RULES:
            if dc not in CREDIT_DAYCOUNT_RULES[credits]: continue
        bldg = str(row["BUILDING"])
        il   = str(row.get("PRIMARY_INSTRUCTOR_LAST_NAME","")).strip() or "TBA"
        ifi  = str(row.get("PRIMARY_INSTRUCTOR_FIRST_NAME","")).strip()
        instr = f"{il}, {ifi}".strip(", ") or "TBA"
        skel_room = None if bldg == "NCRR" else f"{bldg}-{row['ROOM']}"

        # Tail waste = unused minutes inside the 80-min booking window
        tail_waste = max(0, BOOKING_WINDOW - dmins)

        sections.append({
            "id": i, "crn": safe_int(row["CRN"], i),
            "course": course, "title": str(row["TITLE_SHORT_DESC"]),
            "instructor": instr, "days": days, "day_count": dc,
            "begin_int": bi, "end_int": ei,
            "duration_mins": dmins, "weekly_minutes": wmins,
            "tail_waste": tail_waste,
            "credits": credits, "capacity": cap,
            "actual_enroll": actual, "exp_enroll": exp,
            "skel_block": safe_int(row["SKEL_BLOCK"], 0),
            "skel_room": skel_room, "skel_bldg": bldg,
            "color": course_color(course),
        })

    return sections, rooms


def load_data_from_db(db_path, semester, course_scope="core"):
    CORE_COURSES = [1113, 2250, 2260, 2270, 2500, 2700, 3300] 
    scope = normalize_course_scope(course_scope)

    from optimizer.models import CourseSection, Classroom

    room_qs = Classroom.objects.filter(max_enrollment__gt=0)
    rooms = [
        {
            "id": f"{r.building_id}-{r.room_number}",
            "building": str(r.building_id),
            "room": str(r.room_number),
            "capacity": int(r.max_enrollment),
        }
        for r in room_qs
    ]

    if not rooms:
        raise ValueError("No classrooms found with positive max_enrollment.")

    qs = CourseSection.objects.select_related(
        "course",
        "schedule",
        "schedule__professor",
        "schedule__classroom",
    ).filter(
        semester=semester,
        maximum_enrollment__gt=0,  # ← exclude ghost sections
    ).prefetch_related(
        "schedule__schedulemeetingblock_set__time_slot",
        "schedule__schedulemeetingblock_set__meeting_pattern",
    ).filter(semester=semester)

    if scope == "core":
        qs = qs.filter(course__course_number__in=CORE_COURSES)

    rows = qs.all()
    if not rows:
        raise ValueError(f"No course sections found for semester {semester}.")

    sections = []
    for i, cs in enumerate(rows):
        course = cs.course.course_number if cs.course else None
        if course is None:
            continue

        try:
            schedule = cs.schedule
        except Exception:
            continue

        blocks = list(schedule.schedulemeetingblock_set.all())
        if not blocks:
            continue

        smb = blocks[0]
        ts = smb.time_slot
        mp = smb.meeting_pattern

        if not ts or not mp:
            continue

        begin_int = parse_time_text_to_hhmm(ts.start_time)
        end_int_slot = parse_time_text_to_hhmm(ts.end_time)
        slot_duration_mins = hhmm_to_min(end_int_slot) - hhmm_to_min(begin_int)
        duration_mins = smb.class_duration_minutes or slot_duration_mins
        days = pattern_days_to_letters(mp.days) or "MWF"
        day_count = count_days(days)
        weekly_minutes = duration_mins * day_count
        end_int = minutes_to_hhmm(hhmm_to_min(begin_int) + duration_mins)

        credits = safe_int(cs.course.max_credits) or safe_int(cs.course.min_credits)

        if credits in CREDIT_MINUTE_RULES:
            mins_rule = CREDIT_MINUTE_RULES[credits]
            if not (mins_rule["min"] <= weekly_minutes <= mins_rule["max"]):
                continue

        if credits in CREDIT_DAYCOUNT_RULES and day_count not in CREDIT_DAYCOUNT_RULES[credits]:
            continue

        cap = max(cs.maximum_enrollment or 20, 1)
        actual = cs.actual_enrollment or 0
        exp = max(1, round(HIST_FILL.get(course, 0.85) * cap))

        prof = schedule.professor
        if prof:
            instructor = f"{prof.last_name}, {prof.first_name}".strip(", ") or "TBA"
        else:
            instructor = "TBA"

        classroom = schedule.classroom
        skel_building = str(classroom.building_id) if classroom else ""
        skel_room_val = str(classroom.room_number) if classroom else ""
        skel_room = f"{skel_building}-{skel_room_val}" if skel_building and skel_room_val else None

        tail_waste = max(0, BOOKING_WINDOW - duration_mins)

        sections.append({
            "id": i,
            "db_section_id": cs.id,
            "crn": cs.crn,
            "course": course,
            "title": cs.course.course_name,
            "instructor": instructor,
            "days": days,
            "day_count": day_count,
            "begin_int": begin_int,
            "end_int": end_int,
            "duration_mins": duration_mins,
            "weekly_minutes": weekly_minutes,
            "tail_waste": tail_waste,
            "credits": credits,
            "capacity": cap,
            "actual_enroll": actual,
            "exp_enroll": exp,
            "skel_block": snap_block(begin_int),
            "skel_room": skel_room,
            "skel_bldg": skel_building,
            "color": course_color(course),
        })

    if not sections:
        raise ValueError("No sections survived filters and credit-hour validation.")

    return sections, rooms

def load_data(*, source="csv", csv_path=None, db_path=None, semester="202602", course_scope="core"):
    source_normalized = str(source).strip().lower()
    if source_normalized == "db":
        return load_data_from_db(
            db_path=db_path,
            semester=str(semester),
            course_scope=course_scope,
        )
    if source_normalized == "csv":
        return load_data_from_csv(csv_path)
    raise ValueError(f"Unsupported solver data source '{source}'. Expected 'db' or 'csv'.")

# ── Solve ────────────────────────────────────────────────────────────────────

def build_and_solve(sections, rooms, weights, solver_time=60, num_opts=3, log_fn=print):
    W_SKEL_SLOT    = int(weights.get("w_skeleton_slot",    8))
    W_SKEL_BLDG    = int(weights.get("w_skeleton_bldg",    2))
    W_DEAD_GAP     = int(weights.get("w_dead_gap",        10))  # kept for SC-3 (instructor)
    UE_THRESH      = float(weights.get("under_enroll_threshold", 0.60))
    W_UNDER_ENROLL = int(weights.get("w_under_enroll",    12))
    W_BLOCK_OVER   = int(weights.get("w_block_over",      18))
    INSTR_MAX      = int(weights.get("instructor_max_sections", 3))
    W_INSTR_OVL    = int(weights.get("w_instr_overload",  20))
    W_LOWER_MID    = int(weights.get("w_lower_midday",     5))
    W_UPPER_NOMID  = int(weights.get("w_upper_nonmidday", 15))
    BLK_MIN_PCT    = float(weights.get("block_min_pct",   0.15))
    BLK_MAX_PCT    = float(weights.get("block_max_pct",   0.30))
    # SC-9: dead-minutes weight (room idle time > 20 min between sections)
    W_DEAD_MIN     = int(weights.get("w_dead_minutes",    3))

    MIDDAY_BLOCKS = {1, 2, 3}
    LOWER_MAX     = 2250
    ACTIVE_DAYS   = ("M", "T", "W", "R", "F")

    log_fn("Creating CP-SAT model...")
    model   = cp_model.CpModel()
    nr      = len(rooms)
    total   = len(sections)

    log_fn(f"Building assignment variables ({total} sections × {nr} rooms × {len(BLOCK_IDS)} blocks)...")
    assign = {}
    for s in sections:
        sid = s["id"]
        for ri, r in enumerate(rooms):
            if r["capacity"] < s["exp_enroll"]: continue
            for b in BLOCK_IDS:
                assign[sid, ri, b] = model.NewBoolVar(f"x_{sid}_{ri}_{b}")
    log_fn(f"  {len(assign):,} variables created.")

    # Each section exactly once
    for s in sections:
        sid = s["id"]
        choices = [assign[sid,ri,b] for ri in range(nr) for b in BLOCK_IDS
                   if (sid,ri,b) in assign]
        if not choices:
            raise ValueError(f"No feasible room for CRN {s['crn']} (exp_enroll={s['exp_enroll']})")
        model.AddExactlyOne(choices)

    sec_day_map = {
        s["id"]: set(d for d in s["days"] if d in ACTIVE_DAYS)
        for s in sections
    }

    # HC-4: room conflict (day-aware)
    log_fn("Adding room conflict constraints...")
    for ri in range(nr):
        for b in BLOCK_IDS:
            for day in ACTIVE_DAYS:
                occ = [
                    assign[s["id"],ri,b]
                    for s in sections
                    if day in sec_day_map[s["id"]] and (s["id"],ri,b) in assign
                ]
                if len(occ) > 1:
                    model.AddAtMostOne(occ)

    # HC-5: instructor conflict (day-aware)
    log_fn("Adding instructor conflict constraints...")
    by_instr = defaultdict(list)
    for s in sections:
        if s["instructor"] != "TBA": by_instr[s["instructor"]].append(s["id"])
    for instr, sids in by_instr.items():
        if len(sids) < 2: continue
        for b in BLOCK_IDS:
            for day in ACTIVE_DAYS:
                vv = [
                    assign[sid,ri,b]
                    for sid in sids
                    if day in sec_day_map[sid]
                    for ri in range(nr)
                    if (sid,ri,b) in assign
                ]
                if len(vv) > 1:
                    model.AddAtMostOne(vv)

    # HC-6: Hard 20-minute room turnaround between block assignments.
    # For each pair (b1, b2), two sections cannot share a room when:
    #   start(b2) - end_of_first_section < MIN_TURNAROUND
    # on any shared meeting day.
    log_fn("Adding HC-6: hard 20-min room turnaround constraints...")

    hc6_count = 0
    for b1 in BLOCK_IDS:
        for b2 in BLOCK_IDS:
            if b2 <= b1: continue
            for ri in range(nr):
                s1_cands = [s for s in sections if (s["id"],ri,b1) in assign]
                s2_cands = [s for s in sections if (s["id"],ri,b2) in assign]
                if not s1_cands or not s2_cands: continue
                for s1 in s1_cands:
                    for s2 in s2_cands:
                        if s1["id"] == s2["id"]:
                            continue

                        shared = sec_day_map[s1["id"]] & sec_day_map[s2["id"]]
                        if not shared:
                            continue

                        end_s1 = BLOCK_START_MIN[b1] + s1["duration_mins"]
                        gap = BLOCK_START_MIN[b2] - end_s1

                        if gap >= MIN_TURNAROUND:
                            continue

                        model.Add(assign[s1["id"],ri,b1] + assign[s2["id"],ri,b2] <= 1)
                        hc6_count += 1
    log_fn(f"  HC-6: {hc6_count} room-pair turnaround constraints added.")

    # HC-7: Uniform block across all meeting days (already implied by the block
    # variable structure — one block per section covers all its days — but we
    # document this explicitly. No extra constraints needed: the assign variable
    # x[sid,ri,b] inherently applies to ALL days in section.days simultaneously.)
    log_fn("HC-7: block uniformity is structural (one block per section, all days).")

    # HC-2: block distribution band
    log_fn("Adding block distribution constraints...")
    blk_floor = max(1, math.floor(total * BLK_MIN_PCT))
    blk_ceil  = math.ceil(total * BLK_MAX_PCT)

    in_block = {}
    for s in sections:
        sid = s["id"]
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"ib_{sid}_{b}")
            choices = [assign[sid,ri,b] for ri in range(nr) if (sid,ri,b) in assign]
            if choices: model.AddMaxEquality(v, choices)
            else: model.Add(v == 0)
            in_block[sid,b] = v

    blk_over = {}
    for b in BLOCK_IDS:
        sb = model.NewIntVar(0, total, f"sb_{b}")
        model.Add(sb == sum(in_block[s["id"],b] for s in sections))
        model.Add(sb >= blk_floor)
        model.Add(sb <= blk_ceil)
        ov = model.NewIntVar(0, total, f"ov_{b}")
        model.Add(ov >= sb - math.floor(total * BLK_MAX_PCT))
        model.Add(ov >= 0)
        blk_over[b] = ov

    # Instructor-at-block helpers (for SC-3)
    iab = {}
    for instr, sids in by_instr.items():
        iab[instr] = {}
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"iab_{instr}_{b}")
            choices = [assign[sid,ri,b] for sid in sids for ri in range(nr) if (sid,ri,b) in assign]
            if choices: model.AddMaxEquality(v, choices)
            else: model.Add(v == 0)
            iab[instr][b] = v

    # ── Objective ────────────────────────────────────────────────────────────
    obj = []

    # SC-1 skeleton slot fidelity
    for s in sections:
        sid=s["id"]; sb=s["skel_block"]
        for ri in range(nr):
            for b in BLOCK_IDS:
                if (sid,ri,b) not in assign: continue
                d = abs(b - sb)
                if d: obj.append(W_SKEL_SLOT * d * assign[sid,ri,b])

    # SC-2 building continuity
    for s in sections:
        sid=s["id"]; sbld=s["skel_bldg"]
        for ri,r in enumerate(rooms):
            if r["building"] == sbld: continue
            for b in BLOCK_IDS:
                if (sid,ri,b) in assign: obj.append(W_SKEL_BLDG * assign[sid,ri,b])

    # SC-3 instructor dead-gap (block-level, kept for instructor scheduling quality)
    for instr, bmap in iab.items():
        for b1 in BLOCK_IDS:
            for b2 in BLOCK_IDS:
                if b2 <= b1: continue
                gap = b2 - b1
                if gap <= 1: continue
                both = model.NewBoolVar(f"dg_{instr}_{b1}_{b2}")
                model.Add(both <= bmap[b1]); model.Add(both <= bmap[b2])
                model.Add(both >= bmap[b1] + bmap[b2] - 1)
                obj.append(W_DEAD_GAP * (gap - 1) * both)

    # SC-4 under-enrollment
    for s in sections:
        if s["exp_enroll"] / max(s["capacity"],1) >= UE_THRESH: continue
        flag = model.NewBoolVar(f"ue_{s['id']}")
        choices = [assign[s["id"],ri,b] for ri in range(nr) for b in BLOCK_IDS
                   if (s["id"],ri,b) in assign]
        model.AddMaxEquality(flag, choices)
        obj.append(W_UNDER_ENROLL * flag)

    # SC-5 block over-cap
    for b in BLOCK_IDS: obj.append(W_BLOCK_OVER * blk_over[b])

    # SC-6 instructor overload
    for instr, sids in by_instr.items():
        if len(sids) > INSTR_MAX:
            obj.append(W_INSTR_OVL * (len(sids) - INSTR_MAX))

    # SC-8 level distribution
    for s in sections:
        sid=s["id"]; is_lower=(s["course"] <= LOWER_MAX)
        for ri in range(nr):
            for b in BLOCK_IDS:
                if (sid,ri,b) not in assign: continue
                mid = b in MIDDAY_BLOCKS
                if is_lower and mid:           obj.append(W_LOWER_MID   * assign[sid,ri,b])
                elif not is_lower and not mid: obj.append(W_UPPER_NOMID * assign[sid,ri,b])

    # SC-9 room dead-minutes (aligned with analytics definition).
    # Dead minutes across active room/day horizons are equivalent (up to a
    # constant) to minimizing the count of active (room, day) pairs. We use
    # that compact form to keep solve performance stable.
    log_fn("Adding SC-9 room dead-minutes penalties...")

    room_day_used = {}
    for ri in range(nr):
        for day in ACTIVE_DAYS:
            for b in BLOCK_IDS:
                v = model.NewBoolVar(f"room_used_{ri}_{day}_{b}")
                occ = [
                    assign[s["id"],ri,b]
                    for s in sections
                    if day in sec_day_map[s["id"]] and (s["id"],ri,b) in assign
                ]

                if occ:
                    model.AddMaxEquality(v, occ)
                else:
                    model.Add(v == 0)

                room_day_used[ri,day,b] = v

    sc9_terms = 0

    for ri in range(nr):
        for day in ACTIVE_DAYS:
            active = model.NewBoolVar(f"room_day_active_{ri}_{day}")
            model.AddMaxEquality(
                active,
                [room_day_used[ri, day, b] for b in BLOCK_IDS],
            )
            obj.append(W_DEAD_MIN * BOOKING_WINDOW * len(BLOCK_IDS) * active)
            sc9_terms += 1

    log_fn(f"  SC-9: {sc9_terms} active-room-day terms added.")

    log_fn(f"Minimising objective ({len(obj)} terms)...")
    model.Minimize(sum(obj) if obj else model.NewIntVar(0,0,"zero"))

    room_ids  = [r["id"] for r in rooms]
    solutions = []

    for opt_i in range(num_opts):
        log_fn(f"Solving option {chr(65+opt_i)} (seed={17+opt_i*31}, limit={solver_time}s)...")
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
            sid=s["id"]; placed=False
            for ri in range(nr):
                for b in BLOCK_IDS:
                    if (sid,ri,b) in assign and solver.Value(assign[sid,ri,b]):
                        sol[sid] = {"block":b, "room":room_ids[ri]}
                        chosen.append(assign[sid,ri,b]); placed=True; break
                if placed: break
            if not placed:
                sol[sid] = {"block":s["skel_block"], "room":s["skel_room"] or "UNASSIGNED"}

        score = int(round(solver.ObjectiveValue()))
        solutions.append({"label": f"Option {chr(65+opt_i)}", "score": score, "assignment": sol})
        log_fn(f"  Option {chr(65+opt_i)} score={score}")
        if chosen: model.Add(sum(chosen) <= len(chosen) - 3)

    log_fn(f"Finished: {len(solutions)} solution(s) found.")
    return solutions


# ── Analytics ────────────────────────────────────────────────────────────────

def analyze(solution, sections, rooms, weights=None):
    w       = weights or {}
    UE_THR  = float(w.get("under_enroll_threshold", 0.60))
    BLK_MAX = float(w.get("block_max_pct", 0.30))

    sec_idx  = {s["id"]: s for s in sections}
    room_idx = {r["id"]: r for r in rooms}
    total    = len(sections)
    sol      = solution["assignment"]

    moved=0; bldg_changes=0; under_list=[]
    block_dist = defaultdict(int)
    by_instr   = defaultdict(list)

    for sid, asgn in sol.items():
        s = sec_idx[sid]; b = asgn["block"]
        block_dist[b] += 1
        if b != s["skel_block"]: moved += 1
        r = room_idx.get(asgn["room"])
        if r and r["building"] != s["skel_bldg"]: bldg_changes += 1
        fill = s["exp_enroll"] / max(s["capacity"],1)
        if fill < UE_THR:
            under_list.append({"crn":s["crn"],"course":s["course"],
                                "instructor":s["instructor"],
                                "fill_pct":round(fill*100,1),
                                "exp":s["exp_enroll"],"cap":s["capacity"]})
        if s["instructor"] != "TBA": by_instr[s["instructor"]].append(b)

    soft_cap = math.floor(total * BLK_MAX)
    block_rows = []
    for b in BLOCK_IDS:
        cnt  = block_dist.get(b, 0)
        pct  = round(100*cnt/total, 1) if total else 0
        over = max(0, cnt - soft_cap)
        blk_floor = max(1, math.floor(total * float(w.get("block_min_pct",0.15))))
        block_rows.append({"id":b, "label":BLOCK_LABEL[b], "count":cnt,
                           "pct":pct, "over":over,
                           "floor_ok": cnt >= blk_floor,
                           "ceil_ok":  cnt <= soft_cap})

    course_stats = defaultdict(lambda: {"sections":0,"exp":0,"cap":0})
    for sid, asgn in sol.items():
        s = sec_idx[sid]; cs = course_stats[s["course"]]
        cs["sections"] += 1; cs["exp"] += s["exp_enroll"]; cs["cap"] += s["capacity"]
    course_rows = [{"course":k, "sections":v["sections"],
                    "avg_fill":round(100*v["exp"]/max(v["cap"],1),1)}
                   for k,v in sorted(course_stats.items())]

    instr_table = {}
    for sid, asgn in sol.items():
        s=sec_idx[sid]
        if s["instructor"]=="TBA": continue
        instr_table.setdefault(s["instructor"],[]).append({
            "block": asgn["block"], "block_label": BLOCK_LABEL[asgn["block"]],
            "course":s["course"],"crn":s["crn"],"days":s["days"],
            "room": asgn["room"],
        })
    for k in instr_table: instr_table[k].sort(key=lambda x:x["block"])

    # Calendar data
    calendar = []
    for sid, asgn in sol.items():
        s=sec_idx[sid]; b=asgn["block"]
        sh,sm = divmod(BLOCK_HHMM[b], 100)
        start = sh*60+sm
        end   = start + s["duration_mins"]
        for day in s["days"]:
            if day not in "MTWRF": continue
            calendar.append({
                "sid":sid,"crn":s["crn"],"course":s["course"],
                "title":s["title"],"instructor":s["instructor"],
                "days":s["days"],"day":day,
                "start":start,"end":end,
                "duration_mins":s["duration_mins"],
                "tail_waste":s["tail_waste"],
                "room":asgn["room"],
                "face":s["color"]["face"],"edge":s["color"]["edge"],
                "moved": b != s["skel_block"],
            })

    # ── Room dead-minutes analysis ────────────────────────────────────────────
    # Definition used here:
    # For each active (room, day), each universal 80-minute block contributes
    # dead minutes equal to the unused portion of that block.
    # - occupied block: 80 - class_duration
    # - unoccupied block: 80
    # This counts unused time before, between, and after classes.

    room_day_blocks = defaultdict(dict)
    for sid, asgn in sol.items():
        s = sec_idx[sid]
        room_id = asgn["room"]
        block = asgn["block"]
        start_min = BLOCK_START_MIN[block]
        booking_end = start_min + BOOKING_WINDOW
        actual_end = start_min + s["duration_mins"]

        slot_info = {
            "start": start_min,
            "booking_end": booking_end,
            "actual_end": actual_end,
            "tail_waste": max(0, BOOKING_WINDOW - s["duration_mins"]),
            "gap_dead": 0,
            "slot_dead": max(0, BOOKING_WINDOW - s["duration_mins"]),
            "course": s["course"],
            "crn": s["crn"],
            "instructor": s["instructor"],
            "duration_mins": s["duration_mins"],
            "block": block,
            "is_idle": False,
        }

        for day in s["days"]:
            if day not in "MTWRF":
                continue
            room_day_blocks[(room_id, day)][block] = slot_info

    room_dead_summary = {}
    total_dead_minutes = 0

    for (room_id, day), block_map in room_day_blocks.items():
        if room_id not in room_dead_summary:
            room_dead_summary[room_id] = {
                "room_id": room_id,
                "building": room_idx.get(room_id, {}).get("building", "?"),
                "capacity": room_idx.get(room_id, {}).get("capacity", 0),
                "total_dead": 0,
                "total_booked": 0,
                "days": defaultdict(list),
            }
        rs = room_dead_summary[room_id]

        # Active room-day uses full universal block horizon.
        for block in BLOCK_IDS:
            start_min = BLOCK_START_MIN[block]
            booking_end = start_min + BOOKING_WINDOW

            if block in block_map:
                slot = dict(block_map[block])
            else:
                slot = {
                    "start": start_min,
                    "booking_end": booking_end,
                    "actual_end": start_min,
                    "tail_waste": 0,
                    "gap_dead": BOOKING_WINDOW,
                    "slot_dead": BOOKING_WINDOW,
                    "course": None,
                    "crn": None,
                    "instructor": "",
                    "duration_mins": 0,
                    "block": block,
                    "is_idle": True,
                }

            rs["days"][day].append(slot)
            rs["total_dead"] += slot["slot_dead"]
            rs["total_booked"] += BOOKING_WINDOW
            total_dead_minutes += slot["slot_dead"]

    # Convert defaultdict to plain dict for JSON serialisation
    room_dead_list = []
    for rid, rs in sorted(room_dead_summary.items(),
                          key=lambda x: -x[1]["total_dead"]):
        booked = rs["total_booked"]
        dead   = rs["total_dead"]
        util   = round(100 * (booked - dead) / max(booked, 1), 1)
        room_dead_list.append({
            "room_id":      rid,
            "building":     rs["building"],
            "capacity":     rs["capacity"],
            "total_dead":   dead,
            "total_booked": booked,
            "utilization":  util,
            "days":         {d: slots for d, slots in rs["days"].items()},
        })

    return {
        "total": total,
        "score": solution["score"],
        "moved": moved, "moved_pct": round(100*moved/total,1) if total else 0,
        "bldg_changes": bldg_changes,
        "under_count": len(under_list), "under_list": under_list,
        "total_dead_minutes": total_dead_minutes,
        "dead_gap_total": total_dead_minutes,
        "room_dead_list": room_dead_list,
        "block_rows": block_rows,
        "course_rows": course_rows,
        "instr_table": instr_table,
        "calendar": calendar,
    }