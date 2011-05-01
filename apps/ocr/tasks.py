from datetime import timedelta, datetime
import platform

from django.db.models import Q
from django.utils.translation import ugettext as _
from django.core.cache.backends.locmem import CacheClass

from celery.decorators import task, periodic_task
from celery.task.control import inspect

from ocr.api import do_document_ocr
from ocr.literals import QUEUEDOCUMENT_STATE_PENDING, \
    QUEUEDOCUMENT_STATE_PROCESSING, DOCUMENTQUEUE_STATE_ACTIVE, \
    QUEUEDOCUMENT_STATE_ERROR
from ocr.models import QueueDocument, DocumentQueue
from ocr.conf.settings import NODE_CONCURRENT_EXECUTION
from ocr.conf.settings import REPLICATION_DELAY
from ocr.conf.settings import QUEUE_PROCESSING_INTERVAL

LOCK_EXPIRE = 60 * 5 # Lock expires in 5 minutes
local_cache = CacheClass([], {})

@task
def task_process_queue_document(queue_document_id):
    queue_document = QueueDocument.objects.get(id=queue_document_id)
    queue_document.state = QUEUEDOCUMENT_STATE_PROCESSING
    queue_document.node_name = platform.node()
    queue_document.result = task_process_queue_document.request.id
    queue_document.save()
    try:
        do_document_ocr(queue_document.document)
        queue_document.delete()
    except Exception, e:
        queue_document.state = QUEUEDOCUMENT_STATE_ERROR
        queue_document.result = e
        queue_document.save()


def reset_orphans():
    i = inspect().active()
    active_tasks = []
    orphans = []
    
    if i:
        for host, instances in i.items():
            for instance in instances:
                active_tasks.append(instance['id'])

    for document_queue in DocumentQueue.objects.filter(state=DOCUMENTQUEUE_STATE_ACTIVE):
        orphans = document_queue.queuedocument_set.\
            filter(state=QUEUEDOCUMENT_STATE_PROCESSING).\
            exclude(result__in=active_tasks)

    for orphan in orphans:
        orphan.result = _(u'Orphaned')
        orphan.state = QUEUEDOCUMENT_STATE_PENDING
        orphan.delay = False
        orphan.node_name = None
        orphan.save()


@periodic_task(run_every=timedelta(seconds=QUEUE_PROCESSING_INTERVAL))
def task_process_document_queues():
    lock_id = '%s-lock-%s' % ('task_process_document_queues', platform.node())
    acquire_lock = lambda: local_cache.add(lock_id, 'true', LOCK_EXPIRE)
    release_lock = lambda: local_cache.delete(lock_id)

    if acquire_lock():
        reset_orphans()
        q_pending = Q(state=QUEUEDOCUMENT_STATE_PENDING)
        q_delayed = Q(delay=True)
        q_delay_interval = Q(datetime_submitted__lt=datetime.now() - timedelta(seconds=REPLICATION_DELAY))
        for document_queue in DocumentQueue.objects.filter(state=DOCUMENTQUEUE_STATE_ACTIVE):
            if QueueDocument.objects.filter(
                state=QUEUEDOCUMENT_STATE_PROCESSING).filter(
                node_name=platform.node()).count() < NODE_CONCURRENT_EXECUTION:
                try:
                    oldest_queued_document_qs = document_queue.queuedocument_set.filter(
                        (q_pending & ~q_delayed) | (q_pending & q_delayed & q_delay_interval))

                    if oldest_queued_document_qs:
                        oldest_queued_document = oldest_queued_document_qs.order_by('datetime_submitted')[0]
                        task_process_queue_document.delay(oldest_queued_document.id)
                except Exception, e:
                    print 'DocumentQueueWatcher exception: %s' % e
        release_lock()