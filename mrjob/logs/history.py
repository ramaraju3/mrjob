# -*- coding: utf-8 -*-
# Copyright 2015 Yelp and Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Code for parsing the history file, which contains counters and error
messages for each task."""
import re
from logging import getLogger

from .counters import _sum_counters
from .ids import _add_implied_ids
from .wrap import _ls_logs
from .wrap import _cat_log


log = getLogger(__name__)


# what job history (e.g. counters) look like on either YARN or pre-YARN.
# YARN uses - instead of _ to separate fields. This should work for
# non-streaming jars as well.
_HISTORY_LOG_PATH_RE = re.compile(
    r'^(?P<prefix>.*?/)'
    r'(?P<job_id>job_\d+_\d{4})'
    r'[_-]\d+[_-]hadoop[_-](?P<suffix>\S*)$')

# escape sequence in pre-YARN history file. Characters inside COUNTERS
# fields are double escaped
_PRE_YARN_HISTORY_ESCAPE_RE = re.compile(r'\\(.)')

# capture key-value pairs like JOBNAME="streamjob8025762403845318969\.jar"
_PRE_YARN_HISTORY_KEY_PAIR = re.compile(
    r'(?P<key>\w+)="(?P<escaped_value>(\\.|[^"\\])*)"', re.MULTILINE)

# an entire line in a pre-YARN history file
_PRE_YARN_HISTORY_RECORD = re.compile(
    r'^(?P<type>\w+)'
    r'(?P<key_pairs>( ' + _PRE_YARN_HISTORY_KEY_PAIR.pattern + ')*)'
    r' \.$', re.MULTILINE)

# capture one group of counters
# this looks like: {(group_id)(group_name)[counter][counter]...}
_PRE_YARN_COUNTER_GROUP_RE = re.compile(
    r'{\('
    r'(?P<group_id>(\\.|[^)}\\])*)'
    r'\)\('
    r'(?P<group_name>(\\.|[^)}\\])*)'
    r'\)'
    r'(?P<counter_list_str>\[(\\.|[^}\\])*\])'
    r'}')

# parse a single counter from a counter group (counter_list_str above)
# this looks like: [(counter_id)(counter_name)(amount)]
_PRE_YARN_COUNTER_RE = re.compile(
    r'\[\('
    r'(?P<counter_id>(\\.|[^)\\])*)'
    r'\)\('
    r'(?P<counter_name>(\\.|[^)\\])*)'
    r'\)\('
    r'(?P<amount>\d+)'
    r'\)\]')




def _ls_history_logs(fs, log_dir_stream, job_id=None):
    """Yield matching files, optionally filtering by *job_id*. Yields dicts
    with the keys:

    job_id: job_id in path (must match *job_id* if set
    path: path/URI of log file
    yarn: true if this is a YARN log file

    *log_dir_stream* is a sequence of lists of log dirs. For each list, we'll
    look in all directories, and if we find any logs, we'll stop. (The
    assumption is that subsequent lists of log dirs would have copies
    of the same logs, just in a different location.
    """
    return _ls_logs(fs, log_dir_stream, _match_history_log,
                    job_id=job_id)


def _match_history_log(path, job_id=None):
    """Yield paths/uris of all job history files in the given directories,
    optionally filtering by *job_id*.
    """
    m = _HISTORY_LOG_PATH_RE.match(path)
    if not m:
        return None

    if not (job_id is None or m.group('job_id') == job_id):
        return None

    # TODO: couldn't manage to include .jhist in regex; an optional
    # group has less priority than a non-greedy match, apparently
    return dict(job_id=m.group('job_id'), yarn='.jhist' in m.group('suffix'))


def _interpret_history_log(fs, matches):
    """Extract counters and errors from history log.

    Matches is a list of dicts with the keys *job_id* and *yarn*
    (see :py:func:`_ls_history_logs()`)

    We expect *matches* to contain at most one match; further matches
    will be ignored.

    Returns a dictionary with the keys *counters* and *errors*.
    """
    for match in matches:
        if match['yarn']:
            return  # not yet implemented

        path = match['path']
        result = _parse_pre_yarn_history_log(_cat_log(fs, path))

        for error in result['errors']:
            # patch in path
            if 'hadoop_error' in error:
                error['hadoop_error']['path'] = path
            # patch in task_id, job_id
            _add_implied_ids(error)

        return result

    return dict(counters={}, errors=[])


def _parse_pre_yarn_history_log(lines):
    """Collect useful info from a pre-YARN history file. Expects a
    sequence of ``(record_type, {record})`` (see
    :py:func:`_parse_pre_yarn_history_records`).

    This returns a dictionary with the following keys:

    counters: map from group to counter to amount. If job failed, we sum
        counters for succesful tasks
    errors: a list of dictionaries with the keys:
        hadoop_error:
            error: lines of error, as as string
            start_line: first line of log containing the error (0-indexed)
            num_lines: # of lines of log containing the error
        task_attempt_id: ID of task attempt with this error
    """
    # tantalizingly, STATE_STRING contains the split (URI and line numbers)
    # read, but only for successful tasks, which doesn't help with debugging

    task_id_to_counters = {}  # used for successful tasks in failed jobs
    job_counters = None
    errors = []

    for record in _parse_pre_yarn_history_records(lines):
        fields = record['fields']

        # if job is successful, we get counters for the entire job at the end
        if record['type'] == 'Job' and 'COUNTERS' in fields:
            job_counters = _parse_pre_yarn_counters(fields['COUNTERS'])

        # otherwise, compile counters for each successful task
        #
        # Note: this apparently records a higher total than the task tracker
        # (possibly some tasks are duplicates?). Couldn't figure out the logic
        # behind this while looking at the history file
        elif (record['type'] == 'Task' and
              'COUNTERS' in fields and 'TASKID' in fields):
            task_id = fields['TASKID']
            counters = _parse_pre_yarn_counters(fields['COUNTERS'])

            task_id_to_counters[task_id] = counters

        elif (record['type'] in ('MapAttempt', 'ReduceAttempt') and
              'TASK_ATTEMPT_ID' in fields and 'ERROR' in fields):
            errors.append(dict(
                java_error=dict(
                    error=fields['ERROR'],
                    start_line=record['start_line'],
                    num_lines=record['num_lines']),
                task_attempt_id=fields['TASK_ATTEMPT_ID']))

    # if job failed, patch together counters from successful tasks
    if job_counters is None:
        job_counters = _sum_counters(*task_id_to_counters.values())

    return dict(counters=job_counters,
                errors=errors)


def _parse_pre_yarn_history_records(lines):
    """Yield records from the given sequence of lines. For example,
    a line like this:

    Task TASKID="task_201512311928_0001_m_000003" \
    TASK_TYPE="MAP" START_TIME="1451590341378" \
    SPLITS="/default-rack/172\.31\.22\.226" .

    into a record like:

    {
        'fields': {'TASKID': 'task_201512311928_0001_m_00000',
                   'TASK_TYPE': 'MAP',
                   'START_TIME': '1451590341378',
                   'SPLITS': '/default-rack/172.31.22.226'},
        'type': 'Task',
        'line_num': 0,
        'num_lines': 1,
    }

    This handles unescaping values, but doesn't do the further
    unescaping needed to process counters. It can also handle multi-line
    records (e.g. for Java stack traces).
    """
    def yield_record_strings(lines):
        record_lines = []
        start_line = 0

        for line_num, line in enumerate(lines):
            record_lines.append(line)
            if line.endswith(' .\n'):
                yield start_line, len(record_lines), ''.join(record_lines)
                record_lines = []
                start_line = line_num + 1

    for start_line, num_lines, record_str in yield_record_strings(lines):
        record_match = _PRE_YARN_HISTORY_RECORD.match(record_str)

        if not record_match:
            continue

        record_type = record_match.group('type')
        key_pairs = record_match.group('key_pairs')

        fields = {}
        for m in _PRE_YARN_HISTORY_KEY_PAIR.finditer(key_pairs):
            key = m.group('key')
            value = _pre_yarn_history_unescape(m.group('escaped_value'))

            fields[key] = value

        yield dict(
            fields=fields,
            num_lines=num_lines,
            start_line=start_line,
            type=record_type,
        )


def _parse_pre_yarn_counters(counters_str):
    """Parse a COUNTERS field from a pre-YARN history file.

    Returns a map from group to counter to amount.
    """
    counters = {}

    for group_match in _PRE_YARN_COUNTER_GROUP_RE.finditer(counters_str):
        group_name = _pre_yarn_history_unescape(
            group_match.group('group_name'))

        group_counters = {}

        for counter_match in _PRE_YARN_COUNTER_RE.finditer(
                group_match.group('counter_list_str')):

            counter_name = _pre_yarn_history_unescape(
                counter_match.group('counter_name'))
            amount = int(counter_match.group('amount'))

            group_counters[counter_name] = amount

        counters[group_name] = group_counters

    return counters


def _pre_yarn_history_unescape(s):
    """Un-escape string from a pre-YARN history file."""
    return _PRE_YARN_HISTORY_ESCAPE_RE.sub(r'\1', s)
