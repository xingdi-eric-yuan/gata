import os
import torch.nn as nn
import pytorch_lightning as pl
import gym

from typing import Optional
from textworld import EnvInfos

from utils import load_textworld_games
from layers import WordNodeRelInitMixin
from action_selector import ActionSelector
from graph_updater import GraphUpdater


def request_infos_for_train() -> EnvInfos:
    request_infos = EnvInfos()
    request_infos.admissible_commands = True
    request_infos.description = False
    request_infos.location = False
    request_infos.facts = False
    request_infos.last_action = False
    request_infos.game = True

    return request_infos


def request_infos_for_eval() -> EnvInfos:
    request_infos = EnvInfos()
    request_infos.admissible_commands = True
    request_infos.description = True
    request_infos.location = True
    request_infos.facts = True
    request_infos.last_action = True
    request_infos.game = True
    return request_infos


def get_game_dir(
    base_dir_path: str,
    dataset: str,
    difficulty_level: int,
    training_size: Optional[int] = None,
) -> str:
    return os.path.join(
        base_dir_path,
        dataset + ("" if training_size is None else f"_{training_size}"),
        f"difficulty_level_{difficulty_level}",
    )


class GATADoubleDQN(WordNodeRelInitMixin, pl.LightningModule):
    def __init__(
        self,
        difficulty_level: int = 1,
        training_size: int = 1,
        max_episode_steps: int = 100,
        game_batch_size: int = 25,
        hidden_dim: int = 8,
        word_emb_dim: int = 300,
        node_emb_dim: int = 12,
        relation_emb_dim: int = 10,
        text_encoder_num_blocks: int = 1,
        text_encoder_num_conv_layers: int = 3,
        text_encoder_kernel_size: int = 5,
        text_encoder_num_heads: int = 1,
        graph_encoder_num_cov_layers: int = 4,
        graph_encoder_num_bases: int = 3,
        action_scorer_num_heads: int = 1,
        epsilon_anneal_from: float = 1.0,
        epsilon_anneal_to: float = 0.1,
        epsilon_anneal_episodes: float = 20000,
        word_vocab_path: Optional[str] = None,
        node_vocab_path: Optional[str] = None,
        relation_vocab_path: Optional[str] = None,
        pretrained_graph_updater: Optional[GraphUpdater] = None,
        train_env: Optional[gym.Env] = None,
        val_env: Optional[gym.Env] = None,
        test_env: Optional[gym.Env] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        self.save_hyperparameters(
            "difficulty_level",
            "training_size",
            "max_episode_steps",
            "game_batch_size",
            "hidden_dim",
            "word_emb_dim",
            "node_emb_dim",
            "relation_emb_dim",
            "text_encoder_num_blocks",
            "text_encoder_num_conv_layers",
            "text_encoder_kernel_size",
            "text_encoder_num_heads",
            "graph_encoder_num_cov_layers",
            "graph_encoder_num_bases",
            "action_scorer_num_heads",
            "epsilon_anneal_from",
            "epsilon_anneal_to",
            "epsilon_anneal_episodes",
        )

        # load envs
        if train_env is None:
            # load the test rl data
            self.train_env = load_textworld_games(
                "test-data/rl_games",
                "train",
                request_infos_for_train(),
                max_episode_steps,
                game_batch_size,
            )
        else:
            self.train_env = train_env
        if val_env is None:
            # load the test rl data
            self.val_env = load_textworld_games(
                "test-data/rl_games",
                "val",
                request_infos_for_eval(),
                max_episode_steps,
                game_batch_size,
            )
        else:
            self.val_env = val_env
        if test_env is None:
            # load the test rl data
            self.test_env = load_textworld_games(
                "test-data/rl_games",
                "test",
                request_infos_for_eval(),
                max_episode_steps,
                game_batch_size,
            )
        else:
            self.test_env = test_env

        # initialize word (preprocessor), node and relation stuff
        (
            node_name_word_ids,
            node_name_mask,
            rel_name_word_ids,
            rel_name_mask,
        ) = self.init_word_node_rel(
            word_vocab_path=word_vocab_path,
            node_vocab_path=node_vocab_path,
            relation_vocab_path=relation_vocab_path,
        )

        # main action selector
        self.action_selector = ActionSelector(
            hidden_dim,
            self.num_words,
            word_emb_dim,
            self.num_nodes,
            node_emb_dim,
            self.num_relations,
            relation_emb_dim,
            text_encoder_num_blocks,
            text_encoder_num_conv_layers,
            text_encoder_kernel_size,
            text_encoder_num_heads,
            graph_encoder_num_cov_layers,
            graph_encoder_num_bases,
            action_scorer_num_heads,
            node_name_word_ids,
            node_name_mask,
            rel_name_word_ids,
            rel_name_mask,
        )

        # target action selector
        self.target_action_selector = ActionSelector(
            hidden_dim,
            self.num_words,
            word_emb_dim,
            self.num_nodes,
            node_emb_dim,
            self.num_relations,
            relation_emb_dim,
            text_encoder_num_blocks,
            text_encoder_num_conv_layers,
            text_encoder_kernel_size,
            text_encoder_num_heads,
            graph_encoder_num_cov_layers,
            graph_encoder_num_bases,
            action_scorer_num_heads,
            node_name_word_ids,
            node_name_mask,
            rel_name_word_ids,
            rel_name_mask,
        )
        # we don't train the target action selector
        for param in self.target_action_selector.parameters():
            param.requires_grad = False
        # update the target action selector weights to those of the main action selector
        self.update_target_action_selector()

        # graph updater
        if pretrained_graph_updater is None:
            self.graph_updater = GraphUpdater(
                hidden_dim,
                word_emb_dim,
                self.num_nodes,
                node_emb_dim,
                self.num_relations,
                relation_emb_dim,
                text_encoder_num_blocks,
                text_encoder_num_conv_layers,
                text_encoder_kernel_size,
                text_encoder_num_heads,
                graph_encoder_num_cov_layers,
                graph_encoder_num_bases,
                nn.Embedding(self.num_words, word_emb_dim),
                node_name_word_ids,
                node_name_mask,
                rel_name_word_ids,
                rel_name_mask,
            )
        else:
            self.graph_updater = pretrained_graph_updater
        # we use graph updater only to get the current graph representations
        self.graph_updater.eval()
        # we don't want to train the graph updater
        for param in self.graph_updater.parameters():
            param.requires_grad = False

    def update_target_action_selector(self) -> None:
        self.target_action_selector.load_state_dict(self.action_selector.state_dict())
