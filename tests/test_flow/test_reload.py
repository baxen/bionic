import pytest

import sys
from textwrap import dedent

from bionic.exception import CodeVersioningError


# Pytest rewrites modules and caches the compiled pyc file. The cache can
# interfere with the tests below. Hence, we disable the cache here.
@pytest.fixture(autouse=True)
def dont_write_bytecode():
    dont_write_bytecode = sys.dont_write_bytecode
    yield
    sys.dont_write_bytecode = dont_write_bytecode


@pytest.fixture
def tmp_module_path(tmp_path):
    tmp_dir_name = str(tmp_path)
    sys.path.insert(0, tmp_dir_name)

    sys.dont_write_bytecode = True

    yield tmp_path

    # Ensure that the temporary module is not loaded by a future test.
    sys.path.remove(tmp_dir_name)


@pytest.fixture
def write_flow_file(tmp_module_path):
    modules = set()

    def _write_flow_file(module_name, contents):
        (tmp_module_path / f"{module_name}.py").write_text(dedent(contents))
        modules.add(module_name)

    yield _write_flow_file

    # Invalidate caches
    for m in modules:
        sys.modules.pop(m, None)


@pytest.fixture
def write_flow_module(write_flow_file):
    def _write_flow_module(x):
        write_flow_file(
            "flow_module",
            f"""
                import bionic as bn

                builder = bn.FlowBuilder('flow')
                builder.assign('x', {x})

                @builder
                def x_doubled(x):
                    return x*2

                flow = builder.build()
            """,
        )

    return _write_flow_module


def verify_flow_contents(flow, results_map):
    assert {k: flow.get(k) for k in results_map.keys()} == results_map
    assert {k: getattr(flow.get, k)() for k in results_map.keys()} == results_map


@pytest.fixture(params=["reloading in place", "not reloading in place"])
def reloading_in_place(request):
    return request.param == "reloading in place"


# If we are not reloading in place, we want to check not only the reloaded flow
# but also the original flow to verify that the values have not changed.
@pytest.fixture
def check_flow(reloading_in_place):
    expected_results_by_flow = {}

    def _check_flow(flow, expected: dict, reload: bool):
        if reloading_in_place:
            if reload:
                flow.reload()
            verify_flow_contents(flow, expected)
        else:
            nonlocal expected_results_by_flow
            if reload:
                expected_results_by_flow[flow.reloading()] = expected
            else:
                expected_results_by_flow[flow] = expected
            for f, r in expected_results_by_flow.items():
                verify_flow_contents(f, r)

    return _check_flow


def test_no_change(write_flow_module, check_flow):
    write_flow_module(1)

    from flow_module import flow

    check_flow(flow, {"x": 1, "x_doubled": 2}, False)
    check_flow(flow, {"x": 1, "x_doubled": 2}, True)


def test_change_entity_value(write_flow_module, check_flow):
    write_flow_module(1)

    from flow_module import flow

    check_flow(flow, {"x": 1, "x_doubled": 2}, False)

    write_flow_module(2)
    check_flow(flow, {"x": 2, "x_doubled": 4}, True)


def test_change_entity_name(write_flow_file, check_flow):
    def write_flow(z: int):
        write_flow_file(
            "flow_module",
            f"""
                import bionic as bn

                builder = bn.FlowBuilder('flow')
                builder.assign('x', 1)

                @builder
                def x_plus_{z}(x):
                    return x + {z}

                flow = builder.build()
            """,
        )

    write_flow(1)

    from flow_module import flow

    check_flow(flow, {"x": 1, "x_plus_1": 2}, False)

    write_flow(2)
    check_flow(flow, {"x": 1, "x_plus_2": 3}, True)


def test_change_version(write_flow_file, check_flow):
    def write_flow(x: int, y: int):
        write_flow_file(
            "flow_module",
            f"""
                import bionic as bn

                builder = bn.FlowBuilder('flow')

                @builder
                @bn.version(major={x}, minor={y})
                def total():
                    return {x} + {y}

                flow = builder.build()
            """,
        )

    write_flow(10, 1)

    from flow_module import flow

    check_flow(flow, {"total": 11}, False)

    # Major version remains unchanged, cached value is returned
    write_flow(10, 2)
    check_flow(flow, {"total": 11}, True)

    # Major version changed, a new result is computed
    write_flow(20, 2)
    check_flow(flow, {"total": 22}, True)


def test_assist_versioning_mode(write_flow_file, check_flow):
    def write_flow(x: int):
        write_flow_file(
            "flow_module",
            f"""
                import bionic as bn

                builder = bn.FlowBuilder('flow')
                builder.set('core__versioning_mode', 'assist')

                @builder
                def total():
                    return {x}

                @builder
                def one():
                    return 1

                flow = builder.build()
            """,
        )

    write_flow(1)

    from flow_module import flow

    check_flow(flow, {"total": 1, "one": 1}, False)

    write_flow(1)
    check_flow(flow, {"total": 1, "one": 1}, True)

    write_flow(2)
    check_flow(flow, {"one": 1}, True)

    with pytest.raises(CodeVersioningError):
        flow.reloading().get("total")

    with pytest.raises(CodeVersioningError):
        flow.reload()
        flow.get("total")


# Test nondeterministic function marked with the changes_per_run decorator
def test_changes_per_run_decorator(write_flow_file):
    def write_flow():
        write_flow_file(
            "flow_module",
            """
                import bionic as bn
                from random import random

                builder = bn.FlowBuilder('flow')
                builder.assign('x', 1)

                @builder
                @bn.changes_per_run
                def r():
                    return random()

                flow = builder.build()
            """,
        )

    write_flow()

    from flow_module import flow

    r = flow.get("r")
    verify_flow_contents(flow, {"x": 1, "r": r})

    write_flow()
    assert flow.reloading().get("r") != r
    assert flow.get("r") == r
    flow.reload()
    assert flow.get("r") != r
    verify_flow_contents(flow, {"x": 1})
