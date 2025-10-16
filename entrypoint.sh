#!/bin/sh
echo "Starting weekly tender fetcher cron..."
echo "0 9 * * TUE python /app/devaid.py" > /etc/crontab
cron -f
