import pandas as pd
import sqlite3
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
excel_file = os.path.join(script_dir, 'Course Schedule of Classes Proof ALL ++_20260116_124500.xlsx')
db_file = os.path.join(script_dir, 'scheduling.db')

print(f"Reading from: {excel_file}")
print(f"File exists: {os.path.exists(excel_file)}")

try:
    df = pd.read_excel(excel_file, sheet_name='Sheet5', header=0)
    print("Available columns:")
    print(df.columns.tolist())
    
    professors = df[['PRIMARY_INSTRUCTOR_FIRST_NAME', 'PRIMARY_INSTRUCTOR_LAST_NAME']].drop_duplicates()
    professors.columns = ['first_name', 'last_name']
    print(f"Found {len(professors)} unique professors")

    courses = df[['COURSE_NUMBER', 'TITLE_SHORT_DESC', 'MIN_CREDITS', 'MAX_CREDITS']].drop_duplicates()
    courses.columns = ['course_number', 'course_name', 'min_credits', 'max_credits']

    classrooms = df[['BUILDING', 'ROOM', 'BUILDING_DESC', 'MAXIMUM_ENROLLMENT']].drop_duplicates()
    classrooms.columns = ['building_id', 'room_number', 'building_name', 'max_enrollment']

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    inserted = 0
    skipped = 0
    
    # Fill professor table
    for idx, row in professors.iterrows():
        try:
            cursor.execute(
                "INSERT INTO professor (first_name, last_name) VALUES (?, ?)",
                (row['first_name'], row['last_name'])
            )
            inserted += 1
        except sqlite3.IntegrityError as e:
            skipped += 1
    
    print(f"Successfully inserted {inserted} professors, skipped {skipped} duplicates/errors")
    
    # Fill course table
    inserted = 0
    skipped = 0
    for idx, row in courses.iterrows():
        try:
            cursor.execute(
                "INSERT INTO course (course_number, course_name, min_credits, max_credits) VALUES (?, ?, ?, ?)",
                (row['course_number'], row['course_name'], row['min_credits'], row['max_credits'])
            )
            inserted += 1
        except sqlite3.IntegrityError as e:
            skipped += 1

    print(f"Successfully inserted {inserted} courses, skipped {skipped} duplicates/errors")
    
    # Fill classroom table
    inserted = 0
    skipped = 0
    for idx, row in classrooms.iterrows():
        try:
            cursor.execute(
                "INSERT INTO classroom (building_id, room_number, building_name, max_enrollment) VALUES (?, ?, ?, ?)",
                (row['building_id'], row['room_number'], row['building_name'], row['max_enrollment'])
            )
            inserted += 1
        except sqlite3.IntegrityError as e:
            skipped += 1
    print(f"Successfully inserted {inserted} classrooms, skipped {skipped} duplicates/errors")


    conn.commit()
    conn.close()   
    
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()