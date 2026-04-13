from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = 'django-insecure-athena-math-optimizer-local-dev-key'
DEBUG = True
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.staticfiles',
    'optimizer',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'athena.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'athena.wsgi.application'
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR.parent / 'scheduling.db',
    }
}
STATIC_URL = '/static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Solver input source: "db" (SQLite tables) or "csv" (legacy file pipeline)
SCHEDULE_DATA_SOURCE = os.getenv('SCHEDULE_DATA_SOURCE', 'db').lower()

# Legacy CSV path kept for migration/comparison runs.
SCHEDULE_CSV = BASE_DIR / "athena" / "data" / "Course Schedule of Classes Proof ALL ++_20260116_124500.csv"

# Semester loaded by the DB-backed solver path.
SCHEDULE_SEMESTER = os.getenv('SCHEDULE_SEMESTER', '202602')

# Course scope for solver input: 'core' or 'all_math'.
SCHEDULE_COURSE_SCOPE = os.getenv('SCHEDULE_COURSE_SCOPE', 'all_math').lower()