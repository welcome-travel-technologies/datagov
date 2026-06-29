@echo off
cd /d "%~dp0"

REM This helper uses the database configured in the repository-level .env file.
REM With DEBUG=False, Django connects to the configured PostgreSQL database.

echo Applying local database migrations...
python manage.py migrate

echo Creating cache table (safe to run multiple times)...
python manage.py createcachetable

echo Starting Django Q Cluster in a new window...
echo NOTE: If chat is stuck, close that window and re-run this bat to get a fresh worker.
start "Django Q Cluster" cmd /k "cd /d ""%~dp0"" && python manage.py qcluster"

echo Starting Django Development Server...
python manage.py runserver
