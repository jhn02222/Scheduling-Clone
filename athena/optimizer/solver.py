"""
Core CP-SAT solver logic — decoupled from Django views.
All constraint weights are passed in at runtime via the `weights` dict.
"""

import math
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

CORE_COURSES = [1113, 2250, 2260, 2270, 2500, 2700, 3300]
HIST_FILL    = {1113:0.932, 2250:0.934, 2260:0.938, 2270:0.931,
                2500:0.949, 2700:0.978, 3300:0.931}

CREDIT_MINUTE_RULES   = {3:{"min":140,"max":170}, 4:{"min":190,"max":230}}
CREDIT_DAYCOUNT_RULES = {3:{2,3}, 4:{3,4}}

COURSE_COLORS = {
    1113:{"face":"#f472b6","edge":"#db2777"},
    2250:{"face":"#4ade80","edge":"#16a34a"},
    2260:{"face":"#a78bfa","edge":"#7c3aed"},
    2270:{"face":"#fb923c","edge":"#ea580c"},
    2500:{"face":"#60a5fa","edge":"#2563eb"},
    2700:{"face":"#34d399","edge":"#059669"},
    3300:{"face":"#fbbf24","edge":"#d97706"},
}


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
    except: return default

def normalize_days(row):
    mapping = [("M","MONDAY_IND"),("T","TUESDAY_IND"),("W","WEDNESDAY_IND"),
               ("R","THURSDAY_IND"),("F","FRIDAY_IND"),("S","SATURDAY_IND"),("U","SUNDAY_IND")]
    out = []
    for letter, col in mapping:
        v = str(row.get(col,"")).strip().upper()
        if v in {letter,"Y","YES","TRUE","1"}: out.append(letter)
    return "".join(out)


# ── Data loading ─────────────────────────────────────────────────────────────

def load_data(csv_path):
    df = pd.read_csv(csv_path)
    for col in ["COURSE_NUMBER","ACADEMIC_PERIOD","BEGIN_TIME","END_TIME",
                "MAXIMUM_ENROLLMENT","ACTUAL_ENROLLMENT","TOTAL_CREDITS_SECTION",
                "MIN_CREDITS","MAX_CREDITS"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    sp26 = df[
        (df["SUBJECT"] == "MATH") &
        (df["ACADEMIC_PERIOD"] == 202602) &
        (df["BEGIN_TIME"].notna()) &
        (df["END_TIME"].notna()) &
        (df["COURSE_NUMBER"].isin(CORE_COURSES))
    ].copy()

    if sp26.empty:
        raise ValueError("No Spring 2026 MATH sections found for the core courses.")

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
        sections.append({
            "id": i, "crn": safe_int(row["CRN"], i),
            "course": course, "title": str(row["TITLE_SHORT_DESC"]),
            "instructor": instr, "days": days, "day_count": dc,
            "begin_int": bi, "end_int": ei,
            "duration_mins": dmins, "weekly_minutes": wmins,
            "credits": credits, "capacity": cap,
            "actual_enroll": actual, "exp_enroll": exp,
            "skel_block": safe_int(row["SKEL_BLOCK"], 0),
            "skel_room": skel_room, "skel_bldg": bldg,
            "color": COURSE_COLORS.get(course, {"face":"#e5e7eb","edge":"#9ca3af"}),
        })

    return sections, rooms


# ── Solve ────────────────────────────────────────────────────────────────────

def build_and_solve(sections, rooms, weights, solver_time=60, num_opts=3, log_fn=print):
    W_SKEL_SLOT    = int(weights.get("w_skeleton_slot",    8))
    W_SKEL_BLDG    = int(weights.get("w_skeleton_bldg",    2))
    W_DEAD_GAP     = int(weights.get("w_dead_gap",        10))
    UE_THRESH      = float(weights.get("under_enroll_threshold", 0.60))
    W_UNDER_ENROLL = int(weights.get("w_under_enroll",    12))
    W_BLOCK_OVER   = int(weights.get("w_block_over",      18))
    INSTR_MAX      = int(weights.get("instructor_max_sections", 3))
    W_INSTR_OVL    = int(weights.get("w_instr_overload",  20))
    W_LOWER_MID    = int(weights.get("w_lower_midday",     5))
    W_UPPER_NOMID  = int(weights.get("w_upper_nonmidday", 15))
    BLK_MIN_PCT    = float(weights.get("block_min_pct",   0.15))
    BLK_MAX_PCT    = float(weights.get("block_max_pct",   0.30))
    MIDDAY_BLOCKS  = {1, 2, 3}
    LOWER_MAX      = 2250

    log_fn("Creating CP-SAT model...")
    model = cp_model.CpModel()
    nr    = len(rooms)
    total = len(sections)

    log_fn("Creating assignment variables...")
    assign = {}
    var_count = 0
    for s in sections:
        sid = s["id"]
        for ri, r in enumerate(rooms):
            if r["capacity"] < s["exp_enroll"]:
                continue
            for b in BLOCK_IDS:
                assign[sid, ri, b] = model.NewBoolVar(f"x_{sid}_{ri}_{b}")
                var_count += 1
    log_fn(f"Created {var_count} assignment variables.")

    log_fn("Adding exactly-one assignment constraints...")
    for s in sections:
        sid = s["id"]
        choices = [
            assign[sid, ri, b]
            for ri in range(nr)
            for b in BLOCK_IDS
            if (sid, ri, b) in assign
        ]
        if not choices:
            raise ValueError(f"No feasible room for CRN {s['crn']} (exp_enroll={s['exp_enroll']})")
        model.AddExactlyOne(choices)

    log_fn("Adding room/block conflict constraints...")
    for ri in range(nr):
        for b in BLOCK_IDS:
            occ = [assign[s["id"], ri, b] for s in sections if (s["id"], ri, b) in assign]
            if len(occ) > 1:
                model.AddAtMostOne(occ)

    log_fn("Grouping sections by instructor...")
    by_instr = defaultdict(list)
    for s in sections:
        if s["instructor"] != "TBA":
            by_instr[s["instructor"]].append(s["id"])

    log_fn("Adding instructor conflict constraints...")
    for instr, sids in by_instr.items():
        if len(sids) < 2:
            continue
        for b in BLOCK_IDS:
            vv = [
                assign[sid, ri, b]
                for sid in sids
                for ri in range(nr)
                if (sid, ri, b) in assign
            ]
            if len(vv) > 1:
                model.AddAtMostOne(vv)

    log_fn("Building block distribution helper variables...")
    blk_floor = max(1, math.floor(total * BLK_MIN_PCT))
    blk_ceil  = math.ceil(total * BLK_MAX_PCT)

    in_block = {}
    for s in sections:
        sid = s["id"]
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"ib_{sid}_{b}")
            choices = [assign[sid, ri, b] for ri in range(nr) if (sid, ri, b) in assign]
            if choices:
                model.AddMaxEquality(v, choices)
            else:
                model.Add(v == 0)
            in_block[sid, b] = v

    blk_over = {}
    for b in BLOCK_IDS:
        sb = model.NewIntVar(0, total, f"sb_{b}")
        model.Add(sb == sum(in_block[s["id"], b] for s in sections))
        model.Add(sb >= blk_floor)
        model.Add(sb <= blk_ceil)

        ov = model.NewIntVar(0, total, f"ov_{b}")
        model.Add(ov >= sb - math.floor(total * BLK_MAX_PCT))
        model.Add(ov >= 0)
        blk_over[b] = ov

    log_fn("Building instructor/block helper variables...")
    iab = {}
    for instr, sids in by_instr.items():
        iab[instr] = {}
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"iab_{instr}_{b}")
            choices = [
                assign[sid, ri, b]
                for sid in sids
                for ri in range(nr)
                if (sid, ri, b) in assign
            ]
            if choices:
                model.AddMaxEquality(v, choices)
            else:
                model.Add(v == 0)
            iab[instr][b] = v

    log_fn("Building objective function...")
    obj = []

    log_fn("Adding skeleton slot penalties...")
    for s in sections:
        sid = s["id"]
        sb  = s["skel_block"]
        for ri in range(nr):
            for b in BLOCK_IDS:
                if (sid, ri, b) not in assign:
                    continue
                d = abs(b - sb)
                if d:
                    obj.append(W_SKEL_SLOT * d * assign[sid, ri, b])

    log_fn("Adding building change penalties...")
    for s in sections:
        sid  = s["id"]
        sbld = s["skel_bldg"]
        for ri, r in enumerate(rooms):
            if r["building"] == sbld:
                continue
            for b in BLOCK_IDS:
                if (sid, ri, b) in assign:
                    obj.append(W_SKEL_BLDG * assign[sid, ri, b])

    log_fn("Adding dead gap penalties...")
    for instr, bmap in iab.items():
        for b1 in BLOCK_IDS:
            for b2 in BLOCK_IDS:
                if b2 <= b1:
                    continue
                gap = b2 - b1
                if gap <= 1:
                    continue
                both = model.NewBoolVar(f"dg_{instr}_{b1}_{b2}")
                model.Add(both <= bmap[b1])
                model.Add(both <= bmap[b2])
                model.Add(both >= bmap[b1] + bmap[b2] - 1)
                obj.append(W_DEAD_GAP * (gap - 1) * both)

    log_fn("Adding under-enrollment penalties...")
    for s in sections:
        if s["exp_enroll"] / max(s["capacity"], 1) >= UE_THRESH:
            continue
        flag = model.NewBoolVar(f"ue_{s['id']}")
        choices = [
            assign[s["id"], ri, b]
            for ri in range(nr)
            for b in BLOCK_IDS
            if (s["id"], ri, b) in assign
        ]
        model.AddMaxEquality(flag, choices)
        obj.append(W_UNDER_ENROLL * flag)

    log_fn("Adding block overflow penalties...")
    for b in BLOCK_IDS:
        obj.append(W_BLOCK_OVER * blk_over[b])

    log_fn("Adding instructor overload penalties...")
    for instr, sids in by_instr.items():
        if len(sids) > INSTR_MAX:
            obj.append(W_INSTR_OVL * (len(sids) - INSTR_MAX))

    log_fn("Adding course timing preference penalties...")
    for s in sections:
        sid = s["id"]
        is_lower = (s["course"] <= LOWER_MAX)
        for ri in range(nr):
            for b in BLOCK_IDS:
                if (sid, ri, b) not in assign:
                    continue
                mid = b in MIDDAY_BLOCKS
                if is_lower and mid:
                    obj.append(W_LOWER_MID * assign[sid, ri, b])
                elif not is_lower and not mid:
                    obj.append(W_UPPER_NOMID * assign[sid, ri, b])

    log_fn(f"Finalizing objective with {len(obj)} objective terms...")
    model.Minimize(sum(obj) if obj else model.NewIntVar(0, 0, "zero"))

    room_ids  = [r["id"] for r in rooms]
    solutions = []

    log_fn(f"Starting solve loop for up to {num_opts} option(s)...")
    for opt_i in range(num_opts):
        log_fn(f"Preparing solver for option {chr(65 + opt_i)}...")
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = float(solver_time)
        solver.parameters.num_search_workers = 4
        solver.parameters.random_seed = 17 + opt_i * 31
        solver.parameters.log_search_progress = False

        log_fn(f"Solving option {chr(65 + opt_i)} with {solver_time}s limit...")
        status = solver.Solve(model)
        status_name = solver.StatusName(status)
        log_fn(f"Solver finished option {chr(65 + opt_i)} with status: {status_name}")

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break

        log_fn(f"Extracting solution for option {chr(65 + opt_i)}...")
        sol = {}
        chosen = []

        for s in sections:
            sid = s["id"]
            placed = False
            for ri in range(nr):
                for b in BLOCK_IDS:
                    if (sid, ri, b) in assign and solver.Value(assign[sid, ri, b]):
                        sol[sid] = {"block": b, "room": room_ids[ri]}
                        chosen.append(assign[sid, ri, b])
                        placed = True
                        break
                if placed:
                    break

            if not placed:
                sol[sid] = {
                    "block": s["skel_block"],
                    "room": s["skel_room"] or "UNASSIGNED"
                }

        score = int(round(solver.ObjectiveValue()))
        label = f"Option {chr(65 + opt_i)}"
        solutions.append({
            "label": label,
            "score": score,
            "assignment": sol
        })
        log_fn(f"Saved {label} with score {score}.")

        if chosen:
            log_fn(f"Adding diversity cut for {label}...")
            model.Add(sum(chosen) <= len(chosen) - 3)

    log_fn(f"Finished build_and_solve with {len(solutions)} solution(s).")
    return solutions
    W_SKEL_SLOT    = int(weights.get("w_skeleton_slot",    8))
    W_SKEL_BLDG    = int(weights.get("w_skeleton_bldg",    2))
    W_DEAD_GAP     = int(weights.get("w_dead_gap",        10))
    UE_THRESH      = float(weights.get("under_enroll_threshold", 0.60))
    W_UNDER_ENROLL = int(weights.get("w_under_enroll",    12))
    W_BLOCK_OVER   = int(weights.get("w_block_over",      18))
    INSTR_MAX      = int(weights.get("instructor_max_sections", 3))
    W_INSTR_OVL    = int(weights.get("w_instr_overload",  20))
    W_LOWER_MID    = int(weights.get("w_lower_midday",     5))
    W_UPPER_NOMID  = int(weights.get("w_upper_nonmidday", 15))
    BLK_MIN_PCT    = float(weights.get("block_min_pct",   0.15))
    BLK_MAX_PCT    = float(weights.get("block_max_pct",   0.30))
    MIDDAY_BLOCKS  = {1, 2, 3}
    LOWER_MAX      = 2250

    model   = cp_model.CpModel()
    nr      = len(rooms)
    total   = len(sections)

    assign = {}
    for s in sections:
        sid = s["id"]
        for ri, r in enumerate(rooms):
            if r["capacity"] < s["exp_enroll"]: continue
            for b in BLOCK_IDS:
                assign[sid, ri, b] = model.NewBoolVar(f"x_{sid}_{ri}_{b}")

    for s in sections:
        sid = s["id"]
        choices = [assign[sid,ri,b] for ri in range(nr) for b in BLOCK_IDS
                   if (sid,ri,b) in assign]
        if not choices:
            raise ValueError(f"No feasible room for CRN {s['crn']} (exp_enroll={s['exp_enroll']})")
        model.AddExactlyOne(choices)

    for ri in range(nr):
        for b in BLOCK_IDS:
            occ = [assign[s["id"],ri,b] for s in sections if (s["id"],ri,b) in assign]
            if len(occ) > 1: model.AddAtMostOne(occ)

    by_instr = defaultdict(list)
    for s in sections:
        if s["instructor"] != "TBA": by_instr[s["instructor"]].append(s["id"])
    for instr, sids in by_instr.items():
        if len(sids) < 2: continue
        for b in BLOCK_IDS:
            vv = [assign[sid,ri,b] for sid in sids for ri in range(nr) if (sid,ri,b) in assign]
            if len(vv) > 1: model.AddAtMostOne(vv)

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

    iab = {}
    for instr, sids in by_instr.items():
        iab[instr] = {}
        for b in BLOCK_IDS:
            v = model.NewBoolVar(f"iab_{instr}_{b}")
            choices = [assign[sid,ri,b] for sid in sids for ri in range(nr) if (sid,ri,b) in assign]
            if choices: model.AddMaxEquality(v, choices)
            else: model.Add(v == 0)
            iab[instr][b] = v

    obj = []

    for s in sections:
        sid=s["id"]; sb=s["skel_block"]
        for ri in range(nr):
            for b in BLOCK_IDS:
                if (sid,ri,b) not in assign: continue
                d = abs(b - sb)
                if d: obj.append(W_SKEL_SLOT * d * assign[sid,ri,b])

    for s in sections:
        sid=s["id"]; sbld=s["skel_bldg"]
        for ri,r in enumerate(rooms):
            if r["building"] == sbld: continue
            for b in BLOCK_IDS:
                if (sid,ri,b) in assign: obj.append(W_SKEL_BLDG * assign[sid,ri,b])

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

    for s in sections:
        if s["exp_enroll"] / max(s["capacity"],1) >= UE_THRESH: continue
        flag = model.NewBoolVar(f"ue_{s['id']}")
        choices = [assign[s["id"],ri,b] for ri in range(nr) for b in BLOCK_IDS
                   if (s["id"],ri,b) in assign]
        model.AddMaxEquality(flag, choices)
        obj.append(W_UNDER_ENROLL * flag)

    for b in BLOCK_IDS:
        obj.append(W_BLOCK_OVER * blk_over[b])

    for instr, sids in by_instr.items():
        if len(sids) > INSTR_MAX:
            obj.append(W_INSTR_OVL * (len(sids) - INSTR_MAX))

    for s in sections:
        sid=s["id"]; is_lower=(s["course"] <= LOWER_MAX)
        for ri in range(nr):
            for b in BLOCK_IDS:
                if (sid,ri,b) not in assign: continue
                mid = b in MIDDAY_BLOCKS
                if is_lower and mid:           obj.append(W_LOWER_MID    * assign[sid,ri,b])
                elif not is_lower and not mid: obj.append(W_UPPER_NOMID  * assign[sid,ri,b])

    model.Minimize(sum(obj) if obj else model.NewIntVar(0,0,"zero"))

    room_ids  = [r["id"] for r in rooms]
    solutions = []

    for opt_i in range(num_opts):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = solver_time
        solver.parameters.num_search_workers  = 4
        solver.parameters.random_seed         = 17 + opt_i * 31
        solver.parameters.log_search_progress = False
        status = solver.Solve(model)
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
        if chosen: model.Add(sum(chosen) <= len(chosen) - 3)

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

    dead_gap_total = 0
    instr_gaps = {}
    for instr, blocks in by_instr.items():
        sb = sorted(set(blocks))
        gaps = []
        for i in range(len(sb)-1):
            g = sb[i+1]-sb[i]
            if g > 1:
                dead_gap_total += (g-1)
                gaps.append({"from": BLOCK_LABEL[sb[i]], "to": BLOCK_LABEL[sb[i+1]],
                              "empty": g-1})
        if gaps: instr_gaps[instr] = gaps

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
                           "ceil_ok":  cnt <= soft_cap + 1})

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
                "start":start,"end":end,"room":asgn["room"],
                "face":s["color"]["face"],"edge":s["color"]["edge"],
                "moved": b != s["skel_block"],
            })

    return {
        "total": total,
        "score": solution["score"],
        "moved": moved, "moved_pct": round(100*moved/total,1) if total else 0,
        "bldg_changes": bldg_changes,
        "under_count": len(under_list), "under_list": under_list,
        "dead_gap_total": dead_gap_total, "instr_gaps": instr_gaps,
        "block_rows": block_rows,
        "course_rows": course_rows,
        "instr_table": instr_table,
        "calendar": calendar,
    }