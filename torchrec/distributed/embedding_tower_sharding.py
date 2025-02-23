#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from collections import OrderedDict
from typing import (
    TypeVar,
    Dict,
    Optional,
    Any,
    Type,
    List,
    Iterator,
    Tuple,
    Set,
    cast,
)

import torch
import torch.distributed as dist
from torch import nn
from torch.nn.parallel import DistributedDataParallel
from torchrec.distributed.comm import intra_and_cross_node_pg
from torchrec.distributed.dist_data import (
    PooledEmbeddingsAllToAll,
    PooledEmbeddingsAwaitable,
)
from torchrec.distributed.embedding import EmbeddingCollectionSharder
from torchrec.distributed.embedding_sharding import (
    SparseFeaturesAllToAll,
    SparseFeaturesListAwaitable,
)
from torchrec.distributed.embedding_types import (
    BaseEmbeddingSharder,
    SparseFeatures,
    SparseFeaturesList,
)
from torchrec.distributed.embeddingbag import EmbeddingBagCollectionSharder
from torchrec.distributed.types import (
    ParameterSharding,
    ShardingEnv,
    ShardedModule,
    Awaitable,
    ShardedModuleContext,
    ShardingType,
    LazyAwaitable,
)
from torchrec.distributed.utils import append_prefix
from torchrec.modules.embedding_modules import (
    EmbeddingBagCollection,
    EmbeddingCollection,
)
from torchrec.modules.embedding_tower import (
    EmbeddingTower,
    EmbeddingTowerCollection,
    tower_input_params,
)
from torchrec.optim.fused import FusedOptimizerModule
from torchrec.optim.keyed import KeyedOptimizer, CombinedOptimizer
from torchrec.sparse.jagged_tensor import (
    KeyedJaggedTensor,
)

M = TypeVar("M", bound=nn.Module)


def _replace_sharding_with_intra_node(
    table_name_to_parameter_sharding: Dict[str, ParameterSharding], local_size: int
) -> None:
    for _, value in table_name_to_parameter_sharding.items():
        if value.sharding_type == ShardingType.TABLE_ROW_WISE.value:
            value.sharding_type = ShardingType.ROW_WISE.value
        elif value.sharding_type == ShardingType.TABLE_COLUMN_WISE.value:
            value.sharding_type = ShardingType.COLUMN_WISE.value
        else:
            raise ValueError(f"Sharding type not supported {value.sharding_type}")
        if value.ranks:
            value.ranks = [rank % local_size for rank in value.ranks]
        if value.sharding_spec:
            # pyre-ignore [6, 16]
            for (shard, rank) in zip(value.sharding_spec.shards, value.ranks):
                shard.placement._rank = rank


class TowerLazyAwaitable(LazyAwaitable[torch.Tensor]):
    def __init__(
        self,
        awaitable: PooledEmbeddingsAwaitable,
    ) -> None:
        super().__init__()
        self._awaitable = awaitable

    def _wait_impl(self) -> torch.Tensor:
        return self._awaitable.wait()


class ShardedEmbeddingTower(
    ShardedModule[
        SparseFeaturesList,
        torch.Tensor,
        torch.Tensor,
    ],
    FusedOptimizerModule,
):
    def __init__(
        self,
        module: EmbeddingTower,
        table_name_to_parameter_sharding: Dict[str, ParameterSharding],
        embedding_sharder: BaseEmbeddingSharder[nn.Module],
        kjt_features: List[str],
        wkjt_features: List[str],
        env: ShardingEnv,
        fused_params: Optional[Dict[str, Any]] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()
        intra_pg, cross_pg = intra_and_cross_node_pg(device)
        # pyre-ignore [11]
        self._intra_pg: Optional[dist.ProcessGroup] = intra_pg
        self._cross_pg: Optional[dist.ProcessGroup] = cross_pg
        self._device = device
        self._output_dist: Optional[PooledEmbeddingsAllToAll] = None
        self._cross_pg_global_batch_size: int = 0
        self._cross_pg_world_size: int = dist.get_world_size(self._cross_pg)

        self._has_uninitialized_output_dist = True

        # make sure all sharding on single physical node
        devices_per_host = dist.get_world_size(intra_pg)
        tower_devices = set()
        for sharding in table_name_to_parameter_sharding.values():
            # pyre-ignore [6]
            tower_devices.update(sharding.ranks)
        host = {tower_device // devices_per_host for tower_device in tower_devices}
        assert len(host) == 1, f"{tower_devices}, {table_name_to_parameter_sharding}"
        self._tower_node: int = next(iter(host))
        self._active_device: bool = {dist.get_rank() // devices_per_host} == host

        # input_dist
        self._kjt_feature_names: List[str] = kjt_features
        self._wkjt_feature_names: List[str] = wkjt_features
        self._has_uninitialized_input_dist: bool = True
        self._cross_dist: nn.Module = nn.Module()
        self._kjt_features_order: List[int] = []
        self._wkjt_features_order: List[int] = []
        self._has_kjt_features_permute: bool = False
        self._has_wkjt_features_permute: bool = False

        self.embedding: Optional[nn.Module] = None
        self.interaction: Optional[nn.Module] = None
        if self._active_device:
            _replace_sharding_with_intra_node(
                table_name_to_parameter_sharding, dist.get_world_size(self._intra_pg)
            )
            intra_env: ShardingEnv = ShardingEnv(
                world_size=dist.get_world_size(self._intra_pg),
                rank=dist.get_rank(self._intra_pg),
                pg=self._intra_pg,
            )
            # shard embedding module
            self.embedding = embedding_sharder.shard(
                module.embedding,
                table_name_to_parameter_sharding,
                intra_env,
                device,
            )
            # Hiearcherial DDP
            self.interaction = DistributedDataParallel(
                module=module.interaction.to(self._device),
                device_ids=[self._device],
                process_group=self._intra_pg,
                gradient_as_bucket_view=True,
                broadcast_buffers=False,
            )

    def _create_input_dist(
        self,
        kjt_feature_names: List[str],
        wkjt_feature_names: List[str],
    ) -> None:
        if self._kjt_feature_names != kjt_feature_names:
            self._has_kjt_features_permute = True
            for f in self._kjt_feature_names:
                self._kjt_features_order.append(kjt_feature_names.index(f))
            self.register_buffer(
                "_kjt_features_order_tensor",
                torch.tensor(
                    self._kjt_features_order, device=self._device, dtype=torch.int32
                ),
            )

        if self._wkjt_feature_names != wkjt_feature_names:
            self._has_wkjt_features_permute = True
            for f in self._wkjt_feature_names:
                self._wkjt_features_order.append(wkjt_feature_names.index(f))
            self.register_buffer(
                "_wkjt_features_order_tensor",
                torch.tensor(
                    self._wkjt_features_order, device=self._device, dtype=torch.int32
                ),
            )

        node_count = dist.get_world_size(self._cross_pg)
        kjt_features_per_node = [
            len(self._kjt_feature_names) if node == self._tower_node else 0
            for node in range(node_count)
        ]
        wkjt_features_per_node = [
            len(self._wkjt_feature_names) if node == self._tower_node else 0
            for node in range(node_count)
        ]
        self._cross_dist = SparseFeaturesAllToAll(
            self._cross_pg,
            kjt_features_per_node,
            wkjt_features_per_node,
            self._device,
        )

    # pyre-ignore [14]
    def input_dist(
        self,
        ctx: ShardedModuleContext,
        features: KeyedJaggedTensor,
        optional_features: Optional[KeyedJaggedTensor] = None,
    ) -> Awaitable[SparseFeaturesList]:

        # optional_features are populated only if both kjt and weighted kjt present in tower
        if self._wkjt_feature_names and self._kjt_feature_names:
            kjt_features = features
            wkjt_features = optional_features
        elif self._wkjt_feature_names:
            kjt_features = None
            wkjt_features = features
        else:
            kjt_features = features
            wkjt_features = None

        if self._has_uninitialized_input_dist:
            self._cross_pg_global_batch_size = (
                features.stride() * self._cross_pg_world_size
            )
            self._create_input_dist(
                kjt_features.keys() if kjt_features else [],
                wkjt_features.keys() if wkjt_features else [],
            )
            self._has_uninitialized_input_dist = False

        with torch.no_grad():
            if self._has_kjt_features_permute:
                # pyre-ignore [16]
                kjt_features = kjt_features.permute(
                    self._kjt_features_order,
                    self._kjt_features_order_tensor,
                )
            if self._has_wkjt_features_permute:
                wkjt_features = wkjt_features.permute(
                    self._wkjt_features_order,
                    self._wkjt_features_order_tensor,
                )
            tensor_awaitable = self._cross_dist(
                SparseFeatures(
                    id_list_features=kjt_features,
                    id_score_list_features=wkjt_features,
                )
            )
            return SparseFeaturesListAwaitable([tensor_awaitable.wait()])

    def compute(
        self, ctx: ShardedModuleContext, dist_input: SparseFeaturesList
    ) -> torch.Tensor:
        kjt_features = dist_input[0].id_list_features
        wkjt_features = dist_input[0].id_score_list_features

        if self._active_device:
            if kjt_features and wkjt_features:
                # pyre-ignore [29]
                embeddings = self.embedding(kjt_features, wkjt_features)
            elif wkjt_features:
                # pyre-ignore [29]
                embeddings = self.embedding(wkjt_features)
            else:
                # pyre-ignore [29]
                embeddings = self.embedding(kjt_features)
            # pyre-ignore [29]
            output = self.interaction(embeddings)
        else:
            output = torch.empty(
                [self._cross_pg_global_batch_size, 0],
                device=self._device,
                requires_grad=True,
            )
        return output

    def _create_output_dist(
        self, ctx: ShardedModuleContext, output: torch.Tensor
    ) -> None:
        # Determine the output_dist splits and the all_to_all output size
        assert len(output.shape) == 2
        local_dim_sum = torch.tensor(
            [
                output.shape[1],
            ],
            dtype=torch.int64,
            device=self._device,
        )
        dim_sum_per_rank = [
            torch.zeros(
                1,
                dtype=torch.int64,
                device=self._device,
            )
            for i in range(dist.get_world_size(self._cross_pg))
        ]
        dist.all_gather(
            dim_sum_per_rank,
            local_dim_sum,
            group=self._cross_pg,
        )
        dim_sum_per_rank = [x.item() for x in dim_sum_per_rank]
        self._output_dist = PooledEmbeddingsAllToAll(
            pg=self._cross_pg, dim_sum_per_rank=dim_sum_per_rank, device=self._device
        )

    def output_dist(
        self, ctx: ShardedModuleContext, output: torch.Tensor
    ) -> LazyAwaitable[torch.Tensor]:
        if self._has_uninitialized_output_dist:
            self._create_output_dist(ctx, output)
            self._has_uninitialized_output_dist = False
        # pyre-ignore [29]
        return TowerLazyAwaitable(self._output_dist(output))

    # pyre-ignore [14]
    def state_dict(
        self,
        destination: Optional[Dict[str, Any]] = None,
        prefix: str = "",
        keep_vars: bool = False,
    ) -> Dict[str, Any]:
        if destination is None:
            destination = OrderedDict()
            # pyre-ignore [16]
            destination._metadata = OrderedDict()
        if self._active_device:
            # pyre-ignore [16]
            self.embedding.state_dict(destination, prefix + "embedding.", keep_vars)
            # pyre-ignore [16]
            self.interaction.module.state_dict(
                destination, prefix + "interaction.", keep_vars
            )
        return destination

    @property
    def fused_optimizer(self) -> KeyedOptimizer:
        if self.embedding:
            # pyre-ignore [7]
            return self.embedding.fused_optimizer
        else:
            return CombinedOptimizer([])

    def named_parameters(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, nn.Parameter]]:
        if self._active_device:
            # pyre-ignore[16]
            yield from self.embedding.named_parameters(
                append_prefix(prefix, "embedding"), recurse
            )
            # pyre-ignore[16]
            yield from self.interaction.module.named_parameters(
                append_prefix(prefix, "interaction"), recurse
            )
        else:
            yield from ()

    def named_buffers(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        if self._active_device:
            # pyre-ignore[16]
            yield from self.embedding.named_buffers(
                append_prefix(prefix, "embedding"), recurse
            )
            # pyre-ignore[16]
            yield from self.interaction.module.named_buffers(
                append_prefix(prefix, "interaction"), recurse
            )
        yield from ()

    def sparse_grad_parameter_names(
        self,
        destination: Optional[List[str]] = None,
        prefix: str = "",
    ) -> List[str]:
        destination = [] if destination is None else destination
        if self._active_device:
            # pyre-ignore[16]
            self.embedding.sparse_grad_parameter_names(
                destination, append_prefix(prefix, "embedding")
            )
        return destination

    def sharded_parameter_names(self, prefix: str = "") -> Iterator[str]:
        if self._active_device:
            # pyre-ignore[16]
            yield from self.embedding.sharded_parameter_names(
                append_prefix(prefix, "embedding")
            )
            # pyre-ignore[16]
            for name, _ in self.interaction.module.named_parameters(
                append_prefix(prefix, "interaction")
            ):
                yield name
        else:
            yield from ()

    def named_modules(
        self,
        memo: Optional[Set[nn.Module]] = None,
        prefix: str = "",
        remove_duplicate: bool = True,
    ) -> Iterator[Tuple[str, nn.Module]]:
        yield from [(prefix, self)]


class ShardedEmbeddingTowerCollection(
    ShardedModule[
        SparseFeaturesList,
        torch.Tensor,
        torch.Tensor,
    ],
    FusedOptimizerModule,
):
    def __init__(
        self,
        module: EmbeddingTowerCollection,
        table_name_to_parameter_sharding: Dict[str, ParameterSharding],
        tower_sharder: BaseEmbeddingSharder[EmbeddingTower],
        env: ShardingEnv,
        fused_params: Optional[Dict[str, Any]] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()

        intra_pg, cross_pg = intra_and_cross_node_pg(device)
        self._intra_pg: Optional[dist.ProcessGroup] = intra_pg
        self._cross_pg: Optional[dist.ProcessGroup] = cross_pg
        self._cross_pg_world_size: int = dist.get_world_size(self._cross_pg)
        self._intra_pg_world_size: int = dist.get_world_size(self._intra_pg)
        self._device = device
        self._tower_id: int = dist.get_rank() // self._intra_pg_world_size
        self._output_dist: Optional[PooledEmbeddingsAllToAll] = None
        self._cross_pg_global_batch_size: int = 0
        self._is_weighted: bool = False
        self._has_uninitialized_input_dist: bool = True
        self._has_uninitialized_output_dist: bool = True
        self._kjt_features_order: List[int] = []
        self._wkjt_features_order: List[int] = []
        self._kjt_feature_names: List[str] = []
        self._wkjt_feature_names: List[str] = []
        self._kjt_num_features_per_pt: List[int] = []
        self._wkjt_num_features_per_pt: List[int] = []
        self._has_kjt_features_permute: bool = False
        self._has_wkjt_features_permute: bool = False
        self.embeddings: nn.ModuleDict = nn.ModuleDict()
        self.interactions: nn.ModuleDict = nn.ModuleDict()
        self.ctxs: List[ShardedModuleContext] = []
        self.input_dist_params: List[Tuple[bool, bool]] = []
        self._cross_dist: nn.Module = nn.Module()

        # groups parameter sharding into physical towers
        tables_per_pt: List[Set[str]] = [
            set() for _ in range(self._cross_pg_world_size)
        ]
        [
            tables_per_pt[i].add(k)
            for i in range(self._cross_pg_world_size)
            for k, v in table_name_to_parameter_sharding.items()
            # pyre-ignore [16]
            if v.ranks[0] // self._intra_pg_world_size == i
        ]

        # create mapping of logical towers to physical towers
        tables_per_lt: List[Set[str]] = []
        for tower in module.towers:
            tables_per_lt.append(set(tower_sharder.shardable_parameters(tower).keys()))

        logical_to_physical_order: List[List[int]] = [
            [] for _ in range(self._cross_pg_world_size)
        ]
        feature_names_by_pt: List[Tuple[List[str], List[str]]] = [
            ([], []) for _ in range(self._cross_pg_world_size)
        ]

        for i, pt_tables in enumerate(tables_per_pt):
            found = False
            for j, lt_tables in enumerate(tables_per_lt):
                if lt_tables.issubset(pt_tables):
                    logical_to_physical_order[i].append(j)
                    found = True
            if not found:
                raise RuntimeError(
                    f"Could not find any towers with features: {pt_tables}"
                )

        for pt_index, lt_on_pt in enumerate(logical_to_physical_order):
            for lt_index in lt_on_pt:
                # pyre-ignore [16]
                kjt_features, wkjt_features = tower_sharder.embedding_feature_names(
                    module.towers[lt_index]
                )
                feature_names_by_pt[pt_index][0].extend(kjt_features)
                feature_names_by_pt[pt_index][1].extend(wkjt_features)

        for kjt_names, wkjt_names in feature_names_by_pt:
            self._kjt_feature_names.extend(kjt_names)
            self._wkjt_feature_names.extend(wkjt_names)
            self._kjt_num_features_per_pt.append(len(kjt_names))
            self._wkjt_num_features_per_pt.append(len(wkjt_names))

        local_towers: List[Tuple[str, EmbeddingTower]] = [
            (str(i), tower)
            for i, tower in enumerate(module.towers)
            if i in logical_to_physical_order[self._tower_id]
        ]

        if local_towers:
            _replace_sharding_with_intra_node(
                table_name_to_parameter_sharding, dist.get_world_size(self._intra_pg)
            )
            intra_env: ShardingEnv = ShardingEnv(
                world_size=dist.get_world_size(self._intra_pg),
                rank=dist.get_rank(self._intra_pg),
                pg=self._intra_pg,
            )
            for i, tower in local_towers:
                # pyre-ignore [16]
                self.embeddings[i] = tower_sharder.embedding_sharder(tower).shard(
                    tower.embedding,
                    table_name_to_parameter_sharding,
                    intra_env,
                    device,
                )
                self.ctxs.append(self.embeddings[i].create_context())
                self.input_dist_params.append(tower_input_params(tower.embedding))
                # Hiearcherial DDP
                self.interactions[i] = DistributedDataParallel(
                    module=tower.interaction.to(self._device),
                    device_ids=[self._device],
                    process_group=self._intra_pg,
                    gradient_as_bucket_view=True,
                    broadcast_buffers=False,
                    static_graph=True,
                )

        # Setup output_dist for quantized comms
        embedding_dists = []
        for embedding in self.embeddings.values():
            embedding_dists.extend(embedding._input_dists)
        self._output_dists: nn.ModuleList = nn.ModuleList(embedding_dists)

    def _create_input_dist(
        self,
        kjt_feature_names: List[str],
        wkjt_feature_names: List[str],
    ) -> None:

        if self._kjt_feature_names != kjt_feature_names:
            self._has_kjt_features_permute = True
            for f in self._kjt_feature_names:
                self._kjt_features_order.append(kjt_feature_names.index(f))
            self.register_buffer(
                "_kjt_features_order_tensor",
                torch.tensor(
                    self._kjt_features_order, device=self._device, dtype=torch.int32
                ),
            )

        if self._wkjt_feature_names != wkjt_feature_names:
            self._has_wkjt_features_permute = True
            for f in self._wkjt_feature_names:
                self._wkjt_features_order.append(wkjt_feature_names.index(f))
            self.register_buffer(
                "_wkjt_features_order_tensor",
                torch.tensor(
                    self._wkjt_features_order, device=self._device, dtype=torch.int32
                ),
            )
        self._cross_dist = SparseFeaturesAllToAll(
            self._cross_pg,
            self._kjt_num_features_per_pt,
            self._wkjt_num_features_per_pt,
            self._device,
        )

    # pyre-ignore [14]
    def input_dist(
        self,
        ctx: ShardedModuleContext,
        kjt_features: KeyedJaggedTensor,
        wkjt_features: KeyedJaggedTensor,
    ) -> Awaitable[SparseFeaturesList]:

        if self._has_uninitialized_input_dist:
            self._cross_pg_global_batch_size = (
                kjt_features.stride() * self._cross_pg_world_size
            )
            self._create_input_dist(
                kjt_features.keys() if kjt_features else [],
                wkjt_features.keys() if wkjt_features else [],
            )
            self._has_uninitialized_input_dist = False
        with torch.no_grad():
            if self._has_kjt_features_permute:
                kjt_features = kjt_features.permute(
                    self._kjt_features_order,
                    cast(torch.Tensor, self._kjt_features_order_tensor),
                )
            if self._has_wkjt_features_permute:
                wkjt_features = wkjt_features.permute(
                    self._wkjt_features_order,
                    cast(torch.Tensor, self._wkjt_features_order_tensor),
                )
            sparse_features_awaitable = self._cross_dist(
                SparseFeatures(
                    id_list_features=kjt_features,
                    id_score_list_features=wkjt_features,
                )
            )

            sparse_features = sparse_features_awaitable.wait().wait()

            input_dists = []
            for ctx, embedding, input_dist_params in zip(
                self.ctxs, self.embeddings.values(), self.input_dist_params
            ):
                kjt_param, wkjt_param = input_dist_params
                if kjt_param and wkjt_param:
                    input_dists.append(
                        embedding.input_dist(
                            ctx,
                            sparse_features.id_list_features,
                            sparse_features.id_score_list_features,
                        )
                    )
                elif kjt_param:
                    input_dists.append(
                        embedding.input_dist(ctx, sparse_features.id_list_features)
                    )
                else:
                    input_dists.append(
                        embedding.input_dist(
                            ctx, sparse_features.id_score_list_features
                        )
                    )
            return SparseFeaturesListAwaitable(input_dists)

    def compute(
        self, ctx: ShardedModuleContext, dist_input: SparseFeaturesList
    ) -> torch.Tensor:

        if self.embeddings:
            output = torch.cat(
                [
                    interaction(embedding.compute_and_output_dist(ctx, _input))
                    for ctx, embedding, interaction, _input in zip(
                        self.ctxs,
                        self.embeddings.values(),
                        self.interactions.values(),
                        dist_input,
                    )
                ],
                dim=1,
            )

        else:
            output = torch.empty(
                [self._cross_pg_global_batch_size, 0],
                device=self._device,
                requires_grad=True,
            )

        return output

    def _create_output_dist(
        self, ctx: ShardedModuleContext, output: torch.Tensor
    ) -> None:
        # Determine the output_dist splits and the all_to_all output size
        assert len(output.shape) == 2
        local_dim_sum = torch.tensor(
            [
                output.shape[1],
            ],
            dtype=torch.int64,
            device=self._device,
        )
        dim_sum_per_rank = [
            torch.zeros(
                1,
                dtype=torch.int64,
                device=self._device,
            )
            for i in range(dist.get_world_size(self._cross_pg))
        ]
        dist.all_gather(
            dim_sum_per_rank,
            local_dim_sum,
            group=self._cross_pg,
        )
        dim_sum_per_rank = [x.item() for x in dim_sum_per_rank]
        self._output_dist = PooledEmbeddingsAllToAll(
            pg=self._cross_pg, dim_sum_per_rank=dim_sum_per_rank, device=self._device
        )

    def output_dist(
        self, ctx: ShardedModuleContext, output: torch.Tensor
    ) -> LazyAwaitable[torch.Tensor]:
        if self._has_uninitialized_output_dist:
            self._create_output_dist(ctx, output)
            self._has_uninitialized_output_dist = False
        # pyre-ignore [29]
        return TowerLazyAwaitable(self._output_dist(output))

    # pyre-ignore [14]
    def state_dict(
        self,
        destination: Optional[Dict[str, Any]] = None,
        prefix: str = "",
        keep_vars: bool = False,
    ) -> Dict[str, Any]:
        if destination is None:
            destination = OrderedDict()
            # pyre-ignore [16]
            destination._metadata = OrderedDict()
        for i, embedding in self.embeddings.items():
            embedding.state_dict(
                destination, prefix + f"towers.{i}.embedding.", keep_vars
            )
        for i, interaction in self.interactions.items():
            interaction.module.state_dict(
                destination, prefix + f"towers.{i}.interaction.", keep_vars
            )
        return destination

    @property
    def fused_optimizer(self) -> KeyedOptimizer:
        return CombinedOptimizer(
            [
                (name, embedding.fused_optimizer)
                for name, embedding in self.embeddings.items()
            ]
        )

    def named_parameters(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, nn.Parameter]]:
        for i, embedding in self.embeddings.items():
            yield from (
                embedding.named_parameters(
                    append_prefix(prefix, f"towers.{i}.embedding"), recurse
                )
            )
        for i, interaction in self.interactions.items():
            yield from (
                interaction.module.named_parameters(
                    append_prefix(prefix, f"towers.{i}.interaction"), recurse
                )
            )

    def named_buffers(
        self, prefix: str = "", recurse: bool = True, remove_duplicate: bool = True
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        for i, embedding in self.embeddings.items():
            yield from (
                embedding.named_buffers(
                    append_prefix(prefix, f"towers.{i}.embedding"), recurse
                )
            )
        for i, interaction in self.interactions.items():
            yield from (
                interaction.module.named_buffers(
                    append_prefix(prefix, f"towers.{i}.interaction"), recurse
                )
            )

    def sparse_grad_parameter_names(
        self,
        destination: Optional[List[str]] = None,
        prefix: str = "",
    ) -> List[str]:
        destination = [] if destination is None else destination
        for i, embedding in self.embeddings.items():
            embedding.sparse_grad_parameter_names(
                destination, append_prefix(prefix, f"towers.{i}.embedding")
            )
        return destination

    def sharded_parameter_names(self, prefix: str = "") -> Iterator[str]:
        for i, embedding in self.embeddings.items():
            yield from (
                embedding.sharded_parameter_names(
                    append_prefix(prefix, f"towers.{i}.embedding")
                )
            )
        for i, interaction in self.interactions.items():
            yield from (
                key
                for key, _ in interaction.module.named_parameters(
                    append_prefix(prefix, f"towers.{i}.interaction")
                )
            )

    def named_modules(
        self,
        memo: Optional[Set[nn.Module]] = None,
        prefix: str = "",
        remove_duplicate: bool = True,
    ) -> Iterator[Tuple[str, nn.Module]]:
        yield from [(prefix, self)]


class EmbeddingTowerSharder(BaseEmbeddingSharder[EmbeddingTower]):
    def shard(
        self,
        module: EmbeddingTower,
        params: Dict[str, ParameterSharding],
        env: ShardingEnv,
        device: Optional[torch.device] = None,
    ) -> ShardedEmbeddingTower:
        kjt_features, wkjt_features = self.embedding_feature_names(module)

        return ShardedEmbeddingTower(
            module=module,
            table_name_to_parameter_sharding=params,
            embedding_sharder=self.embedding_sharder(module),
            kjt_features=kjt_features,
            wkjt_features=wkjt_features,
            env=env,
            fused_params=self.fused_params,
            device=device,
        )

    def sharding_types(self, compute_device_type: str) -> List[str]:
        """
        List of supported sharding types. See ShardingType for well-known examples.
        """
        return [
            ShardingType.TABLE_ROW_WISE.value,
            ShardingType.TABLE_COLUMN_WISE.value,
        ]

    def shardable_parameters(self, module: EmbeddingTower) -> Dict[str, nn.Parameter]:
        """
        List of parameters, which can be sharded.
        """
        return self.embedding_sharder(module).shardable_parameters(module.embedding)

    @property
    def module_type(self) -> Type[EmbeddingTower]:
        return EmbeddingTower

    def embedding_sharder(
        self, module: EmbeddingTower
    ) -> BaseEmbeddingSharder[nn.Module]:
        embedding: nn.Module = module.embedding
        if isinstance(embedding, EmbeddingBagCollection):
            # pyre-ignore [7]
            return EmbeddingBagCollectionSharder(self.fused_params)
        elif isinstance(embedding, EmbeddingCollection):
            # pyre-ignore [7]
            return EmbeddingCollectionSharder(self.fused_params)
        else:
            raise RuntimeError(f"Unsupported embedding type: {type(module)}")

    def embedding_feature_names(
        self, module: EmbeddingTower
    ) -> Tuple[List[str], List[str]]:
        embedding: nn.Module = module.embedding
        if not (
            isinstance(embedding, EmbeddingBagCollection)
            or isinstance(embedding, EmbeddingCollection)
        ):
            raise RuntimeError(f"unsupported embedding type: {type(module)}")

        kjt_features: List[str] = []
        wkjt_features: List[str] = []
        configs = []

        weighted = False
        if isinstance(embedding, EmbeddingBagCollection):
            configs = embedding.embedding_bag_configs
            weighted = embedding.is_weighted
        elif isinstance(embedding, EmbeddingCollection):
            configs = embedding.embedding_configs

        for config in configs:
            if getattr(config, "weighted", weighted):
                wkjt_features.extend(config.feature_names)
            else:
                kjt_features.extend(config.feature_names)
        return kjt_features, wkjt_features


class EmbeddingTowerCollectionSharder(BaseEmbeddingSharder[EmbeddingTowerCollection]):
    def __init__(self, fused_params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        self._tower_sharder = EmbeddingTowerSharder(self.fused_params)

    def shard(
        self,
        module: EmbeddingTowerCollection,
        params: Dict[str, ParameterSharding],
        env: ShardingEnv,
        device: Optional[torch.device] = None,
    ) -> ShardedEmbeddingTowerCollection:

        return ShardedEmbeddingTowerCollection(
            module=module,
            table_name_to_parameter_sharding=params,
            tower_sharder=self._tower_sharder,
            env=env,
            fused_params=self.fused_params,
            device=device,
        )

    def sharding_types(self, compute_device_type: str) -> List[str]:
        """
        List of supported sharding types. See ShardingType for well-known examples.
        """
        return [
            ShardingType.TABLE_ROW_WISE.value,
            ShardingType.TABLE_COLUMN_WISE.value,
        ]

    def shardable_parameters(
        self, module: EmbeddingTowerCollection
    ) -> Dict[str, nn.Parameter]:
        """
        List of parameters, which can be sharded.
        """

        named_parameters: Dict[str, nn.Parameter] = {}
        for tower in module.towers:
            named_parameters.update(self._tower_sharder.shardable_parameters(tower))
        return named_parameters

    @property
    def module_type(self) -> Type[EmbeddingTowerCollection]:
        return EmbeddingTowerCollection
