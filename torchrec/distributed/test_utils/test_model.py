#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from dataclasses import dataclass
from typing import List, cast, Optional, Tuple, Any, Dict, Union

import torch
import torch.nn as nn
from torchrec.distributed.embedding_tower_sharding import (
    EmbeddingTowerSharder,
    EmbeddingTowerCollectionSharder,
)
from torchrec.distributed.embedding_types import EmbeddingTableConfig
from torchrec.distributed.embeddingbag import (
    EmbeddingBagSharder,
    EmbeddingBagCollectionSharder,
)
from torchrec.modules.embedding_configs import EmbeddingBagConfig, BaseEmbeddingConfig
from torchrec.modules.embedding_modules import EmbeddingBagCollection
from torchrec.modules.embedding_tower import EmbeddingTower, EmbeddingTowerCollection
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor, KeyedTensor
from torchrec.streamable import Pipelineable


@dataclass
class ModelInput(Pipelineable):
    float_features: torch.Tensor
    idlist_features: KeyedJaggedTensor
    idscore_features: KeyedJaggedTensor
    label: torch.Tensor

    @staticmethod
    def generate(
        batch_size: int,
        world_size: int,
        num_float_features: int,
        tables: Union[List[EmbeddingTableConfig], List[EmbeddingBagConfig]],
        weighted_tables: Union[List[EmbeddingTableConfig], List[EmbeddingBagConfig]],
        pooling_avg: int = 10,
    ) -> Tuple["ModelInput", List["ModelInput"]]:
        """
        Returns a global (single-rank training) batch
        and a list of local (multi-rank training) batches of world_size.
        """
        idlist_features = [
            feature for table in tables for feature in table.feature_names
        ]
        idscore_features = [
            feature for table in weighted_tables for feature in table.feature_names
        ]

        idlist_ind_ranges = [table.num_embeddings for table in tables]
        idscore_ind_ranges = [table.num_embeddings for table in weighted_tables]

        # Generate global batch.
        global_idlist_lengths = []
        global_idlist_indices = []
        global_idscore_lengths = []
        global_idscore_indices = []
        global_idscore_weights = []

        for ind_range in idlist_ind_ranges:
            lengths = torch.abs(
                torch.randn(batch_size * world_size) + pooling_avg
            ).int()
            num_indices = cast(int, torch.sum(lengths).item())
            indices = torch.randint(0, ind_range, (num_indices,))
            global_idlist_lengths.append(lengths)
            global_idlist_indices.append(indices)
        global_idlist_kjt = KeyedJaggedTensor(
            keys=idlist_features,
            values=torch.cat(global_idlist_indices),
            lengths=torch.cat(global_idlist_lengths),
        )

        for ind_range in idscore_ind_ranges:
            lengths = torch.abs(
                torch.randn(batch_size * world_size) + pooling_avg
            ).int()
            num_indices = cast(int, torch.sum(lengths).item())
            indices = torch.randint(0, ind_range, (num_indices,))
            weights = torch.rand((num_indices,))
            global_idscore_lengths.append(lengths)
            global_idscore_indices.append(indices)
            global_idscore_weights.append(weights)
        global_idscore_kjt = (
            KeyedJaggedTensor(
                keys=idscore_features,
                values=torch.cat(global_idscore_indices),
                lengths=torch.cat(global_idscore_lengths),
                weights=torch.cat(global_idscore_weights),
            )
            if global_idscore_indices
            else None
        )

        global_float = torch.rand((batch_size * world_size, num_float_features))
        global_label = torch.rand(batch_size * world_size)

        # Split global batch into local batches.
        local_inputs = []
        for r in range(world_size):
            local_idlist_lengths = []
            local_idlist_indices = []
            local_idscore_lengths = []
            local_idscore_indices = []
            local_idscore_weights = []

            for lengths, indices in zip(global_idlist_lengths, global_idlist_indices):
                local_idlist_lengths.append(
                    lengths[r * batch_size : (r + 1) * batch_size]
                )
                lengths_cumsum = [0] + lengths.view(world_size, -1).sum(dim=1).cumsum(
                    dim=0
                ).tolist()
                local_idlist_indices.append(
                    indices[lengths_cumsum[r] : lengths_cumsum[r + 1]]
                )

            for lengths, indices, weights in zip(
                global_idscore_lengths, global_idscore_indices, global_idscore_weights
            ):
                local_idscore_lengths.append(
                    lengths[r * batch_size : (r + 1) * batch_size]
                )
                lengths_cumsum = [0] + lengths.view(world_size, -1).sum(dim=1).cumsum(
                    dim=0
                ).tolist()
                local_idscore_indices.append(
                    indices[lengths_cumsum[r] : lengths_cumsum[r + 1]]
                )
                local_idscore_weights.append(
                    weights[lengths_cumsum[r] : lengths_cumsum[r + 1]]
                )

            local_idlist_kjt = KeyedJaggedTensor(
                keys=idlist_features,
                values=torch.cat(local_idlist_indices),
                lengths=torch.cat(local_idlist_lengths),
            )

            local_idscore_kjt = (
                KeyedJaggedTensor(
                    keys=idscore_features,
                    values=torch.cat(local_idscore_indices),
                    lengths=torch.cat(local_idscore_lengths),
                    weights=torch.cat(local_idscore_weights),
                )
                if local_idscore_indices
                else None
            )

            local_input = ModelInput(
                float_features=global_float[r * batch_size : (r + 1) * batch_size],
                idlist_features=local_idlist_kjt,
                idscore_features=local_idscore_kjt,
                label=global_label[r * batch_size : (r + 1) * batch_size],
            )
            local_inputs.append(local_input)

        return (
            ModelInput(
                float_features=global_float,
                idlist_features=global_idlist_kjt,
                idscore_features=global_idscore_kjt,
                label=global_label,
            ),
            local_inputs,
        )

    def to(self, device: torch.device, non_blocking: bool = False) -> "ModelInput":
        return ModelInput(
            float_features=self.float_features.to(
                device=device, non_blocking=non_blocking
            ),
            idlist_features=self.idlist_features.to(
                device=device, non_blocking=non_blocking
            ),
            # pyre-ignore [6]
            idscore_features=self.idscore_features.to(
                device=device, non_blocking=non_blocking
            )
            if self.idscore_features is not None
            else None,
            label=self.label.to(device=device, non_blocking=non_blocking),
        )

    def record_stream(self, stream: torch.cuda.streams.Stream) -> None:
        self.float_features.record_stream(stream)
        self.idlist_features.record_stream(stream)
        if self.idscore_features is not None:
            self.idscore_features.record_stream(stream)
        self.label.record_stream(stream)


class TestDenseArch(nn.Module):
    """
    Basic nn.Module for testing

    Args:
        device

    Call Args:
        dense_input: torch.Tensor

    Returns:
        KeyedTensor

    Example::

        TestDenseArch()
    """

    def __init__(
        self,
        num_float_features: int = 10,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        if device is None:
            device = torch.device("cpu")
        self.linear: nn.modules.Linear = nn.Linear(
            in_features=num_float_features, out_features=8, device=device
        )

    def forward(self, dense_input: torch.Tensor) -> torch.Tensor:
        return self.linear(dense_input)


class TestOverArch(nn.Module):
    """
    Basic nn.Module for testing

    Args:
        device

    Call Args:
        dense: torch.Tensor,
        sparse: KeyedTensor,

    Returns:
        torch.Tensor

    Example::

        TestOverArch()
    """

    def __init__(
        self,
        tables: List[EmbeddingBagConfig],
        weighted_tables: List[EmbeddingBagConfig],
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        if device is None:
            device = torch.device("cpu")
        self._features: List[str] = [
            feature for table in tables for feature in table.feature_names
        ]
        self._weighted_features: List[str] = [
            feature for table in weighted_tables for feature in table.feature_names
        ]
        in_features = (
            8
            + sum([table.embedding_dim * len(table.feature_names) for table in tables])
            + sum(
                [
                    table.embedding_dim * len(table.feature_names)
                    for table in weighted_tables
                ]
            )
        )
        self.linear: nn.modules.Linear = nn.Linear(
            in_features=in_features, out_features=16, device=device
        )

    def forward(
        self,
        dense: torch.Tensor,
        sparse: KeyedTensor,
    ) -> torch.Tensor:
        ret_list = []
        ret_list.append(dense)
        for feature_name in self._features:
            ret_list.append(sparse[feature_name])
        for feature_name in self._weighted_features:
            ret_list.append(sparse[feature_name])
        return self.linear(torch.cat(ret_list, dim=1))


class TestSparseArch(nn.Module):
    """
    Basic nn.Module for testing

    Args:
        tables
        device

    Call Args:
        features

    Returns:
        KeyedTensor
    """

    def __init__(
        self,
        tables: List[EmbeddingBagConfig],
        weighted_tables: List[EmbeddingBagConfig],
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        if device is None:
            device = torch.device("cpu")
        self.ebc: EmbeddingBagCollection = EmbeddingBagCollection(
            tables=tables,
            device=device,
        )
        self.weighted_ebc: EmbeddingBagCollection = EmbeddingBagCollection(
            tables=weighted_tables,
            is_weighted=True,
            device=device,
        )

    def forward(
        self, features: KeyedJaggedTensor, weighted_features: KeyedJaggedTensor
    ) -> KeyedTensor:
        ebc = self.ebc(features)
        w_ebc = self.weighted_ebc(weighted_features)
        return KeyedTensor(
            keys=ebc.keys() + w_ebc.keys(),
            length_per_key=ebc.length_per_key() + w_ebc.length_per_key(),
            values=torch.cat([ebc.values(), w_ebc.values()], dim=1),
        )


class TestSparseNNBase(nn.Module):
    """
    Base class for a SparseNN model.

    Args:
        tables: List[BaseEmbeddingConfig],
        weighted_tables: Optional[List[BaseEmbeddingConfig]],
        embedding_groups: Optional[Dict[str, List[str]]],
        dense_device: Optional[torch.device],
        sparse_device: Optional[torch.device],
    """

    def __init__(
        self,
        tables: List[BaseEmbeddingConfig],
        weighted_tables: Optional[List[BaseEmbeddingConfig]] = None,
        embedding_groups: Optional[Dict[str, List[str]]] = None,
        dense_device: Optional[torch.device] = None,
        sparse_device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        if dense_device is None:
            dense_device = torch.device("cpu")
        if sparse_device is None:
            sparse_device = torch.device("cpu")


class TestSparseNN(TestSparseNNBase):
    """
    Simple version of a SparseNN model.

    Args:
        tables: List[EmbeddingBagConfig],
        weighted_tables: Optional[List[EmbeddingBagConfig]],
        embedding_groups: Optional[Dict[str, List[str]]],
        dense_device: Optional[torch.device],
        sparse_device: Optional[torch.device],

    Call Args:
        input: ModelInput,

    Returns:
        torch.Tensor

    Example::

        TestSparseNN()
    """

    def __init__(
        self,
        tables: List[EmbeddingBagConfig],
        num_float_features: int = 10,
        weighted_tables: Optional[List[EmbeddingBagConfig]] = None,
        embedding_groups: Optional[Dict[str, List[str]]] = None,
        dense_device: Optional[torch.device] = None,
        sparse_device: Optional[torch.device] = None,
    ) -> None:
        super().__init__(
            tables=cast(List[BaseEmbeddingConfig], tables),
            weighted_tables=cast(Optional[List[BaseEmbeddingConfig]], weighted_tables),
            embedding_groups=embedding_groups,
            dense_device=dense_device,
            sparse_device=sparse_device,
        )
        if weighted_tables is None:
            weighted_tables = []

        self.dense = TestDenseArch(num_float_features, dense_device)
        self.sparse = TestSparseArch(tables, weighted_tables, sparse_device)
        self.over = TestOverArch(tables, weighted_tables, dense_device)

    def forward(
        self,
        input: ModelInput,
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        dense_r = self.dense(input.float_features)
        sparse_r = self.sparse(input.idlist_features, input.idscore_features)
        over_r = self.over(dense_r, sparse_r)
        pred = torch.sigmoid(torch.mean(over_r, dim=1))
        if self.training:
            return (
                torch.nn.functional.binary_cross_entropy_with_logits(pred, input.label),
                pred,
            )
        else:
            return pred


class TestTowerInteraction(nn.Module):
    """
    Basic nn.Module for testing

    Args:
        tables: List[EmbeddingBagConfig],
        device: Optional[torch.device],

    Call Args:
        sparse: KeyedTensor,

    Returns:
        torch.Tensor

    Example:
        >>> TestOverArch()
    """

    def __init__(
        self,
        tables: List[EmbeddingBagConfig],
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        if device is None:
            device = torch.device("cpu")
        self._features: List[str] = [
            feature for table in tables for feature in table.feature_names
        ]
        in_features = sum(
            [table.embedding_dim * len(table.feature_names) for table in tables]
        )
        self.linear: nn.modules.Linear = nn.Linear(
            in_features=in_features,
            out_features=in_features,
            device=device,
        )

    def forward(
        self,
        sparse: KeyedTensor,
    ) -> torch.Tensor:
        ret_list = []
        for feature_name in self._features:
            ret_list.append(sparse[feature_name])
        return self.linear(torch.cat(ret_list, dim=1))


class TestTowerSparseNN(TestSparseNNBase):
    """
    Simple version of a SparseNN model.

    Args:
        tables: List[EmbeddingBagConfig],
        embedding_groups: Optional[Dict[str, List[str]]],
        dense_device: Optional[torch.device],
        sparse_device: Optional[torch.device],

    Call Args:
        input: ModelInput,

    Returns:
        torch.Tensor

    Example:
        >>> TestSparseNN()
    """

    def __init__(
        self,
        tables: List[EmbeddingBagConfig],
        num_float_features: int = 10,
        weighted_tables: Optional[List[EmbeddingBagConfig]] = None,
        embedding_groups: Optional[Dict[str, List[str]]] = None,
        dense_device: Optional[torch.device] = None,
        sparse_device: Optional[torch.device] = None,
    ) -> None:
        super().__init__(
            tables=cast(List[BaseEmbeddingConfig], tables),
            weighted_tables=cast(Optional[List[BaseEmbeddingConfig]], weighted_tables),
            embedding_groups=embedding_groups,
            dense_device=dense_device,
            sparse_device=sparse_device,
        )

        self.dense = TestDenseArch(num_float_features, dense_device)

        # TODO: after adding planner support for tower_module, we can random assign
        # tables to towers, but for now the match planner default layout
        self.tower_0 = EmbeddingTower(
            embedding_module=EmbeddingBagCollection(tables=[tables[0], tables[2]]),
            interaction_module=TestTowerInteraction(tables=[tables[0], tables[2]]),
        )
        self.tower_1 = EmbeddingTower(
            embedding_module=EmbeddingBagCollection(tables=[tables[1]]),
            interaction_module=TestTowerInteraction(tables=[tables[1]]),
        )
        self.tower_2 = EmbeddingTower(
            embedding_module=EmbeddingBagCollection(
                # pyre-ignore [16]
                tables=[weighted_tables[0]],
                is_weighted=True,
            ),
            interaction_module=TestTowerInteraction(tables=[weighted_tables[0]]),
        )
        self.over = nn.Linear(
            in_features=8
            # pyre-ignore [16]
            + self.tower_0.interaction.linear.out_features
            # pyre-ignore [16]
            + self.tower_1.interaction.linear.out_features
            # pyre-ignore [16]
            + self.tower_2.interaction.linear.out_features,
            out_features=16,
            device=dense_device,
        )

    def forward(
        self,
        input: ModelInput,
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        dense_r = self.dense(input.float_features)
        tower_0_r = self.tower_0(input.idlist_features)
        tower_1_r = self.tower_1(input.idlist_features)
        tower_2_r = self.tower_2(input.idscore_features)

        sparse_r = torch.cat([tower_0_r, tower_1_r, tower_2_r], dim=1)
        over_r = self.over(torch.cat([dense_r, sparse_r], dim=1))
        pred = torch.sigmoid(torch.mean(over_r, dim=1))
        if self.training:
            return (
                torch.nn.functional.binary_cross_entropy_with_logits(pred, input.label),
                pred,
            )
        else:
            return pred


class TestTowerCollectionSparseNN(TestSparseNNBase):
    """
    Simple version of a SparseNN model.

    Constructor Args:
        tables: List[EmbeddingBagConfig],
        embedding_groups: Optional[Dict[str, List[str]]],
        dense_device: Optional[torch.device],
        sparse_device: Optional[torch.device],

    Call Args:
        input: ModelInput,

    Returns:
        torch.Tensor

    Example:
        >>> TestSparseNN()
    """

    def __init__(
        self,
        tables: List[EmbeddingBagConfig],
        num_float_features: int = 10,
        weighted_tables: Optional[List[EmbeddingBagConfig]] = None,
        embedding_groups: Optional[Dict[str, List[str]]] = None,
        dense_device: Optional[torch.device] = None,
        sparse_device: Optional[torch.device] = None,
    ) -> None:
        super().__init__(
            tables=cast(List[BaseEmbeddingConfig], tables),
            weighted_tables=cast(Optional[List[BaseEmbeddingConfig]], weighted_tables),
            embedding_groups=embedding_groups,
            dense_device=dense_device,
            sparse_device=sparse_device,
        )

        self.dense = TestDenseArch(num_float_features, dense_device)
        # TODO: after adding planner support for tower_module, we can random assign
        # tables to towers, but for now the match planner default layout
        tower_0 = EmbeddingTower(
            embedding_module=EmbeddingBagCollection(tables=[tables[0], tables[2]]),
            interaction_module=TestTowerInteraction(tables=[tables[0], tables[2]]),
        )
        tower_1 = EmbeddingTower(
            embedding_module=EmbeddingBagCollection(tables=[tables[1]]),
            interaction_module=TestTowerInteraction(tables=[tables[1]]),
        )
        tower_2 = EmbeddingTower(
            embedding_module=EmbeddingBagCollection(
                # pyre-ignore [16]
                tables=[weighted_tables[0]],
                is_weighted=True,
            ),
            interaction_module=TestTowerInteraction(tables=[weighted_tables[0]]),
        )
        self.tower_arch = EmbeddingTowerCollection(towers=[tower_0, tower_1, tower_2])
        self.over = nn.Linear(
            in_features=8
            # pyre-ignore [16]
            + tower_0.interaction.linear.out_features
            # pyre-ignore [16]
            + tower_1.interaction.linear.out_features
            # pyre-ignore [16]
            + tower_2.interaction.linear.out_features,
            out_features=16,
            device=dense_device,
        )

    def forward(
        self,
        input: ModelInput,
    ) -> Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        dense_r = self.dense(input.float_features)
        sparse_r = self.tower_arch(input.idlist_features, input.idscore_features)
        over_r = self.over(torch.cat([dense_r, sparse_r], dim=1))
        pred = torch.sigmoid(torch.mean(over_r, dim=1))
        if self.training:
            return (
                torch.nn.functional.binary_cross_entropy_with_logits(pred, input.label),
                pred,
            )
        else:
            return pred


class TestEBCSharder(EmbeddingBagCollectionSharder):
    def __init__(
        self,
        sharding_type: str,
        kernel_type: str,
        fused_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        if fused_params is None:
            fused_params = {}
        super().__init__(fused_params)
        self._sharding_type = sharding_type
        self._kernel_type = kernel_type

    """
    Restricts sharding to single type only.
    """

    def sharding_types(self, compute_device_type: str) -> List[str]:
        return [self._sharding_type]

    """
    Restricts to single impl.
    """

    def compute_kernels(
        self, sharding_type: str, compute_device_type: str
    ) -> List[str]:
        return [self._kernel_type]

    @property
    def fused_params(self) -> Optional[Dict[str, Any]]:
        return self._fused_params


class TestEBSharder(EmbeddingBagSharder):
    def __init__(
        self, sharding_type: str, kernel_type: str, fused_params: Dict[str, Any]
    ) -> None:
        super().__init__(fused_params)
        self._sharding_type = sharding_type
        self._kernel_type = kernel_type

    """
    Restricts sharding to single type only.
    """

    def sharding_types(self, compute_device_type: str) -> List[str]:
        return [self._sharding_type]

    """
    Restricts to single impl.
    """

    def compute_kernels(
        self, sharding_type: str, compute_device_type: str
    ) -> List[str]:
        return [self._kernel_type]

    @property
    def fused_params(self) -> Optional[Dict[str, Any]]:
        return self._fused_params


class TestETSharder(EmbeddingTowerSharder):
    def __init__(
        self, sharding_type: str, kernel_type: str, fused_params: Dict[str, Any]
    ) -> None:
        super().__init__(fused_params)
        self._sharding_type = sharding_type
        self._kernel_type = kernel_type

    """
    Restricts sharding to single type only.
    """

    def sharding_types(self, compute_device_type: str) -> List[str]:
        return [self._sharding_type]

    """
    Restricts to single impl.
    """

    def compute_kernels(
        self, sharding_type: str, compute_device_type: str
    ) -> List[str]:
        return [self._kernel_type]

    @property
    def fused_params(self) -> Optional[Dict[str, Any]]:
        return self._fused_params


class TestETCSharder(EmbeddingTowerCollectionSharder):
    def __init__(
        self, sharding_type: str, kernel_type: str, fused_params: Dict[str, Any]
    ) -> None:
        super().__init__(fused_params)
        self._sharding_type = sharding_type
        self._kernel_type = kernel_type

    """
    Restricts sharding to single type only.
    """

    def sharding_types(self, compute_device_type: str) -> List[str]:
        return [self._sharding_type]

    """
    Restricts to single impl.
    """

    def compute_kernels(
        self, sharding_type: str, compute_device_type: str
    ) -> List[str]:
        return [self._kernel_type]

    @property
    def fused_params(self) -> Optional[Dict[str, Any]]:
        return self._fused_params
