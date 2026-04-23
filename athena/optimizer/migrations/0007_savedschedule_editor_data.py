from django.db import migrations, models
 
 
class Migration(migrations.Migration):
 
    dependencies = [
        ('optimizer', '0006_merge_0004_professor_is_active_0005_courseconfig'),
    ]
 
    operations = [
        migrations.AddField(
            model_name='savedschedule',
            name='editor_data',
            field=models.JSONField(blank=True, null=True,
                help_text='Manual overrides applied on top of solution_data'),
        ),
        migrations.AddField(
            model_name='savedschedule',
            name='is_edited',
            field=models.BooleanField(default=False,
                help_text='True if this schedule has manual editor overrides'),
        ),
    ]
 