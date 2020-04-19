# Copyright 2014-2020 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Script which sends data for microbenchmarks which is generated by py.test benchmark plugin to
CodeSpeed instance.
"""

from __future__ import absolute_import

if False:
    from typing import List

import glob
import json
import argparse
import logging
from io import open

from scalyr_agent.util import rfc3339_to_datetime

from utils import add_common_parser_arguments
from utils import parse_auth_credentials
from utils import initialize_logging
from utils import send_payload_to_codespeed

LOG = logging.getLogger(__name__)


def parse_data_file(data_path):
    """
    Parse data file with benchmark run results.
    """
    with open(data_path, "r") as fp:
        content = fp.read()

    data = json.loads(content)
    return data


def seconds_to_ms(value):
    return value * 1000


def format_benchmark_data_for_codespeed(
    data, codespeed_project, codespeed_executable, codespeed_environment
):
    # type: (dict, str, str, str) -> List[dict]
    """
    Format benchmark data dictionary for CodeSpeed /add/result payload format.

    :param data: Raw data as exposed by pytest-benchmark.
    """
    commit_id = data["commit_info"]["id"]
    branch = data["commit_info"]["branch"]
    author_time = data["commit_info"]["author_time"]

    author_time = author_time.split("+")[0]

    revision_date = rfc3339_to_datetime(author_time).strftime("%Y-%m-%d %H:%M:%S")  # type: ignore

    payload = []

    for item in data["benchmarks"]:
        # We use median for the actual value
        value = item["stats"]["median"]
        value_min = item["stats"]["min"]
        value_max = item["stats"]["max"]
        value_stddev = item["stats"]["stddev"]

        benchmark = item["name"]
        submit_result_to_codespeed = item["options"].get(
            "submit_result_to_codespeed", False
        )

        if not submit_result_to_codespeed:
            continue

        # Convert all the input values to milliseconds
        # NOTE: Input values are in seconds
        value = seconds_to_ms(value)
        value_min = seconds_to_ms(value_min)
        value_max = seconds_to_ms(value_max)
        value_stddev = seconds_to_ms(value_stddev)

        item = {
            "commitid": commit_id,
            "revision_date": revision_date,
            "branch": branch,
            "project": codespeed_project,
            "executable": codespeed_executable,
            "benchmark": benchmark,
            "environment": codespeed_environment,
            "result_value": value,
            "min": value_min,
            "max": value_max,
            "std_dev": value_stddev,
        }
        payload.append(item)

    return payload


def main(
    data_path,
    codespeed_url,
    codespeed_auth,
    codespeed_project,
    codespeed_executable,
    codespeed_environment,
    dry_run=False,
):
    data_paths = glob.glob(data_path)

    for data_path in data_paths:
        LOG.info("Processing file: %s" % (data_path))
        data = parse_data_file(data_path)

        payload = format_benchmark_data_for_codespeed(
            data=data,
            codespeed_project=codespeed_project,
            codespeed_executable=codespeed_executable,
            codespeed_environment=codespeed_environment,
        )

        if not payload:
            continue

        commit_id = payload[0].get("commitid", "unknown")

        send_payload_to_codespeed(
            codespeed_url=codespeed_url,
            codespeed_auth=codespeed_auth,
            commit_id=commit_id,
            payload=payload,
            dry_run=dry_run,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Send microbenchmarks data to CodeSpeed")
    )

    # Add common arguments
    parser = add_common_parser_arguments(
        parser=parser,
        include_branch_arg=False,
        include_commit_id_arg=False,
        include_commit_date_arg=False,
        use_defaults_from_env_variables=True,
    )

    # Add arguments which are specific to this script
    parser.add_argument(
        "--data-path",
        type=str,
        required=True,
        help=("Path to the JSON file with benchmark result data."),
    )

    args = parser.parse_args()

    codespeed_auth = parse_auth_credentials(args.codespeed_auth)

    initialize_logging(debug=args.debug)
    main(
        data_path=args.data_path,
        codespeed_url=args.codespeed_url,
        codespeed_auth=codespeed_auth,
        codespeed_project=args.codespeed_project,
        codespeed_executable=args.codespeed_executable,
        codespeed_environment=args.codespeed_environment,
        dry_run=args.dry_run,
    )
