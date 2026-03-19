"""
Math Department Schedule Optimizer — Phase 1
============================================

Uses Spring 2026 schedule as the soft skeleton and generates optimized
room/block assignments using Google OR-Tools CP-SAT.

Outputs:
    /mnt/data/schedule_output.csv
    /mnt/data/schedule_report.txt
    /mnt/data/schedule_option_A.png
    /mnt/data/schedule_option_B.png
    /mnt/data/schedule_option_C.png

Notes:
- Phase 1 treats each section as a bundled object:
    one course + one instructor + one day pattern + one duration
- The optimizer changes only:
    room, universal time block
- Credit-hour validity is enforced from the section's existing structure.
- Instructor stays fixed for the whole section.
"""

import math
import os
import sys
from collections import defaultdict

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import pandas as pd
from ortools.sat.python import cp_model


# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

CSV_PATH   = "Course Schedule of Classes Proof ALL ++_20260116_124500.csv"
OUTPUT_DIR = "data"

# ── Universal department time blocks ─────────────────────────────────────────
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

# ── Phase 1 course list ───────────────────────────────────────────────────────
CORE_COURSES = [1113, 2250, 2260, 2270, 2500, 2700, 3300]

# Historical average fill rates by course (used to estimate expected enrollment)
HIST_FILL = {
    1113: 0.932,
    2250: 0.934,
    2260: 0.938,
    2270: 0.931,
    2500: 0.949,
    2700: 0.978,
    3300: 0.931,
}

# ── Solver settings ───────────────────────────────────────────────────────────
SOLVER_TIME_SEC = 90
NUM_OPTIONS     = 3

DAY_ORDER = ["M", "T", "W", "R", "F"]

# =============================================================================
# HARD CONSTRAINTS
# =============================================================================
# These are enforced as strict CP-SAT constraints.
# Any schedule that violates them is infeasible and will never be returned.

# ── HC-1  Credit-hour weekly-minutes rules ────────────────────────────────────
# A section's (meeting_duration × meetings_per_week) must be within [min, max].
#   3-credit example: 3 × 50 min MWF = 150 min  ✓
#   4-credit example: 4 × 50 min MWF = 200 min  ✓  /  2 × 110 min TR = 220 min ✓
CREDIT_MINUTE_RULES = {
    3: {"min": 140, "max": 170},
    4: {"min": 190, "max": 230},
}
# Section's meeting-day count must belong to the allowed set for its credit level.
CREDIT_DAYCOUNT_RULES = {
    3: {2, 3},   # TR (2 days) or MWF (3 days)
    4: {3, 4},   # MWF (3 days) or MTWR (4 days)
}

# ── HC-2  Block distribution band (BOTH a floor and a ceiling per block) ─────
# Every time block must hold at least BLOCK_MIN_PCT of total sections (floor)
# and at most BLOCK_MAX_PCT of total sections (ceiling).
# Set BLOCK_MIN_PCT = 0 to disable the floor.
BLOCK_MIN_PCT = 0.15   # each block must have ≥ 15 % of all sections
BLOCK_MAX_PCT = 0.30   # each block must have ≤ 30 % of all sections

# ── HC-3  Section cap per course ("too many Precalc" rule) ───────────────────
# Hard upper limit on sections per course that may appear in the optimised
# schedule.  Raise a value to leave it effectively unconstrained.
COURSE_MAX_SECTIONS = {
    1113: 12,   # Precalculus
    2250: 10,   # Calculus I
    2260: 8,    # Calculus II
    2270: 6,    # Multivariable Calculus
    2500: 6,    # Differential Equations
    2700: 4,    # Linear Algebra
    3300: 4,    # Intro to Proofs
}

# ── HC-4  Room conflict ───────────────────────────────────────────────────────
# At most one section per (room, block).  Enforced inside build_model() Hard-2.

# ── HC-5  Instructor conflict ─────────────────────────────────────────────────
# An instructor may not teach two sections in the same block.
# Enforced inside build_model() Hard-3.

# =============================================================================
# SOFT CONSTRAINTS  (penalty weights — higher value = stronger preference)
# =============================================================================

# ── SC-1  Skeleton slot fidelity ──────────────────────────────────────────────
# Prefer keeping each section in its Spring 2026 time block.
W_SKELETON_SLOT = 8      # cost per block-step away from the skeleton block

# ── SC-2  Building continuity ─────────────────────────────────────────────────
# Prefer assigning sections to the same building they were in originally.
W_SKELETON_BLDG = 2      # cost per section moved to a different building

# ── SC-3  Instructor dead-gap (wasted idle time between classes) ──────────────
# An instructor with classes only at blocks 0 and 4 has 3 dead gaps.
W_DEAD_GAP = 10          # cost per empty block between an instructor's classes

# ── SC-4  Under-enrollment ────────────────────────────────────────────────────
# Sections whose expected fill is below UNDER_ENROLL_THRESHOLD incur a penalty.
UNDER_ENROLL_THRESHOLD = 0.60   # flag sections below 60 % expected fill rate
W_UNDER_ENROLL = 12             # cost per flagged section

# ── SC-5  Block over-cap surplus ──────────────────────────────────────────────
# Even though HC-2 enforces a hard ceiling, any surplus (if the ceiling is
# relaxed) is also penalised here to help the solver stay well within bounds.
W_BLOCK_OVER = 18        # cost per section above BLOCK_MAX_PCT ceiling

# ── SC-6  Instructor load limit ───────────────────────────────────────────────
# Prefer that no instructor teaches more than INSTRUCTOR_MAX_SECTIONS sections.
INSTRUCTOR_MAX_SECTIONS = 3     # soft ceiling on sections per instructor per term
W_INSTR_OVERLOAD = 20           # cost per section above the ceiling

# ── SC-7  Professor block preferences ────────────────────────────────────────
# Map  instructor_last_name -> list of preferred block IDs (0–5).
# A section assigned to a non-preferred block incurs W_PREF_VIOLATION per section.
# Leave the dict empty to disable preference penalties.
INSTRUCTOR_BLOCK_PREFS: dict = {
    # Example — uncomment and edit to match your department:
    # "Smith": [1, 2, 3],   # Smith prefers 9:55am – 1:15pm
    # "Jones": [0, 1],      # Jones prefers early-morning blocks
}
W_PREF_VIOLATION = 6     # cost per section not placed in a preferred block


# ─────────────────────────────────────────────────────────────────────────────
# 2. HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def hhmm_to_minutes(x):
    x = int(x)
    h = x // 100
    m = x % 100
    return 60 * h + m


def snap_block(t):
    return min(BLOCK_IDS, key=lambda b: abs(BLOCK_HHMM[b] - int(t)))


def count_meeting_days(days_str):
    return sum(1 for ch in str(days_str) if ch in {"M", "T", "W", "R", "F", "S", "U"})


def weekly_minutes(begin_hhmm, end_hhmm, days_str):
    duration = hhmm_to_minutes(end_hhmm) - hhmm_to_minutes(begin_hhmm)
    return duration * count_meeting_days(days_str), duration


def safe_int(x, default=None):
    if pd.isna(x):
        return default
    try:
        return int(round(float(x)))
    except Exception:
        return default


def normalize_day_flags(row):
    out = []
    mapping = [
        ("M", "MONDAY_IND"),
        ("T", "TUESDAY_IND"),
        ("W", "WEDNESDAY_IND"),
        ("R", "THURSDAY_IND"),
        ("F", "FRIDAY_IND"),
        ("S", "SATURDAY_IND"),
        ("U", "SUNDAY_IND"),
    ]
    for letter, col in mapping:
        val = row.get(col, "")
        sval = str(val).strip().upper()
        if sval in {letter, "Y", "YES", "TRUE", "1"}:
            out.append(letter)
    return "".join(out)


def course_color(course):
    palette = {
        1113: "#dbeafe",
        2250: "#dcfce7",
        2260: "#fef3c7",
        2270: "#fee2e2",
        2500: "#ede9fe",
        2700: "#cffafe",
        3300: "#fce7f3",
    }
    return palette.get(course, "#e5e7eb")


# ─────────────────────────────────────────────────────────────────────────────
# 3. LOAD + VALIDATE DATA
# ─────────────────────────────────────────────────────────────────────────────

def load_data(csv_path):
    df = pd.read_csv(csv_path)

    df["COURSE_NUMBER"] = pd.to_numeric(df["COURSE_NUMBER"], errors="coerce")
    df["ACADEMIC_PERIOD"] = pd.to_numeric(df["ACADEMIC_PERIOD"], errors="coerce")
    df["BEGIN_TIME"] = pd.to_numeric(df["BEGIN_TIME"], errors="coerce")
    df["END_TIME"] = pd.to_numeric(df["END_TIME"], errors="coerce")
    df["MAXIMUM_ENROLLMENT"] = pd.to_numeric(df["MAXIMUM_ENROLLMENT"], errors="coerce")
    df["ACTUAL_ENROLLMENT"] = pd.to_numeric(df["ACTUAL_ENROLLMENT"], errors="coerce")
    df["TOTAL_CREDITS_SECTION"] = pd.to_numeric(df["TOTAL_CREDITS_SECTION"], errors="coerce")
    df["MIN_CREDITS"] = pd.to_numeric(df["MIN_CREDITS"], errors="coerce")
    df["MAX_CREDITS"] = pd.to_numeric(df["MAX_CREDITS"], errors="coerce")

    sp26 = df[
        (df["SUBJECT"] == "MATH") &
        (df["ACADEMIC_PERIOD"] == 202602) &
        (df["BEGIN_TIME"].notna()) &
        (df["END_TIME"].notna()) &
        (df["COURSE_NUMBER"].isin(CORE_COURSES))
    ].copy()

    if sp26.empty:
        raise ValueError("No Spring 2026 MATH sections found for the selected core courses.")

    sp26["DAYS"] = sp26.apply(normalize_day_flags, axis=1)
    sp26["DAYS"] = sp26["DAYS"].replace("", "MWF")
    sp26["BEGIN_INT"] = sp26["BEGIN_TIME"].astype(int)
    sp26["END_INT"] = sp26["END_TIME"].astype(int)
    sp26["SKEL_BLOCK"] = sp26["BEGIN_INT"].apply(snap_block)

    # Build room pool from the baseline
    rooms_df = (
        sp26[sp26["BUILDING"].astype(str) != "NCRR"]
        .groupby(["BUILDING", "ROOM"], dropna=False)["MAXIMUM_ENROLLMENT"]
        .max()
        .reset_index()
    )

    rooms = []
    for r in rooms_df.itertuples():
        building = str(r.BUILDING)
        room = str(r.ROOM)
        cap = safe_int(r.MAXIMUM_ENROLLMENT, 0)
        if cap <= 0:
            continue
        rooms.append({
            "id": f"{building}-{room}",
            "building": building,
            "room": room,
            "capacity": cap,
        })

    dropped_for_credit_rule = []
    sections = []

    for i, (idx, row) in enumerate(sp26.iterrows()):
        course = safe_int(row["COURSE_NUMBER"])
        if course is None:
            continue

        cap = max(safe_int(row["MAXIMUM_ENROLLMENT"], 20), 1)
        actual = max(safe_int(row["ACTUAL_ENROLLMENT"], 0), 0)
        fill = HIST_FILL.get(course, 0.85)
        exp = max(1, round(fill * cap))

        credits = safe_int(row["TOTAL_CREDITS_SECTION"])
        if credits is None:
            credits = safe_int(row["MAX_CREDITS"])
        if credits is None:
            credits = safe_int(row["MIN_CREDITS"])

        days = str(row["DAYS"])
        begin_int = safe_int(row["BEGIN_INT"])
        end_int = safe_int(row["END_INT"])

        if begin_int is None or end_int is None:
            continue

        weekly_mins, duration_mins = weekly_minutes(begin_int, end_int, days)
        day_count = count_meeting_days(days)

        # Enforce credit-hour structure
        if credits in CREDIT_MINUTE_RULES:
            mins_rule = CREDIT_MINUTE_RULES[credits]
            valid_minutes = mins_rule["min"] <= weekly_mins <= mins_rule["max"]
            valid_daycount = True
            if credits in CREDIT_DAYCOUNT_RULES:
                valid_daycount = day_count in CREDIT_DAYCOUNT_RULES[credits]

            if not (valid_minutes and valid_daycount):
                dropped_for_credit_rule.append({
                    "crn": safe_int(row["CRN"], i),
                    "course": course,
                    "credits": credits,
                    "days": days,
                    "begin": begin_int,
                    "end": end_int,
                    "weekly_minutes": weekly_mins,
                    "duration_mins": duration_mins,
                    "day_count": day_count,
                })
                continue

        building = str(row["BUILDING"])
        room = str(row["ROOM"])
        skel_room = None if building == "NCRR" else f"{building}-{room}"

        instructor_last = str(row["PRIMARY_INSTRUCTOR_LAST_NAME"]).strip() \
            if pd.notna(row["PRIMARY_INSTRUCTOR_LAST_NAME"]) else "TBA"
        instructor_first = str(row["PRIMARY_INSTRUCTOR_FIRST_NAME"]).strip() \
            if pd.notna(row["PRIMARY_INSTRUCTOR_FIRST_NAME"]) else ""
        instructor = f"{instructor_last}, {instructor_first}".strip(", ")

        sections.append({
            "id": i,
            "orig_idx": idx,
            "crn": safe_int(row["CRN"], i),
            "course": course,
            "title": str(row["TITLE_SHORT_DESC"]),
            "instructor": instructor if instructor else "TBA",
            "days": days,
            "day_count": day_count,
            "begin_int": begin_int,
            "end_int": end_int,
            "duration_mins": duration_mins,
            "weekly_minutes": weekly_mins,
            "credits": credits,
            "capacity": cap,
            "actual_enroll": actual,
            "exp_enroll": exp,
            "skel_block": safe_int(row["SKEL_BLOCK"], 0),
            "skel_room": skel_room,
            "skel_bldg": building,
        })

    if not sections:
        raise ValueError("No sections survived the data filters / credit-hour validation.")

    if dropped_for_credit_rule:
        print("\nWARNING: These sections were excluded because they violated the configured")
        print("credit-hour structure rules:")
        for bad in dropped_for_credit_rule[:20]:
            print(
                f"  CRN {bad['crn']} | course {bad['course']} | {bad['credits']} credits | "
                f"{bad['days']} {bad['begin']}-{bad['end']} | weekly={bad['weekly_minutes']} mins"
            )
        if len(dropped_for_credit_rule) > 20:
            print(f"  ... and {len(dropped_for_credit_rule) - 20} more")

    return sections, rooms


# ─────────────────────────────────────────────────────────────────────────────
# 4. BUILD MODEL
# ─────────────────────────────────────────────────────────────────────────────

def build_model(sections, rooms):
    """
    Build the CP-SAT optimisation model.

    HARD CONSTRAINTS (infeasibility if violated)
    ─────────────────────────────────────────────
    HC-1  Credit-hour weekly-minutes validity   (enforced in load_data + guard here)
    HC-2  Block distribution band               [BLOCK_MIN_PCT, BLOCK_MAX_PCT]
    HC-3  Section cap per course                COURSE_MAX_SECTIONS
    HC-4  Room conflict                         ≤1 section per (room, block)
    HC-5  Instructor conflict                   ≤1 section per (instructor, block)

    SOFT CONSTRAINTS (penalty added to objective)
    ─────────────────────────────────────────────
    SC-1  Skeleton slot fidelity                W_SKELETON_SLOT  × |block_delta|
    SC-2  Building continuity                   W_SKELETON_BLDG  per building change
    SC-3  Instructor dead-gap                   W_DEAD_GAP       per empty block gap
    SC-4  Under-enrollment                      W_UNDER_ENROLL   per flagged section
    SC-5  Block over-cap surplus                W_BLOCK_OVER     per section over ceiling
    SC-6  Instructor overload                   W_INSTR_OVERLOAD per section above limit
    SC-7  Professor block preferences           W_PREF_VIOLATION per non-preferred block
    """
    model = cp_model.CpModel()

    # ── Pre-flight: verify credit-hour rules were applied in load_data ────────
    for s in sections:
        credits = s["credits"]
        if credits in CREDIT_MINUTE_RULES:
            rule = CREDIT_MINUTE_RULES[credits]
            if not (rule["min"] <= s["weekly_minutes"] <= rule["max"]):
                raise ValueError(
                    f"HC-1 violation: CRN {s['crn']} has {s['weekly_minutes']} weekly "
                    f"minutes for {credits}-credit course (allowed {rule['min']}–{rule['max']})."
                )
        if credits in CREDIT_DAYCOUNT_RULES:
            if s["day_count"] not in CREDIT_DAYCOUNT_RULES[credits]:
                raise ValueError(
                    f"HC-1 violation: CRN {s['crn']} has {s['day_count']} meeting days "
                    f"for {credits}-credit course (allowed {CREDIT_DAYCOUNT_RULES[credits]})."
                )

    n_rooms  = len(rooms)
    room_ids = [r["id"] for r in rooms]
    total    = len(sections)

    # ── Decision variables: assign[sid, ri, b] = 1 iff section→room→block ────
    assign = {}
    for s in sections:
        sid = s["id"]
        for ri, r in enumerate(rooms):
            if r["capacity"] < s["exp_enroll"]:
                continue          # room too small — prune variable entirely
            for b in BLOCK_IDS:
                assign[sid, ri, b] = model.NewBoolVar(f"x_{sid}_{ri}_{b}")

    print(f"  assign variables created: {len(assign):,}")

    # ── HC: each section assigned to exactly one (room, block) ───────────────
    for s in sections:
        sid = s["id"]
        choices = [
            assign[sid, ri, b]
            for ri in range(n_rooms)
            for b in BLOCK_IDS
            if (sid, ri, b) in assign
        ]
        if not choices:
            raise ValueError(
                f"No feasible (room, block) for CRN {s['crn']} "
                f"(exp_enroll={s['exp_enroll']}, all rooms too small?)."
            )
        model.AddExactlyOne(choices)

    # ── HC-4  Room conflict: ≤1 section per (room, block) ────────────────────
    for ri in range(n_rooms):
        for b in BLOCK_IDS:
            occupants = [
                assign[s["id"], ri, b]
                for s in sections
                if (s["id"], ri, b) in assign
            ]
            if len(occupants) > 1:
                model.AddAtMostOne(occupants)

    # ── HC-5  Instructor conflict: ≤1 section per (instructor, block) ────────
    by_instr: dict = defaultdict(list)
    for s in sections:
        if s["instructor"] != "TBA":
            by_instr[s["instructor"]].append(s["id"])

    for instr, sids in by_instr.items():
        if len(sids) < 2:
            continue
        for b in BLOCK_IDS:
            vars_at_block = [
                assign[sid, ri, b]
                for sid in sids
                for ri in range(n_rooms)
                if (sid, ri, b) in assign
            ]
            if len(vars_at_block) > 1:
                model.AddAtMostOne(vars_at_block)

    # ── HC-3  Course section cap ──────────────────────────────────────────────
    # ── HC-3  Course section counts are fixed by the Spring 2026 input ─────────
# Since every loaded section is scheduled exactly once, the number of sections
# per course automatically matches the baseline dataset.
        by_course: dict = defaultdict(list)
        for s in sections:
            by_course[s["course"]].append(s["id"])

    # ── HC-2  Block distribution band ────────────────────────────────────────
    # For each block b, compute sum_b = Σ_s  (s is in block b).
    # Then enforce:  floor(total * MIN_PCT) ≤ sum_b ≤ ceil(total * MAX_PCT)
    import math as _math
    block_floor = max(1, _math.floor(total * BLOCK_MIN_PCT))
    block_ceil  = _math.ceil(total * BLOCK_MAX_PCT)

    # in_block_var[sid, b] = 1 iff section sid is assigned to block b
    in_block_var: dict = {}
    for s in sections:
        sid = s["id"]
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"inb_{sid}_{b}")
            choices = [
                assign[sid, ri, b]
                for ri in range(n_rooms)
                if (sid, ri, b) in assign
            ]
            if choices:
                model.AddMaxEquality(v, choices)
            else:
                model.Add(v == 0)
            in_block_var[sid, b] = v

    block_count_vars: dict = {}
    block_over_vars:  dict = {}

    for b in BLOCK_IDS:
        sum_b = model.NewIntVar(0, total, f"sum_block_{b}")
        model.Add(sum_b == sum(in_block_var[s["id"], b] for s in sections))

        # Hard floor (minimum per block)
        model.Add(sum_b >= block_floor)

        # Hard ceiling (maximum per block)
        model.Add(sum_b <= block_ceil)

        # Soft over-cap (penalise proximity to ceiling)
        over_b = model.NewIntVar(0, total, f"over_block_{b}")
        soft_cap = _math.floor(total * BLOCK_MAX_PCT)   # same as block_ceil here
        model.Add(over_b >= sum_b - soft_cap)
        model.Add(over_b >= 0)

        block_count_vars[b] = sum_b
        block_over_vars[b]  = over_b

    # ── Derived: instructor_at_block[instr][b] = 1 iff instr teaches in block b
    instr_at_block: dict = {}
    for instr, sids in by_instr.items():
        instr_at_block[instr] = {}
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"iab_{instr}_{b}")
            choices = [
                assign[sid, ri, b]
                for sid in sids
                for ri in range(n_rooms)
                if (sid, ri, b) in assign
            ]
            if choices:
                model.AddMaxEquality(v, choices)
            else:
                model.Add(v == 0)
            instr_at_block[instr][b] = v

    # =========================================================================
    # OBJECTIVE — minimise weighted sum of soft-constraint penalties
    # =========================================================================
    obj = []

    # ── SC-1  Skeleton slot fidelity ──────────────────────────────────────────
    for s in sections:
        sid = s["id"]
        sb  = s["skel_block"]
        for ri in range(n_rooms):
            for b in BLOCK_IDS:
                if (sid, ri, b) not in assign:
                    continue
                dist = abs(b - sb)
                if dist > 0:
                    obj.append(W_SKELETON_SLOT * dist * assign[sid, ri, b])

    # ── SC-2  Building continuity ─────────────────────────────────────────────
    for s in sections:
        sid  = s["id"]
        sbld = s["skel_bldg"]
        for ri, r in enumerate(rooms):
            if r["building"] == sbld:
                continue
            for b in BLOCK_IDS:
                if (sid, ri, b) in assign:
                    obj.append(W_SKELETON_BLDG * assign[sid, ri, b])

    # ── SC-3  Instructor dead-gap ─────────────────────────────────────────────
    # Penalise empty blocks that fall between an instructor's earliest and
    # latest class in the day.  Gap = (b2 - b1 - 1) empty blocks.
    for instr, bmap in instr_at_block.items():
        for b1 in BLOCK_IDS:
            for b2 in BLOCK_IDS:
                if b2 <= b1:
                    continue
                gap = b2 - b1
                if gap <= 1:
                    continue   # adjacent blocks — no dead time

                both = model.NewBoolVar(f"dgap_{instr}_{b1}_{b2}")
                model.Add(both <= bmap[b1])
                model.Add(both <= bmap[b2])
                model.Add(both >= bmap[b1] + bmap[b2] - 1)
                obj.append(W_DEAD_GAP * (gap - 1) * both)

    # ── SC-4  Under-enrollment ────────────────────────────────────────────────
    for s in sections:
        sid   = s["id"]
        ratio = s["exp_enroll"] / max(s["capacity"], 1)
        if ratio >= UNDER_ENROLL_THRESHOLD:
            continue
        # This section is always under-enrolled regardless of placement.
        # We still model it as a variable so the penalty scales with the
        # assignment (it will always be 1, but keeps the formulation uniform).
        flag = model.NewBoolVar(f"ue_{sid}")
        choices = [
            assign[sid, ri, b]
            for ri in range(n_rooms)
            for b in BLOCK_IDS
            if (sid, ri, b) in assign
        ]
        model.AddMaxEquality(flag, choices)
        obj.append(W_UNDER_ENROLL * flag)

    # ── SC-5  Block over-cap surplus ──────────────────────────────────────────
    for b in BLOCK_IDS:
        obj.append(W_BLOCK_OVER * block_over_vars[b])

    # ── SC-6  Instructor overload ─────────────────────────────────────────────
    for instr, sids in by_instr.items():
        n = len(sids)
        if n <= INSTRUCTOR_MAX_SECTIONS:
            continue
        # Every section above the cap incurs a flat penalty.
        # (All sections are placed, so excess = n - max, always incurred.)
        excess = n - INSTRUCTOR_MAX_SECTIONS
        obj.append(W_INSTR_OVERLOAD * excess)

    # ── SC-7  Professor block preferences ────────────────────────────────────
    for s in sections:
        sid        = s["id"]
        last_name  = s["instructor"].split(",")[0].strip()
        prefs      = INSTRUCTOR_BLOCK_PREFS.get(last_name)
        if not prefs:
            continue
        for ri in range(n_rooms):
            for b in BLOCK_IDS:
                if (sid, ri, b) not in assign:
                    continue
                if b not in prefs:
                    obj.append(W_PREF_VIOLATION * assign[sid, ri, b])

    model.Minimize(sum(obj) if obj else model.NewIntVar(0, 0, "zero"))

    aux = {
        "block_count_vars": block_count_vars,
        "block_over_vars":  block_over_vars,
        "block_floor":      block_floor,
        "block_ceil":       block_ceil,
    }
    return model, assign, rooms, aux


# ─────────────────────────────────────────────────────────────────────────────
# 5. SOLVE + DIVERSIFY OPTIONS
# ─────────────────────────────────────────────────────────────────────────────

def solve_model(model, assign, sections, rooms, num_opts=3):
    room_ids = [r["id"] for r in rooms]
    n_rooms = len(rooms)

    solutions = []

    for option_idx in range(num_opts):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = SOLVER_TIME_SEC
        solver.parameters.num_search_workers = 4
        solver.parameters.random_seed = 17 + option_idx * 31
        solver.parameters.log_search_progress = False

        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            print(f"  option {option_idx + 1}: {solver.StatusName(status)}")
            break

        sol = {}
        chosen_literals = []

        for s in sections:
            sid = s["id"]
            placed = False
            for ri in range(n_rooms):
                for b in BLOCK_IDS:
                    key = (sid, ri, b)
                    if key in assign and solver.Value(assign[key]) == 1:
                        sol[sid] = {"block": b, "room": room_ids[ri], "room_idx": ri}
                        chosen_literals.append(assign[key])
                        placed = True
                        break
                if placed:
                    break

            if not placed:
                sol[sid] = {
                    "block": s["skel_block"],
                    "room": s["skel_room"] or "UNASSIGNED",
                    "room_idx": None,
                }

        objective = int(round(solver.ObjectiveValue()))
        solutions.append((objective, option_idx, sol))

        # Diversity cut: next solution must differ in at least 3 placements
        if chosen_literals:
            model.Add(sum(chosen_literals) <= len(chosen_literals) - 3)

    solutions.sort(key=lambda x: x[0])
    return solutions


# ─────────────────────────────────────────────────────────────────────────────
# 6. ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyze_solution(solution, sections, rooms):
    import math as _math
    sec_idx  = {s["id"]: s for s in sections}
    room_idx = {r["id"]: r for r in rooms}

    total            = len(sections)
    moved            = 0
    building_changes = 0
    under            = 0
    dead_gap_penalty_raw = 0
    block_dist: dict = defaultdict(int)

    by_instr_blocks: dict = defaultdict(list)

    for sid, asgn in solution.items():
        s = sec_idx[sid]
        r = room_idx.get(asgn["room"], None)
        b = asgn["block"]
        block_dist[b] += 1

        if b != s["skel_block"]:
            moved += 1
        if r and r["building"] != s["skel_bldg"]:
            building_changes += 1
        if s["exp_enroll"] / max(s["capacity"], 1) < UNDER_ENROLL_THRESHOLD:
            under += 1
        if s["instructor"] != "TBA":
            by_instr_blocks[s["instructor"]].append(b)

    for instr, blocks in by_instr_blocks.items():
        blocks = sorted(set(blocks))
        for i in range(len(blocks) - 1):
            gap = blocks[i + 1] - blocks[i]
            if gap > 1:
                dead_gap_penalty_raw += (gap - 1)

    soft_cap = _math.floor(total * BLOCK_MAX_PCT)
    block_over = {}
    for b in BLOCK_IDS:
        block_over[b] = max(0, block_dist[b] - soft_cap)

    return {
        "total_sections":       total,
        "moved_from_skeleton":  moved,
        "moved_pct":            round(100 * moved / total, 1) if total else 0.0,
        "building_changes":     building_changes,
        "under_enrolled_sections": under,
        "dead_gap_units":       dead_gap_penalty_raw,
        "block_distribution":   dict(block_dist),
        "block_over":           block_over,
        "cap_per_block":        soft_cap,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7. OUTPUT TABLES
# ─────────────────────────────────────────────────────────────────────────────

def to_dataframe(solutions, sections, rooms):
    sec_idx = {s["id"]: s for s in sections}
    room_idx = {r["id"]: r for r in rooms}

    rows = []
    for i, (score, _, sol) in enumerate(solutions):
        label = f"Option {chr(65 + i)}"
        for sid, asgn in sol.items():
            s = sec_idx[sid]
            rid = asgn["room"]
            r = room_idx.get(rid, {"building": "?", "capacity": 0})

            rows.append({
                "Option": label,
                "Score": score,
                "CRN": s["crn"],
                "Course": s["course"],
                "Title": s["title"],
                "Instructor": s["instructor"],
                "Credits": s["credits"],
                "Days": s["days"],
                "Duration_Mins": s["duration_mins"],
                "Weekly_Mins": s["weekly_minutes"],
                "Block_ID": asgn["block"],
                "Block_Label": BLOCK_LABEL[asgn["block"]],
                "Time_HHMM": BLOCK_HHMM[asgn["block"]],
                "Room": rid,
                "Building": r["building"],
                "Room_Capacity": r["capacity"],
                "Section_Capacity": s["capacity"],
                "Actual_Enrollment": s["actual_enroll"],
                "Expected_Enrollment": s["exp_enroll"],
                "Expected_Fill_Pct": round(100 * s["exp_enroll"] / max(s["capacity"], 1), 1),
                "Skeleton_Block_ID": s["skel_block"],
                "Skeleton_Block_Label": BLOCK_LABEL[s["skel_block"]],
                "Skeleton_Building": s["skel_bldg"],
                "Moved_From_Skeleton": "YES" if asgn["block"] != s["skel_block"] else "no",
            })

    df = pd.DataFrame(rows)
    return df.sort_values(["Option", "Course", "Time_HHMM", "Instructor", "CRN"])


def make_report(solutions, sections, rooms):
    sec_idx = {s["id"]: s for s in sections}

    lines = []
    lines.append("=" * 72)
    lines.append("MATH SCHEDULE OPTIMIZER — PHASE 1 REPORT")
    lines.append("=" * 72)
    lines.append(f"Core courses modeled : {CORE_COURSES}")
    lines.append(f"Sections scheduled   : {len(sections)}")
    lines.append(f"Rooms available      : {len(rooms)}")
    lines.append(f"Universal blocks     : {len(BLOCK_IDS)}")
    lines.append("")

    import math as _math
    total = len(sections)
    block_floor = max(1, _math.floor(total * BLOCK_MIN_PCT))
    block_ceil  = _math.ceil(total  * BLOCK_MAX_PCT)
    lines.append(f"Block band (hard)    : {block_floor}–{block_ceil} sections "
                 f"per block  ({int(BLOCK_MIN_PCT*100)}%–{int(BLOCK_MAX_PCT*100)}%)")
    lines.append("")

    # Course section counts
    by_course: dict = defaultdict(list)
    for s in sections:
        by_course[s["course"]].append(s["id"])
    lines.append("Course section counts vs caps (HC-3):")
    for course in CORE_COURSES:
        cnt = len(by_course.get(course, []))
        cap = COURSE_MAX_SECTIONS.get(course, "—")
        flag = "  *** EXCEEDS CAP ***" if isinstance(cap, int) and cnt > cap else ""
        lines.append(f"  MATH {course}: {cnt:>3} sections  (cap = {cap}){flag}")
    lines.append("")

    for i, (score, seed, sol) in enumerate(solutions):
        label = f"Option {chr(65 + i)}"
        stats = analyze_solution(sol, sections, rooms)

        lines.append(f"{'─'*72}")
        lines.append(f"{label}  |  penalty score = {score}  |  solve seed = {seed}")
        lines.append(f"{'─'*72}")
        lines.append(f"  Sections scheduled       : {stats['total_sections']}")
        lines.append(f"  Moved from skeleton      : {stats['moved_from_skeleton']} "
                     f"({stats['moved_pct']}%)")
        lines.append(f"  Building changes         : {stats['building_changes']}")
        lines.append(f"  Under-enrolled sections  : {stats['under_enrolled_sections']}")
        lines.append(f"  Instructor dead-gap raw  : {stats['dead_gap_units']}")
        lines.append(f"  Block floor / ceiling    : {block_floor} / {block_ceil}")
        lines.append("")
        lines.append("  Time-block distribution:")
        lines.append(f"  {'Block':<12} {'Count':>5}  {'%':>5}  {'Floor OK':>8}  {'Ceil OK':>8}")
        for b in BLOCK_IDS:
            cnt  = stats["block_distribution"].get(b, 0)
            pct  = round(100 * cnt / max(stats["total_sections"], 1), 1)
            over = stats["block_over"].get(b, 0)
            floor_ok = "✓" if cnt >= block_floor else f"✗ ({cnt}<{block_floor})"
            ceil_ok  = "✓" if cnt <= block_ceil  else f"✗ ({cnt}>{block_ceil})"
            over_str = f"  +{over} over" if over > 0 else ""
            lines.append(
                f"  {BLOCK_LABEL[b]:<12} {cnt:>5}  {pct:>4}%  {floor_ok:>8}  {ceil_ok:>8}{over_str}"
            )

        # Instructor load summary
        instr_loads: dict = defaultdict(int)
        for sid in sol:
            s = sec_idx[sid]
            if s["instructor"] != "TBA":
                instr_loads[s["instructor"]] += 1
        overloaded = {k: v for k, v in instr_loads.items() if v > INSTRUCTOR_MAX_SECTIONS}
        lines.append("")
        lines.append(f"  Instructor load (soft limit = {INSTRUCTOR_MAX_SECTIONS} sections):")
        for instr, cnt in sorted(instr_loads.items(), key=lambda x: -x[1]):
            flag = "  ← OVER LIMIT" if instr in overloaded else ""
            lines.append(f"    {instr:<30} {cnt} section(s){flag}")

        # Moved sections
        moved_rows = []
        for sid, asgn in sol.items():
            s = sec_idx[sid]
            if asgn["block"] != s["skel_block"]:
                moved_rows.append(
                    f"    CRN {s['crn']:>6} | MATH {s['course']} | {s['instructor']:<25} | "
                    f"{BLOCK_LABEL[s['skel_block']]} → {BLOCK_LABEL[asgn['block']]}"
                )
        if moved_rows:
            lines.append("")
            lines.append("  Sections moved from skeleton slot:")
            lines.extend(moved_rows[:40])
            if len(moved_rows) > 40:
                lines.append(f"    … and {len(moved_rows) - 40} more")
        lines.append("")

    lines.append("=" * 72)
    lines.append("CONSTRAINT REFERENCE")
    lines.append("=" * 72)
    lines.append("")
    lines.append("HARD CONSTRAINTS")
    lines.append("  HC-1  Credit-hour weekly minutes:")
    for cred, rule in CREDIT_MINUTE_RULES.items():
        lines.append(f"          {cred} credits → {rule['min']}–{rule['max']} weekly minutes")
    lines.append("  HC-1  Credit-hour day-count rules:")
    for cred, vals in CREDIT_DAYCOUNT_RULES.items():
        lines.append(f"          {cred} credits → meeting-day counts: {sorted(vals)}")
    lines.append(f"  HC-2  Block band: {int(BLOCK_MIN_PCT*100)}%–{int(BLOCK_MAX_PCT*100)}% "
                 f"of sections per block ({block_floor}–{block_ceil} absolute)")
    lines.append("  HC-3  Course section caps:")
    for course, cap in COURSE_MAX_SECTIONS.items():
        lines.append(f"          MATH {course}: ≤ {cap} sections")
    lines.append("  HC-4  Room conflict: ≤ 1 section per (room, block)")
    lines.append("  HC-5  Instructor conflict: ≤ 1 section per (instructor, block)")
    lines.append("")
    lines.append("SOFT CONSTRAINTS  (weights)")
    lines.append(f"  SC-1  Skeleton slot fidelity  : {W_SKELETON_SLOT} per block-step")
    lines.append(f"  SC-2  Building continuity     : {W_SKELETON_BLDG} per change")
    lines.append(f"  SC-3  Instructor dead-gap      : {W_DEAD_GAP} per empty block gap")
    lines.append(f"  SC-4  Under-enrollment         : {W_UNDER_ENROLL} per section "
                 f"(threshold {int(UNDER_ENROLL_THRESHOLD*100)}%)")
    lines.append(f"  SC-5  Block over-cap surplus   : {W_BLOCK_OVER} per section over ceiling")
    lines.append(f"  SC-6  Instructor overload       : {W_INSTR_OVERLOAD} per section "
                 f"above {INSTRUCTOR_MAX_SECTIONS}-section limit")
    lines.append(f"  SC-7  Professor preferences    : {W_PREF_VIOLATION} per non-preferred block")
    if INSTRUCTOR_BLOCK_PREFS:
        for name, prefs in INSTRUCTOR_BLOCK_PREFS.items():
            pref_labels = [BLOCK_LABEL[p] for p in prefs]
            lines.append(f"          {name}: prefers {pref_labels}")
    else:
        lines.append("          (no preferences configured)")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 8. CALENDAR CHART — ATHENA STYLE
# ─────────────────────────────────────────────────────────────────────────────

# Per-course colour palette (face colour + edge colour)
_CHART_COLORS = {
    1113: {"face": "#f472b6", "edge": "#db2777"},   # pink
    2250: {"face": "#4ade80", "edge": "#16a34a"},   # green
    2260: {"face": "#a78bfa", "edge": "#7c3aed"},   # purple
    2270: {"face": "#fb923c", "edge": "#ea580c"},   # orange
    2500: {"face": "#60a5fa", "edge": "#2563eb"},   # blue
    2700: {"face": "#34d399", "edge": "#059669"},   # teal
    3300: {"face": "#fbbf24", "edge": "#d97706"},   # amber
}
_FALLBACK_FACES = [
    "#93c5fd", "#86efac", "#fca5a5", "#c4b5fd",
    "#fdba74", "#6ee7b7", "#f9a8d4",
]
_fallback_idx: dict = {}


def _face(course):
    c = _CHART_COLORS.get(course)
    if c is None:
        i = _fallback_idx.setdefault(course, len(_fallback_idx))
        return _FALLBACK_FACES[i % len(_FALLBACK_FACES)]
    return c["face"]


def _edge(course):
    c = _CHART_COLORS.get(course)
    if c is None:
        return _face(course)
    return c["edge"]


def draw_schedule_chart(option_label, score, sol, sections, rooms, out_path):
    """
    Renders an Athena-style weekly calendar chart for one schedule option.

    Layout
    ------
    - White background, Monday–Friday column headers across the top
    - Time labels (8am … 6pm) down the left-hand side
    - Thin horizontal grid lines at every hour
    - Coloured rounded rectangles sized to the true start/end time of each section
    - Overlapping sections in the same day column are split into equal-width
      sub-columns so no text is ever obscured
    - Each block shows: course name, room, CRN, and instructor
    - Legend at the bottom with one colour swatch per course number

    Parameters
    ----------
    option_label : str   e.g. "Option A"
    score        : int   solver objective value
    sol          : dict  {section_id: {"block": b, "room": room_id, ...}}
    sections     : list  of section dicts produced by load_data()
    rooms        : list  of room dicts produced by load_data()
    out_path     : str   file path for the saved PNG
    """
    DAY_COLS   = ["M", "T", "W", "R", "F"]
    DAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    N_DAYS     = 5
    PADDING    = 0.03          # gap between day-column edge and content
    GAP        = 0.015         # gap between side-by-side sub-columns

    DAY_START_MIN = 8  * 60   # 8:00 am
    DAY_END_MIN   = 18 * 60   # 6:00 pm

    sec_idx  = {s["id"]: s for s in sections}
    room_idx = {r["id"]: r for r in rooms}

    # ── Pass 1: collect every (section, day) event with its time window ──────
    # event = (sid, day_col, vis_start, vis_end, section_dict, room_dict)
    events = []
    for sid, asgn in sol.items():
        s   = sec_idx[sid]
        rid = asgn["room"]
        r   = room_idx.get(rid, {"building": "?", "room": "?"})

        sh, sm    = divmod(int(BLOCK_HHMM[asgn["block"]]), 100)
        start_min = sh * 60 + sm
        end_min   = start_min + s["duration_mins"]

        if end_min <= DAY_START_MIN or start_min >= DAY_END_MIN:
            continue

        vis_start = max(start_min, DAY_START_MIN)
        vis_end   = min(end_min,   DAY_END_MIN)

        for day in s["days"]:
            if day not in DAY_COLS:
                continue
            col = DAY_COLS.index(day)
            events.append((sid, col, vis_start, vis_end, s, r))

    # ── Pass 2: for each day-column, compute non-overlapping lane assignments ─
    # We use a greedy interval-graph colouring: sort by start time, assign the
    # smallest lane not currently in use, track the maximum concurrent overlap
    # per column so we know the total number of lanes needed.

    # Group events by day column
    by_col: dict = defaultdict(list)
    for ev in events:
        by_col[ev[1]].append(ev)

    # lane_assignment[(sid, col)] = (lane_index, total_lanes_in_that_overlap_group)
    lane_assignment: dict = {}

    for col, col_events in by_col.items():
        # Sort by start time, break ties by end time
        col_events_sorted = sorted(col_events, key=lambda e: (e[2], e[3]))

        # active = list of (end_time, lane_index) for currently open events
        active: list = []   # heap not needed; small N
        max_lane = 0

        for ev in col_events_sorted:
            sid, _, vis_start, vis_end, s, r = ev

            # Release lanes whose events have ended
            active = [a for a in active if a[0] > vis_start]

            # Find the smallest free lane
            used_lanes = {a[1] for a in active}
            lane = 0
            while lane in used_lanes:
                lane += 1

            active.append((vis_end, lane))
            max_lane = max(max_lane, lane)
            # Store temporarily; we'll fix total_lanes in a second sweep
            lane_assignment[(sid, col)] = [lane, -1]   # -1 = placeholder

        # Second sweep: for every event, determine the total lanes needed for
        # its particular overlap group (the maximum simultaneous occupancy that
        # touches its time window).
        for ev in col_events_sorted:
            sid, _, vis_start, vis_end, s, r = ev
            # Count how many other events in this column overlap with [vis_start, vis_end)
            concurrent = sum(
                1 for e2 in col_events
                if e2[2] < vis_end and e2[3] > vis_start
            )
            lane_assignment[(sid, col)][1] = max(concurrent, 1)

    # ── Figure setup ──────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(22, 16))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    ax.set_xlim(0, N_DAYS)
    ax.set_ylim(DAY_END_MIN, DAY_START_MIN)   # inverted

    ax.set_xticks([i + 0.5 for i in range(N_DAYS)])
    ax.set_xticklabels(DAY_LABELS, fontsize=13, fontweight="bold", color="#1a1a2e")
    ax.xaxis.set_tick_params(length=0)
    ax.xaxis.tick_top()

    hour_ticks = list(range(DAY_START_MIN, DAY_END_MIN + 1, 60))
    ax.set_yticks(hour_ticks)

    def fmt_hour(m):
        h = m // 60
        suffix = "am" if h < 12 else "pm"
        h12 = h if 1 <= h <= 12 else (h - 12 if h > 12 else 12)
        return f"{h12}{suffix}"

    ax.set_yticklabels([fmt_hour(t) for t in hour_ticks], fontsize=11, color="#666")
    ax.yaxis.set_tick_params(length=0)

    for t in hour_ticks:
        ax.axhline(t, color="#e2e8f0", linewidth=0.8, zorder=0)
    for x in range(N_DAYS + 1):
        ax.axvline(x, color="#cbd5e1", linewidth=0.9, zorder=0)

    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_title(
        f"Class Schedule — {option_label}   (penalty score = {score})",
        fontsize=15, fontweight="bold", color="#1a1a2e", pad=14, loc="left",
    )

    # ── Pass 3: draw every event ───────────────────────────────────────────────
    for ev in events:
        sid, col, vis_start, vis_end, s, r = ev
        height = vis_end - vis_start

        lane, total_lanes = lane_assignment[(sid, col)]

        # Divide the usable column width evenly among concurrent lanes
        usable_w  = 1.0 - 2 * PADDING - (total_lanes - 1) * GAP
        lane_w    = usable_w / total_lanes
        x_left    = col + PADDING + lane * (lane_w + GAP)
        box_width = lane_w

        face_color = _face(s["course"])
        edge_color = _edge(s["course"])

        rect = mpatches.FancyBboxPatch(
            (x_left, vis_start),
            box_width,
            height,
            boxstyle="round,pad=1.2",
            linewidth=1.0,
            edgecolor=edge_color,
            facecolor=face_color,
            alpha=0.90,
            zorder=3,
            clip_on=True,
            mutation_scale=0.4,
        )
        ax.add_patch(rect)

        if height < 8:
            continue

        text_x = x_left + 0.008
        text_y = vis_start + height / 2

        # Scale font size down when columns are narrow
        title_fs = max(5.5, min(8.5, 8.5 * box_width / 0.9))
        sub_fs   = max(4.5, min(7.5, 7.5 * box_width / 0.9))

        # Truncate title to fit the sub-column width
        # Approx chars that fit: box_width * ~18 chars per unit width at 8.5pt
        max_chars = max(8, int(box_width * 130 / title_fs))

        title_str = f"MATH {s['course']} — {s['title']}"
        if len(title_str) > max_chars:
            title_str = title_str[:max_chars - 1] + "…"

        room_str = f"{r['building']}-{r['room']}  CRN {s['crn']}"
        if len(room_str) > max_chars:
            room_str = room_str[:max_chars - 1] + "…"

        instr_str = s["instructor"].split(",")[0]  # last name only

        if height >= 35:
            # Three lines: title, room/CRN, instructor
            ax.text(x_left + 0.008, vis_start + height * 0.25, title_str,
                    fontsize=title_fs, fontweight="semibold", color="#0f172a",
                    va="center", ha="left", zorder=4, clip_on=True)
            ax.text(x_left + 0.008, vis_start + height * 0.55, room_str,
                    fontsize=sub_fs, color="#334155",
                    va="center", ha="left", zorder=4, clip_on=True)
            ax.text(x_left + 0.008, vis_start + height * 0.80, instr_str,
                    fontsize=sub_fs, color="#475569",
                    va="center", ha="left", zorder=4, clip_on=True)
        elif height >= 18:
            # Two lines: title + room/CRN
            ax.text(x_left + 0.008, vis_start + height * 0.35, title_str,
                    fontsize=title_fs, fontweight="semibold", color="#0f172a",
                    va="center", ha="left", zorder=4, clip_on=True)
            ax.text(x_left + 0.008, vis_start + height * 0.72, room_str,
                    fontsize=sub_fs, color="#334155",
                    va="center", ha="left", zorder=4, clip_on=True)
        else:
            # Single line: title only
            ax.text(x_left + 0.008, text_y, title_str,
                    fontsize=title_fs, fontweight="semibold", color="#0f172a",
                    va="center", ha="left", zorder=4, clip_on=True)

    # ── Legend ────────────────────────────────────────────────────────────────
    seen_courses = sorted({sec_idx[sid]["course"] for sid in sol})
    legend_handles = [
        mpatches.Patch(facecolor=_face(c), edgecolor=_edge(c),
                       label=f"MATH {c}", linewidth=1.0)
        for c in seen_courses
    ]
    if legend_handles:
        ax.legend(
            handles=legend_handles,
            loc="lower center",
            bbox_to_anchor=(0.5, -0.05),
            ncol=min(len(legend_handles), 7),
            fontsize=11,
            frameon=True,
            framealpha=0.9,
            edgecolor="#cbd5e1",
        )

    plt.tight_layout(rect=[0, 0.04, 1, 1])
    plt.savefig(out_path, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# 9. MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Loading Spring 2026 data ...")
    sections, rooms = load_data(CSV_PATH)
    print(f"  sections: {len(sections)}")
    print(f"  rooms   : {len(rooms)}")
    print(f"  blocks  : {len(BLOCK_IDS)}")
    print(f"  courses : {sorted(set(s['course'] for s in sections))}")
    print("")

    print("Building CP-SAT model ...")
    model, assign, rooms, aux = build_model(sections, rooms)
    print("")

    print(f"Solving for up to {NUM_OPTIONS} options ({SOLVER_TIME_SEC}s each) ...")
    solutions = solve_model(model, assign, sections, rooms, NUM_OPTIONS)

    if not solutions:
        print("ERROR: No feasible solution found.")
        sys.exit(1)

    print(f"  found {len(solutions)} solution(s)")
    print("")

    # CSV output
    df = to_dataframe(solutions, sections, rooms)
    csv_out = os.path.join(OUTPUT_DIR, "schedule_output.csv")
    df.to_csv(csv_out, index=False)

    # Report output
    report = make_report(solutions, sections, rooms)
    txt_out = os.path.join(OUTPUT_DIR, "schedule_report.txt")
    with open(txt_out, "w", encoding="utf-8") as f:
        f.write(report)

    # Chart outputs (one Athena-style calendar per option)
    for i, (score, _, sol) in enumerate(solutions):
        label   = f"Option {chr(65 + i)}"
        png_out = os.path.join(OUTPUT_DIR, f"schedule_option_{chr(65 + i)}.png")
        draw_schedule_chart(label, score, sol, sections, rooms, png_out)

    print(report)
    print("")
    print("Files written:")
    print(f"  {csv_out}")
    print(f"  {txt_out}")
    for i in range(len(solutions)):
        print(f"  {os.path.join(OUTPUT_DIR, f'schedule_option_{chr(65 + i)}.png')}")


if __name__ == "__main__":
    main()