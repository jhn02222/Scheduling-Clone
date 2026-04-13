import argparse
import json
import os
import sqlite3

import pandas as pd


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV_FILE = os.path.join(SCRIPT_DIR, "Course Schedule of Classes Proof ALL ++_20260116_124500.csv")
DEFAULT_DB_FILE = os.path.join(SCRIPT_DIR, "scheduling.db")
DAY_ORDER = ["M", "T", "W", "R", "F", "S", "U"]
# Allowed baseline patterns for solver inputs.
ALLOWED_PATTERN_DAYS = {"MWF", "MW", "TR", "MTWF", "MWRF"}


def to_int(value, default=None):
    if pd.isna(value):
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def normalize_days(row):
    mapping = [
        ("M", "MONDAY_IND"),
        ("T", "TUESDAY_IND"),
        ("W", "WEDNESDAY_IND"),
        ("R", "THURSDAY_IND"),
        ("F", "FRIDAY_IND"),
        ("S", "SATURDAY_IND"),
        ("U", "SUNDAY_IND"),
    ]
    out = []
    for letter, col in mapping:
        val = str(row.get(col, "")).strip().upper()
        if val in {letter, "Y", "YES", "TRUE", "1"}:
            out.append(letter)
    return "".join(out)


def hhmm_to_minutes(hhmm):
    hhmm = int(hhmm)
    return (hhmm // 100) * 60 + (hhmm % 100)


def parse_section_number(section_value, fallback_crn):
    if pd.isna(section_value):
        return fallback_crn
    raw = str(section_value).strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return fallback_crn
    parsed = int(digits)
    if parsed <= 0:
        return fallback_crn
    return parsed


def seed_professors(cursor, df):
    existing = {
        (str(last), str(first)): pid
        for pid, first, last in cursor.execute("SELECT id, first_name, last_name FROM professor")
    }

    inserted = 0
    for row in df[["PRIMARY_INSTRUCTOR_FIRST_NAME", "PRIMARY_INSTRUCTOR_LAST_NAME"]].drop_duplicates().itertuples(index=False):
        first = str(row.PRIMARY_INSTRUCTOR_FIRST_NAME).strip() if pd.notna(row.PRIMARY_INSTRUCTOR_FIRST_NAME) else ""
        last = str(row.PRIMARY_INSTRUCTOR_LAST_NAME).strip() if pd.notna(row.PRIMARY_INSTRUCTOR_LAST_NAME) else ""
        if not first and not last:
            continue
        key = (last, first)
        if key in existing:
            continue
        cursor.execute("INSERT INTO professor (first_name, last_name) VALUES (?, ?)", (first, last))
        existing[key] = cursor.lastrowid
        inserted += 1

    return existing, inserted


def seed_courses(cursor, df):
    inserted = 0
    for row in (
        df[["COURSE_NUMBER", "TITLE_SHORT_DESC", "MIN_CREDITS", "MAX_CREDITS"]]
        .dropna(subset=["COURSE_NUMBER"])
        .drop_duplicates()
        .itertuples(index=False)
    ):
        course_number = to_int(row.COURSE_NUMBER)
        if course_number is None:
            continue
        course_name = str(row.TITLE_SHORT_DESC).strip() if pd.notna(row.TITLE_SHORT_DESC) else "Untitled Course"
        min_credits = to_int(row.MIN_CREDITS)
        max_credits = to_int(row.MAX_CREDITS)
        cursor.execute(
            """
            INSERT INTO course (course_number, course_name, min_credits, max_credits)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(course_number) DO UPDATE SET
                course_name = excluded.course_name,
                min_credits = excluded.min_credits,
                max_credits = excluded.max_credits
            """,
            (course_number, course_name, min_credits, max_credits),
        )
        inserted += 1
    return inserted


def seed_classrooms(cursor, df):
    inserted = 0
    room_rows = (
        df[["BUILDING", "ROOM", "BUILDING_DESC", "MAXIMUM_ENROLLMENT"]]
        .dropna(subset=["BUILDING", "ROOM"])
        .drop_duplicates()
    )
    for row in room_rows.itertuples(index=False):
        building = str(row.BUILDING).strip()
        if not building.isdigit():
            continue
        building_id = int(building)
        room_number = str(row.ROOM).strip()
        if not room_number:
            continue
        building_name = str(row.BUILDING_DESC).strip() if pd.notna(row.BUILDING_DESC) else "Unknown"
        max_enrollment = to_int(row.MAXIMUM_ENROLLMENT, default=0)
        if max_enrollment <= 0:
            continue

        cursor.execute(
            """
            INSERT INTO classroom (building_id, room_number, building_name, max_enrollment)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(building_id, room_number) DO UPDATE SET
                building_name = excluded.building_name,
                max_enrollment = excluded.max_enrollment
            """,
            (building_id, room_number, building_name, max_enrollment),
        )
        inserted += 1
    return inserted


def fetch_time_slots(cursor):
    slots = []
    for sid, start_text, end_text in cursor.execute(
        "SELECT id, start_time, end_time FROM time_slot ORDER BY start_time"
    ):
        start_hhmm = to_int(start_text.replace(":", ""))
        end_hhmm = to_int(end_text.replace(":", ""))
        if start_hhmm is None or end_hhmm is None:
            raise ValueError("time_slot rows must use HH:MM text values.")
        slots.append(
            {
                "id": sid,
                "start_hhmm": start_hhmm,
                "end_hhmm": end_hhmm,
                "start_minutes": hhmm_to_minutes(start_hhmm),
            }
        )
    if not slots:
        raise ValueError("No rows found in time_slot. Seed db_setup.sql first.")
    return slots


def fetch_meeting_patterns(cursor):
    by_days = {}
    for pattern_id, _name, days in cursor.execute(
        "SELECT id, pattern_name, days FROM meeting_pattern"
    ):
        parsed_days = json.loads(days)
        letters = "".join(DAY_ORDER[idx] for idx in parsed_days)
        by_days[letters] = pattern_id
    if not by_days:
        raise ValueError("No rows found in meeting_pattern. Seed db_setup.sql first.")
    return by_days


def nearest_slot_id(begin_hhmm, slots):
    return min(slots, key=lambda item: abs(item["start_hhmm"] - begin_hhmm))["id"]


def resolve_meeting_pattern_id(days_letters, pattern_map):
    canonical = "TR" if days_letters == "TTh" else days_letters
    if canonical not in ALLOWED_PATTERN_DAYS:
        return None

    if canonical in pattern_map:
        return pattern_map[canonical]
    if canonical == "TR" and "TTh" in pattern_map:
        return pattern_map["TTh"]
    if canonical == "TR" and "TR" in pattern_map:
        return pattern_map["TR"]
    return None


def import_semester_schedule(cursor, df, semester, professors_by_name):
    slots = fetch_time_slots(cursor)
    pattern_map = fetch_meeting_patterns(cursor)

    classroom_ids = {
        (str(bid), str(room)): cid
        for cid, bid, room in cursor.execute("SELECT id, building_id, room_number FROM classroom")
    }

    section_inserted = 0
    schedule_inserted = 0
    block_inserted = 0

    sem_df = df[(df["SUBJECT"] == "MATH") & (df["ACADEMIC_PERIOD"] == semester)].copy()
    sem_df = sem_df[sem_df["CRN"].notna() & sem_df["COURSE_NUMBER"].notna()]

    for _idx, row in sem_df.iterrows():
        course_number = to_int(row["COURSE_NUMBER"])
        crn = to_int(row["CRN"])
        if course_number is None or crn is None:
            continue

        section_source = row["SECTION"] if "SECTION" in row else None
        section_number = parse_section_number(section_source, fallback_crn=crn)
        max_enrollment = to_int(row["MAXIMUM_ENROLLMENT"])
        actual_enrollment = to_int(row["ACTUAL_ENROLLMENT"], default=0)

        cursor.execute(
            """
            INSERT OR IGNORE INTO course_section (
                course_number,
                crn,
                semester,
                section_number,
                maximum_enrollment,
                actual_enrollment
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (course_number, crn, str(semester), section_number, max_enrollment, actual_enrollment),
        )
        if cursor.rowcount:
            section_inserted += 1

        section_id_row = cursor.execute(
            "SELECT id FROM course_section WHERE semester = ? AND crn = ?",
            (str(semester), crn),
        ).fetchone()
        if section_id_row is None:
            raise ValueError(
                "Unable to resolve course_section id after insert/upsert for "
                f"semester={semester}, crn={crn}, course_number={course_number}, "
                f"section_number={section_number}."
            )
        section_id = section_id_row[0]

        first = str(row["PRIMARY_INSTRUCTOR_FIRST_NAME"]).strip() if pd.notna(row["PRIMARY_INSTRUCTOR_FIRST_NAME"]) else ""
        last = str(row["PRIMARY_INSTRUCTOR_LAST_NAME"]).strip() if pd.notna(row["PRIMARY_INSTRUCTOR_LAST_NAME"]) else ""
        professor_id = professors_by_name.get((last, first))

        building = str(row["BUILDING"]).strip() if pd.notna(row["BUILDING"]) else ""
        room_number = str(row["ROOM"]).strip() if pd.notna(row["ROOM"]) else ""
        classroom_id = classroom_ids.get((building, room_number))

        cursor.execute(
            """
            INSERT OR IGNORE INTO schedule (course_section_id, professor_id, classroom_id)
            VALUES (?, ?, ?)
            """,
            (section_id, professor_id, classroom_id),
        )
        if cursor.rowcount:
            schedule_inserted += 1

        schedule_id_row = cursor.execute(
            "SELECT id FROM schedule WHERE course_section_id = ?",
            (section_id,),
        ).fetchone()
        schedule_id = schedule_id_row[0]

        begin_hhmm = to_int(row["BEGIN_TIME"])
        end_hhmm = to_int(row["END_TIME"])
        if begin_hhmm is None or end_hhmm is None:
            continue

        days_letters = normalize_days(row.to_dict())
        meeting_pattern_id = resolve_meeting_pattern_id(days_letters, pattern_map)
        if meeting_pattern_id is None:
            continue

        time_slot_id = nearest_slot_id(begin_hhmm, slots)
        duration = hhmm_to_minutes(end_hhmm) - hhmm_to_minutes(begin_hhmm)
        class_duration = duration if duration in (55, 80) else None

        cursor.execute(
            """
            INSERT OR IGNORE INTO schedule_meeting_block (
                schedule_id,
                time_slot_id,
                meeting_pattern_id,
                class_duration_minutes
            ) VALUES (?, ?, ?, ?)
            """,
            (schedule_id, time_slot_id, meeting_pattern_id, class_duration),
        )
        if cursor.rowcount:
            block_inserted += 1

    return {
        "rows_in_semester": len(sem_df),
        "course_section_inserted": section_inserted,
        "schedule_inserted": schedule_inserted,
        "schedule_meeting_block_inserted": block_inserted,
    }


def main():
    parser = argparse.ArgumentParser(description="Seed scheduling.db and import a semester baseline schedule.")
    parser.add_argument("--csv", default=DEFAULT_CSV_FILE, help="Path to source CSV file")
    parser.add_argument("--db", default=DEFAULT_DB_FILE, help="Path to SQLite DB")
    parser.add_argument("--semester", type=int, default=202602, help="Academic period to import")
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        raise FileNotFoundError(f"CSV file not found: {args.csv}")
    if not os.path.exists(args.db):
        raise FileNotFoundError(f"SQLite DB not found: {args.db}")

    df = pd.read_csv(args.csv)
    conn = sqlite3.connect(args.db)
    try:
        cursor = conn.cursor()
        professors_by_name, professor_inserted = seed_professors(cursor, df)
        course_upserts = seed_courses(cursor, df)
        classroom_upserts = seed_classrooms(cursor, df)
        sem_stats = import_semester_schedule(cursor, df, args.semester, professors_by_name)
        conn.commit()
    finally:
        conn.close()

    print("Import complete")
    print(f"  Professors inserted: {professor_inserted}")
    print(f"  Courses upserted: {course_upserts}")
    print(f"  Classrooms upserted: {classroom_upserts}")
    print(f"  Semester rows considered: {sem_stats['rows_in_semester']}")
    print(f"  Course sections inserted: {sem_stats['course_section_inserted']}")
    print(f"  Schedule rows inserted: {sem_stats['schedule_inserted']}")
    print(f"  Meeting blocks inserted: {sem_stats['schedule_meeting_block_inserted']}")


if __name__ == "__main__":
    main()