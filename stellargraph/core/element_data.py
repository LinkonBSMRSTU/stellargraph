# -*- coding: utf-8 -*-
#
# Copyright 2017-2020 Data61, CSIRO
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
import itertools
from collections import defaultdict

import numpy as np
import pandas as pd
import scipy.sparse as sps

from ..globalvar import SOURCE, TARGET, WEIGHT
from .validation import require_dataframe_has_columns, comma_sep


class ExternalIdIndex:
    """
    An ExternalIdIndex maps between "external IDs" and "integer locations" or "internal locations"
    (ilocs).

    It is designed to allow handling only efficient integers internally, but easily convert between
    them and the user-facing IDs.
    """

    def __init__(self, ids):
        self._index = pd.Index(ids)
        self._dtype = np.min_scalar_type(len(self._index))

        if not self._index.is_unique:
            # had some duplicated IDs, which is an error
            duplicated = self._index[self._index.duplicated()].unique()
            raise ValueError(
                f"expected IDs to appear once, found some that appeared more: {comma_sep(duplicated)}"
            )

    @property
    def pandas_index(self) -> pd.Index:
        """
        Return a collection of all the elements contained in this index.
        """
        return self._index

    def __len__(self):
        return len(self._index)

    def contains_external(self, id):
        """
        Whether the external ID is indexed by this ``ExternalIdIndex``.
        """
        return id in self._index

    def is_valid(self, ilocs: np.ndarray) -> np.ndarray:
        """
        Flags the locations of all the ilocs that are valid (that is, where to_iloc didn't fail).
        """
        return (0 <= ilocs) & (ilocs < len(self))

    def require_valid(self, query_ids, ilocs: np.ndarray) -> np.ndarray:
        valid = self.is_valid(ilocs)

        if not valid.all():
            missing_values = np.asarray(query_ids)[~valid]

            if len(missing_values) == 1:
                raise KeyError(missing_values[0])

            raise KeyError(missing_values)

    def to_iloc(self, ids, smaller_type=True, strict=False) -> np.ndarray:
        """
        Convert external IDs ``ids`` to integer locations.

        Args:
            ids: a collection of external IDs
            smaller_type: if True, convert the ilocs to the smallest type that can hold them, to reduce storage
            strict: if True, check that all IDs are known and throw a KeyError if not

        Returns:
            A numpy array of the integer locations for each id that exists, with missing IDs
            represented by either the largest value of the dtype (if smaller_type is True) or -1 (if
            smaller_type is False)
        """
        internal_ids = self._index.get_indexer(ids)
        if strict:
            self.require_valid(ids, internal_ids)

        # reduce the storage required (especially useful if this is going to be stored rather than
        # just transient)
        if smaller_type:
            return internal_ids.astype(self._dtype)
        return internal_ids

    def from_iloc(self, internal_ids) -> np.ndarray:
        """
        Convert integer locations to their corresponding external ID.
        """
        return self._index.to_numpy()[internal_ids]


class ElementData:
    """
    An ``ElementData`` stores "shared" information about a set of a graph elements (nodes or
    edges). Elements of every type must have this information, such as the type itself or the
    source, target and weight for edges.

    It indexes these in terms of ilocs (see :class:`ExternalIdIndex`). The data is stored as columns
    of raw numpy arrays, because indexing such arrays is significantly (orders of magnitude) faster
    than indexing pandas dataframes, series or indices.

    Args:
        shared (pandas DataFrame): information for each element
        type_starts (list of tuple of type name, int): the starting iloc of the elements of each type within ``shared``
    """

    # any columns that must be in the `shared` dataframes passed to `__init__` (this should be
    # overridden by subclasses as appropriate)
    _SHARED_REQUIRED_COLUMNS = []

    def __init__(self, shared, type_starts):
        if not isinstance(shared, pd.DataFrame):
            raise TypeError(
                f"shared: expected pandas DataFrame, found {type(shared).__name__}"
            )

        require_dataframe_has_columns("shared", shared, self._SHARED_REQUIRED_COLUMNS)

        if not isinstance(type_starts, list):
            raise TypeError(
                f"type_starts: expected list, found {type(type_starts).__name__}"
            )

        type_ranges = {}
        type_stops = type_starts[1:] + [(None, len(shared))]
        consecutive_types = zip(type_starts, type_stops)
        for idx, ((type_name, start), (_, stop)) in enumerate(consecutive_types):
            if idx == 0 and start != 0:
                raise ValueError(
                    f"type_starts: expected first type ({type_name!r}) to start at index 0, found start {start}"
                )
            if start > stop:
                raise TypeError(
                    f"type_starts (for {type_name!r}): expected valid type range, found start ({start}) after stop ({stop})"
                )
            type_ranges[type_name] = range(start, stop)

        self._id_index = ExternalIdIndex(shared.index)
        self._columns = {name: data.to_numpy() for name, data in shared.iteritems()}

        # there's typically a small number of types, so we can map them down to a small integer type
        # (usually uint8) for minimum storage requirements
        all_types = [type_name for type_name, _ in type_starts]
        type_sizes = [len(type_ranges[type_name]) for type_name in all_types]

        self._type_index = ExternalIdIndex(all_types)
        self._type_column = self._type_index.to_iloc(all_types).repeat(type_sizes)
        self._type_element_ilocs = type_ranges

    def __len__(self) -> int:
        return len(self._id_index)

    def __contains__(self, item) -> bool:
        return self._id_index.contains_external(item)

    def _column(self, column) -> np.ndarray:
        return self._columns[column]

    @property
    def ids(self) -> ExternalIdIndex:
        """
        Returns:
             All of the IDs of these elements.
        """
        return self._id_index

    @property
    def types(self) -> ExternalIdIndex:
        """
        Returns:
            All the type names of these elements.
        """
        return self._type_index

    def type_range(self, type_name):
        """
        Returns:
            A range over the ilocs of the given type name
        """
        return self._type_element_ilocs[type_name]

    @property
    def type_ilocs(self) -> np.ndarray:
        """
        Returns:
            A numpy array with the type of each element, stores as the raw iloc of that type.
        """
        return self._type_column

    def type_of_iloc(self, id_ilocs) -> np.ndarray:
        """
        Return the types of the ID(s).

        Args:
            id_ilocs: a "selector" based on the element ID integer locations

        Returns:
             A sequence of types, corresponding to each of the ID(s) integer locations
        """
        type_codes = self._type_column[id_ilocs]
        return self._type_index.from_iloc(type_codes)


class NodeData(ElementData):
    """
    Args:
        shared (pandas DataFrame): information for the nodes
        type_starts (list of tuple of type name, int): the starting iloc of the nodes of each type within ``shared``
        features (dict of type name to numpy array): a 2D numpy or scipy array of feature vectors for the nodes of each type
    """

    def __init__(self, shared, type_starts, features):
        super().__init__(shared, type_starts)
        if not isinstance(features, dict):
            raise TypeError(f"features: expected dict, found {type(features).__name__}")

        for key, data in features.items():
            if not isinstance(data, (np.ndarray, sps.spmatrix)):
                raise TypeError(
                    f"features[{key!r}]: expected numpy or scipy array, found {type(data).__name__}"
                )

            if len(data.shape) != 2:
                raise ValueError(
                    f"features[{key!r}]: expected 2 dimensions, found {len(data.shape)}"
                )

            rows, _columns = data.shape
            expected = len(self._type_element_ilocs[key])
            if rows != expected:
                raise ValueError(
                    f"features[{key!r}]: expected one feature per ID, found {expected} IDs and {rows} feature rows"
                )

        self._features = features

    def features_of_type(self, type_name) -> np.ndarray:
        """
        Returns all features for a given type.

        Args:
            type_name (hashable): the name of the type
        """
        return self._features[type_name]

    def features(self, type_name, id_ilocs) -> np.ndarray:
        """
        Return features for a set of IDs within a given type.

        Args:
            type_name (hashable): the name of the type for all of the IDs
            ids (iterable of IDs): a sequence of IDs of elements of type type_name

        Returns:
            A 2D numpy array, where the rows correspond to the ids
        """
        start = self._type_element_ilocs[type_name].start
        feature_ilocs = id_ilocs - start

        # FIXME: better error messages
        if (feature_ilocs < 0).any():
            # ids were < start, e.g. from an earlier type, or unknown (-1)
            raise ValueError("unknown IDs")

        try:
            return self._features[type_name][feature_ilocs, :]
        except IndexError:
            # some of the indices were too large (from a later type)
            raise ValueError("unknown IDs")

    def feature_info(self):
        """
        Returns:
             A dictionary of type_name to a tuple of an integer representing the size of the
             features of that type, and the dtype of the features.
        """
        return {
            type_name: (type_features.shape[1], type_features.dtype)
            for type_name, type_features in self._features.items()
        }


def _numpyise(d, dtype):
    return {k: np.array(v, dtype=dtype) for k, v in d.items()}


class LazyAdjLists:
    def __init__(self, init_index):
        self.init_index = init_index
        self.undirected = self.ins = self.outs = None

    def force_init(self):
        self.lookup(ins=True, outs=True)
        self.lookup(ins=True, outs=False)
        self.lookup(ins=False, outs=True)

    def lookup(self, *, ins, outs):
        if ins and outs:
            if self.undirected is None:
                self.undirected = self.init_index(ins=True, outs=True)
            return self.undirected
        if ins:
            if self.ins is None:
                self.ins = self.init_index(ins=True, outs=False)
            return self.ins
        if outs:
            if self.outs is None:
                self.outs = self.init_index(ins=False, outs=True)
            return self.outs

        raise ValueError(
            "expected at least one of 'ins' or 'outs' to be True, found neither"
        )


class EdgeData(ElementData):
    """
    Args:
        shared (pandas DataFrame): information for the edges
        type_starts (list of tuple of type name, int): the starting iloc of the edges of each type within ``shared``
        node_data (NodeData): node data containing node types and IDs
    """

    _SHARED_REQUIRED_COLUMNS = [SOURCE, TARGET, WEIGHT]

    def __init__(self, shared, type_starts, node_data):
        super().__init__(shared, type_starts)

        # cache these columns to avoid having to do more method and dict look-ups
        self.sources = self._column(SOURCE)
        self.targets = self._column(TARGET)
        self.weights = self._column(WEIGHT)
        self.node_data = node_data

        dtype = np.min_scalar_type(len(self.sources))
        self.dtype = dtype

        # These are lazily initialized, to only pay the (construction) time and memory cost when
        # actually using them
        self._adj_list = LazyAdjLists(self._init_adj_list)

        self._adj_list_by_other_node_type = LazyAdjLists(
            self._init_adj_list_by_other_node_type
        )

        # when there's no neighbors for something, an empty array should be returned; this uses a
        # tiny dtype to minimise unnecessary type promotion (e.g. if this is used with an int32
        # array, the result will still be int32).
        self._empty_ilocs = np.array([], dtype=np.uint8)

    def _init_adj_list(self, *, ins, outs):
        index = {}
        edge_iter = enumerate(zip(self.sources, self.targets))

        if ins and outs:
            for i, (src, tgt) in edge_iter:
                index.setdefault(tgt, []).append(i)
                if src != tgt:
                    index.setdefault(src, []).append(i)
        elif ins:
            for i, (src, tgt) in edge_iter:
                index.setdefault(tgt, []).append(i)
        elif outs:
            for i, (src, tgt) in edge_iter:
                index.setdefault(src, []).append(i)
        else:
            raise ValueError(
                "expected at least one of 'ins' or 'outs' to be True, found neither"
            )

        return _numpyise(index, self.dtype)

    def _init_adj_list_by_other_node_type(self, *, ins, outs):
        index = {}
        source_types = self.node_data.type_ilocs[self.sources]
        target_types = self.node_data.type_ilocs[self.targets]
        edge_iter = enumerate(
            zip(self.sources, self.targets, source_types, target_types)
        )

        if ins and outs:
            for i, (src, tgt, src_type, tgt_type) in edge_iter:
                index.setdefault(src_type, {}).setdefault(tgt, []).append(i)
                if src != tgt:
                    index.setdefault(tgt_type, {}).setdefault(src, []).append(i)
        elif ins:
            for i, (src, tgt, src_type, tgt_type) in edge_iter:
                index.setdefault(src_type, {}).setdefault(tgt, []).append(i)
        elif outs:
            for i, (src, tgt, src_type, tgt_type) in edge_iter:
                index.setdefault(tgt_type, {}).setdefault(src, []).append(i)
        else:
            raise ValueError(
                "expected at least one of 'ins' or 'outs' to be True, found neither"
            )

        return {k: _numpyise(v, self.dtype) for k, v in index.items()}

    def degrees(self, *, ins=True, outs=True):
        """
        Compute the degrees of every non-isolated node.

        Args:
            ins (bool): count incoming edges
            outs (bool): count outgoing edges

        Returns:
            The in-, out- or total (summed) degree of all non-isolated nodes as a numpy array (if
            ``ret`` is the return value, ``ret[i]`` is the degree of the node with iloc ``i``)
        """
        adj = self._adj_list.lookup(ins=ins, outs=outs)
        return defaultdict(int, ((key, len(value)) for key, value in adj.items()))

    def edge_ilocs(
        self, node_iloc, *, ins, outs, other_node_type_iloc=None
    ) -> np.ndarray:
        """
        Return the integer locations of the edges for the given node iloc

        Args:
            node_iloc: the iloc of the node


        Returns:
            The integer locations of the edges for the given node iloc.
        """

        if other_node_type_iloc is not None:
            return (
                self._adj_list_by_other_node_type.lookup(ins=ins, outs=outs)
                .get(other_node_type_iloc, {})
                .get(node_iloc, self._empty_ilocs)
            )
        else:
            return self._adj_list.lookup(ins=ins, outs=outs).get(
                node_iloc, self._empty_ilocs
            )
