from __future__ import annotations

from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Sequence,
    TypeVar,
    cast,
    overload,
)

import numpy as np

from pandas._libs import lib
from pandas._libs.arrays import NDArrayBacked
from pandas._typing import (
    ArrayLike,
    Dtype,
    F,
    PositionalIndexer2D,
    PositionalIndexerTuple,
    ScalarIndexer,
    SequenceIndexer,
    Shape,
    TakeIndexer,
    npt,
    type_t,
)
from pandas.compat import (
    pa_version_under1p01,
    pa_version_under2p0,
    pa_version_under5p0,
)
from pandas.errors import AbstractMethodError
from pandas.util._decorators import doc
from pandas.util._validators import (
    validate_bool_kwarg,
    validate_fillna_kwargs,
    validate_insert_loc,
)

from pandas.core.dtypes.common import (
    is_array_like,
    is_bool_dtype,
    is_dtype_equal,
    is_integer,
    is_scalar,
    pandas_dtype,
)
from pandas.core.dtypes.dtypes import (
    DatetimeTZDtype,
    ExtensionDtype,
    PeriodDtype,
)
from pandas.core.dtypes.missing import (
    array_equivalent,
    isna,
)

from pandas.core import missing
from pandas.core.algorithms import (
    take,
    unique,
    value_counts,
)
from pandas.core.array_algos.quantile import quantile_with_mask
from pandas.core.array_algos.transforms import shift
from pandas.core.arrays.base import ExtensionArray
from pandas.core.construction import extract_array
from pandas.core.indexers import (
    check_array_indexer,
    validate_indices,
)
from pandas.core.sorting import nargminmax

NDArrayBackedExtensionArrayT = TypeVar(
    "NDArrayBackedExtensionArrayT", bound="NDArrayBackedExtensionArray"
)

if not pa_version_under1p01:
    import pyarrow as pa
    import pyarrow.compute as pc

if TYPE_CHECKING:
    from pandas._typing import (
        NumpySorter,
        NumpyValueArrayLike,
    )

    from pandas import Series


def ravel_compat(meth: F) -> F:
    """
    Decorator to ravel a 2D array before passing it to a cython operation,
    then reshape the result to our own shape.
    """

    @wraps(meth)
    def method(self, *args, **kwargs):
        if self.ndim == 1:
            return meth(self, *args, **kwargs)

        flags = self._ndarray.flags
        flat = self.ravel("K")
        result = meth(flat, *args, **kwargs)
        order = "F" if flags.f_contiguous else "C"
        return result.reshape(self.shape, order=order)

    return cast(F, method)


class NDArrayBackedExtensionArray(NDArrayBacked, ExtensionArray):
    """
    ExtensionArray that is backed by a single NumPy ndarray.
    """

    _ndarray: np.ndarray

    # scalar used to denote NA value inside our self._ndarray, e.g. -1
    #  for Categorical, iNaT for Period. Outside of object dtype,
    #  self.isna() should be exactly locations in self._ndarray with
    #  _internal_fill_value.
    _internal_fill_value: Any

    def _box_func(self, x):
        """
        Wrap numpy type in our dtype.type if necessary.
        """
        return x

    def _validate_scalar(self, value):
        # used by NDArrayBackedExtensionIndex.insert
        raise AbstractMethodError(self)

    # ------------------------------------------------------------------------

    def view(self, dtype: Dtype | None = None) -> ArrayLike:
        # We handle datetime64, datetime64tz, timedelta64, and period
        #  dtypes here. Everything else we pass through to the underlying
        #  ndarray.
        if dtype is None or dtype is self.dtype:
            return self._from_backing_data(self._ndarray)

        if isinstance(dtype, type):
            # we sometimes pass non-dtype objects, e.g np.ndarray;
            #  pass those through to the underlying ndarray
            return self._ndarray.view(dtype)

        dtype = pandas_dtype(dtype)
        arr = self._ndarray

        if isinstance(dtype, (PeriodDtype, DatetimeTZDtype)):
            cls = dtype.construct_array_type()
            return cls(arr.view("i8"), dtype=dtype)
        elif dtype == "M8[ns]":
            from pandas.core.arrays import DatetimeArray

            return DatetimeArray(arr.view("i8"), dtype=dtype)
        elif dtype == "m8[ns]":
            from pandas.core.arrays import TimedeltaArray

            return TimedeltaArray(arr.view("i8"), dtype=dtype)

        # error: Argument "dtype" to "view" of "_ArrayOrScalarCommon" has incompatible
        # type "Union[ExtensionDtype, dtype[Any]]"; expected "Union[dtype[Any], None,
        # type, _SupportsDType, str, Union[Tuple[Any, int], Tuple[Any, Union[int,
        # Sequence[int]]], List[Any], _DTypeDict, Tuple[Any, Any]]]"
        return arr.view(dtype=dtype)  # type: ignore[arg-type]

    def take(
        self: NDArrayBackedExtensionArrayT,
        indices: TakeIndexer,
        *,
        allow_fill: bool = False,
        fill_value: Any = None,
        axis: int = 0,
    ) -> NDArrayBackedExtensionArrayT:
        if allow_fill:
            fill_value = self._validate_scalar(fill_value)

        new_data = take(
            self._ndarray,
            indices,
            allow_fill=allow_fill,
            fill_value=fill_value,
            axis=axis,
        )
        return self._from_backing_data(new_data)

    # ------------------------------------------------------------------------

    def equals(self, other) -> bool:
        if type(self) is not type(other):
            return False
        if not is_dtype_equal(self.dtype, other.dtype):
            return False
        return bool(array_equivalent(self._ndarray, other._ndarray))

    @classmethod
    def _from_factorized(cls, values, original):
        assert values.dtype == original._ndarray.dtype
        return original._from_backing_data(values)

    def _values_for_argsort(self) -> np.ndarray:
        return self._ndarray

    def _values_for_factorize(self):
        return self._ndarray, self._internal_fill_value

    # Signature of "argmin" incompatible with supertype "ExtensionArray"
    def argmin(self, axis: int = 0, skipna: bool = True):  # type: ignore[override]
        # override base class by adding axis keyword
        validate_bool_kwarg(skipna, "skipna")
        if not skipna and self._hasna:
            raise NotImplementedError
        return nargminmax(self, "argmin", axis=axis)

    # Signature of "argmax" incompatible with supertype "ExtensionArray"
    def argmax(self, axis: int = 0, skipna: bool = True):  # type: ignore[override]
        # override base class by adding axis keyword
        validate_bool_kwarg(skipna, "skipna")
        if not skipna and self._hasna:
            raise NotImplementedError
        return nargminmax(self, "argmax", axis=axis)

    def unique(self: NDArrayBackedExtensionArrayT) -> NDArrayBackedExtensionArrayT:
        new_data = unique(self._ndarray)
        return self._from_backing_data(new_data)

    @classmethod
    @doc(ExtensionArray._concat_same_type)
    def _concat_same_type(
        cls: type[NDArrayBackedExtensionArrayT],
        to_concat: Sequence[NDArrayBackedExtensionArrayT],
        axis: int = 0,
    ) -> NDArrayBackedExtensionArrayT:
        dtypes = {str(x.dtype) for x in to_concat}
        if len(dtypes) != 1:
            raise ValueError("to_concat must have the same dtype (tz)", dtypes)

        new_values = [x._ndarray for x in to_concat]
        new_arr = np.concatenate(new_values, axis=axis)
        return to_concat[0]._from_backing_data(new_arr)

    @doc(ExtensionArray.searchsorted)
    def searchsorted(
        self,
        value: NumpyValueArrayLike | ExtensionArray,
        side: Literal["left", "right"] = "left",
        sorter: NumpySorter = None,
    ) -> npt.NDArray[np.intp] | np.intp:
        # TODO(2.0): use _validate_setitem_value once dt64tz mismatched-timezone
        #  deprecation is enforced
        npvalue = self._validate_searchsorted_value(value)
        return self._ndarray.searchsorted(npvalue, side=side, sorter=sorter)

    def _validate_searchsorted_value(
        self, value: NumpyValueArrayLike | ExtensionArray
    ) -> NumpyValueArrayLike:
        # TODO(2.0): after deprecation in datetimelikearraymixin is enforced,
        #  we can remove this and use _validate_setitem_value directly
        if isinstance(value, ExtensionArray):
            return value.to_numpy()
        else:
            return value

    @doc(ExtensionArray.shift)
    def shift(self, periods=1, fill_value=None, axis=0):

        fill_value = self._validate_shift_value(fill_value)
        new_values = shift(self._ndarray, periods, axis, fill_value)

        return self._from_backing_data(new_values)

    def _validate_shift_value(self, fill_value):
        # TODO(2.0): after deprecation in datetimelikearraymixin is enforced,
        #  we can remove this and use validate_fill_value directly
        return self._validate_scalar(fill_value)

    def __setitem__(self, key, value):
        key = check_array_indexer(self, key)
        value = self._validate_setitem_value(value)
        self._ndarray[key] = value

    def _validate_setitem_value(self, value):
        return value

    @overload
    def __getitem__(self, key: ScalarIndexer) -> Any:
        ...

    @overload
    def __getitem__(
        self: NDArrayBackedExtensionArrayT,
        key: SequenceIndexer | PositionalIndexerTuple,
    ) -> NDArrayBackedExtensionArrayT:
        ...

    def __getitem__(
        self: NDArrayBackedExtensionArrayT,
        key: PositionalIndexer2D,
    ) -> NDArrayBackedExtensionArrayT | Any:
        if lib.is_integer(key):
            # fast-path
            result = self._ndarray[key]
            if self.ndim == 1:
                return self._box_func(result)
            return self._from_backing_data(result)

        # error: Incompatible types in assignment (expression has type "ExtensionArray",
        # variable has type "Union[int, slice, ndarray]")
        key = extract_array(key, extract_numpy=True)  # type: ignore[assignment]
        key = check_array_indexer(self, key)
        result = self._ndarray[key]
        if lib.is_scalar(result):
            return self._box_func(result)

        result = self._from_backing_data(result)
        return result

    def _fill_mask_inplace(
        self, method: str, limit, mask: npt.NDArray[np.bool_]
    ) -> None:
        # (for now) when self.ndim == 2, we assume axis=0
        func = missing.get_fill_func(method, ndim=self.ndim)
        func(self._ndarray.T, limit=limit, mask=mask.T)
        return

    @doc(ExtensionArray.fillna)
    def fillna(
        self: NDArrayBackedExtensionArrayT, value=None, method=None, limit=None
    ) -> NDArrayBackedExtensionArrayT:
        value, method = validate_fillna_kwargs(
            value, method, validate_scalar_dict_value=False
        )

        mask = self.isna()
        # error: Argument 2 to "check_value_size" has incompatible type
        # "ExtensionArray"; expected "ndarray"
        value = missing.check_value_size(
            value, mask, len(self)  # type: ignore[arg-type]
        )

        if mask.any():
            if method is not None:
                # TODO: check value is None
                # (for now) when self.ndim == 2, we assume axis=0
                func = missing.get_fill_func(method, ndim=self.ndim)
                npvalues = self._ndarray.T.copy()
                func(npvalues, limit=limit, mask=mask.T)
                npvalues = npvalues.T

                # TODO: PandasArray didn't used to copy, need tests for this
                new_values = self._from_backing_data(npvalues)
            else:
                # fill with value
                new_values = self.copy()
                new_values[mask] = value
        else:
            # We validate the fill_value even if there is nothing to fill
            if value is not None:
                self._validate_setitem_value(value)

            new_values = self.copy()
        return new_values

    # ------------------------------------------------------------------------
    # Reductions

    def _wrap_reduction_result(self, axis: int | None, result):
        if axis is None or self.ndim == 1:
            return self._box_func(result)
        return self._from_backing_data(result)

    # ------------------------------------------------------------------------
    # __array_function__ methods

    def _putmask(self, mask: npt.NDArray[np.bool_], value) -> None:
        """
        Analogue to np.putmask(self, mask, value)

        Parameters
        ----------
        mask : np.ndarray[bool]
        value : scalar or listlike

        Raises
        ------
        TypeError
            If value cannot be cast to self.dtype.
        """
        value = self._validate_setitem_value(value)

        np.putmask(self._ndarray, mask, value)

    def _where(
        self: NDArrayBackedExtensionArrayT, mask: npt.NDArray[np.bool_], value
    ) -> NDArrayBackedExtensionArrayT:
        """
        Analogue to np.where(mask, self, value)

        Parameters
        ----------
        mask : np.ndarray[bool]
        value : scalar or listlike

        Raises
        ------
        TypeError
            If value cannot be cast to self.dtype.
        """
        value = self._validate_setitem_value(value)

        res_values = np.where(mask, self._ndarray, value)
        return self._from_backing_data(res_values)

    # ------------------------------------------------------------------------
    # Index compat methods

    def insert(
        self: NDArrayBackedExtensionArrayT, loc: int, item
    ) -> NDArrayBackedExtensionArrayT:
        """
        Make new ExtensionArray inserting new item at location. Follows
        Python list.append semantics for negative values.

        Parameters
        ----------
        loc : int
        item : object

        Returns
        -------
        type(self)
        """
        loc = validate_insert_loc(loc, len(self))

        code = self._validate_scalar(item)

        new_vals = np.concatenate(
            (
                self._ndarray[:loc],
                np.asarray([code], dtype=self._ndarray.dtype),
                self._ndarray[loc:],
            )
        )
        return self._from_backing_data(new_vals)

    # ------------------------------------------------------------------------
    # Additional array methods
    #  These are not part of the EA API, but we implement them because
    #  pandas assumes they're there.

    def value_counts(self, dropna: bool = True):
        """
        Return a Series containing counts of unique values.

        Parameters
        ----------
        dropna : bool, default True
            Don't include counts of NA values.

        Returns
        -------
        Series
        """
        if self.ndim != 1:
            raise NotImplementedError

        from pandas import (
            Index,
            Series,
        )

        if dropna:
            # error: Unsupported operand type for ~ ("ExtensionArray")
            values = self[~self.isna()]._ndarray  # type: ignore[operator]
        else:
            values = self._ndarray

        result = value_counts(values, sort=False, dropna=dropna)

        index_arr = self._from_backing_data(np.asarray(result.index._data))
        index = Index(index_arr, name=result.index.name)
        return Series(result._values, index=index, name=result.name)

    def _quantile(
        self: NDArrayBackedExtensionArrayT,
        qs: npt.NDArray[np.float64],
        interpolation: str,
    ) -> NDArrayBackedExtensionArrayT:
        # TODO: disable for Categorical if not ordered?

        # asarray needed for Sparse, see GH#24600
        mask = np.asarray(self.isna())
        mask = np.atleast_2d(mask)

        arr = np.atleast_2d(self._ndarray)
        fill_value = self._internal_fill_value

        res_values = quantile_with_mask(arr, mask, fill_value, qs, interpolation)
        res_values = self._cast_quantile_result(res_values)
        result = self._from_backing_data(res_values)
        if self.ndim == 1:
            assert result.shape == (1, len(qs)), result.shape
            result = result[0]

        return result

    # TODO: see if we can share this with other dispatch-wrapping methods
    def _cast_quantile_result(self, res_values: np.ndarray) -> np.ndarray:
        """
        Cast the result of quantile_with_mask to an appropriate dtype
        to pass to _from_backing_data in _quantile.
        """
        return res_values

    # ------------------------------------------------------------------------
    # numpy-like methods

    @classmethod
    def _empty(
        cls: type_t[NDArrayBackedExtensionArrayT], shape: Shape, dtype: ExtensionDtype
    ) -> NDArrayBackedExtensionArrayT:
        """
        Analogous to np.empty(shape, dtype=dtype)

        Parameters
        ----------
        shape : tuple[int]
        dtype : ExtensionDtype
        """
        # The base implementation uses a naive approach to find the dtype
        #  for the backing ndarray
        arr = cls._from_sequence([], dtype=dtype)
        backing = np.empty(shape, dtype=arr._ndarray.dtype)
        return arr._from_backing_data(backing)


ArrowExtensionArrayT = TypeVar("ArrowExtensionArrayT", bound="ArrowExtensionArray")


class ArrowExtensionArray(ExtensionArray):
    """
    Base class for ExtensionArray backed by Arrow array.
    """

    _data: pa.ChunkedArray

    def __init__(self, values: pa.ChunkedArray) -> None:
        self._data = values

    def __arrow_array__(self, type=None):
        """Convert myself to a pyarrow Array or ChunkedArray."""
        return self._data

    def equals(self, other) -> bool:
        if not isinstance(other, ArrowExtensionArray):
            return False
        # I'm told that pyarrow makes __eq__ behave like pandas' equals;
        #  TODO: is this documented somewhere?
        return self._data == other._data

    @property
    def nbytes(self) -> int:
        """
        The number of bytes needed to store this object in memory.
        """
        return self._data.nbytes

    def __len__(self) -> int:
        """
        Length of this array.

        Returns
        -------
        length : int
        """
        return len(self._data)

    def isna(self) -> npt.NDArray[np.bool_]:
        """
        Boolean NumPy array indicating if each value is missing.

        This should return a 1-D array the same length as 'self'.
        """
        if pa_version_under2p0:
            return self._data.is_null().to_pandas().values
        else:
            return self._data.is_null().to_numpy()

    def copy(self: ArrowExtensionArrayT) -> ArrowExtensionArrayT:
        """
        Return a shallow copy of the array.

        Underlying ChunkedArray is immutable, so a deep copy is unnecessary.

        Returns
        -------
        type(self)
        """
        return type(self)(self._data)

    @doc(ExtensionArray.factorize)
    def factorize(self, na_sentinel: int = -1) -> tuple[np.ndarray, ExtensionArray]:
        encoded = self._data.dictionary_encode()
        indices = pa.chunked_array(
            [c.indices for c in encoded.chunks], type=encoded.type.index_type
        ).to_pandas()
        if indices.dtype.kind == "f":
            indices[np.isnan(indices)] = na_sentinel
        indices = indices.astype(np.int64, copy=False)

        if encoded.num_chunks:
            uniques = type(self)(encoded.chunk(0).dictionary)
        else:
            uniques = type(self)(pa.array([], type=encoded.type.value_type))

        return indices.values, uniques

    def take(
        self,
        indices: TakeIndexer,
        allow_fill: bool = False,
        fill_value: Any = None,
    ):
        """
        Take elements from an array.

        Parameters
        ----------
        indices : sequence of int or one-dimensional np.ndarray of int
            Indices to be taken.
        allow_fill : bool, default False
            How to handle negative values in `indices`.

            * False: negative values in `indices` indicate positional indices
              from the right (the default). This is similar to
              :func:`numpy.take`.

            * True: negative values in `indices` indicate
              missing values. These values are set to `fill_value`. Any other
              other negative values raise a ``ValueError``.

        fill_value : any, optional
            Fill value to use for NA-indices when `allow_fill` is True.
            This may be ``None``, in which case the default NA value for
            the type, ``self.dtype.na_value``, is used.

            For many ExtensionArrays, there will be two representations of
            `fill_value`: a user-facing "boxed" scalar, and a low-level
            physical NA value. `fill_value` should be the user-facing version,
            and the implementation should handle translating that to the
            physical version for processing the take if necessary.

        Returns
        -------
        ExtensionArray

        Raises
        ------
        IndexError
            When the indices are out of bounds for the array.
        ValueError
            When `indices` contains negative values other than ``-1``
            and `allow_fill` is True.

        See Also
        --------
        numpy.take
        api.extensions.take

        Notes
        -----
        ExtensionArray.take is called by ``Series.__getitem__``, ``.loc``,
        ``iloc``, when `indices` is a sequence of values. Additionally,
        it's called by :meth:`Series.reindex`, or any other method
        that causes realignment, with a `fill_value`.
        """
        # TODO: Remove once we got rid of the (indices < 0) check
        if not is_array_like(indices):
            indices_array = np.asanyarray(indices)
        else:
            # error: Incompatible types in assignment (expression has type
            # "Sequence[int]", variable has type "ndarray")
            indices_array = indices  # type: ignore[assignment]

        if len(self._data) == 0 and (indices_array >= 0).any():
            raise IndexError("cannot do a non-empty take")
        if indices_array.size > 0 and indices_array.max() >= len(self._data):
            raise IndexError("out of bounds value in 'indices'.")

        if allow_fill:
            fill_mask = indices_array < 0
            if fill_mask.any():
                validate_indices(indices_array, len(self._data))
                # TODO(ARROW-9433): Treat negative indices as NULL
                indices_array = pa.array(indices_array, mask=fill_mask)
                result = self._data.take(indices_array)
                if isna(fill_value):
                    return type(self)(result)
                # TODO: ArrowNotImplementedError: Function fill_null has no
                # kernel matching input types (array[string], scalar[string])
                result = type(self)(result)
                result[fill_mask] = fill_value
                return result
                # return type(self)(pc.fill_null(result, pa.scalar(fill_value)))
            else:
                # Nothing to fill
                return type(self)(self._data.take(indices))
        else:  # allow_fill=False
            # TODO(ARROW-9432): Treat negative indices as indices from the right.
            if (indices_array < 0).any():
                # Don't modify in-place
                indices_array = np.copy(indices_array)
                indices_array[indices_array < 0] += len(self._data)
            return type(self)(self._data.take(indices_array))

    def value_counts(self, dropna: bool = True) -> Series:
        """
        Return a Series containing counts of each unique value.

        Parameters
        ----------
        dropna : bool, default True
            Don't include counts of missing values.

        Returns
        -------
        counts : Series

        See Also
        --------
        Series.value_counts
        """
        from pandas import (
            Index,
            Series,
        )

        vc = self._data.value_counts()

        values = vc.field(0)
        counts = vc.field(1)
        if dropna and self._data.null_count > 0:
            mask = values.is_valid()
            values = values.filter(mask)
            counts = counts.filter(mask)

        # No missing values so we can adhere to the interface and return a numpy array.
        counts = np.array(counts)

        index = Index(type(self)(values))

        return Series(counts, index=index).astype("Int64")

    @classmethod
    def _concat_same_type(
        cls: type[ArrowExtensionArrayT], to_concat
    ) -> ArrowExtensionArrayT:
        """
        Concatenate multiple ArrowExtensionArrays.

        Parameters
        ----------
        to_concat : sequence of ArrowExtensionArrays

        Returns
        -------
        ArrowExtensionArray
        """
        import pyarrow as pa

        chunks = [array for ea in to_concat for array in ea._data.iterchunks()]
        arr = pa.chunked_array(chunks)
        return cls(arr)

    def __setitem__(self, key: int | slice | np.ndarray, value: Any) -> None:
        """Set one or more values inplace.

        Parameters
        ----------
        key : int, ndarray, or slice
            When called from, e.g. ``Series.__setitem__``, ``key`` will be
            one of

            * scalar int
            * ndarray of integers.
            * boolean ndarray
            * slice object

        value : ExtensionDtype.type, Sequence[ExtensionDtype.type], or object
            value or values to be set of ``key``.

        Returns
        -------
        None
        """
        key = check_array_indexer(self, key)
        indices = self._indexing_key_to_indices(key)
        value = self._maybe_convert_setitem_value(value)

        argsort = np.argsort(indices)
        indices = indices[argsort]

        if is_scalar(value):
            value = np.broadcast_to(value, len(self))
        elif len(indices) != len(value):
            raise ValueError("Length of indexer and values mismatch")
        else:
            value = np.asarray(value)[argsort]

        self._data = self._set_via_chunk_iteration(indices=indices, value=value)

    def _indexing_key_to_indices(
        self, key: int | slice | np.ndarray
    ) -> npt.NDArray[np.intp]:
        """
        Convert indexing key for self into positional indices.

        Parameters
        ----------
        key : int | slice | np.ndarray

        Returns
        -------
        npt.NDArray[np.intp]
        """
        n = len(self)
        if isinstance(key, slice):
            indices = np.arange(n)[key]
        elif is_integer(key):
            indices = np.arange(n)[[key]]  # type: ignore[index]
        elif is_bool_dtype(key):
            key = np.asarray(key)
            if len(key) != n:
                raise ValueError("Length of indexer and values mismatch")
            indices = key.nonzero()[0]
        else:
            key = np.asarray(key)
            indices = np.arange(n)[key]
        return indices

    def _maybe_convert_setitem_value(self, value):
        """Maybe convert value to be pyarrow compatible."""
        raise NotImplementedError()

    def _set_via_chunk_iteration(
        self, indices: npt.NDArray[np.intp], value: npt.NDArray[Any]
    ) -> pa.ChunkedArray:
        """
        Loop through the array chunks and set the new values while
        leaving the chunking layout unchanged.

        Parameters
        ----------
        indices : npt.NDArray[np.intp]
            Position indices for the underlying ChunkedArray.

        value : ExtensionDtype.type, Sequence[ExtensionDtype.type], or object
            value or values to be set of ``key``.

        Notes
        -----
        Assumes that indices is sorted. Caller is responsible for sorting.
        """
        new_data = []
        stop = 0
        for chunk in self._data.iterchunks():
            start, stop = stop, stop + len(chunk)
            if len(indices) == 0 or stop <= indices[0]:
                new_data.append(chunk)
            else:
                n = int(np.searchsorted(indices, stop, side="left"))
                c_ind = indices[:n] - start
                indices = indices[n:]
                n = len(c_ind)
                c_value, value = value[:n], value[n:]
                new_data.append(self._replace_with_indices(chunk, c_ind, c_value))
        return pa.chunked_array(new_data)

    @classmethod
    def _replace_with_indices(
        cls,
        chunk: pa.Array,
        indices: npt.NDArray[np.intp],
        value: npt.NDArray[Any],
    ) -> pa.Array:
        """
        Replace items selected with a set of positional indices.

        Analogous to pyarrow.compute.replace_with_mask, except that replacement
        positions are identified via indices rather than a mask.

        Parameters
        ----------
        chunk : pa.Array
        indices : npt.NDArray[np.intp]
        value : npt.NDArray[Any]
            Replacement value(s).

        Returns
        -------
        pa.Array
        """
        n = len(indices)

        if n == 0:
            return chunk

        start, stop = indices[[0, -1]]

        if (stop - start) == (n - 1):
            # fast path for a contiguous set of indices
            arrays = [
                chunk[:start],
                pa.array(value, type=chunk.type),
                chunk[stop + 1 :],
            ]
            arrays = [arr for arr in arrays if len(arr)]
            if len(arrays) == 1:
                return arrays[0]
            return pa.concat_arrays(arrays)

        mask = np.zeros(len(chunk), dtype=np.bool_)
        mask[indices] = True

        if pa_version_under5p0:
            arr = chunk.to_numpy(zero_copy_only=False)
            arr[mask] = value
            return pa.array(arr, type=chunk.type)

        if isna(value).all():
            return pc.if_else(mask, None, chunk)

        return pc.replace_with_mask(chunk, mask, value)
