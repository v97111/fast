# Gunicorn configuration for Railway/Heroku deployment
# -w 2: Use 2 workers for better resilience and performance
# -k gthread: Use threaded workers (no additional dependencies required)
# -t 120: Set timeout to 120 seconds for worker requests
# -b 0.0.0.0:$PORT: Bind to all interfaces on the platform-provided port
web: gunicorn -w 2 -k gthread -t 120 -b 0.0.0.0:$PORT main:app