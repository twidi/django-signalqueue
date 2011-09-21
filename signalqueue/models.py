
from django.db import models
from datetime import datetime
from contextlib import contextmanager
from delegate import DelegateManager, DelegateQuerySet, delegate
from signalqueue.worker.base import QueueBase
from signalqueue.utils import logg


class SignalQuerySet(models.query.QuerySet):
    """
    SignalQuerySet is a QuerySet that works as a signalqueue backend.
    
    The actual QueueBase override methods are implemented here and delegated to
    SignalManager, which is a DelegateManager subclass with the QueueBase
    implementation "mixed in".
    
    Since you can't nakedly instantiate managers outside of a model
    class, we use a proxy class to hand off SignalQuerySet's delegated
    manager to the queue config stuff. See the working implementation in
    signalqueue.worker.backends.DatabaseQueueProxy for details.
    
    """
    @delegate
    def queued(self, enqueued=True):
        return self.filter(queue_name=self.queue_name, enqueued=enqueued).order_by("createdate")
    
    @delegate
    def ping(self):
        return True
    
    @delegate
    def push(self, value):
        logg.info("*** push() value: %s" % value)
        return self.get_or_create(queue_name=self.queue_name, value=value, enqueued=True)
    
    @delegate
    def pop(self):
        """ Dequeued signals are marked as such (but not deleted) by default. """
        out = self.queued()[0]
        out.enqueued = False
        out.save()
        return str(out.value)
    
    def count(self, enqueued=True):
        """ This override can't be delegated as the super() call isn't portable. """
        return super(self.__class__, self.all().queued(enqueued=enqueued)).count()
    
    @delegate
    def clear(self):
        self.queued().update(enqueued=False)
    
    @delegate
    def values(self, floor=0, ceil=-1):
        if floor < 1:
            floor = 0
        if ceil < 1:
            ceil = self.count()
        
        out = self.queued()[floor:ceil]
        return [str(value[0]) for value in out.values_list('value')]

class SignalManager(DelegateManager, QueueBase):
    __queryset__ = SignalQuerySet
    
    def __init__(self, *args, **kwargs):
        DelegateManager.__init__(self, *args, **kwargs)
        QueueBase.__init__(self, *args, **kwargs)
    
    def count(self, enqueued=True):
        return self.queued(enqueued=enqueued).count()
    
    def _get_queue_name(self):
        if self._queue_name:
            return self._queue_name
        return None
    
    def _set_queue_name(self, queue_name):
        self._queue_name = queue_name
        self.__queryset__.queue_name = queue_name
    
    queue_name = property(_get_queue_name, _set_queue_name)

class EnqueuedSignal(models.Model):
    class Meta:
        abstract = False
        verbose_name = "Enqueued Signal"
        verbose_name_plural = "Enqueued Signals"
    
    objects = SignalManager()
    
    createdate = models.DateTimeField("Created on",
        default=datetime.now,
        blank=True,
        null=True,
        editable=False)
    
    enqueued = models.BooleanField("Enqueued",
        default=True,
        editable=True)
    
    queue_name = models.CharField(verbose_name="Queue Name",
        max_length=255, db_index=True,
        default="default",
        unique=False,
        blank=True,
        null=False)
    
    value = models.TextField(verbose_name="Serialized Signal Value",
        editable=False,
        unique=True, db_index=True,
        blank=True,
        null=True)
    
    def __repr__(self):
        if self.value:
            return str(self.value)
        return "{'instance':null}"
    
    def __unicode__(self):
        if self.value:
            return unicode(self.value)
        return u"{'instance':null}"

class WorkerExceptionLogQuerySet(models.query.QuerySet):
    
    @delegate
    def unviewed(self):
        return self.filter(viewed=False)
    
    @delegate
    def viewed(self):
        return self.filter(viewed=True)
    
    @delegate
    def bycount(self):
        return self.order_by('count')
    
    @delegate
    def byqueue(self):
        return self.order_by('queue_name')
    
    @delegate
    def byclass(self):
        return self.order_by('exception_class')
    
    @delegate
    def fromqueue(self, queue_name="default"):
        return self.filter(queue_name__iexact=queue_name)
    
    @delegate
    def withtype(self, exception_type):
        return self.filter(exception_type__iexact=exception_type)
    
    @delegate
    def withclass(self, *args, **kwargs):
        return self.withtype(*args, **kwargs)
    
    @delegate
    def withmodule(self, exception_module):
        return self.filter(exception_module__iexact=exception_module)
    
    @delegate
    def like(self, exception):
        if isinstance(exception, Exception):
            return self.withtype(exception.__class__.__name__).withmodule(exception.__class__.__module__)
        #elif issubclass(exception, Exception):
        elif type(exception) == type(type):
            return self.withtype(exception.__name__).withmodule(exception.__module__)
        return self.none()
    
    @delegate
    def totalcount(self):
        return self.aggregate(totalcount=models.Sum('count'))['totalcount']


class WorkerExceptionLogManager(DelegateManager):
    __queryset__ = WorkerExceptionLogQuerySet
    _exceptions = dict()
    
    def _make_key(self, key_type, key_value):
        return str("%s.%s:%s" % (
            getattr(key_type, '__module__', "None"),
            key_type.__name__,
            str(key_value),
        ))
    
    def log_exception(self, exception, queue_name='default'):
        import sys
        
        try:
            raise exception
        except:
            exc_type, exc_value, tb = sys.exc_info()
        
        key = self._make_key(exc_type, str(exception))
        exc_log_entry = None
        
        if key not in self._exceptions:
            from django.views.debug import ExceptionReporter
            
            # first arg  to ExceptionReporter.__init__() is usually a request object
            reporter = ExceptionReporter(None, exc_type, exc_value, tb)
            html = reporter.get_traceback_html()
            
            exc_log_entry = self.create(
                exception_type=exc_type.__name__,
                exception_module=getattr(exc_type, '__module__', "None"),
                queue_name=queue_name,
                message=str(exception),
                html=html,
            )
            self._exceptions.update({ key: exc_log_entry, })
        
        else:
            exc_log_entry = self._exceptions[key]
            exc_log_entry.increment()
            exc_log_entry.save()
        
        return exc_log_entry
    
    @contextmanager
    def log(self, queue_name="default", exc_type=Exception):
        """
        Context manager for logging exceptions. Use like so:
        
        from signalqueue.models import log_exceptions
        
        with log_exceptions(queue_name="my_queue", exc_type=ValueError):
            something_that_might_throw_an_exception()
        
        """
        try:
            yield
        except exc_type, exc:
            self.log_exception(exc, queue_name)

class WorkerExceptionLog(models.Model):
    class Meta:
        abstract = False
        verbose_name = "Worker Exception Log Entry"
        verbose_name_plural = "Worker Exception Logs"
    
    objects = WorkerExceptionLogManager()
    
    viewed = models.BooleanField("Viewed",
        default=False,
        blank=True,
        null=False,
        editable=False)
    
    createdate = models.DateTimeField("Created on",
        default=datetime.now,
        blank=True,
        null=True,
        editable=False)
    
    count = models.IntegerField("Count",
        default=1,
        blank=True,
        null=False,
        editable=False)
    
    exception_type = models.CharField(verbose_name="Exception Type",
        max_length=255, db_index=True,
        editable=False,
        unique=False,
        blank=True,
        null=False)
    
    exception_module = models.CharField(verbose_name="Exception Module",
        max_length=255, db_index=True,
        editable=False,
        unique=False,
        blank=True,
        null=False)
    
    queue_name = models.CharField(verbose_name="Queue Name",
        max_length=255, db_index=True,
        default='default',
        unique=False,
        blank=True,
        null=False)
    
    message = models.TextField(verbose_name="Exception Message",
        editable=False,
        unique=False,
        blank=True,
        null=False)
    
    html = models.TextField(verbose_name="Exception HTML",
        editable=False,
        unique=False,
        blank=True,
        null=False)
    
    def increment(self):
        self.count = self.count + 1
    
    def __unicode__(self):
        return "<Log:%s.%s queue:%s cnt:%s ('%s')>" % (
            self.exception_module,
            self.exception_type,
            self.queue_name,
            self.count,
            self.message,
        )
    
    def __str__(self):
        return self.__unicode__()
    
    def __repr__(self):
        return self.__unicode__()

log_exceptions = WorkerExceptionLog.objects.log

