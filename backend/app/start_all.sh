#!/bin/bash
cd "$(dirname "$0")"

# This helper uses the database configured in the repository-level .env file.
# With DEBUG=False, Django connects to the configured PostgreSQL database.

echo "Applying local database migrations..."
python manage.py migrate

echo "Creating cache table (safe to run multiple times)..."
python manage.py createcachetable

echo "Starting Django Q Cluster in background..."
python manage.py qcluster &
QCLUSTER_PID=$!

echo "Starting Django Development Server..."
python manage.py runserver

# When runserver is stopped, kill the qcluster background process
kill $QCLUSTER_PID