# Copyright 2018-2020 Streamlit Inc.
# Author: Dominik Moritz

"""
Contains the logic to find all the referenced objects for a code object.
"""

import attr
import dis
import importlib
import inspect
import warnings

from .utils.misc import oneline


@attr.s(frozen=True)
class CodeContext:
    globals = attr.ib()
    cells = attr.ib()
    varnames = attr.ib()


def get_code_context(func) -> CodeContext:
    code = func.__code__

    # Mapping from variable name to the value if we can resolve it.
    # Otherwise map to the name.
    cells = {}

    for var in code.co_cellvars:
        cells[var] = var  # Instead of value, we use the name.

    if code.co_freevars:
        assert len(code.co_freevars) == len(func.__closure__)
        cells.update(
            zip(code.co_freevars, map(lambda c: c.cell_contents, func.__closure__))
        )

    varnames = {}
    if inspect.ismethod(func):
        varnames = {"self": func.__self__}

    return CodeContext(globals=func.__globals__, cells=cells, varnames=varnames)


def get_referenced_objects(code, context):
    # Top of the stack
    tos = None
    lineno = None
    refs = []

    def set_tos(t):
        nonlocal tos
        if tos is not None:
            # Hash tos so we support reading multiple objects
            refs.append(tos)
        tos = t

    # Our goal is to find referenced objects. The problem is that co_names
    # does not have full qualified names in it. So if you access `foo.bar`,
    # co_names has `foo` and `bar` in it but it doesn't tell us that the
    # code reads `bar` of `foo`. We are going over the bytecode to resolve
    # from which object an attribute is requested.
    # Read more about bytecode at https://docs.python.org/3/library/dis.html

    for op in dis.get_instructions(code):
        try:
            # Sometimes starts_line is None, in which case let's just remember the
            # previous start_line (if any). This way when there's an exception we at
            # least can point users somewhat near the line where the error stems from.
            if op.starts_line is not None:
                lineno = op.starts_line

            if op.opname in ["LOAD_GLOBAL", "LOAD_NAME"]:
                if op.argval in context.globals:
                    set_tos(context.globals[op.argval])
                else:
                    set_tos(op.argval)
            elif op.opname in ["LOAD_DEREF", "LOAD_CLOSURE"]:
                set_tos(context.cells[op.argval])
            elif op.opname == "IMPORT_NAME":
                try:
                    set_tos(importlib.import_module(op.argval))
                except ImportError:
                    set_tos(op.argval)
            elif op.opname in ["LOAD_METHOD", "LOAD_ATTR", "IMPORT_FROM"]:
                if tos is None:
                    refs.append(op.argval)
                elif isinstance(tos, str):
                    tos += "." + op.argval
                else:
                    tos = getattr(tos, op.argval)
            elif op.opname == "DELETE_FAST" and tos:
                del context.varnames[op.argval]
                tos = None
            elif op.opname == "STORE_FAST" and tos:
                context.varnames[op.argval] = tos
                tos = None
            elif op.opname == "LOAD_FAST" and op.argval in context.varnames:
                set_tos(context.varnames[op.argval])
            else:
                # For all other instructions, hash the current TOS.
                if tos is not None:
                    refs.append(tos)
                    tos = None
        except Exception as e:
            message = oneline(
                f"""
            Bionic found a code reference in file ${code.co_filename}
            at line ${lineno} that it cannot hash when hashing
            ${code.co_name}. We will ignore hashing the code reference,
            which means any changes to the reference won't invalidate
            the cache.

            This is most likely a bug in Bionic. Please raise a new
            issue at https://github.com/square/bionic/issues to let us
            know.
            """
            )
            message = message + "\n" + str(e)
            warnings.warn(message)

    return refs
