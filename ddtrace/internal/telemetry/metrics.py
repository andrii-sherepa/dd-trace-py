# -*- coding: utf-8 -*-
import abc
import time
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Text

import six

from ddtrace.internal.telemetry.constants import TELEMETRY_METRIC_TYPE_COUNT
from ddtrace.internal.telemetry.constants import TELEMETRY_METRIC_TYPE_DISTRIBUTIONS
from ddtrace.internal.telemetry.constants import TELEMETRY_METRIC_TYPE_GAUGE
from ddtrace.internal.telemetry.constants import TELEMETRY_METRIC_TYPE_RATE


MetricType = Text
MetricTagType = Dict[str, Any]


class Metric(six.with_metaclass(abc.ABCMeta)):
    """
    Telemetry Metrics are stored in DD dashboards, check the metrics in datadoghq.com/metric/explorer
    """

    metric_type = ""
    __slots__ = ["namespace", "name", "_tags", "is_common_to_all_tracers", "interval", "_points", "_count"]

    def __init__(self, namespace, name, tags, common, interval=None):
        # type: (str, str, MetricTagType, bool, Optional[float]) -> None
        """
        namespace: the scope of the metric: tracer, appsec, etc.
        name: string
        tags: extra information attached to a metric
        common: set to True if a metric is common to all tracers, false if it is python specific
        interval: field set for gauge and rate metrics, any field set is ignored for count metrics (in secs)
        """
        self.name = name.lower()
        self.is_common_to_all_tracers = common
        self.interval = interval
        self.namespace = namespace.lower()
        self._tags = {k.lower(): str(v).lower() for k, v in tags.items()}
        self._count = 0.0
        self._points = []  # type: List

    @classmethod
    def get_id(cls, name, namespace, tags, metric_type):
        # type: (str, str, Dict[str, Any], str) -> str
        """
        https://www.datadoghq.com/blog/the-power-of-tagged-metrics/#whats-a-metric-tag
        """
        str_tags = str(sorted(tags.items())) if tags else ""
        return ("%s-%s-%s-%s" % (name, namespace, str_tags, metric_type)).lower()

    def __hash__(self):
        return hash(self.get_id(self.name, self.namespace, self._tags, self.metric_type))

    @abc.abstractmethod
    def add_point(self, value=1.0):
        # type: (float) -> None
        """adds timestamped data point associated with a metric"""
        pass

    def to_dict(self):
        # type: () -> Dict
        """returns a dictionary containing the metrics fields expected by the telemetry intake service"""
        data = {
            "metric": self.name,
            "type": self.metric_type,
            "common": self.is_common_to_all_tracers,
            "points": self._points,
            "tags": ["%s:%s" % (k, v) for k, v in self._tags.items()],
        }
        if self.interval is not None:
            data["interval"] = int(self.interval)
        return data


class CountMetric(Metric):
    """
    A count type adds up all the submitted values in a time interval. This would be suitable for a
    metric tracking the number of website hits, for instance.
    """

    metric_type = TELEMETRY_METRIC_TYPE_COUNT

    def add_point(self, value=1.0):
        # type: (float) -> None
        """adds timestamped data point associated with a metric"""
        if self._points:
            self._points[0][1] += value
        else:
            self._points = [[time.time(), value]]


class GaugeMetric(Metric):
    """
    A gauge type takes the last value reported during the interval. This type would make sense for tracking RAM or
    CPU usage, where taking the last value provides a representative picture of the host’s behavior during the time
    interval. In this case, using a different type such as count would probably lead to inaccurate and extreme values.
    Choosing the correct metric type ensures accurate data.
    """

    metric_type = TELEMETRY_METRIC_TYPE_GAUGE

    def add_point(self, value=1.0):
        # type: (float) -> None
        """adds timestamped data point associated with a metric"""
        self._points = [(time.time(), value)]


class RateMetric(Metric):
    """
    The rate type takes the count and divides it by the length of the time interval. This is useful if you’re
    interested in the number of hits per second.
    """

    metric_type = TELEMETRY_METRIC_TYPE_RATE

    def add_point(self, value=1.0):
        # type: (float) -> None
        """Example:
        https://github.com/DataDog/datadogpy/blob/ee5ac16744407dcbd7a3640ee7b4456536460065/datadog/threadstats/metrics.py#L181
        """
        self._count += value
        rate = (self._count / self.interval) if self.interval else 0.0
        self._points = [(time.time(), rate)]


class DistributionMetric(Metric):
    """
    The rate type takes the count and divides it by the length of the time interval. This is useful if you’re
    interested in the number of hits per second.
    """

    metric_type = TELEMETRY_METRIC_TYPE_DISTRIBUTIONS

    def add_point(self, value=1.0):
        # type: (float) -> None
        """Example:
        https://github.com/DataDog/datadogpy/blob/ee5ac16744407dcbd7a3640ee7b4456536460065/datadog/threadstats/metrics.py#L181
        """
        self._points.append(value)

    def to_dict(self):
        # type: () -> Dict
        """returns a dictionary containing the metrics fields expected by the telemetry intake service"""
        data = {
            "metric": self.name,
            "points": self._points,
            "tags": ["%s:%s" % (k, v) for k, v in self._tags.items()],
        }
        return data
