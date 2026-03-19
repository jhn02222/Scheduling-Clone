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
import matplotlib.patches as patches
import pandas as pd
from ortools.sat.python import cp_model


# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

CSV_PATH = "Course Schedule of Classes Proof ALL ++_20260116_124500.csv"
OUTPUT_DIR = "data"

# Universal department blocks
TIME_BLOCKS = [
    (0, 815,  "8:15 AM"),
    (1, 955,  "9:55 AM"),
    (2, 1135, "11:35 AM"),
    (3, 1315, "1:15 PM"),
    (4, 1455, "2:55 PM"),
    (5, 1635, "4:35 PM"),
]
BLOCK_IDS = [b[0] for b in TIME_BLOCKS]
BLOCK_HHMM = {b[0]: b[1] for b in TIME_BLOCKS}
BLOCK_LABEL = {b[0]: b[2] for b in TIME_BLOCKS}

# Max fraction of all sections allowed in any one block (soft)
BLOCK_CAP_PCT = 0.30

# Penalty weights
W_SKELETON_SLOT = 8      # per block-step away from Spring 2026 slot
W_SKELETON_BLDG = 2      # per section moved to different building
W_DEAD_GAP = 10          # per empty block between instructor classes
W_UNDER_ENROLL = 12      # per section under threshold
W_BLOCK_OVER = 18        # per section over block cap

# Phase 1 course list
CORE_COURSES = [1113, 2250, 2260, 2270, 2500, 2700, 3300]

# Historical average fill rates by course
HIST_FILL = {
    1113: 0.932,
    2250: 0.934,
    2260: 0.938,
    2270: 0.931,
    2500: 0.949,
    2700: 0.978,
    3300: 0.931,
}

UNDER_ENROLL_THRESHOLD = 0.60
SOLVER_TIME_SEC = 90
NUM_OPTIONS = 3

# Credit-hour rules
# Adjust these to match department policy exactly if needed.
CREDIT_MINUTE_RULES = {
    3: {"min": 140, "max": 170},
    4: {"min": 190, "max": 230},
}
CREDIT_DAYCOUNT_RULES = {
    3: {2, 3},
    4: {3, 4},
}

DAY_ORDER = ["M", "T", "W", "R", "F"]


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
    model = cp_model.CpModel()

    # Explicit validation guard
    for s in sections:
        credits = s["credits"]
        if credits in CREDIT_MINUTE_RULES:
            mins_rule = CREDIT_MINUTE_RULES[credits]
            if not (mins_rule["min"] <= s["weekly_minutes"] <= mins_rule["max"]):
                raise ValueError(
                    f"Section CRN {s['crn']} violates minutes rule for {credits} credits."
                )
        if credits in CREDIT_DAYCOUNT_RULES:
            if s["day_count"] not in CREDIT_DAYCOUNT_RULES[credits]:
                raise ValueError(
                    f"Section CRN {s['crn']} violates day-count rule for {credits} credits."
                )

    n_rooms = len(rooms)
    room_ids = [r["id"] for r in rooms]

    # assign[sid, room_index, block] = 1 iff section placed there
    assign = {}
    for s in sections:
        sid = s["id"]
        for ri, r in enumerate(rooms):
            if r["capacity"] < s["exp_enroll"]:
                continue
            for b in BLOCK_IDS:
                assign[sid, ri, b] = model.NewBoolVar(f"x_{sid}_{ri}_{b}")

    print(f"  assign variables created: {len(assign):,}")

    # Hard 1: each section assigned exactly once
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
                f"No feasible room/block assignments for CRN {s['crn']} "
                f"(expected enrollment = {s['exp_enroll']})."
            )
        model.AddExactlyOne(choices)

    # Hard 2: at most one section per room per block
    for ri in range(n_rooms):
        for b in BLOCK_IDS:
            occupants = [
                assign[s["id"], ri, b]
                for s in sections
                if (s["id"], ri, b) in assign
            ]
            if len(occupants) > 1:
                model.AddAtMostOne(occupants)

    # Hard 3: instructor conflict by block
    by_instr = defaultdict(list)
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

    # Derived: instructor has at least one class in block b
    instr_at_block = {}
    for instr, sids in by_instr.items():
        instr_at_block[instr] = {}
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"instr_{instr}_b{b}")
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

    obj = []

    # A. Skeleton slot deviation
    for s in sections:
        sid = s["id"]
        sb = s["skel_block"]
        for ri in range(n_rooms):
            for b in BLOCK_IDS:
                if (sid, ri, b) not in assign:
                    continue
                dist = abs(b - sb)
                if dist > 0:
                    obj.append(W_SKELETON_SLOT * dist * assign[sid, ri, b])

    # B. Building change penalty
    for s in sections:
        sid = s["id"]
        sbld = s["skel_bldg"]
        for ri, r in enumerate(rooms):
            if r["building"] == sbld:
                continue
            for b in BLOCK_IDS:
                if (sid, ri, b) in assign:
                    obj.append(W_SKELETON_BLDG * assign[sid, ri, b])

    # C. Instructor dead-gap penalty
    for instr, bmap in instr_at_block.items():
        for b1 in BLOCK_IDS:
            for b2 in BLOCK_IDS:
                if b2 <= b1:
                    continue
                gap = b2 - b1
                if gap <= 1:
                    continue

                both = model.NewBoolVar(f"both_{instr}_{b1}_{b2}")
                model.Add(both <= bmap[b1])
                model.Add(both <= bmap[b2])
                model.Add(both >= bmap[b1] + bmap[b2] - 1)

                obj.append(W_DEAD_GAP * (gap - 1) * both)

    # D. Under-enrollment penalty
    # In Phase 1, this is section-level, based on estimated fill vs room cap.
    for s in sections:
        sid = s["id"]
        # This uses the section's baseline capacity expectation threshold.
        ratio = s["exp_enroll"] / max(s["capacity"], 1)
        if ratio >= UNDER_ENROLL_THRESHOLD:
            continue

        flag = model.NewBoolVar(f"under_{sid}")
        choices = [
            assign[sid, ri, b]
            for ri in range(n_rooms)
            for b in BLOCK_IDS
            if (sid, ri, b) in assign
        ]
        model.AddMaxEquality(flag, choices)
        obj.append(W_UNDER_ENROLL * flag)

    # E. Time-block cap
    total_sections = len(sections)
    cap = int(total_sections * BLOCK_CAP_PCT)

    block_count_vars = {}
    block_over_vars = {}

    for b in BLOCK_IDS:
        in_block = []
        for s in sections:
            sid = s["id"]
            v = model.NewBoolVar(f"s_{sid}_in_block_{b}")
            choices = [
                assign[sid, ri, b]
                for ri in range(n_rooms)
                if (sid, ri, b) in assign
            ]
            if choices:
                model.AddMaxEquality(v, choices)
            else:
                model.Add(v == 0)
            in_block.append(v)

        sum_b = model.NewIntVar(0, total_sections, f"sum_block_{b}")
        over_b = model.NewIntVar(0, total_sections, f"over_block_{b}")
        model.Add(sum_b == sum(in_block))
        model.Add(over_b >= sum_b - cap)
        model.Add(over_b >= 0)

        block_count_vars[b] = sum_b
        block_over_vars[b] = over_b
        obj.append(W_BLOCK_OVER * over_b)

    model.Minimize(sum(obj))

    aux = {
        "block_count_vars": block_count_vars,
        "block_over_vars": block_over_vars,
    }
    return model, assign, rooms, aux


# ─────────────────────────────────────────────────────────────────────────────
# 5. SOLVE + DIVERSIFY OPTIONS
# ─────────────────────────────────────────────────────────────────────────────

def solve_model(model, assign, sections, rooms, num_opts=3):
    room_ids = [r["id"] for r in rooms]
    n_rooms = len(rooms)

    solutions = []
    no_good_cuts = []

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
            no_good_cuts.append(True)

    solutions.sort(key=lambda x: x[0])
    return solutions


# ─────────────────────────────────────────────────────────────────────────────
# 6. ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyze_solution(solution, sections, rooms):
    sec_idx = {s["id"]: s for s in sections}
    room_idx = {r["id"]: r for r in rooms}

    total = len(sections)
    moved = 0
    building_changes = 0
    under = 0
    dead_gap_penalty_raw = 0
    block_dist = defaultdict(int)

    by_instr_blocks = defaultdict(list)

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

    cap = int(total * BLOCK_CAP_PCT)
    block_over = {}
    for b in BLOCK_IDS:
        block_over[b] = max(0, block_dist[b] - cap)

    return {
        "total_sections": total,
        "moved_from_skeleton": moved,
        "moved_pct": round(100 * moved / total, 1) if total else 0.0,
        "building_changes": building_changes,
        "under_enrolled_sections": under,
        "dead_gap_units": dead_gap_penalty_raw,
        "block_distribution": dict(block_dist),
        "block_over": block_over,
        "cap_per_block": cap,
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
    lines.append(f"Core courses modeled: {CORE_COURSES}")
    lines.append(f"Sections scheduled  : {len(sections)}")
    lines.append(f"Rooms available     : {len(rooms)}")
    lines.append(f"Universal blocks    : {len(BLOCK_IDS)}")
    lines.append("")

    for i, (score, seed, sol) in enumerate(solutions):
        label = f"Option {chr(65 + i)}"
        stats = analyze_solution(sol, sections, rooms)

        lines.append(f"{label} | penalty score = {score} | solve index = {seed}")
        lines.append("-" * 56)
        lines.append(f"  Sections scheduled      : {stats['total_sections']}")
        lines.append(f"  Moved from skeleton     : {stats['moved_from_skeleton']} "
                     f"({stats['moved_pct']}%)")
        lines.append(f"  Building changes        : {stats['building_changes']}")
        lines.append(f"  Under-enrolled sections : {stats['under_enrolled_sections']}")
        lines.append(f"  Instructor dead-gap raw : {stats['dead_gap_units']}")
        lines.append(f"  Block cap               : {stats['cap_per_block']} sections")
        lines.append("")
        lines.append("  Time-block distribution:")

        for b in BLOCK_IDS:
            cnt = stats["block_distribution"].get(b, 0)
            pct = round(100 * cnt / max(stats["total_sections"], 1), 1)
            over = stats["block_over"].get(b, 0)
            lines.append(
                f"    {BLOCK_LABEL[b]:<10}  {cnt:>3}  {pct:>5}%"
                + (f"  | over by {over}" if over > 0 else "")
            )

        moved_rows = []
        for sid, asgn in sol.items():
            s = sec_idx[sid]
            if asgn["block"] != s["skel_block"]:
                moved_rows.append(
                    f"    CRN {s['crn']:>6} | {s['course']} | {s['instructor']:<25} | "
                    f"{BLOCK_LABEL[s['skel_block']]} -> {BLOCK_LABEL[asgn['block']]}"
                )

        if moved_rows:
            lines.append("")
            lines.append("  Sections moved from skeleton:")
            lines.extend(moved_rows[:40])
            if len(moved_rows) > 40:
                lines.append(f"    ... and {len(moved_rows) - 40} more")

        lines.append("")
        lines.append("")

    lines.append("=" * 72)
    lines.append("WEIGHTS")
    lines.append("=" * 72)
    lines.append(f"Skeleton slot deviation : {W_SKELETON_SLOT}")
    lines.append(f"Building change         : {W_SKELETON_BLDG}")
    lines.append(f"Instructor dead gap     : {W_DEAD_GAP}")
    lines.append(f"Under-enrollment        : {W_UNDER_ENROLL}")
    lines.append(f"Block cap overage       : {W_BLOCK_OVER}")
    lines.append("")
    lines.append("Credit-minute rules:")
    for cred, rule in CREDIT_MINUTE_RULES.items():
        lines.append(f"  {cred} credits -> {rule['min']} to {rule['max']} weekly minutes")
    lines.append("")
    lines.append("Credit day-count rules:")
    for cred, vals in CREDIT_DAYCOUNT_RULES.items():
        lines.append(f"  {cred} credits -> allowed meeting-day counts: {sorted(vals)}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 8. CALENDAR CHART OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def draw_schedule_chart(option_label, score, sol, sections, rooms, out_path):
    sec_idx = {s["id"]: s for s in sections}
    room_idx = {r["id"]: r for r in rooms}

    # Row ordering by room, then block
    room_order = sorted(rooms, key=lambda r: (r["building"], r["room"]))
    room_to_row = {r["id"]: i for i, r in enumerate(room_order)}

    num_rooms = len(room_order)
    fig_h = max(14, 0.32 * num_rooms + 4)
    fig, ax = plt.subplots(figsize=(28, fig_h))

    ax.set_xlim(0, 5)
    ax.set_ylim(0, num_rooms)
    ax.invert_yaxis()

    ax.set_xticks(range(5))
    ax.set_xticklabels(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"], fontsize=12)

    ytick_positions = [i + 0.5 for i in range(num_rooms)]
    ytick_labels = [f"{r['building']}-{r['room']} (cap {r['capacity']})" for r in room_order]
    ax.set_yticks(ytick_positions)
    ax.set_yticklabels(ytick_labels, fontsize=8)

    ax.set_title(f"{option_label} — Big Schedule Chart (Score = {score})", fontsize=18, pad=18)

    # Light grid
    for x in range(6):
        ax.axvline(x, color="#d1d5db", linewidth=0.8)
    for y in range(num_rooms + 1):
        ax.axhline(y, color="#e5e7eb", linewidth=0.4)

    # Place one rectangle per scheduled day for each section
    # Within each room row, stack by block using vertical offsets.
    room_day_block_counts = defaultdict(int)

    for sid, asgn in sol.items():
        s = sec_idx[sid]
        rid = asgn["room"]
        if rid not in room_to_row:
            continue

        base_row = room_to_row[rid]
        block = asgn["block"]
        days = s["days"]

        # Spread multiple sections in same room+day by tiny offset
        for day in days:
            if day not in DAY_ORDER:
                continue
            day_col = DAY_ORDER.index(day)

            stack_idx = room_day_block_counts[(rid, day_col, block)]
            room_day_block_counts[(rid, day_col, block)] += 1

            # Use block to subdivide each room row into 6 mini-slots
            mini_h = 1.0 / 6.0
            y = base_row + block * mini_h + 0.01 + stack_idx * 0.012
            h = mini_h - 0.02

            rect = patches.Rectangle(
                (day_col + 0.03, y),
                0.94,
                h,
                linewidth=0.8,
                edgecolor="#374151",
                facecolor=course_color(s["course"]),
                alpha=0.95,
            )
            ax.add_patch(rect)

            label = (
                f"{s['course']} | CRN {s['crn']}\n"
                f"{BLOCK_LABEL[block]} | {s['instructor'].split(',')[0]}\n"
                f"Exp {s['exp_enroll']}/{s['capacity']}"
            )
            ax.text(
                day_col + 0.05,
                y + h / 2,
                label,
                fontsize=6.5,
                va="center",
                ha="left",
                color="#111827",
                clip_on=True,
            )

    # Add a small legend for block meaning
    legend_text = "Blocks: " + " | ".join([f"{b}: {BLOCK_LABEL[b]}" for b in BLOCK_IDS])
    fig.text(0.5, 0.01, legend_text, ha="center", fontsize=11)

    plt.tight_layout(rect=[0, 0.02, 1, 0.98])
    plt.savefig(out_path, dpi=220, bbox_inches="tight")
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

    # Chart outputs
    for i, (score, _, sol) in enumerate(solutions):
        label = f"Option {chr(65 + i)}"
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