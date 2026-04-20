-- CLASSROOMS
CREATE TABLE IF NOT EXISTS classroom (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id INTEGER NOT NULL,
    room_number TEXT NOT NULL,
    building_name TEXT NOT NULL,
    max_enrollment INTEGER NOT NULL,
    UNIQUE(building_id, room_number)
);

-- TIME_SLOTS
-- Institutional block windows are fixed at 80 minutes.
CREATE TABLE IF NOT EXISTS time_slot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK (duration_minutes = 80),
    UNIQUE(start_time, end_time)
);

-- MEETING_PATTERNS
CREATE TABLE IF NOT EXISTS meeting_pattern (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name TEXT NOT NULL UNIQUE,
    days TEXT NOT NULL,
    num_days INTEGER
);

-- COURSES
CREATE TABLE IF NOT EXISTS course (
    course_number INTEGER PRIMARY KEY,
    course_name TEXT NOT NULL,
    min_credits INTEGER,
    max_credits INTEGER
);

-- COURSE_SECTIONS (semester-specific offerings with CRN)
CREATE TABLE IF NOT EXISTS course_section (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_number INTEGER NOT NULL,
    crn INTEGER NOT NULL,
    semester TEXT NOT NULL,
    section_number INTEGER DEFAULT 1,
    maximum_enrollment INTEGER,
    actual_enrollment INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (course_number) REFERENCES course(course_number),
    UNIQUE(semester, crn),
    UNIQUE(semester, course_number, section_number)
);

-- PROFESSORS
CREATE TABLE IF NOT EXISTS professor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT not null,
    last_name TEXT not null
);

-- PROFESSOR_PREFERENCES
CREATE TABLE IF NOT EXISTS professor_preference (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    professor_id INTEGER NOT NULL UNIQUE,
    preferred_days TEXT,
    preferred_time_slots TEXT,
    avoid_time_slots TEXT,
    preferred_courses TEXT,
    avoid_courses TEXT,
    min_gap_between_classes INTEGER DEFAULT 0,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (professor_id) REFERENCES professor(id)
);

-- SCHEDULE
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_section_id INTEGER NOT NULL,
    professor_id INTEGER,
    classroom_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (course_section_id) REFERENCES course_section(id),
    FOREIGN KEY (professor_id) REFERENCES professor(id),
    FOREIGN KEY (classroom_id) REFERENCES classroom(id),
    UNIQUE(course_section_id)
);

-- SCHEDULE_MEETING_BLOCK
-- class_duration_minutes stores actual class length inside the 80-minute block.
-- Valid durations are currently 55 or 80 minutes.
CREATE TABLE IF NOT EXISTS schedule_meeting_block (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    time_slot_id INTEGER NOT NULL,
    meeting_pattern_id INTEGER NOT NULL,
    class_duration_minutes INTEGER CHECK (class_duration_minutes IN (55, 80)),
    FOREIGN KEY (schedule_id) REFERENCES schedule(id),
    FOREIGN KEY (time_slot_id) REFERENCES time_slot(id),
    FOREIGN KEY (meeting_pattern_id) REFERENCES meeting_pattern(id),
    UNIQUE(schedule_id, time_slot_id, meeting_pattern_id)
);

-- CONSTRAINTS
CREATE TABLE IF NOT EXISTS constraint_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    constraint_type TEXT NOT NULL,
    description TEXT NOT NULL,
    involves_professor_id INTEGER,
    involves_course_id INTEGER,
    constraint_rule TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    priority INTEGER DEFAULT 1,
    FOREIGN KEY (involves_professor_id) REFERENCES professor(id),
    FOREIGN KEY (involves_course_id) REFERENCES course(course_number)
);

CREATE TABLE IF NOT EXISTS user (
    username TEXT PRIMARY KEY;
    password TEXT NOT NULL;
)

INSERT OR IGNORE INTO time_slot (start_time, end_time, duration_minutes) VALUES
    ('08:15', '09:35', 80),
    ('09:55', '11:15', 80),
    ('11:35', '12:55', 80),
    ('13:15', '14:35', 80),
    ('14:55', '16:15', 80),
    ('16:35', '17:55', 80);

INSERT OR IGNORE INTO meeting_pattern (pattern_name, days, num_days) VALUES
    ('MWF', '[0, 2, 4]', 3),
    ('MW', '[0, 2]', 2),
    ('TTh', '[1, 3]', 2),
    ('MTWF', '[0, 1, 2, 4]', 4),
    ('MWThF', '[0, 2, 3, 4]', 4);


