import pandas
import numpy as np
import scipy
import statsmodels.api as sm
import traceback
import logging
import re
from time import time
from msgpack import unpackb, packb
from redis import StrictRedis

from settings import (
    FULL_DURATION,
    MAX_TOLERABLE_BOREDOM,
    MIN_TOLERABLE_LENGTH,
    STALE_PERIOD,
    REDIS_SOCKET_PATH,
    BOREDOM_SET_SIZE,
    ENABLE_BOUNDARY_DEBUG,
)

from algorithm_exceptions import *

logger = logging.getLogger("BoundaryLog")
redis_conn = StrictRedis(unix_socket_path=REDIS_SOCKET_PATH)

"""
This is no man's land. Do anything you want in here,
as long as you return a boolean that determines whether the input
timeseries is anomalous or not.

To add an algorithm, define it here, and add its name to settings.ALGORITHMS.
"""


def tail_avg(timeseries):
    """
    This is a utility function used to calculate the average of the last three
    datapoints in the series as a measure, instead of just the last datapoint.
    It reduces noise, but it also reduces sensitivity and increases the delay
    to detection.
    """
    try:
        t = (timeseries[-1][1] + timeseries[-2][1] + timeseries[-3][1]) / 3
        return t
    except IndexError:
        return timeseries[-1][1]


def autoaggregate_ts(timeseries, autoaggregate_value):
    """
    This is a utility function used to autoaggregate a timeseries.  If a
    timeseries data set has 6 datapoints per minute but only one data value
    every minute then autoaggregate will aggregate every autoaggregate_value.
    """
    if ENABLE_BOUNDARY_DEBUG:
        logger.info("debug - autoaggregate_ts at %s seconds" % str(autoaggregate_value))

    aggregated_timeseries = []

    if len(timeseries) < 60:
        if ENABLE_BOUNDARY_DEBUG:
            logger.info("debug - autoaggregate_ts - timeseries less than 60 datapoints, TooShort")
        raise TooShort()

    int_end_timestamp = int(timeseries[-1][0])
    last_hour = int_end_timestamp - 3600
    last_timestamp = int_end_timestamp
    next_timestamp = last_timestamp - int(autoaggregate_value)
    start_timestamp = last_hour

    if ENABLE_BOUNDARY_DEBUG:
        logger.info("debug - autoaggregate_ts - aggregating from %s to %s" % (str(start_timestamp), str(int_end_timestamp)))

    valid_timestamps = False
    try:
        valid_timeseries = int_end_timestamp - start_timestamp
        if valid_timeseries == 3600:
            valid_timestamps = True
    except Exception as e:
        logger.error("Algorithm error: " + traceback.format_exc())
        logger.error("error: %e" % e)
        aggregated_timeseries = []
        return aggregated_timeseries

    if valid_timestamps:
        try:
            # Check sane variables otherwise we can just hang here in a while loop
            while int(next_timestamp) > int(start_timestamp):
                value = np.sum(scipy.array([int(x[1]) for x in timeseries if x[0] <= last_timestamp and x[0] > next_timestamp]))
                aggregated_timeseries += ((last_timestamp, value),)
                last_timestamp = next_timestamp
                next_timestamp = last_timestamp - autoaggregate_value
            aggregated_timeseries.reverse()
            return aggregated_timeseries
        except Exception as e:
            logger.error("Algorithm error: " + traceback.format_exc())
            logger.error("error: %e" % e)
            aggregated_timeseries = []
            return aggregated_timeseries
    else:
        logger.error("could not aggregate - timestamps not valid for aggregation")
        aggregated_timeseries = []
        return aggregated_timeseries


def less_than(timeseries, metric_name, metric_expiration_time, metric_min_average, metric_min_average_seconds, metric_trigger):
        # timeseries, metric_name, metric_expiration_time, metric_min_average,
        # metric_min_average_seconds, metric_trigger, autoaggregate,
        # autoaggregate_value):
    """
    A timeseries is anomalous if the datapoint is less than metric_trigger
    """
    if len(timeseries) < 10:
        return False

    if timeseries[-1][1] < metric_trigger:
        if ENABLE_BOUNDARY_DEBUG:
            logger.info("debug - less_than - " + str(timeseries[-1][1]) + " less than " + str(metric_trigger))
        return True

    return False


def greater_than(timeseries, metric_name, metric_expiration_time, metric_min_average, metric_min_average_seconds, metric_trigger):
    """
    A timeseries is anomalous if the datapoint is greater than metric_trigger
    """

    if len(timeseries) < 10:
        return False

    if timeseries[-1][1] > metric_trigger:
        if ENABLE_BOUNDARY_DEBUG:
            logger.info("debug - grater_than - " + str(timeseries[-1][1]) + " greater than " + str(metric_trigger))
        return True

    return False


def detect_drop_off_cliff(timeseries, metric_name, metric_expiration_time, metric_min_average, metric_min_average_seconds, metric_trigger):
    """
    A timeseries is anomalous if the average of the last 10 datapoints is
    <trigger> times greater than the last data point AND if has not experienced
    frequent cliff drops in the last 10 datapoints.  If the timeseries has
    experienced 2 or more datapoints of equal or less values in the last 10 or
    EXPIRATION_TIME datapoints or is less than a MIN_AVERAGE if set the
    algorithm determines the datapoint as NOT anomalous but normal.
    This algorithm is most suited to timeseries with most datapoints being > 100
    (e.g high rate).  The arbitrary <trigger> values become more noisy with
    lower value datapoints, but it still matches drops off cliffs.
    EXPERIMENTAL
    """

    if len(timeseries) < 30:
        return False

    int_end_timestamp = int(timeseries[-1][0])
    # Determine resolution of the data set
    int_second_last_end_timestamp = int(timeseries[-2][0])
    resolution = int_end_timestamp - int_second_last_end_timestamp
    ten_data_point_seconds = resolution * 10
    ten_datapoints_ago = int_end_timestamp - ten_data_point_seconds

    ten_datapoint_array = scipy.array([x[1] for x in timeseries if x[0] <= int_end_timestamp and x[0] > ten_datapoints_ago])
    ten_datapoint_array_len = len(ten_datapoint_array)

    if ten_datapoint_array_len > 3:

        ten_datapoint_min_value = np.amin(ten_datapoint_array)

        # DO NOT handle if negative integers are in the range, where is the
        # bottom of the cliff if a range goes negative?  Testing with a noisy
        # sine wave timeseries that had a drop off cliff introduced to the
        # postive data side, proved that this algorithm does work on timeseries
        # with data values in the negative range
        if ten_datapoint_min_value < 0:
            return False

        # autocorrect if there are there are 0s in the data, like graphite expects
        # 1 datapoint every 10 seconds, but the timeseries only has 1 every 60 seconds

        ten_datapoint_max_value = np.amax(ten_datapoint_array)

        # The algorithm should have already fired in 10 datapoints if the
        # timeseries dropped off a cliff, these are all zero
        if ten_datapoint_max_value == 0:
            return False

        # If the lowest is equal to the highest, no drop off cliff
        if ten_datapoint_min_value == ten_datapoint_max_value:
            return False

#        if ten_datapoint_max_value < 10:
#            return False

        ten_datapoint_array_sum = np.sum(ten_datapoint_array)
        ten_datapoint_value = int(ten_datapoint_array[-1])
        ten_datapoint_average = ten_datapoint_array_sum / ten_datapoint_array_len
        ten_datapoint_value = int(ten_datapoint_array[-1])

        # if a metric goes up and down a lot and falls off a cliff frequently
        # it is normal, not anomalous
        number_of_similar_datapoints = len(np.where(ten_datapoint_array <= ten_datapoint_min_value))

        # Detect once only - to make this useful and not noisy the first one
        # would have already fired and detected the drop
        if number_of_similar_datapoints > 2:
            return False

        # evaluate against 20 datapoints as well, reduces chatter on peaky ones
        # tested with 60 as well and 20 is sufficient to filter noise
        twenty_data_point_seconds = resolution * 20
        twenty_datapoints_ago = int_end_timestamp - twenty_data_point_seconds
        twenty_datapoint_array = scipy.array([x[1] for x in timeseries if x[0] <= int_end_timestamp and x[0] > twenty_datapoints_ago])
        number_of_similar_datapoints_in_twenty = len(np.where(twenty_datapoint_array <= ten_datapoint_min_value))
        if number_of_similar_datapoints_in_twenty > 2:
            return False

        # Check if there is a similar data point in EXPIRATION_TIME
        # Disabled as redis alert cache will filter on this
#        if metric_expiration_time > twenty_data_point_seconds:
#            expiration_time_data_point_seconds = metric_expiration_time
#            expiration_time_datapoints_ago = int_end_timestamp - metric_expiration_time
#            expiration_time_datapoint_array = scipy.array([x[1] for x in timeseries if x[0] <= int_end_timestamp and x[0] > expiration_time_datapoints_ago])
#            number_of_similar_datapoints_in_expiration_time = len(np.where(expiration_time_datapoint_array <= ten_datapoint_min_value))
#            if number_of_similar_datapoints_in_expiration_time > 2:
#                return False

        if metric_min_average > 0 and metric_min_average_seconds > 0:
            min_average = metric_min_average
            min_average_seconds = metric_min_average_seconds
            min_average_data_point_seconds = resolution * min_average_seconds
#            min_average_datapoints_ago = int_end_timestamp - (resolution * min_average_seconds)
            min_average_datapoints_ago = int_end_timestamp - min_average_seconds
            min_average_array = scipy.array([x[1] for x in timeseries if x[0] <= int_end_timestamp and x[0] > min_average_datapoints_ago])
            min_average_array_average = np.sum(min_average_array) / len(min_average_array)
            if min_average_array_average < min_average:
                return False

        if ten_datapoint_max_value < 101:
            trigger = 15
        if ten_datapoint_max_value < 20:
            trigger = ten_datapoint_average / 2
        if ten_datapoint_max_value > 100:
            trigger = 100
        if ten_datapoint_value == 0:
            # Cannot divide by 0, so set to 0.1 to prevent error
            ten_datapoint_value = 0.1
        if ten_datapoint_value == 1:
            trigger = 1
        if ten_datapoint_value == 1 and ten_datapoint_max_value < 10:
            trigger = 0.1
        if ten_datapoint_value == 0.1 and ten_datapoint_average < 1 and ten_datapoint_array_sum < 7:
            trigger = 7

        ten_datapoint_result = ten_datapoint_average / ten_datapoint_value
        if int(ten_datapoint_result) > trigger:
            if ENABLE_BOUNDARY_DEBUG:
                logger.info(
                    "detect_drop_off_cliff - %s, ten_datapoint_value = %s, ten_datapoint_array_sum = %s, ten_datapoint_average = %s, trigger = %s, ten_datapoint_result = %s" % (
                        str(int_end_timestamp),
                        str(ten_datapoint_value),
                        str(ten_datapoint_array_sum),
                        str(ten_datapoint_average),
                        str(trigger), str(ten_datapoint_result)))
            return True

    return False


def run_selected_algorithm(
        timeseries, metric_name, metric_expiration_time, metric_min_average,
        metric_min_average_seconds, metric_trigger, alert_threshold,
        metric_alerters, autoaggregate, autoaggregate_value, algorithm):
    """
    Filter timeseries and run selected algorithm.
    """

    if ENABLE_BOUNDARY_DEBUG:
        logger.info(
            'debug - assigning in algoritms.py - %s, %s' % (
                metric_name, algorithm))

    # Get rid of short series
    if len(timeseries) < MIN_TOLERABLE_LENGTH:
        if ENABLE_BOUNDARY_DEBUG:
            logger.info('debug - TooShort - %s, %s' % (metric_name, algorithm))
        raise TooShort()

    # Get rid of stale series
    if time() - timeseries[-1][0] > STALE_PERIOD:
        if ENABLE_BOUNDARY_DEBUG:
            logger.info('debug - Stale - %s, %s' % (metric_name, algorithm))
        raise Stale()

    # Get rid of boring series
    if len(set(item[1] for item in timeseries[-MAX_TOLERABLE_BOREDOM:])) == BOREDOM_SET_SIZE:
        if ENABLE_BOUNDARY_DEBUG:
            logger.info('debug - Boring - %s, %s' % (metric_name, algorithm))
        raise Boring()

    if autoaggregate:
        if ENABLE_BOUNDARY_DEBUG:
            logger.info("debug - auto aggregating " + metric_name + " for " + algorithm)
        try:
            agg_timeseries = autoaggregate_ts(timeseries, autoaggregate_value)
            aggregatation_failed = False
            if ENABLE_BOUNDARY_DEBUG:
                logger.info("debug - aggregated_timeseries returned " + metric_name + " for " + algorithm)
        except Exception as e:
            logger.error("Algorithm error: " + traceback.format_exc())
            logger.error("error: %e" % s)
            if ENABLE_BOUNDARY_DEBUG:
                logger.info("debug error - autoaggregate excpection " + metric_name + " for " + algorithm)

        if len(agg_timeseries) > 10:
            timeseries = agg_timeseries
        else:
            timeseries = agg_timeseries
            if ENABLE_BOUNDARY_DEBUG:
                logger.info("debug - auto aggregation failed for " + metric_name + " with resultant timeseries being length of " + str(len(agg_timeseries)))

    if len(timeseries) < 10:
        if ENABLE_BOUNDARY_DEBUG:
            logger.info("debug - timeseries too short - " + metric_name + " - timeseries length - " + str(len(timeseries)))
        return False, [], 1, metric_name, metric_expiration_time, metric_min_average, metric_min_average_seconds, metric_trigger, alert_threshold, metric_alerters, algorithm

    try:
        ensemble = [globals()[algorithm](timeseries, metric_name, metric_expiration_time, metric_min_average, metric_min_average_seconds, metric_trigger)]
        if ensemble.count(True) == 1:
            if ENABLE_BOUNDARY_DEBUG:
                logger.info(
                    'debug - anomalous datapoint = %s - %s, %s, %s, %s, %s, %s, %s, %s' % (
                        str(timeseries[-1][1]),
                        str(metric_name), str(metric_expiration_time),
                        str(metric_min_average),
                        str(metric_min_average_seconds),
                        str(metric_trigger), str(alert_threshold),
                        str(metric_alerters), str(algorithm))
                )
            return True, ensemble, timeseries[-1][1], metric_name, metric_expiration_time, metric_min_average, metric_min_average_seconds, metric_trigger, alert_threshold, metric_alerters, algorithm
        else:
            if ENABLE_BOUNDARY_DEBUG:
                logger.info(
                    'debug - not anomalous datapoint = %s - %s, %s, %s, %s, %s, %s, %s, %s' % (
                        str(timeseries[-1][1]),
                        str(metric_name), str(metric_expiration_time),
                        str(metric_min_average),
                        str(metric_min_average_seconds),
                        str(metric_trigger), str(alert_threshold),
                        str(metric_alerters), str(algorithm))
                )
            return False, ensemble, timeseries[-1][1], metric_name, metric_expiration_time, metric_min_average, metric_min_average_seconds, metric_trigger, alert_threshold, metric_alerters, algorithm
    except:
        logger.error("Algorithm error: " + traceback.format_exc())
        return False, [], 1, metric_name, metric_expiration_time, metric_min_average, metric_min_average_seconds, metric_trigger, alert_threshold, metric_alerters, algorithm
