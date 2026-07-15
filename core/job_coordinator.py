import threading
import logging
import concurrent.futures
from typing import Optional, Callable

logger = logging.getLogger("milo.job_coordinator")

class AlreadyRunning(Exception):
    pass

class Ticket:
    def __init__(self, job_type: str, business_id: Optional[str] = None):
        self.job_type = job_type
        self.business_id = business_id
        self.is_cancelled = False

class JobCoordinator:
    def __init__(self):
        self.active_jobs = {}
        self.lock = threading.Lock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=20)

    def start_job(self, job_type: str, business_id: Optional[str] = None) -> Ticket:
        key = (job_type, business_id)
        with self.lock:
            if key in self.active_jobs:
                raise AlreadyRunning(f"{job_type} for {business_id} already running")
            ticket = Ticket(job_type, business_id)
            self.active_jobs[key] = ticket
            return ticket

    def finish_job(self, ticket: Ticket):
        key = (ticket.job_type, ticket.business_id)
        with self.lock:
            if self.active_jobs.get(key) is ticket:
                del self.active_jobs[key]

    def run_with_timeout(self, job_type: str, business_id: Optional[str], timeout: float, func: Callable, *args, **kwargs):
        try:
            ticket = self.start_job(job_type, business_id)
        except AlreadyRunning as e:
            logger.info(str(e))
            raise

        if 'ticket' in func.__code__.co_varnames:
            kwargs['ticket'] = ticket

        def _wrapper():
            try:
                func(*args, **kwargs)
            finally:
                self.finish_job(ticket)

        future = self.pool.submit(_wrapper)
        try:
            future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            ticket.is_cancelled = True
            logger.warning(f"Job {job_type} for {business_id} exceeded {timeout}s timeout and was cancelled.")
            # don't cancel future because it's running

    def run_background_with_timeout(self, job_type: str, business_id: Optional[str], timeout: float, func: Callable, *args, **kwargs):
        """Starts a job with timeout in the background without blocking."""
        try:
            ticket = self.start_job(job_type, business_id)
        except AlreadyRunning:
            raise
            
        def _bg_thread():
            try:
                if 'ticket' in func.__code__.co_varnames:
                    kwargs['ticket'] = ticket

                def _wrapper():
                    try:
                        func(*args, **kwargs)
                    except Exception as e:
                        logger.error(f"Error in {job_type}: {e}")

                future = self.pool.submit(_wrapper)
                try:
                    future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    ticket.is_cancelled = True
                    logger.warning(f"Job {job_type} exceeded {timeout}s timeout and was cancelled.")
            finally:
                self.finish_job(ticket)

        threading.Thread(target=_bg_thread, daemon=True).start()
