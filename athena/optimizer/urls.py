from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from optimizer import views
urlpatterns = [
    path('',        views.index,         name='index'),
    path('run/',    views.run_optimizer,  name='run'),
    path('status/', views.job_status,     name='status'),
    path('export/', views.export_csv,     name='export'),
]