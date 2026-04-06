-- BUILDINGS
CREATE TABLE IF NOT EXISTS building (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- CLASSROOMS
CREATE TABLE IF NOT EXISTS classroom (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id INTEGER NOT NULL,
    room_number TEXT NOT NULL,
    actual_enrollment INTEGER DEFAULT 0,
    maximum_enrollment INTEGER NOT NULL,
    active_learning BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (building_id) REFERENCES building(id),
    UNIQUE(building_id, room_number)
);

-- TIME_SLOTS
CREATE TABLE IF NOT EXISTS time_slot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_minutes INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(start_time, end_time)
);

-- MEETING_PATTERNS
CREATE TABLE IF NOT EXISTS meeting_pattern (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name TEXT NOT NULL UNIQUE,
    days TEXT NOT NULL,
    num_days INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- COURSES
CREATE TABLE IF NOT EXISTS course (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    course_number TEXT NOT NULL,
    course_code TEXT GENERATED ALWAYS AS (subject || ' ' || course_number) STORED UNIQUE,
    course_name TEXT NOT NULL,
    min_credits INTEGER,
    max_credits INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- PROFESSORS
CREATE TABLE IF NOT EXISTS professor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT,
    last_name TEXT,
    full_name TEXT GENERATED ALWAYS AS (TRIM(COALESCE(first_name || ' ' || last_name, ''))) STORED,
    department TEXT DEFAULT 'MATH',
    email TEXT UNIQUE,
    phone TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- PROFESSOR_PREFERENCES
CREATE TABLE IF NOT EXISTS professor_preference (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    professor_id INTEGER NOT NULL UNIQUE,
    preferred_days TEXT,
    avoid_days TEXT,
    preferred_time_slots TEXT,
    avoid_time_slots TEXT,
    preferred_buildings TEXT,
    preferred_courses TEXT,
    avoid_courses TEXT,
    min_gap_between_classes INTEGER DEFAULT 0,
    no_back_to_back INTEGER DEFAULT 0,
    office_building_id INTEGER,
    notes TEXT,
    created_at DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (professor_id) REFERENCES professor(id),
    FOREIGN KEY (office_building_id) REFERENCES building(id)
);

-- SCHEDULE
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crn INTEGER NOT NULL,
    semester TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    professor_id INTEGER,
    classroom_id INTEGER,
    section_number INTEGER DEFAULT 1,
    maximum_enrollment INTEGER,
    actual_enrollment INTEGER DEFAULT 0,
    created_at DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (course_id) REFERENCES course(id),
    FOREIGN KEY (professor_id) REFERENCES professor(id),
    FOREIGN KEY (classroom_id) REFERENCES classroom(id),
    UNIQUE(semester, course_id, section_number),
    UNIQUE(semester, crn)
);

-- SCHEDULE_MEETING_BLOCK
CREATE TABLE IF NOT EXISTS schedule_meeting_block (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    time_slot_id INTEGER NOT NULL,
    meeting_pattern_id INTEGER NOT NULL,
    created_at DEFAULT CURRENT_TIMESTAMP,
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
    created_at DEFAULT CURRENT_TIMESTAMP,
    priority INTEGER DEFAULT 1,
    FOREIGN KEY (involves_professor_id) REFERENCES professor(id),
    FOREIGN KEY (involves_course_id) REFERENCES course(id)
);

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
    ('MWThF', '[0, 1, 2, 3]', 4),
    ('MWTh', '[0, 2, 3]', 3);