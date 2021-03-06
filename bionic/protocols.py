"""
Contains bionic's concept of a "protocol": an object that specifies how to
handle certain types of data.  A protocol is similar to a Python type, but
might be more or less specific.  For example, PicklableProtocol can be used
for any type that can be safely pickled.  On the other hand, DataFrameProtocol
takes additional arguments that constrain the structure of the DataFrames it
handles.  Protocols specify how to serialize, deserialize, and validate data.

This module contains a BaseProtocol class and various subclasses.
"""

from collections import Counter
import json
import pickle
import sys
import tempfile
import shutil
import warnings
from pathlib import Path

import yaml
import numpy as np
from pyarrow import parquet, Table
import pandas as pd

from .decoration import decorator_updating_accumulator
from .exception import UnsupportedSerializedValueError
from .deps.optdep import import_optional_dependency
from .utils.files import recursively_copy_path
from .utils.misc import (
    oneline,
    read_hashable_bytes_from_file_or_dir,
    single_element,
)
from . import tokenization


def check_is_like_protocol(obj):
    for method_name in "validate", "read", "write":
        if not hasattr(obj, method_name):
            raise ValueError(
                oneline(
                    f"""
                Expected {obj!r} to be a kind of Protocol, but didn't find
                expected method {method_name!r}"""
                )
            )


class BaseProtocol:
    def validate(self, value):
        """
        Checks if a value is valid for this protocol. Throws an exeception if the value
        is invalid; otherwise does nothing.
        """
        pass

    def value_is_valid(self, value):
        """Indicates whether the passed value is valid for this protocol."""
        try:
            self.validate(value)
        except Exception:
            return False
        return True

    def get_fixed_file_extension(self):
        """
        Returns a file extension identifying this protocol. This value will be appended
        to the name of any file written by the protocol, and may be used to determine
        whether a file can be read by the protocol.

        This string should be unique, not shared with any other protocol. By
        convention, it doesn't include an initial period, but may include periods in
        the middle.  (For example, `"csv"`, and `"csv.zip"` would both be sensible
        file extensions.)

        If a protocol uses different file extensions for different input values, it
        should implement ``file_extension_for_value`` instead.
        """
        raise NotImplementedError()

    def file_extension_for_value(self, value):
        """
        Like ``get_fixed_file_extension``, but may provide different extensions
        depending on the value to be serialized.
        """
        return self.get_fixed_file_extension()

    def can_read_file_extension(self, extension):
        """Indicates whether a file extension can be read by this protocol."""
        return extension == self.get_fixed_file_extension()

    def supports_value(self, value):
        """Indicates whether a value can be serialized by this protocol."""
        return any(isinstance(value, type_) for type_ in self.get_all_supported_types())

    def write(self, value, path):
        """Serializes the object ``value`` to the pathlib path ``path``."""
        raise NotImplementedError()

    def read(self, path):
        """Deserializes an object from the pathlib path ``path``, and returns it."""
        raise NotImplementedError()

    def read_with_extension(self, path, extension):
        """
        Deserializes an object from the pathlib path ``path``, and returns it.

        May use the file extension ``extension`` to determine what deserialization
        method to use. This extension must be the same extension generated by the
        protocol that originally serialized the value.
        """
        return self.read(path)

    SIMPLE_TYPES = {
        bool,
        str,
        bytes,
        str,
        int,
        int,
        float,
    }

    def tokenize(self, value):
        """
        Converts a valid value into a unique hash which can be used as a filename.
        """
        if type(value) in self.SIMPLE_TYPES:
            return tokenization.tokenize(value)
        else:
            return tokenization.tokenize(value, self._write_to_bytes)

    def tokenize_file(self, path):
        """Like ``tokenize``, but operates on a serialized value."""
        return tokenization.tokenize(path, read_hashable_bytes_from_file_or_dir)

    def _write_to_bytes(self, value):
        """
        Like ``write``, but returns an array of bytes rather than writing to a file.
        """
        file_name = "temp_file"
        temp_dir = Path(tempfile.mkdtemp())
        try:
            temp_file_path = temp_dir / file_name
            self.write(value, temp_file_path)
            return read_hashable_bytes_from_file_or_dir(temp_file_path)
        finally:
            shutil.rmtree(str(temp_dir))

    # This lets a protocol object be used in two different ways:
    # - called with a function or provider and used as a decorator
    # - called with keyword arguments to construct a new instance
    # The advantage is that later we can do something like:
    #
    #    frame = DataFrameProtocol()
    #
    # and then do either
    #
    #    @builder
    #    @frame
    #    def func(...):
    #        ...
    #
    # OR
    #
    #    @builder
    #    @frame(arg1=..., arg2=...)
    #    def func(...):
    #        ...
    #
    def __call__(self, func=None, **kwargs):
        if func is not None:
            if len(kwargs) > 0:
                raise ValueError(
                    f"{self} can't be called with both a function and keywords"
                )
            if not callable(func):
                raise ValueError(
                    oneline(
                        f"""
                    {self} must be used either (a) directly as a decorator or
                    (b) with keyword arguments;
                    it can't take positional arguments.
                    """
                    )
                )

            return decorator_updating_accumulator(
                lambda acc: acc.update_attr(
                    "protocol", self, "protocol_decorator", raise_if_already_set=False
                )
            )(func)
        else:
            return self.__class__(**kwargs)

    def __repr__(self):
        return f"{self.__class__.__name__}(...)"


class JsonProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values are built-in types that are
    JSON-serializable: the supported types are int, float, str, bool, list,
    and dict. Note that dict keys must be strings, and each element of a list
    or dict must itself be a supported built-in type.

    These values will be serialized to JSON files.
    """

    def get_fixed_file_extension(self):
        return "json"

    def validate(self, value):
        if value is None:
            return
        if isinstance(value, list):
            for elem in value:
                self.validate(elem)
            return
        if isinstance(value, dict):
            for key, elem in value.items():
                assert isinstance(key, str)
                self.validate(elem)
            return

        assert isinstance(value, (int, float, str, bool))

    def write(self, value, path):
        with path.open("w", encoding="utf-8") as file_:
            json.dump(value, file_, ensure_ascii=False)

    def read(self, path):
        with path.open("r", encoding="utf-8") as file_:
            return json.load(file_)


class PicklableProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values can be serialized using the
    ``pickle`` library.

    Parameters:
        pickle_protocol_version: int (default: 4)
            The pickle serialization protocol to use.
    """

    def __init__(self, pickle_protocol_version=4):
        super(PicklableProtocol, self).__init__()
        self._pickle_protocol_version = pickle_protocol_version

    def get_fixed_file_extension(self):
        return "pkl"

    def write(self, value, path):
        with path.open("wb") as file_:
            pickle.dump(value, file_, protocol=self._pickle_protocol_version)

    def read(self, path):
        with path.open("rb") as file_:
            return pickle.load(file_)


class DillableProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values can be serialized using the
    ``dill`` library.

    This is useful for objects that can't be pickled for some reason.
    """

    def get_fixed_file_extension(self):
        return "dill"

    def __init__(self, suppress_dill_side_effects=True):
        super(DillableProtocol, self).__init__()

        self._suppress_dill_side_effects = suppress_dill_side_effects
        self._dill = None

    def _get_dill_module(self):
        if self._dill is not None:
            return self._dill

        dill_already_imported = "dill" in sys.modules
        self._dill = import_optional_dependency(
            "dill", purpose="the @dillable protocol"
        )
        if not dill_already_imported and self._suppress_dill_side_effects:
            self._dill.extend(False)

        return self._dill

    def write(self, value, path):
        with path.open("wb") as file_:
            self._get_dill_module().dump(value, file_)

    def read(self, path):
        with path.open("rb") as file_:
            return self._get_dill_module().load(file_)


class ParquetDataFrameProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values always have the
    ``pandas.DataFrame`` type.

    These values will be serialized to Parquet files.

    Parameters:
        check_dtypes: boolean (default: True)
            Attempt to check for column types not supported by the file format.
    """

    def __init__(self, check_dtypes=True):
        super(ParquetDataFrameProtocol, self).__init__()

        self._check_dtypes = check_dtypes

    def get_fixed_file_extension(self):
        return "pq"

    def validate(self, value):
        assert isinstance(value, pd.DataFrame)

    def read(self, path):
        with path.open("rb") as file_:
            return parquet.read_table(file_).to_pandas()

    def write(self, df, path):
        self._check_no_duplicate_cols(df)
        if self._check_dtypes:
            self._check_no_categorical_cols(df)
        with path.open("wb") as file_:
            parquet.write_table(Table.from_pandas(df), file_)

    def _check_no_duplicate_cols(self, df):
        duplicate_cols = {
            elem: count for elem, count in Counter(df.columns).items() if count > 1
        }
        if duplicate_cols:
            raise ValueError(
                oneline(
                    f"""
                    Attempted to serialize to Parquet a dataframe which has
                    duplicate columns with the following counts:
                    {duplicate_cols}. You can fix this by dropping duplicate
                    columns with something like: """
                )
                + "\n"
                + "df = df.loc[:, ~df.columns.duplicated()]"
            )

    def _check_no_categorical_cols(self, df):
        categorical_cols = [
            col for col in df.columns if df[col].dtype.name == "category"
        ]

        if categorical_cols:
            raise ValueError(
                oneline(
                    f"""
                Attempted to serialize to Parquet a dataframe which has
                categorical columns: {categorical_cols!r} --
                these columns may be transformed to another type and/or
                lose some information.
                You can fix this by using
                (a) ``@frame(file_format='feather')`` to use the Feather
                format instead, or
                (b) ``@frame(check_dtypes=False)`` to ignore this check."""
                )
            )


class FeatherDataFrameProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values always have the
    ``pandas.DataFrame`` type.

    These values will be serialized to Feather files.
    """

    def get_fixed_file_extension(self):
        return "feather"

    def validate(self, value):
        assert isinstance(value, pd.DataFrame)

    def read(self, path):
        with path.open("rb") as file_:
            return pd.read_feather(file_)

    def write(self, df, path):
        with path.open("wb") as file_:
            df.to_feather(file_)


Image = import_optional_dependency("PIL.Image", raise_on_missing=False)


class ImageProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values always have the
    ``Pillow.Image`` type.

    These values will be serialized to PNG files.
    """

    def get_fixed_file_extension(self):
        return "png"

    def validate(self, value):
        # If Image is None, then the PIL library is not present, which
        # presumably means this value is not a PIL image, and hence should fail
        # validation.
        assert Image is not None
        assert isinstance(value, Image.Image)

    def read(self, path):
        with path.open("rb") as file_:
            Image = import_optional_dependency(
                "PIL.Image", purpose="the @image decorator"
            )
            image = Image.open(file_)
            # Image.open() is lazy; if we don't call load() now, the file can
            # be closed or possibly invalidated before it actually gets read.
            image.load()
            return image

    def write(self, image, path):
        with path.open("wb") as file_:
            image.save(file_, format="png")


class NumPyProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values always have the
    ``numpy.ndarray`` type.

    These values will be serialized to .npy files.
    """

    def get_fixed_file_extension(self):
        return ".npy"

    def validate(self, value):
        assert isinstance(value, np.ndarray)

    def read(self, path):
        with path.open("rb") as file_:
            return np.load(file_)

    def write(self, array, path):
        with path.open("wb") as file_:
            np.save(file_, array)


dd = import_optional_dependency("dask.dataframe", raise_on_missing=False)


class DaskProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values always have the
    ``dask.dataframe.DataFrame`` type.

    These values will be serialized to a .dask.pq directory.
    """

    def get_fixed_file_extension(self):
        return "pq.dask"

    def validate(self, value):
        # If dd is None, then dask with dataframe (i.e. dask[dataframe]) is not present,
        # which presumably means this value is not a dask dataframe, and hence should fail
        # validation.
        assert dd is not None
        assert isinstance(value, dd.DataFrame)

    def read(self, path):
        dd = import_optional_dependency("dask.dataframe", purpose="the @dask decorator")
        with warnings.catch_warnings():
            warnings.filterwarnings("error", message=r".*cannot\s+autodetect\s+index.*")
            try:
                return dd.read_parquet(path)
            except RuntimeWarning as e:
                raise UnsupportedSerializedValueError(
                    f"Reading dataframe failed due to present MultiIndex: {e}"
                )

    def write(self, df, path):
        dd.to_parquet(df, path, write_index=True)


class YamlProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values can be serialized using the
    ``PyYAML`` library.

    Parameters
    ----------
    **kwargs: keyword args for ``yaml.dump``
        E.g. ``default_flow_style``, ``encoding``, etc.
    """

    def __init__(self, **kwargs):
        super(YamlProtocol, self).__init__()
        self._kwargs = kwargs

    def get_fixed_file_extension(self):
        return "yaml"

    def write(self, value, path):
        with path.open("w", encoding="utf-8") as file_:
            yaml.dump(value, file_, **self._kwargs)

    def read(self, path):
        with path.open("r", encoding="utf-8") as file_:
            return yaml.safe_load(file_)


class PathProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values are ``pathlib.Path`` objects
    referring to local files. When the Path is serialized, the underlying
    files are transferred to Bionic's internal file cache; this means a Path
    can be serialized to a cloud cache and then deserialized on a different
    machine and still work.  The Path can refer to either a file or a directory.

    Parameters
    ----------
    operation: {"move", "copy"} (default: "copy")
        Indicates whether the underlying file should be moved or copied to
        Bionic's internal cache. If the file is created by this entity
        function, it probably makes sense to use "move", since no one else
        should be accessing the file anyway. If the file already existed,
        then "copy" is better.
    """

    def __init__(self, operation="copy"):
        super(PathProtocol, self).__init__()
        known_operations = ("move", "copy")
        if operation not in known_operations:
            raise ValueError(
                oneline(
                    f"""
                Operation must be in {known_operations!r}:
                got {operation}."""
                )
            )
        self.operation = operation

    def get_fixed_file_extension(self):
        return "as_path"

    def validate(self, value):
        assert isinstance(value, Path)

    def write(self, value, path):
        # We store the path object as a directory containing the actual file:
        #     .../XXX.as_path/ORIG_FILENAME
        # (We use a directory because we don't control the `XXX.as_path` name,
        # so this way the file itself will keep its original name and
        # extension.)

        src_path = value
        dst_dir_path = path
        dst_dir_path.mkdir()
        dst_path = dst_dir_path / src_path.name

        if self.operation == "move":
            shutil.move(src_path, dst_path)
        elif self.operation == "copy":
            recursively_copy_path(src_path, dst_path)
        else:
            raise AssertionError(f"Unexpected operation: {self.operation!r}")

    def read(self, path):
        return single_element(path.iterdir())


class CombinedProtocol(BaseProtocol):
    """
    Decorator generator indicating that an entity's values should be handled
    differently depending on their types.

    This protocol combines multiple protocols, and handles each value according
    to the first protocol that supports it.

    Parameters
    ----------
    subprotocols: Sequence of Protocols
        An ordered sequence of protocols to try for each value.

    Returns
    -------
    Function:
        An entity decorator.
    """

    def __init__(self, *subprotocols):
        super(CombinedProtocol, self).__init__()

        self._subprotocols = subprotocols

    def _protocol_for_value(self, value):
        for protocol in self._subprotocols:
            if protocol.value_is_valid(value):
                return protocol
        return None

    def _protocol_for_extension(self, extension):
        for protocol in self._subprotocols:
            if protocol.can_read_file_extension(extension):
                return protocol
        return None

    def can_read_file_extension(self, extension):
        return self._protocol_for_extension(extension) is not None

    def file_extension_for_value(self, value):
        return self._protocol_for_value(value).file_extension_for_value(value)

    def validate(self, value):
        if self._protocol_for_value(value) is not None:
            return

        if not self._subprotocols:
            raise AssertionError("No subprotocols defined")
        else:
            self._subprotocols[-1].validate(value)

    def read_with_extension(self, path, extension):
        protocol = self._protocol_for_extension(extension)
        if protocol is None:
            raise ValueError(
                "This protocol doesn't know how to read a file with "
                f"extension {extension!r}"
            )
        return protocol.read_with_extension(path, extension)

    def write(self, value, path):
        self._protocol_for_value(value).write(value, path)

    def __repr__(self):
        return f"CombinedProtocol{tuple(self._subprotocols)!r}"


class TypeProtocol(PicklableProtocol):
    """
    Indicates that an entity's values will always have a specific type.

    Parameters
    ----------
    type_: Type
        The expected type for this entity.

    Returns
    -------
    Function:
        A entity decorator.
    """

    def __init__(self, type_):
        super(TypeProtocol, self).__init__()

        self._type = type_

    def validate(self, value):
        assert isinstance(value, self._type)

    def __repr__(self):
        return f"TypeProtocol({self._type.__name__})"


class EnumProtocol(PicklableProtocol):
    """
    Indicates that an entity will only have one of a specific set of values.

    Parameters
    ----------
    allowed_values: Sequence of objects
        The expected possible values for this entity.

    Returns
    -------
    Function:
        An entity decorator.
    """

    def __init__(self, *allowed_values):
        super(EnumProtocol, self).__init__()

        self._allowed_values = allowed_values

    def validate(self, value):
        assert value in self._allowed_values

    def __repr__(self):
        return f"EnumProtocol{tuple(self._allowed_values)!r}"


gpd = import_optional_dependency("geopandas", raise_on_missing=False)


class GeoPandasProtocol(BaseProtocol):
    """
    Decorator indicating that an entity's values always have the
    ``geopandas.geodataframe.GeoDataFrame`` type.

    These values will be serialized to SHP files.
    """

    def get_fixed_file_extension(self):
        return "shp"

    def write(self, value, path):
        self._check_column_name_length_restriction(value)
        value.to_file(path)

    def _check_column_name_length_restriction(self, df):
        long_columns = [c for c in df.columns if len(c) > 10]
        if long_columns:
            raise ValueError(
                oneline(
                    f"""
                    GeoDataFrames truncate all column names to length 10 due
                    to constraints from ShapeFiles. Column names {long_columns}
                    have length greater than 10. You can fix this by renaming
                    your columns to be a shorter length."""
                )
            )

    def read(self, path):
        gpd = import_optional_dependency("geopandas", purpose="deserialize a .shp file")
        return gpd.read_file(path)

    def validate(self, value):
        assert gpd is not None
        assert isinstance(value, gpd.GeoDataFrame)


class TupleProtocol(BaseProtocol):
    """
    Describes values that are Python tuples of a fixed length. This is used mainly
    by Bionic's infrastructure for values corresponding to tuple descriptors.

    This protocol does not support serializing or deserializing values.
    """

    def __init__(self, length):
        super(TupleProtocol, self).__init__()

        self._expected_length = length

    def validate(self, value):
        try:
            items = tuple(value)
        except TypeError as e:
            raise AssertionError(str(e))
        if len(items) != self._expected_length:
            message = f"""
            Expected a sequence with length {self._expected_length};
            instead, got {len(items)} values: {items!r}
            """
            raise AssertionError(oneline(message))
