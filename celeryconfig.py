# celeryconfig.py
import os


broker_url = os.getenv('REDIS_URL')
result_backend = os.getenv('REDIS_URL')
task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']
enable_utc = True
worker_concurrency = 4
broker_connection_retry_on_startup = True