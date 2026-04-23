from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from optimizer import views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.index, name='index'),
    path('login/', auth_views.LoginView.as_view(template_name='optimizer/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='login'), name='logout'),
    path('register/', views.register, name='register'),
    path('run/', views.run_optimizer, name='run_optimizer'),
    path('status/', views.job_status, name='job_status'),
    path('export/', views.export_csv, name='export_csv'),
    path('save/', views.save_schedule, name='save_schedule'),
    path('schedules/json/', views.schedules_json, name='schedules_json'),
    path('schedules/<int:schedule_id>/load/', views.load_schedule, name='load_schedule'),
    path('schedules/<int:schedule_id>/delete/', views.delete_schedule, name='delete_schedule'),
    path('professors/json/', views.professors_json, name='professors_json'),
    path('professors/add/', views.professor_add_json, name='professor_add'),
    path('professors/<int:prof_id>/json/', views.professor_pref_json, name='professor_pref_json'),
    path('professors/<int:prof_id>/pref/', views.professor_save_pref, name='professor_save_pref'),
    path('professors/<int:prof_id>/delete/', views.professor_delete, name='professor_delete'),
    path('professors/<int:prof_id>/toggle/', views.professor_toggle_active, name='professor_toggle'),
    path('courses/json/',                           views.courses_json,          name='courses_json'),
    path('courses/add/',                            views.course_add,            name='course_add'),
    path('courses/<int:course_id>/json/',            views.course_detail_json,    name='course_detail_json'),
    path('courses/<int:course_id>/toggle/',          views.course_toggle_active,  name='course_toggle'),
    path('courses/<int:course_id>/delete/',          views.course_delete,         name='course_delete'),
 
]