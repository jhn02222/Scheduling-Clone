from django.db import migrations, models
 
 
class Migration(migrations.Migration):
 
    dependencies = [
        ('optimizer', '0001_initial'),   # ← adjust to your last migration name
    ]
 
    operations = [
        migrations.CreateModel(
            name='CourseConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID')),
                ('course_number', models.IntegerField(unique=True)),
                ('display_name', models.CharField(blank=True, max_length=120)),
                ('is_active', models.BooleanField(default=True)),
                ('min_sections', models.IntegerField(blank=True, null=True,
                    help_text='Minimum number of sections that must be scheduled')),
                ('max_sections', models.IntegerField(blank=True, null=True,
                    help_text='Maximum number of sections allowed')),
                ('banned_blocks', models.CharField(blank=True, default='', max_length=40,
                    help_text='Comma-separated block IDs (0–5) forbidden for this course')),
                ('max_per_block', models.IntegerField(blank=True, null=True,
                    help_text='Max sections of this course allowed in any single time block')),
                ('preferred_building', models.CharField(blank=True, default='', max_length=40,
                    help_text='Preferred building code (soft constraint)')),
                ('required_room_type', models.CharField(
                    choices=[('any', 'Any room'), ('lecture', 'Lecture hall (≥ 60 seats)'),
                             ('seminar', 'Seminar room (< 40 seats)'), ('lab', 'Computer lab')],
                    default='any', max_length=20,
                    help_text='Room type requirement (hard constraint)')),
                ('min_room_capacity', models.IntegerField(blank=True, null=True,
                    help_text='Force a minimum room capacity regardless of enrollment')),
            ],
            options={'ordering': ['course_number'], 'verbose_name': 'Course Configuration'},
        ),
    ]
 