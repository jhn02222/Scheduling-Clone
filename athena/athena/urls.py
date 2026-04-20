from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from optimizer import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.index, name="index"),
    path("login/", auth_views.LoginView.as_view(template_name="optimizer/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="login"), name="logout"),
    path("register/", views.register, name="register"),
    path("run/", views.run_optimizer, name="run_optimizer"),
    path("status/", views.job_status, name="job_status"),
    path("export/", views.export_csv, name="export_csv"),
    path("save/", views.save_schedule, name="save_schedule"),
    path("schedules/", views.saved_schedules, name="saved_schedules"),
]
