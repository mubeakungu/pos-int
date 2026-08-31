#!/usr/bin/env bash

echo "--> Running database upgrade..."
flask db upgrade

echo "--> Starting Gunicorn server..."
gunicorn --bind 0.0.0.0:10000 --workers 4 --threads 4 --worker-class gthread "app:app"
