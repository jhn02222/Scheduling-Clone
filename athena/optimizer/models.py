from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User

#testing
class Classroom(models.Model):
    building_id = models.IntegerField()
    room_number = models.TextField()
    building_name = models.TextField()
    max_enrollment = models.IntegerField()

    class Meta:
        db_table = "classroom"
        managed = True
        constraints = [
            models.UniqueConstraint(
                fields=["building_id", "room_number"],
                name="classroom_building_room_unique",
            )
        ]


class TimeSlot(models.Model):
    start_time = models.TextField()
    end_time = models.TextField()
    duration_minutes = models.IntegerField()

    class Meta:
        db_table = "time_slot"
        managed = True
        constraints = [
            models.UniqueConstraint(
                fields=["start_time", "end_time"],
                name="time_slot_start_end_unique",
            )
        ]


class MeetingPattern(models.Model):
    pattern_name = models.TextField(unique=True)
    days = models.TextField()
    num_days = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "meeting_pattern"
        managed = True


class Course(models.Model):
    course_number = models.IntegerField(primary_key=True)
    course_name = models.TextField()
    min_credits = models.IntegerField(null=True, blank=True)
    max_credits = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "course"
        managed = True


class CourseSection(models.Model):
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        db_column="course_number",
        null=True,
        blank=True,
    )
    crn = models.IntegerField()
    semester = models.TextField()
    section_number = models.IntegerField(default=1)
    maximum_enrollment = models.IntegerField(null=True, blank=True)
    actual_enrollment = models.IntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "course_section"
        managed = True
        constraints = [
            models.UniqueConstraint(
                fields=["semester", "crn"],
                name="course_section_sem_crn_unique",
            ),
            models.UniqueConstraint(
                fields=["semester", "course", "section_number"],
                name="course_section_sem_course_section_unique",
            ),
        ]


class Professor(models.Model):
    first_name = models.TextField()
    last_name = models.TextField()
    is_active = models.BooleanField(default=True)
    
    class Meta:
        db_table = "professor"
        managed = True


class ProfessorPreference(models.Model):
    TENURED_CHOICES = [('yes', 'Yes'), ('no', 'No'), ('unknown', 'Unknown')]
    TIME_PREF_CHOICES = [
        ('morning', 'Morning (8-11am)'),
        ('midday', 'Midday (11am-2pm)'),
        ('afternoon', 'Afternoon (2-6pm)'),
        ('no_early', 'No Early Morning'),
        ('no_late', 'No Late Afternoon'),
        ('any', 'No Preference'),
    ]
    DAY_PREF_CHOICES = [
        ('MWF', 'Mon/Wed/Fri'),
        ('TR', 'Tue/Thu'),
        ('MW', 'Mon/Wed'),
        ('any', 'No Preference'),
    ]
    LEVEL_PREF_CHOICES = [
        ('lower', 'Lower Division (1000-2000)'),
        ('upper', 'Upper Division (3000-4000)'),
        ('grad', 'Graduate (5000+)'),
        ('any', 'No Preference'),
    ]
    LOAD_PREF_CHOICES = [
        ('1', '1 Section'),
        ('2', '2 Sections'),
        ('3', '3 Sections'),
        ('4', '4+ Sections'),
        ('any', 'No Preference'),
    ]

    professor = models.OneToOneField(
        Professor, 
        on_delete=models.CASCADE,
        related_name='preference', 
        db_column='professor_id',
        null=True,   
        blank=True,  
    )
    tenured = models.CharField(
        max_length=10, choices=TENURED_CHOICES, default='unknown'
    )
    time_of_day = models.CharField(
        max_length=20, choices=TIME_PREF_CHOICES, default='any'
    )
    day_pattern = models.CharField(
        max_length=10, choices=DAY_PREF_CHOICES, default='any'
    )
    level_preference = models.CharField(
        max_length=10, choices=LEVEL_PREF_CHOICES, default='any'
    )
    max_sections = models.CharField(
        max_length=5, choices=LOAD_PREF_CHOICES, default='any'
    )
    avoid_back_to_back = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'professor_preference'
        managed = True


class Schedule(models.Model):
    course_section = models.OneToOneField(
        CourseSection,
        on_delete=models.CASCADE,
        db_column="course_section_id",
    )
    professor = models.ForeignKey(
        Professor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="professor_id",
    )
    classroom = models.ForeignKey(
        Classroom,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="classroom_id",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "schedule"
        managed = True


class ScheduleMeetingBlock(models.Model):
    schedule = models.ForeignKey(
        Schedule,
        on_delete=models.CASCADE,
        db_column="schedule_id",
    )
    time_slot = models.ForeignKey(
        TimeSlot,
        on_delete=models.CASCADE,
        db_column="time_slot_id",
    )
    meeting_pattern = models.ForeignKey(
        MeetingPattern,
        on_delete=models.CASCADE,
        db_column="meeting_pattern_id",
    )
    class_duration_minutes = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "schedule_meeting_block"
        managed = True
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "time_slot", "meeting_pattern"],
                name="schedule_meeting_block_unique",
            )
        ]


class ConstraintRecord(models.Model):
    constraint_type = models.TextField()
    description = models.TextField()
    involves_professor = models.ForeignKey(
        Professor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="involves_professor_id",
    )
    involves_course = models.ForeignKey(
        Course,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="involves_course_id",
    )
    constraint_rule = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    priority = models.IntegerField(default=1)

    class Meta:
        db_table = "constraint_record"
        managed = True


class SavedSchedule(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="saved_schedules",
    )
    name = models.CharField(max_length=200)
    semester = models.TextField()
    solution_data = models.JSONField()
    stats_data = models.JSONField()
    weights_used = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    score = models.IntegerField(null=True)

    class Meta:
        ordering = ["-created_at"]


class CourseConfig(models.Model):
    """
    Per-course hard and soft constraints for the optimizer.
    One row per course number. All fields optional — None = unconstrained.
    """
    course_number = models.IntegerField(unique=True)
    display_name  = models.CharField(max_length=120, blank=True)
 
    # Active / inactive — inactive courses are excluded from the run entirely
    is_active = models.BooleanField(default=True)
 
    # ── Section count constraint ───────────────────────────────────────────
    # If set, solver must assign exactly this many sections for the course.
    # Set min_sections = max_sections for an exact count.
    min_sections = models.IntegerField(null=True, blank=True,
        help_text="Minimum number of sections that must be scheduled")
    max_sections = models.IntegerField(null=True, blank=True,
        help_text="Maximum number of sections allowed")
 
    # ── Banned time blocks ─────────────────────────────────────────────────
    # Comma-separated block IDs (0-5) that are forbidden for this course.
    # e.g. "5" bans the 4:35 PM block; "0,5" bans 8:15 AM and 4:35 PM.
    banned_blocks = models.CharField(max_length=40, blank=True, default="",
        help_text="Comma-separated block IDs (0–5) forbidden for this course")
 
    # ── Max sections per block ─────────────────────────────────────────────
    max_per_block = models.IntegerField(null=True, blank=True,
        help_text="Max sections of this course allowed in any single time block")
 
    # ── Building preference ────────────────────────────────────────────────
    # Soft preference — solver penalises assignments outside this building.
    preferred_building = models.CharField(max_length=40, blank=True, default="",
        help_text="Preferred building code (soft constraint)")
 
    # ── Room type requirement ──────────────────────────────────────────────
    ROOM_TYPE_CHOICES = [
        ("any",       "Any room"),
        ("lecture",   "Lecture hall (≥ 60 seats)"),
        ("seminar",   "Seminar room (< 40 seats)"),
        ("lab",       "Computer lab"),
    ]
    required_room_type = models.CharField(max_length=20, default="any",
        choices=ROOM_TYPE_CHOICES,
        help_text="Room type requirement (hard constraint)")
 
    # Minimum capacity override — if set, overrides exp_enroll-based room filter
    min_room_capacity = models.IntegerField(null=True, blank=True,
        help_text="Force a minimum room capacity regardless of enrollment")
 
    class Meta:
        ordering = ["course_number"]
        verbose_name = "Course Configuration"
 
    def __str__(self):
        return f"MATH {self.course_number} config"
 
    def get_banned_block_list(self):
        """Return list of int block IDs that are banned."""
        if not self.banned_blocks:
            return []
        return [int(b.strip()) for b in self.banned_blocks.split(",") if b.strip().isdigit()]
 