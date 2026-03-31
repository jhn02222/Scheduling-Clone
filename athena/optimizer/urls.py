from django.urls import path
from . import views

urlpatterns = [
    path('',        views.index,         name='index'),
    path('run/',    views.run_optimizer,  name='run'),
    path('status/', views.job_status,     name='status'),
    path('export/', views.export_csv,     name='export'),
]