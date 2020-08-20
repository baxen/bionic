import pytest

from bionic.code_references import get_code_context, get_referenced_objects
from bionic.flow import FlowBuilder
from bionic.utils.misc import oneline


global_val = 42


def get_references(func):
    context = get_code_context(func)
    return get_referenced_objects(func.__code__, context)


def test_empty_references():
    def x():
        pass

    assert get_references(x) == []

    def x():
        return 42

    assert get_references(x) == []

    def x(val="42"):
        return val

    assert get_references(x) == []


def test_global_references():
    def x():
        return global_val

    assert get_references(x) == [42]


def test_free_references():
    free_val = "42"

    def x():
        return free_val

    assert get_references(x) == ["42"]


def test_cell_references():
    def x():
        cell_val = "42"

        def y():
            return cell_val

    assert get_references(x) == ["cell_val"]


def test_import_references():
    def x():
        import pytest

        return pytest

    assert get_references(x) == [pytest]


def test_function_references():
    def x():
        return "42"

    def y():
        return x()

    assert get_references(y) == [x]

    def y():
        return oneline("Use a function in another module")

    assert get_references(y) == [oneline]

    def y():
        return func_does_not_exist()  # noqa: F821

    assert get_references(y) == ["func_does_not_exist"]


def test_method_references():
    class MyClass:
        def __init__(self):
            self.value = "42"

        def log_val(self):
            import logging

            logging.log(self.value)

    def x():
        my_class = MyClass()
        my_class.log_val()

    assert get_references(x) == [MyClass, "log_val"]

    def y(my_class):
        my_class.log_val()

    assert get_references(y) == ["log_val"]


def test_class_references():
    class MyClass:
        def __init__(self):
            self.value = "42"

        def log_val(self):
            import logging

            logging.log(self.value)

    def x():
        my_class = MyClass()
        return my_class

    assert get_references(x) == [MyClass]

    def x():
        builder = FlowBuilder()
        builder.assign("cls", MyClass)
        return builder

    assert get_references(x) == [FlowBuilder, "assign", MyClass]
