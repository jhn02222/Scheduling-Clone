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