# worker.py
import os
import psycopg2
import requests
from celery import Celery
import logging
import ssl
from kombu.utils.url import maybe_sanitize_url
from urllib.parse import quote
import redis
import socket

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Get Redis connection details
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD')
REDIS_HOST = os.getenv('REDIS_HOST', 'summary-redis-cache.redis.cache.windows.net')
REDIS_PORT = os.getenv('REDIS_PORT', '6380')

# Configure Celery
celery_app = Celery('file_processor')

celery_app.conf.update(
    # Broker settings
    broker_url=os.getenv('REDIS_URL'),
    broker_connection_retry=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=None,  # Retry forever
    broker_connection_timeout=60,
    broker_heartbeat=None,  # Disable heartbeat
    broker_pool_limit=None,  # No limit

    # Connection settings
    broker_transport_options={
        'visibility_timeout': 43200,  # 12 hours
        'socket_timeout': 60,
        'socket_connect_timeout': 60,
        'socket_keepalive': True,
        'retry_on_timeout': True,
        'max_retries': None,  # Retry forever
        'socket_keepalive_options': {
            socket.TCP_KEEPIDLE: 30,     # Using socket constant instead of string
            socket.TCP_KEEPINTVL: 5,     # Using socket constant instead of string
            socket.TCP_KEEPCNT: 3        # Using socket constant instead of string
        }
    },

    # Result backend settings (disable to reduce connections)
    result_backend=None,  # Disable result backend since we update DB directly

    # Worker settings
    worker_concurrency=4,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,  # Restart worker process after 100 tasks
    worker_max_memory_per_child=150000,  # 150MB

    worker_enable_remote_control=False,  # Disable remote control
    worker_send_task_events=False,       # Disable task events
    worker_pool_restarts=False,          # Disable pool restarts
    worker_disable_rate_limits=True,     # Disable rate limits since we handle it in task

    # Turn off worker communication features
    worker_enable_mingle=False,          # Disable mingle
    worker_enable_gossip=False,  
    
    # Task settings
    task_serializer='json',
    accept_content=['json'],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_rate_limit='10/m',
    task_time_limit=300,
    
    # Connection recovery settings
    broker_connection_backoff=3,
    broker_connection_backoff_max=60,
    
    # Logging
    worker_redirect_stdouts_level='INFO'
)

@celery_app.task(bind=True, 
                rate_limit='10/m',
                time_limit=310,
                soft_time_limit=300)  # Removed retry-related parameters since we'll handle it ourselves
def process_file(self, file_id: int, file_url: str, opportunity_id: int):
    """Celery task to process a single file"""
    logger.info(f"Processing KOSSSS file {file_id}")
    
    try:
        # Make request to summary generation service
        api_url = 'https://summary-summarizer.azurewebsites.net/summarize_attachment'
        payload = {'file_url': file_url}
        
        response = requests.post(api_url, json=payload, timeout=300)
        response.raise_for_status()
        
        summary = response.json().get('summary')
        
        if not summary:
            raise ValueError("No summary received from API")
            
        # Update database with summary
        with psycopg2.connect(os.getenv('DATABASE_URL')) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE resource_links 
                    SET summary = %s, 
                        status = 'completed',
                        processed_at = NOW()
                    WHERE id = %s
                """, (summary, file_id))
                conn.commit()
                
        logger.info(f"Successfully processed file {file_id}")
        
    except Exception as e:
        logger.error(f"Error processing file {file_id}: {str(e)}")
        
        try:
            with psycopg2.connect(os.getenv('DATABASE_URL')) as conn:
                with conn.cursor() as cur:
                    # First get current retry count
                    cur.execute("""
                        SELECT retry_count 
                        FROM resource_links 
                        WHERE id = %s
                    """, (file_id,))
                    result = cur.fetchone()
                    retry_count = (result[0] or 0) if result else 0
                    
                    if retry_count >= 2:  # Max 3 attempts (initial + 2 retries)
                        cur.execute("""
                            UPDATE resource_links 
                            SET status = 'failed',
                                error_message = %s,
                                processed_at = NOW()
                            WHERE id = %s
                        """, (str(e), file_id))
                        logger.error(f"File {file_id} failed after {retry_count + 1} attempts")
                    else:
                        # Increment retry count and keep status as pending
                        cur.execute("""
                            UPDATE resource_links 
                            SET status = 'pending',
                                error_message = %s,
                                retry_count = retry_count + 1
                            WHERE id = %s
                        """, (str(e), file_id))
                        logger.info(f"File {file_id} scheduled for retry {retry_count + 1}/3")
                    conn.commit()
        except Exception as db_error:
            logger.error(f"Error updating database for file {file_id}: {str(db_error)}")
        
        raise  # Raise the exception to mark the task as failed

if __name__ == '__main__':
    celery_app.start()