import pytest

import json
from pathlib import Path
from subprocess import check_call

import dask.dataframe as dd

from ..helpers import df_from_csv_str, equal_frame_and_index_content

import bionic as bn


@pytest.fixture(scope="function")
def preset_builder(builder):
    builder.assign("x", 2)
    builder.assign("y", 3)

    @builder
    def f(x, y):
        return x + y

    return builder


@pytest.fixture(scope="function")
def flow(preset_builder):
    return preset_builder.build()


@pytest.fixture(scope="function")
def expected_dask_df():
    df_value = df_from_csv_str(
        """
    color,number
    red,1
    blue,2
    green,3
    """
    )
    return dd.from_pandas(df_value, npartitions=1)


@pytest.fixture(scope="function")
def dask_flow(builder, expected_dask_df):
    @builder
    @bn.protocol.dask
    def dask_df():
        return expected_dask_df

    return builder.build()


def test_copy_file_to_existing_local_dir(flow, tmp_path):
    dir_path = tmp_path / "output"
    dir_path.mkdir()
    flow.get("f", mode="FileCopier").copy(destination=dir_path)

    expected_file_path = dir_path / "f.json"
    assert json.loads(expected_file_path.read_bytes()) == 5


def test_copy_file_to_local_file(flow, tmp_path):
    file_path = tmp_path / "data.json"
    flow.get("f", mode="FileCopier").copy(destination=file_path)

    assert json.loads(file_path.read_bytes()) == 5


def test_copy_file_to_local_file_using_str(flow, tmp_path):
    file_path = tmp_path / "data.json"
    file_path_str = str(file_path)
    flow.get("f", mode="FileCopier").copy(destination=file_path_str)
    assert json.loads(file_path.read_bytes()) == 5


@pytest.mark.needs_gcs
def test_copy_file_to_gcs_dir(flow, tmp_path, tmp_gcs_url_prefix):
    flow.get("f", mode="FileCopier").copy(destination=tmp_gcs_url_prefix)
    cloud_url = tmp_gcs_url_prefix + "f.json"
    local_path = tmp_path / "f.json"
    check_call(f"gsutil -m cp {cloud_url} {local_path}", shell=True)
    assert json.loads(local_path.read_bytes()) == 5


@pytest.mark.needs_gcs
def test_copy_file_to_gcs_file(flow, tmp_path, tmp_gcs_url_prefix):
    cloud_url = tmp_gcs_url_prefix + "f.json"
    flow.get("f", mode="FileCopier").copy(destination=cloud_url)
    local_path = tmp_path / "f.json"
    check_call(f"gsutil -m cp {cloud_url} {local_path}", shell=True)
    assert json.loads(local_path.read_bytes()) == 5


def test_copy_dask_to_dir(tmp_path, expected_dask_df, dask_flow):
    destination = tmp_path / "output"
    destination.mkdir()
    expected_dir_path = destination / "dask_df.pq.dask"

    dask_flow.get("dask_df", mode="FileCopier").copy(destination=destination)

    actual = dd.read_parquet(expected_dir_path)
    assert equal_frame_and_index_content(actual.compute(), expected_dask_df.compute())


@pytest.mark.needs_gcs
def test_copy_dask_to_gcs_dir(
    tmp_path, tmp_gcs_url_prefix, expected_dask_df, dask_flow
):
    cloud_url = tmp_gcs_url_prefix + "output"

    dask_flow.get("dask_df", mode="FileCopier").copy(destination=cloud_url)

    check_call(f"gsutil -m cp -r {cloud_url} {tmp_path}", shell=True)
    actual = dd.read_parquet(tmp_path / "output")
    assert equal_frame_and_index_content(actual.compute(), expected_dask_df.compute())


def test_get_multi_value_entity(builder):
    my_set = {"oscar", "the", "grouch"}
    builder.assign("val", values=my_set)

    @builder
    def multi_entity(val):
        return val

    flow = builder.build()
    results = flow.get("multi_entity", collection=set, mode=Path)
    results = {json.loads(res.read_bytes()) for res in results}

    assert results == my_set
