from django.core.management.base import BaseCommand
from optimizer.models import Professor, Schedule

class Command(BaseCommand):
    help = 'Auto-deactivate professors with no 2026 sections'

    def handle(self, *args, **kwargs):
        semester = '202602'
        profs_with_sections = set(
            Schedule.objects.filter(
                course_section__semester=semester,
                course_section__maximum_enrollment__gt=0
            ).values_list('professor_id', flat=True)
        )

        deactivated = 0
        for prof in Professor.objects.all():
            if prof.id not in profs_with_sections:
                if prof.is_active:
                    prof.is_active = False
                    prof.save()
                    deactivated += 1

        self.stdout.write(f'Deactivated {deactivated} professors with no 2026 sections.')
        self.stdout.write(f'Professors with 2026 sections: {len(profs_with_sections)}')
